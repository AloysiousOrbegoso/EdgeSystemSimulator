"""
Scenario runner — executes one scenario end-to-end.

The runner is a client of the scheduler service (and the edge nodes). It
does not run inside the scheduler service's process — it talks to it via
HTTP. This separation matters because:
  - Scenarios can be triggered from CLI, the dashboard, or test code.
  - The scheduler stays focused on serving HTTP; long-running scenarios
    don't tie up FastAPI handlers.
  - A scenario can be aborted by killing the runner process without
    corrupting the scheduler.

For each scenario, the runner does:

    for each trial in 1..scenario.trials:
        seed = scenario.seed_base + trial
        timeline = build_task_timeline(scenario, seed, id_prefix=...)
        for each algorithm in scenario.algorithms:
            POST /trial/reset                       # clean slate
            POST /config/algorithm                  # switch algo
            POST /nodes/register (for each node)    # ensure config
            POST /trial/start                       # open event log
            replay timeline at wall-clock pace
            wait for queue + in-flight to drain
            POST /trial/end                         # write CSV row

CLI:
    python -m workload.scenario_runner scenarios/01_low_workload.yaml \
        --scheduler http://localhost:8000

The CLI is mostly for development. The dashboard (later) will call the
same runner functions in-process via the scheduler's API.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from shared.models import NodeSpec, Task, TrialResult

from workload.arrival import (
    Scenario,
    TimelineEvent,
    build_task_timeline,
    load_scenario,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# Maximum time we wait for the queue and in-flight tasks to drain after the
# last timeline event, before declaring the trial finished. Prevents a
# stuck scenario from running forever.
DRAIN_TIMEOUT_SECONDS = 600.0
DRAIN_POLL_INTERVAL = 0.5


@dataclass
class RunnerConfig:
    scheduler_url: str = "http://localhost:8000"
    request_timeout_seconds: float = 5.0
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_scenario(
    scenario_path: Path | str,
    config: RunnerConfig | None = None,
) -> list[TrialResult]:
    """Execute every trial × algorithm in a scenario file.

    Returns a TrialResult for each (algorithm, trial) pair. The CSV row
    has already been appended by the scheduler at /trial/end; the returned
    list is for in-process consumers (e.g., the dashboard) that want to
    show progress without parsing the CSV.
    """
    cfg = config or RunnerConfig()
    scenario = load_scenario(scenario_path)
    return run_loaded_scenario(scenario, cfg)


def run_loaded_scenario(
    scenario: Scenario, config: RunnerConfig
) -> list[TrialResult]:
    """Same as run_scenario but takes an already-parsed Scenario."""
    results: list[TrialResult] = []
    with httpx.Client(
        base_url=config.scheduler_url,
        timeout=config.request_timeout_seconds,
    ) as client:
        # Register nodes once at the start. Re-registration is idempotent
        # in NodeState — it discards reservations, but we /trial/reset
        # before each trial anyway, so this is safe.
        _register_nodes(client, scenario.nodes)

        for trial_index in range(1, scenario.trials + 1):
            seed = scenario.seed_base + trial_index
            comparison_id = f"{scenario.name}_{trial_index}"

            # Build the task timeline ONCE per trial. The same task list
            # is replayed against each algorithm — this is what makes the
            # observations paired (Friedman blocking).
            id_prefix = f"{scenario.name}-t{trial_index}"
            timeline = build_task_timeline(
                scenario=scenario, seed=seed, id_prefix=id_prefix
            )
            total_tasks_in_timeline = sum(
                len(ev.tasks) for ev in timeline if ev.kind == "submit_tasks"
            )
            logger.info(
                "Trial %d/%d: %d events, %d tasks, seed=%d",
                trial_index,
                scenario.trials,
                len(timeline),
                total_tasks_in_timeline,
                seed,
            )

            for algorithm in scenario.algorithms:
                run_id = (
                    f"{scenario.name}_{algorithm}_t{trial_index}_{int(time.time())}"
                )
                logger.info(
                    "  → Running %s (run_id=%s)", algorithm, run_id
                )

                # 1. Hard reset: clears queue, reservations, edge state.
                _post_ack(client, "/trial/reset")

                # 2. Switch algorithm.
                _post(
                    client,
                    "/config/algorithm",
                    json={"name": algorithm},
                )

                # 3. Re-register nodes (in case /trial/reset cleared them
                # in a future change; safe and cheap right now).
                _register_nodes(client, scenario.nodes)

                # 4. Start trial: opens event log, enables sampling.
                _post(
                    client,
                    "/trial/start",
                    json={
                        "run_id": run_id,
                        "scenario_name": scenario.name,
                        "algorithm": algorithm,
                        "trial_index": trial_index,
                        "comparison_id": comparison_id,
                        "seed": seed,
                    },
                )

                # 5. Replay timeline at wall-clock pace.
                t0 = time.time()
                _replay_timeline(client, timeline, t0)

                # 6. Wait for drain.
                _wait_for_drain(client, config.drain_timeout_seconds)
                time.sleep(1.5)

                # 7. End trial: writes CSV row, returns TrialResult.
                resp = _post(client, "/trial/end")
                result = TrialResult(**resp.json())
                results.append(result)
                logger.info(
                    "    done: makespan=%.2fs avg_rt=%.2fs completed=%d/%d",
                    result.makespan_seconds,
                    result.avg_response_time_seconds,
                    result.tasks_completed,
                    result.tasks_total,
                )

    return results


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _register_nodes(client: httpx.Client, nodes: list[NodeSpec]) -> None:
    """POST /nodes/register for each NodeSpec in the scenario."""
    for spec in nodes:
        _post(
            client,
            "/nodes/register",
            json=spec.model_dump(mode="json"),
        )


def _replay_timeline(
    client: httpx.Client, timeline: list[TimelineEvent], t0: float
) -> None:
    """Wait until each event's offset and execute it against the scheduler.

    Sleeps in short slices so wall-clock drift accumulated from earlier
    HTTP calls does not push later events out of order. The scheduler
    timestamps tasks at submission, so the recorded submitted_at fields
    reflect actual replay time.
    """
    for ev in timeline:
        target = t0 + ev.at
        while True:
            now = time.time()
            if now >= target:
                break
            time.sleep(min(target - now, 0.05))
        if ev.kind == "submit_tasks":
            _submit_batch(client, ev.tasks)
        elif ev.kind == "inject_memory":
            _inject_memory(
                node_id=ev.node_id or "",
                kb=ev.inject_kb,
                hold_seconds=ev.inject_hold_seconds,
                client=client,
            )


def _submit_batch(client: httpx.Client, tasks: list[Task]) -> None:
    payload = [t.model_dump(mode="json") for t in tasks]
    _post(client, "/submit_batch", json=payload)


def _inject_memory(
    node_id: str, kb: int, hold_seconds: float, client: httpx.Client
) -> None:
    """Send /admin/inject_memory directly to the named edge node.

    The runner needs the node's URL, which it gets by querying the
    scheduler's /nodes endpoint. We do this lazily to avoid an extra round
    trip per replay.
    """
    nodes_resp = _get(client, "/nodes")
    node_url = None
    for n in nodes_resp.json().get("nodes", []):
        if n["spec"]["node_id"] == node_id:
            node_url = n["spec"]["url"]
            break
    if not node_url:
        logger.warning(
            "Cannot inject memory: node_id=%s not registered", node_id
        )
        return
    # Use a separate, short-lived client targeted at the edge node URL.
    with httpx.Client(timeout=2.0) as edge_client:
        try:
            resp = edge_client.post(
                f"{node_url}/admin/inject_memory",
                json={"kb": kb, "hold_seconds": hold_seconds},
            )
            resp.raise_for_status()
            logger.info(
                "Injected %d KB on %s for %.1fs", kb, node_id, hold_seconds
            )
        except Exception as exc:
            logger.warning(
                "Memory inject failed for %s: %s", node_id, exc
            )


def _wait_for_drain(client: httpx.Client, timeout_seconds: float) -> None:
    """Block until queue is empty AND no node has active tasks."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = _get(client, "/system/state").json()
        queue_size = state.get("queue_size", 0)
        nodes = _get(client, "/nodes").json().get("nodes", [])
        active_total = sum(
            int(n.get("status", {}).get("active_task_count", 0)) for n in nodes
        )
        if queue_size == 0 and active_total == 0:
            return
        time.sleep(DRAIN_POLL_INTERVAL)
    logger.warning(
        "Drain timeout after %.0fs; ending trial anyway", timeout_seconds
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(client: httpx.Client, path: str, json: Any = None) -> httpx.Response:
    resp = client.post(path, json=json)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"POST {path} returned {resp.status_code}: {resp.text}"
        )
    return resp


