"""
Learned per-node state for adaptive scheduling.

The Adaptive Min-Min algorithm replaces the static completion-time
estimate of plain Min-Min with a dynamic one. Two quantities are learned
per node from observed task completions:

    μ̂_n   service rate, in workload-units per second.
            Updated by EWMA after every completed task:
                μ̂_n ← (1 - α) · μ̂_n + α · (w_t / d_t)

    π_n   network penalty (RTT + failure backoff), in seconds.
            Updated by EWMA when a measurement is provided.
            In our v1 testbed (Docker loopback), π_n is stubbed to 0.0;
            the field is structurally present so a future extension can
            wire in real RTT or tc/netem-injected delays without changing
            the algorithm signature. See ARCHITECTURE.md §13.

The scheduler service maintains one LearnedState instance and:
    - reads from it inside Adaptive Min-Min (via .snapshot())
    - writes to it from the completion-observation path (via
      .update_service_rate() called when a task finishes)

This module has no I/O and is fully thread-safe.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from shared.constants import SECONDS_PER_WORKLOAD_UNIT

logger = logging.getLogger(__name__)


# EWMA defaults from ARCHITECTURE.md §15. Tunable per-instance via the
# LearnedState constructor.
DEFAULT_ALPHA = 0.3
DEFAULT_BETA = 0.3
# Initial μ̂_n: 1.0 / SECONDS_PER_WORKLOAD_UNIT = 10 units/sec, i.e. the
# nominal "all nodes equally fast" prior. EWMA pulls this toward each
# node's measured speed as completions accumulate.
DEFAULT_INITIAL_MU = 1.0 / SECONDS_PER_WORKLOAD_UNIT
# Initial π_n: 0.0 (no measured network delay yet).
DEFAULT_INITIAL_PI = 0.0


@dataclass
class _NodeLearned:
    """Per-node learned state. Internal — exposed via snapshot()."""

    mu: float                                 # service rate (units/sec)
    pi: float                                 # network penalty (seconds)
    n_completions_observed: int = 0           # for diagnostics


@dataclass
class LearnedSnapshot:
    """Read-only view passed into the algorithm.

    The algorithm copies what it needs at the start of each schedule()
    call and never mutates this object.
    """

    mu_by_node: dict[str, float] = field(default_factory=dict)
    pi_by_node: dict[str, float] = field(default_factory=dict)


class LearnedState:
    """Per-node EWMA estimates with thread-safe updates.

    Construct one of these per scheduler service. Initial values are
    applied lazily — the first reference to a node creates its entry
    using DEFAULT_INITIAL_MU and DEFAULT_INITIAL_PI.
    """

    def __init__(
        self,
        alpha: float = DEFAULT_ALPHA,
        beta: float = DEFAULT_BETA,
        initial_mu: float = DEFAULT_INITIAL_MU,
        initial_pi: float = DEFAULT_INITIAL_PI,
    ) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        if not 0.0 < beta <= 1.0:
            raise ValueError(f"beta must be in (0, 1], got {beta}")
        if initial_mu <= 0.0:
            raise ValueError(f"initial_mu must be > 0, got {initial_mu}")

        self._alpha = alpha
        self._beta = beta
        self._initial_mu = initial_mu
        self._initial_pi = initial_pi

        self._lock = threading.Lock()
        self._nodes: dict[str, _NodeLearned] = {}

    # ---------------------------------------------------------------
    # Updates — called from the completion-observation path
    # ---------------------------------------------------------------

    def update_service_rate(
        self,
        node_id: str,
        workload_units: int,
        observed_duration_seconds: float,
    ) -> None:
        """EWMA-update μ̂_n with one completed task's measurement.

            μ̂_n ← (1 - α) · μ̂_n + α · (w_t / d_t)

        Defensive guards:
          - Skips updates for non-positive duration (clock skew, instant
            completions). Such measurements would diverge μ̂.
          - Skips updates for non-positive workload_units (programming bug
            elsewhere).
        """
        if workload_units <= 0 or observed_duration_seconds <= 0.0:
            return
        sample_mu = workload_units / observed_duration_seconds
        with self._lock:
            entry = self._nodes.setdefault(
                node_id,
                _NodeLearned(mu=self._initial_mu, pi=self._initial_pi),
            )
            entry.mu = (1.0 - self._alpha) * entry.mu + self._alpha * sample_mu
            entry.n_completions_observed += 1

    def update_network_penalty(
        self, node_id: str, observed_seconds: float
    ) -> None:
        """EWMA-update π_n with one network-penalty measurement.

            π_n ← (1 - β) · π_n + β · observed

        In the v1 testbed (Docker loopback), nothing calls this in the
        hot path — RTT is sub-millisecond and bypasses signal. The
        method exists so a future extension that measures /status RTT
        or simulates tc/netem delays can wire updates in without
        changing the surrounding code.
        """
        if observed_seconds < 0.0:
            return
        with self._lock:
            entry = self._nodes.setdefault(
                node_id,
                _NodeLearned(mu=self._initial_mu, pi=self._initial_pi),
            )
            entry.pi = (1.0 - self._beta) * entry.pi + self._beta * observed_seconds

    # ---------------------------------------------------------------
    # Reads — called from the algorithm
    # ---------------------------------------------------------------

    def get_mu(self, node_id: str) -> float:
        """Current μ̂_n. Returns the initial prior if the node has never
        been seen — algorithms should call this for any feasible node."""
        with self._lock:
            entry = self._nodes.get(node_id)
            return entry.mu if entry is not None else self._initial_mu

    def get_pi(self, node_id: str) -> float:
        with self._lock:
            entry = self._nodes.get(node_id)
            return entry.pi if entry is not None else self._initial_pi

    def snapshot(self, node_ids: list[str] | None = None) -> LearnedSnapshot:
        """Return a frozen view of μ̂ and π for the given nodes.

        If node_ids is None, returns state for every node currently
        tracked. The algorithm typically passes the list of feasible
        nodes from its NodeView input so the snapshot includes any node
        that hasn't been observed yet (filling in the prior).
        """
        with self._lock:
            if node_ids is None:
                mu = {nid: e.mu for nid, e in self._nodes.items()}
                pi = {nid: e.pi for nid, e in self._nodes.items()}
            else:
                mu = {}
                pi = {}
                for nid in node_ids:
                    entry = self._nodes.get(nid)
                    if entry is not None:
                        mu[nid] = entry.mu
                        pi[nid] = entry.pi
                    else:
                        mu[nid] = self._initial_mu
                        pi[nid] = self._initial_pi
        return LearnedSnapshot(mu_by_node=mu, pi_by_node=pi)

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def reset(self) -> None:
        """Drop all learned state. Called by /trial/reset between trials
        so the second trial doesn't inherit the first's μ̂.

        This is important for the experimental protocol: each trial
        starts adaptive Min-Min from the same prior, so observations are
        properly paired across algorithms."""
        with self._lock:
            self._nodes.clear()

    # ---------------------------------------------------------------
    # Diagnostics — used by /system/state
    # ---------------------------------------------------------------

    def diagnostics(self) -> dict[str, dict[str, float | int]]:
        """Return a JSON-friendly dump for inspection."""
        with self._lock:
            return {
                nid: {
                    "mu": e.mu,
                    "pi": e.pi,
                    "n_completions_observed": e.n_completions_observed,
                }
                for nid, e in self._nodes.items()
            }