"""
Thesis-ready summary report generator.

Reads:
  results/runs.csv                          — all trial outcomes
  results/analysis/friedman_summary.csv     — Friedman results (auto-built if missing)
  results/analysis/timeseries/*.png         — visual artifacts (auto-built if missing)

Writes:
  results/analysis/summary.md
  results/analysis/friedman_summary.csv     (if it didn't exist)
  results/analysis/timeseries/*.png         (if they didn't exist)

The Markdown is structured for direct paste into the thesis's evaluation
chapter. Sections in order:

  1. Run inventory          — how many trials per (scenario, algorithm)
  2. Per-metric Friedman     — χ², p, mean ranks, mean ± std per algorithm
  3. Per-scenario summary    — mean ± std for the headline metrics, both algos
  4. Plot index              — links to PNGs grouped by scenario
  5. Caveats                 — known data quality issues observed in the run

CLI:
    python -m scheduler.analysis.report
        --results-dir results
        [--regen-plots] [--rerun-friedman]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from scheduler.analysis.friedman import (
    DEFAULT_METRICS,
    LOWER_IS_BETTER,
    run_friedman,
    write_friedman_csv,
)
from scheduler.analysis.timeseries import regenerate_plots

logger = logging.getLogger(__name__)


# Metrics that get prominent treatment in the per-scenario summary table.
HEADLINE_METRICS = [
    "makespan_seconds",
    "avg_response_time_seconds",
    "workload_variance",
    "distribution_balance_index",
    "avg_memory_utilization_pct",
    "avg_cpu_utilization_pct",
    "scheduling_overhead_seconds_total",
    "tasks_completed",
    "tasks_rejected",
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_report(
    results_dir: Path | str = "results",
    regen_plots: bool = False,
    rerun_friedman: bool = True,
    metrics: list[str] | None = None,
) -> Path:
    """Build summary.md and return its path.

    `regen_plots` rebuilds every PNG even if some already exist (useful
    after schema changes). `rerun_friedman` is on by default because it's
    cheap and ensures the table reflects the current CSV.
    """
    results_dir = Path(results_dir)
    runs_csv = results_dir / "runs.csv"
    analysis_dir = results_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    timeseries_dir = analysis_dir / "timeseries"

    if not runs_csv.exists():
        raise FileNotFoundError(
            f"runs.csv not found at {runs_csv}; run a scenario first"
        )

    df = pd.read_csv(runs_csv)
    if df.empty:
        raise RuntimeError(f"{runs_csv} is empty")

    # Run / refresh Friedman.
    target_metrics = metrics or DEFAULT_METRICS
    friedman_summary_path = analysis_dir / "friedman_summary.csv"
    if rerun_friedman or not friedman_summary_path.exists():
        results = run_friedman(runs_csv, metrics=target_metrics)
        write_friedman_csv(results, analysis_dir)
    friedman_df = (
        pd.read_csv(friedman_summary_path)
        if friedman_summary_path.exists()
        else pd.DataFrame()
    )

    # Regenerate plots if requested or directory empty.
    if regen_plots or not timeseries_dir.exists() or not any(timeseries_dir.glob("*.png")):
        regenerate_plots(results_dir=results_dir)

    # Build the markdown sections.
    sections: list[str] = []
    sections.append(_section_header(df))
    sections.append(_section_run_inventory(df))
    sections.append(_section_friedman(friedman_df))
    sections.append(_section_per_scenario_summary(df))
    sections.append(_section_plot_index(df, timeseries_dir, results_dir))
    sections.append(_section_caveats(df))

    out_path = analysis_dir / "summary.md"
    out_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    logger.info("Wrote summary report to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _section_header(df: pd.DataFrame) -> str:
    n_rows = len(df)
    scenarios = sorted(df["scenario_name"].unique().tolist())
    algorithms = sorted(df["algorithm"].unique().tolist())
    return (
        "# Performance Evaluation Summary\n\n"
        f"Total trial rows: **{n_rows}**  \n"
        f"Scenarios: {', '.join(f'`{s}`' for s in scenarios)}  \n"
        f"Algorithms: {', '.join(f'`{a}`' for a in algorithms)}"
    )


def _section_run_inventory(df: pd.DataFrame) -> str:
    """Table of trial counts per (scenario, algorithm)."""
    pivot = (
        df.groupby(["scenario_name", "algorithm"])
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )
    lines = ["## 1. Run inventory", "", _df_to_markdown(pivot.reset_index())]
    return "\n".join(lines)


def _section_friedman(friedman_df: pd.DataFrame) -> str:
    """Friedman results — chi-square, p-value, mean ranks, mean ± std."""
    if friedman_df.empty:
        return "## 2. Friedman test results\n\n_No Friedman results available._"

    lines = [
        "## 2. Friedman test results",
        "",
        "Per-metric Friedman test using `comparison_id` as the blocking unit. "
        "**Lower mean rank** indicates better performance for "
        "smaller-is-better metrics (most metrics in this thesis); "
        "see ARCHITECTURE.md §7 for the full direction map.",
        "",
    ]

    cols = ["metric", "n_blocks", "k_algorithms", "chi_square", "p_value",
            "significant_at_alpha_05"]
    cols += [c for c in friedman_df.columns if c.startswith("mean_rank_")]
    cols += [c for c in friedman_df.columns if c.startswith("mean_value_")]
    cols += [c for c in friedman_df.columns if c.startswith("std_value_")]
    cols = [c for c in cols if c in friedman_df.columns]

    formatted = friedman_df[cols].copy()
    # Round floats for readability.
    for c in formatted.columns:
        if formatted[c].dtype == float:
            formatted[c] = formatted[c].map(
                lambda v: f"{v:.4g}" if pd.notna(v) else ""
            )
    # Annotate the metric name with direction-of-improvement hint.
    formatted["metric"] = formatted["metric"].apply(
        lambda m: f"{m} ↓" if m in LOWER_IS_BETTER else f"{m} ↑"
    )

    lines.append(_df_to_markdown(formatted))
    lines.append("")
    lines.append("Legend: ↓ smaller is better, ↑ larger is better. "
                 "`significant_at_alpha_05` is True when p < 0.05.")
    return "\n".join(lines)


def _section_per_scenario_summary(df: pd.DataFrame) -> str:
    """Mean ± std for headline metrics per (scenario, algorithm)."""
    lines = ["## 3. Per-scenario summary"]
    available = [m for m in HEADLINE_METRICS if m in df.columns]
    if not available:
        return "\n".join(lines + ["", "_No headline metrics available._"])

    for scenario in sorted(df["scenario_name"].unique()):
        sub = df[df["scenario_name"] == scenario]
        lines.append("")
        lines.append(f"### `{scenario}`")
        lines.append("")
        rows = []
        for metric in available:
            row = {"metric": metric + (" ↓" if metric in LOWER_IS_BETTER else " ↑")}
            for algo in sorted(sub["algorithm"].unique()):
                vals = sub.loc[sub["algorithm"] == algo, metric].dropna()
                if vals.empty:
                    row[algo] = ""
                    continue
                mean = vals.mean()
                std = vals.std(ddof=1) if len(vals) > 1 else 0.0
                row[algo] = f"{mean:.3g} ± {std:.3g}  (n={len(vals)})"
            rows.append(row)
        lines.append(_df_to_markdown(pd.DataFrame(rows)))
    return "\n".join(lines)


def _section_plot_index(
    df: pd.DataFrame, timeseries_dir: Path, results_dir: Path
) -> str:
    """Markdown links to all PNGs, grouped by scenario."""
    lines = ["## 4. Plot index"]
    if not timeseries_dir.exists():
        lines.append("")
        lines.append("_No plots generated yet._")
        return "\n".join(lines)

    pngs = sorted(timeseries_dir.glob("*.png"))
    if not pngs:
        lines.append("")
        lines.append("_No plots found in timeseries/._")
        return "\n".join(lines)

    by_scenario: dict[str, list[Path]] = {}
    for p in pngs:
        # Filename pattern: <scenario>_<algorithm-or-node>_<kind>.png
        # Use the leading scenario name as the group key.
        scenario = p.name.split("_")[0]
        by_scenario.setdefault(scenario, []).append(p)

    for scenario in sorted(by_scenario.keys()):
        lines.append("")
        lines.append(f"### `{scenario}`")
        for p in by_scenario[scenario]:
            rel = p.relative_to(results_dir)
            lines.append(f"- [{p.name}]({rel.as_posix()})")
    return "\n".join(lines)


def _section_caveats(df: pd.DataFrame) -> str:
    """Surface known data-quality issues so the report is honest."""
    notes: list[str] = []

    # Algorithms with very few trials per scenario are flagged.
    counts = df.groupby(["scenario_name", "algorithm"]).size()
    sparse = counts[counts < 5]
    if not sparse.empty:
        notes.append(
            f"- {len(sparse)} (scenario, algorithm) pairs have fewer than 5 "
            "trials. Friedman may have low power for these scenarios."
        )

    # Scenarios with only one algorithm prevent any pairwise comparison.
    algos_per_scenario = df.groupby("scenario_name")["algorithm"].nunique()
    single = algos_per_scenario[algos_per_scenario < 2]
    if not single.empty:
        names = ", ".join(f"`{n}`" for n in single.index.tolist())
        notes.append(
            f"- The following scenarios have only one algorithm and "
            f"contribute nothing to Friedman: {names}."
        )

    # Tasks-rejected fraction higher than 5% on any row.
    if "tasks_rejected" in df.columns and "tasks_total" in df.columns:
        # Compute rejection ratio defensively; treat divide-by-zero and
        # NaN as "not high reject" rather than relying on the deprecated
        # use_inf_as_na option.
        ratio = df["tasks_rejected"] / df["tasks_total"].replace(0, pd.NA)
        high_reject = df[(df["tasks_total"] > 0) & (ratio > 0.05).fillna(False)]
        if not high_reject.empty:
            notes.append(
                f"- {len(high_reject)} trials had >5% tasks rejected by the "
                "edge nodes; check the JSONL event logs for those run_ids "
                "to confirm rejections were expected (e.g., Resource-"
                "Constrained or Node Saturation scenarios)."
            )

    lines = ["## 5. Caveats and data quality"]
    if not notes:
        lines.append("")
        lines.append("_No anomalies detected in the current results._")
    else:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _df_to_markdown(df: pd.DataFrame) -> str:
    """Pandas .to_markdown requires `tabulate`; this is a minimal fallback
    so the report module has no extra dependency just for one table.
    Renders a basic GFM table."""
    if df.empty:
        return "_(no data)_"
    headers = [str(c) for c in df.columns]
    rows = [
        [("" if pd.isna(v) else str(v)) for v in row]
        for row in df.itertuples(index=False, name=None)
    ]
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the thesis-ready summary report"
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Path to the results/ directory (default: %(default)s)",
    )
    parser.add_argument(
        "--regen-plots",
        action="store_true",
        help="Force regeneration of all PNGs (default: only if missing)",
    )
    parser.add_argument(
        "--no-rerun-friedman",
        action="store_true",
        help="Do not rerun Friedman; use existing friedman_summary.csv",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        out = generate_report(
            results_dir=args.results_dir,
            regen_plots=args.regen_plots,
            rerun_friedman=not args.no_rerun_friedman,
        )
    except Exception:
        logger.exception("Report generation failed")
        return 1
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())