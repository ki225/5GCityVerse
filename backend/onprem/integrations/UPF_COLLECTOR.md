# Native UPF kernel collector

`integrations/upf.py` is an executable, standard-library-only HTTP sidecar. It reads
the real Linux `gtp5g` generic-netlink family. It does not consume backend desired
counts, simulation traffic, PFCP success logs or uploaded evidence files.

```text
python -m backend.onprem.integrations.upf --upf upf-embb --interface upfgtp --host <POD_IP> --port 9081 --loopback-listener
python -m backend.onprem.integrations.upf --upf upf-embb --once
```

Run in the UPF Pod's network namespace with `CAP_NET_ADMIN`, readable `/proc`
and `/sys/module/gtp5g/version`, and exactly one `gtp5g` interface named
`upfgtp`. No host namespace or writable host mount is required. Bind the
primary Pod IP and restrict Service access to the internal evidence coordinator.
`--loopback-listener` additionally binds `127.0.0.1` on the same port for
`kubectl port-forward`; it does not expose the collector on Multus N3/N4/N6
interfaces. All requested sockets must bind successfully before serving.

The module accepts only installed `gtp5g` version **0.9.14**. Changing this
requires reviewing its wire ABI and updating compatibility tests. The pinned
free5GC v4.2.2 go-upf requires `go-gtp5gnl` v1.6.1.

## Actual upstream interfaces

- [go-upf v4.2.2 submodule go.mod](https://github.com/free5gc/go-upf/blob/04c1ab640350f5d354d09fac82cd7b4d66c78533/go.mod)
- [go-gtp5gnl v1.6.1 PDR attribute decoder](https://github.com/free5gc/go-gtp5gnl/blob/v1.6.1/attr_pdr.go)
- [go-gtp5gnl v1.6.1 FAR attribute decoder](https://github.com/free5gc/go-gtp5gnl/blob/v1.6.1/attr_far.go)
- [gtp5g v0.9.14 PDR kernel emitter](https://github.com/free5gc/gtp5g/blob/v0.9.14/src/genl/genl_pdr.c)
- [gtp5g v0.9.14 FAR kernel emitter](https://github.com/free5gc/gtp5g/blob/v0.9.14/src/genl/genl_far.c)

Only `RTM_GETLINK`, generic-family lookup, `CMD_GET_PDR=7` and
`CMD_GET_FAR=8` are sent. No add/update/delete netlink command exists in the
exporter. The response decodes native-endian rule IDs, PFCP local SEID and TEIDs;
IPv4 attributes contain network-order address bytes. The upstream dump omits
interface IDs, so the exporter rejects namespaces containing multiple `gtp5g`
devices instead of guessing which UPF owns a rule.

## Current state and lifecycle

Each request reads interface/namespace identity, all PDRs/FARs twice, then reads
identity again. A changed namespace, differing snapshots, multipart truncation,
kernel error, interrupted dump, sequence mismatch or timeout returns unavailable.
No prior successful snapshot is reused. This makes deletion visible immediately
on the next observation and prevents replaying state after collector restart.
The five-second collection limit leaves room inside the verifier's freshness
window; actual 1000-UE performance still requires measuring the target cluster.

Kernel rules are joined by local PFCP SEID and PDR-to-FAR references. A complete
session needs one UL F-TEID PDR and one DL UE-address PDR, their forwarding FARs,
and complete GTP-U/IPv4 outer-header creation to port 2152. Drop, buffer, missing
FAR, missing PDR, zero tunnel, ambiguous IP or duplicate rule identities cannot
become a complete session. Incomplete/orphan state remains in `residuals`.

This proves **installed kernel forwarding rules**, not reception of a particular
PFCP response packet. AMF/SMF OAM and a successful UE TUN-to-N6 probe provide the
other independent parts of the acceptance evidence chain. SUPI, PDU session ID
and generation are deliberately absent from kernel records; the coordinator
must join native SMF identity/IP and actual UE workload attribution, rejecting
ambiguous mappings rather than assigning identities from counts.

## HTTP contract

`GET /v1/upf-state`: HTTP 200 only for a complete, fresh kernel read; otherwise
HTTP 503. Every response sends `Cache-Control: no-store`.

```text
schemaVersion: cityverse.upf/v1
available: true | false
source: gtp5g-netlink
upf: upf-embb | upf-urllc | upf-mmtc | upf-v2x
observedAt: timezone-aware ISO timestamp, null on unavailable
epoch: boot-ID + netns inode + interface index + collector instance UUID
interfaceIndex: actual Linux interface index
sessions: [{ip,upf,pfcpSeid,uplinkTeid,downlinkTeid,upfAddress,gnbAddress,
            interfaceIndex,observedAt,evidence:kernel-installed-pdr-far}]
residuals: [{pfcpSeid,ip,reason}]
errors: [diagnostic]
```

`pfcpSeid` is a decimal string. TEIDs are positive JSON integers. A successful
empty `sessions` array is **not** proof of full cleanup if `residuals` is nonempty.
The coordinator must carry every UPF epoch into its evidence; epoch changes reset
the 60-second stable window. Poll every source, including UPFs with requested
count zero, to detect residual contexts.

## Verification performed

`test/subsystems/onprem/test_onprem_upf.py` tests binary Linux ABI fixtures, endian/nested
TLV parsing, incomplete/failed forwarding, orphan FARs, deletion, namespace and
collector restart, duplicate IP/TEID, interrupted/replayed messages and a real
local HTTP server's 200-to-503 transition. The protocol fixtures are constructed
from the pinned kernel emitter ABI; they are not represented as live captures.

The executable was run under WSL Linux using its actual netlink sockets. The
base WSL namespace lacks the required `gtp5g` UPF interface/module, so it correctly
returned unavailable. A live sidecar with kernel rules and real UE traffic is
still required for end-to-end acceptance.
