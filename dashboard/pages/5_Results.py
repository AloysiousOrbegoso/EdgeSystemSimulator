"""Results page — runs.csv viewer, Friedman test trigger, plot gallery."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd
import streamlit as st


RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "results"))
RUNS_CSV = RESULTS_DIR / "runs.csv"
ANALYSIS_DIR = RESULTS_DIR / "analysis"
TIMESERIES_DIR = ANALYSIS_DIR / "timeseries"
FRIEDMAN_CSV = ANALYSIS_DIR / "friedman_summary.csv"
SUMMARY_MD = ANALYSIS_DIR / "summary.md"


st.title("Results")

if not RUNS_CSV.exists():
    st.info(
        f"No runs.csv yet at `{RUNS_CSV}`. Run a scenario first from the "
        "Scenarios page (or via `python -m workload.scenario_runner`)."
    )
    st.stop()


# ---------------------------------------------------------------------------
# runs.csv
# ---------------------------------------------------------------------------

st.subheader("runs.csv")

df = pd.read_csv(RUNS_CSV)
st.metric("Trial-rows", len(df))

with st.expander("Filter", expanded=True):
    fc1, fc2 = st.columns(2)
    scenarios = sorted(df["scenario_name"].dropna().unique().tolist())
    algorithms = sorted(df["algorithm"].dropna().unique().tolist())
    sel_scenarios = fc1.multiselect(
        "Scenarios", scenarios, default=scenarios
    )
    sel_algorithms = fc2.multiselect(
        "Algorithms", algorithms, default=algorithms
    )

filtered = df[
    df["scenario_name"].isin(sel_scenarios)
    & df["algorithm"].isin(sel_algorithms)
]

# Pick a manageable column subset by default; toggleable.
default_cols = [
    "run_id",
    "scenario_name",
    "algorithm",
    "trial_index",
    "comparison_id",
    "makespan_seconds",
    "avg_response_time_seconds",
    "workload_variance",
    "avg_memory_utilization_pct",
    "avg_cpu_utilization_pct",
    "scheduling_overhead_seconds_total",
    "tasks_completed",
    "tasks_rejected",
]
cols_present = [c for c in default_cols if c in filtered.columns]
all_cols = list(filtered.columns)

with st.expander("Choose columns", expanded=False):
    chosen_cols = st.multiselect(
        "Columns to display", all_cols, default=cols_present
    )
chosen_cols = chosen_cols or cols_present

st.dataframe(filtered[chosen_cols], use_container_width=True)

st.download_button(
    label="Download filtered as CSV",
    data=filtered.to_csv(index=False).encode("utf-8"),
    file_name="runs_filtered.csv",
    mime="text/csv",
)


# ---------------------------------------------------------------------------
# Per-scenario quick comparison
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Quick comparison")

if len(filtered) == 0:
    st.info("No rows after filter.")
else:
    metric_options = [
        "makespan_seconds",
        "avg_response_time_seconds",
        "workload_variance",
        "avg_memory_utilization_pct",
        "avg_cpu_utilization_pct",
        "scheduling_overhead_seconds_total",
        "tasks_rejected",
    ]
    metric_options = [m for m in metric_options if m in filtered.columns]
    if metric_options:
        chosen_metric = st.selectbox("Metric", metric_options, index=0)
        agg = (
            filtered.groupby(["scenario_name", "algorithm"])[chosen_metric]
            .agg(["mean", "std", "count"])
            .reset_index()
        )
        st.dataframe(agg, use_container_width=True)


# ---------------------------------------------------------------------------
# Run Friedman + regenerate plots + summary
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Analysis")

ac1, ac2, ac3 = st.columns(3)
with ac1:
    if st.button("Run Friedman"):
        try:
            from scheduler.analysis.friedman import (
                DEFAULT_METRICS,
                run_friedman,
                write_friedman_csv,
            )
            with st.spinner("Running Friedman tests..."):
                results = run_friedman(RUNS_CSV, metrics=DEFAULT_METRICS)
                write_friedman_csv(results, ANALYSIS_DIR)
            st.success(f"Wrote {len(results)} metric results to {FRIEDMAN_CSV}.")
        except Exception as e:
            st.error(f"Friedman failed: {e}")
            logging.exception("Friedman run failed")

with ac2:
    if st.button("Regenerate plots"):
        try:
            from scheduler.analysis.timeseries import regenerate_plots
            with st.spinner("Building time-series plots..."):
                produced = regenerate_plots(results_dir=RESULTS_DIR)
            count = sum(len(v) for v in produced.values())
            st.success(f"Regenerated {count} plots.")
        except Exception as e:
            st.error(f"Plot regen failed: {e}")
            logging.exception("Plot regeneration failed")

with ac3:
    if st.button("Build summary.md"):
        try:
            from scheduler.analysis.report import generate_report
            with st.spinner("Generating summary report..."):
                out = generate_report(
                    results_dir=RESULTS_DIR, regen_plots=False, rerun_friedman=True
                )
            st.success(f"Wrote {out}")
        except Exception as e:
            st.error(f"Summary failed: {e}")
            logging.exception("Summary build failed")


# ---------------------------------------------------------------------------
# Friedman view
# ---------------------------------------------------------------------------

if FRIEDMAN_CSV.exists():
    st.divider()
    st.subheader("Friedman summary")
    fdf = pd.read_csv(FRIEDMAN_CSV)
    st.dataframe(fdf, use_container_width=True)


# ---------------------------------------------------------------------------
# Plot gallery
# ---------------------------------------------------------------------------

if TIMESERIES_DIR.exists():
    pngs = sorted(TIMESERIES_DIR.glob("*.png"))
    if pngs:
        st.divider()
        st.subheader("Plots")
        # Group by leading scenario name (filename convention from
        # scheduler.analysis.timeseries: <scenario>_<...>.png).
        from collections import defaultdict
        groups: dict[str, list[Path]] = defaultdict(list)
        for p in pngs:
            scenario_key = p.name.split("_")[0]
            groups[scenario_key].append(p)
        for scenario_key in sorted(groups.keys()):
            with st.expander(f"`{scenario_key}` — {len(groups[scenario_key])} plots"):
                for p in groups[scenario_key]:
                    st.image(str(p), caption=p.name, use_container_width=True)


# ---------------------------------------------------------------------------
# Markdown summary, if present
# ---------------------------------------------------------------------------

if SUMMARY_MD.exists():
    st.divider()
    st.subheader("summary.md")
    st.markdown(SUMMARY_MD.read_text(encoding="utf-8"))