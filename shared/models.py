"""
Pydantic data models — the wire format and internal vocabulary for the whole
testbed.

Everything that crosses a process boundary (HTTP requests, JSONL log lines,
CSV row serialization) goes through these models. The scheduler service, the
edge node service, and the verification runner all import from here, which
guarantees that a Task created on the scheduler side deserializes correctly on
the edge side.

Models are grouped into four sections:

1. Tasks and assignments      — what flows from scheduler to edge node
2. Nodes                      — node configuration and live state
3. Performance results        — TrialResult: one row in results/runs.csv
4. Verification results       — assertion outcomes and the verification report

Pydantic v2 conventions are used throughout (Field, model_dump, etc.).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.task_classes import TaskClass


# ---------------------------------------------------------------------------
# 1. Tasks and assignments
# ---------------------------------------------------------------------------


class Task(BaseModel):
    """A unit of work submitted to the scheduler.

    `memory_kb` and `workload_units` are sampled by the workload generator
    from the per-class ranges in shared/task_classes.py. `cpu_cores_required`
    defaults to 1 — we have not yet identified any test scenario that needs
    multi-core tasks, but the field exists so the algorithm interface is
    forward-compatible.
    """

    task_id: str
    task_class: TaskClass
    memory_kb: int = Field(
        ..., gt=0, description="RAM held while running, in KB"
    )
    workload_units: int = Field(
        ...,
        gt=0,
        description="Compute work; runtime = workload_units * SECONDS_PER_WORKLOAD_UNIT",
    )
    cpu_cores_required: int = Field(default=1, ge=1)
    submitted_at: float = Field(
        ..., description="Epoch seconds; set when scheduler enqueues the task"
    )
    arrival_offset: float = Field(
        default=0.0,
        description="t-offset within a scenario; informational, not used at runtime",
    )
    trial_id: str | None = None
    run_id: str | None = Field(
        default=None,
        description="Identifies a specific (scenario, algorithm, trial) execution",
    )


class Assignment(BaseModel):
    """Records the lifecycle of one task on one node.

    The scheduler creates an Assignment when an algorithm decides where a task
    goes. Timestamps fill in as the task progresses: dispatched → started →
    completed (or failed/rejected). The trial recorder serializes Assignments
    into the JSONL event log and uses them to compute per-trial metrics.
    """

    task_id: str
    node_id: str
    decided_at: float
    dispatched_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    status: Literal[
        "pending",
        "dispatched",
        "running",
        "completed",
        "failed",
        "rejected_no_fit",
        "rejected_node_error",
    ]
    rejection_reason: str | None = None


class AdmitDecision(BaseModel):
    """Returned by an edge node's admission control.

    Even when the scheduler's mirror believes a node has capacity, the edge
    node has the final say (it might be processing an injected memory hold,
    for example). Algorithm bugs that try to over-allocate show up as
    AdmitDecision(admitted=False, ...) responses.
    """

    admitted: bool
    reason: str | None = Field(
        default=None,
        description=(
            "One of: node_inactive, class_not_accepted, insufficient_memory, "
            "insufficient_cpu_cores"
        ),
    )


# ---------------------------------------------------------------------------
# 2. Nodes
# ---------------------------------------------------------------------------


class NodeSpec(BaseModel):
    """Static configuration of one edge node.

    Loaded from environment variables at edge node boot. Hardware fields
    (RAM, flash, cores) match the heterogeneous device classes in
    shared/constants.py. `accepted_classes` is the hard feasibility gate
    that encodes the PDF rule "Edge Node 1 can accommodate only lightweight
    tasks" and similar constraints for the other nodes.
    """

    node_id: str
    url: str = Field(..., description="Base URL the scheduler uses to reach this node")
    total_ram_kb: int
    usable_ram_kb: int = Field(
        ...,
        description="Defaults to ~70% of total_ram_kb; rest is reserved for system overhead",
    )
    flash_kb: int
    saturation_threshold_kb: int = Field(
        ...,
        description="80% of total_ram_kb; observable signal for the adaptive trigger",
    )
    cpu_cores: int = Field(..., ge=1)
    cpu_saturation_threshold_pct: float = Field(default=0.80, ge=0.0, le=1.0)
    accepted_classes: list[TaskClass] = Field(
        default_factory=list,
        description="Task classes this node will admit; enforced both at scheduler and edge",
    )
    is_active: bool = True


class NodeRuntimeStatus(BaseModel):
    """Live state of one edge node, as observed by the scheduler.

    Populated by the scheduler's reconciliation thread polling /status, or
    inferred from the optimistic mirror between polls. Used by algorithms via
    NodeView to make placement decisions.
    """

    reachable: bool = False
    used_ram_kb: int = 0
    available_ram_kb: int = 0
    is_memory_saturated: bool = False
    cpu_cores_in_use: int = 0
    cpu_utilization_pct: float = 0.0
    is_cpu_saturated: bool = False
    active_task_count: int = 0
    last_seen: float | None = None
    error: str | None = None


class NodeView(BaseModel):
    """The view of a node that an algorithm sees during scheduling.

    Combines the static spec with live runtime status, plus three projected
    fields the scheduler computes by replaying recent assignments. Algorithms
    must not write to NodeView — it is read-only input to .schedule().
    """

    spec: NodeSpec
    status: NodeRuntimeStatus
    projected_used_ram_kb: int = Field(
        ...,
        description="Mirror including in-flight reservations from the current tick",
    )
    projected_used_cores: int
    projected_completion_time: float = Field(
        ...,
        description="Earliest time this node has free capacity again",
    )


# ---------------------------------------------------------------------------
# 3. Performance results — one row per (algorithm, scenario, trial)
# ---------------------------------------------------------------------------


class TrialResult(BaseModel):
    """One row in results/runs.csv. The schema is shaped so a Friedman test
    pivots cleanly: rows = comparison_id, columns = algorithm, cells = metric.

    All fields are populated by the trial recorder when /trial/end is called.
    Anything that needs richer time-series data (e.g., distribution-balance
    plots) lives in the JSONL event log at events/<run_id>.jsonl.
    """

    # Identifying fields
    run_id: str
    scenario_name: str
    algorithm: str
    trial_index: int = Field(..., ge=1)
    comparison_id: str = Field(
        ...,
        description="Pivot key for Friedman blocking — same across algorithms within a trial",
    )
    seed: int
    started_at: float
    completed_at: float

    # --- RQ1: time patterns ---
    makespan_seconds: float
    avg_response_time_seconds: float
    completion_time_p50_seconds: float
    completion_time_p95_seconds: float

    # --- RQ2: workload distribution ---
    workload_variance: float = Field(
        ..., description="Variance of total task counts across nodes"
    )
    distribution_balance_index: float = Field(
        ...,
        description="Mean coefficient of variation of running-task counts over time",
    )
    distribution_balance_timeseries_path: str = Field(
        ...,
        description="Relative path to JSONL with 1Hz running-task samples",
    )

    # --- RQ3: memory utilization ---
    avg_memory_utilization_pct: float
    peak_memory_utilization_pct: float
    p95_memory_utilization_pct: float
    time_above_memory_saturation_seconds: float

    # --- RQ3: CPU utilization ---
    avg_cpu_utilization_pct: float
    peak_cpu_utilization_pct: float
    p95_cpu_utilization_pct: float
    time_above_cpu_saturation_seconds: float

    # --- RQ4: scheduling overhead ---
    scheduling_overhead_seconds_total: float
    scheduling_decisions_count: int
    scheduling_overhead_per_decision_mean_ms: float
    scheduling_overhead_per_decision_p95_ms: float

    # --- RQ4: completion / rejection patterns ---
    tasks_total: int
    tasks_completed: int
    tasks_rejected: int = Field(..., description="Could not be placed within timeout")
    tasks_failed: int = Field(..., description="Placed but errored during execution")
    rejected_by_class: dict[str, int] = Field(default_factory=dict)
    completed_by_class: dict[str, int] = Field(default_factory=dict)
    avg_response_time_by_class: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 4. Verification results — pass/fail report, separate from performance CSV
# ---------------------------------------------------------------------------


class AssertionResult(BaseModel):
    """Outcome of one named assertion against one trial's collected context.

    `name` matches the registered assertion in
    verification/assertions/library.py. `detail` is human-readable and is what
    appears in the verification report when an assertion fails — keep it
    informative (counts, examples) rather than just "failed".
    """

    name: str
    passed: bool
    detail: str


class VerificationVerdict(BaseModel):
    """One verdict per (test, algorithm, sub_run, trial) combination.

    Tests with sub_runs (e.g., stability_under_load) produce one verdict per
    sub_run trial. Tests with trials > 1 (e.g., beta) produce one verdict per
    trial. Single-trial tests have sub_run=None and trial_index=None.
    """

    test_id: str
    test_type: Literal["alpha", "beta", "whitebox", "blackbox"]
    algorithm: str
    sub_run: str | None = None
    trial_index: int | None = None
    overall_passed: bool
    assertion_results: list[AssertionResult]
    started_at: float
    completed_at: float
    event_log_path: str


class VerificationReport(BaseModel):
    """Aggregate of all verdicts from one /tests/run_all invocation.

    Serialized to results/verification/report.json (machine-readable) and
    rendered to results/verification/report.md (human-readable, paste into
    the thesis testing chapter).
    """

    model_config = ConfigDict(arbitrary_types_allowed=False)

    started_at: float
    completed_at: float
    total_verdicts: int
    passed: int
    failed: int
    verdicts: list[VerificationVerdict]