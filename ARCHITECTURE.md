# Edge Scheduling Testbed — Target Architecture (v3)

**Project:** Optimized Adaptive Min–Min Scheduling for Dynamic Task Allocation in Resource-Constrained Edge Systems
**Status:** Design specification (not yet implemented)
**Goal:** A plug-and-play testbed where Min-Min and Adaptive Min-Min can be compared, the PDF's performance scenarios produce Friedman-ready data for the thesis's evaluation chapter, and the PDF's Alpha / Beta / White-Box / Black-Box tests produce a separate pass/fail verification report for the thesis's testing chapter.

---

## 0. Research questions and tests this testbed must answer

The architecture is justified by the questions and tests it has to answer. Anything not in service of these is out of scope.

### Performance research questions

1. **RQ1 — Task execution time patterns under dynamic workload:** makespan, average task response time.
2. **RQ2 — Workload distribution variation:** workload variance among nodes, task distribution balance over time.
3. **RQ3 — Resource utilization trends:** CPU utilization, memory utilization (both as time-series and aggregates).
4. **RQ4 — Adaptive vs traditional Min-Min comparison:** task completion patterns, load distribution, resource usage, scheduling overhead.

Statistical validation across performance RQs uses the **Friedman test**. The result schema is built so Friedman can be applied directly without reshaping.

### Verification testing

The PDF also specifies four classes of system tests that produce **pass/fail verdicts**, not metrics:

- **Alpha testing** — controlled, fixed task set, end-to-end correctness
- **Beta testing** — realistic dynamic workload, system stability
- **White-Box testing** — task selection logic, node selection logic, adaptive trigger logic, termination logic
- **Black-Box testing** — correct scheduling output, stability under load, behavior under dynamic conditions

These produce a separate verification report, not CSV rows for Friedman.

---

## 1. Design principles

1. **Memory and CPU are first-class resources.** Every component speaks in KB of RAM and CPU cores. A task has both `memory_kb` and `cpu_cores_required`. A node has both `usable_ram_kb` and `cpu_cores`. The thesis is about resource-aware scheduling on resource-constrained nodes; the system must model both axes.
2. **Algorithms are plug-and-play behind a stable interface.** Only Min-Min and Adaptive Min-Min are in scope, but the interface is general — adding a new algorithm is one new file plus one registry entry, with no changes to scheduler service, edge node, dashboard, or metrics.
3. **Performance and verification are separate concerns.** Performance scenarios produce metric rows for Friedman analysis. Verification tests produce pass/fail verdicts against contracts. They share infrastructure (same edge nodes, same scheduler, same task generator) but have separate runners, separate file trees, and separate reports.
4. **Same task set across algorithms.** Per the experimental protocol, both algorithms must process the same task set on the same node configuration so observations are paired. The runner enforces this via shared seeds within a `comparison_id`.
5. **Online API, batch internals.** Submission is HTTP-driven, but the scheduler operates on a queue and runs batch algorithms on a tick. Min-Min is batch by definition.
6. **Friedman-ready performance results.** The performance results CSV is shaped so Friedman can be applied directly: one row per (algorithm, scenario, trial), with `comparison_id` as the blocking key.
7. **Reusable assertion library.** Verification assertions are defined once and referenced by name from any test file. Black-Box and White-Box tests sharing the same assertion (e.g., "edge1 only lightweight") use the same code path.
8. **Everything is measured per-trial.** No aggregate-only metrics. Each trial writes a CSV row plus a JSONL event log so analyses can be re-run from archived data without rebooting the testbed.

---

## 2. File and directory layout

```
thesis/
├── docker-compose.yml
├── prometheus.yml
├── ARCHITECTURE.md                       # this document
│
├── Dockerfile.edge
├── Dockerfile.scheduler
├── Dockerfile.dashboard
│
├── shared/                               # imported by scheduler, edge, dashboard
│   ├── __init__.py
│   ├── models.py                         # Task, NodeSpec, Assignment, TrialResult, VerificationVerdict
│   ├── task_classes.py                   # class → memory range, class → workload range
│   └── constants.py                      # node profiles, saturation thresholds, etc.
│
├── edge/
│   ├── __init__.py
│   ├── edgenode.py                       # FastAPI app
│   ├── memory_manager.py                 # RAM admission + tracking
│   ├── cpu_manager.py                    # core admission + tracking
│   └── execution.py                      # task simulation (sleep-based)
│
├── scheduler/
│   ├── __init__.py
│   ├── service.py                        # FastAPI app
│   ├── queue.py                          # pending task queue
│   ├── dispatcher.py                     # HTTP dispatch to edge nodes
│   ├── node_state.py                     # scheduler-side mirror (RAM + cores)
│   ├── trial_recorder.py                 # per-task timestamps, per-trial CSV/JSONL
│   ├── algorithms/
│   │   ├── __init__.py                   # registry: min_min, adaptive_min_min
│   │   ├── base.py                       # SchedulingAlgorithm ABC
│   │   ├── min_min.py                    # baseline
│   │   └── adaptive_min_min.py           # proposed (formula plugged in later)
│   └── analysis/
│       ├── __init__.py
│       ├── friedman.py                   # Friedman test, ranks, p-values
│       ├── timeseries.py                 # rebuild plots from JSONL
│       └── report.py                     # generate thesis-ready performance summary
│
├── workload/
│   ├── __init__.py
│   ├── generator.py                      # task generation by class (memory + workload sampling)
│   ├── arrival.py                        # arrival schedules (t=0 batch, drip, spike)
│   └── scenario_runner.py                # loads YAML, drives a performance trial end-to-end
│
├── verification/                         # SEPARATE from scenarios; pass/fail verdicts only
│   ├── __init__.py
│   ├── runner.py                         # loads test YAML, drives test, evaluates assertions
│   ├── assertions/
│   │   ├── __init__.py                   # assertion registry
│   │   └── library.py                    # all named assertions, registered by decorator
│   └── report.py                         # generates verification report (md + json)
│
├── scenarios/                            # PERFORMANCE — produces runs.csv → Friedman
│   ├── 01_low_workload.yaml
│   ├── 02_moderate_workload.yaml
│   ├── 03_high_workload.yaml
│   ├── 04_homogeneous.yaml
│   ├── 05_heterogeneous.yaml
│   ├── 06_varying_demand.yaml
│   ├── 07_dynamic_arrival.yaml
│   ├── 08_task_spike.yaml
│   ├── 09_resource_constrained.yaml
│   ├── 10_node_saturation.yaml
│   ├── 11_makespan_eval.yaml
│   ├── 12_response_time_eval.yaml
│   ├── 13_workload_variance_eval.yaml
│   ├── 14_memory_utilization_eval.yaml
│   └── 15_scheduling_overhead_eval.yaml
│
├── tests/                                # VERIFICATION — produces pass/fail report
│   ├── alpha.yaml
│   ├── beta.yaml
│   ├── whitebox/
│   │   ├── task_selection_logic.yaml
│   │   ├── node_selection_logic.yaml
│   │   ├── adaptive_trigger_logic.yaml
│   │   └── termination_logic.yaml
│   └── blackbox/
│       ├── correct_scheduling_output.yaml
│       ├── stability_under_load.yaml
│       └── behavior_under_dynamic_conditions.yaml
│
├── dashboard/
│   ├── app.py                            # Streamlit entry
│   ├── api_client.py
│   ├── pages/
│   │   ├── 1_Overview.py
│   │   ├── 2_Nodes.py
│   │   ├── 3_Scenarios.py                # performance scenario runs
│   │   ├── 4_Verification.py             # verification test runs
│   │   └── 5_Results.py                  # Friedman + plots
│   └── plots.py                          # matplotlib/plotly figures
│
└── results/
    ├── runs.csv                          # performance — one row per (algorithm, scenario, trial)
    ├── events/                           # performance event logs
    │   └── <run_id>.jsonl
    ├── analysis/                         # Friedman + plots
    │   ├── friedman_<metric>.csv
    │   ├── timeseries/<scenario>_<algorithm>_<metric>.png
    │   └── summary.md                    # thesis-ready performance summary
    └── verification/                     # SEPARATE artifact
        ├── report.md                     # human-readable pass/fail report
        ├── report.json                   # machine-readable
        └── events/                       # per-test event logs (for debugging failures)
            └── <test_id>_<algorithm>.jsonl
```

