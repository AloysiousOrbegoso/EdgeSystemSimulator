"""
FastAPI service for one edge node.

This is the thin HTTP layer over the three workhorse classes:
    MemoryManager  — RAM accounting and admission
    CpuManager     — CPU core accounting and admission
    ExecutionEngine — task lifecycle (admit -> sleep -> release)

Configuration comes from environment variables, set per-container in
docker-compose.yml. The variables and their defaults are documented in the
config block below.

Endpoints:
    POST /execute             admit and start running one task
    GET  /status              current spec + runtime state
    POST /admin/inject_memory drive the node toward saturation (test scenario)
    POST /admin/reset         clear all tasks, holds, and history
    GET  /metrics             Prometheus exposition
    GET  /health              liveness probe

Run with:
    uvicorn edge.edgenode:app --host 0.0.0.0 --port 5000

The same image is built once and run four times in docker-compose, with
different env vars per container to produce edge1..edge4.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

from shared.constants import CPU_SATURATION_PCT, MEMORY_SATURATION_PCT
from shared.models import AdmitDecision, NodeRuntimeStatus, NodeSpec, Task, TaskClass

from edge.cpu_manager import CpuManager
from edge.execution import CompletionRecord, ExecutionEngine
from edge.memory_manager import MemoryManager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------


def _parse_accepted_classes(raw: str) -> list[TaskClass]:
    """Parse the comma-separated ACCEPTED_CLASSES env var.

    Example values:
        "lightweight"
        "lightweight,moderate,heavy"
        "lightweight,moderate,heavy,very_heavy"

    Whitespace around items is ignored. Unknown class names raise ValueError
    so a typo in docker-compose fails fast on container start instead of
    silently producing a node that accepts nothing.
    """
    items = [s.strip() for s in raw.split(",") if s.strip()]
    valid = {c.value for c in TaskClass}
    out: list[TaskClass] = []
    for item in items:
        if item not in valid:
            raise ValueError(
                f"ACCEPTED_CLASSES contains unknown class '{item}'. "
                f"Valid values: {sorted(valid)}"
            )
        out.append(TaskClass(item))
    if not out:
        raise ValueError("ACCEPTED_CLASSES must list at least one class")
    return out


def _load_node_spec_from_env() -> NodeSpec:
    """Build a NodeSpec from environment variables.

    Required vars (no sane default for a thesis testbed):
        NODE_ID           e.g. "edge1"
        TOTAL_RAM_KB      e.g. 8
        USABLE_RAM_KB     e.g. 5
        FLASH_KB          e.g. 256
        CPU_CORES         e.g. 1
        ACCEPTED_CLASSES  e.g. "lightweight"

    Optional:
        NODE_URL          defaults to http://<NODE_ID>:5000 (Docker DNS)
        SATURATION_THRESHOLD_KB
                          defaults to floor(TOTAL_RAM_KB * 0.80)
    """

    def required(name: str) -> str:
        val = os.environ.get(name)
        if val is None or val == "":
            raise RuntimeError(
                f"Edge node startup failed: required env var {name} is not set"
            )
        return val

    node_id = required("NODE_ID")
    total_ram_kb = int(required("TOTAL_RAM_KB"))
    usable_ram_kb = int(required("USABLE_RAM_KB"))
    flash_kb = int(required("FLASH_KB"))
    cpu_cores = int(required("CPU_CORES"))
    accepted = _parse_accepted_classes(required("ACCEPTED_CLASSES"))

    url = os.environ.get("NODE_URL", f"http://{node_id}:5000")
    saturation_default = int(total_ram_kb * MEMORY_SATURATION_PCT)
    saturation_threshold_kb = int(
        os.environ.get("SATURATION_THRESHOLD_KB", saturation_default)
    )

    return NodeSpec(
        node_id=node_id,
        url=url,
        total_ram_kb=total_ram_kb,
        usable_ram_kb=usable_ram_kb,
        flash_kb=flash_kb,
        saturation_threshold_kb=saturation_threshold_kb,
        cpu_cores=cpu_cores,
        cpu_saturation_threshold_pct=CPU_SATURATION_PCT,
        accepted_classes=accepted,
        is_active=True,
    )


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
# All metrics are labelled by node_id so a single Prometheus instance can
# scrape all four edge nodes and distinguish them. The exporter rebuilds the
# gauge values from the managers on every /metrics scrape; counters and the
# runtime histogram are incremented inline as events happen.

NODE_ID_LABEL = "node_id"
CLASS_LABEL = "task_class"
REASON_LABEL = "reason"

EDGE_TOTAL_RAM_KB = Gauge(
    "edge_total_ram_kb",
    "Configured total RAM on this edge node in KB",
    [NODE_ID_LABEL],
)
EDGE_USABLE_RAM_KB = Gauge(
    "edge_usable_ram_kb",
    "Allocatable portion of total RAM after the 70%% rule",
    [NODE_ID_LABEL],
)
EDGE_USED_RAM_KB = Gauge(
    "edge_used_ram_kb",
    "RAM currently held by tasks plus active injections",
    [NODE_ID_LABEL],
)
EDGE_AVAILABLE_RAM_KB = Gauge(
    "edge_available_ram_kb",
    "Usable RAM minus current task holds",
    [NODE_ID_LABEL],
)
EDGE_MEMORY_UTILIZATION_PCT = Gauge(
    "edge_memory_utilization_pct",
    "used_ram_kb / total_ram_kb * 100",
    [NODE_ID_LABEL],
)
EDGE_IS_MEMORY_SATURATED = Gauge(
    "edge_is_memory_saturated",
    "1 when used_ram_kb >= saturation_threshold_kb, else 0",
    [NODE_ID_LABEL],
)

EDGE_CPU_CORES = Gauge(
    "edge_cpu_cores",
    "Configured CPU cores on this edge node",
    [NODE_ID_LABEL],
)
EDGE_CPU_CORES_IN_USE = Gauge(
    "edge_cpu_cores_in_use",
    "Cores currently held by running tasks",
    [NODE_ID_LABEL],
)
EDGE_CPU_UTILIZATION_PCT = Gauge(
    "edge_cpu_utilization_pct",
    "cores_in_use / cpu_cores * 100",
    [NODE_ID_LABEL],
)
EDGE_IS_CPU_SATURATED = Gauge(
    "edge_is_cpu_saturated",
    "1 when CPU utilization >= saturation threshold, else 0",
    [NODE_ID_LABEL],
)

EDGE_ACTIVE_TASK_COUNT = Gauge(
    "edge_active_task_count",
    "Number of tasks currently running on the node",
    [NODE_ID_LABEL],
)

EDGE_TASKS_ADMITTED_TOTAL = Counter(
    "edge_tasks_admitted_total",
    "Tasks accepted by admission control, by class",
    [NODE_ID_LABEL, CLASS_LABEL],
)
EDGE_TASKS_REJECTED_TOTAL = Counter(
    "edge_tasks_rejected_total",
    "Tasks refused by admission control, by reason",
    [NODE_ID_LABEL, REASON_LABEL],
)
EDGE_TASKS_COMPLETED_TOTAL = Counter(
    "edge_tasks_completed_total",
    "Tasks that finished executing, by class",
    [NODE_ID_LABEL, CLASS_LABEL],
)
EDGE_TASK_RUNTIME_SECONDS = Histogram(
    "edge_task_runtime_seconds",
    "Per-task execution duration on this node, by class",
    [NODE_ID_LABEL, CLASS_LABEL],
)


# ---------------------------------------------------------------------------
# Module-level state — wired up in lifespan, used by handlers
# ---------------------------------------------------------------------------
# Globals are awkward, but FastAPI's app.state is opaque to type checkers
# and the alternative (passing through Depends()) clutters every handler
# signature. We keep the globals private and expose them only through the
# typed helpers below.

_node_spec: NodeSpec | None = None
_memory: MemoryManager | None = None
_cpu: CpuManager | None = None
_engine: ExecutionEngine | None = None
_active_lock = threading.Lock()


def _is_active() -> bool:
    """Read the node's is_active flag. Used by the execution engine."""
    with _active_lock:
        return _node_spec.is_active if _node_spec is not None else False


