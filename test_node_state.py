"""Quick sanity test for NodeState."""
from shared.constants import HETEROGENEOUS_4NODE
from shared.models import NodeRuntimeStatus
from scheduler.node_state import NodeState

ns = NodeState()
for spec in HETEROGENEOUS_4NODE:
    ns.register_node(spec)

print("Registered nodes:", ns.list_node_ids())

# Initial views — all empty
views = ns.snapshot_views()
for v in views:
    print(f"  {v.spec.node_id}: proj_ram={v.projected_used_ram_kb}KB "
          f"proj_cores={v.projected_used_cores} reachable={v.status.reachable}")

# Reserve some tasks on edge3
print("\nReserving tasks on edge3:")
print("  task A (50KB, 1 core, 30u, 3.0s):",
      ns.reserve("edge3", "A", memory_kb=50, cpu_cores_required=1,
                 workload_units=30, expected_runtime_seconds=3.0))
print("  task B (100KB, 1 core, 60u, 6.0s):",
      ns.reserve("edge3", "B", memory_kb=100, cpu_cores_required=1,
                 workload_units=60, expected_runtime_seconds=6.0))
# Should fail — would exceed 185KB usable
print("  task C (50KB, 0 core, 10u, 1.0s):",
      ns.reserve("edge3", "C", memory_kb=50, cpu_cores_required=0,
                 workload_units=10, expected_runtime_seconds=1.0))

view3 = ns.snapshot_view("edge3")
print(f"\nedge3 after reservations: proj_ram={view3.projected_used_ram_kb}KB "
      f"proj_cores={view3.projected_used_cores} "
      f"completion_time={view3.projected_completion_time:.2f}")
print(f"  in_flight_count={ns.in_flight_count('edge3')} "
      f"in_flight_workload={ns.in_flight_workload('edge3')}u")

# Reconcile with a fresh status
print("\nReconciling edge3 with fresh status:")
ns.reconcile("edge3", NodeRuntimeStatus(
    reachable=True, used_ram_kb=150, available_ram_kb=35,
    is_memory_saturated=False, cpu_cores_in_use=2, cpu_utilization_pct=100.0,
    is_cpu_saturated=True, active_task_count=2,
))
view3 = ns.snapshot_view("edge3")
print(f"  status.reachable={view3.status.reachable}")
print(f"  status.is_cpu_saturated={view3.status.is_cpu_saturated}")
print(f"  projected_used_ram_kb still={view3.projected_used_ram_kb}KB (unchanged)")

# Release task A
print("\nReleasing task A:", ns.release("edge3", "A", workload_units=30))
view3 = ns.snapshot_view("edge3")
print(f"  edge3 after release: proj_ram={view3.projected_used_ram_kb}KB "
      f"proj_cores={view3.projected_used_cores}")

# Trial reset
print("\nReset:")
ns.reset_all()
view3 = ns.snapshot_view("edge3")
print(f"  edge3 after reset: proj_ram={view3.projected_used_ram_kb}KB "
      f"proj_cores={view3.projected_used_cores} "
      f"in_flight={ns.in_flight_count('edge3')}")