Three things to notice:

- **`shared/` is imported by scheduler, edge, and verification.** Pydantic models live here so the wire format and report schemas can't drift.
- **`scheduler/analysis/` is separate from runtime.** Friedman processing and plot regeneration run on archived `runs.csv` and JSONL — no need to keep the testbed alive to redo a chart.
- **`scenarios/` and `tests/` are siblings, not nested.** They share infrastructure but produce different artifacts. This is the heart of the v3 separation: performance lives in one tree, verification in another.

---

## 3. Core data model (`shared/models.py`)

This is the vocabulary the entire system speaks. Get this right and everything downstream falls into place.

```python
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field

class TaskClass(str, Enum):
    LIGHTWEIGHT = "lightweight"      #   2–5  KB RAM
    MODERATE    = "moderate"         #   8–24 KB RAM
    HEAVY       = "heavy"            #  32–64 KB RAM
    VERY_HEAVY  = "very_heavy"       #  96–160 KB RAM

class Task(BaseModel):
    task_id: str
    task_class: TaskClass
    memory_kb: int = Field(..., gt=0,
        description="RAM held while running")
    workload_units: int = Field(..., gt=0,
        description="Compute work; runtime = workload_units * SECONDS_PER_UNIT")
    cpu_cores_required: int = Field(default=1, ge=1)
    submitted_at: float                  # epoch seconds, set at submission
    arrival_offset: float = 0.0          # t-offset for scheduled arrival
    trial_id: Optional[str] = None
    run_id: Optional[str] = None

class NodeSpec(BaseModel):
    node_id: str
    url: str
    total_ram_kb: int                    # 8 / 128 / 264 / 520
    usable_ram_kb: int                   # 70% of total_ram_kb (rounded)
    flash_kb: int                        # 256 / 1024 / 2048 / 4096
    saturation_threshold_kb: int         # 80% of total_ram_kb
    cpu_cores: int                       # 1 / 1 / 2 / 4 (hetero default)
    cpu_saturation_threshold_pct: float = 0.80
    accepted_classes: list[TaskClass]    # feasibility gate per PDF rules
    is_active: bool = True

class NodeRuntimeStatus(BaseModel):
    reachable: bool = False
    used_ram_kb: int = 0
    available_ram_kb: int = 0
    is_memory_saturated: bool = False
    cpu_cores_in_use: int = 0
    cpu_utilization_pct: float = 0.0     # cores_in_use / cpu_cores * 100
    is_cpu_saturated: bool = False
    active_task_count: int = 0
    last_seen: Optional[float] = None
    error: Optional[str] = None

class Assignment(BaseModel):
    task_id: str
    node_id: str
    decided_at: float
    dispatched_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: Literal["pending", "dispatched", "running", "completed",
                    "failed", "rejected_no_fit", "rejected_node_error"]
    rejection_reason: Optional[str] = None

class TrialResult(BaseModel):
    """One row in results/runs.csv. Friedman-ready: pivot by comparison_id."""
    run_id: str
    scenario_name: str
    algorithm: str
    trial_index: int                     # 1..N
    comparison_id: str                   # e.g. "high_workload_3" — same across algorithms
    seed: int
    started_at: float
    completed_at: float

    # --- RQ1: time patterns ---
    makespan_seconds: float
    avg_response_time_seconds: float
    completion_time_p50_seconds: float
    completion_time_p95_seconds: float

    # --- RQ2: workload distribution ---
    workload_variance: float                 # variance of task counts across nodes
    distribution_balance_index: float        # mean coefficient of variation over time
    distribution_balance_timeseries_path: str  # JSONL relative path

    # --- RQ3: resource utilization (memory) ---
    avg_memory_utilization_pct: float
    peak_memory_utilization_pct: float
    p95_memory_utilization_pct: float
    time_above_memory_saturation_seconds: float

    # --- RQ3: resource utilization (CPU) ---
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
    tasks_rejected: int                      # could not be placed
    tasks_failed: int                        # placed but errored
    rejected_by_class: dict[str, int]
    completed_by_class: dict[str, int]
    avg_response_time_by_class: dict[str, float]


# --- Verification models ---

class AssertionResult(BaseModel):
    name: str
    passed: bool
    detail: str

class VerificationVerdict(BaseModel):
    """One verdict per (test, algorithm, sub_run, trial) combination."""
    test_id: str
    test_type: Literal["alpha", "beta", "whitebox", "blackbox"]
    algorithm: str
    sub_run: Optional[str] = None        # for stability_under_load
    trial_index: Optional[int] = None    # for tests with trials > 1
    overall_passed: bool                 # all assertions passed
    assertion_results: list[AssertionResult]
    started_at: float
    completed_at: float
    event_log_path: str

class VerificationReport(BaseModel):
    """Aggregated across all tests in a run of /tests/run_all."""
    started_at: float
    completed_at: float
    total_verdicts: int
    passed: int
    failed: int
    verdicts: list[VerificationVerdict]
```

### Task class → memory + workload mapping (`shared/task_classes.py`)

```python
TASK_CLASS_MEMORY_KB = {
    TaskClass.LIGHTWEIGHT: (2, 5),
    TaskClass.MODERATE:    (8, 24),
    TaskClass.HEAVY:       (32, 64),
    TaskClass.VERY_HEAVY:  (96, 160),
}

# Workload units sampled per class so heavier tasks naturally take longer.
# Runtime = workload_units * SECONDS_PER_UNIT, independent of memory size.
TASK_CLASS_WORKLOAD_UNITS = {
    TaskClass.LIGHTWEIGHT: (5, 10),
    TaskClass.MODERATE:    (10, 25),
    TaskClass.HEAVY:       (25, 50),
    TaskClass.VERY_HEAVY:  (50, 100),
}

SECONDS_PER_WORKLOAD_UNIT = 0.1          # tunable in constants.py
```

