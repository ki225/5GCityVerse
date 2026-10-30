# Intent-driven service control

This implementation is separate from the legacy treatment/holdout diagnostic. Formal traffic uses strategy none; IntentService manages every matching session. CITYVERSE_LEGACY_AI_DIAGNOSTICS=true explicitly enables the old experiment and does not establish formal service acceptance.

## Configuration and authority

CPU/RAM targets may set `derive_impacted_services: true` for `resize_nf`. In that mode, omit `impacted_service_ids` from both model parameters and `fixed_parameters`; the trusted native service inventory supplies the complete current scope (active intent UUIDs and `service-type:<type>` for classified sessions without a contract). The validated action and its native preflight receipt retain those exact IDs. Every subsequent preflight compares the current scope again; a newly joined service blocks the old action. This does not authorize missing classification or incomplete native inventory.

Set CITYVERSE_INTENT_WRITE_TOKEN in the server environment. All intent and operation record endpoints require its bearer token; the browser session identifier partitions records and is not authentication. The UI token is memory-only and cleared on dialog close.

Set CITYVERSE_INTENT_CONFIG_FILE to an absolute operator-controlled JSON path and CITYVERSE_INTENT_CONFIG_SHA256 to its SHA-256. Both are required together. Configuration is bounded to 1 MiB and schema cityverse.intent-configuration/v1. Start from backend/onprem/intents/configuration.example.json. An empty configuration has no authorized policies or native providers. Never put credentials into the JSON: provider token_file references an absolute private credential file.

Authority arrays identify existing policies; a model cannot create policy IDs or raise a budget. Configure budgets, targets, calibrated capacity_profiles and commissioned session_bindings before enabling changes. Target numeric bounds, fixed parameters, prices, guarantees and continuity permissions are operator-owned. Native providers must independently report capabilities and compare-and-swap support. Missing native integrations are not_configured.

A session binding joins exact native SUPI, session ID and runtime generation to network scope, SUPI, PDU session ID and establishment epoch, S-NSSAI, DNN, SMF, UPF, slice instance, path revision and application classification. Never copy a prior session's epoch into a new session. A same-SUPI match is insufficient. The native UPF epoch also invalidates prior plans.

Capacity profiles require measured hardware, software, workload, CPU/RAM and valid calibration range. Shared-NF capacity planning additionally requires fresh offered-load evidence for the complete actual target cohort. Pinned calibration workload is not a substitute for current load. Current receiver evidence does not provide every possible RTT statistic; missing metrics remain unknown.

## API and UI

POST /api/intents accepts a canonical ServiceIntent or {text}. Optional city_traffic supplies a validated city event request with requestId, baselineUes, events and strategy none. Natural language is compiled and authorized before traffic is started. External scenario admission must be explicitly confirmed before planning or native changes. Failed admission aborts the queued operation.

Mutations require Idempotency-Key. PATCH /api/intents/{id} also requires If-Match for revision checks. Submission returns HTTP 202 and an operation ID. GET /api/intents/{id}, /api/operations/{id} and /api/intents/{id}/members expose contract, operation and paginated membership. Reconcile and release are asynchronous POST actions. GET /api/capabilities exposes configured versus unavailable actions.

The on-prem Service intents dialog supports current traffic or a new city event, progress, reassessment, release and an evidence report. Understanding, planning, native installation and service effect are separate results. A hold is acceptable only with actual passing observations.

## Native integrations

The registry defines typed contracts for update_qos, resize_nf, add_upf, assign_sessions, provision_node, place_nf and drain_and_remove. Concrete clients are bounded and fixed-origin; they do not execute model-selected shell commands. Native QER changes require exact-session identity, version and owner checks, plus independent UPF/PDR/QER readback. Kubernetes resize uses the native resize subresource and current node allocation, with memory and all affected-service evidence. Node provisioning is an interface intentionally left uncommissioned until the operator specifies its resource source.

### UPF lifecycle: native SMF deployed; end-to-end steering remains unverified

The managed UPF controller server and SMF selection/drain/admission/census extensions are implemented. The baseline-compatible SMF extension has been deployed and its old/new authenticated native inventory APIs read back successfully. The standalone UPF controller HTTP transport has been exercised on the actual cluster; native path commissioning and live steering/effect remain separate acceptance gates. Controller contract tests and native Go tests alone do not establish those effects. The original pinned SMF baseline lacked these primitives; the [source-hash and line-evidence audit](../../../docs/onprem/intent-upf-controller-primitives-2026-10-27.md) records that baseline, not the newly patched or deployed image. Held reservation and ready-UPF pre-traffic planning are implemented; their native commissioning and live validation remain incomplete. Active-session migration remains unsupported.

