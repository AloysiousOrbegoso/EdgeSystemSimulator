"""
Adaptive Min-Min scheduling algorithm.

The structure is identical to plain Min-Min — outer loop picks the global
argmin of completion-time across (task, node) pairs, repeats until no
more placements are possible — but the completion-time function uses
*learned* per-node estimates instead of static workload-unit math.

Per the thesis spec (Adaptive_Min_Min_Scheduling.pdf §3.1.1.2):

    ECT_{t,n} = Q_n(t0) + p̂_{t,n} + π_n

Where:
    Q_n(t0)   = max(0, R_n - t0)              queueing delay
    R_n       = t0 + current_load_n / μ̂_n     predicted ready time
    p̂_{t,n}   = w_t / μ̂_n                    predicted processing time
    π_n        network penalty                 (stubbed to 0 in v1)

After each placement within a tick the algorithm performs a SHADOW
ready-time update:

    R̃_n* ← max(t0, R̃_n*) + p̂_{t*,n*} + π_n*

so subsequent iterations within the same micro-batch see the cost of
already-placed tasks. The scheduler commits the shadow values implicitly
through the dispatcher's reservation pipeline; this algorithm only
maintains the local table.

Feasibility filtering (class, RAM, cores) is identical to plain Min-Min.
The adaptive ECT is purely a *preference* function — it ranks feasible
nodes, not the legality of placement. See ARCHITECTURE.md §10 for the
rationale.

Time complexity per tick: O(B * N * B) = O(B² N) where B = pending tasks
and N = nodes; in our scenarios B ≤ 40 and N = 4, so well under 1ms.
"""

from __future__ import annotations

from shared.constants import SECONDS_PER_WORKLOAD_UNIT
from shared.models import Assignment, Task

from scheduler.algorithms.base import NodeView, SchedulingAlgorithm
from scheduler.learned_state import LearnedState


class AdaptiveMinMinAlgorithm(SchedulingAlgorithm):
    """Adaptive Min-Min — Min-Min selection rule with learned ECT.

    Construct with a LearnedState reference; the scheduler service feeds
    that LearnedState with completion observations. The algorithm reads
    a snapshot of the learned state at the start of each tick.
    """

    name = "adaptive_min_min"

    def __init__(self, learned_state: LearnedState) -> None:
        self._learned = learned_state

    def schedule(
        self,
        pending: list[Task],
        nodes: list[NodeView],
        now: float,
    ) -> list[Assignment]:
        if not pending or not nodes:
            return []

        # Feasibility-active candidate nodes only.
        candidate_nodes = [
            n for n in nodes if n.spec.is_active and n.status.reachable
        ]
        if not candidate_nodes:
            return []

        # Local mirrors. proj_ram and proj_cores serve the same role as in
        # plain Min-Min: they let feasibility checks see earlier placements
        # in this same tick.
        proj_ram: dict[str, int] = {
            n.spec.node_id: n.projected_used_ram_kb for n in candidate_nodes
        }
        proj_cores: dict[str, int] = {
            n.spec.node_id: n.projected_used_cores for n in candidate_nodes
        }
        # current_load_n: sum of remaining workload-units for in-flight
        # tasks on node n. We approximate this from the projected mirror
        # using the relation:
        #       current_load_units = (projected_completion_time - now)
        #                            * μ̂_n
        # This works because projected_completion_time was advanced by
        # expected_runtime, which the dispatcher computed as
        # workload_units * SECONDS_PER_WORKLOAD_UNIT — and SECONDS_PER_
        # WORKLOAD_UNIT corresponds to μ̂ = 1/SECONDS_PER_WORKLOAD_UNIT
        # under the initial prior. After EWMA convergence, the conversion
        # is approximate; the learned μ̂ takes over once a few completions
        # have been observed.
        # Simpler: track free_at directly (already does the right thing).
        free_at: dict[str, float] = {
            n.spec.node_id: n.projected_completion_time for n in candidate_nodes
        }

        # Pull learned state ONCE for the whole tick. The algorithm runs
        # in a few microseconds; pulling once gives a consistent view
        # even if completions are arriving in parallel.
        node_ids = [n.spec.node_id for n in candidate_nodes]
        learned = self._learned.snapshot(node_ids)
        mu_by_node = learned.mu_by_node
        pi_by_node = learned.pi_by_node

        node_by_id: dict[str, NodeView] = {
            n.spec.node_id: n for n in candidate_nodes
        }

        unassigned: list[Task] = list(pending)
        assignments: list[Assignment] = []

        while unassigned:
            best_task: Task | None = None
            best_node_id: str | None = None
            best_ect: float | None = None
            best_processing_time: float | None = None

            for task in unassigned:
                task_best_ect: float | None = None
                task_best_node_id: str | None = None
                task_best_processing_time: float | None = None

                for node_id, view in node_by_id.items():
                    spec = view.spec
                    # --- feasibility ---
                    if task.task_class not in spec.accepted_classes:
                        continue
                    if proj_ram[node_id] + task.memory_kb > spec.usable_ram_kb:
                        continue
                    if proj_cores[node_id] + task.cpu_cores_required > spec.cpu_cores:
                        continue

                    # --- adaptive ECT ---
                    mu = mu_by_node.get(node_id, 1.0 / SECONDS_PER_WORKLOAD_UNIT)
                    pi = pi_by_node.get(node_id, 0.0)
                    # Q_n(t0) = max(0, R_n - t0). free_at[n] is the
                    # scheduler's R̃_n (shadow ready time) carried into
                    # this iteration; the relation R_n = max(t0, R̃_n)
                    # is enforced by the max() below.
                    queueing_delay = max(0.0, free_at[node_id] - now)
                    processing_time = task.workload_units / mu
                    ect = queueing_delay + processing_time + pi

                    if task_best_ect is None or ect < task_best_ect:
                        task_best_ect = ect
                        task_best_node_id = node_id
                        task_best_processing_time = processing_time

                if task_best_ect is None:
                    continue

                if best_ect is None or task_best_ect < best_ect:
                    best_task = task
                    best_node_id = task_best_node_id
                    best_ect = task_best_ect
                    best_processing_time = task_best_processing_time

            if best_task is None:
                break

            assert best_node_id is not None
            assert best_processing_time is not None
            assert best_ect is not None

            # Commit the placement.
            assignments.append(
                Assignment(
                    task_id=best_task.task_id,
                    node_id=best_node_id,
                    decided_at=now,
                    status="dispatched",
                )
            )
            proj_ram[best_node_id] += best_task.memory_kb
            proj_cores[best_node_id] += best_task.cpu_cores_required

            # Shadow ready-time update:
            #     R̃_n* ← max(t0, R̃_n*) + p̂_{t*,n*} + π_n*
            pi_star = pi_by_node.get(best_node_id, 0.0)
            free_at[best_node_id] = (
                max(now, free_at[best_node_id]) + best_processing_time + pi_star
            )
            unassigned.remove(best_task)

        return assignments