"""
Scheduler-side mirror of edge node resource state.

The scheduler maintains its own picture of each edge node's resource usage,
called the "projected" state. This mirror exists for one reason: scheduling
decisions are made faster than edge nodes can reply with current state.

Lifecycle of a typical task placement:

    1. Algorithm picks (task t, node n) at scheduling tick.
    2. Scheduler dispatches t to n via HTTP POST /execute.
    3. Edge node admits t and starts running it (background thread).
    4. Eventually edge node finishes t and responds with completion.

If the scheduler waited for step 4 to update its picture of n's resources,
it could not place anything else on n until then. So instead, the scheduler
reserves the resources optimistically at step 1 (in this mirror), dispatches
in step 2, and trusts the projection until either:
  - the edge node confirms completion (release here)
  - the reconcile thread polls /status and corrects drift

`reconcile()` is the safety net: every ~500ms the scheduler polls each edge,
and if the actual state diverges from the projection (e.g., a task failed,
or admin/inject_memory changed something), the projection snaps to reality.

Thread-safety: every public method acquires `self._lock`. Reservations are
serialized so two algorithm passes within the same tick cannot double-book
the same KB or cores.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from shared.models import NodeRuntimeStatus, NodeSpec

from scheduler.algorithms.base import NodeView

logger = logging.getLogger(__name__)


@dataclass
class _NodeRecord:
    """Per-node mirror entry. Internal — not exposed across the public API."""

    spec: NodeSpec
    # Projected resource holds — what the scheduler believes is in flight or
    # already committed to this node, including reservations not yet
    # confirmed by the edge node.
    projected_used_ram_kb: int = 0
    projected_used_cores: int = 0
    # Per-task reservation amounts, so release() can free the exact amount
    # that reserve() took. Without this, a partial release after reconcile
    # could leak resources or under-release.
    reservations: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Sum of remaining workload-units across in-flight tasks. Used by
    # adaptive algorithms to compute current_load_n. Plain Min-Min ignores
    # this field; we maintain it for both algorithms so switching between
    # them at runtime does not require warming up state.
    in_flight_workload_units: int = 0
    # Last live status snapshot from the edge node. Refreshed by
    # reconcile(). NodeView builds its `status` field from here.
    last_status: NodeRuntimeStatus = field(
        default_factory=lambda: NodeRuntimeStatus(reachable=False)
    )
    last_reconcile_at: float = 0.0
    # Earliest wall-clock time at which this node has free capacity again,
    # used by Min-Min's completion-time calculation. Advanced by reserve()
    # via expected runtime, decreased by release() when tasks complete.
    projected_completion_time: float = 0.0


class NodeState:
    """Registry + projected-state mirror for all edge nodes.

    Construct one of these per scheduler service. Add nodes via
    `register_node()` at startup, then use `snapshot_views()` to feed
    algorithms and `reserve()` / `release()` to keep the projection in sync
    with dispatch decisions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, _NodeRecord] = {}

    # ---------------------------------------------------------------
    # Registration — called once at startup per node
    # ---------------------------------------------------------------

    def register_node(self, spec: NodeSpec) -> None:
        """Add or replace a node in the registry.

        Replacement (calling register_node twice with the same node_id) is
        allowed — it discards any existing reservations for that node. The
        scenario runner uses this to swap node profiles between scenarios
        (e.g., switching to the homogeneous configuration for the relevant
        test case) without restarting the scheduler.
        """
        with self._lock:
            self._nodes[spec.node_id] = _NodeRecord(spec=spec)
            logger.info(
                "Registered node %s (RAM=%dKB usable=%dKB cores=%d accepts=%s)",
                spec.node_id,
                spec.total_ram_kb,
                spec.usable_ram_kb,
                spec.cpu_cores,
                [c.value for c in spec.accepted_classes],
            )

    def unregister_node(self, node_id: str) -> None:
        """Remove a node from the registry. No-op if not present."""
        with self._lock:
            self._nodes.pop(node_id, None)

    def list_node_ids(self) -> list[str]:
        with self._lock:
            return list(self._nodes.keys())

    def has_node(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._nodes

    def get_spec(self, node_id: str) -> NodeSpec | None:
        with self._lock:
            record = self._nodes.get(node_id)
            return record.spec if record else None

    # ---------------------------------------------------------------
    # Views — what algorithms consume
    # ---------------------------------------------------------------

    def snapshot_views(self) -> list[NodeView]:
        """Return a list of NodeViews, one per registered node.

        Algorithms call this at the start of each scheduling tick. The
        returned NodeView objects are independent copies — algorithms can
        keep them around for the duration of their .schedule() call without
        worrying about the mirror changing underneath them.
        """
        with self._lock:
            return [self._build_view_locked(record) for record in self._nodes.values()]

    def snapshot_view(self, node_id: str) -> NodeView | None:
        """Return one NodeView, or None if the node is not registered."""
        with self._lock:
            record = self._nodes.get(node_id)
            return self._build_view_locked(record) if record else None

    # ---------------------------------------------------------------
    # Reservation lifecycle — called by the dispatcher
    # ---------------------------------------------------------------

    def reserve(
        self,
        node_id: str,
        task_id: str,
        memory_kb: int,
        cpu_cores_required: int,
        workload_units: int,
        expected_runtime_seconds: float,
    ) -> bool:
        """Reserve resources on a node for an in-flight task.

        Called immediately after the algorithm decides to place `task_id` on
        `node_id` and before (or in parallel with) the HTTP dispatch. The
        projection updates instantly so subsequent placements within the
        same tick see the new state.

        Returns False (and reserves nothing) if:
          - the node is not registered
          - the same task_id is already reserved on this node
          - the projected total would exceed usable RAM or core count

        The caller (dispatcher) treats False as "do not dispatch" and
        leaves the task in the queue for the next tick. In normal operation
        this should not happen because algorithms run their own feasibility
        checks; the guard here is defense in depth.
        """
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                logger.warning(
                    "reserve() for unknown node_id=%s task=%s", node_id, task_id
                )
                return False
            if task_id in record.reservations:
                logger.warning(
                    "reserve() for task=%s already reserved on node=%s",
                    task_id,
                    node_id,
                )
                return False
            new_ram = record.projected_used_ram_kb + memory_kb
            new_cores = record.projected_used_cores + cpu_cores_required
            if new_ram > record.spec.usable_ram_kb:
                logger.warning(
                    "reserve() for task=%s on node=%s would exceed RAM "
                    "(%dKB > %dKB)",
                    task_id,
                    node_id,
                    new_ram,
                    record.spec.usable_ram_kb,
                )
                return False
            if new_cores > record.spec.cpu_cores:
                logger.warning(
                    "reserve() for task=%s on node=%s would exceed cores "
                    "(%d > %d)",
                    task_id,
                    node_id,
                    new_cores,
                    record.spec.cpu_cores,
                )
                return False

            # All checks passed; commit the reservation.
            record.projected_used_ram_kb = new_ram
            record.projected_used_cores = new_cores
            record.reservations[task_id] = (memory_kb, cpu_cores_required)
            record.in_flight_workload_units += workload_units
            # Advance the projected free-time of the node by the new task's
            # expected runtime. If the node was already busy past now, we
            # extend from that future time; otherwise we extend from now.
            now = time.time()
            base = max(record.projected_completion_time, now)
            record.projected_completion_time = base + expected_runtime_seconds
            return True

    def release(
        self,
        node_id: str,
        task_id: str,
        workload_units: int = 0,
    ) -> bool:
        """Release resources held by `task_id` on `node_id`.

        Called when the scheduler observes (via completion callback or
        reconciliation) that a task has finished. Returns True if the
        reservation existed and was released, False otherwise. The False
        case is benign — it just means we already released this task
        through another path (e.g., reconcile preceded the callback).

        `workload_units` matches what was passed to reserve(); used to
        decrement in_flight_workload_units. Default of 0 keeps callers that
        don't track this from getting it wrong — they just stop seeing the
        task in resource accounting, which is the important part.
        """
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                return False
            held = record.reservations.pop(task_id, None)
            if held is None:
                return False
            memory_kb, cores = held
            record.projected_used_ram_kb = max(
                0, record.projected_used_ram_kb - memory_kb
            )
            record.projected_used_cores = max(
                0, record.projected_used_cores - cores
            )
            record.in_flight_workload_units = max(
                0, record.in_flight_workload_units - workload_units
            )
            # We do not roll back projected_completion_time on release.
            # That field is monotonic by design — releasing a task does not
            # mean the node is idle now (other tasks may still be running),
            # and reconcile() will correct it when actual state diverges.
            return True

    # ---------------------------------------------------------------
    # Reconciliation — drift correction from the polling thread
    # ---------------------------------------------------------------

    def reconcile(self, node_id: str, status: NodeRuntimeStatus) -> None:
        """Update last_status from a fresh /status response.

        Called by the scheduler's reconcile thread (~2 Hz). The status is
        stored verbatim for use in NodeView.status. We do NOT overwrite
        projected_used_ram_kb or projected_used_cores from status — those
        reflect what the scheduler has *committed* to dispatching, which
        may legitimately exceed what the edge has *admitted* yet (the
        race window between dispatch decision and edge admission).

        However, if the edge has been observed unreachable, we mark
        last_status accordingly so algorithms can filter the node out via
        NodeView.status.reachable.
        """
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                return
            record.last_status = status
            record.last_reconcile_at = time.time()

    def mark_unreachable(self, node_id: str, error: str) -> None:
        """Force the node's status to reachable=False with an error message.

        Used by the dispatcher when a /execute call returns a network error
        rather than a structured response. Lets algorithms filter the node
        out on the next tick without waiting for the reconcile thread to
        notice.
        """
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                return
            record.last_status = NodeRuntimeStatus(
                reachable=False,
                used_ram_kb=record.last_status.used_ram_kb,
                available_ram_kb=record.last_status.available_ram_kb,
                is_memory_saturated=record.last_status.is_memory_saturated,
                cpu_cores_in_use=record.last_status.cpu_cores_in_use,
                cpu_utilization_pct=record.last_status.cpu_utilization_pct,
                is_cpu_saturated=record.last_status.is_cpu_saturated,
                active_task_count=record.last_status.active_task_count,
                last_seen=record.last_status.last_seen,
                error=error,
            )

    # ---------------------------------------------------------------
    # Lifecycle — called by /trial/reset
    # ---------------------------------------------------------------

    def reset_all(self) -> None:
        """Drop every reservation across every node.

        Called between trials. Does NOT unregister nodes — the registry
        survives the reset; only the projected state is cleared. The
        scheduler is responsible for also calling /admin/reset on each
        edge node so the actual state matches.
        """
        with self._lock:
            for record in self._nodes.values():
                record.projected_used_ram_kb = 0
                record.projected_used_cores = 0
                record.reservations.clear()
                record.in_flight_workload_units = 0
                record.projected_completion_time = 0.0

    # ---------------------------------------------------------------
    # Debugging / inspection
    # ---------------------------------------------------------------

    def in_flight_count(self, node_id: str) -> int:
        """Number of tasks the scheduler thinks are running on this node."""
        with self._lock:
            record = self._nodes.get(node_id)
            return len(record.reservations) if record else 0

    def in_flight_workload(self, node_id: str) -> int:
        """Sum of remaining workload units across in-flight tasks on this
        node. Adaptive Min-Min uses this as one of its inputs to
        current_load_n."""
        with self._lock:
            record = self._nodes.get(node_id)
            return record.in_flight_workload_units if record else 0

    # ---------------------------------------------------------------
    # Internal helpers (assume lock is held)
    # ---------------------------------------------------------------

    def _build_view_locked(self, record: _NodeRecord) -> NodeView:
        """Construct a NodeView from an internal record. Lock must be held."""
        return NodeView(
            spec=record.spec,
            status=record.last_status,
            projected_used_ram_kb=record.projected_used_ram_kb,
            projected_used_cores=record.projected_used_cores,
            projected_completion_time=record.projected_completion_time,
        )