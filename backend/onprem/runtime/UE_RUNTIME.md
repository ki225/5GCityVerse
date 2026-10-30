# On-prem UE runtime implementation status

The public API admits at most 100 UEs; this document also describes the internal
1000-identity pool and historical subsystem fixtures. Those do not increase the
public admission limit or establish completed live acceptance. Subsystem tests use
explicit fixtures and subprocess doubles. No kubeadm cluster, subscriber DB,
PDU session, PFCP tunnel, or user-plane packet has been verified by those tests.

## Implemented

- `GET /api/onprem/ue` (alias `/api/onprem/ue/status`), `POST /api/onprem/ue`
  with exactly `embb`, `urllc`, `mmtc`, `v2x` integer counts, public sum 0–100.
  The internal controller retains its 1000-identity envelope for stored-state
  compatibility; the application service enforces the public limit.
- Durable SQLite generations, idempotent equal targets, unique allocation,
  process fencing, restart observation invalidation, websocket `ue_status`.
- Explicit-context Kubernetes launch driver with NGAP/subscriber gates, fixed
  ownership labels, batch ramp, previous generation teardown and timeout cleanup.
- Native AMF and SMF OAM reads, identity/context joining and strict versioned
  independent evidence validation. No count is inferred from Pod replicas.
- In-process `NativeCollector` joins native OAM, fresh UPF kernel-exporter
  snapshots, Kubernetes ownership/UIDs, bulk subscriber reads and actual probes.
- `TunProber` discovers nodes/interfaces and independently reads every SUPI's
  `nr-cli --exec ps-list` (session ID, IP, DNN and S-NSSAI), then batches `ping -I`
  inside that Pod: two kubectl exec calls per Pod, up to eight Pods and 64 ping
  processes per Pod concurrently. Tests cover 1000 fixture UEs in 20 Pods with
  40 exec calls, not 3000. Target-cluster throughput remains unverified.
- Explicit subscriber CLI, four-slice profile, read-back and uniqueness checks,
  idempotent creation and fail-closed drift detection without resetting live SQN.

## Native collection and deployment requirements

`CITYVERSE_ONPREM_EVIDENCE_MODE=native` selects the implemented in-process
coordinator. It shares the controller's durable GenerationStore directly; no
browser evidence upload or second generation service is involved. Four UPF
sidecars expose `/v1/upf-state` from fresh gtp5g netlink PDR/FAR dumps. Native OAM
supplies SUPI/session identity; the coordinator joins unique PDU IPs to actual
kernel sessions, retains unresolved kernel records as residual contexts, and
uses Pod UID/generation ownership for UE attribution. UPF namespace/exporter
epoch changes reset the stability window.

The collector makes four bulk MongoDB reads of UDR authentication, AM, SM and
SMF-selection subscriptions, comparing all 1000 SUPIs, four allowed slices,
DNN and the mounted credential groups. It never returns raw subscriber records
or key/OPc values. gNB `nr-cli --exec status` must report `is-ngap-up: true`.
Collection has a 12-second deadline; evidence remains limited to 15 seconds of
age. Missing or incompatible sources fail closed with a fixed source phase and
exception class, never raw credential-bearing error text.

Without an intent, native mode observes generation 0 read-only; no UE workload
mutation occurs. Pinned SMF has no upstream full context-inventory endpoint, so an empty
AMF and empty kernel dump cannot prove absence of unknown SMF orphan contexts.
The adapter makes a real SMF capability request even with no AMF contexts,
persists known SMF references beside the UE SQLite store across restarts, and
queries them until an explicit native 404. An earlier opt-in pinned inventory-v1
diagnostic prototype used `sync.Map.Range`, but its traversal
is not an atomic snapshot. It can add newly discovered release
obligations, never delete old obligations, and still reports
`inventoryComplete: false`: zero remains `verification_unavailable`, never
`stopped`. Positive UE intents can be commissioned through the API once source
capabilities, subscriber pool and NGAP are healthy, but the full zero-baseline
acceptance runner intentionally cannot pass this inventory-consistency limitation.
That prototype is not used by default and has not been deployed. No context reset
is performed.

The source code and fixture tests do not demonstrate that the target deployment
has kernel modules, compatible images, access to native APIs, or sufficient
capacity. Deploy the sidecars and commission all sources before live UE gates.

