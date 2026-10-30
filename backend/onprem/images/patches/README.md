# NRF 200 update ID preservation

## PCF UDR policy GET transport retry (2026-10-18)

In the generation 142, 50-UE URLLC round, one UE did not establish a PDU. The PCF's AM policy GET to UDR failed with `http2: client conn could not be established`, so no AM policy was created. The later SM policy request received `POLICY_CONTEXT_DENIED`. The round was canceled and its failure result retained.

`pcf-udr-policy-get-retry.patch` applies to PCF `df6c0daacbc7fe5f610f19665e169f013eb05721` with the existing OAM patch and covers AM and SM policy GETs. It retries only when `*url.Error` contains that connection-establishment error, for at most three attempts with 50/100 ms waits, while honoring context cancellation. HTTP error responses and policy writes are not retried.

`test/api/pcf_udr_policy_get_test.go` uses the real PCF consumer, OpenAPI client, and an HTTP server to test success after a transient error, retry exhaustion, 503, and 401 for both GETs. Before the fix, four cases failed and four passed; afterward, **all eight passed**.

With zero UEs and no UE Pods, the image was deployed using Deployment UID, resourceVersion, and old-image CAS checks; Ready was read back and the Recreate strategy retained:
`docker.io/cityverse/free5gc-pcf@sha256:832a8ed1a5f8191a3ced78d82c341f41568dbab9d16ea3d12c3bfd45644111de`。
The rollback image is
`docker.io/cityverse/free5gc-pcf@sha256:ca5b9622ed9dda89143730bbf080a9b001220ddc833402ddacf7b046fec9d6d9`。
Deployment and source, binary, and OCI receipts are stored in `.tmp/live-ai50-20261018/r20/`. Local API tests do not establish a passed continuous AI E2E run.

## AUSF NRF discovery transport retry (2026-10-18)

In the generation 116, 50-UE round, one UE received Registration Reject. AUSF and AMF records showed that the AUSF-to-NRF UDM discovery GET failed with `http2: client conn could not be established`; the UE key was not mismatched.

Apply `ausf-nrf-discovery-retry.patch` to the v1.4.3 source with the NRF ID fix above. It retries only that connection-establishment error twice, waiting 50/100 ms and honoring context cancellation. HTTP responses and authentication writes are not retried. If the source came from a Windows cache, normalize `nrf_service.go` to LF before applying `patch --fuzz=0`.

`test/api/ausf_nrf_discovery_test.go` covers four HTTP boundary cases. The two retry cases failed before the fix; all four passed afterward.

The deployed image is:
`docker.io/cityverse/ausf@sha256:e783724a36cc0b2eef4c6b16fc208097274a03b7fc9be3a027fbd6703031a6d9`。
The operator inventory's `ausfImage` can select this pin. The deployment was updated with UID/resourceVersion CAS checks at zero UEs, using one replica and Recreate; the NRF ID fix remains. This local fix and its API tests do not establish a passed 50-UE AI E2E run.

## Existing ID and other NF patches

The pinned NRF persists NF profiles. On a core restart, registering the same
stable NF ID returns HTTP 200 with no `Location`. Upstream AUSF and AMF used
the ID parsed from `Location` unconditionally, replacing their valid local ID
with an empty string. The next UE authentication request then sent an empty
`ausfInstanceId` or `AmfInstanceId` and UDM rejected it with HTTP 400.

Apply `ausf-nrf-200-id.patch` to free5GC AUSF v1.4.3 (`44dd406a`) and
`amf-nrf-200-id.patch` to the existing CityVerse CAS+AMBR+NSSF AMF source
(`02675d949f207a95379af458c7e80fabe802d728` plus those reviewed changes).
Both patches retain the configured stable ID when NRF returns no new ID;
non-empty IDs from HTTP 201 are still accepted.

Initial, digest-pinned overlays built from these sources:

