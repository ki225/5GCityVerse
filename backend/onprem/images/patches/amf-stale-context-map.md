# AMF stale-context map removal: focused runtime patch

## Scope and provenance

Apply only to free5gc AMF commit `02675d949f207a95379af458c7e80fabe802d728`, the `NFs/amf` gitlink of free5gc `v4.2.2`.
The single production change replaces `UePool.Delete(ue.Supi)` with `UePool.CompareAndDelete(ue.Supi, ue)`.
It is derived from [official PR #240](https://github.com/free5gc/amf/pull/240), head `e1502366592729e3de19db6a458b2fbeba494320`. That PR was **open and unmerged** when reviewed; this is not an official released fix or the complete PR.

This prevents a delayed old `AmfUe.Remove()` from deleting a different, newer object under the same SUPI. Current-owner removal retains its original behavior. No timer, NAS, RAN, consumer, subscriber, or policy behavior is otherwise changed.

`RemoveAmfUe(false)` invokes the purge helper, but this is NOT unconditional remote NF deletion: pinned `common/user_profile.go:115` returns when `ContextValid=false`; SDM unsubscribe additionally requires its subscription ID and UECM purge requires its registration flag. `handler.go:1073` sets ContextValid after successful UDM registration/data/subscription. The synthetic old SecurityMode fixture exercises that existing guard; historical generation-90 heap flags were not captured.

Not fixed or claimed: all duplicate-SUPI lifecycle races, already-valid old contexts with subscriber side effects, reused RAN IDs, multi-access/PDU migration, the initiating transport delay, or UERANSIM LIMITED-SERVICE. The last issue requires separate investigation/fix.

## Reproduction

Use a fresh source export, not the existing checkout (whose HEAD may differ), and Go's exact `go.mod` toolchain requirement. Commands below run only offline-source unit tests, never an AMF service or Kubernetes workload.

1. `git archive 02675d949f207a95379af458c7e80fabe802d728` from an official AMF repository into a fresh temporary directory.
2. `git apply --check /absolute/path/amf-stale-context-map.patch` there.
3. Apply only the patch's two added `cityverse_stale_remove_test.go` files using `git apply --include=internal/context/cityverse_stale_remove_test.go --include=internal/gmm/common/cityverse_stale_remove_test.go PATCH`.
4. `GOTOOLCHAIN=auto go test -count=1 -run TestCityverse -v ./internal/context ./internal/gmm/common`. Original source must fail the two newer-owner-retention tests. Normal-owner removal and existing invalid-context purge guard should pass.
5. Apply only production hunk: `git apply --include=internal/context/amf_ue.go PATCH`.
6. Repeat step 4, then `GOTOOLCHAIN=auto go test -race -count=1 -run TestCityverse ./internal/context ./internal/gmm/common`.

The common-package fixture uses a loopback `httptest` NF tripwire, a real old RAN context and a synthetic new Registered object. It invokes the actual `RemoveAmfUe(old, false)` path and checks no NF request, no replacement-state mutation, and normal old RAN removal. It is not a live SBI/PDU test. The separate invalid-context guard test leaves nonempty IDs/registration flags unchanged, proving that the existing guard does not reach downstream purge consumers.

## Validation status

Verified on Linux/WSL with Go 1.25.5 using a fresh exact-pin export `/tmp/cityverse-amf-cas.zH9MY1`:

- `git apply --check`: passed.
- Original source plus tests only: two expected failures (stale map removal in context and actual common.Remove path), two passes (normal owner removal and existing invalid-context purge guard), exit 1.
- One-line CAS patch plus identical tests: all four passed, exit 0.
- Identical focused tests with `-race`: both packages passed, exit 0 (context 1.171s, common 1.122s).

These are deterministic delayed-cleanup source-path fixtures; running them under the race detector does not establish arbitrary concurrent protocol correctness. No native runtime effectiveness or capacity claim is made.

## Deployment and rollback boundary

The local-only `amf-cas-image/build.py` build completed using pinned Go 1.25.5 and the original runtime image. Hadolint reported no findings, inherited runtime configuration (excluding labels) matched exactly, and a network-disabled `--help` smoke passed independently. The binary SHA256 is `5ae9b3a7859024d35978878433efc5c65d8e4baf754ebeff1d6e77f6c138886b`. Build evidence is retained in `.cache/kubeadm-lab/amf-cas-build-02`; the earlier module-download failure remains in build-01. Checkov and CVE scanning were not completed. Original runtime UID and health behavior were intentionally retained; this is not a hardening claim.

The patched image is now deployed on the lab core node as `docker.io/cityverse/amf@sha256:ece65f6aaf62851ae1ab0ba860b34565994c0f4f6a9a057d4eb9506eeb329cf3`. Native import independently verified the manifest, config and binary hashes. The original rollback image is `free5gc/amf:v4.2.2@sha256:8fc343349f7b0d55a257ecae328bff4e2d4b6800004ed932c128b2699a3860e7`; Docker's local config ID is not the distribution manifest digest.

Maintenance evidence in `.cache/kubeadm-lab/amf-cas-maintenance-01/postgate-readonly.json` records zero managed UE Pods, healthy AMF SBI/NRF, four restarted gNBs with established SCTP and successful NG Setup, unchanged other NF Pod UIDs, and only the AMF image changed across the five Deployment specifications. The first gate exited 1 because its SCTP parser did not normalize the primary-address `*` marker; only the corrected read-only gate was rerun, with no repeated maintenance. A transient VM clock discrepancy subsequently recovered without clock/NTP mutation; original timestamps are retained. Fresh clock and native-observation gates remain required before UE testing. This is infrastructure health evidence, not a successful post-patch 25-UE test.

Deployment requires a clean zero-owned-UE maintenance window and preserved Deployment image/arguments/config/replica counts as rollback input. Fixed N2 addressing prohibits simultaneous old/new AMF Pods. Pinned UERANSIM does not establish automatic gNB reconnection after AMF association shutdown, so any necessary gNB restart must be separately scoped, preserve its image/configuration and verify fresh SCTP/NG Setup. A source/build test pass does not establish native SBI/N2 health or 25-UE capacity.
