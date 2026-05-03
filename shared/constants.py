"""
System-wide constants and the default heterogeneous node profile.

This file is the single source of truth for tunable numeric parameters and
the canonical 4-node configuration from the thesis test specification.
Anything that wants to know "what is the saturation threshold?" or "what
does the default node lineup look like?" imports from here.

Edge nodes loaded from environment variables (in production) override these
defaults. The constants are also used directly by:
  - workload generators (SECONDS_PER_WORKLOAD_UNIT for runtime calculation)
  - the scheduler tick loop (TICK_INTERVAL_SECONDS)
  - the trial recorder (SAMPLE_INTERVAL_SECONDS for 1Hz sampling)
  - the placement-timeout logic (TASK_PLACEMENT_TIMEOUT_SECONDS)

All numeric defaults match Section 15 of ARCHITECTURE.md.
"""

from shared.models import NodeSpec
from shared.task_classes import TaskClass


# ---------------------------------------------------------------------------
# Resource fractions
# ---------------------------------------------------------------------------

# Of the total RAM on a node, only this fraction is allocatable to tasks; the
# rest is reserved for system overhead, communication, and scheduler ops.
# The thesis test spec defines this as 70%.
USABLE_RAM_PCT: float = 0.70

# Memory utilization at or above this fraction of total_ram_kb is considered
# "saturated". Used as the adaptive trigger threshold and as the cutoff for
# the time_above_memory_saturation_seconds metric.
MEMORY_SATURATION_PCT: float = 0.80

# CPU saturation threshold. Mirrors memory for symmetry; not specified by the
# thesis directly, but RQ3 requires CPU utilization as a measured outcome and
# this gives the "time above saturation" metric a defined cutoff.
CPU_SATURATION_PCT: float = 0.80


# ---------------------------------------------------------------------------
# Time and execution constants
# ---------------------------------------------------------------------------

# Runtime per workload unit, in seconds. A task with workload_units=10 takes
# ~1 second to "execute" (actually a time.sleep) on a node. Tunable; raise
# this to slow scenarios down for easier debugging, lower it to run more
# scenarios per minute during regression testing.
SECONDS_PER_WORKLOAD_UNIT: float = 0.1

# How often the scheduler runs its scheduling tick. Lower = more responsive
# but more CPU on the scheduler service. 100 ms is fine for the task volumes
# in our scenarios (max ~40 tasks at once).
TICK_INTERVAL_SECONDS: float = 0.1

# How often the scheduler reconciles its node-state mirror against /status
# from each edge node. Catches drift between optimistic mirror updates and
# actual edge state.
RECONCILE_INTERVAL_SECONDS: float = 0.5

# How often the trial recorder samples per-node memory, CPU, and running-task
# distribution into the JSONL event log. 1 Hz is what the PDF asks for in the
# Memory Utilization Evaluation test case.
SAMPLE_INTERVAL_SECONDS: float = 1.0

# If a task cannot be placed on any feasible node within this many seconds
# (because every feasible node is full), the scheduler gives up and records
# it as rejected_no_fit. Prevents the queue from growing unboundedly while
# still satisfying the "no task should be skipped" intent of Alpha testing
# under normal conditions.
TASK_PLACEMENT_TIMEOUT_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Statistical analysis constants
# ---------------------------------------------------------------------------

# Significance threshold for the Friedman test. Comparison metrics with
# p < FRIEDMAN_ALPHA are reported as statistically significant in the
# performance summary report.
FRIEDMAN_ALPHA: float = 0.05


# ---------------------------------------------------------------------------
# Heterogeneous 4-node profile (the thesis baseline configuration)
# ---------------------------------------------------------------------------
#
# These four NodeSpecs encode the test specification verbatim:
#
#   Node    Total RAM   Usable RAM   Flash    Saturation@80%   Cores
#   edge1     8 KB         5 KB      256 KB      6 KB           1
#   edge2   128 KB        90 KB        1 MB    102 KB           1
#   edge3   264 KB       185 KB        2 MB    211 KB           2
#   edge4   520 KB       364 KB        4 MB    416 KB           4
#
# Allocation rules (from accepted_classes):
#   edge1: lightweight only
#   edge2: lightweight, moderate, heavy
#   edge3: lightweight, moderate, heavy, very_heavy
#   edge4: all classes (most capable node)
#
# Core counts are a defensible mapping to constrained device classes
# (Arduino-class single core through RPi-Zero-class quad core). Tunable here
# without touching scheduler or algorithm code.
#
# URLs use Docker-Compose service names; on a non-Docker setup, override via
# the scheduler's /nodes registration endpoint or environment variables.

HETEROGENEOUS_4NODE: list[NodeSpec] = [
    NodeSpec(
        node_id="edge1",
        url="http://edge1:5000",
        total_ram_kb=8,
        usable_ram_kb=5,
        flash_kb=256,
        saturation_threshold_kb=6,
        cpu_cores=1,
        accepted_classes=[TaskClass.LIGHTWEIGHT],
    ),
    NodeSpec(
        node_id="edge2",
        url="http://edge2:5000",
        total_ram_kb=128,
        usable_ram_kb=90,
        flash_kb=1024,
        saturation_threshold_kb=102,
        cpu_cores=1,
        accepted_classes=[
            TaskClass.LIGHTWEIGHT,
            TaskClass.MODERATE,
            TaskClass.HEAVY,
        ],
    ),
    NodeSpec(
        node_id="edge3",
        url="http://edge3:5000",
        total_ram_kb=264,
        usable_ram_kb=185,
        flash_kb=2048,
        saturation_threshold_kb=211,
        cpu_cores=2,
        accepted_classes=[
            TaskClass.LIGHTWEIGHT,
            TaskClass.MODERATE,
            TaskClass.HEAVY,
            TaskClass.VERY_HEAVY,
        ],
    ),
    NodeSpec(
        node_id="edge4",
        url="http://edge4:5000",
        total_ram_kb=520,
        usable_ram_kb=364,
        flash_kb=4096,
        saturation_threshold_kb=416,
        cpu_cores=4,
        accepted_classes=[
            TaskClass.LIGHTWEIGHT,
            TaskClass.MODERATE,
            TaskClass.HEAVY,
            TaskClass.VERY_HEAVY,
        ],
    ),
]


# Named profile registry. Scenario YAML files reference profiles by name
# (e.g., `nodes: heterogeneous_4node`), and the scenario runner looks them
# up here. Add new presets by appending to this dict.
NODE_PROFILES: dict[str, list[NodeSpec]] = {
    "heterogeneous_4node": HETEROGENEOUS_4NODE,
}


def get_node_profile(name: str) -> list[NodeSpec]:
    """Return the named node profile, or raise KeyError with a helpful message.

    Used by the scenario runner when resolving the `nodes:` field of a YAML
    file. If a scenario inlines a custom node list instead of naming a preset,
    this function is bypassed entirely.
    """
    if name not in NODE_PROFILES:
        available = ", ".join(sorted(NODE_PROFILES.keys()))
        raise KeyError(
            f"Unknown node profile '{name}'. Available profiles: {available}"
        )
    # Return a fresh list each time so callers cannot accidentally mutate the
    # canonical preset. The NodeSpec instances themselves are still shared,
    # but Pydantic models are effectively immutable for our use cases.
    return list(NODE_PROFILES[name])