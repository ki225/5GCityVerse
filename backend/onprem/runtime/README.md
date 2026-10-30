# Runtime

This directory contains only code needed by the current native execution path.

| File | Responsibility |
| --- | --- |
| `ue.py` | UE intent and SQLite store, evidence validation, controller, and reconcile lifecycle. |
| `kubeadm.py` | Kubernetes UE driver with target and ownership checks. |
| `free_ran_ue.py` | FRU Pod manifests and driver. |
| `device_sessions.py` | Restricted native device compiler and commissioned profile; the production AI path selects one FreeRAN PDU. |
| `round_resources.py` | Native UE, Pod, and receiver resource adapter for scenarios, with identity and window validation and release evidence; called by the coordinator. |
| `bootstrap.py` | Check and start city infrastructure and the UE baseline. |
| `traffic_manager.py` | Container and image configuration for the resident Pod traffic manager. |
| `topology.py` | Shared deployment defaults for slices, IP pools, IMSI starting points, and receiver addresses; does not contain measured results or scenario population. |
| `errors.py` | Shared error types independent of Kubernetes or legacy implementations. |

General utilities are separate:

- [`storage/readonly.py`](../storage/readonly.py): Read-only access to the existing UE ledger and generation decoding.
- [`storage/locks.py`](../storage/locks.py): Cross-process exclusion locks.
- [`storage/recovery_guard.py`](../storage/recovery_guard.py): Recognize real journals and sidecars; ordinary generations do not enter incident-specific branches.
- [`evidence/ownership.py`](../evidence/ownership.py): Read-only round and policy ownership evidence.

The former `gen263_*.py`, `exceptional_recovery.py`, and `local_kubernetes.py` moved to [`archive/legacy/runtime/`](../../../archive/legacy/runtime). Generation-specific store methods for generations 11, 17, 27, 30, and 263 moved to `generation_store.py` there.

Normal operations can progress beyond generation 263. A generation is a sequence number, not proof of a historical incident. Existing incomplete recovery records still block operations and are not ignored because files moved.

Deployment defaults and image digests remain. This split did not change network topology or UE traffic settings. Verification ran only `test/api/`; the live backend was not reloaded.

Production AI composition uses `kubeadm.configured_driver()` to read the same digest-pinned configuration as IntentService. It selects `FreeRanKubeadmDriver` with matching `ue_device_profile` and `ue_device_image`, rejecting vendor or image mismatches early. Scenarios, pre-traffic checks, and population use this controller without a cross-vendor fallback. See [the formal FreeRAN runtime composition](../intents/README.md#formal-freeran-runtime-composition) for configuration and verification limits.

## FreeRAN native acceptance tools

- `scripts/onprem/commission_fru_device_profile.py` checks the city gNB and pinned image source.
- `scripts/onprem/commission_free_ran_profile.py` builds a time-limited launch profile from an archived native SMF census, actual UE Pods, and evidence before and after measurement. PDU IDs come from native readback.
- `scripts/onprem/free_ran_device_smoke.py` uses an archived job to verify single-UE, single-PDU reserve, admission, and consume flows, UL/DL, normal shutdown, and two reconnection rounds. Start it with `start_native_backend.py --free-ran-device-smoke ... --free-ran-device-smoke-sha256 ...`. It keeps explicit mutation, traffic, and SMF inventory enablement and the OS state lock.
- When a WSL host can route an approved ClusterIP, use `--native-service-forwards` for native service reads instead of relying on long-running `kubectl port-forward` streams. Native zero-state and source-availability checks must pass before creating a UE.
- When active N6 uses traffic-manager, `CITYVERSE_ONPREM_TRAFFIC_MANAGER_IMAGE` in the acceptance flow must use the same pinned digest. The native receiver still requires ownership and Pod UID checks.
- Recovery mode requires an archived reservation and installation from the same owner. `failed-native-zero` mode also requires two complete native zero-state observations, canceled admission, the same SMF boot, no UE Pod, a database backup, and CAS auditing. Recovery mode must not start a new UE or register a new nonce.

See [FreeRAN V2 live acceptance](../../../test/RESULTS-2026-10-29-free-ran-v2-live.md) for progress and evidence. Acceptance for parent rebuild, population commissioning, and live AI QER, placement, and SLA is recorded separately.