### Default node profile (`shared/constants.py`)

The "heterogeneous" baseline from the PDF, with CPU cores layered in:

```python
USABLE_RAM_PCT = 0.70
MEMORY_SATURATION_PCT = 0.80
CPU_SATURATION_PCT = 0.80

HETEROGENEOUS_4NODE = [
    NodeSpec(node_id="edge1", url="http://edge1:5000",
             total_ram_kb=8,   usable_ram_kb=5,   flash_kb=256,
             saturation_threshold_kb=6,  cpu_cores=1,
             accepted_classes=[TaskClass.LIGHTWEIGHT]),
    NodeSpec(node_id="edge2", url="http://edge2:5000",
             total_ram_kb=128, usable_ram_kb=90,  flash_kb=1024,
             saturation_threshold_kb=102, cpu_cores=1,
             accepted_classes=[TaskClass.LIGHTWEIGHT, TaskClass.MODERATE,
                               TaskClass.HEAVY]),
    NodeSpec(node_id="edge3", url="http://edge3:5000",
             total_ram_kb=264, usable_ram_kb=185, flash_kb=2048,
             saturation_threshold_kb=211, cpu_cores=2,
             accepted_classes=[TaskClass.LIGHTWEIGHT, TaskClass.MODERATE,
                               TaskClass.HEAVY, TaskClass.VERY_HEAVY]),
    NodeSpec(node_id="edge4", url="http://edge4:5000",
             total_ram_kb=520, usable_ram_kb=364, flash_kb=4096,
             saturation_threshold_kb=416, cpu_cores=4,
             accepted_classes=[TaskClass.LIGHTWEIGHT, TaskClass.MODERATE,
                               TaskClass.HEAVY, TaskClass.VERY_HEAVY]),
]
```

`accepted_classes` is the **hard feasibility gate** — it directly encodes the PDF's "Edge Node 1 can accommodate only lightweight tasks" rule. The scheduler must filter on this before any algorithm-specific logic runs, and the edge node enforces it as a second line of defense.

The CPU core counts (1/1/2/4) are a defensible mapping to constrained device classes (Arduino-class single core, ESP32-class single core, Cortex-M dual core, RPi-Zero quad core). They are tunable in `constants.py`.

---

## 4. Edge node (`edge/`)

### Responsibilities

The edge node simulates a resource-constrained device.

1. Boot with a `NodeSpec` from environment variables.
2. Accept or reject tasks based on (a) class feasibility, (b) RAM availability, (c) CPU core availability, (d) `is_active`.
3. Hold the task's RAM and core(s) for the task's runtime; release on completion.
4. Expose accurate `/status` and Prometheus metrics for both memory and CPU.
5. Provide admin endpoints to inject memory pressure and reset state between trials.

### Key endpoint shapes

```
POST /execute
  body: Task
  201 {assignment}      → accepted, runs in background
  409 {reason}          → rejected (memory, cores, or class)
  503 {reason}          → node inactive

GET /status
  → {node_spec, runtime_status} including CPU + memory

POST /admin/inject_memory
  body: {kb: int, hold_seconds: float}
  → reserves N KB for hold_seconds, then releases
  (used by Node Saturation scenario to drive edge2 to ~80%)

POST /admin/reset
  → drops all in-flight tasks, zeroes used_ram_kb and cpu_cores_in_use,
    clears injected memory
  (called between trials by the scenario runner and verification runner)
```

### Admission control

```python
def try_admit(task: Task) -> AdmitDecision:
    with lock:
        if not is_active:
            return AdmitDecision(False, "node_inactive")
        if task.task_class not in spec.accepted_classes:
            return AdmitDecision(False, "class_not_accepted")
        if used_ram_kb + task.memory_kb > spec.usable_ram_kb:
            return AdmitDecision(False, "insufficient_memory")
        if cpu_cores_in_use + task.cpu_cores_required > spec.cpu_cores:
            return AdmitDecision(False, "insufficient_cpu_cores")
        used_ram_kb += task.memory_kb
        cpu_cores_in_use += task.cpu_cores_required
        active_tasks[task.task_id] = task
        return AdmitDecision(True, None)
```

The class and resource checks are intentionally redundant with the scheduler's filters. The scheduler should never send an infeasible task, but the edge node refusing it makes algorithm bugs visible immediately rather than silently corrupting results.

### Prometheus metrics (per node)

```
edge_total_ram_kb{node_id}                  gauge
edge_usable_ram_kb{node_id}                 gauge
edge_used_ram_kb{node_id}                   gauge
edge_available_ram_kb{node_id}              gauge
edge_memory_utilization_pct{node_id}        gauge
edge_is_memory_saturated{node_id}           gauge (0/1)

edge_cpu_cores{node_id}                     gauge
edge_cpu_cores_in_use{node_id}              gauge
edge_cpu_utilization_pct{node_id}           gauge
edge_is_cpu_saturated{node_id}              gauge (0/1)

edge_active_task_count{node_id}             gauge
edge_tasks_admitted_total{node_id, class}   counter
edge_tasks_rejected_total{node_id, reason}  counter
edge_tasks_completed_total{node_id, class}  counter
edge_task_runtime_seconds{node_id, class}   histogram
```

These map directly onto the Memory Utilization Evaluation, Workload Variance Evaluation, and the CPU side of RQ3.

---

## 5. Scheduler (`scheduler/`)

### Architecture shift

The scheduler does **not** make a decision inside `/submit_task`. Submission enqueues. A separate scheduling tick (background thread, default 100 ms, also fires on demand when the queue grows) runs the active algorithm on the *full pending set* against the *full node set* and dispatches every assignment produced.

This is what makes Min-Min possible. Min-Min is batch by definition.

### Service endpoints

```
# Task submission
POST /submit_task            → enqueue
POST /submit_batch           → enqueue many at once
GET  /queue                  → current pending tasks

# System state and config
GET  /system/state           → scheduler summary + node mirror
GET  /nodes                  → registered nodes with runtime status
POST /config/algorithm       → switch active algorithm
GET  /config/algorithms      → list registered algorithms (returns ["min_min", "adaptive_min_min"])

# Trial lifecycle
POST /trial/start            → begin a new trial (assigns run_id, opens event log)
POST /trial/end              → finalize trial, write TrialResult row to CSV
POST /trial/reset            → /admin/reset on all nodes, clear queue, reseed RNG

# Performance scenario runner
POST /scenario/run           → run all trials × all algorithms in a scenario
POST /scenario/run_single    → one (scenario, algorithm, trial) tuple
GET  /scenario/list          → enumerate scenarios/*.yaml

# Verification test runner (NEW in v3)
POST /tests/run              → run a single verification test by id
POST /tests/run_all          → run all tests in tests/, produce VerificationReport
GET  /tests/list             → enumerate tests/

# Performance analysis
POST /analysis/friedman      → run Friedman test on results/runs.csv
POST /analysis/regenerate_plots  → rebuild timeseries PNGs from JSONL

# Health
GET  /metrics                → Prometheus
GET  /health
```

