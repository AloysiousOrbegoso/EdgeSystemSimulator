"""
Task execution engine for one edge node.

Coordinates the full lifecycle of a task on a node:

    submit -> admit -> simulate work -> release -> record completion

Admission consults both the memory manager and the CPU manager. If either
refuses, the task is rejected. If both accept, the task is launched on a
background thread that sleeps for the simulated runtime and then releases
both resources.

This file deliberately knows nothing about HTTP — it operates purely on
shared.models.Task instances. The FastAPI layer in edgenode.py is a thin
wrapper that converts request bodies to Tasks and calls into here.

Thread-safety: the execution engine takes the memory lock first, then the
CPU lock. Callers should not hold either lock when entering this module.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from shared.constants import SECONDS_PER_WORKLOAD_UNIT
from shared.models import AdmitDecision, Task, TaskClass

from edge.cpu_manager import CpuManager
from edge.memory_manager import MemoryManager

logger = logging.getLogger(__name__)


@dataclass
class _RunningTask:
    """In-flight bookkeeping for one admitted task."""

    task: Task
    admitted_at: float
    runtime_seconds: float
    thread: threading.Thread | None = None
    completed_at: float | None = None


@dataclass
class CompletionRecord:
    """What the engine reports when a task finishes.

    `status` is "completed" on normal completion, or "failed" if the
    background thread crashed before release. The edge node's /status and
    Prometheus metrics consult these records to count completions per class.
    """

    task_id: str
    task_class: TaskClass
    admitted_at: float
    completed_at: float
    duration_seconds: float
    status: str  # "completed" | "failed"
    error: str | None = None


# Type alias for the optional callback the FastAPI layer can register to be
# notified when a task finishes. The scheduler can also poll /status, but a
# webhook-style callback is cheaper and lower-latency.
CompletionCallback = Callable[[CompletionRecord], None]


class ExecutionEngine:
    """Per-node task lifecycle coordinator.

    One instance per edge node. Construct with the node's MemoryManager,
    CpuManager, and the node's NodeSpec.accepted_classes list (the feasibility
    gate that encodes which task classes this node will admit at all).
    """

    def __init__(
        self,
        node_id: str,
        memory: MemoryManager,
        cpu: CpuManager,
        accepted_classes: list[TaskClass],
        is_active_provider: Callable[[], bool] | None = None,
        completion_callback: CompletionCallback | None = None,
    ) -> None:
        self.node_id = node_id
        self._memory = memory
        self._cpu = cpu
        self._accepted_classes = set(accepted_classes)
        # `is_active_provider` is a thunk so the FastAPI layer can flip the
        # node into maintenance mode without rebuilding the engine. None
        # means "always active" (the common case during tests).
        self._is_active_provider = is_active_provider or (lambda: True)
        self._completion_callback = completion_callback

        self._lock = threading.Lock()
        self._running: dict[str, _RunningTask] = {}
        # Bounded history of recent completions, kept so /status and
        # Prometheus exporters can report counts without doing their own
        # bookkeeping. New entries push out the oldest.
        self._completion_history: list[CompletionRecord] = []
        self._history_cap = 1000

    # ---------------------------------------------------------------
    # Admission — the hot path called from POST /execute
    # ---------------------------------------------------------------

    def try_admit(self, task: Task) -> AdmitDecision:
        """Atomically run all admission checks and reserve resources.

        Returns AdmitDecision(admitted=True) and starts the task on a
        background thread on success. On failure, no resources are reserved.

        The check order is: active -> class -> memory -> cores. If any step
        rejects, earlier reservations (memory, in particular) are unwound
        before returning. This is the only place in the codebase where both
        managers are mutated in sequence; everywhere else they are independent.
        """
        if not self._is_active_provider():
            return AdmitDecision(admitted=False, reason="node_inactive")

        if task.task_class not in self._accepted_classes:
            return AdmitDecision(admitted=False, reason="class_not_accepted")

        # Reserve memory first. If memory fails, we never touch the CPU lock.
        if not self._memory.try_admit_for_task(task.task_id, task.memory_kb):
            return AdmitDecision(admitted=False, reason="insufficient_memory")

        # Now reserve CPU cores. If this fails, give the memory back.
        if not self._cpu.try_admit_for_task(task.task_id, task.cpu_cores_required):
            self._memory.release_for_task(task.task_id)
            return AdmitDecision(admitted=False, reason="insufficient_cpu_cores")

        # Both reservations held — record the running task and launch its
        # execution thread. The thread is responsible for releasing both
        # reservations when the simulated work completes.
        runtime = task.workload_units * SECONDS_PER_WORKLOAD_UNIT
        admitted_at = time.time()
        with self._lock:
            if task.task_id in self._running:
                # Defensive: managers refused a duplicate, but if we somehow
                # got here, undo and bail. This should be unreachable.
                self._memory.release_for_task(task.task_id)
                self._cpu.release_for_task(task.task_id)
                return AdmitDecision(
                    admitted=False, reason="duplicate_task_id"
                )
            record = _RunningTask(
                task=task,
                admitted_at=admitted_at,
                runtime_seconds=runtime,
            )
            self._running[task.task_id] = record

        # Start the background thread OUTSIDE the lock so a slow thread
        # start cannot stall other admissions.
        thread = threading.Thread(
            target=self._run_task,
            args=(task.task_id,),
            name=f"task-{task.task_id}",
            daemon=True,
        )
        record.thread = thread
        thread.start()

        return AdmitDecision(admitted=True, reason=None)

    # ---------------------------------------------------------------
    # Background execution
    # ---------------------------------------------------------------

    def _run_task(self, task_id: str) -> None:
        """Sleep for the task's runtime, then release resources.

        Runs on a daemon thread per task. Any exception inside the sleep
        (which should be impossible for time.sleep, but we guard anyway)
        is captured and reported as a failed completion rather than
        crashing the thread silently and leaking resources.
        """
        # Pull the record. If it's gone (reset() while running), exit
        # cleanly without releasing — reset() already cleared the managers.
        with self._lock:
            record = self._running.get(task_id)
            if record is None:
                return
            runtime = record.runtime_seconds
            task = record.task
            admitted_at = record.admitted_at

        error: str | None = None
        try:
            time.sleep(runtime)
        except Exception as exc:  # pragma: no cover — sleep doesn't normally raise
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Task %s on %s raised during sleep: %s",
                task_id,
                self.node_id,
                error,
            )

        completed_at = time.time()
        # Release resources whether the simulated run succeeded or failed.
        self._memory.release_for_task(task_id)
        self._cpu.release_for_task(task_id)

        # Move from _running into completion history under the lock.
        completion = CompletionRecord(
            task_id=task_id,
            task_class=task.task_class,
            admitted_at=admitted_at,
            completed_at=completed_at,
            duration_seconds=completed_at - admitted_at,
            status="failed" if error else "completed",
            error=error,
        )
        with self._lock:
            self._running.pop(task_id, None)
            self._completion_history.append(completion)
            if len(self._completion_history) > self._history_cap:
                # Drop the oldest entries in one slice rather than popping
                # from the front repeatedly.
                drop = len(self._completion_history) - self._history_cap
                self._completion_history = self._completion_history[drop:]

        # Fire the callback OUTSIDE the lock so a slow callback cannot
        # stall completions of other tasks.
        if self._completion_callback is not None:
            try:
                self._completion_callback(completion)
            except Exception as exc:
                logger.exception(
                    "Completion callback for task %s raised: %s", task_id, exc
                )

    # ---------------------------------------------------------------
    # Read-only state, used by /status
    # ---------------------------------------------------------------

    def active_task_count(self) -> int:
        """Number of tasks currently running on the node."""
        with self._lock:
            return len(self._running)

    def running_task_ids(self) -> list[str]:
        """Snapshot of currently-running task IDs (for debugging)."""
        with self._lock:
            return list(self._running.keys())

    def recent_completions(self, limit: int = 100) -> list[CompletionRecord]:
        """Return up to `limit` most-recent completion records, newest last.

        The edge node's Prometheus exporter calls this to count completions
        per class.
        """
        with self._lock:
            if limit >= len(self._completion_history):
                return list(self._completion_history)
            return list(self._completion_history[-limit:])

    # ---------------------------------------------------------------
    # Lifecycle — called by /admin/reset between trials
    # ---------------------------------------------------------------

    def reset(self) -> None:
        """Drop all running tasks and history.

        The background threads will continue running their time.sleep but,
        on wake, find their record gone and exit without releasing (the
        managers are reset() separately by the FastAPI layer in the
        canonical reset order).

        Lock ordering for /admin/reset is: execution -> memory -> cpu. The
        FastAPI handler is responsible for calling each in that order.
        """
        with self._lock:
            self._running.clear()
            self._completion_history.clear()