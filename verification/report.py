"""
Verification report rendering — turns results/verification/report.json
into a readable results/verification/report.md.

Kept separate from verification/runner.py so the report can be regenerated
without re-running tests, and so the runner has no Markdown logic.

The Markdown is structured for direct paste into the thesis testing
chapter:

    # System Verification Report
    Generated: <timestamp>
    Total verdicts: N | Passed: P | Failed: F

    ## Alpha Testing
    ✅ alpha — N/N assertions passed

    ## Beta Testing
    ...

    ## White-Box Testing
    ...

    ## Black-Box Testing
    ...

Failed assertions are expanded inline with their `detail` string and a
relative path to the JSONL event log for debugging.

CLI:
    python -m verification.report
        --results-dir results
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from shared.models import VerificationReport, VerificationVerdict

logger = logging.getLogger(__name__)


TYPE_HEADERS = {
    "alpha": "Alpha Testing",
    "beta": "Beta Testing",
    "whitebox": "White-Box Testing",
    "blackbox": "Black-Box Testing",
}
TYPE_ORDER = ["alpha", "beta", "whitebox", "blackbox"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_report(
    results_dir: Path | str = "results",
) -> Path:
    """Read report.json, write report.md, return the markdown path."""
    results_dir = Path(results_dir)
    json_path = results_dir / "verification" / "report.json"
    md_path = results_dir / "verification" / "report.md"
    if not json_path.exists():
        raise FileNotFoundError(
            f"verification report.json not found at {json_path}; "
            "run the verification suite first"
        )

    raw = json_path.read_text(encoding="utf-8")
    report = VerificationReport.model_validate_json(raw)

    md = _render_markdown(report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")
    logger.info("Wrote verification report (md) to %s", md_path)
    return md_path


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(report: VerificationReport) -> str:
    sections: list[str] = []
    sections.append(_section_header(report))
    sections.extend(_sections_per_type(report))
    sections.append(_section_failures_detail(report))
    return "\n\n".join(sections) + "\n"


def _section_header(report: VerificationReport) -> str:
    started = datetime.fromtimestamp(report.started_at, tz=timezone.utc).isoformat()
    completed = datetime.fromtimestamp(report.completed_at, tz=timezone.utc).isoformat()
    duration = report.completed_at - report.started_at

    algorithms = sorted({v.algorithm for v in report.verdicts}) or ["(none)"]
    pass_pct = (report.passed / report.total_verdicts * 100.0) if report.total_verdicts else 0.0
    overall = "✅ ALL PASSED" if report.failed == 0 else f"❌ {report.failed} FAILED"

    return (
        "# System Verification Report\n\n"
        f"Generated: `{completed}`  \n"
        f"Started:   `{started}`  \n"
        f"Duration:  {duration:.1f}s  \n"
        f"Algorithms: {', '.join(f'`{a}`' for a in algorithms)}  \n\n"
        f"**{overall}** "
        f"({report.passed}/{report.total_verdicts} verdicts, {pass_pct:.1f}%)"
    )


def _sections_per_type(report: VerificationReport) -> list[str]:
    """One section per test type, in fixed order."""
    by_type: dict[str, list[VerificationVerdict]] = defaultdict(list)
    for v in report.verdicts:
        by_type[v.test_type].append(v)

    sections: list[str] = []
    for ttype in TYPE_ORDER:
        if ttype not in by_type:
            continue
        sections.append(_section_for_type(ttype, by_type[ttype]))
    # Any unexpected types (shouldn't happen with the YAML schema, but be
    # forgiving) get appended at the end.
    for ttype in by_type:
        if ttype not in TYPE_ORDER:
            sections.append(_section_for_type(ttype, by_type[ttype]))
    return sections


def _section_for_type(
    ttype: str, verdicts: list[VerificationVerdict]
) -> str:
    """One section: list each test_id with pass/fail per algorithm."""
    header = TYPE_HEADERS.get(ttype, ttype.title())
    lines = [f"## {header}"]

    # Group by test_id, then by sub_run, then list per-algorithm verdicts.
    by_test: dict[str, list[VerificationVerdict]] = defaultdict(list)
    for v in verdicts:
        by_test[v.test_id].append(v)

    for test_id in sorted(by_test.keys()):
        test_verdicts = by_test[test_id]
        all_passed = all(v.overall_passed for v in test_verdicts)
        icon = "✅" if all_passed else "❌"
        lines.append("")
        lines.append(f"### {icon} `{test_id}`")
        lines.extend(_render_verdicts_for_test(test_verdicts))

    return "\n".join(lines)


def _render_verdicts_for_test(
    verdicts: list[VerificationVerdict],
) -> list[str]:
    """Group by sub_run (None means top-level) then by algorithm."""
    out: list[str] = []
    by_subrun: dict[str | None, list[VerificationVerdict]] = defaultdict(list)
    for v in verdicts:
        by_subrun[v.sub_run].append(v)

    # Stable order: None first (top-level), then alphabetical sub_run.
    keys = sorted(by_subrun.keys(), key=lambda k: ("" if k is None else k))
    for key in keys:
        sub_verdicts = by_subrun[key]
        if key is not None:
            out.append("")
            out.append(f"#### sub-run: `{key}`")
        # Group by algorithm to summarize trial counts.
        by_algo: dict[str, list[VerificationVerdict]] = defaultdict(list)
        for v in sub_verdicts:
            by_algo[v.algorithm].append(v)
        for algo in sorted(by_algo.keys()):
            algo_verdicts = by_algo[algo]
            passed = sum(1 for v in algo_verdicts if v.overall_passed)
            total = len(algo_verdicts)
            n_assert = (
                len(algo_verdicts[0].assertion_results) if algo_verdicts else 0
            )
            line_icon = "✅" if passed == total else "❌"
            out.append(
                f"- {line_icon} `{algo}`: {passed}/{total} trials passed "
                f"({n_assert} assertions per trial)"
            )
    return out


def _section_failures_detail(report: VerificationReport) -> str:
    """Expand every failed verdict with its failing-assertion details."""
    failures = [v for v in report.verdicts if not v.overall_passed]
    if not failures:
        return "## Failure detail\n\n_No failures._"

    lines = ["## Failure detail", ""]
    for v in failures:
        header = f"### ❌ `{v.test_id}` / `{v.algorithm}`"
        if v.sub_run:
            header += f" / sub-run `{v.sub_run}`"
        if v.trial_index is not None:
            header += f" / trial {v.trial_index}"
        lines.append(header)
        lines.append("")
        if v.event_log_path:
            lines.append(f"Event log: `{v.event_log_path}`")
            lines.append("")
        for r in v.assertion_results:
            if r.passed:
                continue
            lines.append(f"- ❌ **{r.name}** — {r.detail}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render verification report.json into report.md"
    )
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        out = render_report(results_dir=args.results_dir)
    except Exception:
        logger.exception("Render failed")
        return 1
    print(f"Verification markdown written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())