# Optimized Adaptive Min-Min Scheduling Testbed

A reproducible testbed for evaluating the **Optimized Adaptive Min–Min Scheduling**
algorithm against the traditional **Min–Min** baseline on a simulated, resource-constrained
heterogeneous edge environment.

This repository is the experimental apparatus for an undergraduate thesis at FEU
Institute of Technology (Bermejo, Garvez, Imperial, Orbegoso — April 2026).

---

## Contents

- [What this is](#what-this-is)
- [Architecture at a glance](#architecture-at-a-glance)
- [Quickstart with Docker](#quickstart-with-docker)
- [Running an experiment end-to-end](#running-an-experiment-end-to-end)
- [Repository layout](#repository-layout)
- [Local development without Docker](#local-development-without-docker)
- [Adding a new scenario or verification test](#adding-a-new-scenario-or-verification-test)
- [Adding a new algorithm](#adding-a-new-algorithm)
- [Troubleshooting](#troubleshooting)
- [Further reading](#further-reading)

---

## What this is

The thesis compares two scheduling algorithms — **plain Min-Min** and an **Adaptive Min-Min**
variant — across the four research questions:

1. **RQ1** — task execution time patterns (makespan, average response time)
2. **RQ2** — workload distribution variation (variance, balance over time)
3. **RQ3** — resource utilization trends (memory, CPU)
4. **RQ4** — adaptive vs traditional Min-Min comparison (completion patterns,
   load distribution, resource usage, scheduling overhead)

Statistical validation uses the **Friedman test** with `comparison_id` as the
blocking unit (paired observations: same task set across both algorithms).

The testbed simulates four heterogeneous edge nodes with the hardware profile
defined in the test specification:

| Node  | Total RAM | Usable RAM (70%) | Flash  | CPU cores | Accepted task classes                           |
| ----- | --------- | ---------------- | ------ | --------- | ----------------------------------------------- |
| edge1 | 8 KB      | 5 KB             | 256 KB | 1         | lightweight                                     |
| edge2 | 128 KB    | 90 KB            | 1 MB   | 1         | lightweight, moderate, heavy                    |
| edge3 | 264 KB    | 185 KB           | 2 MB   | 2         | lightweight, moderate, heavy, very_heavy        |
| edge4 | 520 KB    | 364 KB           | 4 MB   | 4         | lightweight, moderate, heavy, very_heavy (all)  |

Memory saturation threshold is **80% of total RAM** per node.

---

## Architecture at a glance

```
        ┌─────────────────────────┐
        │      Dashboard          │  Streamlit, port 8501
        │  (HTTP client only)     │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────┐        ┌────────────────────┐
        │      Scheduler          │ ◄────► │    Prometheus      │
        │  - queue                │        │   port 9090        │
        │  - pluggable algorithm  │        └────────────────────┘
        │  - dispatcher           │
        │  - trial recorder       │
        │  - 1Hz sampling         │
        │  - analysis (Friedman)  │
        │     port 8000           │
        └────┬──────┬──────┬──────┘
             │      │      │      ...
             ▼      ▼      ▼
        ┌──────┐ ┌──────┐ ┌──────┐
        │edge1 │ │edge2 │ │edge3 │  4 edge containers,
        │ 8KB  │ │128KB │ │264KB │  each simulating one
        └──────┘ └──────┘ └──────┘  resource-constrained node
                                     (edge4 not shown)
```

Two algorithms are pluggable behind one interface (`SchedulingAlgorithm`):
**plain Min-Min** and **Adaptive Min-Min**. The adaptive variant maintains
per-node EWMA estimates of service rate (μ̂_n) and network penalty (π_n),
updated on every observed task completion, and uses them to compute a
dynamic Expected Completion Time:

> ECT(t, n) = Q_n(t₀) + ŵ_t / μ̂_n + π_n

For full detail see `ARCHITECTURE.md` (the design specification this codebase
implements).

---

## Quickstart with Docker

You need Docker Desktop (or Docker Engine + Compose v2 on Linux). No Python
install needed on the host for the basic flow.

```bash
# from the project root
docker compose up --build
```

This brings up:

| Service     | Host port | Purpose                                       |
| ----------- | --------- | --------------------------------------------- |
| scheduler   | 8000      | API + algorithm engine; `/docs` for Swagger UI |
| dashboard   | 8501      | Streamlit UI                                  |
| prometheus  | 9090      | Metrics                                       |
| edge1–4     | (internal)| Simulated edge nodes                          |

Open **`http://localhost:8501`** for the dashboard, or **`http://localhost:8000/docs`**
for the scheduler API.

To stop:

```bash
docker compose down
```

Generated artifacts (CSVs, JSONL event logs, plots, reports) live in `./results/`
on the host (bind-mounted into the scheduler and dashboard containers).

---

## Running an experiment end-to-end

The minimum viable thesis run, in three steps.

### 1. Run a performance scenario

From the dashboard's **Scenarios** page, pick `01_low_workload.yaml` and click
**Run scenario**. It executes 5 trials × 2 algorithms = 10 trial-runs and
appends rows to `results/runs.csv`.

Or from the CLI (running on the host, talking to the dockerized scheduler):

```bash
python -m workload.scenario_runner scenarios/01_low_workload.yaml \
    --scheduler http://localhost:8000
```

To run every scenario in `scenarios/`:

```bash
for f in scenarios/*.yaml; do
    python -m workload.scenario_runner "$f" --scheduler http://localhost:8000
done
```

A full sweep (10 scenarios × 2 algorithms × 5 trials) takes roughly 30–60
minutes depending on workload sizes.

### 2. Run the verification suite

From the dashboard's **Verification** page, click **Run all tests**. Or:

```bash
python -m verification.runner --scheduler http://localhost:8000
```

This runs the 9 verification tests (alpha, beta, 4 white-box, 3 black-box)
and writes `results/verification/report.json`.

### 3. Generate the analysis

From the dashboard's **Results** page, click **Build summary.md**. Or:

```bash
python -m scheduler.analysis.report --results-dir results
python -m verification.report     --results-dir results
```

Outputs:

- `results/runs.csv` — one row per (algorithm, scenario, trial)
- `results/events/<run_id>.jsonl` — fine-grained event log per trial
- `results/analysis/friedman_summary.csv` — Friedman test results per metric
- `results/analysis/timeseries/*.png` — utilization line charts and overlays
- `results/analysis/summary.md` — thesis-ready performance summary
- `results/verification/report.md` — pass/fail verification report
- `results/verification/report.json` — same data, machine-readable

---

## Repository layout

```
thesis/
├── ARCHITECTURE.md             # Design specification (v3)
├── README.md                   # This file
├── docker-compose.yml          # Stack definition
├── Dockerfile.{edge,scheduler,dashboard}
├── prometheus.yml              # Scrape config
├── requirements.txt
│
├── shared/                     # Pydantic models, constants, task classes
│   ├── __init__.py
│   ├── models.py               # Task, NodeSpec, Assignment, TrialResult, ...
│   ├── task_classes.py         # TaskClass enum + memory/workload ranges
│   └── constants.py            # Heterogeneous 4-node profile, EWMA params
│
├── edge/                       # FastAPI service, runs once per edge node
│   ├── edgenode.py             # HTTP layer
│   ├── memory_manager.py       # RAM admission + injection
│   ├── cpu_manager.py          # CPU core admission
│   └── execution.py            # Task lifecycle (admit → sleep → release)
│
├── scheduler/                  # FastAPI service, the orchestrator
│   ├── service.py              # Endpoints + tick + reconcile + sampling
│   ├── queue.py                # Pending task queue
│   ├── node_state.py           # Optimistic mirror of edge state
│   ├── dispatcher.py           # HTTP dispatch with reservation & re-enqueue
│   ├── trial_recorder.py       # JSONL event log + CSV row + 1Hz sampling
│   ├── learned_state.py        # Per-node EWMA (μ̂_n, π_n) for adaptive
│   ├── algorithms/
│   │   ├── base.py             # SchedulingAlgorithm ABC + NodeView
│   │   ├── min_min.py          # Baseline
│   │   ├── adaptive_min_min.py # Proposed (uses learned_state)
│   │   └── __init__.py         # Registry
│   └── analysis/
│       ├── friedman.py         # Per-metric Friedman test
│       ├── timeseries.py       # Plot regeneration from JSONL
│       └── report.py           # summary.md generator
│
├── workload/                   # Task generation & scenario execution
│   ├── generator.py            # Per-class Task sampling (seeded)
│   ├── arrival.py              # Scenario YAML loader, timeline builder
│   └── scenario_runner.py      # End-to-end trial driver (CLI + library)
│
├── verification/               # Pass/fail testing framework
│   ├── runner.py               # Drives a verification test
│   ├── report.py               # JSON → Markdown rendering
│   └── assertions/library.py   # Named, reusable assertions
│
├── dashboard/                  # Streamlit multipage app
│   ├── app.py                  # Entry + sidebar
│   ├── api_client.py           # Wraps the scheduler API
│   └── pages/                  # Overview / Nodes / Scenarios /
│                               #   Verification / Results
│
├── scenarios/                  # Performance scenarios (YAML)
│   └── 01_low_workload.yaml ... 10_node_saturation.yaml
│
├── tests/                      # Verification tests (YAML)
│   ├── alpha.yaml
│   ├── beta.yaml
│   ├── whitebox/  *.yaml
│   └── blackbox/  *.yaml
│
└── results/                    # Generated artifacts (gitignored)
    ├── runs.csv
    ├── events/
    ├── analysis/
    └── verification/
```

---

## Local development without Docker

For iterating on Python code without rebuilding images.

### 1. Set up a virtualenv

```bash
python -m venv .venv
source .venv/bin/activate    # on Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install streamlit         # only the dashboard needs this; not in requirements.txt
```

### 2. Run each component manually

You'll need at least one terminal per service. PowerShell shown for the env vars:

**Edge nodes** (4 separate terminals, one per node):

```powershell
# edge1
$env:NODE_ID="edge1"; $env:TOTAL_RAM_KB="8";   $env:USABLE_RAM_KB="5";   $env:FLASH_KB="256";  $env:CPU_CORES="1"; $env:ACCEPTED_CLASSES="lightweight"
uvicorn edge.edgenode:app --host 0.0.0.0 --port 5001
```

```powershell
# edge2
$env:NODE_ID="edge2"; $env:TOTAL_RAM_KB="128"; $env:USABLE_RAM_KB="90";  $env:FLASH_KB="1024"; $env:CPU_CORES="1"; $env:ACCEPTED_CLASSES="lightweight,moderate,heavy"
uvicorn edge.edgenode:app --host 0.0.0.0 --port 5002
```

```powershell
# edge3
$env:NODE_ID="edge3"; $env:TOTAL_RAM_KB="264"; $env:USABLE_RAM_KB="185"; $env:FLASH_KB="2048"; $env:CPU_CORES="2"; $env:ACCEPTED_CLASSES="lightweight,moderate,heavy,very_heavy"
uvicorn edge.edgenode:app --host 0.0.0.0 --port 5003
```

```powershell
# edge4
$env:NODE_ID="edge4"; $env:TOTAL_RAM_KB="520"; $env:USABLE_RAM_KB="364"; $env:FLASH_KB="4096"; $env:CPU_CORES="4"; $env:ACCEPTED_CLASSES="lightweight,moderate,heavy,very_heavy"
uvicorn edge.edgenode:app --host 0.0.0.0 --port 5004
```

**Scheduler** (5th terminal). Disable autoregister, then register the
nodes manually with the localhost ports:

```powershell
$env:SCHEDULER_AUTOREGISTER="0"
uvicorn scheduler.service:app --host 0.0.0.0 --port 8000
```

Then register each node via `http://localhost:8000/docs` → `POST /nodes/register`,
with the URLs as `http://localhost:5001`, `http://localhost:5002`, etc.

**Dashboard** (6th terminal):

```powershell
$env:SCHEDULER_BASE_URL="http://localhost:8000"
streamlit run dashboard/app.py
```

This is tedious — `docker compose up` is recommended unless you're actively
editing service code.

---

## Adding a new scenario or verification test

Both are just YAML files. No code changes needed.

### A new performance scenario

Drop a file in `scenarios/`, e.g. `scenarios/16_my_test.yaml`:

```yaml
name: my_test
description: |
  ...

nodes: heterogeneous_4node          # or inline a custom node list
algorithms: [min_min, adaptive_min_min]
trials: 5
seed_base: 99000

arrivals:
  - at: 0.0
    tasks:
      lightweight: 4
      moderate: 4
      heavy: 2
      very_heavy: 1
```

It now appears in the dashboard's Scenarios page and works with the CLI runner.

### A new verification test

Drop a file under `tests/` (or `tests/whitebox/` / `tests/blackbox/`):

```yaml
test_id: my_check
type: whitebox
description: |
  ...

nodes: heterogeneous_4node
algorithms: [min_min, adaptive_min_min]
trials: 1
seed: 99100

arrivals:
  - at: 0.0
    tasks: { lightweight: 3, moderate: 2 }

assertions:
  - all_tasks_have_terminal_status
  - no_unsupported_allocations
```

Available assertion names are listed in `verification/assertions/library.py`.

---

## Adding a new algorithm

Useful for ablations and sensitivity analyses, even though the thesis itself
only ships with two. The interface is `scheduler/algorithms/base.py`.

1. Implement a subclass of `SchedulingAlgorithm` with a unique `name`.
2. Register it in `scheduler/algorithms/__init__.py` (eagerly if stateless,
   or via `register_*()` from the scheduler's lifespan if it needs runtime
   state like `LearnedState`).
3. Reference its `name` from any scenario's `algorithms:` list.

The interface is intentionally minimal:

```python
class MyAlgorithm(SchedulingAlgorithm):
    name = "my_algorithm"

    def schedule(
        self,
        pending: list[Task],
        nodes: list[NodeView],
        now: float,
    ) -> list[Assignment]:
        # Pure: read the snapshot, return assignments.
        ...
```

---

## Troubleshooting

**`docker compose up --build` fails on Windows / macOS** — Docker Desktop
must be running and have Linux containers enabled. WSL2 is required on
Windows.

**Dashboard shows "scheduler unreachable"** — the scheduler is still
booting, or you're pointing at the wrong URL. Check
`docker compose logs -f scheduler`. The healthcheck takes ~10s after
`up`.

**Tasks immediately rejected** — check the `accepted_classes` for the
node. Tasks of a class not in `accepted_classes` are rejected by the
edge with reason `class_not_accepted`. This is by design (`edge1` only
accepts `lightweight`).

**Adaptive Min-Min behaves like plain Min-Min on the very first scenario** —
expected. The EWMA service-rate estimates start from a uniform prior
(μ̂_n = 10 units/sec for every node) and only diverge once a few task
completions have been observed. After 1–2 trials of warmup, adaptive's
behavior diverges from baseline. This is also why `/trial/reset` clears
learned state — each trial starts from the same prior, so observations
across algorithms are properly paired for Friedman.

**`results/runs.csv` keeps growing** — runs are append-only by design
(each scenario invocation adds rows). To start fresh, delete or rename
the file:

```bash
rm results/runs.csv results/events/*.jsonl
rm -rf results/analysis
```

**Friedman p-values look suspicious / NaN** — usually means too few
complete blocks. Check that both algorithms have rows for the same
`comparison_id`s (`SELECT comparison_id, algorithm, COUNT(*) ... GROUP BY`
is the diagnostic). The Friedman summary's `n_blocks` column tells you
how many were used.

**Plot regeneration is slow** — matplotlib renders are CPU-bound. A full
sweep (15 scenarios, both algorithms, 5 trials, ~80 PNGs) takes a couple
of minutes on a laptop. The dashboard runs this synchronously; for big
sweeps prefer the CLI:

```bash
python -m scheduler.analysis.report --results-dir results --regen-plots
```

---

## Further reading

- **`ARCHITECTURE.md`** — full design specification: data model, scheduler
  internals, scenario format, verification framework, RQ-to-metric mapping
  table, and the Adaptive Min-Min math.
- **`HotfixHive_TestCases.pdf`** (in the thesis appendix) — the test
  specification this testbed implements.
- **`Adaptive_Min_Min_Scheduling.pdf`** (in the thesis appendix) — the
  formal definition of the proposed algorithm.

If something in the codebase doesn't line up with `ARCHITECTURE.md`, the
architecture doc is the source of truth — file an issue.