### Scheduling tick

```python
def scheduler_tick():
    while running:
        sleep(TICK_INTERVAL)
        with lock:
            pending = queue.snapshot()
            if not pending:
                continue
            nodes = node_state.snapshot()    # includes RAM and core projections
            t0 = perf_counter()
            assignments = active_algorithm.schedule(pending, nodes, time())
            decision_ms = (perf_counter() - t0) * 1000
            trial_recorder.record_decision(decision_ms, len(pending), len(assignments))
        for a in assignments:
            queue.remove(a.task_id)
            node_state.reserve(a.node_id, a.memory_kb, a.cpu_cores_required)
            dispatcher.dispatch_async(a)
```

`node_state` is the scheduler's mirror of node memory and core usage. Polling `/status` before every assignment is too slow and too stale for batch scheduling. The mirror is reserved optimistically when an assignment is made and released when the edge node confirms task completion. A reconciliation thread polls `/status` every 500 ms to correct drift.

### Algorithm interface (`scheduler/algorithms/base.py`)

```python
from abc import ABC, abstractmethod
from shared.models import Task, NodeSpec, NodeRuntimeStatus, Assignment

class NodeView(BaseModel):
    """What an algorithm sees about a node."""
    spec: NodeSpec
    status: NodeRuntimeStatus
    projected_used_ram_kb: int
    projected_used_cores: int
    projected_completion_time: float     # earliest time this node has free capacity

class SchedulingAlgorithm(ABC):
    name: str

    @abstractmethod
    def schedule(
        self,
        pending: list[Task],
        nodes: list[NodeView],
        now: float,
    ) -> list[Assignment]:
        """
        Return zero or more assignments. The scheduler dispatches them and
        updates its mirror. Tasks not assigned in this tick remain in the
        queue and are reconsidered next tick (hold-and-retry semantics).
        """
        ...

    def on_node_state_change(self, nodes: list[NodeView]) -> None:
        """Optional hook for stateful algorithms."""
        pass
```

**Why a list of assignments?** Min-Min is iterative: assign one (task, machine) pair, update machine availability *within the algorithm*, repeat. The algorithm needs to return all pairs computed in this batch.

**Why `now`?** Adaptive variants may need wall-clock for time-windowed averages or decay factors.

### Min-Min reference implementation (sketch)

```python
class MinMinAlgorithm(SchedulingAlgorithm):
    name = "min_min"

    def schedule(self, pending, nodes, now):
        unassigned = list(pending)
        assignments = []
        proj_ram   = {n.spec.node_id: n.projected_used_ram_kb for n in nodes}
        proj_cores = {n.spec.node_id: n.projected_used_cores  for n in nodes}
        free_at    = {n.spec.node_id: n.projected_completion_time for n in nodes}

        while unassigned:
            best = None  # (task, node, completion_time)
            for task in unassigned:
                feasible = [
                    n for n in nodes
                    if task.task_class in n.spec.accepted_classes
                    and proj_ram[n.spec.node_id]   + task.memory_kb        <= n.spec.usable_ram_kb
                    and proj_cores[n.spec.node_id] + task.cpu_cores_required <= n.spec.cpu_cores
                ]
                if not feasible:
                    continue
                t_min, n_min = min(
                    ((free_at[n.spec.node_id]
                        + task.workload_units * SECONDS_PER_WORKLOAD_UNIT,
                      n) for n in feasible),
                    key=lambda x: x[0],
                )
                if best is None or t_min < best[2]:
                    best = (task, n_min, t_min)

            if best is None:
                break  # nothing placeable this tick; retry next tick
            task, node, ct = best
            assignments.append(Assignment(
                task_id=task.task_id, node_id=node.spec.node_id,
                decided_at=now, status="dispatched"))
            proj_ram[node.spec.node_id]   += task.memory_kb
            proj_cores[node.spec.node_id] += task.cpu_cores_required
            free_at[node.spec.node_id]     = ct
            unassigned.remove(task)

        return assignments
```

### Adaptive Min-Min (placeholder)

```python
class AdaptiveMinMinAlgorithm(SchedulingAlgorithm):
    name = "adaptive_min_min"

    def __init__(self, formula_params: dict):
        self.params = formula_params

    def schedule(self, pending, nodes, now):
        # Same shape as Min-Min, but the completion-time / fitness function
        # incorporates the adaptive formula:
        #   - memory pressure penalty when a node nears saturation
        #   - CPU pressure penalty
        #   - reward for matching task class to node capability
        #   - whatever else the formula defines
        # To be filled in once the formula is shared.
        raise NotImplementedError("Awaiting formula spec")
```

### Algorithm registry — exactly two algorithms

```python
# scheduler/algorithms/__init__.py
ALGORITHMS = {
    "min_min":          MinMinAlgorithm(),
    "adaptive_min_min": AdaptiveMinMinAlgorithm(formula_params=...),
}
```

`/config/algorithm` validates against this dict's keys. The pluggable interface remains, but the registry is closed at two for this thesis. Adding a future algorithm would be one new file plus one entry — but not a requirement of this work.

### Failure semantics: hold-and-retry

When an algorithm cannot place a task this tick (no feasible node has enough RAM or cores), the task stays in the queue and is reconsidered next tick. After a configurable timeout (`TASK_PLACEMENT_TIMEOUT_SECONDS`, default 60s), it's recorded as `rejected_no_fit` and counted toward `tasks_rejected`. This satisfies the Alpha test's "no task should be skipped" requirement while keeping the scheduler bounded.

---

## 6. Performance scenarios (`workload/`, `scenarios/`)

### Scenario YAML format

One file per PDF performance test case. Includes an `algorithms` list because the experimental protocol requires running both algorithms on the same task set.

**Example: Dynamic Task Arrival**

```yaml
# scenarios/07_dynamic_arrival.yaml
name: dynamic_arrival
description: |
  8 tasks at t=0, then 1 task every 2 seconds for 16 more tasks.
  Total: 24 (8 lightweight, 6 moderate, 6 heavy, 4 very heavy).

nodes: heterogeneous_4node          # presets in shared/constants.py
                                    # (or inline a custom node list)

algorithms: [min_min, adaptive_min_min]
trials: 5                           # PDF requires 5 trials per scenario
seed_base: 42                       # trial k uses seed_base + k for both algorithms

arrivals:
  - at: 0.0
    tasks:
      lightweight: 4
      moderate: 2
      heavy: 1
      very_heavy: 1
  - generator:
      start: 2.0
      interval: 2.0
      count: 16
      mix: { lightweight: 4, moderate: 4, heavy: 5, very_heavy: 3 }
```

