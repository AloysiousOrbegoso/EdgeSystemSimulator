"""
Verification test runner.

Reads a verification YAML file (under tests/), executes one or more trials
against the running scheduler, builds a TestContext from the JSONL event
log, evaluates the named assertions, and returns a VerificationVerdict per
(algorithm, sub_run, trial) combination.

The runner is a client of the scheduler service, just like the scenario
runner. It does NOT run inside the scheduler process.

YAML format (see ARCHITECTURE.md §7 for full spec):

    test_id: alpha
    type: alpha                                 # alpha | beta | whitebox | blackbox
    description: |
      ...
    nodes: heterogeneous_4node                  # or inline NodeSpec list
    algorithms: [min_min, adaptive_min_min]
    trials: 1                                   # default; per-test override OK
    seed: 1000                                  # OR seed_base for trials > 1
    arrivals:
      - at: 0.0
        tasks: { lightweight: 3, moderate: 2, heavy: 2, very_heavy: 1 }
    events:                                     # optional (inject_memory, ...)
      - ...
    assertions:
      - all_tasks_have_terminal_status
      - no_unsupported_allocations
      - ...
    sub_runs:                                   # optional, for stability_under_load
      - name: low_load
        arrivals: [...]
        trials: 5
      - ...

CLI:
    python -m verification.runner tests/alpha.yaml \
        --scheduler http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from shared.models import (
    AssertionResult,
    NodeSpec,
    Task,
    VerificationReport,
    VerificationVerdict,
)

from verification.assertions.library import TestContext, get_assertion
from workload.arrival import (
    ArrivalSpec,
    EventSpec,
    GeneratorArrivalSpec,
    Scenario,
    TimelineEvent,
    _parse_arrival_entry,    # internal but stable
    _parse_event_entry,
    _resolve_nodes,
    build_task_timeline,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# Per-trial drain timeout, mirrors the scenario runner.
DRAIN_TIMEOUT_SECONDS = 600.0
DRAIN_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# Parsed verification test
# ---------------------------------------------------------------------------


@dataclass
class _SubRun:
    name: str
    arrivals: list
    events: list
    trials: int


@dataclass
class VerificationTest:
    """In-memory representation of a parsed test YAML."""

    test_id: str
    test_type: str                              # "alpha" | "beta" | "whitebox" | "blackbox"
    description: str
    nodes: list[NodeSpec]
    algorithms: list[str]
    seed_base: int
    assertions: list[str]
    # If sub_runs is non-empty, the runner ignores top-level arrivals/
    # events/trials and uses each sub-run instead.
    sub_runs: list[_SubRun]
    # Top-level arrivals/events/trials, used when sub_runs is empty.
    arrivals: list = None  # type: ignore[assignment]
    events: list = None    # type: ignore[assignment]
    trials: int = 1


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_test(path: Path | str) -> VerificationTest:
    """Parse a verification YAML file into a VerificationTest."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Test {path} did not parse to a dict")

    source = str(path)
    test_id = raw.get("test_id") or path.stem
    test_type = raw.get("type")
    if test_type not in {"alpha", "beta", "whitebox", "blackbox"}:
        raise ValueError(
            f"{source}: 'type' must be one of alpha|beta|whitebox|blackbox"
        )
    description = raw.get("description", "")
    nodes = _resolve_nodes(raw.get("nodes"), source)

    algorithms = raw.get("algorithms")
    if not algorithms or not isinstance(algorithms, list):
        raise ValueError(f"{source}: 'algorithms' must be a non-empty list")
    seed_base = int(raw.get("seed_base", raw.get("seed", 0)))

    assertions = raw.get("assertions") or []
    if not isinstance(assertions, list) or not all(isinstance(a, str) for a in assertions):
        raise ValueError(f"{source}: 'assertions' must be a list of strings")
    # Validate each name is registered before we run anything.
    for a in assertions:
        try:
            get_assertion(a)
        except KeyError as e:
            raise ValueError(f"{source}: {e}")

    sub_runs_raw = raw.get("sub_runs")
    sub_runs: list[_SubRun] = []
    if isinstance(sub_runs_raw, list) and sub_runs_raw:
        for i, entry in enumerate(sub_runs_raw):
            if not isinstance(entry, dict):
                raise ValueError(f"{source}.sub_runs[{i}] must be a dict")
            name = entry.get("name") or f"sub_{i}"
            sr_arrivals = [
                _parse_arrival_entry(e, f"{source}.sub_runs[{i}].arrivals[{j}]")
                for j, e in enumerate(entry.get("arrivals", []) or [])
            ]
            sr_events = [
                _parse_event_entry(e, f"{source}.sub_runs[{i}].events[{j}]")
                for j, e in enumerate(entry.get("events", []) or [])
            ]
            sr_trials = int(entry.get("trials", 1))
            sub_runs.append(
                _SubRun(name=name, arrivals=sr_arrivals, events=sr_events, trials=sr_trials)
            )

    arrivals = [
        _parse_arrival_entry(e, f"{source}.arrivals[{i}]")
        for i, e in enumerate(raw.get("arrivals", []) or [])
    ]
    events = [
        _parse_event_entry(e, f"{source}.events[{i}]")
        for i, e in enumerate(raw.get("events", []) or [])
    ]
    trials = int(raw.get("trials", 1))

    return VerificationTest(
        test_id=test_id,
        test_type=test_type,
        description=description,
        nodes=nodes,
        algorithms=list(algorithms),
        seed_base=seed_base,
        assertions=assertions,
        sub_runs=sub_runs,
        arrivals=arrivals,
        events=events,
        trials=trials,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_test(
    test_path: Path | str,
    scheduler_url: str = "http://localhost:8000",
    results_dir: Path | str = "results",
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
) -> list[VerificationVerdict]:
    """Run one verification test file end-to-end. Returns one verdict per
    (algorithm, sub_run, trial) combination."""
    test = load_test(test_path)
    return run_loaded_test(
        test=test,
        scheduler_url=scheduler_url,
        results_dir=Path(results_dir),
        drain_timeout_seconds=drain_timeout_seconds,
    )


def run_all_tests(
    tests_dir: Path | str = "tests",
    scheduler_url: str = "http://localhost:8000",
    results_dir: Path | str = "results",
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
) -> VerificationReport:
    """Run every *.yaml under tests_dir (recursively). Aggregates verdicts
    and writes results/verification/report.{md,json}."""
    tests_dir = Path(tests_dir)
    if not tests_dir.exists():
        raise FileNotFoundError(f"tests directory not found: {tests_dir}")

    started_at = time.time()
    all_verdicts: list[VerificationVerdict] = []
    for path in sorted(tests_dir.rglob("*.yaml")):
        logger.info("Running verification test: %s", path)
        try:
            verdicts = run_test(
                test_path=path,
                scheduler_url=scheduler_url,
                results_dir=results_dir,
                drain_timeout_seconds=drain_timeout_seconds,
            )
        except Exception:
            logger.exception("Test %s raised; continuing", path)
            continue
        all_verdicts.extend(verdicts)
    completed_at = time.time()

    report = VerificationReport(
        started_at=started_at,
        completed_at=completed_at,
        total_verdicts=len(all_verdicts),
        passed=sum(1 for v in all_verdicts if v.overall_passed),
        failed=sum(1 for v in all_verdicts if not v.overall_passed),
        verdicts=all_verdicts,
    )
    _write_report(report, results_dir=Path(results_dir))
    return report


def run_loaded_test(
    test: VerificationTest,
    scheduler_url: str,
    results_dir: Path,
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
) -> list[VerificationVerdict]:
    """Run an already-parsed VerificationTest."""
    verdicts: list[VerificationVerdict] = []

    # Build the iteration set: list of (sub_run_name | None, arrivals,
    # events, trials).
    iter_blocks: list[tuple[str | None, list, list, int]] = []
    if test.sub_runs:
        for sr in test.sub_runs:
            iter_blocks.append((sr.name, sr.arrivals, sr.events, sr.trials))
    else:
        iter_blocks.append((None, test.arrivals, test.events, test.trials))

    with httpx.Client(base_url=scheduler_url, timeout=5.0) as client:
        # Register nodes once at the start.
        _register_nodes(client, test.nodes)

        for sub_run, arrivals, events, trials in iter_blocks:
            for trial_index in range(1, trials + 1):
                seed = test.seed_base + trial_index
                # Build a tiny ad-hoc Scenario so we can reuse
                # build_task_timeline without copying its logic.
                scenario = Scenario(
                    name=test.test_id + (f"_{sub_run}" if sub_run else ""),
                    nodes=test.nodes,
                    algorithms=test.algorithms,
                    trials=trials,
                    seed_base=test.seed_base,
                    arrivals=arrivals,
                    events=events,
                )
                id_prefix = f"{scenario.name}-t{trial_index}"
                timeline = build_task_timeline(
                    scenario=scenario, seed=seed, id_prefix=id_prefix
                )

                for algorithm in test.algorithms:
                    run_id = (
                        f"verif_{test.test_id}"
                        + (f"_{sub_run}" if sub_run else "")
                        + f"_{algorithm}_t{trial_index}_{int(time.time())}"
                    )
                    verdict = _run_one_trial(
                        client=client,
                        test=test,
                        sub_run=sub_run,
                        trial_index=trial_index if trials > 1 else None,
                        algorithm=algorithm,
                        run_id=run_id,
                        timeline=timeline,
                        results_dir=results_dir,
                        drain_timeout_seconds=drain_timeout_seconds,
                    )
                    verdicts.append(verdict)

    return verdicts


# ---------------------------------------------------------------------------
# Per-trial execution
# ---------------------------------------------------------------------------


def _run_one_trial(
    client: httpx.Client,
    test: VerificationTest,
    sub_run: str | None,
    trial_index: int | None,
    algorithm: str,
    run_id: str,
    timeline: list[TimelineEvent],
    results_dir: Path,
    drain_timeout_seconds: float,
) -> VerificationVerdict:
    """Reset, configure, replay timeline, drain, end trial, evaluate
    assertions. Returns a VerificationVerdict."""
    # 1. Hard reset.
    _post_ack(client, "/trial/reset")

    # 2. Switch algorithm.
    _post(client, "/config/algorithm", json={"name": algorithm})

    # 3. Re-register nodes (idempotent; cheap).
    _register_nodes(client, test.nodes)

    # 4. Start trial.
    _post(
        client,
        "/trial/start",
        json={
            "run_id": run_id,
            "scenario_name": test.test_id,
            "algorithm": algorithm,
            "trial_index": trial_index or 1,
            "comparison_id": run_id,         # verification doesn't use Friedman
            "seed": test.seed_base + (trial_index or 1),
        },
    )

    started_at = time.time()
    # 5. Replay timeline.
    timed_out = False
    try:
        _replay_timeline(client, timeline, started_at)
        # 6. Wait for drain.
        timed_out = not _wait_for_drain(client, drain_timeout_seconds)
        # 6b. Brief grace period for the scheduler's reconcile poller to
        # observe the final completions and write task_completed events
        # into the JSONL log. The reconcile interval is 500ms, so two
        # reconcile cycles (1s) is enough; we use 1.5s for safety. This
        # only matters for very short trials (alpha-style fixed sets);
        # longer trials drain naturally with these events already written.
        if not timed_out:
            time.sleep(1.5)
    except Exception:
        logger.exception("Trial %s replay failed; ending anyway", run_id)
        timed_out = True
    duration = time.time() - started_at

    # 7. End trial.
    try:
        end_resp = _post(client, "/trial/end")
        # The body is a TrialResult dict; we don't strictly need it for
        # verification but parsing it confirms the trial closed cleanly.
        end_body = end_resp.json()
    except Exception:
        logger.exception("Trial /trial/end failed for %s", run_id)
        end_body = {}

    completed_at = time.time()

    # 8. Build TestContext and evaluate assertions.
    event_log_path = results_dir / "events" / f"{run_id}.jsonl"
    ctx = _build_context(
        test=test,
        sub_run=sub_run,
        trial_index=trial_index,
        algorithm=algorithm,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        timeout_seconds=drain_timeout_seconds,
        completed_within_timeout=not timed_out,
        event_log_path=event_log_path,
    )
    assertion_results: list[AssertionResult] = []
    for name in test.assertions:
        try:
            fn = get_assertion(name)
            assertion_results.append(fn(ctx))
        except Exception as exc:
            assertion_results.append(
                AssertionResult(
                    name=name,
                    passed=False,
                    detail=f"assertion raised: {type(exc).__name__}: {exc}",
                )
            )

    overall_passed = all(r.passed for r in assertion_results)
    if not overall_passed:
        failed_names = [r.name for r in assertion_results if not r.passed]
        logger.warning(
            "Trial %s FAILED. Failing assertions: %s",
            run_id,
            failed_names,
        )

    # The verification event log is shared with performance event logs at
    # this layer; the path is captured for failure debugging.
    rel_path = event_log_path.relative_to(results_dir).as_posix() if event_log_path.exists() else ""

    return VerificationVerdict(
        test_id=test.test_id,
        test_type=test.test_type,                       # type: ignore[arg-type]
        algorithm=algorithm,
        sub_run=sub_run,
        trial_index=trial_index,
        overall_passed=overall_passed,
        assertion_results=assertion_results,
        started_at=started_at,
        completed_at=completed_at,
        event_log_path=rel_path,
    )


# ---------------------------------------------------------------------------
# Context construction from JSONL
# ---------------------------------------------------------------------------


def _build_context(
    test: VerificationTest,
    sub_run: str | None,
    trial_index: int | None,
    algorithm: str,
    run_id: str,
    started_at: float,
    completed_at: float,
    duration_seconds: float,
    timeout_seconds: float,
    completed_within_timeout: bool,
    event_log_path: Path,
) -> TestContext:
    """Replay the JSONL log into per-task records the assertions expect."""
    node_specs = {n.node_id: n for n in test.nodes}
    task_records: dict[str, dict] = {}

    if event_log_path.exists():
        with event_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "task_submitted":
                    tid = ev["task_id"]
                    rec = task_records.setdefault(tid, {})
                    rec.update(
                        task_id=tid,
                        task_class=ev.get("task_class"),
                        memory_kb=ev.get("memory_kb"),
                        workload_units=ev.get("workload_units"),
                        submitted_at_rel=float(ev.get("t", 0.0)),
                        status=rec.get("status", "pending"),
                    )
                elif etype == "task_dispatched":
                    tid = ev["task_id"]
                    rec = task_records.setdefault(tid, {"task_id": tid})
                    rec["dispatched_at_rel"] = float(ev.get("t", 0.0))
                    rec["node_id"] = ev.get("node_id")
                    rec["status"] = "dispatched"
                elif etype == "task_completed":
                    tid = ev["task_id"]
                    rec = task_records.setdefault(tid, {"task_id": tid})
                    rec["completed_at_rel"] = float(ev.get("t", 0.0))
                    rec["status"] = "completed"
                    if not rec.get("node_id"):
                        rec["node_id"] = ev.get("node_id")
                elif etype == "task_rejected":
                    tid = ev["task_id"]
                    rec = task_records.setdefault(tid, {"task_id": tid})
                    rec["status"] = "rejected"
                    rec["rejection_reason"] = ev.get("reason")
                    if ev.get("node_id"):
                        rec["node_id"] = ev.get("node_id")
                elif etype == "task_evicted":
                    tid = ev["task_id"]
                    rec = task_records.setdefault(tid, {"task_id": tid})
                    rec["status"] = "rejected"
                    rec["rejection_reason"] = ev.get("reason", "placement_timeout")

    return TestContext(
        test_id=test.test_id,
        algorithm=algorithm,
        sub_run=sub_run,
        trial_index=trial_index,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        timeout_seconds=timeout_seconds,
        trial_completed_within_timeout=completed_within_timeout,
        node_specs=node_specs,
        task_records=list(task_records.values()),
        event_log_path=event_log_path if event_log_path.exists() else None,
    )


# ---------------------------------------------------------------------------
# HTTP plumbing — same conventions as the scenario runner
# ---------------------------------------------------------------------------


def _register_nodes(client: httpx.Client, nodes: list[NodeSpec]) -> None:
    for spec in nodes:
        _post(client, "/nodes/register", json=spec.model_dump(mode="json"))


def _replay_timeline(
    client: httpx.Client, timeline: list[TimelineEvent], t0: float
) -> None:
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
    nodes_resp = _get(client, "/nodes")
    node_url = None
    for n in nodes_resp.json().get("nodes", []):
        if n["spec"]["node_id"] == node_id:
            node_url = n["spec"]["url"]
            break
    if not node_url:
        logger.warning("Cannot inject memory: node_id=%s not registered", node_id)
        return
    with httpx.Client(timeout=2.0) as edge_client:
        try:
            edge_client.post(
                f"{node_url}/admin/inject_memory",
                json={"kb": kb, "hold_seconds": hold_seconds},
            )
        except Exception as exc:
            logger.warning("Memory inject failed for %s: %s", node_id, exc)


def _wait_for_drain(client: httpx.Client, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            state = _get(client, "/system/state").json()
            queue_size = state.get("queue_size", 0)
            nodes = _get(client, "/nodes").json().get("nodes", [])
            edge_active = sum(
                int(n.get("status", {}).get("active_task_count", 0)) for n in nodes
            )
            projected_active = sum(
                int(n.get("projected_used_cores", 0)) for n in nodes
            )
            if queue_size == 0 and edge_active == 0 and projected_active == 0:
                return True
        except Exception:
            pass
        time.sleep(DRAIN_POLL_INTERVAL)
    return False


def _post(client: httpx.Client, path: str, json: Any = None) -> httpx.Response:
    resp = client.post(path, json=json)
    if resp.status_code >= 400:
        raise RuntimeError(f"POST {path} returned {resp.status_code}: {resp.text}")
    return resp


def _post_ack(client: httpx.Client, path: str) -> httpx.Response:
    resp = client.post(path)
    if resp.status_code >= 400:
        raise RuntimeError(f"POST {path} returned {resp.status_code}: {resp.text}")
    body = resp.json()
    if isinstance(body, dict) and not body.get("ok", True):
        logger.warning("%s soft-failed: %s", path, body.get("detail"))
    return resp


def _get(client: httpx.Client, path: str) -> httpx.Response:
    resp = client.get(path)
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {path} returned {resp.status_code}: {resp.text}")
    return resp


# ---------------------------------------------------------------------------
# Report writing — minimal here; report.py builds the full markdown view.
# ---------------------------------------------------------------------------


def _write_report(report: VerificationReport, results_dir: Path) -> None:
    """Persist the report to results/verification/report.json. The
    Markdown rendering lives in verification/report.py for separation of
    concerns."""
    out_dir = results_dir / "verification"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report.json"
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Wrote verification report (json) to %s", out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a verification test (single file) or all tests"
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        help="Path to a YAML test file. If omitted, runs every YAML under "
        "the directory passed via --tests-dir.",
    )
    parser.add_argument("--scheduler", default="http://localhost:8000")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument("--drain-timeout", type=float, default=DRAIN_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        if args.target:
            verdicts = run_test(
                test_path=args.target,
                scheduler_url=args.scheduler,
                results_dir=args.results_dir,
                drain_timeout_seconds=args.drain_timeout,
            )
            passed = sum(1 for v in verdicts if v.overall_passed)
            print(f"{passed}/{len(verdicts)} verdicts passed for {args.target}")
            return 0 if passed == len(verdicts) else 2
        report = run_all_tests(
            tests_dir=args.tests_dir,
            scheduler_url=args.scheduler,
            results_dir=args.results_dir,
            drain_timeout_seconds=args.drain_timeout,
        )
        print(f"{report.passed}/{report.total_verdicts} verdicts passed")
        return 0 if report.failed == 0 else 2
    except Exception:
        logger.exception("Verification run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())