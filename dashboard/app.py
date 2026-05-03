"""
Streamlit entry point for the testbed dashboard.

The dashboard is a multi-page Streamlit app. Each page lives under
dashboard/pages/ and is auto-discovered by Streamlit's pages convention.
This file is just the landing screen plus the sidebar context.

Run from the project root:
    streamlit run dashboard/app.py

Connection target:
    Reads SCHEDULER_BASE_URL from the environment, default
    http://localhost:8000. Override via the sidebar at runtime if you
    want to point at a different scheduler.
"""

from __future__ import annotations

import os

import streamlit as st

from dashboard.api_client import DEFAULT_BASE_URL, SchedulerClient


st.set_page_config(
    page_title="Edge Scheduling Testbed",
    page_icon="🧮",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Sidebar — common across pages
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("Testbed")

    # Persist the scheduler URL across reruns.
    if "scheduler_url" not in st.session_state:
        st.session_state.scheduler_url = DEFAULT_BASE_URL

    st.session_state.scheduler_url = st.text_input(
        "Scheduler URL",
        value=st.session_state.scheduler_url,
        help="Base URL of the scheduler service. Default reads from the "
             "SCHEDULER_BASE_URL env var.",
    )

    client = SchedulerClient(base_url=st.session_state.scheduler_url)
    st.session_state.client = client

    # Health badge in the sidebar.
    try:
        client.health()
        st.success("scheduler reachable")
    except Exception as e:
        st.error(f"scheduler unreachable: {type(e).__name__}")

    st.divider()
    st.caption("Pages:")
    st.caption("• Overview — live system status")
    st.caption("• Nodes — per-node config + state")
    st.caption("• Scenarios — performance runs (CSV)")
    st.caption("• Verification — pass/fail tests")
    st.caption("• Results — Friedman + plots")


# ---------------------------------------------------------------------------
# Landing content
# ---------------------------------------------------------------------------

st.title("🧮 Edge Scheduling Testbed")
st.markdown(
    """
    Interactive control plane for the **Optimized Adaptive Min-Min Scheduling**
    thesis testbed.

    Use the sidebar to navigate to:

    - **Overview** — algorithm in use, queue depth, live node bars.
    - **Nodes** — inspect each edge node's config and runtime state.
    - **Scenarios** — run performance scenarios, view past results.
    - **Verification** — run the Alpha / Beta / White-Box / Black-Box test
      suite and read the pass/fail report.
    - **Results** — generate the thesis-ready summary report (Friedman
      tests + time-series plots) from `results/runs.csv`.

    All pages talk to the scheduler service via HTTP; nothing is computed
    in this Streamlit process. If the sidebar shows "scheduler unreachable",
    bring up the stack first (`docker compose up`).
    """
)