**Example: Task Spike**

```yaml
# scenarios/08_task_spike.yaml
name: task_spike
nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
trials: 5
seed_base: 100

arrivals:
  - at: 0.0
    tasks: { lightweight: 6, moderate: 4, heavy: 4, very_heavy: 2 }
  - at: 10.0
    tasks: { lightweight: 4, moderate: 3, heavy: 3, very_heavy: 2 }
```

**Example: Node Saturation**

```yaml
# scenarios/10_node_saturation.yaml
name: node_saturation
nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
trials: 5
seed_base: 200

arrivals:
  - at: 0.0
    tasks: { lightweight: 5, moderate: 5, heavy: 3, very_heavy: 1 }    # first 14
events:
  - at: 6.0
    type: inject_memory
    node_id: edge2
    kb: 80                          # drives edge2 to ~80% utilization
    hold_seconds: 60
  - at: 6.5
    type: submit_tasks
    tasks: { lightweight: 4, moderate: 3, heavy: 2, very_heavy: 1 }    # last 10
```

### Scenario runner (`workload/scenario_runner.py`)

The runner enforces the experimental protocol: same seed across algorithms within a trial, full reset between algorithm runs, paired observations via `comparison_id`.

```python
def run_scenario(scenario_path: Path):
    scenario = load_yaml(scenario_path)
    nodes = resolve_nodes(scenario.nodes)
    results = []

    for trial_index in range(1, scenario.trials + 1):
        seed = scenario.seed_base + trial_index
        comparison_id = f"{scenario.name}_{trial_index}"

        # Generate the deterministic task set ONCE per trial.
        # Both algorithms see the same tasks with the same memory_kb and
        # workload_units sampled from the seeded RNG.
        rng = random.Random(seed)
        task_timeline = build_task_timeline(scenario, rng)

        for algorithm_name in scenario.algorithms:
            run_id = f"{scenario.name}_{algorithm_name}_t{trial_index}_{int(time())}"
            scheduler.reset_all()
            scheduler.set_algorithm(algorithm_name)
            scheduler.set_nodes(nodes)
            scheduler.start_trial(run_id, scenario.name, algorithm_name,
                                  trial_index, comparison_id, seed)

            t_start = time()
            for event in task_timeline:
                wait_until(t_start + event.at)
                event.execute()              # submit_batch / inject_memory
            wait_for_drain(scheduler, timeout=...)
            result = scheduler.end_trial()
            results.append(result)

    return results
```

The crucial detail: `build_task_timeline(scenario, rng)` is called **once per trial, before the algorithms loop**. The same task list (same task_ids, memory_kb, workload_units, arrival times) is replayed for each algorithm. This is what makes the data Friedman-compatible — paired observations within `comparison_id`.

---

## 7. Verification testing (`verification/`, `tests/`)

This is the v3 addition. Verification tests share infrastructure with performance scenarios but produce a fundamentally different artifact: pass/fail verdicts, not metric rows.

### Why this is separate from `scenarios/`

A performance scenario asks "how well does this algorithm perform?" — output is a number to be compared statistically. A verification test asks "does the system behave correctly?" — output is a verdict against a contract. Mixing them blurs reporting: when your committee asks "did you test the system," you point at one report; when they ask "what does the data show," you point at another.

### Verification test YAML format

The format reuses scenario primitives (nodes, arrivals, algorithms, seed) but adds an `assertions` block.

**Example: Alpha test** (single trial, fixed task set)

```yaml
# tests/alpha.yaml
test_id: alpha
type: alpha
description: |
  Fixed set of 8 tasks under controlled conditions.
  Verifies scheduling process from task intake to task completion.

nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
trials: 1
seed: 1000

arrivals:
  - at: 0.0
    tasks: { lightweight: 3, moderate: 2, heavy: 2, very_heavy: 1 }

assertions:
  - all_tasks_have_terminal_status
  - no_unsupported_allocations
  - no_duplicate_assignments
  - all_assignments_logged
  - no_task_skipped
  - sufficient_memory_for_each_assignment
```

**Example: Beta test** (5 trials, dynamic arrivals)

```yaml
# tests/beta.yaml
test_id: beta
type: beta
description: 24 tasks via dynamic arrivals, heterogeneous nodes, 5 trials.

nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
trials: 5
seed_base: 1100

arrivals:
  - at: 0.0
    tasks: { lightweight: 3, moderate: 2, heavy: 2, very_heavy: 1 }
  - generator:
      start: 2.0
      interval: 2.0
      count: 16
      mix: { lightweight: 5, moderate: 5, heavy: 4, very_heavy: 2 }

assertions:
  - scheduler_did_not_hang
  - no_node_crashed
  - all_tasks_have_terminal_status
  - no_unsupported_allocations
  - valid_scheduling_decisions_throughout
```

**Example: White-Box adaptive trigger logic**

```yaml
# tests/whitebox/adaptive_trigger_logic.yaml
test_id: whitebox_adaptive_trigger_logic
type: whitebox
description: |
  20 tasks; force edge2 to ~80% memory; verify subsequent
  moderate/heavy tasks are redirected away from the stressed node.

nodes: heterogeneous_4node
algorithms: [adaptive_min_min]      # only meaningful for the adaptive algorithm
trials: 1
seed: 1200

arrivals:
  - at: 0.0
    tasks: { lightweight: 6, moderate: 5, heavy: 5, very_heavy: 4 }
events:
  - at: 4.0
    type: inject_memory
    node_id: edge2
    kb: 80
    hold_seconds: 30
  - at: 4.5
    type: submit_tasks
    tasks: { moderate: 3, heavy: 2 }   # extra moderate+ tasks after saturation

assertions:
  - adaptive_trigger_activated
  - moderate_plus_redirected_after_saturation
  - no_unsupported_allocations
```

**Example: Black-Box stability under load** (uses sub_runs)

```yaml
# tests/blackbox/stability_under_load.yaml
test_id: blackbox_stability_under_load
type: blackbox
description: |
  System remains operational at 12 / 24 / 40 task workloads.
  Each sub-run executed 5 times.

nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
seed_base: 1300

sub_runs:
  - name: low_load
    arrivals: [{ at: 0.0, tasks: { lightweight: 4, moderate: 4, heavy: 3, very_heavy: 1 } }]
    trials: 5
  - name: moderate_load
    arrivals: [{ at: 0.0, tasks: { lightweight: 8, moderate: 7, heavy: 6, very_heavy: 3 } }]
    trials: 5
  - name: high_load
    arrivals: [{ at: 0.0, tasks: { lightweight: 12, moderate: 10, heavy: 10, very_heavy: 8 } }]
    trials: 5

assertions:
  - scheduler_did_not_hang
  - no_node_crashed
  - no_unhandled_exceptions
  - all_tasks_have_terminal_status
  - no_unsupported_allocations
```

