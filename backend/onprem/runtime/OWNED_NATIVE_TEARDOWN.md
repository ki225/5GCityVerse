# Owned native teardown integration

`NativeRoundResources.request_owned_drain` can use the existing sole
`UEController` writer to perform an accepted-v2 normal stop. The configured path
pins the original generation, allocation hash, exact current device/session
identities, all protected NF process identities and native SMF/PFCP/PCF/AMF
baseline **before** the zero-generation request or NAS command. Its healthy-stop
SQLite transaction writes both the zero generation and its typed receipt.
The ordinary mutation blocker is retained; this path does not bypass quarantine.

The driver sends one journalled `deregister switch-off` per pinned physical UE.
An unknown ACK authorizes readback only. The cohort driver advances other UEs
while one awaits terminal state. Only after every pinned UE is independently
terminal with an idle traffic sidecar can the standard-SBI fallback release
exact orphan SMF contexts, then PCF children, then the pinned AMF context.
The adapter never deletes Pods. Existing post-NAS evidence, UID-fenced Pod
retirement, RAN absence, traffic absence and PCF release coverage remain required.

Restart reads the immutable pre-stop journal and the exact generation receipt.
It does not recapture a post-NAS baseline, infer `original + 1`, or repeat an
uncertain stop/NAS/SBI dispatch. Exact previously observed terminal process proof
can survive a subsequently absent Pod; unknown absence or a replacement blocks.
Only the complete native round release proof closes the current stop journal.
An uncertain SMF release fences its own PDU rather than all other pinned PDUs.
The remaining contexts can progress only while complete native SMF and independent
UPF counters agree on the known remainder with no residuals. Original unknown
commands settle only after complete SMF/kernel absence; no command is replayed.

## Trusted server configuration

At application startup set both:

* `CITYVERSE_ONPREM_NATIVE_TEARDOWN_CONFIG_FILE`: absolute private JSON file.
* `CITYVERSE_ONPREM_NATIVE_TEARDOWN_CONFIG_SHA256`: SHA256 of its exact bytes.

No configuration means the new path is unconfigured. `application/providers.py`
loads the pinned file and installs a lazy factory on the real resources/controller
chain. It resolves `context.intent_service.repository`, `context.round_ledger`,
the existing `NativeCollector` and the same UE driver at drain time. No second
controller, public URL argument, lock or credential fallback is introduced.

The JSON object has exactly these keys:

```json
{
  "schema": "owned-native-teardown-configuration/v1",
  "native": {
    "journal_file": "/private/owned-teardown.sqlite3",
    "smf_sbi": {"origin": "http://127.0.0.1:18081", "auth_mode": "bearer", "token_file": "/private/smf-standard-sbi-token"},
    "pcf_sbi": {"origin": "http://127.0.0.1:18082", "auth_mode": "bearer", "token_file": "/private/pcf-standard-sbi-token"},
    "amf_sbi": {"origin": "http://127.0.0.1:18083", "auth_mode": "bearer", "token_file": "/private/amf-standard-sbi-token"}
  },
  "smf_read": {"origin": "http://127.0.0.1:18081", "token_file": "/private/smf-qer-token"},
  "amf_read": {"origin": "http://127.0.0.1:18083", "auth_mode": "bearer", "token_file": "/private/amf-oam-token"},
  "pcf_read": {"origin": "http://127.0.0.1:18082", "auth_mode": "bearer", "token_file": "/private/pcf-oam-token"},
  "upfs": [],
  "protected_nfs": []
}
```

The loopback ports above illustrate the shape, not commissioned endpoints. Fill
`upfs` with the actual four commissioned `UpfReadBinding` objects, each with
`logical_target`, `census_target_id`, `native_upf_id`, `node_ip`, `evidence_origin`,
`current_session_target_id`. These must match the existing native collector URLs
and actual SMF UUID/N4 census; there is no name alias or guessed UUID. Fill
`protected_nfs` with `ProtectedNf` objects: `role`, `pod_name`, `container_name`,
`process_pid`. The complete live namespace outside the exact owned UE Pod UIDs
must equal these selectors. Selectors for only seven core roles cannot stand in
for a real 23-Pod namespace. POSIX files and credential references must be private.
Read and standard-SBI credential references are separate and are never logged.

## Current supported boundary

This integration requires an accepted, current v2 snapshot with one original,
non-reused `ue_allocation` receipt, restored policy ownership, closed admissions,
an exact running release-operation resource lease, and the pinned configuration.
Setup failures before accepted v2 are explicitly unsupported. The legacy path
remains unchanged without this configuration. Exceptional already-native-zero
quarantine recovery remains its separate strict typed port.

The currently supported topology is the existing four commissioned UPFs. A later
explicit binding-set expansion must retain complete census and exact UUID/epoch
proof; a fifth UPF cannot be aliased onto an existing logical target. These source
tests use explicit native HTTP/Kubernetes doubles, not a live commissioning claim.
The default protected-process reader requires `sh`, `cat`, `awk` and `readlink`
inside each selected container. A distroless container fails that mode; it never
automatically falls back or substitutes Pod age or another container's PID.

## Explicit same-node SMF process reader

For the distroless SMF only, set its exact protected selector to
`"process_identity": "host_cri"` (role/container `smf`, process PID `1`) and add
this optional top-level object to the same SHA256-pinned private configuration:

```json
{
  "host_cri": {
    "node_name": "ACTUAL_COMMISSIONED_NODE_NAME",
    "node_uid": "ACTUAL_COMMISSIONED_NODE_UID",
    "runtime_version": "containerd://2.2.1",
    "runtime_endpoint": "unix:///run/containerd/containerd.sock",
    "crictl_path": "/usr/bin/crictl",
    "executable": "/free5gc/smf"
  }
}
```

Replace the Node placeholders with independent native evidence before sealing
the file. The only allowed local runtime/tool/executable paths are those shown.
No model or request supplies a shell command, path, socket or container ID.
The existing explicit-context Kubernetes TLS identity reads the exact Node;
UID, runtime version and native boot ID must match the host process. Exact
PodUID/namespace/name/container labels, runtime `CONTAINER_RUNNING`, container ID,
host PID, `NSpid=(hostPID,1)`, `/proc` start ticks, PID-namespace inode, executable
and kernel boot are checked at opening and closing. Kubernetes Pod/container
status is rechecked too. Only selected identity fields and original opening /
closing timestamps escape the parser; raw inspect/environment is never logged
or persisted. The spawned inspect child has bounded time/output and is reaped.

The other protected NFs retain their container process readers. All process
reads use at most four workers, drain every future and share an eight-second
whole-census budget including original opening and closing Pod membership.
Both Pod-list reads transmit that same original deadline to the existing native
metadata reader. A driver without this deadline-aware port is unsupported here;
the convenience method's new five-second timeout is never used as a fallback.
The original observation clock is retained and the existing ten-second native
freshness limit is unchanged. Source tests demonstrate these contracts with
explicit doubles; actual whole-23-NF timing and native commissioning remain
separate acceptance steps. Repeated process proofs during staged teardown may
also expose the existing drain-permit freshness limit; issued timestamps are
never refreshed by this reader.
