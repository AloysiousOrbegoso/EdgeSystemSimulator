"""Nodes page — per-node spec/state details and registration form."""

from __future__ import annotations

import json

import streamlit as st

from dashboard.api_client import SchedulerClient


st.title("Nodes")

client: SchedulerClient = st.session_state.get("client") or SchedulerClient()

try:
    nodes = client.nodes()
except Exception as e:
    st.error(f"Could not fetch nodes: {e}")
    st.stop()

if not nodes:
    st.info("No nodes registered.")
else:
    for node in nodes:
        spec = node.get("spec", {})
        status = node.get("status", {})
        node_id = spec.get("node_id", "?")
        with st.expander(f"`{node_id}`", expanded=True):
            sc1, sc2 = st.columns(2)
            with sc1:
                st.markdown("**Spec**")
                st.json({
                    "url": spec.get("url"),
                    "total_ram_kb": spec.get("total_ram_kb"),
                    "usable_ram_kb": spec.get("usable_ram_kb"),
                    "flash_kb": spec.get("flash_kb"),
                    "saturation_threshold_kb": spec.get("saturation_threshold_kb"),
                    "cpu_cores": spec.get("cpu_cores"),
                    "accepted_classes": spec.get("accepted_classes"),
                    "is_active": spec.get("is_active"),
                })
            with sc2:
                st.markdown("**Live status**")
                st.json(status)
                st.caption(
                    f"Projected (mirror): used_ram_kb="
                    f"{node.get('projected_used_ram_kb', 0)}, "
                    f"used_cores={node.get('projected_used_cores', 0)}, "
                    f"completion_time="
                    f"{node.get('projected_completion_time', 0):.2f}"
                )

st.divider()


# ---------------------------------------------------------------------------
# Register / replace a node
# ---------------------------------------------------------------------------

st.subheader("Register or replace a node")
st.caption(
    "Registering a node with an existing node_id replaces it (and clears its "
    "reservations). Useful for swapping between heterogeneous and "
    "homogeneous configurations."
)

with st.form("register_node"):
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        node_id = st.text_input("node_id", value="")
        url = st.text_input("url", value="http://edge1:5000")
        cpu_cores = st.number_input("cpu_cores", min_value=1, value=1)
    with fc2:
        total_ram_kb = st.number_input("total_ram_kb", min_value=1, value=128)
        usable_ram_kb = st.number_input("usable_ram_kb", min_value=1, value=90)
        flash_kb = st.number_input("flash_kb", min_value=1, value=1024)
    with fc3:
        saturation_threshold_kb = st.number_input(
            "saturation_threshold_kb", min_value=1, value=102
        )
        accepted = st.multiselect(
            "accepted_classes",
            ["lightweight", "moderate", "heavy", "very_heavy"],
            default=["lightweight", "moderate", "heavy"],
        )
        is_active = st.checkbox("is_active", value=True)

    submitted = st.form_submit_button("Register node")
    if submitted:
        if not node_id or not accepted:
            st.error("node_id and at least one accepted_class are required.")
        else:
            payload = {
                "node_id": node_id,
                "url": url,
                "total_ram_kb": int(total_ram_kb),
                "usable_ram_kb": int(usable_ram_kb),
                "flash_kb": int(flash_kb),
                "saturation_threshold_kb": int(saturation_threshold_kb),
                "cpu_cores": int(cpu_cores),
                "accepted_classes": accepted,
                "is_active": bool(is_active),
            }
            try:
                resp = client.register_node(payload)
                st.success(f"Registered `{node_id}`: {resp.get('detail', '')}")
            except Exception as e:
                st.error(f"Registration failed: {e}")