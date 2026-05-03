"""Verification page — Alpha / Beta / White-Box / Black-Box tests.

Lists every YAML under tests/, lets you run a single test or all of them,
shows the latest pass/fail report.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import streamlit as st

from dashboard.api_client import SchedulerClient
from verification.runner import run_all_tests, run_test


TESTS_DIR = Path(os.environ.get("TESTS_DIR", "tests"))
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "results"))


st.title("Verification")

client: SchedulerClient = st.session_state.get("client") or SchedulerClient()

if not TESTS_DIR.exists():
    st.error(f"tests directory not found: {TESTS_DIR.resolve()}")
    st.stop()

test_paths = sorted(TESTS_DIR.rglob("*.yaml"))
if not test_paths:
    st.info(f"No verification YAML files in {TESTS_DIR.resolve()}.")
    st.stop()


# ---------------------------------------------------------------------------
# Picker + run buttons
# ---------------------------------------------------------------------------

rel_paths = [p.relative_to(TESTS_DIR).as_posix() for p in test_paths]
selected_rel = st.selectbox("Test", rel_paths)
selected_path = TESTS_DIR / selected_rel

with st.expander("Show test YAML"):
    st.code(selected_path.read_text(encoding="utf-8"), language="yaml")

drain_timeout = st.number_input(
    "Drain timeout per trial (seconds)",
    min_value=10.0,
    value=600.0,
    step=10.0,
)

run_c1, run_c2 = st.columns(2)
with run_c1:
    if st.button("Run selected test"):
        try:
            with st.spinner(f"Running {selected_rel}..."):
                verdicts = run_test(
                    test_path=selected_path,
                    scheduler_url=client.base_url,
                    results_dir=RESULTS_DIR,
                    drain_timeout_seconds=float(drain_timeout),
                )
            passed = sum(1 for v in verdicts if v.overall_passed)
            (st.success if passed == len(verdicts) else st.warning)(
                f"{passed}/{len(verdicts)} verdicts passed."
            )
            for v in verdicts:
                icon = "✅" if v.overall_passed else "❌"
                hdr = f"{icon} `{v.test_id}` / `{v.algorithm}`"
                if v.sub_run:
                    hdr += f" / sub_run=`{v.sub_run}`"
                if v.trial_index is not None:
                    hdr += f" / trial={v.trial_index}"
                with st.expander(hdr, expanded=not v.overall_passed):
                    for r in v.assertion_results:
                        st.markdown(
                            f"- {'✅' if r.passed else '❌'} **{r.name}** — {r.detail}"
                        )
                    if v.event_log_path:
                        st.caption(f"event log: `{v.event_log_path}`")
        except Exception as e:
            st.error(f"Run failed: {e}")
            logging.exception("Verification run failed")

with run_c2:
    if st.button("Run all tests"):
        try:
            with st.spinner("Running all verification tests..."):
                report = run_all_tests(
                    tests_dir=TESTS_DIR,
                    scheduler_url=client.base_url,
                    results_dir=RESULTS_DIR,
                    drain_timeout_seconds=float(drain_timeout),
                )
            (st.success if report.failed == 0 else st.warning)(
                f"{report.passed}/{report.total_verdicts} verdicts passed "
                f"({report.failed} failed)."
            )
        except Exception as e:
            st.error(f"Run-all failed: {e}")
            logging.exception("Verification run-all failed")


# ---------------------------------------------------------------------------
# Latest report
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Latest report")

report_json = RESULTS_DIR / "verification" / "report.json"
report_md = RESULTS_DIR / "verification" / "report.md"

if not report_json.exists():
    st.info("No verification report yet — run all tests to generate one.")
else:
    if st.button("Render markdown report"):
        try:
            from verification.report import render_report
            render_report(results_dir=RESULTS_DIR)
            st.success("Rendered report.md.")
        except Exception as e:
            st.error(f"Render failed: {e}")

    try:
        report_data = json.loads(report_json.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Cannot read report.json: {e}")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total verdicts", report_data.get("total_verdicts", 0))
        c2.metric("Passed", report_data.get("passed", 0))
        c3.metric("Failed", report_data.get("failed", 0))

        if report_md.exists():
            with st.expander("View full markdown", expanded=False):
                st.markdown(report_md.read_text(encoding="utf-8"))