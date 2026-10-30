# Pinned WebConsole subscriber contract

The source of truth is free5GC v4.2.2's WebConsole submodule
`3878732bd6b16e6c9c26e87c2d107e58a9965f33`:

- [SubsData model](https://github.com/free5gc/webconsole/blob/3878732bd6b16e6c9c26e87c2d107e58a9965f33/backend/WebUI/model_subs_data.go)
- [Authentication web model](https://github.com/free5gc/webconsole/blob/3878732bd6b16e6c9c26e87c2d107e58a9965f33/backend/WebUI/model_web_auth_subs.go)
- [HTTP handlers](https://github.com/free5gc/webconsole/blob/3878732bd6b16e6c9c26e87c2d107e58a9965f33/backend/WebUI/api_webui.go)
- [Routes](https://github.com/free5gc/webconsole/blob/3878732bd6b16e6c9c26e87c2d107e58a9965f33/backend/WebUI/routers.go)

The Go field is named `WebAuthenticationSubscription`, but its JSON tag is
`AuthenticationSubscription`. The provisioner correctly sends the latter.
Creation explicitly calls `POST /api/subscriber/{supi}/20893/1`; both the route
without `/1` and with `/1` exist, but the explicit suffix fixes the intended count.
Each payload supplies a unique one-element `gpsis` list because the handler
indexes `Gpsis[0]`. Omitting it can fail before the subscriber is written.

An absent subscriber's individual GET can return HTTP 200 with zero-value
`SubsData`, so HTTP 404 is not used as the existence gate. The provisioner first
reads `/api/subscriber`, checks unique identities, creates missing entries,
reads each profile back, and verifies the final 1000-identity pool. It compares
managed fields while ignoring unrequested response defaults such as encryption
metadata and empty flow arrays; malformed fields and duplicate entries fail.

## Authentication and secret handling

Use `CITYVERSE_ONPREM_WEBCONSOLE_URL` plus either:

- `CITYVERSE_ONPREM_WEBCONSOLE_TOKEN`, supplied via Secret; or
- `CITYVERSE_ONPREM_WEBCONSOLE_CREDENTIALS_FILE`, a mounted JSON object with
  `username` and `password`; or
- `CITYVERSE_ONPREM_WEBCONSOLE_USERNAME` and
  `CITYVERSE_ONPREM_WEBCONSOLE_PASSWORD`, both injected by the environment.

Without a token, the helper sends `POST /api/login` and keeps the returned
`access_token` only in memory. Credential values are not CLI arguments, printed
reports or exception messages. The transport does not disable TLS verification.

Run explicitly after mounting the separate four-group UE credential Secret:

```text
python -m backend.onprem.operations.provisioning.subscribers --credentials-file /credentials/groups.json --apply
```

## Existing-subscriber safety and current limitation

The individual GET reads `webAuthenticationSubscription`; its sequence number
is a **web copy**, not the live authentication state used by UDR/UDM. The pinned
PUT/PATCH handler converts that web copy back to runtime authentication data.
Replaying its SQN could undo AKA progress. Therefore unchanged subscribers are
skipped with no writes; drift stops provisioning rather than issuing an unsafe
PUT/PATCH. Automatic safe drift repair remains outstanding and requires an
atomic targeted database update that preserves live authentication fields.

The summary reports `webCredentialsMatched: true` and
`runtimeCredentialsVerified: false`, not `credentialsMatched: true`. Only the
independent runtime Mongo reader may establish the latter for the orchestration
gate. Successful WebConsole read-back alone does not prove live AKA credentials.

The aggregate `GET /api/subscriber` has a bounded 30-second timeout: the real
1000-record lab response took 9.031 seconds and intermittently exceeded the
former 10-second limit. Other requests remain bounded at 10 seconds. No write
is automatically retried; after a transport failure, inspect existing records
and preserve them before resuming. An aggregate timeout is not reported as a
successful provisioning run.

Tests model the pinned HTTP shapes, check 1000 unique creation/idempotency,
default-field projection, drift refusal, independent advanced runtime SQN,
malformed/duplicate responses and secret-safe login. These are contract tests;
live deployment read-back remains an integration gate.