New-session selection and active-session movement are distinct operations. `pending_admission.py` now provides the typed Python pending-admission protocol, native SMF client, lineage verification, and pre-traffic orchestration boundary described below. Its native routes require the matching SMF extension; the pinned upstream base alone does not provide them. Setting `mode=new_sessions` cannot move an already active UE's PDU session. Moving an established session requires separately implemented state transfer and continuity; migration remains unsupported by this implementation.

Do not enable add_upf/place_nf/assign_sessions/drain_and_remove merely because an HTTP client exists or a Pod is Ready. Required work includes native selection/drain/CAS extensions, pinned network/IP allocation, dynamic PFCP/path/session readback and the concrete lifecycle server. Missing primitives remain not_configured; source-audit evidence does not establish live deployment behavior.

Operations use SQLite journals, revisions, finite resource leases and owner-scoped compensation. External IO and model calls run outside database transactions. Native unknown outcomes require readback/recovery rather than blind replay. Whole post-installation receiver windows must begin after the confirmed native installation time. Different receiver measurement intervals are not combined into a fabricated group window.

Native installation can be asynchronous. The runtime persists and polls readback within each action timeout and plan/intent validity, without repeating the mutation. A pending Pod, PFCP exchange, or resize does not trigger an immediate success or an immediate speculative rollback. Only confirmed native readback starts the installation timestamp; timeout or expired authority enters compensation/recovery.

### Pending admission and pre-traffic ordering

`AdmissionIdentity` contains network scope, SUPI, PDU-session ID and nonce. `PendingAdmission` adds owner, actual target-UPF UUID and validity. No established-session epoch is fabricated. `SessionAssignment(mode=new_sessions)` carries pending admissions with empty established `session_keys`; explicit migration retains exact active keys and is rejected unless a separately commissioned native migration capability exists.

`FixedOriginSmfAdmissionApi` uses authenticated, fixed-origin `/nsmf-oam/v1/session-admissions` routes. A registered/selected policy is not a bound session. `PendingAdmissionCoordinator.read_binding()` returns a binding only after native proof supplies actual SM-context reference, session incarnation, selected UPF, creation time, SMF boot identity, nonce/owner and original installation revision. Binding is not receiver-traffic proof. An uncertain registration is recovered by readback, never by blindly consuming or replacing the nonce.

Optional trusted callback:

```python
pre_traffic_admission(intent, traffic_body, operation_id) -> PreparedSessionAdmissions
observer.bind_admissions(intent, prepared, native_binding_batch)  # optional trusted join
```

When `requires_pre_traffic_admission` is enabled, native reservation and installed-policy readbacks are journaled **before** `traffic_starter` is called. The runtime then waits for actual native creation, rereads observation and resolves complete service membership. The observer must join the remaining S-NSSAI/DNN/SMF/application information using real native inventory and trusted reservation metadata; a native reference alone does not invent those fields. Traffic-start failure cancels owned selection policies. Cancelling a pending selection policy alone does not tear down or migrate an active PDU. A formal intent-owned traffic release additionally drains its exact owned native UE lease and verifies disappearance before final release.

Held UE admission now uses a separate current-state SQLite reservation with owner/start-operation/request identity, expiry, content CAS and exact SUPI/PDU/nonces. It does not create a runnable generation. Normal generation writes refuse a live hold; typed consumption atomically creates exactly that allocation, and restart retries return the same generation. The controller requires fresh native empty-fleet proof and an explicit commissioned launch profile matching the real driver image/config digest. No PDU ID is inferred from a default or from the -p concurrency flag.

The concrete pre-traffic workflow reserves identities, reads actual native capacity, asks the existing OpenAI proposal provider, independently validates full reservation coverage/budgets/current calibration, installs native pending selection, and only then hands the same reservation to native traffic startup. Missing native load/profile/authority yields `needs_measurement`, releases the hold and permits retry with the same idempotency key. This phase currently admits selection on existing ready UPFs; pre-traffic infrastructure creation remains uncommissioned and is rejected. Source and fake-controller tests do not establish live steering or measured service improvement.

For a planned pending-session assignment, effect verification resolves actual newly bound session keys from native readback. Empty planned keys never count as an empty successful cohort. The fresh observation must contain every newly bound session, preserve all prior members, and contain no unrelated new service members; the evaluator receives this actual complete membership. Existing active sessions cannot be relabeled as newly created bindings.

