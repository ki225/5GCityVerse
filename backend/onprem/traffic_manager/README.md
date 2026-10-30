# Persistent traffic manager

One manager runs as PID 1 in each UE/N6 Pod's existing `traffic` container:

```text
NativeRoundBatch → persistent HTTP connections → Go manager → fixed Bash workers → iperf3
```

Identity, detached status and global iperf3 absence read procfs/state directly,
without subprocesses. Both the 1–49 UE batch and larger detached paths use the
manager. Other
actions retain the existing worker scripts, durable reservation, deadlines,
common release epoch, sealed results and exact process/socket cleanup.
Each Pod has an automatically generated management credential and downward-API
identity. The endpoint is for the trusted WSL lab network.

## Build

From the repository root, with Go installed:

```sh
python scripts/onprem/build_traffic_manager.py
docker build --network=none -t cityverse/traffic-manager:local backend/onprem/traffic_manager
```

The builder exports the exact Python worker strings into Go embed assets, then
builds a static Linux amd64 executable. No Go dependencies are downloaded. The
Dockerfile retains the original digest-pinned iperf3 base image.

### Single UDP listener build

The optional `udp-single-listener` Docker target patches ESnet iperf 3.21.
After accepting the final peer of a single, unidirectional UDP stream, upstream
binds a replacement listener that its server loop immediately closes. The patch
duplicates the existing connected descriptor for that close. It preserves the
stream socket, peer, cookie, buffers and accounting; multiple streams and
`--bidir` retain the upstream listener path. This reduces calls intercepted by
WSL mirrored networking without changing measurement duration or acceptance.

Build on Linux/WSL with gcc, make, autotools and static libc development files:

```sh
# Download the ESnet 3.21 tag archive separately. The builder checks its SHA256.
python scripts/onprem/build_traffic_manager.py --iperf3-source /private/path/iperf-3.21.tar.gz
docker build --network=none --target udp-single-listener -t cityverse/traffic-manager:udp-single-listener backend/onprem/traffic_manager
```

Source: `https://codeload.github.com/esnet/iperf/tar.gz/refs/tags/3.21`.
The source SHA256 is pinned in the builder. Build receipts, including patch and
binary hashes, are in `.tmp/traffic-manager-build/iperf3-build.json`. Generated
binaries are local build artifacts. The default Docker target keeps stock iperf3.
This optimization alone does not establish live AI E2E acceptance.

Import the image into the target K8s container runtime. Use its **manifest
digest**, not the Docker configuration/image ID, for deployment.

## Enable

Set `CITYVERSE_ONPREM_TRAFFIC_MANAGER_IMAGE` to the same digest-pinned image for
the backend and the infrastructure renderer. Start the existing WSL profile
with scenario, batch and detached traffic enabled. New UE Pods use the manager
automatically. Without the image setting, the legacy runtime is retained.

For existing N6 receivers, use `scripts/onprem/commission_traffic_manager.py`
with explicit image, kubeconfig, context, namespace and private output directory.
It plans by default; `--execute` switches the four traffic containers using
UID/resourceVersion CAS. It requires an idle completed round, zero UE allocation,
no UE Pods, no running receiver iperf3 and Recreate deployment strategy. Backups
contain generated credentials and must remain private. Reload the backend with
the same image setting after commissioning.

The backend reuses three connection lanes per exact Pod/container epoch: reads,
short writes and long batch workers. Long workers have separate admission slots.
Failed commands discard the connection and are not
automatically replayed or sent through exec as a fallback.

## Necessary verification

- `test/subsystems/onprem/test_onprem_traffic_manager_transport.py`: backend routing,
  connection reuse, no replay, identity fences and commissioning constraints.
- `main_test.go`: real HTTP keep-alive, authentication, identity rejection and
  direct status reads with external commands unavailable. Export scripts before
  running `go test` on Linux.
- `test/traffic_manager_smoke.py`: disposable Docker container, real UL/DL iperf3,
  sealed output and cleanup. Run as root on Linux/WSL; it configures only the
  disposable container's network namespace.
- `test/traffic_manager_k8s_smoke.py`: disposable K8s Pod, actual production HTTP
  adapter, connection reuse, foreign identity rejection and Pod removal.

Smoke tests do not establish UE TUN, AI or 50-UE E2E acceptance. iperf3 startup,
native observations and network performance remain separate measurements.
# iperf accounting time

The optional build pinned to ESnet 3.21 also applies `patches/iperf3-3.21-accounting-clock.patch`.
For a single, unidirectional UDP stream without omit time, JSON includes a `CLOCK_BOOTTIME` microsecond reading in `start.accounting_timestamp` around the native monotonic accounting start. The patch does not change sockets, timers, or counters.
The backend compares this start, local accounting duration, and collector boot clock within the same Linux boot to check a common interval and freshness. Wall-clock adjustments cannot change the validation result.
UTC is derived from the observation time for display only. Connection setup and process teardown are not treated as accounting start times.
Older binaries without this field use conservative estimation; malformed fields do not fall back to the old path and masquerade as valid. Build receipts also record the accounting patch SHA256.

