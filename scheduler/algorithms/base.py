"""
Base interface for pluggable scheduling algorithms.

This module defines the contract that every scheduling algorithm must
implement. The two algorithms in this thesis (plain Min-Min and Adaptive
Min-Min) both subclass `SchedulingAlgorithm` and implement `.schedule()`.
The scheduler service holds whichever algorithm is currently active and
calls its `.schedule()` method on every tick.

The interface is deliberately minimal:
  - one required method (.schedule)
  - one optional hook (.on_node_state_change)
  - a read-only NodeView passed in by the caller

Algorithms must NOT mutate NodeView. They must NOT call back into the
scheduler. They must NOT do I/O. Their job is pure: given a snapshot of
pending tasks and a snapshot of node state, return a list of assignments.

This purity is what makes algorithms unit-testable in isolation — you can
fabricate Tasks and NodeViews in a test and verify the algorithm produces
the expected assignments without booting any edge nodes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from shared.models import Assignment, NodeRuntimeStatus, NodeSpec, Task


class NodeView(BaseModel):
    """The view of a node that an algorithm sees during scheduling.

    Combines static spec, live runtime status, and three projected fields
    that the scheduler maintains via its optimistic mirror:

      - `projected_used_ram_kb` includes in-flight reservations from earlier
        decisions in the current scheduling tick.
      - `projected_used_cores` is the same idea for CPU cores.
      - `projected_completion_time` is when this node next has free capacity
        (used by Min-Min's expected-completion-time calculation).

    Algorithms read these fields and may keep their own local copies during
    a single `schedule()` call (e.g., Min-Min mutates a local dict to track
    progressive reservations within the batch), but they MUST NOT mutate
    the NodeView itself — the scheduler reuses the same instance across
    algorithms within one tick.
    """

    spec: NodeSpec
    status: NodeRuntimeStatus
    projected_used_ram_kb: int = Field(
        ...,
        description="Mirror including in-flight reservations from this tick",
    )
    projected_used_cores: int
    projected_completion_time: float = Field(
        ...,
        description="Wall-clock time at which this node has free capacity again",
    )


class SchedulingAlgorithm(ABC):
    """Abstract base class for all scheduling algorithms.

    Subclasses must set `name` (the registry key) and implement `schedule()`.
    Algorithms are stateless across ticks by default; if you need state
    (e.g., a sliding window of recent placements), keep it in instance
    attributes and update via `on_node_state_change()` or inside
    `schedule()` itself.
    """

    name: str  # set by subclass; used as the algorithm registry key

    @abstractmethod
    def schedule(
        self,
        pending: list[Task],
        nodes: list[NodeView],
        now: float,
    ) -> list[Assignment]:
        """Decide which pending tasks go where, in this scheduling tick.

        Args:
            pending: All tasks currently in the scheduler's queue. The
                algorithm decides which subset to place this tick.
            nodes: All registered edge nodes with their current state.
                Includes nodes that are inactive or unreachable —
                algorithms should filter on `spec.is_active` and
                `status.reachable` before considering a node feasible.
            now: Wall-clock time (epoch seconds) at the start of this
                tick. Used by adaptive algorithms that compute
                time-decayed averages or react to recent saturation.

        Returns:
            A list of Assignments. Each assignment must reference a
            task_id present in `pending` and a node_id present in `nodes`.
            Tasks not assigned this tick remain in the queue and are
            reconsidered next tick (hold-and-retry).

        Notes:
            - Returning an empty list is valid (e.g., when no task can
              fit on any feasible node right now).
            - Returning the same task_id more than once is a bug; the
              scheduler will detect it and log a warning.
            - The algorithm may make a coherent local mirror of node
              state (reserving RAM/cores within the algorithm as it
              picks tasks), but the scheduler will repopulate the real
              mirror on its end based on the returned assignments.
        """
        ...

    def on_node_state_change(self, nodes: list[NodeView]) -> None:
        """Optional hook fired when node state changes meaningfully.

        Default implementation does nothing. Stateful algorithms can
        override this to refresh internal caches (e.g., a saturation
        history window). Called by the scheduler after reconciliation
        polls when at least one node's reachability or saturation flag
        has changed since the last call.
        """
        return None