## Verification scope

Run python -m pytest test/intents -q and npm --prefix frontend test -- --run, then npm --prefix frontend run build. The isolated browser test uses real HTTP and SQLite with an intentionally unavailable compiler; it proves the needs_definition path and UI behavior, not network effect. Genuine OpenAI compile evidence, controller-contract tests and native live evidence must be reported separately.

Full city acceptance still requires actual multi-session native traffic, membership changes, QoS/CPU/capacity bottlenecks, competing services, failure/restart and expiry cleanup, plus fixed/rule/AI comparison from the same initial conditions. Never infer these results from unit tests or Pod readiness.

## Module boundaries

ServiceIntent and immutable session/observation types define the domain boundary. MembershipResolver and EffectivePolicyCoordinator calculate current membership and a single effective policy. ResourceProposalProvider proposes data; CapacityPolicyValidator and ConstrainedPlanner independently admit and rank it. ActuatorRegistry selects typed provider adapters. IntentRepository owns durable transactions and leases; IntentService coordinates the asynchronous state machine and compensation. The HTTP controller and city bridge call the service rather than directly changing infrastructure.


Restoration is asynchronous: the runtime journals a compensating receipt before dispatch, sends restoration once, then polls read-only native restoration state within the bounded cleanup deadline. The cleanup interval remains available after intent expiry. A restart resumes the durable receipt cursor without replaying restoration writes. Missing native baseline/ownership/deletion evidence leaves recovery required; pending cleanup never counts as restored.


Native traffic evidence keeps uplink and downlink report windows separate. Each directional report must bind its native SUPI, PDU-session ID and SM-context reference. offeredObservedMbps supplies actual sender load; configured offeredBySession/offeredBySupi values are requested-load diagnostics only. Combined load, loss and demand satisfaction require exactly matching direction windows, and group sums require matching windows across every member. Unknown combined evidence remains unknown; directional QER diagnosis can still use the corresponding measured sender/receiver, QER cap and explicitly commissioned directional path capacity.


Formal intent-owned traffic starts with mandatory intent, operation_id and owner metadata. The accepted native traffic lease and startup receipt are stored atomically in a bounded current-resource table. Startup crash recovery uses read-only matching-lease lookup; it never relaunches an unknown start. Traffic cleanup is requested only after native policy/selection restoration completes, and must explicitly confirm `released: true` before the intent expires. Failed cleanup retains the lease and restoration journal; a retry only finishes traffic cleanup rather than replaying native restoration. Failed admission or an undefined intent does not call traffic finish.


Restoration uses three durable phases for every formal traffic lease, including QoS-only or no-infrastructure plans: restore QoS and close future admissions; issue an exact owned UE-drain permit and verify native UE disappearance; then remove/restore owned UPF/node resources. The final traffic lease is finished only after complete infrastructure restoration. The same journal handles failed-plan compensation and restart; an intermediate policy-restored flag never means final restoration or expiration. Same-target UPF stages preserve the original creation baseline rather than briefly reopening selection during per-stage rollback.

Wire load calibration is explicit: `nf_ingress_wire_bps` is interface RX, not service payload. Target policies and capacity profiles must match metric name/source/scope. Pre-traffic payload forecasts require a commissioned multiplier and packet/workload profile before comparison with wire calibration; missing evidence remains unknown. QoS mutation additionally requires current receiver coverage of every native user of each affected UPF, including users outside the target city service group.


## Native multi-session commissioning

The v2 current projection keeps physical devices and exact PDU sessions separate. Runtime guards include the canonical session key, generation, Pod UID, container ID, process epoch, SMF boot and native UPF UUID/epoch. A configured UERANSIM device requires an explicitly pinned device driver/profile plus actual native observer proof. The deployed free-ran gNB uses a proprietary radio protocol; it cannot validate UERANSIM by an image swap. Matching RAN/N2/N3/NSSAI commissioning and live two-PDU proof remain required.

A generic `service_catalog` maps commissioned scenario names to `service_type` and `region`; the old `ue_classification.service_mapping` remains a compatibility fallback. `SessionWorkloadCompiler` uses each exact session identity and actual session slice. Future reserved demand and actual v2 sampling share this compiler. Its functional mode caps offered demand to the generator budget; QoS mode preserves declared demand. The v1 per-UE `serviceRequirements` contract is rejected for v2 rather than silently multiplying its meaning per PDU. Declared applications do not create observed flow bindings.

