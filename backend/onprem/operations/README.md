# Operations

Explicitly invoked operational tools and current recovery adapters. Importing a module does not execute a command; side-effecting work must be started by its CLI or a higher-level coordinator.

| Directory | Contents |
| --- | --- |
| `provisioning/` | Subscriber provisioning with WebConsole authentication and runtime SQN protection. |
| `diagnostics/` | Network telemetry plans and bundles, native session readback, and owned-resource cleanup evidence. |
| `validation/` | Functional traffic production and acceptance, plus network lifecycle receipts; delegates to current collectors and traffic adapters. |
| `recovery/` | `retired_epoch.py` and `retired_epoch_worker.py`: the general retired process epoch cleanup protocol used by current batches. |

Run commands from the project root:

```sh
python -m backend.onprem.operations.diagnostics.network_telemetry_cli --help
python -m backend.onprem.operations.diagnostics.native_session_readback --help
python -m backend.onprem.operations.validation.functional_traffic_acceptance --help
python -m backend.onprem.operations.provisioning.subscribers --help
```

Incident-specific generation, missing-Pod, and WSL tools moved to `archive/legacy/operations/recovery/` and are not part of normal backend startup. An existing explicit startup recovery intent still loads its legacy adapter on demand and is not ignored because of the move.
