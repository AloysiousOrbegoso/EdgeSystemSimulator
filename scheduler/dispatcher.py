"""
HTTP dispatcher — sends task assignments to edge nodes.

The scheduling tick produces Assignments. The dispatcher converts each
Assignment into a POST /execute call against the assigned node and reacts
to the response:

  201  → task admitted; reservation stays in node_state, completion will
         arrive via the reconcile poller noticing active_task_count drop
  409  → task rejected (insufficient memory / cores / class). The
         scheduler's projection was wrong about this node; release the
         reservation and re-enqueue the task for next tick.
  503  → node inactive. Same handling as 409: release + re-enqueue.
  network error → mark node unreachable, release + re-enqueue.

Dispatch is fire-and-forget from the scheduling tick's perspective. We use
a thread pool so a slow edge cannot stall the tick. httpx is the HTTP
client (already in requirements.txt).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import httpx

from shared.constants import SECONDS_PER_WORKLOAD_UNIT
from shared.models import Assignment, Task

from scheduler.node_state import NodeState
from scheduler.queue import TaskQueue

logger = logging.getLogger(__name__)


# Callback fired when a dispatch result is known. The recorder uses this to
# log task_dispatched / task_rejected events. Signature:
#   (task, assignment, outcome, detail)
# where outcome is one of: "dispatched", "rejected", "node_error".
DispatchCallback = Callable[[Task, Assignment, str, str | None], None]


class Dispatcher:
    """Sends assignments to edge nodes; coordinates with NodeState and
    TaskQueue on rejection / failure paths."""

    def __init__(
        self,
        node_state: NodeState,
        queue: TaskQueue,
        max_workers: int = 8,
        request_timeout_seconds: float = 5.0,
        dispatch_callback: DispatchCallback | None = None,
    ) -> None:
        self._node_state = node_state
        self._queue = queue
        self._timeout = request_timeout_seconds
        self._callback = dispatch_callback

        self._client = httpx.Client(timeout=request_timeout_seconds)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="dispatch"
        )
        self._closed = False
        self._closed_lock = threading.Lock()

    # ---------------------------------------------------------------
    # Public API — called by the scheduling tick
    # ---------------------------------------------------------------

    def dispatch(self, task: Task, assignment: Assignment) -> None:
        """Reserve resources in the mirror and submit the HTTP call on the
        thread pool. Non-blocking for the caller."""
        # Reserve BEFORE the HTTP call so subsequent ticks see the updated
        # mirror. If the edge rejects, release happens in the callback.
        expected_runtime = task.workload_units * SECONDS_PER_WORKLOAD_UNIT
        reserved = self._node_state.reserve(
            node_id=assignment.node_id,
            task_id=task.task_id,
            memory_kb=task.memory_kb,
            cpu_cores_required=task.cpu_cores_required,
            workload_units=task.workload_units,
            expected_runtime_seconds=expected_runtime,
        )
        if not reserved:
            # Mirror refused — algorithm and mirror disagreed (shouldn't
            # happen in normal operation). Re-enqueue the task and bail.
            logger.warning(
                "Mirror refused reservation for task=%s on node=%s; "
                "re-enqueuing",
                task.task_id,
                assignment.node_id,
            )
            self._queue.enqueue(task)
            return

        with self._closed_lock:
            if self._closed:
                # Service shutting down; release and drop.
                self._node_state.release(
                    assignment.node_id, task.task_id, task.workload_units
                )
                return
            self._pool.submit(self._do_dispatch, task, assignment)

    # ---------------------------------------------------------------
    # Worker — runs on the thread pool
    # ---------------------------------------------------------------

    def _do_dispatch(self, task: Task, assignment: Assignment) -> None:
        """Issue the HTTP POST, interpret the response, fire the callback."""
        spec = self._node_state.get_spec(assignment.node_id)
        if spec is None:
            # Node was unregistered between assignment and dispatch.
            self._on_node_error(task, assignment, "node_unregistered")
            return

        url = f"{spec.url}/execute"
        try:
            resp = self._client.post(
                url, json=task.model_dump(mode="json")
            )
        except httpx.RequestError as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Dispatch network error for task=%s to %s: %s",
                task.task_id,
                url,
                error,
            )
            self._on_node_error(task, assignment, error)
            return

        if resp.status_code == 201:
            # Admitted. Reservation stays in place; completion is observed
            # by the reconcile poller when active_task_count drops.
            if self._callback is not None:
                try:
                    self._callback(task, assignment, "dispatched", None)
                except Exception:
                    logger.exception("dispatch_callback raised")
            return

        # Non-success path. Body should be {admitted: false, reason: "..."}.
        try:
            body = resp.json()
            reason = body.get("reason") or f"http_{resp.status_code}"
        except Exception:
            reason = f"http_{resp.status_code}"

        # Release the reservation we made optimistically.
        self._node_state.release(
            assignment.node_id, task.task_id, task.workload_units
        )

        if resp.status_code in (409, 503):
            # Rejected by edge node. Re-enqueue so the next tick can
            # retry on a different node.
            logger.info(
                "Edge %s rejected task=%s reason=%s; re-enqueueing",
                assignment.node_id,
                task.task_id,
                reason,
            )
            self._queue.enqueue(task)
            if self._callback is not None:
                try:
                    self._callback(task, assignment, "rejected", reason)
                except Exception:
                    logger.exception("dispatch_callback raised")
        else:
            # Unexpected status code. Treat as node error.
            logger.warning(
                "Dispatch to %s for task=%s got unexpected HTTP %d: %s",
                url,
                task.task_id,
                resp.status_code,
                reason,
            )
            self._on_node_error(task, assignment, reason)

    def _on_node_error(
        self, task: Task, assignment: Assignment, error: str
    ) -> None:
        """Common handling for network errors and unexpected status codes:
        release the reservation, mark node unreachable, re-enqueue, log."""
        self._node_state.release(
            assignment.node_id, task.task_id, task.workload_units
        )
        self._node_state.mark_unreachable(assignment.node_id, error)
        self._queue.enqueue(task)
        if self._callback is not None:
            try:
                self._callback(task, assignment, "node_error", error)
            except Exception:
                logger.exception("dispatch_callback raised")

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def close(self) -> None:
        """Stop accepting new dispatches and wait for in-flight ones to
        finish. Called from the scheduler service shutdown."""
        with self._closed_lock:
            self._closed = True
        self._pool.shutdown(wait=True)
        self._client.close()