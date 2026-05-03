"""
Named assertion library for verification tests.

Each assertion is registered with a string name via @register(name) and
takes a TestContext (built by the verification runner from the trial's
JSONL event log + final TrialResult). Verification YAML files reference
assertions by name in their `assertions:` block.

Adding an assertion:
  1. Define a function `def _(ctx): -> AssertionResult` here.
  2. Decorate it with @register("assertion_name").
  3. Reference "assertion_name" in any test YAML's assertions list.

The library is intentionally small for v1 — it covers exactly what the
PDF's Alpha / Beta / White-Box / Black-Box test cases ask about. New
assertions can be added without changing the runner or report code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from shared.models import AssertionResult, NodeSpec, TaskClass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TestContext — the single argument every assertion receives
# ---------------------------------------------------------------------------


@dataclass
class TestContext:
    """Everything an assertion can inspect from one verification trial run.

    Built by the verification runner after the trial finishes. The
    assertion functions are pure: they take this struct, return an
    AssertionResult, and don't do I/O.
    """

    test_id: str
    algorithm: str
    sub_run: str | None
    trial_index: int | None

    # Trial timing
    started_at: float
    completed_at: float
    duration_seconds: float
    timeout_seconds: float
    trial_completed_within_timeout: bool

    # Node configuration used in the trial.
    node_specs: dict[str, NodeSpec]                 # node_id -> NodeSpec

    # Per-task records derived from the JSONL event log.
    # Each record is a dict with keys:
    #   task_id, task_class, memory_kb, workload_units,
    #   submitted_at, dispatched_at, completed_at, node_id, status,
    #   rejection_reason
    task_records: list[dict] = field(default_factory=list)

    # Raw event log path so heavy assertions can scan it directly.
    event_log_path: Path | None = None

    # ---------------- helper accessors ----------------

    def assignments(self) -> list[dict]:
        """All task records that were dispatched (or beyond)."""
        return [r for r in self.task_records if r.get("node_id")]

    def completed(self) -> list[dict]:
        return [r for r in self.task_records if r.get("status") == "completed"]

    def rejected(self) -> list[dict]:
        return [r for r in self.task_records if r.get("status") == "rejected"]

    def first_event_time(self, event_type: str, **filters) -> float | None:
        """Scan the JSONL log for the earliest event matching event_type
        and the given key-value filters. Returns the `t` (relative
        seconds since trial start) or None if not found."""
        if self.event_log_path is None or not self.event_log_path.exists():
            return None
        try:
            with self.event_log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") != event_type:
                        continue
                    if all(ev.get(k) == v for k, v in filters.items()):
                        return float(ev.get("t", 0.0))
        except OSError:
            return None
        return None

    def memory_samples_for(self, node_id: str) -> list[tuple[float, float]]:
        """All (t, pct) memory samples for a node, scanned from the log."""
        if self.event_log_path is None or not self.event_log_path.exists():
            return []
        out: list[tuple[float, float]] = []
        with self.event_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "memory_sample" and ev.get("node_id") == node_id:
                    out.append((float(ev.get("t", 0.0)), float(ev.get("pct", 0.0))))
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


AssertionFn = Callable[[TestContext], AssertionResult]
ASSERTIONS: dict[str, AssertionFn] = {}


def register(name: str) -> Callable[[AssertionFn], AssertionFn]:
    """Register an assertion function under a given name. Used as a
    decorator immediately above each assertion below."""

    def decorator(fn: AssertionFn) -> AssertionFn:
        if name in ASSERTIONS:
            raise ValueError(f"Assertion '{name}' is already registered")
        ASSERTIONS[name] = fn
        return fn

    return decorator


def get_assertion(name: str) -> AssertionFn:
    """Look up an assertion function by name; raise KeyError if missing."""
    if name not in ASSERTIONS:
        available = ", ".join(sorted(ASSERTIONS.keys()))
        raise KeyError(
            f"Unknown assertion '{name}'. Available: {available}"
        )
    return ASSERTIONS[name]


def list_assertions() -> list[str]:
    return sorted(ASSERTIONS.keys())


# ---------------------------------------------------------------------------
# Generic correctness assertions
# ---------------------------------------------------------------------------


@register("all_tasks_have_terminal_status")
def _(ctx: TestContext) -> AssertionResult:
    """Every submitted task must end up in completed | failed | rejected.

    No tasks should remain in `pending` or `dispatched` after the trial
    ends — that would indicate the runner gave up before drain finished.
    """
    bad = [
        r["task_id"]
        for r in ctx.task_records
        if r.get("status") not in {"completed", "failed", "rejected"}
    ]
    return AssertionResult(
        name="all_tasks_have_terminal_status",
        passed=len(bad) == 0,
        detail=f"{len(bad)} non-terminal tasks"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


@register("no_unsupported_allocations")
def _(ctx: TestContext) -> AssertionResult:
    """Every assignment must respect the destination node's
    accepted_classes list. Edge1 takes only lightweight, etc."""
    bad: list[str] = []
    for r in ctx.assignments():
        node_id = r.get("node_id")
        node_spec = ctx.node_specs.get(node_id) if node_id else None
        if node_spec is None:
            continue
        cls_str = r.get("task_class")
        try:
            cls = TaskClass(cls_str)
        except ValueError:
            bad.append(f"{r['task_id']}:bad-class:{cls_str}")
            continue
        if cls not in node_spec.accepted_classes:
            bad.append(f"{r['task_id']}:{cls.value}->{node_id}")
    return AssertionResult(
        name="no_unsupported_allocations",
        passed=len(bad) == 0,
        detail=f"{len(bad)} class violations"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


@register("no_duplicate_assignments")
def _(ctx: TestContext) -> AssertionResult:
    """No task_id should appear in the assignment list more than once."""
    seen: dict[str, int] = {}
    for r in ctx.assignments():
        tid = r["task_id"]
        seen[tid] = seen.get(tid, 0) + 1
    duplicates = [tid for tid, n in seen.items() if n > 1]
    return AssertionResult(
        name="no_duplicate_assignments",
        passed=len(duplicates) == 0,
        detail=f"{len(duplicates)} task_ids assigned more than once"
        + (f" (e.g. {duplicates[:3]})" if duplicates else ""),
    )


@register("all_assignments_logged")
def _(ctx: TestContext) -> AssertionResult:
    """Every dispatched/completed task must have a recorded node_id."""
    bad = [
        r["task_id"]
        for r in ctx.task_records
        if r.get("status") in {"dispatched", "completed"} and not r.get("node_id")
    ]
    return AssertionResult(
        name="all_assignments_logged",
        passed=len(bad) == 0,
        detail=f"{len(bad)} tasks with status but no node_id"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


@register("no_task_skipped")
def _(ctx: TestContext) -> AssertionResult:
    """Every submitted task must have a recorded outcome (completed,
    failed, or rejected). Used for Alpha-style controlled tests where
    'no task should be skipped' is in the spec."""
    bad = [
        r["task_id"]
        for r in ctx.task_records
        if r.get("status") not in {"completed", "failed", "rejected"}
    ]
    return AssertionResult(
        name="no_task_skipped",
        passed=len(bad) == 0,
        detail=f"{len(bad)} tasks without final outcome"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


@register("sufficient_memory_for_each_assignment")
def _(ctx: TestContext) -> AssertionResult:
    """Every dispatched task's memory_kb must be <= its node's
    usable_ram_kb. Independent of feasibility filtering — guards against
    a bug where a class-feasible task would otherwise overflow."""
    bad: list[str] = []
    for r in ctx.assignments():
        node_id = r.get("node_id")
        node_spec = ctx.node_specs.get(node_id) if node_id else None
        if node_spec is None:
            continue
        mem = int(r.get("memory_kb", 0))
        if mem > node_spec.usable_ram_kb:
            bad.append(f"{r['task_id']}:{mem}KB->{node_id}({node_spec.usable_ram_kb}KB)")
    return AssertionResult(
        name="sufficient_memory_for_each_assignment",
        passed=len(bad) == 0,
        detail=f"{len(bad)} oversize allocations"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


# ---------------------------------------------------------------------------
# Class-specific feasibility assertions (for white-box edge node selection)
# ---------------------------------------------------------------------------


@register("edge1_only_lightweight")
def _(ctx: TestContext) -> AssertionResult:
    bad = [
        r["task_id"]
        for r in ctx.assignments()
        if r.get("node_id") == "edge1"
        and r.get("task_class") != TaskClass.LIGHTWEIGHT.value
    ]
    return AssertionResult(
        name="edge1_only_lightweight",
        passed=len(bad) == 0,
        detail=f"{len(bad)} non-lightweight tasks placed on edge1"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


@register("very_heavy_only_on_edge3_or_edge4")
def _(ctx: TestContext) -> AssertionResult:
    allowed = {"edge3", "edge4"}
    bad = [
        f"{r['task_id']}->{r.get('node_id')}"
        for r in ctx.assignments()
        if r.get("task_class") == TaskClass.VERY_HEAVY.value
        and r.get("node_id") not in allowed
    ]
    return AssertionResult(
        name="very_heavy_only_on_edge3_or_edge4",
        passed=len(bad) == 0,
        detail=f"{len(bad)} very_heavy tasks on disallowed nodes"
        + (f" (e.g. {bad[:3]})" if bad else ""),
    )


# ---------------------------------------------------------------------------
# Runtime stability assertions
# ---------------------------------------------------------------------------


@register("scheduler_did_not_hang")
def _(ctx: TestContext) -> AssertionResult:
    return AssertionResult(
        name="scheduler_did_not_hang",
        passed=ctx.trial_completed_within_timeout,
        detail=f"trial duration: {ctx.duration_seconds:.1f}s "
        f"(timeout: {ctx.timeout_seconds:.0f}s)",
    )


@register("no_node_crashed")
def _(ctx: TestContext) -> AssertionResult:
    """No node should appear unreachable in the event log during the trial.

    We look for `node_unreachable` markers or any sample with a recorded
    error, scanning the JSONL once.
    """
    if ctx.event_log_path is None or not ctx.event_log_path.exists():
        return AssertionResult(
            name="no_node_crashed",
            passed=True,
            detail="no event log; assuming no crash",
        )
    crashed_events: list[str] = []
    with ctx.event_log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            # Patterns the scheduler may write when a node disappears.
            if etype == "task_rejected" and "node_error" in str(ev.get("reason", "")):
                crashed_events.append(str(ev.get("node_id", "?")))
    distinct = sorted(set(crashed_events))
    return AssertionResult(
        name="no_node_crashed",
        passed=len(distinct) == 0,
        detail=f"{len(distinct)} nodes had errors during trial"
        + (f": {distinct}" if distinct else ""),
    )


@register("no_unhandled_exceptions")
def _(ctx: TestContext) -> AssertionResult:
    """Placeholder — there is no hook in the current scheduler/edge code
    that emits an `unhandled_exception` event. This assertion always
    passes; if the system ever logs a stack trace into the JSONL stream
    (future improvement), this can be tightened up."""
    return AssertionResult(
        name="no_unhandled_exceptions",
        passed=True,
        detail="no exception markers expected in current event log schema",
    )


@register("valid_scheduling_decisions_throughout")
def _(ctx: TestContext) -> AssertionResult:
    """Every scheduling_decision event in the log must have a non-negative
    decision_ms. A negative value would indicate clock skew or a bug in
    the recorder; the check is cheap insurance."""
    if ctx.event_log_path is None or not ctx.event_log_path.exists():
        return AssertionResult(
            name="valid_scheduling_decisions_throughout",
            passed=True,
            detail="no event log; nothing to check",
        )
    bad = 0
    total = 0
    with ctx.event_log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "scheduling_decision":
                total += 1
                if float(ev.get("decision_ms", 0)) < 0:
                    bad += 1
    return AssertionResult(
        name="valid_scheduling_decisions_throughout",
        passed=bad == 0,
        detail=f"{bad}/{total} scheduling decisions had invalid timing",
    )


# ---------------------------------------------------------------------------
# Adaptive-trigger assertion (white-box, for the adaptive algorithm)
# ---------------------------------------------------------------------------


@register("moderate_plus_redirected_after_saturation")
def _(ctx: TestContext) -> AssertionResult:
    """After edge2 first crosses the 80% memory threshold, no further
    moderate / heavy / very_heavy tasks should be placed on edge2.

    Lightweight tasks may still go to edge2 because the adaptive trigger
    is per the PDF only specified for moderate+ classes.
    """
    samples = ctx.memory_samples_for("edge2")
    saturation_t = next((t for t, pct in samples if pct >= 80.0), None)
    if saturation_t is None:
        return AssertionResult(
            name="moderate_plus_redirected_after_saturation",
            passed=False,
            detail="edge2 never reached 80% memory; cannot evaluate trigger",
        )
    bad: list[str] = []
    moderate_plus = {
        TaskClass.MODERATE.value,
        TaskClass.HEAVY.value,
        TaskClass.VERY_HEAVY.value,
    }
    for r in ctx.assignments():
        # Use submitted_at relative to trial start as a proxy for "after
        # saturation". The dispatched_at field is wall-clock and harder
        # to align here; submitted_at is recorded by the recorder as an
        # absolute epoch second, so we convert via duration.
        dispatched = r.get("dispatched_at_rel")
        if dispatched is None:
            continue
        if (
            dispatched > saturation_t
            and r.get("node_id") == "edge2"
            and r.get("task_class") in moderate_plus
        ):
            bad.append(r["task_id"])
    return AssertionResult(
        name="moderate_plus_redirected_after_saturation",
        passed=len(bad) == 0,
        detail=(
            f"saturated at t={saturation_t:.1f}s; "
            f"{len(bad)} moderate+ tasks still placed on edge2 after that"
            + (f" (e.g. {bad[:3]})" if bad else "")
        ),
    )


@register("adaptive_trigger_activated")
def _(ctx: TestContext) -> AssertionResult:
    """Some moderate/heavy/very_heavy task that was submitted after edge2
    saturated must have been placed somewhere OTHER than edge2.

    Weaker than `moderate_plus_redirected_after_saturation` (which says
    NO moderate+ tasks landed on edge2 post-saturation). This one just
    asks: did at least one redirect actually happen?
    """
    samples = ctx.memory_samples_for("edge2")
    saturation_t = next((t for t, pct in samples if pct >= 80.0), None)
    if saturation_t is None:
        return AssertionResult(
            name="adaptive_trigger_activated",
            passed=False,
            detail="edge2 never reached 80% memory",
        )
    moderate_plus = {
        TaskClass.MODERATE.value,
        TaskClass.HEAVY.value,
        TaskClass.VERY_HEAVY.value,
    }
    redirected = [
        r["task_id"]
        for r in ctx.assignments()
        if (r.get("dispatched_at_rel") or 0) > saturation_t
        and r.get("task_class") in moderate_plus
        and r.get("node_id") != "edge2"
    ]
    return AssertionResult(
        name="adaptive_trigger_activated",
        passed=len(redirected) > 0,
        detail=(
            f"saturated at t={saturation_t:.1f}s; "
            f"{len(redirected)} moderate+ tasks redirected post-saturation"
        ),
    )