"""Quick sanity test for MinMinAlgorithm."""
import time
from shared.task_classes import TaskClass
from shared.models import Task, NodeRuntimeStatus
from shared.constants import HETEROGENEOUS_4NODE
from scheduler.algorithms.base import NodeView
from scheduler.algorithms.min_min import MinMinAlgorithm


def make_view(spec, used_ram=0, used_cores=0, free_at=0.0):
    return NodeView(
        spec=spec,
        status=NodeRuntimeStatus(
            reachable=True,
            used_ram_kb=used_ram,
            available_ram_kb=spec.usable_ram_kb - used_ram,
            cpu_cores_in_use=used_cores,
            active_task_count=used_cores,
        ),
        projected_used_ram_kb=used_ram,
        projected_used_cores=used_cores,
        projected_completion_time=free_at,
    )


def make_task(task_id, task_class, memory_kb, workload_units):
    return Task(
        task_id=task_id,
        task_class=task_class,
        memory_kb=memory_kb,
        workload_units=workload_units,
        submitted_at=time.time(),
    )


# Use the canonical 4-node profile, all empty
nodes = [make_view(spec) for spec in HETEROGENEOUS_4NODE]

# A small varied workload
tasks = [
    make_task("t1", TaskClass.LIGHTWEIGHT, 3, 5),
    make_task("t2", TaskClass.MODERATE, 20, 15),
    make_task("t3", TaskClass.HEAVY, 50, 30),
    make_task("t4", TaskClass.VERY_HEAVY, 120, 60),
    make_task("t5", TaskClass.LIGHTWEIGHT, 4, 8),
]

algo = MinMinAlgorithm()
assignments = algo.schedule(tasks, nodes, now=time.time())

print(f"Placed {len(assignments)} of {len(tasks)} tasks:")
for a in assignments:
    task = next(t for t in tasks if t.task_id == a.task_id)
    print(f"  {a.task_id} ({task.task_class.value}, "
          f"{task.memory_kb}KB, {task.workload_units}u) -> {a.node_id}")

# Check feasibility was respected
node_specs = {spec.node_id: spec for spec in HETEROGENEOUS_4NODE}
for a in assignments:
    task = next(t for t in tasks if t.task_id == a.task_id)
    spec = node_specs[a.node_id]
    assert task.task_class in spec.accepted_classes, \
        f"FEASIBILITY VIOLATION: {task.task_class} on {a.node_id}"
print("\nAll placements respect accepted_classes ✓")

# Specifically: very_heavy must be on edge3 or edge4
vh = [a for a in assignments if a.task_id == "t4"]
if vh:
    assert vh[0].node_id in ("edge3", "edge4"), \
        f"t4 (very_heavy) on {vh[0].node_id} — should be edge3 or edge4"
    print(f"t4 (very_heavy) correctly placed on {vh[0].node_id} ✓")