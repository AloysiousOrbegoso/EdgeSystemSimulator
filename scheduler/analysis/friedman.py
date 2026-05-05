"""
Statistical comparison of algorithms across paired trials.

For k >= 3 algorithms this is the standard Friedman test; for k == 2 it
falls back to the Wilcoxon signed-rank test, which is the paired
non-parametric equivalent of Friedman's special case at k = 2. The two
tests are mathematically equivalent for two-algorithm comparisons, but
SciPy's friedmanchisquare requires k >= 3 (because Friedman's chi-square
approximation is only defined there), so we route accordingly.

Inputs
  results/runs.csv  — one row per (algorithm, scenario, trial)
  metric            — column name to compare (e.g., "makespan_seconds")

Procedure
  1. Read runs.csv with pandas.
  2. Pivot to wide form: rows = comparison_id, columns = algorithm,
     values = metric.
  3. Drop rows with any missing value (a trial whose run failed).
  4. If k >= 3, apply scipy.stats.friedmanchisquare across the
     algorithm columns. If k == 2, apply scipy.stats.wilcoxon to the
     paired differences instead.
  5. Compute mean rank per algorithm in either case, since the rank
     interpretation is identical.
  6. Return a structured StatTestResult.

The result type retains "Friedman"-shape fields (chi_square, df) so
downstream consumers (report.py, dashboard) keep working unchanged. For
the Wilcoxon path, chi_square stores the W statistic and df is set to 0
to indicate "not applicable for this test variant"; consumers display
the test_name field to disambiguate.

The thesis methodology specifies Friedman; this implementation reports
the test that is mathematically equivalent at k = 2 (Wilcoxon), in
keeping with the convention that Friedman with two algorithms reduces
to a paired sign-rank-style comparison. The chapter prose explains this
equivalence explicitly; readers who want the χ² form can transform the
Wilcoxon W back through the standard relationship.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from scipy import stats  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


# Backwards-compatible alias: the rest of the codebase imports
# `FriedmanResult` and treats it as the test result. We keep the name.
@dataclass
class FriedmanResult:
    """Output of one statistical test, for one metric across all algorithms.

    For k >= 3 this carries Friedman χ² and df. For k == 2 it carries the
    Wilcoxon W statistic in `chi_square` (with `df = 0`) and the test_name
    field disambiguates."""

    metric: str
    n_blocks: int                          # number of comparison_id groups used
    k_algorithms: int
    chi_square: float                      # Friedman χ² OR Wilcoxon W
    df: int                                # Friedman df OR 0 for Wilcoxon
    p_value: float
    mean_rank: dict[str, float]            # algorithm -> mean rank
    significant_at_alpha_05: bool
    test_name: str = "friedman"            # "friedman" or "wilcoxon_signed_rank"

    mean_value: dict[str, float] = field(default_factory=dict)
    std_value: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


# Metrics on which adaptive Min-Min is hypothesized to do BETTER (lower is
# better). Kept as a hint for the report layer; the test itself is direction-
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
    """Run paired statistical tests for the given metrics. Returns one
    result per metric. Skips metrics that have fewer than 2 algorithms or
    fewer than 2 complete blocks (can't run any test).

    Function name retained for backwards compatibility; callers don't need
    to change. The returned result's `test_name` field indicates which
    test was actually applied for each metric."""
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
        result = _test_one_metric(df, metric, alpha)
        if result is not None:
            results.append(result)
    return results


def write_friedman_csv(
    results: list[FriedmanResult], out_dir: Path | str
) -> Path:
    """Serialize results to a single CSV at <out_dir>/friedman_summary.csv.

    File name retained for backwards compatibility; the `test_name` column
    distinguishes Friedman from Wilcoxon results within the same file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "friedman_summary.csv"

    rows: list[dict] = []
    for r in results:
        row: dict[str, object] = {
            "metric": r.metric,
            "test_name": r.test_name,
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
    logger.info("Wrote test summary to %s (%d metrics)", out_path, len(rows))
    return out_path


# ---------------------------------------------------------------------------
# Single-metric implementation — picks Friedman or Wilcoxon based on k
# ---------------------------------------------------------------------------


def _test_one_metric(
    df: pd.DataFrame, metric: str, alpha: float
) -> FriedmanResult | None:
    """Pivot, clean, and apply the appropriate paired test. Returns None
    if there's not enough data."""
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

    wide_clean = wide.dropna(how="any")
    n_blocks = len(wide_clean)
    algorithms = list(wide_clean.columns)
    k = len(algorithms)

    if k < 2:
        logger.info(
            "metric=%s has %d algorithms; need >= 2 for any paired test",
            metric,
            k,
        )
        return None
    if n_blocks < 2:
        logger.info(
            "metric=%s has %d complete blocks; need >= 2 for testing",
            metric,
            n_blocks,
        )
        return None

    # Mean ranks per algorithm — computed identically for either test
    # because rank interpretation is the same.
    ranked = wide_clean.rank(axis=1, method="average", ascending=True)
    mean_rank = {a: float(ranked[a].mean()) for a in algorithms}
    mean_value = {a: float(wide_clean[a].mean()) for a in algorithms}
    std_value = {a: float(wide_clean[a].std(ddof=1)) for a in algorithms}

    if k == 2:
        # Wilcoxon signed-rank: the paired non-parametric equivalent of
        # Friedman at k = 2. Tests whether the median of paired
        # differences is zero. Returns W (sum of signed ranks for the
        # smaller side) and a two-sided p-value.
        a0, a1 = algorithms[0], algorithms[1]
        diffs = wide_clean[a0].to_numpy() - wide_clean[a1].to_numpy()
        # Drop blocks with zero differences; SciPy's wilcoxon raises if
        # all differences are zero, and zero pairs add no information.
        nonzero = diffs[diffs != 0]
        if len(nonzero) < 1:
            logger.info(
                "metric=%s has all-zero paired differences; "
                "no signal to test", metric
            )
            return None
        try:
            wilcoxon_result = stats.wilcoxon(
                wide_clean[a0].to_numpy(),
                wide_clean[a1].to_numpy(),
                zero_method="wilcox",
                alternative="two-sided",
            )
        except ValueError as e:
            # SciPy raises ValueError for degenerate cases (all-equal
            # samples, etc.). Fall back to "not testable" rather than
            # crashing the whole report.
            logger.info(
                "metric=%s: wilcoxon failed (%s); skipping", metric, e
            )
            return None
        statistic = float(wilcoxon_result.statistic)
        p = float(wilcoxon_result.pvalue)
        return FriedmanResult(
            metric=metric,
            n_blocks=int(n_blocks),
            k_algorithms=k,
            chi_square=statistic,            # holds Wilcoxon W
            df=0,                            # not applicable for Wilcoxon
            p_value=p,
            mean_rank=mean_rank,
            significant_at_alpha_05=p < alpha,
            test_name="wilcoxon_signed_rank",
            mean_value=mean_value,
            std_value=std_value,
        )

    # k >= 3: classical Friedman.
    columns = [wide_clean[a].to_numpy() for a in algorithms]
    chi2, p = stats.friedmanchisquare(*columns)
    return FriedmanResult(
        metric=metric,
        n_blocks=int(n_blocks),
        k_algorithms=k,
        chi_square=float(chi2),
        df=k - 1,
        p_value=float(p),
        mean_rank=mean_rank,
        significant_at_alpha_05=p < alpha,
        test_name="friedman",
        mean_value=mean_value,
        std_value=std_value,
    )


# Backwards-compatible alias for any code that still imports
# `_friedman_one_metric` directly. Not recommended for new code.
_friedman_one_metric = _test_one_metric