The local lab helper `python scripts/onprem/start_native_backend.py --check-only`
verifies the dedicated kubeconfig/context, downloaded Windows kubectl checksum,
actual service names/ports and free loopback ports. Without `--check-only`, it
supervises eight loopback-only service forwards and a single uvicorn worker.
It reuses `.cache/kubeadm-lab/groups.json` and `backend-venv`, writes private logs
under the ignored lab directory, and cleans up its children on Ctrl-C. It does
not create credentials, subscribers or UE intents. Mutations are disabled unless
explicitly launched with `--enable-mutations` after source commissioning.
Current lab forwards: AMF `18080`, SMF `18081`, Mongo `27018`, WebConsole `15000`,
and four UPF exporters `19081` through `19084`, all bound to `127.0.0.1`.

The actual UE launch ramp defaults to `nr-ue -t 500` (500 ms between UEs) and
30 seconds between Pod launch units, maximum 50 UEs per unit. A 1000-UE launch
therefore takes approximately ten minutes before the final 60-second stability
window, plus any previous-generation retirement. For a **new** capacity test
attempt the launcher supports `--ue-timeout-seconds 1800` (default 900). This
does not change 15-second evidence freshness or 60-second stability. Do not
extend or rewrite the timestamp of an existing failed generation; retain its
failure and create an explicit new intent only after safe known-context cleanup.

Native SMF OAM exposes SUPI, session ID, S-NSSAI, DNN, PDU address and connection
state, but its `Tunnel` field is not populated. Merely mapping a slice to its
configured UPF does not prove a tunnel. Existing patched-SMF logs are not a
valid substitute for the locked unmodified upstream runtime. No fallback
collector, generated traffic or success-log multiplier is provided.

The native adapter was checked against the actual free5GC v4.2.2 submodules:

- AMF `02675d949f207a95379af458c7e80fabe802d728`,
  `internal/sbi/processor/oam.go` and `internal/sbi/api_oam.go`.
- SMF `2232d2992f204207e58830ac8ee55bf0cd832f92`, same files.

WebConsole provisioning uses the pinned `/api/subscriber/{supi}/20893` GET and
`/api/subscriber/{supi}/20893/1` POST contract. Existing profile drift is rejected;
PUT/PATCH is not used because it could reset the live AKA sequence number.
The CLI verifies the web profile; native collection independently compares the
actual UDR authentication data before allowing UE launch.
An unsupported schema stops provisioning, rather than reporting success.

## Configuration

| Environment variable | Default / purpose |
| --- | --- |
| `CITYVERSE_ONPREM_RUNTIME` | `legacy`; set `kubeadm` only as the explicit migration selector after gates |
| `CITYVERSE_ONPREM_UE_STATE_FILE` | `.tmp/onprem-ue.sqlite3`; persistent volume required in deployment |
| `CITYVERSE_ONPREM_EVIDENCE_MODE` | absent: external HTTP adapter; `native` enables in-process native collection |
| `CITYVERSE_ONPREM_UPF_EVIDENCE_URLS` | JSON map of exactly four slice names to sidecar base URLs; defaults to namespace service DNS |
| `CITYVERSE_ONPREM_MONGO_URI` | required in native mode; read-only Mongo credentials from local secret environment |
| `CITYVERSE_ONPREM_MONGO_DATABASE` | `free5gc`; deployed UDR database name |
| `CITYVERSE_ONPREM_CREDENTIALS_FILE` | required in native mode; mounted `groups.json`, never put key values in environment |
| `CITYVERSE_ONPREM_EVIDENCE_URL` | absent: `verification_unavailable`; trusted internal collector base URL |
| `CITYVERSE_ONPREM_EVIDENCE_TOKEN` | optional bearer token, load from Secret |
| `CITYVERSE_ONPREM_AMF_OAM_URL` | explicit AMF SBI base URL enables native OAM joining |
| `CITYVERSE_ONPREM_SMF_OAM_URL` | explicit SMF SBI base URL, required with AMF URL |
| `CITYVERSE_ONPREM_OAM_TOKEN` | optional NF OAuth bearer token, load from Secret |
| `CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED` | `false`; `true` explicitly enables Kubernetes mutations |
| `CITYVERSE_ONPREM_KUBE_CONTEXT` | required for mutations; never defaults to current context |
| `CITYVERSE_ONPREM_NAMESPACE` | `free5gc-onprem`; must carry `cityverse.io/platform=kubeadm` |
| `CITYVERSE_ONPREM_UE_BATCH_SIZE` | `50`; integer 1–250 |
| `CITYVERSE_ONPREM_UE_BATCH_INTERVAL` | `30` seconds default, minimum `2` |
| `CITYVERSE_ONPREM_UE_TEMPO_MS` | `500` default; real `nr-ue -t` per-UE startup delay, integer `1..60000` |
| `CITYVERSE_ONPREM_UE_TIMEOUT` | `900` seconds; cleanup current owned UE Pods on timeout |
| `CITYVERSE_ONPREM_UERANSIM_IMAGE` | official v4.0.1 image pinned to platform-verified digest |
| `CITYVERSE_ONPREM_UE_INIT_IMAGE` | `python:3.12-slim`; pin an approved digest before live rollout |
| `CITYVERSE_ONPREM_WEBCONSOLE_URL` | explicit installation provisioning URL |
| `CITYVERSE_ONPREM_WEBCONSOLE_TOKEN` | provisioning token from Secret |

