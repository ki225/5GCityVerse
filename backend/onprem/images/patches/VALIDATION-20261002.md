# Controlled WSL restart and free-ran-ue live validation — 2026-10-02

Context: `cityverse-kubeadm-lab`; namespace: `free5gc-onprem`.

After the approved Windows/WSL restart, the three KVM nodes and API relay were
recovered. Stale VM PID files were moved to a scoped recovery directory. Multus
had hit its 50 MiB limit during CNI ADD; its limit was raised to 256 MiB and
its DaemonSet became 3/3 Ready. This is a lab resource correction, not a 5GC
protocol change.

Persistent NRF returned HTTP 200 without `Location` when AMF/AUSF updated
their stable NF profiles. Both NFs incorrectly replaced the configured ID
with empty string, causing UDM to reject `AmfInstanceId`/`ausfInstanceId`.
The AMF and AUSF source patches preserve the existing ID on HTTP 200.

Live load exposed intermittent HTTP/2 connection-establishment failures in
UDM→NRF discovery, UDM→UDR auth-status PUT, SMF→UDM SDM GET, and AMF→NRF
discovery. Each fix retries only that transport error, at most twice, with
bounded short delays. No semantic HTTP response is retried. A separate AMF
race started T3560 after sending NAS; a fast local UE could respond before
the timer was assigned, leaving an orphan timer that later deleted a healthy
UE context. Authentication and Security Mode timers now arm before downlink.

Final immutable image digests:

| NF | Digest |
| --- | --- |
| AMF | `docker.io/cityverse/amf@sha256:2cbb81d389c899b24690ba3f8de175a7a5f4e7160a163c6b109dfcbf1efaefd6` |
| AUSF | `docker.io/cityverse/ausf@sha256:91e0a48f7ed5f8764533b892b552d372e7e7b987fc9d7c52749c64d1e5252777` |
| UDM | `docker.io/cityverse/udm@sha256:08e37bd4f030e91d88a54aaba66f97cb5cf88ca87a39af6ce6778aba15bf5397` |
| SMF | `docker.io/cityverse/free5gc-smf@sha256:9f221da946c513842d68e0bfb32effcfc9159a6d99a60aa74d847ff0640c398a` |

These digests were imported into this lab control-plane node's containerd and
are **not evidence of a public registry push**. A new node or rebuilt VM needs
the retained OCI archives or a separately authorized registry publication.

The final deterministic Helm render used upstream revision
`0d0b4b392bbb1b099acb9a1b37c39e0647ff6d4c` and yielded 95 resources,
21 Deployments, SHA-256
`78ec1da527600dac89d558a0878dcd17e4c7a920b29ce78856687316b3d8f5a8`.
The 94 mutable resources were server-dry-run and applied. The already
successful `cityverse-subscriber-provision` Job (1 succeeded) was excluded
because Kubernetes forbids updating its Pod template; its existing image and
provisioned subscribers were retained. All 21 Deployments rolled out 1/1.
All three nodes were Ready; NRF reported seven distinct expected stable NF IDs.

Final free-ran-ue live test sequence passed:

| Stage | UE registration + PDU | N6 probes | Normal deregistration / cleanup |
| --- | --- | --- | --- |
| 100 | 100/100 in 29.094 s | 100/100 | AMF 0, SMF 0, all UPFs 0 |
| 250 | 250/250 in 57.422 s | 250/250 | AMF 0, SMF 0, all UPFs 0 |
| 500 | 500/500 in 104.734 s | 500/500 | AMF 0, SMF 0, all UPFs 0 |

Each stage held the UEs active during validation. Starts were serialized
within each of four slices (four slice workers in parallel), so this proves
500 simultaneous active UE/PDU sessions in this lab configuration, not 500
registration attempts launched at one instant. The benchmark removed all
owned UE/gNB Pods, Services, and ConfigMaps. After the final apply, an
additional 10-UE live smoke passed registration, PDU, N6 and normal cleanup
with zero AMF/SMF/UPF state.

Evidence/build receipts and harness remain under
`.cache/free-ran-ue-preflight/diagnostics/`; the final source patches are in
this directory. No high-privilege diagnostic Pod was created.