Legacy policy-restored receipts do not prove UE disappearance. Recovery requires the original immutable typed lease and fresh exact-generation native gone proof. An already terminal idempotency key preserves its original result; a new release key can initiate the compatible owned cleanup.

## Current population adapter

Pinned intent configuration may include `"population": {"enabled": true}` to
compose the existing device population writer, pre-traffic capacity planner,
native SMF admission coordinator, current native classification reader and QER
provider. Missing mandatory ports fail configuration. This switch does not
commission the feature: public `fleet_join` and `fleet_leave` remain false until
the native v2 path is commissioned. No request field can override that gate.

The current source supports whole physical-device deltas in one owned formal
v2 fleet. It keeps surviving native session identities, validates capacity and
installs exact future admissions before creation, then inherits all matching
verified homogeneous current service policies before publishing the new cohort
to traffic sampling. Inheritance proves native installation, not receiver
effect, and creates no AI diagnosis or synthetic measurement. Application-flow
inheritance and heterogeneous per-member templates fail closed. New native
targets require current pinned budget, rate and continuity authority.

Scoped departure closes the departing PDU selections and restores their exact
QER baselines before the existing writer retires the owned device. Foreign
co-resident service dependencies or claims block departure. Uncertain commands
retain ownership and are read back; they never trigger whole-fleet cleanup.
The current desired policy definition survives an empty group and is retired
only after full confirmed expiry. Empty current groups require a fresh native
absence proof from the exact committed population delta; old receiver windows
are never carried into the new population revision.

Newly installed matching members invalidate satisfaction for every affected
contract. Current desired membership includes those members with explicit
pending counts until normal receiver verification covers the whole cohort.
Old-cohort planning cannot withdraw or certify their policies. A departing
pending identity is pruned only after the exact owned native absence barrier.

A verified hold can persist a typed v3 `observe_native` service definition. It
contains selector/objective/authority identity and the actual unchanged native
baseline, with no rate allocation template or QoS claim. Creation requires
whole-cohort receiver attainment and protected-service verification, exact
native session-policy versions in both observation seals, and fresh unowned
QER readback, or positive current committed shared-policy/native installation
proof. Its rule is `observe_effective_configuration`, with `allocating=false`.
Original unowned baseline remains restoration provenance. Existing `native_qos`
definitions remain intact. Legacy v2 `preserve_native_configuration` retains
its strict semantics: shared allocation stays unavailable until a new genuine
whole-cohort effect/current-contract proof explicitly recertifies it as v3.

Population admission still requires genuine calibrated capacity, authorized
native admission and exact creation bindings. An `observe_native` member must
match the commissioned logical target, native UPF identity/epoch, S-NSSAI, DNN
and actual baseline. Current native APIs expose session AMBR MBR and PDR
cardinality; this definition makes no gate/GBR/QFI observability claim. A
divergent, foreign-owned or insufficient baseline fails closed. With no effective
allocation the original unowned baseline must remain unchanged. For v3 plus
matching authorized allocating contracts, all current native QoS claims merge
into one effective allocation; the observer contributes constraints and no rate
claim. Pre-offer checks validate current authority, per-session rate-envelope
constraints, budget, continuity and fresh calibrated pretraffic capacity. They
do not establish that all service objectives are already attained. Native owner prefix alone
is insufficient: exact committed claims, owner/operation/version/QER/path and
readback must match. Group-rate prediction is currently unsupported and fails
closed; RTT/loss/throughput attainment remains pending fresh whole-cohort receiver
verification. These checks never relabel an unmeasured SLA as met.

Exact session/resource and current intent leases fence both allocating and
observe-only inheritance through actual traffic-cohort publication. Native
baseline readback is checked again immediately before publication while the
leases remain held; only exact committed-cohort readback unlocks them. A lost
publication acknowledgement retains exclusive leases even after their clock
expires. Restart confirms the original publication without replaying native
writes, then unlocks; legitimate policy changes can proceed afterward. No
SQLite transaction is held across native I/O. This fences writers using the
service's authoritative lease protocol; it does not authorize external writers.

Completed publication acknowledgement remains read-only after expiry or while
the original lease is restoring, provided immutable ownership, renewal watermark,
round/generation/cohort and request/idempotency fingerprints still match. A
retained unknown acknowledgement blocks new population reservation.

If publication failed, whole-fleet owner release first freezes the exact current
delta under the population writer lock. It reads back uncertain native commands,
restores uncommitted installations, closes original future admission nonces and
quiesces the runtime's full old-plus-added allocation. An atomic ownership check
then transfers retained leases to the same full-release operation; other pending
operations remain blocked. Durable pre-runtime-dispatch failure can be closed
without inventing native absence. A dispatched command with no positive native
readback remains recovery required, including an unchanged-baseline GET that
cannot exclude a request still in transit. This is never TTL-based takeover.

