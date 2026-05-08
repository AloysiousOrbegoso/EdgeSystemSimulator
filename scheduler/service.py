"""
Scheduler FastAPI service — orchestrator with adaptive support.

Adds in this iteration (over the previous service.py):
  - Owns a LearnedState instance and registers AdaptiveMinMinAlgorithm
    against it during startup.
  - Feeds μ̂_n updates from each observed completion (workload_units,
    measured duration) into LearnedState.
  - Clears LearnedState as part of /trial/reset so trials don't leak
    learned-state from each other.
  - Exposes /system/learned_state for inspection.

Everything else from the previous version is unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

from shared.constants import (
    HETEROGENEOUS_4NODE,
    RECONCILE_INTERVAL_SECONDS,
    SAMPLE_INTERVAL_SECONDS,
    TASK_PLACEMENT_TIMEOUT_SECONDS,
    TICK_INTERVAL_SECONDS,
)
from shared.models import (
    Assignment,
    NodeRuntimeStatus,
    NodeSpec,
    Task,
    TrialResult,
)

from scheduler.algorithms import (
    get_algorithm,
    list_algorithm_names,
    register_adaptive,
)
from scheduler.algorithms.base import SchedulingAlgorithm
from scheduler.dispatcher import Dispatcher
from scheduler.learned_state import LearnedState
from scheduler.node_state import NodeState
from scheduler.queue import TaskQueue
from scheduler.trial_recorder import TrialRecorder

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

SCHED_QUEUE_DEPTH = Gauge(
    "scheduler_queue_depth", "Number of pending tasks in the scheduler queue"
)
SCHED_ACTIVE_ALGORITHM = Gauge(
    "scheduler_active_algorithm",
    "1 for the currently active algorithm; 0 for others",
    ["algorithm"],
)
SCHED_TRIAL_ACTIVE = Gauge(
    "scheduler_trial_active", "1 when a trial is in progress, else 0"
)
SCHED_LEARNED_MU = Gauge(
    "scheduler_learned_mu",
    "Current learned service rate (workload-units/sec) per node",
    ["node_id"],
)
SCHED_TASKS_SUBMITTED_TOTAL = Counter(
    "scheduler_tasks_submitted_total", "Tasks accepted by /submit_task"
)
SCHED_TASKS_DISPATCHED_TOTAL = Counter(
    "scheduler_tasks_dispatched_total",
    "Tasks successfully admitted by an edge node",
    ["node_id"],
)
SCHED_TASKS_REJECTED_TOTAL = Counter(
    "scheduler_tasks_rejected_total",
    "Tasks rejected by an edge node and re-enqueued",
    ["node_id", "reason"],
)
SCHED_TASKS_TIMED_OUT_TOTAL = Counter(
    "scheduler_tasks_timed_out_total",
    "Tasks evicted from the queue after exceeding placement timeout",
)
SCHED_TICK_DURATION_SECONDS = Histogram(
    "scheduler_tick_duration_seconds",
    "Wall-clock time spent inside one scheduling tick",
)
SCHED_DECISIONS_PER_TICK = Histogram(
    "scheduler_decisions_per_tick",
    "Number of assignments produced by one scheduling tick",
)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_node_state: NodeState | None = None
_queue: TaskQueue | None = None
_dispatcher: Dispatcher | None = None
_recorder: TrialRecorder | None = None
_learned: LearnedState | None = None
_active_algorithm_name: str = "min_min"
_algo_lock = threading.Lock()

_tick_stop = threading.Event()
_tick_thread: threading.Thread | None = None
_reconcile_stop = threading.Event()
_reconcile_thread: threading.Thread | None = None
_sample_stop = threading.Event()
_sample_thread: threading.Thread | None = None

_inflight: dict[str, set[str]] = {}
_inflight_lock = threading.Lock()
_dispatched_tasks: dict[str, tuple[Task, float]] = {}


def _require_initialized() -> tuple[
    NodeState, TaskQueue, Dispatcher, TrialRecorder, LearnedState
]:
    if (
        _node_state is None
        or _queue is None
        or _dispatcher is None
        or _recorder is None
        or _learned is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scheduler not initialized yet",
        )
    return _node_state, _queue, _dispatcher, _recorder, _learned


def _get_active_algorithm() -> SchedulingAlgorithm:
    with _algo_lock:
        return get_algorithm(_active_algorithm_name)


# ---------------------------------------------------------------------------
# Dispatch outcome callback — links dispatcher events to the recorder
# ---------------------------------------------------------------------------


def _on_dispatch_outcome(
    task: Task, assignment: Assignment, outcome: str, detail: str | None
) -> None:
    if _recorder is None or not _recorder.is_active():
        return
    if outcome == "dispatched":
        SCHED_TASKS_DISPATCHED_TOTAL.labels(node_id=assignment.node_id).inc()
        _recorder.record_task_dispatched(task, assignment.node_id)
        with _inflight_lock:
            _inflight.setdefault(assignment.node_id, set()).add(task.task_id)
            _dispatched_tasks[task.task_id] = (task, time.time())
    elif outcome == "rejected":
        SCHED_TASKS_REJECTED_TOTAL.labels(
            node_id=assignment.node_id, reason=detail or "unknown"
        ).inc()
        _recorder.record_task_rejected(task, assignment.node_id, detail or "unknown")
    elif outcome == "node_error":
        SCHED_TASKS_REJECTED_TOTAL.labels(
            node_id=assignment.node_id, reason="node_error"
        ).inc()
        _recorder.record_task_rejected(
            task, assignment.node_id, f"node_error: {detail}"
        )


# ---------------------------------------------------------------------------
# Background: scheduling tick
# ---------------------------------------------------------------------------


def _scheduling_tick_loop() -> None:
    logger.info("Scheduling tick loop started (interval=%ss)", TICK_INTERVAL_SECONDS)
    while not _tick_stop.is_set():
        _tick_stop.wait(TICK_INTERVAL_SECONDS)
        if _tick_stop.is_set():
            break
        try:
            _run_one_tick()
        except Exception:
            logger.exception("Scheduling tick raised; continuing")
    logger.info("Scheduling tick loop stopped")


def _run_one_tick() -> None:
    if _node_state is None or _queue is None or _dispatcher is None:
        return

    expired = _queue.evict_expired(TASK_PLACEMENT_TIMEOUT_SECONDS)
    if expired:
        SCHED_TASKS_TIMED_OUT_TOTAL.inc(len(expired))
        for t in expired:
            logger.info(
                "Task %s evicted from queue after %.1fs without placement",
                t.task_id,
                TASK_PLACEMENT_TIMEOUT_SECONDS,
            )
            if _recorder is not None:
                _recorder.record_task_evicted(t, "placement_timeout")

    pending = _queue.snapshot()
    SCHED_QUEUE_DEPTH.set(len(pending))
    if not pending:
        return

    nodes = _node_state.snapshot_views()
    algo = _get_active_algorithm()

    t0 = time.perf_counter()
    try:
        assignments: list[Assignment] = algo.schedule(
            pending=pending, nodes=nodes, now=time.time()
        )
    except Exception:
        logger.exception("Algorithm %s raised in schedule()", algo.name)
        return
    duration_ms = (time.perf_counter() - t0) * 1000.0

    SCHED_TICK_DURATION_SECONDS.observe(duration_ms / 1000.0)
    SCHED_DECISIONS_PER_TICK.observe(len(assignments))
    if _recorder is not None:
        _recorder.record_scheduling_decision(
            decision_ms=duration_ms,
            pending_size=len(pending),
            assignments_made=len(assignments),
        )

    if not assignments:
        return

    task_by_id = {t.task_id: t for t in pending}
    seen: set[str] = set()
    for a in assignments:
        if a.task_id in seen:
            logger.warning(
                "Algorithm %s emitted duplicate task_id=%s; ignoring",
                algo.name,
                a.task_id,
            )
            continue
        seen.add(a.task_id)
        task = task_by_id.get(a.task_id)
        if task is None:
            logger.warning(
                "Algorithm %s assigned unknown task_id=%s; ignoring",
                algo.name,
                a.task_id,
            )
            continue
        _queue.remove(task.task_id)
        _dispatcher.dispatch(task, a)


# ---------------------------------------------------------------------------
# Background: reconcile poller
# ---------------------------------------------------------------------------


def _reconcile_loop() -> None:
    logger.info(
        "Reconcile poller started (interval=%ss)", RECONCILE_INTERVAL_SECONDS
    )
    client = httpx.Client(timeout=2.0)
    try:
        while not _reconcile_stop.is_set():
            _reconcile_stop.wait(RECONCILE_INTERVAL_SECONDS)
            if _reconcile_stop.is_set():
                break
            if _node_state is None:
                continue
            for node_id in _node_state.list_node_ids():
                spec = _node_state.get_spec(node_id)
                if spec is None:
                    continue
                try:
                    resp = client.get(f"{spec.url}/status")
                    if resp.status_code != 200:
                        _node_state.mark_unreachable(
                            node_id, f"http_{resp.status_code}"
                        )
                        continue
                    body = resp.json()
                    runtime = NodeRuntimeStatus(**body["runtime"])
                    _node_state.reconcile(node_id, runtime)
                    _observe_completions(node_id, runtime)
                except Exception as exc:
                    _node_state.mark_unreachable(
                        node_id, f"{type(exc).__name__}: {exc}"
                    )
    finally:
        client.close()
    logger.info("Reconcile poller stopped")


def _observe_completions(node_id: str, runtime: NodeRuntimeStatus) -> None:
    """Process completions reported by the edge node directly.

    Edges include `recent_completions` in /status responses with true
    execution durations. The scheduler uses those durations for EWMA
    updates and as the basis for releasing reservations — replacing the
    previous count-subtraction heuristic, which was biased by reconcile
    poll latency and produced systematically low μ̂ estimates that
    starved certain nodes.

    Tasks reported in `recent_completions` that the scheduler doesn't
    have in its in-flight set (e.g., late-arriving callbacks across a
    /trial/reset boundary) are silently ignored.
    """
    if _node_state is None or _recorder is None or _learned is None:
        return
    if not runtime.recent_completions:
        return

    with _inflight_lock:
        for completion in runtime.recent_completions:
            tid = completion.task_id
            in_flight_set = _inflight.get(node_id, set())
            if tid not in in_flight_set:
                # Stale callback — task isn't tracked by the scheduler
                # right now (likely cleared by /trial/reset).
                continue
            entry = _dispatched_tasks.pop(tid, None)
            in_flight_set.discard(tid)
            if entry is None:
                continue
            task, _dispatched_at = entry
            duration = float(completion.duration_seconds)
            _node_state.release(node_id, tid, task.workload_units)
            _recorder.record_task_completed(tid, node_id, duration)
            _learned.update_service_rate(
                node_id=node_id,
                workload_units=task.workload_units,
                observed_duration_seconds=duration,
            )
            SCHED_LEARNED_MU.labels(node_id=node_id).set(
                _learned.get_mu(node_id)
            )


# ---------------------------------------------------------------------------
# Background: 1 Hz sampling
# ---------------------------------------------------------------------------


def _sampling_loop() -> None:
    logger.info("Sampling loop started (interval=%ss)", SAMPLE_INTERVAL_SECONDS)
    while not _sample_stop.is_set():
        _sample_stop.wait(SAMPLE_INTERVAL_SECONDS)
        if _sample_stop.is_set():
            break
        if _recorder is None or not _recorder.is_active():
            continue
        if _node_state is None:
            continue
        try:
            _take_one_sample()
        except Exception:
            logger.exception("Sampling iteration raised; continuing")
    logger.info("Sampling loop stopped")


def _take_one_sample() -> None:
    assert _node_state is not None and _recorder is not None
    views = _node_state.snapshot_views()
    running: dict[str, int] = {}
    for v in views:
        node_id = v.spec.node_id
        used_kb = (
            v.status.used_ram_kb if v.status.reachable else v.projected_used_ram_kb
        )
        _recorder.record_memory_sample(
            node_id=node_id, used_kb=used_kb, total_kb=v.spec.total_ram_kb
        )
        cores_used = (
            v.status.cpu_cores_in_use if v.status.reachable else v.projected_used_cores
        )
        _recorder.record_cpu_sample(
            node_id=node_id, cores_in_use=cores_used, cores_total=v.spec.cpu_cores
        )
        running[node_id] = (
            v.status.active_task_count
            if v.status.reachable
            else _node_state.in_flight_count(node_id)
        )
    _recorder.record_distribution_sample(running)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _node_state, _queue, _dispatcher, _recorder, _learned
    global _tick_thread, _reconcile_thread, _sample_thread

    _node_state = NodeState()
    _queue = TaskQueue()
    _recorder = TrialRecorder(
        results_dir=Path(os.environ.get("RESULTS_DIR", "results"))
    )
    _learned = LearnedState()
    register_adaptive(_learned)
    _dispatcher = Dispatcher(
        node_state=_node_state,
        queue=_queue,
        dispatch_callback=_on_dispatch_outcome,
    )

    if os.environ.get("SCHEDULER_AUTOREGISTER", "1") != "0":
        for spec in HETEROGENEOUS_4NODE:
            _node_state.register_node(spec)

    for name in list_algorithm_names():
        SCHED_ACTIVE_ALGORITHM.labels(algorithm=name).set(
            1.0 if name == _active_algorithm_name else 0.0
        )
    SCHED_TRIAL_ACTIVE.set(0)

    _tick_stop.clear()
    _reconcile_stop.clear()
    _sample_stop.clear()
    _tick_thread = threading.Thread(
        target=_scheduling_tick_loop, name="scheduler-tick", daemon=True
    )
    _reconcile_thread = threading.Thread(
        target=_reconcile_loop, name="scheduler-reconcile", daemon=True
    )
    _sample_thread = threading.Thread(
        target=_sampling_loop, name="scheduler-sample", daemon=True
    )
    _tick_thread.start()
    _reconcile_thread.start()
    _sample_thread.start()

    logger.info(
        "Scheduler ready: algorithms=%s active=%s nodes=%s",
        list_algorithm_names(),
        _active_algorithm_name,
        _node_state.list_node_ids(),
    )

    yield

    _tick_stop.set()
    _reconcile_stop.set()
    _sample_stop.set()
    if _tick_thread is not None:
        _tick_thread.join(timeout=2.0)
    if _reconcile_thread is not None:
        _reconcile_thread.join(timeout=2.0)
    if _sample_thread is not None:
        _sample_thread.join(timeout=2.0)
    if _dispatcher is not None:
        _dispatcher.close()


app = FastAPI(title="Scheduler", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SubmitTaskResponse(BaseModel):
    task_id: str
    enqueued: bool
    queue_size: int


class SubmitBatchResponse(BaseModel):
    enqueued_count: int
    skipped_count: int
    queue_size: int


class QueueResponse(BaseModel):
    size: int
    tasks: list[Task]


class NodesResponse(BaseModel):
    nodes: list[dict[str, Any]]


class AlgorithmsResponse(BaseModel):
    active: str
    available: list[str]


class SetAlgorithmRequest(BaseModel):
    name: str


class StartTrialRequest(BaseModel):
    run_id: str
    scenario_name: str
    algorithm: str
    trial_index: int
    comparison_id: str
    seed: int


class TrialActiveResponse(BaseModel):
    active: bool
    run_id: str | None = None


class Ack(BaseModel):
    ok: bool = True
    detail: str | None = None


# ---------------------------------------------------------------------------
# Task submission
# ---------------------------------------------------------------------------


@app.post("/submit_task", response_model=SubmitTaskResponse)
def submit_task(task: Task) -> SubmitTaskResponse:
    _, queue, _, recorder, _ = _require_initialized()
    task = task.model_copy(update={"submitted_at": time.time()})
    enqueued = queue.enqueue(task)
    if enqueued:
        SCHED_TASKS_SUBMITTED_TOTAL.inc()
        recorder.record_task_submitted(task)
    return SubmitTaskResponse(
        task_id=task.task_id, enqueued=enqueued, queue_size=queue.size()
    )


@app.post("/submit_batch", response_model=SubmitBatchResponse)
def submit_batch(tasks: list[Task]) -> SubmitBatchResponse:
    _, queue, _, recorder, _ = _require_initialized()
    now = time.time()
    # Always overwrite submitted_at — see submit_task for rationale.
    stamped = [t.model_copy(update={"submitted_at": now}) for t in tasks]
    added = queue.enqueue_many(stamped)
    SCHED_TASKS_SUBMITTED_TOTAL.inc(added)
    for t in stamped:
        if queue.contains(t.task_id):
            recorder.record_task_submitted(t)
    return SubmitBatchResponse(
        enqueued_count=added,
        skipped_count=len(tasks) - added,
        queue_size=queue.size(),
    )


@app.get("/queue", response_model=QueueResponse)
def get_queue() -> QueueResponse:
    _, queue, _, _, _ = _require_initialized()
    snap = queue.snapshot()
    return QueueResponse(size=len(snap), tasks=snap)


# ---------------------------------------------------------------------------
# Node management
# ---------------------------------------------------------------------------


@app.get("/nodes", response_model=NodesResponse)
def get_nodes() -> NodesResponse:
    state, _, _, _, _ = _require_initialized()
    views = state.snapshot_views()
    return NodesResponse(
        nodes=[
            {
                "spec": v.spec.model_dump(mode="json"),
                "status": v.status.model_dump(mode="json"),
                "projected_used_ram_kb": v.projected_used_ram_kb,
                "projected_used_cores": v.projected_used_cores,
                "projected_completion_time": v.projected_completion_time,
            }
            for v in views
        ]
    )


@app.post("/nodes/register", response_model=Ack)
def register_node(spec: NodeSpec) -> Ack:
    state, _, _, _, _ = _require_initialized()
    state.register_node(spec)
    return Ack(ok=True, detail=f"registered {spec.node_id}")


# ---------------------------------------------------------------------------
# Algorithm management
# ---------------------------------------------------------------------------


@app.get("/config/algorithms", response_model=AlgorithmsResponse)
def get_algorithms() -> AlgorithmsResponse:
    return AlgorithmsResponse(
        active=_active_algorithm_name,
        available=list_algorithm_names(),
    )


@app.post("/config/algorithm", response_model=Ack)
def set_algorithm(req: SetAlgorithmRequest) -> Ack:
    global _active_algorithm_name
    try:
        get_algorithm(req.name)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with _algo_lock:
        _active_algorithm_name = req.name
    for name in list_algorithm_names():
        SCHED_ACTIVE_ALGORITHM.labels(algorithm=name).set(
            1.0 if name == req.name else 0.0
        )
    logger.info("Active algorithm switched to %s", req.name)
    return Ack(ok=True, detail=f"active algorithm: {req.name}")


# ---------------------------------------------------------------------------
# Trial lifecycle
# ---------------------------------------------------------------------------


@app.post("/trial/start", response_model=Ack)
def trial_start(req: StartTrialRequest) -> Ack:
    _, _, _, recorder, _ = _require_initialized()
    try:
        recorder.start_trial(
            run_id=req.run_id,
            scenario_name=req.scenario_name,
            algorithm=req.algorithm,
            trial_index=req.trial_index,
            comparison_id=req.comparison_id,
            seed=req.seed,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    SCHED_TRIAL_ACTIVE.set(1)
    return Ack(ok=True, detail=f"trial {req.run_id} started")


@app.post("/trial/end", response_model=TrialResult)
def trial_end() -> TrialResult:
    _, _, _, recorder, _ = _require_initialized()
    try:
        result = recorder.end_trial()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    SCHED_TRIAL_ACTIVE.set(0)
    return result


@app.get("/trial/active", response_model=TrialActiveResponse)
def trial_active() -> TrialActiveResponse:
    _, _, _, recorder, _ = _require_initialized()
    return TrialActiveResponse(
        active=recorder.is_active(), run_id=recorder.active_run_id()
    )


@app.post("/trial/reset", response_model=Ack)
def trial_reset() -> Ack:
    """Hard reset between trials. Adaptive learned state is cleared so
    each trial starts adaptive Min-Min from the same prior — this is
    what lets paired observations across algorithms be compared
    cleanly via Friedman."""
    state, queue, _, recorder, learned = _require_initialized()
    if recorder.is_active():
        try:
            with recorder._lock:                              # type: ignore[attr-defined]
                if recorder._event_file is not None:          # type: ignore[attr-defined]
                    recorder._event_file.close()              # type: ignore[attr-defined]
                    recorder._event_file = None               # type: ignore[attr-defined]
                recorder._active = None                       # type: ignore[attr-defined]
        except Exception:
            logger.exception("Error tearing down active trial during reset")
    SCHED_TRIAL_ACTIVE.set(0)

    queue.clear()
    state.reset_all()
    learned.reset()
    with _inflight_lock:
        _inflight.clear()
        _dispatched_tasks.clear()

    # Clear the Prometheus-side learned-mu gauges so a stale value does
    # not linger until the next completion fires an update.
    for node_id in state.list_node_ids():
        try:
            SCHED_LEARNED_MU.remove(node_id)
        except KeyError:
            pass

    client = httpx.Client(timeout=2.0)
    failed: list[str] = []
    try:
        for node_id in state.list_node_ids():
            spec = state.get_spec(node_id)
            if spec is None:
                continue
            try:
                resp = client.post(f"{spec.url}/admin/reset")
                if resp.status_code != 200:
                    failed.append(f"{node_id}:http_{resp.status_code}")
            except Exception as e:
                failed.append(f"{node_id}:{type(e).__name__}")
    finally:
        client.close()

    if failed:
        return Ack(
            ok=False,
            detail=f"reset partial; edge reset failures: {', '.join(failed)}",
        )
    return Ack(ok=True, detail="reset complete")


# ---------------------------------------------------------------------------
# System state, metrics, health
# ---------------------------------------------------------------------------


@app.get("/system/state")
def system_state() -> dict[str, Any]:
    state, queue, _, recorder, _ = _require_initialized()
    return {
        "active_algorithm": _active_algorithm_name,
        "queue_size": queue.size(),
        "node_count": len(state.list_node_ids()),
        "node_ids": state.list_node_ids(),
        "trial_active": recorder.is_active(),
        "trial_run_id": recorder.active_run_id(),
    }


@app.get("/system/learned_state")
def learned_state_view() -> dict[str, Any]:
    """Diagnostics for adaptive Min-Min — current μ̂_n and π_n per node,
    plus the number of completions observed for each."""
    _, _, _, _, learned = _require_initialized()
    return learned.diagnostics()


@app.get("/metrics")
def metrics() -> Response:
    if _queue is not None:
        SCHED_QUEUE_DEPTH.set(_queue.size())
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok"} 