"""
Pending task queue.

Thread-safe holding area for tasks waiting to be scheduled. Task submission
enqueues here; the scheduling tick reads a snapshot, runs the algorithm, and
removes assigned tasks. Unassigned tasks remain for the next tick
(hold-and-retry).

Each task carries an `enqueued_at` timestamp so the scheduler can enforce
TASK_PLACEMENT_TIMEOUT_SECONDS — tasks held longer than that are dropped
and counted toward tasks_rejected.
"""

from __future__ import annotations

import threading
import time

from shared.models import Task


class TaskQueue:
    """FIFO queue of pending tasks with timeout-based eviction.

    Order is insertion order, but algorithms (Min-Min, Adaptive Min-Min)
    don't rely on it — they consider all pending tasks together. The order
    matters only for tie-breaking when two tasks have identical metrics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}        # insertion-ordered (Python 3.7+)
        self._enqueued_at: dict[str, float] = {}

    # ---------------------------------------------------------------
    # Producer side — submission
    # ---------------------------------------------------------------

    def enqueue(self, task: Task) -> bool:
        """Add `task` to the queue. Returns False if a task with the same
        task_id is already pending — duplicate submissions are refused
        rather than silently overwriting."""
        with self._lock:
            if task.task_id in self._tasks:
                return False
            self._tasks[task.task_id] = task
            self._enqueued_at[task.task_id] = time.time()
            return True

    def enqueue_many(self, tasks: list[Task]) -> int:
        """Bulk enqueue. Returns count of tasks actually added (duplicates
        are skipped)."""
        added = 0
        with self._lock:
            now = time.time()
            for task in tasks:
                if task.task_id in self._tasks:
                    continue
                self._tasks[task.task_id] = task
                self._enqueued_at[task.task_id] = now
                added += 1
        return added

    # ---------------------------------------------------------------
    # Consumer side — scheduler tick
    # ---------------------------------------------------------------

    def snapshot(self) -> list[Task]:
        """Return a list copy of all pending tasks. Algorithms operate on
        this snapshot; subsequent enqueue/remove calls do not affect it."""
        with self._lock:
            return list(self._tasks.values())

    def remove(self, task_id: str) -> Task | None:
        """Remove a task by id and return it. Returns None if the task is
        no longer pending (already removed or never existed)."""
        with self._lock:
            self._enqueued_at.pop(task_id, None)
            return self._tasks.pop(task_id, None)

    def remove_many(self, task_ids: list[str]) -> list[Task]:
        """Bulk remove. Returns the actually-removed tasks (skipping any
        that were no longer present)."""
        removed: list[Task] = []
        with self._lock:
            for tid in task_ids:
                self._enqueued_at.pop(tid, None)
                t = self._tasks.pop(tid, None)
                if t is not None:
                    removed.append(t)
        return removed

    # ---------------------------------------------------------------
    # Timeout handling — called by the scheduler tick
    # ---------------------------------------------------------------

    def evict_expired(self, timeout_seconds: float) -> list[Task]:
        """Remove and return tasks that have been pending longer than
        `timeout_seconds`. Caller is responsible for recording these as
        rejected_no_fit in the trial recorder."""
        cutoff = time.time() - timeout_seconds
        evicted: list[Task] = []
        with self._lock:
            stale_ids = [
                tid for tid, ts in self._enqueued_at.items() if ts < cutoff
            ]
            for tid in stale_ids:
                self._enqueued_at.pop(tid, None)
                t = self._tasks.pop(tid, None)
                if t is not None:
                    evicted.append(t)
        return evicted

    # ---------------------------------------------------------------
    # Inspection
    # ---------------------------------------------------------------

    def size(self) -> int:
        with self._lock:
            return len(self._tasks)

    def contains(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def enqueued_at(self, task_id: str) -> float | None:
        """Wall-clock time when `task_id` was enqueued, or None if absent."""
        with self._lock:
            return self._enqueued_at.get(task_id)

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def clear(self) -> list[Task]:
        """Drop all pending tasks and return them. Called by /trial/reset."""
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            self._enqueued_at.clear()
            return tasks