# Scenarios

This module retains three responsibilities for a scenario: **accept demand, coordinate execution, and present status**.

| File | Responsibility |
| --- | --- |
| `contracts.py` | Scenario-to-slice mapping, request validation, allocation, and round states. Public requests allow only `ai` and 1–100 UEs; the internal journal envelope can still read historical data. |
| `coordinator.py` | Coordinate startup, cancellation, recovery, measurement, adaptive AI calls, results, and cleanup for one round. |
| `presentation.py` | Project status and traffic participant counts for frontend full and summary views; it performs no network operation. |

```text
API → ScenarioService → Coordinator
                         ├─ storage.RoundLedger: persistence, idempotency, conflicts, and recovery state
                         ├─ runtime.NativeRoundResources: UE, Pod, and native evidence
                         ├─ traffic: traffic planning, batch execution, and measurement
                         └─ agents: Planner, Executor, and Validator
```

## Functionality moved to other modules

| New location | Purpose |
| --- | --- |
| `storage/rounds.py` | Production round repository, batch result publication, maintenance markers, and transaction locks. |
| `runtime/round_resources.py` and `runtime/errors.py` | Native resource adapters, window and ownership checks, and shared errors. |
| `traffic/planning.py` and `traffic/workload.py` | Traffic and SUPI shard planning and workload metadata written to the journal. |
| `traffic/batch.py` | Batch preparation, synchronized start, collection, and cleanup. |
| `traffic/protocol.py` and `traffic/worker.py` | Real traffic work contract, worker shell protocol, and measurement validation. |
| `traffic/transports/` | Pod manager HTTP and CRI read, release, and postflight adapters with stage-specific limits. |
| `traffic/tuning.py` | Pre-execution socket checks and tuning saved in the reservation. |
| `capacity/` | Calibration admission, receipts, and verified grants. The move added no capacity threshold or capacity test. |
| `evidence/throughput.py` | Offline throughput A/B evidence interpretation. |

## Legacy code

`archive/legacy/scenarios/` contains:

- Reducers, producers, and journals for old fixed commissioning.
- Incident-specific ledger methods for generations 13, 17, and 34.
- Legacy N5, static, and 500-UE frozen-oracle policy execution branches.

Operational compatibility entry points remain. Production AI decisions for 1–100 UEs still use the adaptive runtime. Existing journal, Pod and UE identity checks, deadlines, traffic worker contracts, and cleanup evidence are not relaxed by the move.

Routine verification runs only the API regression:

```sh
python test/api_regression.py --output .tmp/scenario-api-regression-new
```

The API suite covers consecutive scenarios and failure cleanup using the real coordinator and ledger with external adapter doubles. It does not prove live UE, LLM, or QER effects; this change did not reload the live backend.
