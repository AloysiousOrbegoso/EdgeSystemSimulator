"""Scenarios page — pick a YAML scenario, run it against the scheduler.

Discovers scenarios from `./scenarios/*.yaml`. Running invokes the
existing scenario_runner module in-process (the same code path the CLI
uses). Long runs are blocking from Streamlit's perspective; the page
shows a spinner and the per-trial progress as TrialResults stream back.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import streamlit as st

from dashboard.api_client import SchedulerClient
from workload.arrival import load_scenario
from workload.scenario_runner import RunnerConfig, run_loaded_scenario


SCENARIOS_DIR = Path(os.environ.get("SCENARIOS_DIR", "scenarios"))


st.title("Scenarios")

client: SchedulerClient = st.session_state.get("client") or SchedulerClient()

# Pull the list of scenarios.
if not SCENARIOS_DIR.exists():
    st.error(
        f"Scenarios directory not found: {SCENARIOS_DIR.resolve()}. "
        "Run streamlit from the project root."
    )
    st.stop()

scenario_paths = sorted(SCENARIOS_DIR.glob("*.yaml"))
if not scenario_paths:
    st.info(f"No scenario YAML files in {SCENARIOS_DIR.resolve()}.")
    st.stop()


# ---------------------------------------------------------------------------
# Scenario picker + preview
# ---------------------------------------------------------------------------

names = [p.name for p in scenario_paths]
selected_name = st.selectbox("Scenario", names)
selected_path = SCENARIOS_DIR / selected_name

try:
    scenario = load_scenario(selected_path)
except Exception as e:
    st.error(f"Failed to parse scenario: {e}")
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("Trials", scenario.trials)
c2.metric("Algorithms", len(scenario.algorithms))
c3.metric("Nodes", len(scenario.nodes))

st.markdown(f"**Description:** {scenario.description.strip() or '_(none)_'}")
st.markdown(f"**Algorithms:** {', '.join(f'`{a}`' for a in scenario.algorithms)}")
with st.expander("Show parsed scenario"):
    st.code(selected_path.read_text(encoding="utf-8"), language="yaml")


# ---------------------------------------------------------------------------
# Run scenario
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Run")

st.caption(
    "Each (algorithm × trial) writes one row to `results/runs.csv` and one "
    "JSONL file to `results/events/`. Re-running a scenario is additive — "
    "rows accumulate."
)

drain_timeout = st.number_input(
    "Drain timeout per trial (seconds)",
    min_value=10.0,
    value=600.0,
    step=10.0,
    help="Maximum time to wait for the queue and in-flight tasks to drain "
    "before forcibly ending the trial.",
)

if st.button("Run scenario"):
    cfg = RunnerConfig(
        scheduler_url=client.base_url,
        drain_timeout_seconds=float(drain_timeout),
    )
    expected = scenario.trials * len(scenario.algorithms)
    progress = st.progress(0.0, text=f"Starting {expected} trial-runs...")
    placeholder = st.empty()
    try:
        # The runner returns the full result list at the end. We don't get
        # streaming updates here, so we just show "running…" and block.
        # For longer scenarios consider running from CLI with `python -m
        # workload.scenario_runner` so logs stream to your terminal.
        with st.spinner(f"Running {selected_name} ({expected} trial-runs)..."):
            results = run_loaded_scenario(scenario, cfg)
        progress.progress(1.0, text="Done")
        st.success(
            f"Completed {len(results)} trial-runs. "
            "See the Results page for analysis."
        )
        # Show the last few rows.
        if results:
            with st.expander("Latest TrialResults", expanded=True):
                for r in results[-min(6, len(results)):]:
                    st.write(
                        f"- `{r.run_id}` | algo=`{r.algorithm}` "
                        f"| trial={r.trial_index} | makespan={r.makespan_seconds:.2f}s "
                        f"| avg_rt={r.avg_response_time_seconds:.2f}s "
                        f"| completed={r.tasks_completed}/{r.tasks_total}"
                    )
    except Exception as e:
        st.error(f"Run failed: {type(e).__name__}: {e}")
        logging.exception("Scenario run failed from dashboard")