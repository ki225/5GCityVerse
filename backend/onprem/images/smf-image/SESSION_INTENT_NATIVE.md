# Native session and UPF control extension

The Dockerfile applies `patches/onprem-deployed-baseline.patch` and then `patches/session-qer-actuator.patch` to pinned upstream revision `2232d2992f204207e58830ac8ee55bf0cd832f92`. The baseline reproduces the deployed `ki225/smf` revision `f91c2cd4b2c78139050797b451f5e3dfc3b9047f` plus its UDM SDM GET retry and on-prem SUPI-only QER patch. It preserves the authoritative atomic inventory v2 API; the cloud QER patch alone is insufficient to reproduce that deployment. The extension implements exact-session QER actuation, current-session census, dynamic UPF registration/selection/drain, and new-session admission hooks. Existing active-session migration is unsupported.

All routes below use the existing OAM authentication guard and `X-SMF-QER-Actuator-Token`. The token is read from the existing mounted token file; no new token belongs in source or request bodies.

The preserved legacy `GET /nsmf-oam/v1/sm-contexts` uses its separate original `Authorization: Bearer` credential from `CITYVERSE_SMF_INVENTORY_TOKEN`. It returns `cityverse.smf-oam/v2` with an atomic membership-and-projection snapshot, and shares the process boot identity and pool revision fence with the new census. Neither credential grants the other API's authority. The legacy `/qer-actuation` route continues to reject context-, slice-, and DNN-wide selectors and rechecks its exact SUPI while holding the session lock. PCF compatibility remains in its existing separate PCF/backend deployment.

## Configuration

* `SMF_SESSION_QER_NETWORK_SCOPE`: operator-pinned network scope.
* `SMF_SESSION_QER_STATE_DIR`: persistent private (0700) directory. Journal failures block writes; never use an ephemeral directory for production ownership.
* `SMF_SESSION_QER_UPF_ORIGINS`: JSON mapping native UPF node IPv4 addresses to fixed, commissioned `/v1/upf-state` evidence origins. Caller-supplied origins are forbidden.
* `SMF_DYNAMIC_UPF_TEMPLATES`: operator-pinned JSON map from logical UPF target ID to `{node:<factory.UPNode>,linkTo:[existing AN names]}`. Templates require explicit nonoverlapping IPv4 pools and S-NSSAI/DNN capability. Registration creates a real native UPF UUID and starts PFCP association; it does not mean association or traffic succeeded.

The native control extension supports a single authoritative SMF process per configured ownership store. Boot epochs fence stale controller claims across restart. It is not a distributed multi-SMF ownership service.

## Routes under `/nsmf-oam/v1`

* GET `session-qer-capability`; POST `session-qer-inventory` with `{session_keys:[canonical keys]}`; POST `session-qer-actuation` with exact native identity, rates, owner, operation and expected version.
* GET `session-active-inventory`: `source=native-smf-active-sessions`, `complete`, `census_revision`, `network_scope`, `smf_boot_epoch`, `observed_at`, `sessions`. Each session contains native `smContextRef`, `establishedEpoch` (the same Ref), SUPI, PDU session ID, S-NSSAI, DNN, actual UPF UUID/node address/PFCP SEID/kernel epoch and `created_at`. Every active entry must pass current native kernel evidence; concurrent pool add/remove or state transition changes the revision and returns failure. Constructors publish fully initialized contexts. No application classification is inferred.
* GET `upf-control-capability`; POST `upf-control/{read,register,selection,unregister}`. Command selectors use logical `targetId`; writes require `ownerId`, `operationId`, `expectedVersion`. Selection additionally requires `accepting`. Removal requires the same owner/operation, stopped admission, and zero active **and inflight** references/SEIDs.
* POST `session-admissions`; GET `session-admissions/:nonce`; POST `session-admissions/:nonce/release`.

Admission registration body is `{networkScope,supi,pduSessionId,nonce,ownerId,operationId,expectedVersion,targetUpfId,validFrom,validUntil}`. `targetUpfId` is the **actual UUID from native UPF readback**. Register before UE/session creation. Different nonces cannot claim the same scope/SUPI/PDU identity concurrently. Same SUPI with different PDU IDs is allowed. Nonces are immutable and cannot be recycled after use or cancellation.

Native census reads fetch one independent kernel dump per fixed UPF origin within a single HTTP request, retaining the native observation clock. The cache is bounded to 64 origins, 32 MiB and a ten-second acquisition budget; another request acquires new evidence. Actuation before/after reads never share this inventory cache. Topology census releases topology locks before waiting for session locks, then revalidates the topology/ownership snapshot. Cleanly removed UPF targets retain a tombstone; another owner can reuse a target only with its fresh tombstone CAS.

Complete active inventory additionally reads **every configured native UPF**, including UPFs with zero SMF sessions. Kernel session SEID sets must exactly equal the SMF census per UPF; orphan or missing sessions, orphan QERs/PDRs, inflight contexts or topology changes reject completeness. The `upfs` array carries `target_id`, actual `upf_id`, `node_ip`, `upf_epoch`, and original native `observed_at`. This distinguishes a complete shared-NF census from merely verifying selected SMF sessions.

Admission readback has `source=native-smf-session-admission/v1`, native `observed_at`, `boot_epoch`, `registered_at`, immutable `installation_version`, current CAS `version`, and status `registered`, `selected`, `bound`, `expired`, or `cancelled`. `selected` is tentative. `bound` requires a real active SMContext, target UUID and remote PFCP SEID. Binding fields are `smContextRef`, `establishedEpoch`, canonical `session_key`, `created_at`, `upf_id`, `upf_node_ip`, and immutable `installation_version`. Cancellation disables future admission and retains the record; it never tears down an active PDU session.

Canonical session keys are compact JSON `[network_scope,supi,pdu_session_id,smContextRef]`. Measurement windows must start after native session creation and confirmed resource installation. Registration, readiness, association and policy acceptance are installation evidence; actual receiver traffic and service improvement are separate effect evidence.

## Verification boundary

Go tests exercise same-SUPI/two-PDU isolation, native identity/CAS/owner fences, durable QER predecessor and initial baselines, unknown-outcome replay rejection, admission overlap/no-fallback/creation fencing, stale cancellation and wire lineage. These tests use controlled test doubles and do not constitute live data-plane proof. Live acceptance still requires commissioning the fixed origins/persistent store/templates, deploying the patched image, and measuring exact newly bound sessions and restoration.