- AUSF: `docker.io/cityverse/ausf@sha256:91e0a48f7ed5f8764533b892b552d372e7e7b987fc9d7c52749c64d1e5252777`
- AMF: `docker.io/cityverse/amf@sha256:5b8b8ba320b8b63166494f8567af70e7239dfee8d2a0e920d328f73f5d514247`

The 250-UE retest exposed a separate AMF T3560 race: the local UE could send
Authentication Response before AMF assigned the timer, and that late timer
would later remove an already registered UE (leaving its SMF/UPF session).
`amf-t3560-pre-downlink.patch` arms Authentication and Security Mode timers
before sending NAS. It is layered on the NRF-ID-fixed AMF; the resulting live
digest is `docker.io/cityverse/amf@sha256:d9b68b3d5d137e1d801c51bce39f87f89633a5f14cd1454b005cfa2066c3b0cd`.

UDM NRF discovery GET also had a transient HTTP/2 connection-establishment
failure at 250 UE. `udm-nrf-discovery-retry.patch` applies to UDM v1.4.3
(`630da428`), retrying only that transport error twice with short delays;
semantic NRF responses are not retried. The deployed immutable UDM digest is
`docker.io/cityverse/udm@sha256:3acc3f32cd70dbd5e7bf196445969ac56e281d91255bd6f9d16244264e4c69a5`.

The lab inventory selects the final AMF/UDM digests below and the AUSF digest
above. Image build receipts and test
sources are retained under `.cache/free-ran-ue-preflight/diagnostics/ausf-nrf-id`
and `.cache/free-ran-ue-preflight/diagnostics/amf-nrf-id`.
The subsequent AMF and UDM build receipts and test sources are under
`.cache/free-ran-ue-preflight/diagnostics/amf-timer-fix` and
`.cache/free-ran-ue-preflight/diagnostics/udm-nrf-retry`.

The next 100-UE run revealed an independent SMF-to-UDM SDM GET HTTP/2
connection-establishment failure. The historical inventory-v1 SMF build applied
`smf-udm-sdm-get-retry.patch` after the read-only inventory patch on pinned SMF
`2232d299`; it retries only that transport error on the idempotent GET.
The resulting inventory-v2 SMF binary is layered on the deployed image as
`docker.io/cityverse/free5gc-smf@sha256:9f221da946c513842d68e0bfb32effcfc9159a6d99a60aa74d847ff0640c398a`.
Build receipt and test source:
`.cache/free-ran-ue-preflight/diagnostics/smf-sdm-retry`.

Earlier UDM logs also showed an HTTP/2 connection-establishment failure on
the idempotent UDR authentication-status PUT. Layer
`udm-udr-auth-put-retry.patch` after `udm-nrf-discovery-retry.patch` on UDM
v1.4.3. The final UDM digest, including both bounded transport retries, is
`docker.io/cityverse/udm@sha256:08e37bd4f030e91d88a54aaba66f97cb5cf88ca87a39af6ce6778aba15bf5397`.
Build receipt and test source: `.cache/free-ran-ue-preflight/diagnostics/udm-auth-retry`.

The next 100-UE run exposed AMF-to-NRF AUSF discovery failing with the same
HTTP/2 connection-setup error. Layer `amf-nrf-discovery-retry.patch` after the
AMF timer patch; it retries only that error on the idempotent NRF GET. Final
AMF digest: `docker.io/cityverse/amf@sha256:2cbb81d389c899b24690ba3f8de175a7a5f4e7160a163c6b109dfcbf1efaefd6`.
Build receipt and test source:
`.cache/free-ran-ue-preflight/diagnostics/amf-nrf-discovery-retry`.