### Default trial counts

- **Default:** `trials: 1` (most White-Box tests, Alpha)
- **Beta:** `trials: 5` (PDF requirement)
- **Black-Box Stability Under Load:** 3 sub-runs × `trials: 5` each = 15 verification runs per algorithm
- **Black-Box Behavior Under Dynamic Conditions:** `trials: 5` (PDF requirement)
- **Black-Box Correct Scheduling Output:** `trials: 1` (single check)
- **All four White-Box tests:** `trials: 1` (single logical check each)

### Reusable assertion library (`verification/assertions/library.py`)

Every assertion is named, registered once, and referenced by name from any test YAML. This is what removes duplication between Black-Box and White-Box tests that share contracts.

```python
# verification/assertions/library.py
from typing import Callable

ASSERTIONS: dict[str, Callable[[TestContext], AssertionResult]] = {}

def register(name: str):
    def deco(fn): ASSERTIONS[name] = fn; return fn
    return deco

@register("all_tasks_have_terminal_status")
def _(ctx):
    bad = [t for t in ctx.assignments
           if t.status not in {"completed", "failed", "rejected_no_fit"}]
    return AssertionResult(
        name="all_tasks_have_terminal_status",
        passed=len(bad) == 0,
        detail=f"{len(bad)} tasks in non-terminal state"
    )

@register("no_unsupported_allocations")
def _(ctx):
    bad = []
    for a in ctx.assignments:
        node_spec = ctx.node_specs[a.node_id]
        if a.task.task_class not in node_spec.accepted_classes:
            bad.append((a.task_id, a.task.task_class.value, a.node_id))
    return AssertionResult(
        name="no_unsupported_allocations",
        passed=len(bad) == 0,
        detail=f"{len(bad)} class violations: {bad[:3]}"
    )

@register("scheduler_did_not_hang")
def _(ctx):
    return AssertionResult(
        name="scheduler_did_not_hang",
        passed=ctx.trial_completed_within_timeout,
        detail=f"trial duration: {ctx.duration_seconds:.1f}s "
               f"(timeout: {ctx.timeout_seconds}s)"
    )

@register("adaptive_trigger_activated")
def _(ctx):
    saturation_t = ctx.first_event_time(
        "memory_saturation_reached", node_id="edge2")
    if saturation_t is None:
        return AssertionResult(
            name="adaptive_trigger_activated",
            passed=False,
            detail="edge2 never reached saturation"
        )
    redirected = [
        a for a in ctx.assignments
        if a.decided_at > saturation_t
        and a.node_id != "edge2"
        and a.task.task_class in {TaskClass.MODERATE, TaskClass.HEAVY,
                                   TaskClass.VERY_HEAVY}
    ]
    return AssertionResult(
        name="adaptive_trigger_activated",
        passed=len(redirected) > 0,
        detail=f"{len(redirected)} moderate+ tasks redirected after saturation"
    )

# ~15-20 assertions total, covering all PDF white-box and black-box checks
```

### Verification runner (`verification/runner.py`)

```python
def run_test(test_path: Path) -> list[VerificationVerdict]:
    test = load_yaml(test_path)
    nodes = resolve_nodes(test.nodes)
    verdicts = []

    sub_runs = test.sub_runs if test.sub_runs else [None]   # None = no sub_run

    for sub_run in sub_runs:
        spec = sub_run if sub_run else test
        for trial_index in range(1, spec.trials + 1):
            seed = test.seed_base + trial_index if test.seed_base else test.seed
            rng = random.Random(seed)
            task_timeline = build_task_timeline(spec, rng)

            for algorithm_name in test.algorithms:
                # Run trial (same machinery as performance runner)
                ctx = run_trial_collecting_context(
                    nodes, algorithm_name, task_timeline, ...)

                # Evaluate assertions
                assertion_results = [
                    ASSERTIONS[name](ctx) for name in test.assertions
                ]
                verdicts.append(VerificationVerdict(
                    test_id=test.test_id,
                    test_type=test.type,
                    algorithm=algorithm_name,
                    sub_run=sub_run.name if sub_run else None,
                    trial_index=trial_index if spec.trials > 1 else None,
                    overall_passed=all(r.passed for r in assertion_results),
                    assertion_results=assertion_results,
                    started_at=ctx.started_at,
                    completed_at=ctx.completed_at,
                    event_log_path=ctx.event_log_path,
                ))

    return verdicts
```

### Verification report (`verification/report.py`)

`results/verification/report.md` is what gets pasted into the thesis's testing chapter:

```markdown
# System Verification Report
Generated: 2026-04-28T15:30:00Z
Algorithms tested: min_min, adaptive_min_min
Total verdicts: 38 | Passed: 38 | Failed: 0

## Alpha Testing
✅ alpha — 8 tasks, controlled conditions
   min_min: 6/6 assertions passed
   adaptive_min_min: 6/6 assertions passed

## Beta Testing
✅ beta — 24 tasks, dynamic arrivals, 5 trials
   min_min: 5/5 trials passed (5/5 assertions each)
   adaptive_min_min: 5/5 trials passed (5/5 assertions each)

## White-Box Testing
✅ whitebox/task_selection_logic
✅ whitebox/node_selection_logic
✅ whitebox/adaptive_trigger_logic    (adaptive_min_min only)
✅ whitebox/termination_logic

## Black-Box Testing
✅ blackbox/correct_scheduling_output
✅ blackbox/stability_under_load
   - low_load: 5/5 trials passed (both algorithms)
   - moderate_load: 5/5 trials passed (both algorithms)
   - high_load: 5/5 trials passed (both algorithms)
✅ blackbox/behavior_under_dynamic_conditions — 5 trials, both algorithms
```

Failed assertions are expanded with detail and a link to the JSONL event log path for debugging.

`results/verification/report.json` carries the same data structurally for the dashboard.

---

## 8. Performance metrics, statistics, and analysis layer

### Metric definitions

| Metric | Definition | Source |
|---|---|---|
| **Makespan** | `max(completed_at) − min(submitted_at)` per trial | event log |
| **Avg response time** | mean of `completed_at − submitted_at` per task | event log |
| **Response p50/p95** | percentiles of per-task response times | event log |
| **Workload variance** | variance of total task counts across nodes | assignment log |
| **Distribution balance index** | mean coefficient of variation of running-task counts, sampled at 1 Hz | distribution sample log |
| **Memory util (avg/peak/p95)** | over per-node `used_ram / total_ram` samples at 1 Hz, averaged across nodes per stat | memory sample log |
| **Time above memory saturation** | seconds where `used_ram / total_ram >= 0.80` summed across nodes | memory sample log |
| **CPU util (avg/peak/p95)** | over per-node `cores_in_use / cpu_cores` samples at 1 Hz, averaged across nodes | CPU sample log |
| **Time above CPU saturation** | seconds where CPU util >= threshold summed across nodes | CPU sample log |
| **Scheduling overhead total** | cumulative wall-clock inside `algorithm.schedule()` | scheduler `perf_counter` |
| **Per-decision mean / p95** | per-decision overhead distribution | scheduler decision log |
| **Tasks rejected by class** | count of `rejected_no_fit` per task class | assignment log |
| **Avg response time by class** | per-class mean of response times | event log |

