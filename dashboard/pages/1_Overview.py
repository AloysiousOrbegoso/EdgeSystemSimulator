"""Overview page — live system status, current algorithm, queue, node bars."""

from __future__ import annotations

import streamlit as st

from dashboard.api_client import SchedulerClient


st.title("Overview")

client: SchedulerClient = st.session_state.get("client") or SchedulerClient()


# ---------------------------------------------------------------------------
# Top-line metrics
# ---------------------------------------------------------------------------

try:
    state = client.system_state()
except Exception as e:
    st.error(f"Could not reach scheduler: {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Active algorithm", state.get("active_algorithm", "?"))
c2.metric("Queue depth", state.get("queue_size", 0))
c3.metric("Nodes", state.get("node_count", 0))
trial_active = state.get("trial_active", False)
c4.metric("Trial active", "yes" if trial_active else "no")

if trial_active:
    st.info(f"Active trial run_id: `{state.get('trial_run_id') or '-'}`")

st.divider()


# ---------------------------------------------------------------------------
# Per-node live bars
# ---------------------------------------------------------------------------

st.subheader("Node memory + CPU")

try:
    nodes = client.nodes()
except Exception as e:
    st.error(f"Could not fetch nodes: {e}")
    nodes = []

if not nodes:
    st.info("No nodes registered yet.")
else:
    for node in nodes:
        spec = node.get("spec", {})
        status = node.get("status", {})
        node_id = spec.get("node_id", "?")
        total_ram = int(spec.get("total_ram_kb", 0) or 0)
        usable_ram = int(spec.get("usable_ram_kb", 0) or 0)
        used_ram = int(status.get("used_ram_kb", 0) or 0)
        cpu_cores = int(spec.get("cpu_cores", 0) or 0)
        cpu_in_use = int(status.get("cpu_cores_in_use", 0) or 0)
        is_mem_sat = bool(status.get("is_memory_saturated", False))
        is_cpu_sat = bool(status.get("is_cpu_saturated", False))
        reachable = bool(status.get("reachable", False))

        with st.container():
            badges = []
            if not reachable:
                badges.append(":red[unreachable]")
            if is_mem_sat:
                badges.append(":orange[mem saturated]")
            if is_cpu_sat:
                badges.append(":orange[cpu saturated]")
            badge_text = " ".join(badges) if badges else ":green[healthy]"
            st.markdown(f"**`{node_id}`** {badge_text}")

            mc1, mc2 = st.columns(2)
            with mc1:
                st.caption(
                    f"Memory: {used_ram} / {total_ram} KB "
                    f"(usable cap {usable_ram})"
                )
                if total_ram > 0:
                    st.progress(min(used_ram / total_ram, 1.0))
            with mc2:
                st.caption(f"CPU: {cpu_in_use} / {cpu_cores} cores")
                if cpu_cores > 0:
                    st.progress(min(cpu_in_use / cpu_cores, 1.0))

            st.caption(
                f"Active tasks: {status.get('active_task_count', 0)} | "
                f"Accepted: {', '.join(spec.get('accepted_classes', []))}"
            )

st.divider()


# ---------------------------------------------------------------------------
# Algorithm switcher
# ---------------------------------------------------------------------------

st.subheader("Algorithm")

try:
    algos = client.algorithms()
    available = algos.get("available", [])
    active = algos.get("active", "")

    selected = st.selectbox(
        "Active algorithm", available, index=available.index(active) if active in available else 0
    )
    if st.button("Switch") and selected != active:
        client.set_algorithm(selected)
        st.success(f"Switched to `{selected}`. Refresh to see effect.")
except Exception as e:
    st.warning(f"Cannot list algorithms: {e}")


# ---------------------------------------------------------------------------
# Quick actions
# ---------------------------------------------------------------------------

st.subheader("Quick actions")
col_a, col_b = st.columns(2)
with col_a:
    if st.button("Reset trial state", help="Clears queue, in-flight tasks, and edge node state."):
        try:
            r = client.trial_reset()
            (st.success if r.get("ok") else st.warning)(r.get("detail", "reset complete"))
        except Exception as e:
            st.error(f"Reset failed: {e}")
with col_b:
    if st.button("Refresh"):
        st.rerun()