The 250-UE run and gen25 50-UE formal round each exposed an independent AMF
PDU-path NSSF GET failing before its first HTTP/2 stream was established. Layer
`amf-nssf-pdu-get-retry.patch` on the pinned AMF source after the existing AMF
patches. It retries only an idempotent NSSF GET whose `*url.Error` wraps the
exact new-connection failure, once after 100 ms and within a 21-second bound.
HTTP responses, other transport errors, and cancelled contexts are never
retried. The reviewed build-only candidate manifest is
`sha256:10bcb6eab8fcd1b94db358ee72aaeac6f4220c9bdddeba293d6a5001d38a812b`;
it has not been deployed. Build receipt and tests are retained under
`.cache/free-ran-ue-preflight/diagnostics/amf-nssf-retry`.

## 2026-10-15 live UDM correction

The live 100-UE rerun found the upstream UDM image still deployed. A UDR
authentication-subscription GET failed before its HTTP/2 connection was
established, leaving gen48 at 99 UEs. After cancelling and retiring that fleet,
the existing reviewed `udm-udr-auth-get-retry.patch` image was deployed:

`docker.io/cityverse/udm@sha256:c46ec013fb286c7ff61ed59998b0fda06a5a148cce34d9e5105732a98b77a810`.

The live `/free5gc/udm` SHA256 matches the build receipt:
`83df6d059a27a4ae26e3a2592faee09ab7c7778e068f5b59bf8fa87871adb3d9`.
The WSL inventory example and AI100 E2E preflight now pin this image. Gen50
registered all 100 UEs; gen52 exposed a separate AMF-to-AUSF authentication
POST connection failure, which this UDM patch does not address. Current test
evidence is in `test/RESULTS-2026-10-15-live-rerun.md`.

## 2026-10-18 AMF NGAP field diagnostics

`amf-ngap-encode-diagnostic.patch` adds field identification after a full
InitialContextSetupRequest encoding has already failed. Successful packets
follow the original encoder. Isolated IE encodings are diagnostic only: no
partial packet is returned or transmitted, and no UE data or security key is
logged. It does not fix an empty mandatory IE or change registration policy.

The protocol regression builds and decodes a complete valid request and
checks that an empty AllowedNSSAI returns no packet and identifies IE 0.
The Linux/amd64 candidate is statically compiled with Go 1.25.5 and
CGO_ENABLED=0 over the exact live AMF source and image
`sha256:31397b443b52c63697cfc5fc11df69b110bf9044f77edfacd9f33069e77b21de`.
The diagnostic image is
`docker.io/cityverse/amf@sha256:3d7da6dafe359148b7a8f506b1818d273080212e14d47a35fd27f340f7498382`.
It passed a read-only isolated runtime `--help` smoke and is deployed in the
WSL lab; this is not a successful 50-UE AI E2E or a resolved NGAP root cause.
Build and deployment receipts are private under
`.tmp/live-ai50-20261018/r37-static/`; the earlier dynamically linked candidate
failed to load and is not the running candidate.

### AMF SM-context creation: first HTTP/2 connection establishment

`amf-smf-create-connect-retry.patch` handles the observed gen180 AMF→SMF
`http2: client conn could not be established` URL transport error only.
The pinned x/net/http2 v0.47.0 source emits it before allocating the first
stream (`nextStreamID == 1`). The consumer rebuilds the SDK POST for at most
three attempts with 50/100 ms backoff, respecting cancellation. EOF,
timeouts, HTTP failures and other ambiguous results are never replayed.

Six HTTP API contract cases and the existing NGAP diagnostic regression
passed. The static binary SHA-256 is
`1934fcc77124bc5b0112229d474598e86d13ccb40c27c7c76d385aa3d2565d42`.
The WSL lab now runs
`docker.io/cityverse/amf@sha256:e072c9fbe19d2829ec5f59d31f006041155137fbdaa73c36dafb7eb8765170d2`,
which retains the NGAP diagnostic patch. CRI digest verification, isolated
read-only runtime smoke and rollout passed. The prior `3d7da6da…` image is
retained for rollback. Receipts are under `.tmp/live-ai50-20261018/r41/`.
This patch alone does not establish a passed live AI E2E or the underlying
reason for the connection closing.
