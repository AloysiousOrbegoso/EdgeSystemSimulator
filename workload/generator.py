"""
Task generation — turns task-class counts into concrete Task instances.

Given a deterministic RNG and a class mix like {lightweight: 4, heavy: 2},
produces the corresponding Task objects with sampled memory_kb and
workload_units. The same seed produces the same tasks, which is what makes
trials repeatable across algorithms (paired observations for Friedman).

Task IDs are deterministic given a (run_id, sequence_number) pair so log
output is human-readable and stable across reruns.
"""

from __future__ import annotations

import random
import time

from shared.models import Task
from shared.task_classes import (
    TASK_CLASS_MEMORY_KB,
    TASK_CLASS_WORKLOAD_UNITS,
    TaskClass,
)


def make_task(
    rng: random.Random,
    task_class: TaskClass,
    task_id: str,
    submitted_at: float | None = None,
    arrival_offset: float = 0.0,
) -> Task:
    """Build one Task by sampling memory and workload from the class ranges."""
    mem_lo, mem_hi = TASK_CLASS_MEMORY_KB[task_class]
    wl_lo, wl_hi = TASK_CLASS_WORKLOAD_UNITS[task_class]
    return Task(
        task_id=task_id,
        task_class=task_class,
        memory_kb=rng.randint(mem_lo, mem_hi),
        workload_units=rng.randint(wl_lo, wl_hi),
        cpu_cores_required=1,
        submitted_at=submitted_at if submitted_at is not None else time.time(),
        arrival_offset=arrival_offset,
    )


def generate_batch(
    rng: random.Random,
    mix: dict[TaskClass, int],
    id_prefix: str,
    next_seq: int,
    arrival_offset: float = 0.0,
) -> tuple[list[Task], int]:
    """Generate a batch of tasks matching the class-count mix.

    Returns (tasks, new_next_seq). The mix is processed in TaskClass
    declaration order so the same mix produces the same task ids regardless
    of dict iteration order.
    """
    tasks: list[Task] = []
    seq = next_seq
    for cls in TaskClass:
        count = mix.get(cls, 0)
        for _ in range(count):
            tid = f"{id_prefix}-{seq:04d}"
            tasks.append(
                make_task(
                    rng=rng,
                    task_class=cls,
                    task_id=tid,
                    arrival_offset=arrival_offset,
                )
            )
            seq += 1
    return tasks, seq


def parse_mix(raw: dict[str, int]) -> dict[TaskClass, int]:
    """Convert a YAML-style mix (string keys) to a TaskClass-keyed dict.

    Unknown class names raise ValueError so a typo in a scenario YAML
    fails fast at load time instead of producing an empty batch silently.
    """
    out: dict[TaskClass, int] = {}
    for k, v in raw.items():
        try:
            cls = TaskClass(k)
        except ValueError:
            valid = sorted(c.value for c in TaskClass)
            raise ValueError(
                f"Unknown task class '{k}'. Valid values: {valid}"
            )
        if not isinstance(v, int) or v < 0:
            raise ValueError(
                f"Mix count for class '{k}' must be a non-negative int, "
                f"got {v!r}"
            )
        out[cls] = v
    return out