### Output files per trial

1. **`results/runs.csv`** — appended one row per (algorithm, scenario, trial), serialized `TrialResult`. This is the Friedman input.
2. **`results/events/<run_id>.jsonl`** — one JSON per line, fine-grained replay/analysis.

### Friedman test (`scheduler/analysis/friedman.py`)

The Friedman test ranks algorithms within each block (paired observation) and tests whether the rank sums differ significantly across algorithms.

**Blocking unit:** each `comparison_id` is one Friedman block. With 14 scenarios × 5 trials = 70 blocks per algorithm, the test has ample statistical power.

**Application:** one Friedman test per metric in the `TrialResult`. For each of {makespan, avg_response_time, workload_variance, distribution_balance_index, avg_memory_utilization_pct, avg_cpu_utilization_pct, scheduling_overhead_seconds_total, ...}, the analyzer:

1. Pivots `runs.csv` to wide form: rows = `comparison_id`, columns = algorithms, cells = metric value.
2. Drops rows with missing data (rare, but possible if a run failed).
3. Computes the Friedman χ² statistic, degrees of freedom (k − 1 = 1), and p-value.
4. Computes mean rank per algorithm.

Output: `results/analysis/friedman_<metric>.csv` with columns `metric, n_blocks, k_algorithms, chi_square, df, p_value, mean_rank_min_min, mean_rank_adaptive_min_min`. The summary report aggregates all metrics.

```python
# Pseudocode of what the analyzer produces per metric:
{
    "metric": "makespan_seconds",
    "n_blocks": 70,
    "k_algorithms": 2,
    "chi_square": 47.31,
    "df": 1,
    "p_value": 6.1e-12,
    "mean_rank": {"min_min": 1.86, "adaptive_min_min": 1.14},
}
```

### Time-series outputs (`scheduler/analysis/timeseries.py`)

For each (scenario, algorithm) pair, the analyzer reads the JSONL files for all trials and produces:

- One PNG per resource per node showing utilization over time
- One stacked-area chart per scenario per algorithm showing running-task distribution across nodes
- One overlay chart per scenario showing memory utilization for both algorithms side-by-side
- Same overlay for CPU utilization

These are the visual artifacts for RQ2.2 and RQ3 in the thesis.

### Performance summary report (`scheduler/analysis/report.py`)

CLI: `python -m scheduler.analysis.report --scenarios all --metrics all`. Produces `results/analysis/summary.md` with:

- Table of mean ± std per metric per algorithm per scenario
- Friedman test results table (one row per metric, including χ², df, p-value, mean ranks)
- Cross-references to the time-series PNG files
- Pre-formatted as something you can paste into the thesis with minimal editing

---

## 9. Dashboard (`dashboard/`)

Streamlit, multipage. Five pages.

1. **Overview** — system health, current algorithm, queue depth, live node memory + CPU bars.
2. **Nodes** — view/edit node specs (RAM, flash, cores, accepted classes, active toggle), trigger admin reset, inject memory.
3. **Scenarios** — list of performance YAML scenarios, "Run scenario" (all algorithms × all trials), "Run single trial," live progress.
4. **Verification** — list of verification tests, "Run test" / "Run all," live pass/fail, detail expansion for failed assertions, link to JSONL event log.
5. **Results** — `runs.csv` viewer, paired bar charts per metric, time-series overlays, Friedman test results, CSV export.

The dashboard is purely an HTTP client to the scheduler service. Scenario and verification execution run server-side, so triggering a long run doesn't depend on keeping the browser open.

---

## 10. Infrastructure

### `docker-compose.yml`

```yaml
services:
  scheduler:
    build: { context: ., dockerfile: Dockerfile.scheduler }
    ports: ["8000:8000"]
    volumes:
      - ./results:/app/results
      - ./scenarios:/app/scenarios:ro
      - ./tests:/app/tests:ro
    depends_on: [edge1, edge2, edge3, edge4]

  edge1:
    build: { context: ., dockerfile: Dockerfile.edge }
    environment:
      NODE_ID: edge1
      TOTAL_RAM_KB: 8
      USABLE_RAM_KB: 5
      FLASH_KB: 256
      CPU_CORES: 1
      ACCEPTED_CLASSES: "lightweight"
    ports: ["5001:5000"]

  edge2:
    # TOTAL_RAM_KB=128, USABLE_RAM_KB=90, FLASH_KB=1024, CPU_CORES=1
    # ACCEPTED_CLASSES="lightweight,moderate,heavy"

  edge3:
    # TOTAL_RAM_KB=264, USABLE_RAM_KB=185, FLASH_KB=2048, CPU_CORES=2
    # ACCEPTED_CLASSES="lightweight,moderate,heavy,very_heavy"

  edge4:
    # TOTAL_RAM_KB=520, USABLE_RAM_KB=364, FLASH_KB=4096, CPU_CORES=4
    # ACCEPTED_CLASSES="lightweight,moderate,heavy,very_heavy"

  dashboard:
    build: { context: ., dockerfile: Dockerfile.dashboard }
    ports: ["8501:8501"]
    environment:
      SCHEDULER_BASE_URL: http://scheduler:8000
    depends_on: [scheduler]

  prometheus:
  grafana:
```

The standalone `task_generator` container is removed. Task generation lives inside the scheduler service (driven by scenarios and verification tests) so there's a single source of truth.

### `prometheus.yml`

Adds edge4 to the scrape list and adds CPU metric scrape targets.

---

## 11. RQ → metric → output verification table

This is the contract the testbed must satisfy for the performance side.

| RQ | Metric | Source | Output artifact | Friedman? |
|---|---|---|---|---|
| 1.1 Makespan | `makespan_seconds` | event log | runs.csv column + paired bar chart | ✓ |
| 1.2 Avg response time | `avg_response_time_seconds`, p50, p95 | event log | runs.csv columns + CDF plot | ✓ |
| 2.1 Workload variance | `workload_variance` | assignment log | runs.csv column + paired bar chart | ✓ |
| 2.2 Distribution balance over time | `distribution_balance_index` + JSONL timeseries | 1Hz running-task samples | runs.csv column + stacked area chart | ✓ |
| 3.1 CPU utilization | avg / peak / p95 / time-above-saturation | 1Hz CPU samples | runs.csv columns + line chart per node | ✓ |
| 3.2 Memory utilization | avg / peak / p95 / time-above-saturation | 1Hz memory samples | runs.csv columns + line chart per node | ✓ |
| 4.1 Completion patterns | per-class completion stats, CDF, rejection counts | event log | comparative CDF + per-class bar chart + rejection table | ✓ (per-class) |
| 4.2 Load distribution | reuses 2.1 + 2.2 across algorithms | runs.csv | paired bar charts via comparison_id | ✓ (covered above) |
| 4.3 Resource usage | reuses 3.1 + 3.2 across algorithms | runs.csv | paired bar charts via comparison_id | ✓ (covered above) |
| 4.4 Scheduling overhead | total + per-decision mean + p95 | scheduler perf_counter | runs.csv columns + bar chart | ✓ |
| Statistical validation (all RQs) | Friedman χ², df, p, ranks | runs.csv pivoted by comparison_id | analysis/friedman_<metric>.csv + summary.md | ✓ |

