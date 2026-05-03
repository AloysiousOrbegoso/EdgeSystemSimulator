"""
Friedman test — statistical comparison of algorithms across paired trials.

The Friedman test ranks algorithms within each block (paired observation
indexed by `comparison_id`) and tests whether the rank sums differ
significantly. It is non-parametric, handles repeated measures, and does
not require normally distributed data — exactly the protocol the thesis
specifies.

Inputs
  results/runs.csv  — one row per (algorithm, scenario, trial)
  metric            — column name to compare (e.g., "makespan_seconds")

Procedure
  1. Read runs.csv with pandas.
  2. Pivot to wide form: rows = comparison_id, columns = algorithm,
     values = metric.
  3. Drop rows with any missing value (a trial whose run failed).
  4. Apply scipy.stats.friedmanchisquare across the algorithm columns.
  5. Compute mean rank per algorithm (lower rank = better for "smaller is
     better" metrics; we report the raw ranks and let the consumer
     interpret).
  6. Return a structured FriedmanResult.

With k=2 algorithms, Friedman has df=1 and reduces to a sign-test-like
comparison. The test is still valid; with 70 paired observations
(14 scenarios × 5 trials) the power is more than sufficient.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from scipy import stats  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FriedmanResult:
    """Output of one Friedman test, for one metric across all algorithms."""

    metric: str
    n_blocks: int                          # number of comparison_id groups used
    k_algorithms: int
    chi_square: float
    df: int
    p_value: float
    mean_rank: dict[str, float]            # algorithm -> mean rank
    significant_at_alpha_05: bool

    # Optional: per-algorithm summary stats so the report can include
    # "mean ± std" alongside the test result without a second pass.
    mean_value: dict[str, float] = field(default_factory=dict)
    std_value: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


# Metrics on which adaptive Min-Min is hypothesized to do BETTER (lower is
# better). Kept as a hint for the report layer; Friedman itself is direction-
# agnostic.
LOWER_IS_BETTER = {
    "makespan_seconds",
    "avg_response_time_seconds",
    "completion_time_p50_seconds",
    "completion_time_p95_seconds",
    "workload_variance",
    "distribution_balance_index",
    "peak_memory_utilization_pct",
    "p95_memory_utilization_pct",
    "time_above_memory_saturation_seconds",
    "peak_cpu_utilization_pct",
    "p95_cpu_utilization_pct",
    "time_above_cpu_saturation_seconds",
    "scheduling_overhead_seconds_total",
    "scheduling_overhead_per_decision_mean_ms",
    "scheduling_overhead_per_decision_p95_ms",
    "tasks_rejected",
    "tasks_failed",
}

# Default list of metrics worth Friedman-testing. Anything in TrialResult
# can be tested; this is the curated "thesis-relevant" subset.
DEFAULT_METRICS = sorted(
    LOWER_IS_BETTER
    | {
        "tasks_completed",
        "avg_memory_utilization_pct",
        "avg_cpu_utilization_pct",
    }
)


def run_friedman(
    runs_csv: Path | str,
    metrics: list[str] | None = None,
    alpha: float = 0.05,
) -> list[FriedmanResult]:
    """Run Friedman tests for the given metrics. Returns one result per
    metric. Skips metrics that have fewer than 2 algorithms or fewer than
    2 complete blocks (can't run the test)."""
    runs_csv = Path(runs_csv)
    if not runs_csv.exists():
        raise FileNotFoundError(f"runs.csv not found at {runs_csv}")

    df = pd.read_csv(runs_csv)
    if df.empty:
        logger.warning("runs.csv is empty; nothing to test")
        return []

    target_metrics = metrics or DEFAULT_METRICS
    results: list[FriedmanResult] = []
    for metric in target_metrics:
        if metric not in df.columns:
            logger.info("metric '%s' not in runs.csv; skipping", metric)
            continue
        result = _friedman_one_metric(df, metric, alpha)
        if result is not None:
            results.append(result)
    return results


def write_friedman_csv(
    results: list[FriedmanResult], out_dir: Path | str
) -> Path:
    """Serialize results to a single CSV at <out_dir>/friedman_summary.csv.

    One row per metric. Mean ranks and per-algorithm stats are unrolled
    into prefixed columns (mean_rank_<algo>, mean_value_<algo>, etc.).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "friedman_summary.csv"

    rows: list[dict] = []
    for r in results:
        row: dict[str, object] = {
            "metric": r.metric,
            "n_blocks": r.n_blocks,
            "k_algorithms": r.k_algorithms,
            "chi_square": r.chi_square,
            "df": r.df,
            "p_value": r.p_value,
            "significant_at_alpha_05": r.significant_at_alpha_05,
        }
        for algo, rank in r.mean_rank.items():
            row[f"mean_rank_{algo}"] = rank
        for algo, mean_val in r.mean_value.items():
            row[f"mean_value_{algo}"] = mean_val
        for algo, std_val in r.std_value.items():
            row[f"std_value_{algo}"] = std_val
        rows.append(row)

    pd.DataFrame(rows).to_csv(out_path, index=False)
    logger.info("Wrote Friedman summary to %s (%d metrics)", out_path, len(rows))
    return out_path


# ---------------------------------------------------------------------------
# Single-metric implementation
# ---------------------------------------------------------------------------


def _friedman_one_metric(
    df: pd.DataFrame, metric: str, alpha: float
) -> FriedmanResult | None:
    """Pivot, clean, and test one metric. Returns None if there's not
    enough data."""
    # Pivot to wide form. duplicates within (comparison_id, algorithm) are
    # rare but possible if the runner was restarted mid-trial; aggregate
    # by mean to be safe.
    try:
        wide = df.pivot_table(
            index="comparison_id",
            columns="algorithm",
            values=metric,
            aggfunc="mean",
        )
    except KeyError as e:
        logger.warning("Cannot pivot for metric=%s: %s", metric, e)
        return None

    # Drop blocks where any algorithm is missing.
    wide_clean = wide.dropna(how="any")
    n_blocks = len(wide_clean)
    algorithms = list(wide_clean.columns)
    k = len(algorithms)

    if k < 2:
        logger.info(
            "metric=%s has %d algorithms; need >= 2 for Friedman", metric, k
        )
        return None
    if n_blocks < 2:
        logger.info(
            "metric=%s has %d complete blocks; need >= 2 for Friedman",
            metric,
            n_blocks,
        )
        return None

    # Run the test. friedmanchisquare expects k separate samples (one per
    # algorithm), each with n_blocks values.
    columns = [wide_clean[a].to_numpy() for a in algorithms]
    chi2, p = stats.friedmanchisquare(*columns)

    # Mean ranks per algorithm. Within each block (row), rank columns
    # ascending (smallest value = rank 1). Average ranks per column.
    ranked = wide_clean.rank(axis=1, method="average", ascending=True)
    mean_rank = {a: float(ranked[a].mean()) for a in algorithms}

    mean_value = {a: float(wide_clean[a].mean()) for a in algorithms}
    std_value = {a: float(wide_clean[a].std(ddof=1)) for a in algorithms}

    return FriedmanResult(
        metric=metric,
        n_blocks=int(n_blocks),
        k_algorithms=k,
        chi_square=float(chi2),
        df=k - 1,
        p_value=float(p),
        mean_rank=mean_rank,
        significant_at_alpha_05=p < alpha,
        mean_value=mean_value,
        std_value=std_value,
    )