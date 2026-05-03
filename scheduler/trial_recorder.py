"""
Trial recorder — captures per-trial timestamps, events, and metrics.

Every trial run produces two artifacts:
  1. A JSONL event log at results/events/<run_id>.jsonl with fine-grained
     timestamps for replay, debugging, and time-series plot regeneration.
  2. One row in results/runs.csv (a serialized TrialResult) summarizing the
     trial. This row is the Friedman input.

The recorder is owned by the scheduler service. It exposes:
  - start_trial() / end_trial() — lifecycle
  - record_*() — called from the dispatch path, completion callback,
    1Hz sampling thread, and the algorithm tick

Thread-safety: the recorder serializes writes to its event log and to the
CSV file via internal locks. It is safe to call record_*() from multiple
threads concurrently (dispatch worker, tick thread, sampling thread).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from shared.models import Task, TaskClass, TrialResult

from scheduler.node_state import NodeState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory trial state — only what's needed to compute TrialResult at end.
# ---------------------------------------------------------------------------


@dataclass
class _TaskRecord:
    """Per-task bookkeeping during a trial."""

    task: Task
    submitted_at: float
    dispatched_at: float | None = None
    started_at: float | None = None        # set by completion observation
    completed_at: float | None = None
    node_id: str | None = None
    status: str = "pending"                # dispatched | completed | rejected | failed
    rejection_reason: str | None = None


@dataclass
class _NodeMemorySample:
    """One 1Hz memory sample for a node during the trial."""

    t: float                                # seconds since trial start
    used_kb: int
    total_kb: int


@dataclass
class _NodeCpuSample:
    t: float
    cores_in_use: int
    cores_total: int


@dataclass
class _DistributionSample:
    """Snapshot of running-task counts across nodes at one sample tick."""

    t: float
    running: dict[str, int]                 # node_id -> count


@dataclass
class _SchedulingDecision:
    """One algorithm.schedule() call's wall-clock cost and output size."""

    decision_ms: float
    pending_size: int
    assignments_made: int


@dataclass
class _ActiveTrial:
    run_id: str
    scenario_name: str
    algorithm: str
    trial_index: int
    comparison_id: str
    seed: int
    started_at: float
    event_log_path: Path
    event_log_rel_path: str

    # Per-task lifecycle
    tasks: dict[str, _TaskRecord] = field(default_factory=dict)

    # 1Hz samples
    memory_samples: dict[str, list[_NodeMemorySample]] = field(default_factory=dict)
    cpu_samples: dict[str, list[_NodeCpuSample]] = field(default_factory=dict)
    distribution_samples: list[_DistributionSample] = field(default_factory=list)

    # Per-decision overhead samples
    decisions: list[_SchedulingDecision] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TrialRecorder
# ---------------------------------------------------------------------------