Observe-only fallback inheritance performs no QER write. A legitimate allocating
contract can install the merged configuration while the observer adds zero QoS
claims. Receipts report actual native command count and no verified receiver
effect; every affected desired membership becomes pending, and the observer's
installed QoS count remains zero. Only the normal fresh
whole-group receiver/AI loop can certify service attainment. These are source
contracts with native doubles in tests; the native population capability stays
false until the corresponding live commissioning succeeds.
# Session rebuild and application receiver boundaries

`SessionRebuildProvider` supports only commissioned V2, completely owned single-device,
single-PDU scopes whose service profile can be preserved. It requires explicit continuity
permission and a bounded interruption budget. Geographic replacement preserves the
original demand group, point, service profile, commissioned gNB/UID and offered UL/DL
through a server-only typed replacement reservation. The formal composition selects
`FreeRanKubeadmDriver` and the commissioned FreeRAN device compiler/observer; a UERANSIM
driver or mismatched profile/image is rejected before provider setup. Geographic execution
still requires actual native gNB UID and PDR/FAR evidence, not merely configured coordinates.
Unknown vendor/profile or missing native geography is rejected before removal. No
cross-platform migration is implemented. Source integration does not establish live
commissioning or successful geographic replacement on the current seven gNBs.
This is interrupted stop/recreate with a new native admission nonce and
SessionKey, not transparent migration. Once the old session disappears, restoration
requires a separately authorized admission; the provider does not blindly recreate it.

Application routing uses native session/PDR/FAR and endpoint UID/revision readback.
`start_service_flows` runs bounded UE clients against the selected persistent application
receiver, with sequential UL/DL windows. Its effect gate joins exact `PathEvidence` from
the durable action to the evaluator's receiver evidence and requires complete session /
direction coverage within the evaluated window. Installed routing alone is not proof
of application delivery or SLA improvement.

## Formal FreeRAN runtime composition

`ApplicationContext → configured_driver → FreeRanKubeadmDriver` selects the real UE
runtime before the same controller is passed to scenario rounds, pre-traffic reservation,
population changes and `IntentService`. `configured_native_device_observer` selects its
matching FreeRAN reader. The existing three-role LangGraph and ResourcePlan remain the
formal AI control path. Explicit UERANSIM diagnostic drivers do not enter this composition.

The same digest-pinned `CITYVERSE_INTENT_CONFIG_FILE` used for providers and model policy
contains `ue_device_image` and `ue_device_profile`; set `CITYVERSE_INTENT_CONFIG_SHA256`
to the file's SHA-256. Profile fields are `driver: "free-ran-ue"`, `image_digest`,
`source_revision`, `evidence_reference`, `maximum_sessions: 1`, `gnb_search_endpoint`,
and optional `commissioned_gnb_endpoints`. Source revision and evidence reference must
come from commissioning; a sample label is not native evidence. Endpoints must belong
to the actual commissioned city gNBs. Arbitrary frontend endpoints are not accepted.

The deployed UE manifest source pins its image in `runtime/free_ran_ue.py:UE_IMAGE`,
runs `./free-ran-ue ue -c /run/fru/ue.json -n 1 -p 1` per device, and constructs the
native UE JSON in the credentials-only init container. FreeRAN radio uses ports
31413/31414; its evidence reader joins gNB HTTP context with reciprocal native SCTP and
SMF/UPF/TUN readback. Neither a successful launch nor HTTP readiness proves service SLA.
The deployed FreeRAN UE has no per-UE NAS status HTTP API. Current registration is
independently read from AMF and the exact gNB's SUPI context; the observer also checks
the sealed single-UE process, installed FAR destination and actual TUN/IP. It does
not synthesize a UE-reported MM state. Formal V2 FreeRAN admission requires complete
geographic demand groups so the native gNB/UID selection can be pinned before launch.

Set `CITYVERSE_ONPREM_RAN_BACKEND=free-ran-ue`, an explicit kubeadm context/namespace,
and the existing UE/traffic gates for authorized operation. Formal pre-traffic admission
requires the commissioned device profile/image pair; conflicting image overrides fail.
The existing OpenAI configuration is reused; this change introduces no model endpoint.
Use a separate state directory and exact owner/round/generation for isolated validation;
a fresh SQLite file does not grant ownership of an existing UE population.