def _on_completion(record: CompletionRecord) -> None:
    """Inline Prometheus updates for completion-time metrics.

    Called by ExecutionEngine on a background thread, so this function must
    be cheap and must not block. Counter and histogram operations from
    prometheus_client are thread-safe.
    """
    spec = _node_spec
    if spec is None:
        return
    EDGE_TASKS_COMPLETED_TOTAL.labels(
        node_id=spec.node_id, task_class=record.task_class.value
    ).inc()
    EDGE_TASK_RUNTIME_SECONDS.labels(
        node_id=spec.node_id, task_class=record.task_class.value
    ).observe(record.duration_seconds)


# ---------------------------------------------------------------------------
# Lifespan: build managers + engine on startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the node from env vars and seed the static gauges.

    Lifespan replaces the deprecated @app.on_event("startup") pattern. We
    do not need an explicit shutdown handler — daemon threads exit with
    the process.
    """
    global _node_spec, _memory, _cpu, _engine

    spec = _load_node_spec_from_env()
    memory = MemoryManager(
        total_ram_kb=spec.total_ram_kb,
        usable_ram_kb=spec.usable_ram_kb,
        saturation_threshold_kb=spec.saturation_threshold_kb,
    )
    cpu = CpuManager(
        cpu_cores=spec.cpu_cores,
        saturation_threshold_pct=spec.cpu_saturation_threshold_pct,
    )
    engine = ExecutionEngine(
        node_id=spec.node_id,
        memory=memory,
        cpu=cpu,
        accepted_classes=spec.accepted_classes,
        is_active_provider=_is_active,
        completion_callback=_on_completion,
    )

    _node_spec = spec
    _memory = memory
    _cpu = cpu
    _engine = engine

    # Seed the static gauges that never change at runtime. Other gauges are
    # recomputed on every /metrics scrape.
    EDGE_TOTAL_RAM_KB.labels(node_id=spec.node_id).set(spec.total_ram_kb)
    EDGE_USABLE_RAM_KB.labels(node_id=spec.node_id).set(spec.usable_ram_kb)
    EDGE_CPU_CORES.labels(node_id=spec.node_id).set(spec.cpu_cores)

    logger.info(
        "Edge node %s ready: total=%dKB usable=%dKB cores=%d accepts=%s",
        spec.node_id,
        spec.total_ram_kb,
        spec.usable_ram_kb,
        spec.cpu_cores,
        [c.value for c in spec.accepted_classes],
    )

    yield
    # No teardown work needed.


app = FastAPI(title="Edge Node", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers for handlers
# ---------------------------------------------------------------------------


def _require_initialized() -> tuple[NodeSpec, MemoryManager, CpuManager, ExecutionEngine]:
    """Return the four module globals or raise 503 if startup hasn't run.

    Should never trip in production — lifespan completes before the app
    starts serving requests — but a clean error is friendlier than an
    AttributeError in the rare race or misconfiguration.
    """
    if _node_spec is None or _memory is None or _cpu is None or _engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="edge node not initialized yet",
        )
    return _node_spec, _memory, _cpu, _engine


def _build_runtime_status(
    spec: NodeSpec, mem: MemoryManager, cpu: CpuManager, engine: ExecutionEngine
) -> NodeRuntimeStatus:
    """Assemble the live state slice of a /status response."""
    import time as _time

    mem_snap = mem.snapshot()
    cpu_snap = cpu.snapshot()
    return NodeRuntimeStatus(
        reachable=True,
        used_ram_kb=int(mem_snap["used_ram_kb"]),
        available_ram_kb=int(mem_snap["available_ram_kb"]),
        is_memory_saturated=bool(mem_snap["is_memory_saturated"]),
        cpu_cores_in_use=int(cpu_snap["cpu_cores_in_use"]),
        cpu_utilization_pct=float(cpu_snap["cpu_utilization_pct"]),
        is_cpu_saturated=bool(cpu_snap["is_cpu_saturated"]),
        active_task_count=engine.active_task_count(),
        last_seen=_time.time(),
        error=None,
    )


# ---------------------------------------------------------------------------
# Request/response models for admin endpoints
# ---------------------------------------------------------------------------


class InjectMemoryRequest(BaseModel):
    kb: int = Field(..., gt=0, description="Amount of phantom RAM to reserve")
    hold_seconds: float = Field(..., gt=0, description="Duration of the hold")


class StatusResponse(BaseModel):
    spec: NodeSpec
    runtime: NodeRuntimeStatus


class AdmitResponse(BaseModel):
    admitted: bool
    reason: str | None = None
    task_id: str


class AdminAck(BaseModel):
    ok: bool = True
    detail: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/execute",
    status_code=status.HTTP_201_CREATED,
    response_model=AdmitResponse,
    responses={
        409: {"description": "rejected: insufficient resources or wrong class"},
        503: {"description": "node inactive"},
    },
)
def execute(task: Task) -> Response:
    """Submit a task for execution.

    On success: 201 with {admitted: true, task_id}; the task runs in the
    background and resources are released on completion.

    On failure: 409 (rejected for resource/class reasons) or 503 (node
    marked inactive). The reason field is one of the values listed in the
    AdmitDecision docstring.
    """
    spec, _, _, engine = _require_initialized()
    decision: AdmitDecision = engine.try_admit(task)

    if decision.admitted:
        EDGE_TASKS_ADMITTED_TOTAL.labels(
            node_id=spec.node_id, task_class=task.task_class.value
        ).inc()
        body = AdmitResponse(
            admitted=True, reason=None, task_id=task.task_id
        ).model_dump()
        return Response(
            content=AdmitResponse(**body).model_dump_json(),
            status_code=status.HTTP_201_CREATED,
            media_type="application/json",
        )

    # Rejected. Map reason to HTTP status: node_inactive -> 503, everything
    # else (insufficient_memory, insufficient_cpu_cores, class_not_accepted)
    # -> 409.
    EDGE_TASKS_REJECTED_TOTAL.labels(
        node_id=spec.node_id, reason=decision.reason or "unknown"
    ).inc()

    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if decision.reason == "node_inactive"
        else status.HTTP_409_CONFLICT
    )
    body = AdmitResponse(
        admitted=False, reason=decision.reason, task_id=task.task_id
    )
    return Response(
        content=body.model_dump_json(),
        status_code=http_status,
        media_type="application/json",
    )


@app.get("/status", response_model=StatusResponse)
def get_status() -> StatusResponse:
    """Return the node's static spec plus its current runtime state.

    Called by the scheduler's reconciliation thread every ~500ms and by
    the dashboard for live displays. Cheap: a couple of locked snapshots.
    """
    spec, mem, cpu, engine = _require_initialized()
    return StatusResponse(
        spec=spec,
        runtime=_build_runtime_status(spec, mem, cpu, engine),
    )


@app.post("/admin/inject_memory", response_model=AdminAck)
def inject_memory(req: InjectMemoryRequest) -> AdminAck:
    """Reserve phantom RAM to drive the node toward saturation.

    Used by the Node Saturation test scenario. The injection counts toward
    used_ram_kb and is_memory_saturated() but bypasses the usable_ram_kb
    cap on purpose — the whole point is to push the node past its normal
    operating range and observe how the adaptive trigger reacts.
    """
    _, mem, _, _ = _require_initialized()
    mem.inject(kb=req.kb, hold_seconds=req.hold_seconds)
    return AdminAck(
        ok=True,
        detail=f"injected {req.kb} KB for {req.hold_seconds}s",
    )


@app.post("/admin/reset", response_model=AdminAck)
def admin_reset() -> AdminAck:
    """Drop all in-flight tasks, resource holds, injections, and history.

    Called by the scheduler between trials. Lock ordering: execution engine
    first (so no new task can complete and try to release into freshly
    cleared managers), then memory, then CPU.
    """
    _, mem, cpu, engine = _require_initialized()
    engine.reset()
    mem.reset()
    cpu.reset()
    return AdminAck(ok=True, detail="state cleared")


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus exposition.

    Refreshes the gauges that depend on live state on every scrape, then
    returns the standard text format. Counters and the runtime histogram
    are updated inline by the admit/reject/completion paths and need no
    refresh here.
    """
    spec, mem, cpu, engine = _require_initialized()
    mem_snap = mem.snapshot()
    cpu_snap = cpu.snapshot()
    used = int(mem_snap["used_ram_kb"])
    total = spec.total_ram_kb

    EDGE_USED_RAM_KB.labels(node_id=spec.node_id).set(used)
    EDGE_AVAILABLE_RAM_KB.labels(node_id=spec.node_id).set(
        int(mem_snap["available_ram_kb"])
    )
    EDGE_MEMORY_UTILIZATION_PCT.labels(node_id=spec.node_id).set(
        (used / total) * 100.0 if total > 0 else 0.0
    )
    EDGE_IS_MEMORY_SATURATED.labels(node_id=spec.node_id).set(
        1.0 if mem_snap["is_memory_saturated"] else 0.0
    )

    EDGE_CPU_CORES_IN_USE.labels(node_id=spec.node_id).set(
        int(cpu_snap["cpu_cores_in_use"])
    )
    EDGE_CPU_UTILIZATION_PCT.labels(node_id=spec.node_id).set(
        float(cpu_snap["cpu_utilization_pct"])
    )
    EDGE_IS_CPU_SATURATED.labels(node_id=spec.node_id).set(
        1.0 if cpu_snap["is_cpu_saturated"] else 0.0
    )

    EDGE_ACTIVE_TASK_COUNT.labels(node_id=spec.node_id).set(
        engine.active_task_count()
    )

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe. Returns 200 once the node has finished startup."""
    spec, _, _, _ = _require_initialized()
    return {"status": "ok", "node_id": spec.node_id}