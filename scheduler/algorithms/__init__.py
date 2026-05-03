"""
Algorithm registry.

For this thesis the registry is closed at exactly two entries:
  - "min_min"           — baseline (constructed at import time; stateless)
  - "adaptive_min_min"  — proposed; constructed at scheduler-service
                          startup with a LearnedState reference

Plain Min-Min has no scheduler-side state, so we instantiate it eagerly.
Adaptive Min-Min depends on a LearnedState held by the scheduler service;
it is registered via `register_adaptive(learned_state)` inside the
service's lifespan. Until that runs, only "min_min" appears in
`list_algorithm_names()`.
"""

from __future__ import annotations

from scheduler.algorithms.adaptive_min_min import AdaptiveMinMinAlgorithm
from scheduler.algorithms.base import SchedulingAlgorithm
from scheduler.algorithms.min_min import MinMinAlgorithm
from scheduler.learned_state import LearnedState

# Eagerly registered algorithms (stateless / no service dependencies).
ALGORITHMS: dict[str, SchedulingAlgorithm] = {
    MinMinAlgorithm.name: MinMinAlgorithm(),
}


def register_adaptive(learned_state: LearnedState) -> None:
    """Construct and register Adaptive Min-Min using a LearnedState owned
    by the scheduler service. Called once from the service's lifespan
    after LearnedState has been created.

    Idempotent: re-registering replaces the existing entry. This is
    relied on if the service is hot-reloaded during development."""
    ALGORITHMS[AdaptiveMinMinAlgorithm.name] = AdaptiveMinMinAlgorithm(
        learned_state=learned_state
    )


def get_algorithm(name: str) -> SchedulingAlgorithm:
    """Return the algorithm registered under `name`, or raise KeyError.

    Adaptive Min-Min only resolves after register_adaptive() has run; if
    the service is misconfigured the error message lists what's actually
    available so the user sees the real cause.
    """
    if name not in ALGORITHMS:
        available = ", ".join(sorted(ALGORITHMS.keys()))
        raise KeyError(
            f"Unknown algorithm '{name}'. Available algorithms: {available}"
        )
    return ALGORITHMS[name]


def list_algorithm_names() -> list[str]:
    """Return registered algorithm names, sorted for stable output."""
    return sorted(ALGORITHMS.keys())