def _post_ack(client: httpx.Client, path: str) -> httpx.Response:
    """POST that tolerates a non-200 response if the body has ok=False but
    is otherwise structured. Used for /trial/reset which may return ok=False
    on partial edge reset failures — we want to log and continue."""
    resp = client.post(path)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"POST {path} returned {resp.status_code}: {resp.text}"
        )
    body = resp.json()
    if isinstance(body, dict) and not body.get("ok", True):
        logger.warning("%s soft-failed: %s", path, body.get("detail"))
    return resp


def _get(client: httpx.Client, path: str) -> httpx.Response:
    resp = client.get(path)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GET {path} returned {resp.status_code}: {resp.text}"
        )
    return resp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a scenario file against a scheduler service"
    )
    parser.add_argument(
        "scenario", type=Path, help="Path to scenario YAML file"
    )
    parser.add_argument(
        "--scheduler",
        default="http://localhost:8000",
        help="Scheduler base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=DRAIN_TIMEOUT_SECONDS,
        help="Seconds to wait for queue + in-flight to drain per trial",
    )
    args = parser.parse_args(argv)

    cfg = RunnerConfig(
        scheduler_url=args.scheduler,
        drain_timeout_seconds=args.drain_timeout,
    )
    try:
        results = run_scenario(args.scenario, cfg)
    except Exception:
        logger.exception("Scenario run failed")
        return 1
    logger.info(
        "Scenario complete: %d trial-runs written to runs.csv", len(results)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())