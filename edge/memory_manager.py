"""
Memory manager for one edge node.

Tracks how much RAM is currently held by running tasks and any artificially
injected memory blocks (used by the Node Saturation test scenario). Provides
the admission-control checks that decide whether a new task can be admitted.

Thread-safety: every public method acquires `self._lock`. Callers must not
hold any other lock when calling these methods, to keep the lock ordering
simple (memory_manager → cpu_manager is the convention).

Two kinds of held memory are tracked separately:
  - task_held_kb: KB held by currently-running tasks. Released by
    `release_for_task()` when a task completes.
  - injected_kb: KB held by /admin/inject_memory calls. Released
    automatically after the hold duration expires.

`used_ram_kb` (the thing exposed to the scheduler and to admission checks)
is the sum of both.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _InjectedBlock:
    """One outstanding memory injection. Released when expires_at is reached."""

    kb: int
    expires_at: float


class MemoryManager:
    """Per-node RAM accounting with admission control.

    Construct one of these per edge node. `total_ram_kb` is the node's full
    physical RAM (e.g., 128 KB for edge2). `usable_ram_kb` is the fraction
    available for tasks (e.g., 90 KB after the 70% allocatable rule). The
    saturation threshold is reported but not enforced — saturation is an
    observable signal for adaptive algorithms, not a hard cap.
    """

    def __init__(
        self,
        total_ram_kb: int,
        usable_ram_kb: int,
        saturation_threshold_kb: int,
    ) -> None:
        if usable_ram_kb > total_ram_kb:
            raise ValueError(
                f"usable_ram_kb ({usable_ram_kb}) cannot exceed "
                f"total_ram_kb ({total_ram_kb})"
            )
        self.total_ram_kb = total_ram_kb
        self.usable_ram_kb = usable_ram_kb
        self.saturation_threshold_kb = saturation_threshold_kb

        self._lock = threading.Lock()
        # Map task_id -> kb held. Lets us release the exact amount the task
        # was admitted with, even if classification logic changes later.
        self._task_holds: dict[str, int] = {}
        self._injected_blocks: list[_InjectedBlock] = []

    # ---------------------------------------------------------------
    # Read-only state, used by /status and Prometheus exporters
    # ---------------------------------------------------------------

    def used_ram_kb(self) -> int:
        """Total KB currently held (tasks + active injections)."""
        with self._lock:
            self._reap_expired_injections_locked()
            return self._used_locked()

    def available_ram_kb(self) -> int:
        """KB still allocatable to new tasks under the usable cap."""
        with self._lock:
            self._reap_expired_injections_locked()
            return max(0, self.usable_ram_kb - self._used_locked())

    def is_saturated(self) -> bool:
        """True when used_ram_kb >= saturation_threshold_kb (the 80% line).

        This signal is what an adaptive algorithm reacts to. It is intentionally
        defined against total_ram_kb, not usable_ram_kb, because the thesis
        threshold is 80% of total RAM.
        """
        with self._lock:
            self._reap_expired_injections_locked()
            return self._used_locked() >= self.saturation_threshold_kb

    def snapshot(self) -> dict[str, int | bool]:
        """Atomic read of all memory state. Use this for /status responses
        instead of calling the individual accessors, to avoid inconsistent
        reads if a task completes between calls."""
        with self._lock:
            self._reap_expired_injections_locked()
            used = self._used_locked()
            return {
                "total_ram_kb": self.total_ram_kb,
                "usable_ram_kb": self.usable_ram_kb,
                "used_ram_kb": used,
                "available_ram_kb": max(0, self.usable_ram_kb - used),
                "is_memory_saturated": used >= self.saturation_threshold_kb,
            }

    # ---------------------------------------------------------------
    # Admission and release for tasks
    # ---------------------------------------------------------------

    def can_admit(self, task_id: str, memory_kb: int) -> bool:
        """Return True if `memory_kb` would still fit under usable_ram_kb.

        Does not actually reserve anything — call `try_admit_for_task()` for
        an atomic check-and-reserve. This method exists for read-only checks
        (e.g., the scheduler asking the edge node directly), but in the hot
        path we use try_admit_for_task to avoid TOCTOU bugs.
        """
        if memory_kb <= 0:
            return False
        with self._lock:
            self._reap_expired_injections_locked()
            return self._used_locked() + memory_kb <= self.usable_ram_kb

    def try_admit_for_task(self, task_id: str, memory_kb: int) -> bool:
        """Atomically check capacity and reserve `memory_kb` for `task_id`.

        Returns True if the reservation succeeded, False if there was no
        room. On success, the caller MUST eventually call
        `release_for_task(task_id)` exactly once, or the memory leaks for
        the lifetime of this MemoryManager instance.
        """
        if memory_kb <= 0:
            return False
        with self._lock:
            self._reap_expired_injections_locked()
            if task_id in self._task_holds:
                # Idempotent guard: double-admit is a programming bug. Refuse
                # rather than silently double-charging the node.
                return False
            if self._used_locked() + memory_kb > self.usable_ram_kb:
                return False
            self._task_holds[task_id] = memory_kb
            return True

    def release_for_task(self, task_id: str) -> int:
        """Release the memory held by `task_id`. Returns the freed KB.

        Returns 0 (and does not raise) if the task isn't currently held —
        this can happen on a duplicate completion callback, which we tolerate
        rather than crash the edge node.
        """
        with self._lock:
            return self._task_holds.pop(task_id, 0)

    # ---------------------------------------------------------------
    # Memory injection — used by /admin/inject_memory
    # ---------------------------------------------------------------

    def inject(self, kb: int, hold_seconds: float) -> None:
        """Reserve `kb` KB of phantom memory for `hold_seconds` seconds.

        Used by the Node Saturation test scenario to artificially drive a
        node toward 80% utilization without submitting tasks. The injection
        counts toward used_ram_kb and is_saturated() but does NOT enforce
        the usable_ram_kb cap — injections can intentionally push a node
        above its normal capacity to simulate stress conditions.
        """
        if kb <= 0 or hold_seconds <= 0:
            return
        with self._lock:
            self._injected_blocks.append(
                _InjectedBlock(kb=kb, expires_at=time.time() + hold_seconds)
            )

    def clear_injections(self) -> None:
        """Drop all active injections immediately. Used by /admin/reset
        between trials so that injected memory from one trial does not
        leak into the next."""
        with self._lock:
            self._injected_blocks.clear()

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def reset(self) -> None:
        """Drop all task holds and injections. Called by /admin/reset."""
        with self._lock:
            self._task_holds.clear()
            self._injected_blocks.clear()

    # ---------------------------------------------------------------
    # Internal helpers (assume lock is held)
    # ---------------------------------------------------------------

    def _used_locked(self) -> int:
        """Sum of task and injection holds. Caller must hold self._lock."""
        return sum(self._task_holds.values()) + sum(
            block.kb for block in self._injected_blocks
        )

    def _reap_expired_injections_locked(self) -> None:
        """Drop any injections whose expires_at has passed.

        Called at the top of every read so that snapshot()/used_ram_kb()
        return values that reflect real-time state, even if no other action
        has happened recently to trigger a sweep.
        """
        if not self._injected_blocks:
            return
        now = time.time()
        self._injected_blocks = [
            block for block in self._injected_blocks if block.expires_at > now
        ]