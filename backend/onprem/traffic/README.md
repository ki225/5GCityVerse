# Traffic

Traffic control and measurement adapters. `scenarios/coordinator.py` manages scenario lifecycles, and `runtime/round_resources.py` provides native resources.

| File or directory | Responsibility |
| --- | --- |
| `planning.py` | Build traffic and shard plans from requests and per-UE workloads. |
| `workload.py` | Bind measurements, normal and constrained acceptance requirements, and actual offered load. |
| `protocol.py` | Immutable reservations, identity and deadlines, measurement receipts, and worker contracts. |
| `batch.py` | `NativeRoundBatch` prepares work in parallel, starts it together, collects results, and cleans up. |
| `worker.py` | In-Pod iperf3 execution contract and programs; the resident Go manager uses the corresponding scripts. |
| `capability.py` | Validate native iperf capabilities, sender and receiver windows, and summaries. |
| `tuning.py` | Configurable socket and offload execution settings with host readback checks. |
| `transports/` | Control transport adapters for the resident manager and CRI read, release, and postflight operations. |

This move did not change worker program strings or traffic parameters. `traffic_manager/scripts/` remains a Go embed build input. Historical fixed commissioning is loaded only through an explicit compatibility profile branch.

Manager HTTP connections are reused. After 60 seconds idle, the client rebuilds the connection before the next request to avoid reusing a socket closed by the server's 120-second idle timeout. Connection failures after sending are not retried automatically, preserving protection when a write outcome is unknown. A real HTTP socket case in `test/api/test_traffic_manager_connection_api.py` verifies this behavior.
