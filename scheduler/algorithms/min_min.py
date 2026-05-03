"""
Plain Min-Min scheduling algorithm — the thesis baseline.

Min-Min is a classic batch scheduling heuristic. Given a set of unassigned
tasks T and a set of machines M:

    while T is not empty:
        for each task t in T:
            compute the minimum completion time of t across all feasible
            machines; remember which machine achieved that minimum
        pick the (task, machine) pair with the globally smallest minimum
        assign the task to that machine
        update the machine's projected availability
        remove the task from T

The intent is to "place the cheapest job first on the cheapest machine that
fits it." It greedy-minimizes per-iteration completion time, which tends to
produce short makespans on heterogeneous fleets when no single machine is
overloaded — and is a known-bad fit when one machine is much slower than the
rest, because Min-Min can starve large tasks. The thesis's adaptive variant
addresses that weakness; this baseline implementation does not.

Feasibility for our edge testbed is the conjunction of three conditions:
  1. The node accepts the task's class (NodeSpec.accepted_classes)
  2. The node has enough projected free RAM (memory_kb fits)
  3. The node has enough projected free cores (cpu_cores_required fits)

Tasks that cannot be placed in this tick (no feasible node) remain in the
pending queue. The scheduler retries them next tick under hold-and-retry
semantics; only after TASK_PLACEMENT_TIMEOUT_SECONDS does a stuck task get
recorded as rejected_no_fit.

Completion time on a node is computed as:
    free_at[node] + workload_units * SECONDS_PER_WORKLOAD_UNIT

where free_at[node] starts at NodeView.projected_completion_time (the time
that node will next have free capacity) and is advanced as Min-Min places
tasks onto it within the same tick.
"""

from __future__ import annotations

from shared.constants import SECONDS_PER_WORKLOAD_UNIT
from shared.models import Assignment, Task

from scheduler.algorithms.base import NodeView, SchedulingAlgorithm


class MinMinAlgorithm(SchedulingAlgorithm):
    """Plain Min-Min — picks (task, machine) with globally smallest
    completion time, repeats until no more placements are possible."""

    name = "min_min"

    def schedule(
        self,
        pending: list[Task],
        nodes: list[NodeView],
        now: float,
    ) -> list[Assignment]:
        if not pending or not nodes:
            return []

        # Filter to active, reachable nodes once. Inactive or unreachable
        # nodes stay invisible to the algorithm for the whole tick — even
        # if they recover mid-tick, the scheduler will pick them up next
        # tick after reconciliation.
        candidate_nodes = [
            n for n in nodes if n.spec.is_active and n.status.reachable
        ]
        if not candidate_nodes:
            return []

        # Local mirror of node state. Min-Min mutates these dicts as it
        # places each task, so subsequent task placements within the same
        # tick see the updated availability. Using node_id as the key
        # keeps the lookups O(1) and avoids holding references to NodeView
        # objects that the scheduler might be replacing concurrently.
        proj_ram: dict[str, int] = {
            n.spec.node_id: n.projected_used_ram_kb for n in candidate_nodes
        }
        proj_cores: dict[str, int] = {
            n.spec.node_id: n.projected_used_cores for n in candidate_nodes
        }
        free_at: dict[str, float] = {
            n.spec.node_id: n.projected_completion_time for n in candidate_nodes
        }
        # Lookup back to the NodeView for spec fields (cores, RAM caps,
        # accepted classes). Read-only after this point.
        node_by_id: dict[str, NodeView] = {
            n.spec.node_id: n for n in candidate_nodes
        }

        unassigned: list[Task] = list(pending)
        assignments: list[Assignment] = []

        # The outer loop runs until either:
        #   - all tasks are placed, or
        #   - no remaining task has a feasible machine right now
        # Each iteration runs the Min-Min inner pass: O(T * M) per
        # iteration, O(T^2 * M) overall. With T <= 40 and M = 4 in our
        # scenarios, that is well under a millisecond per tick.
        while unassigned:
            best_task: Task | None = None
            best_node_id: str | None = None
            best_completion: float | None = None

            for task in unassigned:
                # Find the best (lowest completion time) feasible node
                # for this single task. If no node is feasible, this
                # task contributes nothing to the iteration.
                task_best_completion: float | None = None
                task_best_node_id: str | None = None

                for node_id, view in node_by_id.items():
                    spec = view.spec
                    # Feasibility check (1): class
                    if task.task_class not in spec.accepted_classes:
                        continue
                    # Feasibility check (2): memory
                    if proj_ram[node_id] + task.memory_kb > spec.usable_ram_kb:
                        continue
                    # Feasibility check (3): CPU cores
                    if proj_cores[node_id] + task.cpu_cores_required > spec.cpu_cores:
                        continue

                    # All three feasibility checks passed. Compute
                    # completion time on this node.
                    completion = (
                        free_at[node_id]
                        + task.workload_units * SECONDS_PER_WORKLOAD_UNIT
                    )

                    if (
                        task_best_completion is None
                        or completion < task_best_completion
                    ):
                        task_best_completion = completion
                        task_best_node_id = node_id

                if task_best_completion is None:
                    # No feasible node for this task in this iteration.
                    continue

                # Track the global minimum across all tasks.
                if (
                    best_completion is None
                    or task_best_completion < best_completion
                ):
                    best_task = task
                    best_node_id = task_best_node_id
                    best_completion = task_best_completion

            if best_task is None:
                # No remaining task can be placed on any feasible node.
                # Stop and let the scheduler retry next tick.
                break

            # Place the chosen (task, node) pair. We know best_node_id and
            # best_completion are set whenever best_task is set.
            assert best_node_id is not None
            assert best_completion is not None

            assignments.append(
                Assignment(
                    task_id=best_task.task_id,
                    node_id=best_node_id,
                    decided_at=now,
                    status="dispatched",
                )
            )
            # Update the local mirror so future iterations see this
            # placement's resource cost.
            proj_ram[best_node_id] += best_task.memory_kb
            proj_cores[best_node_id] += best_task.cpu_cores_required
            free_at[best_node_id] = best_completion
            unassigned.remove(best_task)

        return assignments