class TrialRecorder:
    """Owns the active trial (if any) and writes its outputs.

    At most one trial can be active at a time on a single scheduler. The
    scenario runner enforces serial execution.
    """

    def __init__(
        self,
        results_dir: Path | str = "results",
    ) -> None:
        self._results_dir = Path(results_dir)
        self._events_dir = self._results_dir / "events"
        self._runs_csv = self._results_dir / "runs.csv"

        self._lock = threading.Lock()
        self._event_log_lock = threading.Lock()
        self._csv_lock = threading.Lock()

        self._active: _ActiveTrial | None = None
        self._event_file = None  # type: ignore[assignment]

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def start_trial(
        self,
        run_id: str,
        scenario_name: str,
        algorithm: str,
        trial_index: int,
        comparison_id: str,
        seed: int,
    ) -> None:
        """Open a new trial. Creates the event log file and resets state.
        Raises RuntimeError if a trial is already active."""
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    f"Cannot start trial {run_id}: trial "
                    f"{self._active.run_id} is still active"
                )
            self._events_dir.mkdir(parents=True, exist_ok=True)
            event_log_rel = f"events/{run_id}.jsonl"
            event_log_path = self._results_dir / event_log_rel
            self._event_file = event_log_path.open("w", encoding="utf-8")

            self._active = _ActiveTrial(
                run_id=run_id,
                scenario_name=scenario_name,
                algorithm=algorithm,
                trial_index=trial_index,
                comparison_id=comparison_id,
                seed=seed,
                started_at=time.time(),
                event_log_path=event_log_path,
                event_log_rel_path=event_log_rel,
            )

        self._emit_event(
            "trial_start",
            scenario=scenario_name,
            algorithm=algorithm,
            trial_index=trial_index,
            comparison_id=comparison_id,
            seed=seed,
        )
        logger.info("Trial started: %s (%s, %s)", run_id, scenario_name, algorithm)

    def end_trial(self) -> TrialResult:
        """Finalize the active trial. Computes TrialResult, writes the CSV
        row, closes the event log. Returns the TrialResult."""
        with self._lock:
            if self._active is None:
                raise RuntimeError("No active trial to end")
            active = self._active
            completed_at = time.time()

        # Compute the metrics from collected state.
        result = self._compute_trial_result(active, completed_at)

        # Emit the trial_end event (includes the headline numbers).
        self._emit_event(
            "trial_end",
            makespan=result.makespan_seconds,
            avg_response_time=result.avg_response_time_seconds,
            tasks_completed=result.tasks_completed,
            tasks_rejected=result.tasks_rejected,
        )

        # Append CSV row.
        self._append_runs_csv(result)

        # Close event log.
        with self._lock:
            if self._event_file is not None:
                self._event_file.close()
                self._event_file = None
            self._active = None

        logger.info(
            "Trial ended: %s makespan=%.2fs avg_rt=%.2fs completed=%d/%d",
            result.run_id,
            result.makespan_seconds,
            result.avg_response_time_seconds,
            result.tasks_completed,
            result.tasks_total,
        )
        return result

    def is_active(self) -> bool:
        with self._lock:
            return self._active is not None

    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active.run_id if self._active else None

    # ---------------------------------------------------------------
    # Event recording — called from various scheduler paths
    # ---------------------------------------------------------------

    def record_task_submitted(self, task: Task) -> None:
        with self._lock:
            if self._active is None:
                return
            self._active.tasks[task.task_id] = _TaskRecord(
                task=task, submitted_at=task.submitted_at
            )
        self._emit_event(
            "task_submitted",
            task_id=task.task_id,
            task_class=task.task_class.value,
            memory_kb=task.memory_kb,
            workload_units=task.workload_units,
            cpu_cores_required=task.cpu_cores_required,
        )

    def record_scheduling_decision(
        self, decision_ms: float, pending_size: int, assignments_made: int
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            self._active.decisions.append(
                _SchedulingDecision(decision_ms, pending_size, assignments_made)
            )
        self._emit_event(
            "scheduling_decision",
            decision_ms=decision_ms,
            pending_size=pending_size,
            assignments_made=assignments_made,
        )

    def record_task_dispatched(self, task: Task, node_id: str) -> None:
        with self._lock:
            if self._active is None:
                return
            rec = self._active.tasks.get(task.task_id)
            if rec is None:
                # Submission wasn't recorded by this trial; create the
                # record now so dispatch is captured at minimum.
                rec = _TaskRecord(task=task, submitted_at=task.submitted_at)
                self._active.tasks[task.task_id] = rec
            rec.dispatched_at = time.time()
            rec.node_id = node_id
            rec.status = "dispatched"
        self._emit_event(
            "task_dispatched", task_id=task.task_id, node_id=node_id
        )

    def record_task_rejected(
        self, task: Task, node_id: str, reason: str
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            rec = self._active.tasks.get(task.task_id)
            if rec is not None:
                rec.status = "rejected"
                rec.rejection_reason = reason
                rec.node_id = node_id
        self._emit_event(
            "task_rejected", task_id=task.task_id, node_id=node_id, reason=reason
        )

    def record_task_completed(
        self, task_id: str, node_id: str, duration_seconds: float
    ) -> None:
        completed_at = time.time()
        started_at = completed_at - duration_seconds
        with self._lock:
            if self._active is None:
                return
            rec = self._active.tasks.get(task_id)
            if rec is not None:
                rec.started_at = started_at
                rec.completed_at = completed_at
                rec.status = "completed"
                if rec.node_id is None:
                    rec.node_id = node_id
        self._emit_event(
            "task_completed",
            task_id=task_id,
            node_id=node_id,
            duration=duration_seconds,
        )

    def record_task_evicted(self, task: Task, reason: str = "placement_timeout") -> None:
        """Task was evicted from the queue without ever being placed."""
        with self._lock:
            if self._active is None:
                return
            rec = self._active.tasks.get(task.task_id)
            if rec is not None:
                rec.status = "rejected"
                rec.rejection_reason = reason
        self._emit_event(
            "task_evicted", task_id=task.task_id, reason=reason
        )

    # ---------------------------------------------------------------
    # 1Hz sampling — called by the sampling thread
    # ---------------------------------------------------------------

    def record_memory_sample(
        self, node_id: str, used_kb: int, total_kb: int
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            t = time.time() - self._active.started_at
            self._active.memory_samples.setdefault(node_id, []).append(
                _NodeMemorySample(t=t, used_kb=used_kb, total_kb=total_kb)
            )
        pct = (used_kb / total_kb * 100.0) if total_kb > 0 else 0.0
        self._emit_event(
            "memory_sample",
            node_id=node_id,
            used_kb=used_kb,
            total_kb=total_kb,
            pct=round(pct, 2),
        )

    def record_cpu_sample(
        self, node_id: str, cores_in_use: int, cores_total: int
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            t = time.time() - self._active.started_at
            self._active.cpu_samples.setdefault(node_id, []).append(
                _NodeCpuSample(t=t, cores_in_use=cores_in_use, cores_total=cores_total)
            )
        pct = (cores_in_use / cores_total * 100.0) if cores_total > 0 else 0.0
        self._emit_event(
            "cpu_sample",
            node_id=node_id,
            cores_in_use=cores_in_use,
            cores_total=cores_total,
            pct=round(pct, 2),
        )

    def record_distribution_sample(self, running: dict[str, int]) -> None:
        with self._lock:
            if self._active is None:
                return
            t = time.time() - self._active.started_at
            self._active.distribution_samples.append(
                _DistributionSample(t=t, running=dict(running))
            )
        self._emit_event("distribution_sample", running=running)

    # ---------------------------------------------------------------
    # Internal: event log
    # ---------------------------------------------------------------

    def _emit_event(self, event_type: str, **payload) -> None:
        """Write one JSONL line. Safe to call from any thread."""
        with self._event_log_lock:
            if self._event_file is None or self._active is None:
                return
            t = time.time() - self._active.started_at
            line = json.dumps({"t": round(t, 4), "type": event_type, **payload})
            self._event_file.write(line + "\n")
            self._event_file.flush()

    # ---------------------------------------------------------------
    # Internal: TrialResult computation
    # ---------------------------------------------------------------

    def _compute_trial_result(
        self, active: _ActiveTrial, completed_at: float
    ) -> TrialResult:
        """Aggregate the in-memory samples into the wide TrialResult row."""
        tasks = list(active.tasks.values())
        completed_tasks = [t for t in tasks if t.status == "completed" and t.completed_at]
        rejected_tasks = [t for t in tasks if t.status == "rejected"]
        failed_tasks = [t for t in tasks if t.status == "failed"]

        # --- RQ1 ---
        if completed_tasks:
            response_times = [
                rec.completed_at - rec.submitted_at        # type: ignore[operator]
                for rec in completed_tasks
            ]
            makespan = max(rec.completed_at for rec in completed_tasks) - min(
                rec.submitted_at for rec in tasks
            )
            avg_rt = statistics.fmean(response_times)
            p50 = statistics.median(response_times)
            p95 = _percentile(response_times, 95)
        else:
            response_times = []
            makespan = 0.0
            avg_rt = 0.0
            p50 = 0.0
            p95 = 0.0

        # --- RQ2 ---
        per_node_counts: dict[str, int] = {}
        for rec in tasks:
            if rec.status in ("completed", "dispatched") and rec.node_id:
                per_node_counts[rec.node_id] = per_node_counts.get(rec.node_id, 0) + 1
        counts = list(per_node_counts.values()) or [0]
        workload_variance = statistics.pvariance(counts) if len(counts) > 1 else 0.0
        balance_index = _coefficient_of_variation_over_time(active.distribution_samples)

        # --- RQ3 memory ---
        mem_avg, mem_peak, mem_p95, mem_above = _aggregate_utilization(
            active.memory_samples,
            saturation_pct=80.0,
            sample_to_pct=lambda s: (s.used_kb / s.total_kb * 100.0)
            if s.total_kb > 0
            else 0.0,
        )

        # --- RQ3 CPU ---
        cpu_avg, cpu_peak, cpu_p95, cpu_above = _aggregate_utilization(
            active.cpu_samples,
            saturation_pct=80.0,
            sample_to_pct=lambda s: (s.cores_in_use / s.cores_total * 100.0)
            if s.cores_total > 0
            else 0.0,
        )

        # --- RQ4 scheduling overhead ---
        decisions = active.decisions
        if decisions:
            overhead_total = sum(d.decision_ms for d in decisions) / 1000.0
            decisions_count = len(decisions)
            decision_durations = [d.decision_ms for d in decisions]
            decision_mean = statistics.fmean(decision_durations)
            decision_p95 = _percentile(decision_durations, 95)
        else:
            overhead_total = 0.0
            decisions_count = 0
            decision_mean = 0.0
            decision_p95 = 0.0

        # --- RQ4 completion / rejection patterns ---
        completed_by_class: dict[str, int] = {c.value: 0 for c in TaskClass}
        rejected_by_class: dict[str, int] = {c.value: 0 for c in TaskClass}
        rt_by_class_lists: dict[str, list[float]] = {c.value: [] for c in TaskClass}
        for rec in completed_tasks:
            cls = rec.task.task_class.value
            completed_by_class[cls] += 1
            rt_by_class_lists[cls].append(rec.completed_at - rec.submitted_at)  # type: ignore[operator]
        for rec in rejected_tasks:
            rejected_by_class[rec.task.task_class.value] += 1
        avg_rt_by_class: dict[str, float] = {
            cls: (statistics.fmean(rts) if rts else 0.0)
            for cls, rts in rt_by_class_lists.items()
        }

        return TrialResult(
            run_id=active.run_id,
            scenario_name=active.scenario_name,
            algorithm=active.algorithm,
            trial_index=active.trial_index,
            comparison_id=active.comparison_id,
            seed=active.seed,
            started_at=active.started_at,
            completed_at=completed_at,
            makespan_seconds=makespan,
            avg_response_time_seconds=avg_rt,
            completion_time_p50_seconds=p50,
            completion_time_p95_seconds=p95,
            workload_variance=float(workload_variance),
            distribution_balance_index=balance_index,
            distribution_balance_timeseries_path=active.event_log_rel_path,
            avg_memory_utilization_pct=mem_avg,
            peak_memory_utilization_pct=mem_peak,
            p95_memory_utilization_pct=mem_p95,
            time_above_memory_saturation_seconds=mem_above,
            avg_cpu_utilization_pct=cpu_avg,
            peak_cpu_utilization_pct=cpu_peak,
            p95_cpu_utilization_pct=cpu_p95,
            time_above_cpu_saturation_seconds=cpu_above,
            scheduling_overhead_seconds_total=overhead_total,
            scheduling_decisions_count=decisions_count,
            scheduling_overhead_per_decision_mean_ms=decision_mean,
            scheduling_overhead_per_decision_p95_ms=decision_p95,
            tasks_total=len(tasks),
            tasks_completed=len(completed_tasks),
            tasks_rejected=len(rejected_tasks),
            tasks_failed=len(failed_tasks),
            rejected_by_class=rejected_by_class,
            completed_by_class=completed_by_class,
            avg_response_time_by_class=avg_rt_by_class,
        )

    # ---------------------------------------------------------------
    # Internal: CSV writing
    # ---------------------------------------------------------------

    def _append_runs_csv(self, result: TrialResult) -> None:
        """Append one row to results/runs.csv. Creates the file with a
        header row if it does not yet exist. dict-typed fields are
        serialized as JSON strings so the CSV is grep-friendly while still
        recoverable."""
        row = result.model_dump(mode="json")
        # Flatten dict fields to JSON strings.
        for k in ("rejected_by_class", "completed_by_class", "avg_response_time_by_class"):
            row[k] = json.dumps(row[k], sort_keys=True)

        with self._csv_lock:
            self._results_dir.mkdir(parents=True, exist_ok=True)
            file_existed = self._runs_csv.exists()
            with self._runs_csv.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not file_existed:
                    writer.writeheader()
                writer.writerow(row)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile. Returns 0.0 on empty input."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _aggregate_utilization(
    samples_by_node: dict,
    saturation_pct: float,
    sample_to_pct,
) -> tuple[float, float, float, float]:
    """Aggregate per-node 1Hz samples into (avg, peak, p95, time-above-sat)
    averaged across nodes. Returns zeros on empty input."""
    if not samples_by_node:
        return 0.0, 0.0, 0.0, 0.0

    per_node_avg: list[float] = []
    per_node_peak: list[float] = []
    per_node_p95: list[float] = []
    total_time_above = 0.0

    for node_id, samples in samples_by_node.items():
        if not samples:
            continue
        pcts = [sample_to_pct(s) for s in samples]
        per_node_avg.append(statistics.fmean(pcts))
        per_node_peak.append(max(pcts))
        per_node_p95.append(_percentile(pcts, 95))
        # Time above saturation: each 1Hz sample represents ~1 second.
        total_time_above += sum(1.0 for p in pcts if p >= saturation_pct)

    if not per_node_avg:
        return 0.0, 0.0, 0.0, 0.0
    return (
        statistics.fmean(per_node_avg),
        max(per_node_peak),
        statistics.fmean(per_node_p95),
        total_time_above,
    )


def _coefficient_of_variation_over_time(
    samples: list,
) -> float:
    """Mean coefficient of variation of running-task counts across nodes,
    averaged over time. CV = stdev / mean; 0 when all nodes equal."""
    if not samples:
        return 0.0
    cvs: list[float] = []
    for s in samples:
        counts = list(s.running.values())
        if not counts:
            continue
        mean = statistics.fmean(counts)
        if mean == 0:
            continue
        std = statistics.pstdev(counts)
        cvs.append(std / mean)
    return statistics.fmean(cvs) if cvs else 0.0