Do not set the mutation flag until platform preflight, inventory review,
subscriber pool and native evidence sources are ready. The namespace
label is an operator attestation, not an independent proof of platform identity.
The driver does not create the namespace or add that label.

The existing migration selector remains `legacy` until live gates pass. In
`kubeadm` mode health, slices, metrics and free5gc status read the new evidence;
old scenario trigger requests return 409. EKS code is unchanged.

For a host process, use a dedicated `KUBECONFIG`, explicit context, and loopback
port-forwards for AMF/SMF SBI, MongoDB, and all four UPF evidence services. In
that case override `CITYVERSE_ONPREM_UPF_EVIDENCE_URLS` with the four localhost
base URLs; host DNS does not resolve Kubernetes `.svc` names. Keep the generated
environment file and credential file under ignored `.cache/kubeadm-lab`, never
commit them. Start one process from the project root:

```text
python -m pip install -r backend/onprem/requirements.txt
python -m uvicorn backend.onprem.api.server:app --host 127.0.0.1 --port 8000 --workers 1
```

Useful commissioning checks are `GET /api/runtime/status` and
`GET /api/onprem/ue`. For example, `phase=native_upf_embb` isolates the eMBB
kernel source; `phase=subscriber_pool` isolates Mongo profile verification.
An unavailable phase must be repaired before claiming registration or traffic.

## Identity and credentials

Pool: `imsi-208930100000001` through `imsi-208930100001000` inclusive.
Four immutable groups of 250 SUPIs share one key/OPc per group. Credentials
follow the SUPI group even when slice assignments change. A range crossing a
group boundary splits into multiple launch units; groups are not synonymous
with launch Pods. The default launch unit is at most 50 UEs to enforce the
50-UE ramp without restarting a growing 250-UE multi-process instance.

Secret `cityverse-ue-credentials` contains `groups.json`: an array of exactly
four objects with 32-hex-digit `key` and `opc`. The init container reads it and
writes the UE config to a memory-backed volume. No secret value is included in
Pod environment variables, ConfigMaps or public status. UE Pods require
privileged access to create TUN devices and run only on RAN-labeled nodes.

Provision explicitly from the project root after mounting the credentials:

```text
python -m backend.onprem.operations.provisioning.subscribers --credentials-file /credentials/groups.json --apply
```

## Collector contract: cityverse.evidence/v1

The native in-process adapter returns the following internal structure. The
optional external HTTP adapter expects the same structure at internal-only
`GET /v1/generations/{generation}/evidence`; no such public endpoint is added:

```text
schemaVersion: cityverse.evidence/v1
generation: integer
observedAt: timezone-aware ISO 8601
sources: {ue,amf,smf,upf,traffic: {available:true,source:string,observedAt,epoch?}}
subscriberPool: {verifiedCount:1000,credentialsMatched:true}
gnb: {embb,urllc,mmtc,v2x: {ngapAssociated:true,observedAt}}
ues: [{supi,generation,
  amf:{registered,observedAt},
  smf:{sessionId,sessionCount:1,established,sst,sd,dnn,ip,upf,observedAt},
  upf:{sessionId,upf,ip,pfcpSeid,uplinkTeid,downlinkTeid,observedAt},
  ue:{sessionId,running,podUid,tun,ip,observedAt},
  traffic:{sessionId,success,podUid,interface,sourceIp,endpoint,
           transmitted,received,observedAt}}]
```

