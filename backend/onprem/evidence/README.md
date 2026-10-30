# Evidence

Current read-only native observation, identity alignment, and evidence interpretation.

| Module | Responsibility |
| --- | --- |
| `collector.py` | Assemble `NativeCollector`, subscriber inventory, and AMF, SMF, UPF, and RAN observations. |
| `daily_native_observation.py` | Cross-source daily observations with UE identities masked. |
| `free_ran_probe.py`, `free_ran_read_transport.py`, `probe.py` | TUN probes and read-only transport for FRU and the legacy runtime. |
| `kubernetes_pod_read.py` | Read-only Kubernetes adapter bound to Pod identity. |
| `ran_http.py` | `RanEvidenceClient` provides Pod HTTP reads with time and response-size limits for RAN observations and gNB UE context checks before cleanup. |
| `ambr.py` | Decode native AMF UE-AMBR and check identity and freshness. |
| `batch_receipts.py` | Traffic receipt references, summaries, and control-data consistency. |
| `scenario_receipts.py` | Pure validation of native result and release receipts shared by backend and browser acceptance tools; the production service does not import test scripts. |
| `ownership.py` | General read-only round and policy ownership reader. |
| `network_telemetry.py`, `throughput.py` | Parse kernel, socket, and interface telemetry and interpret throughput A/B results. |

Diagnostic and acceptance entry points that run commands or write artifacts live in `operations/diagnostics/` and `operations/validation/`. Fixed commissioning and capture tools live in `archive/legacy/evidence/`. An unavailable observation must remain unavailable; catalog data or historical values cannot be presented as current success.
