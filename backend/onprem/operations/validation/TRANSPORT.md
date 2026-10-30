# Kernel distance and 5G path acceptance

Infrastructure owns the Helm plan and the small `n3-delay.sh` executor. This
backend directory owns Kubernetes lookup, Pod/sandbox fences, UE lifecycle,
capture and result validation. No Python is embedded into the infra chart.

## Run in the local WSL lab

Requires root, an explicit onprem context, the existing subscriber Secret,
`crictl`, `tc`, `tcpdump`, `tshark`, `jq`, PyYAML and the three tc kernel modules.
All target Pods must be on this host (node bootID check). Every gNB must have
an empty native `ranUeList`; existing UE Pods or non-default N3 qdiscs stop the run.
The local acceptance uses the existing lab's `gnb-evidence` sidecar to check
native contexts; that sidecar is a backend diagnostic and is not part of Helm IaC.

Export `n3-delay.json` from the rendered `cityverse-transport-delay` ConfigMap
(the `data["n3-delay.json"]` string) or the installed ConfigMap:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" -n free5gc-onprem \
  get configmap cityverse-transport-delay -o json | jq -r '.data["n3-delay.json"]' > n3-delay.json
python3 -m backend.onprem.operations.validation.city_smoke \
  --kubeconfig /etc/kubernetes/admin.conf --context kubernetes-admin@cityverse-wsl \
  --site-values /path/to/filled-wsl-values.yaml \
  --scenario concert --supi imsi-208930100000249 \
  --transport-plan n3-delay.json --output /path/to/new-output-directory
```

The runner applies the city plan, starts captures **before** creating its UE,
verifies PDU IP and TUN route, probes 25 ICMP requests per latency budget,
returns to the actual placement, sends UDP traffic, normally deregisters/deletes
the owned UE and restores the original `noqueue` state. `--traffic-mbps` overrides
the offered functional load. `--leave-city-delay` retains the actual city plan
after traffic and protocol acceptance; it never leaves the temporary counterfactual budget.
The runner deliberately requires clean interfaces on entry, even for a repeat.
Remove the module's own `c170:` qdiscs with `n3-delay.sh remove` before repeating.
Use `--transport-city-only` to validate the actual site without the four-budget
comparison, for example `--scenario industrial-central --supi imsi-208930100000749`.

This is an isolated **functional** test, not a calibrated physical benchmark.
Ping RTT is guest-reported; kernel timers, host load and the existing simulated
radio path add overhead. Edge / Distributed / Central budget comparison uses
the **same** UE and UPF, so it is not evidence of NF relocation. The normal city
placement remains defined in `city.slices.<slice>.site`.

## Protocol evidence

| Interface | Evidence |
| --- | --- |
| N1 | NAS-PDU bytes carried by N2 InitialUEMessage / NAS transport; UE registration and session procedures |
| N2 | SCTP 38412 with decoded NGAP InitialUEMessage, downlink/uplink NAS, PDU Session Resource Setup |
| N3 | UDP 2152 GTP-U in both directions; inner PDU IP; TEIDs joined to NGAP and PFCP |
| N4 | SMF–UPF PFCP 8805 session establishment request/response and subsequent updates/deletion |
| N6 | Decapsulated ICMP request/reply; same identifiers/sequences as inner N3 ICMP, plus receiver UDP bytes |
| N9 | Not configured; each slice currently has one direct gNB–UPF path |

N1 / N2 / N4 are control plane relationships. User payload travels
`UE TUN → simulated UE–gNB channel → gNB → N3 → UPF → N6 → DN`; it does not
traverse every numbered reference point sequentially. The UE/gNB radio segment
is the existing free-ran-ue simulator, not real RF, RRC scheduling or a physical UE.

Outputs under `transport/`: pcap, decoded TSV, capture statistics, per-profile
ping samples, qdisc state/counters and `result.json`. Captures spool to Linux
temporary storage before copying to the output to avoid per-packet Windows mount
I/O. Captures can contain subscriber identifiers and NAS authentication exchanges;
keep them in ignored local artifacts, not in published documentation.
N3 captures use `ip proto 17`, not `udp port 2152`: port filters exclude later
IPv4 fragments. The kernel classifier likewise covers all UDP to an exact N3
peer so every GTP-U fragment receives the same budget. Other destination IPs
and non-UDP traffic remain outside the delayed classes.

`python3 -m backend.onprem.operations.validation.check_delay_safety` exercises
host namespace refusal, foreign qdisc preservation, peer filter installation,
owned reapply/removal and invalid delay rejection in a disposable network namespace.