---

## 12. Mapping back to the PDF test cases

### Performance scenarios (`scenarios/`)

| PDF test case | YAML file | Output |
|---|---|---|
| Low / Moderate / High Workload | 01–03_*.yaml | TrialResult rows for both algorithms |
| Homogeneous | 04_homogeneous.yaml | TrialResult rows |
| Heterogeneous | 05_heterogeneous.yaml | TrialResult rows |
| Varying Task Memory Demand | 06_varying_demand.yaml | TrialResult rows + class-feasibility checks |
| Dynamic Task Arrival | 07_dynamic_arrival.yaml | TrialResult rows + timing in event log |
| Task Spike | 08_task_spike.yaml | TrialResult rows + resilience visible in CPU/memory plots |
| Resource-Constrained | 09_resource_constrained.yaml | TrialResult rows with tighter feasibility |
| Node Saturation | 10_node_saturation.yaml | TrialResult rows + adaptive trigger observable in event log |
| Makespan / Response Time / Variance / Memory Util / CPU Util / Overhead Eval | 11–15_*_eval.yaml | direct columns in runs.csv |

### Verification tests (`tests/`)

| PDF test case | YAML file | Trials | Algorithms |
|---|---|---|---|
| Alpha Testing | tests/alpha.yaml | 1 | both |
| Beta Testing | tests/beta.yaml | 5 | both |
| White-Box: Task Selection | tests/whitebox/task_selection_logic.yaml | 1 | both |
| White-Box: Node Selection | tests/whitebox/node_selection_logic.yaml | 1 | both |
| White-Box: Adaptive Trigger | tests/whitebox/adaptive_trigger_logic.yaml | 1 | adaptive_min_min only |
| White-Box: Termination | tests/whitebox/termination_logic.yaml | 1 | both |
| Black-Box: Correct Output | tests/blackbox/correct_scheduling_output.yaml | 1 | both |
| Black-Box: Stability Under Load | tests/blackbox/stability_under_load.yaml | 3 sub-runs × 5 | both |
| Black-Box: Behavior Under Dynamic Conditions | tests/blackbox/behavior_under_dynamic_conditions.yaml | 5 | both |

Every test case has a home. Nothing requires bespoke code outside the assertion library.

---

## 13. What's deliberately deferred

To keep scope honest, these are explicitly **out** of v1:

- **Real device emulation.** Simulated nodes in Docker, not actual MCUs. Defensible as a controlled testbed.
- **Network latency simulation.** Could be added via tc/netem; PDF doesn't measure it.
- **Persistent task durability.** No DB. `runs.csv`, JSONL, and verification JSON are sufficient.
- **Authentication.** Internal Docker network only.
- **Live editing of running scenarios.** Reset between trials is enough.
- **Real CPU work.** Simulated CPU pressure (Option A) was chosen because the thesis measures *scheduling decisions*, not computational throughput. Real CPU would introduce Docker-level noise unrelated to the algorithm.
- **Additional baseline algorithms.** Only Min-Min and Adaptive Min-Min are in scope.
- **Post-hoc statistical tests.** With k=2 algorithms, post-hoc tests are unnecessary — there's only one pair to compare.
- **Effect size reporting.** Friedman χ², p-value, and mean ranks per algorithm are the complete statistical output.

---

## 14. Suggested build order

When building, this order keeps the system runnable end-to-end at every step:

1. `shared/models.py`, `shared/task_classes.py`, `shared/constants.py`
2. New edge node with RAM + CPU admission control (`edge/`); test with curl
3. Scheduler skeleton: queue, tick, dispatcher, mirror, plain Min-Min algorithm; submit a single batch manually
4. Trial recorder: event JSONL, decision log, per-trial CSV row
5. Performance scenario YAML format + runner; drive the first PDF scenario end-to-end with one algorithm
6. Multi-algorithm runner: same task set, both algorithms, paired comparison_id
7. Sampling threads (1 Hz memory + CPU + distribution); populate the time-series fields
8. Friedman analysis layer + time-series plots + performance summary report
9. Verification framework: assertion library, verification runner, verification report
10. Verification YAML files for Alpha / Beta / White-Box × 4 / Black-Box × 3
11. Dashboard rewrite (Scenarios, Verification, Results pages)
12. Plug in Adaptive Min-Min once formula is shared
13. Run all 15 scenarios × 2 algorithms × 5 trials, run all 9 verification tests, generate thesis-ready summary and verification report

Steps 1–11 stand up the testbed using only baseline Min-Min. Adaptive Min-Min slots in at step 12 with no architectural changes — that's the proof the plug-and-play interface works.

---

## 15. Defaults locked in

These were resolved during architecture design and are now defaults unless overridden in `constants.py` or a scenario/test file:

| Decision | Default |
|---|---|
| CPU model | Option A: simulated, `cpu_utilization = cores_in_use / cpu_cores` |
| Workload units | Sampled per class: lightweight 5–10, moderate 10–25, heavy 25–50, very_heavy 50–100 |
| Runtime per workload unit | `0.1` seconds |
| Performance trials per scenario | 5 |
| Verification default trial count | 1 (override per test where PDF requires more) |
| Scheduler tick interval | 100 ms |
| Reconciliation poll | 500 ms |
| Sampling rate (memory, CPU, distribution) | 1 Hz |
| Failure semantics | Hold-and-retry with 60s timeout, then `rejected_no_fit` |
| Statistical test | Friedman, blocking unit = comparison_id (scenario × trial) |
| Significance threshold | α = 0.05 |
| Memory saturation threshold | 80% (per PDF) |
| CPU saturation threshold | 80% (mirrors memory for symmetry) |
| Distribution balance index | Coefficient of variation of running-task counts |
| Algorithms in scope | min_min, adaptive_min_min (exactly two) |

---

## 16. Open items

Only one item still pending:

1. **Adaptive Min-Min formula.** Once shared, it slots into `scheduler/algorithms/adaptive_min_min.py`. The `NodeView` interface already exposes both memory and CPU pressure, so the formula can use both.

Once the formula is in, the testbed is complete and the thesis's experimental and testing chapters are one runner invocation each away.
