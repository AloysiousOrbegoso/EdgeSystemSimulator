"""
Task class definitions and per-class resource ranges.

A TaskClass is a coarse category for tasks (lightweight / moderate / heavy /
very_heavy). Each class has an associated memory range (in KB) and workload
range (in compute units), both sampled uniformly when generating tasks.

Memory ranges come from the thesis test specification:
    lightweight     2–5   KB
    moderate        8–24  KB
    heavy           32–64 KB
    very_heavy      96–160 KB

Workload ranges are chosen so that heavier task classes naturally take longer
to execute. Runtime in seconds is derived as:
    runtime = workload_units * SECONDS_PER_WORKLOAD_UNIT
where SECONDS_PER_WORKLOAD_UNIT lives in shared/constants.py.

These tables are the single source of truth — workload generators and
algorithms both read from here.
"""

from enum import Enum


class TaskClass(str, Enum):
    """Coarse task category. The string value is also used in YAML scenario
    files and JSONL event logs, so changing these strings is a wire-format
    breaking change."""

    LIGHTWEIGHT = "lightweight"
    MODERATE = "moderate"
    HEAVY = "heavy"
    VERY_HEAVY = "very_heavy"


# Memory ranges per class, in KB. Inclusive on both ends.
# Sampled uniformly when a workload generator creates a task of this class.
TASK_CLASS_MEMORY_KB: dict[TaskClass, tuple[int, int]] = {
    TaskClass.LIGHTWEIGHT: (2, 5),
    TaskClass.MODERATE: (8, 24),
    TaskClass.HEAVY: (32, 64),
    TaskClass.VERY_HEAVY: (96, 160),
}


# Workload units per class, in abstract compute units. Inclusive on both ends.
# Heavier classes have larger ranges so they take proportionally longer to run.
# Runtime = workload_units * SECONDS_PER_WORKLOAD_UNIT (see constants.py).
TASK_CLASS_WORKLOAD_UNITS: dict[TaskClass, tuple[int, int]] = {
    TaskClass.LIGHTWEIGHT: (5, 10),
    TaskClass.MODERATE: (10, 25),
    TaskClass.HEAVY: (25, 50),
    TaskClass.VERY_HEAVY: (50, 100),
}


def memory_range_for(task_class: TaskClass) -> tuple[int, int]:
    """Return the (min, max) memory range in KB for the given task class."""
    return TASK_CLASS_MEMORY_KB[task_class]


def workload_range_for(task_class: TaskClass) -> tuple[int, int]:
    """Return the (min, max) workload-unit range for the given task class."""
    return TASK_CLASS_WORKLOAD_UNITS[task_class]