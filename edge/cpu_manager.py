"""
CPU core manager for one edge node.

Tracks how many CPU cores are currently held by running tasks. Provides the
admission-control checks that decide whether a new task can be admitted from
a CPU-availability standpoint.

Parallel in shape to MemoryManager but simpler:
  - cores are integer slots, not a free-form KB amount
  - there is no "usable vs. total" distinction (every core is allocatable)
  - there is no injection concept (the Node Saturation scenario only stresses
    memory, not CPU; if we ever need CPU stress injection, the same pattern
    from MemoryManager translates directly)

Thread-safety: every public method acquires `self._lock`. By convention the
edge node acquires the memory manager's lock before the CPU manager's lock,
to keep lock ordering consistent across the codebase.
"""

from __future__ import annotations

import threading


class CpuManager:
    """Per-node CPU core accounting with admission control.

    Construct one of these per edge node. `cpu_cores` is the total number of
    cores. The saturation threshold is reported but not enforced — saturation
    is an observable signal for adaptive algorithms, not a hard cap.

    Each running task occupies one or more cores for its full execution
    duration. A task with `cpu_cores_required=1` (the default) reserves one
    slot, and the node refuses admission once all slots are full.
    """

    def __init__(
        self,
        cpu_cores: int,
        saturation_threshold_pct: float = 0.80,
    ) -> None:
        if cpu_cores < 1:
            raise ValueError(f"cpu_cores must be >= 1, got {cpu_cores}")
        if not 0.0 <= saturation_threshold_pct <= 1.0:
            raise ValueError(
                f"saturation_threshold_pct must be in [0, 1], "
                f"got {saturation_threshold_pct}"
            )
        self.cpu_cores = cpu_cores
        self.saturation_threshold_pct = saturation_threshold_pct

        self._lock = threading.Lock()
        # Map task_id -> cores held. Lets us release the exact slot count
        # the task was admitted with.
        self._task_holds: dict[str, int] = {}

    # ---------------------------------------------------------------
    # Read-only state, used by /status and Prometheus exporters
    # ---------------------------------------------------------------

    def cores_in_use(self) -> int:
        """Total cores currently held across all running tasks."""
        with self._lock:
            return self._used_locked()

    def available_cores(self) -> int:
        """Cores still free for new tasks."""
        with self._lock:
            return self.cpu_cores - self._used_locked()

    def utilization_pct(self) -> float:
        """Percent utilization in 0–100 range. Returns 0.0 when cpu_cores
        is 1 and no task is running, or 100.0 when fully occupied."""
        with self._lock:
            return (self._used_locked() / self.cpu_cores) * 100.0

    def is_saturated(self) -> bool:
        """True when cores_in_use / cpu_cores >= saturation_threshold_pct.

        Mirrors MemoryManager.is_saturated() so adaptive algorithms can react
        to either resource hitting its threshold. With only 1 core and the
        default 80% threshold this is effectively True whenever any task is
        running, which is correct: a single-core node IS at capacity.
        """
        with self._lock:
            used = self._used_locked()
            return (used / self.cpu_cores) >= self.saturation_threshold_pct

    def snapshot(self) -> dict[str, int | float | bool]:
        """Atomic read of all CPU state. Use this for /status responses
        instead of calling individual accessors, to avoid inconsistent
        reads if a task completes between calls."""
        with self._lock:
            used = self._used_locked()
            utilization = (used / self.cpu_cores) * 100.0
            return {
                "cpu_cores": self.cpu_cores,
                "cpu_cores_in_use": used,
                "cpu_utilization_pct": utilization,
                "is_cpu_saturated": (used / self.cpu_cores)
                >= self.saturation_threshold_pct,
            }

    # ---------------------------------------------------------------
    # Admission and release for tasks
    # ---------------------------------------------------------------

    def can_admit(self, task_id: str, cores_required: int) -> bool:
        """Return True if `cores_required` cores are still free.

        Does not actually reserve anything. Read-only check; the hot path
        uses `try_admit_for_task()` for atomic check-and-reserve.
        """
        if cores_required < 1:
            return False
        with self._lock:
            return self._used_locked() + cores_required <= self.cpu_cores

    def try_admit_for_task(self, task_id: str, cores_required: int) -> bool:
        """Atomically check capacity and reserve `cores_required` cores
        for `task_id`.

        Returns True if the reservation succeeded, False if there was no
        room. On success, the caller MUST eventually call
        `release_for_task(task_id)` exactly once, or the cores leak for
        the lifetime of this CpuManager instance.
        """
        if cores_required < 1:
            return False
        with self._lock:
            if task_id in self._task_holds:
                # Idempotent guard: double-admit is a programming bug. Refuse
                # rather than silently double-charging cores.
                return False
            if self._used_locked() + cores_required > self.cpu_cores:
                return False
            self._task_holds[task_id] = cores_required
            return True

    def release_for_task(self, task_id: str) -> int:
        """Release the cores held by `task_id`. Returns the freed core count.

        Returns 0 (and does not raise) if the task isn't currently held —
        this can happen on a duplicate completion callback, which we tolerate
        rather than crash the edge node.
        """
        with self._lock:
            return self._task_holds.pop(task_id, 0)

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def reset(self) -> None:
        """Drop all task holds. Called by /admin/reset between trials."""
        with self._lock:
            self._task_holds.clear()

    # ---------------------------------------------------------------
    # Internal helpers (assume lock is held)
    # ---------------------------------------------------------------

    def _used_locked(self) -> int:
        """Sum of task holds. Caller must hold self._lock."""
        return sum(self._task_holds.values())