`sessionId` is a decimal string 1–255; `pfcpSeid` a positive decimal uint64
string; TEIDs are positive uint32 JSON integers; bools are not integers here.
All rows must include actual generation attribution, not relabel old contexts.
Return all runtime-owned contexts, including residual generations and unallocated
SUPIs. Empty rows may be reported only after querying all required sources.
Duplicate identities, IPs, `(podUid,TUN)` mappings, wrong generation or stale
observations cannot produce `established`. Native OAM replaces AMF/SMF fields;
the independent UPF record supplies observed UPF mapping.

The trusted N6 endpoints are fixed to `172.22.61.20` (eMBB), `.62.20` (URLLC),
`.63.20` (mMTC), `.64.20` (V2X). A collector-supplied endpoint map cannot override
those targets. Every observation must be no older than 15 seconds and not predate
the generation; a gap over 15 seconds resets the 60-second stable window.
Public `perUe` includes only whitelisted identity/probe fields, never raw secrets.

## Verification

Routine regression is `python test/api_regression.py --output .tmp/api-regression-new`.
Optional subsystem cases now live in `test/subsystems/onprem/`; historical cases
live in `archive/legacy/tests/`. Their prior coverage includes allocation fragmentation,
concurrent intent requests, restart/fencing, partial 997/1000 results, wrong
identity/tunnel/interface/endpoint/generation, bool rejection, stale evidence,
native OAM joins, subscriber drift/SQN preservation and namespace-bound probes.
`test/subsystems/onprem/test_onprem_collector.py` additionally verifies 1000 fixture UE
joins, bounded batching, source failures, cancellation, stale/future timestamps,
generation/Pod UID replacement, residual kernel sessions, and empty bootstrap.
These are local contract tests, not live capacity proof. Historical UE-only
1/10/50-UE results are recorded in `docs/onprem-kubeadm-50ue-e2e.md`, including
failed and assisted attempts. They are not current 50/100-UE AI/QER acceptance.
The old inventory-v1 traversal was non-atomic and could not establish global
zero-context absence. Current collection has a separately configured authoritative
SMF inventory path and runtime identity checks; its live availability must be
verified on the target runtime, not inferred from historical reports.

Timeout handling requests normal UE deregistration rather than deleting Pods
immediately. A command ACK does not prove shutdown: UERANSIM may cancel a
deregistration command while authenticating and subsequently register. Pod
removal therefore requires actual per-SUPI `MM-NULL` state (or exact
`MM-DEREGISTERED/NO-SUPI` with both SIM absent and emergency false), fresh post-ACK
AMF/SMF/UPF release observations, and atomic UID-constrained deletion. Missing
evidence or residual sessions retains the Pod; it is not a successful cleanup.
Only currently registered/normal-service UEs receive normal deregistration;
in-flight deregistration is not reissued. Retries are bounded to three per
SUPI/Pod UID in a controller process, at least 30 seconds apart. Termination
uses the separate native release observation path without requiring disabled
UEs to continue passing traffic probes; that path cannot establish a positive
traffic-verification result.

For a real-browser 50-UE commissioning run, start the existing native backend
and frontend, verify old-scope release and all seven required NRF profiles, then
run `python scripts/onprem/run_browser_ue_acceptance.py --execute --output <new-directory>`.
The helper submits exactly 13/13/12/12 through the UI, requires HTTP 202 and a
new generation, and checks every public per-UE proof continuously for 60 seconds
before verifying rendered UI totals. It saves screenshots, network/console
events, the intent and every sample. Without `--execute` it only inspects the UI.
`--resume-generation N` observes an existing 50-UE generation without resubmitting
or changing its backend deadline. This manual positive commissioning path does
not claim that the SMF complete-zero baseline gate passed. Preserve separate
raw AMF/SMF/UPF evidence for independent correlation.

## Readiness after NF maintenance

Pod `Ready` is not proof of NRF registration. The lab's NRF rollout left the
still-running PCF, SMF and NSSF undiscoverable (`nfInstances: null`), while AMF
repeatedly logged `AMF can not select an PCF by NRF` and registered no new UEs.
Before UE launch after NRF maintenance, query native NRF discovery for AMF,
AUSF, UDM, UDR, PCF, SMF and NSSF; each required NF must be discoverable with
`nfStatus: REGISTERED` and the expected service. Do not repair this by fabricating
NF profiles or relaxing UE evidence gates. Coordinate supported NF registration
recovery; any NF restart requires a known-session cleanup and impact review.

Similarly, UERANSIM's `is-ngap-up: true` alone can remain stale after an AMF
restart. Reconfirm the current SCTP association and a fresh NG Setup exchange;
do not treat a historical log or cached CLI boolean as a fresh connection.
