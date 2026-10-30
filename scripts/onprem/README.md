# On-prem launchers

## Current native kubeadm runtime

The current native evidence/UE workflow uses `start_native_backend.py` against
the explicit kubeadm context, not the legacy kind switch described below.

```bash
python3 scripts/onprem/start_native_backend.py --check-only
python3 scripts/onprem/start_native_backend.py --enable-mutations --enable-traffic --policy-forwards
```

The second command enables controller operations for future UI requests; it does
not submit UE intent. Keep it running. Ctrl-C stops only its owned backend and
loopback forwards. `--policy-forwards` exposes observation/callback transports;
it does not enable static or AI policy capabilities. Admission remains subject
to the backend's verified/commissioning gates, not the 500-UE application budget.

This launcher inherits the parent process environment. Its backend child loads
root `.env` with `override=False`: a parent environment value wins over `.env`.
It does not read a separate `--env-file`. The current native UE controller and
four baseline gNBs use free-ran-ue. Do not switch the UE image while owned UEs or
traffic remain: cleanup checks the Pod image identity. Restarting the backend
does not render or apply AMF Deployments. See the
[AMF patch boundary](../../backend/onprem/images/patches/amf-stale-context-map.md)
for the separate AMF overlay.

The application request ceiling is 500 UEs across baseline plus all five
events. Trusted, explicit commissioning may set a candidate admission ceiling
of 100, 250, or 500 UEs, but `verifiedMaxUes` remains 1. This changes request
validation, not the verified capacity or policy scope: the current batch
traffic worker still has a 50-UE/100-job bound and AI policy commissioning
remains limited to its existing two-UE profile. The separate private free-ran-ue
500-UE attach/PDU/N6 result cannot validate a 500-UE web scenario or AI
decision/effect E2E.

## Ubuntu / WSL application launchers

Run inside Ubuntu/WSL from the repository root:

```bash
bash scripts/onprem/start-onprem.sh --preflight-only
bash scripts/onprem/start-onprem.sh
bash scripts/onprem/stop-onprem.sh
```

Requires Linux Python 3.10+, Linux Node.js >=22.12, npm, and frontend dependencies
installed with `cd frontend && npm ci`. Activate the backend virtualenv first,
or set `ONPREM_PYTHON=/path/to/venv/bin/python`. Windows Node/npm under `/mnt/`
are rejected. The PowerShell launchers have been removed.

Defaults: backend `http://127.0.0.1:8000`, frontend `http://127.0.0.1:5173`.
The default backend command is the selected Python's
`-m backend.onprem.api.server`. This application-only mode disables local
Kubernetes integration; it does not deploy infra or validate native 5GC.
Frontend runs `npm run build` then `npm run start` (`server.mjs`). Its listener
and API proxy receive `PORT`, `FRONTEND_HOST`, and `CITYVERSE_BACKEND_URL`.

For a native backend already running with the Python supervisor above:

```bash
bash scripts/onprem/start-onprem.sh --use-existing-backend --backend-port 8000
```

This checks backend `/health`, launches only the frontend, and never stops the
external backend. No OpenAI key is needed by the launcher in this mode.
To manage a native supervisor, pass `--backend-command` with its complete
`start_native_backend.py` command and explicit cluster/profile arguments. It
must remain in the foreground, and its API port must match `--backend-port`.
Custom commands inherit their own Kubernetes configuration. Infra remains
separately managed with `infrastructure/onprem/` Helm tooling.

Options (`--help` lists all): `--env-file`, `--backend-host`, `--backend-port`,
`--frontend-host`, `--frontend-port`, `--backend-command`, `--frontend-command`,
`--state-file`, `--readiness-timeout` (default 120 seconds per service).
Command overrides also support `ONPREM_BACKEND_COMMAND` and
`ONPREM_FRONTEND_COMMAND`. Pass the same `--state-file` to stop isolated runs.

Root `.env` or `--env-file` supports literal single-line dotenv values, with
no shell execution or variable expansion. Existing environment values win.
Managed backends require `OPENAI_API_KEY`; default model is `gpt-5.5` if unset.
The frontend receives only a public environment allowlist. Runtime state
contains process identities and URLs, not commands or dotenv values. Keep
credentials out of command arguments.

State/logs default to `.tmp/onprem/runtime.json` and `.tmp/onprem/logs/`.
The stdlib helper `service_launcher.py` serializes operations with a lock and
uses isolated Linux process groups with boot ID/PID/start-time checks.
Startup waits for backend `/health` and frontend `/`; failure cleans owned
groups. Stop sends TERM, then KILL after a bounded grace period. It never
scans ports to kill services. Commands must not daemonize or escape their
process group. Complete native UE/traffic cleanup before stopping its backend.
Stop does not remove infra. Preflight checks tools/configuration/ports only.

Migration scope and verification:

| Requirement | Implementation |
| --- | --- |
| REQ-001: Ubuntu/WSL Bash entry points | `start-onprem.sh`, `stop-onprem.sh`; Linux-only stdlib helper |
| REQ-002: frontend listener/proxy and secret boundary | explicit public environment allowlist and current `server.mjs` variables |
| REQ-003: owned lifecycle and failure cleanup | lock, process groups, boot/PID/start-time identity, bounded readiness |

```bash
python3 -m unittest discover -s scripts/onprem/tests -p test_service_launcher.py -v
shellcheck scripts/onprem/start-onprem.sh scripts/onprem/stop-onprem.sh
bash -n scripts/onprem/start-onprem.sh
bash -n scripts/onprem/stop-onprem.sh
```

The scripts use custom generation because lifecycle/state management crosses
Bash and Python. [Ref: docs/bash-scripting-guide.md -> Strict Mode and Error Handling]
from the `bash-script-generator` skill. WSL validation uses direct Bash syntax
checks and system ShellCheck; the skill's validator wrapper has CRLF endings.

## R95 current-state and memory validation (2026-10-27)

The deployed R95 trial completed five genuine AI cycles with 50 UEs, then
3601.047 real seconds of persistent traffic with zero failed native windows.
The independent strict receipt verifier passed; cleanup was verified and the
fleet returned to zero. This was a normal no-write policy-maintained scenario,
not a constrained QER-effect or capacity promotion. Evidence is retained in
`.tmp/live-soak-20261026-r95b/`, including `report.html` and `e2e-audit.json`.
The original runner summary remains failed because it compared a stale local
sequence after the blocking hold hook. The runner now rereads and fences the
current owner before stop; 43 related regression tests passed. That harness
fix has not itself been rerun through another full hour.

Topology polling uses `RoundLedger.ownership_control` rather than hydrating
historical reports. The tuned worker publishes its release file atomically
and keeps timeout children in the verified supervisor process group. The
Linux backend inherits `MALLOC_ARENA_MAX=4`, `MALLOC_MMAP_THRESHOLD_=131072`,
and `MALLOC_TRIM_THRESHOLD_=131072` from its launch environment, before Python
starts; setting these later through backend dotenv loading is insufficient.
During the hour, RSS ranged from 283.61 to 383.59 MiB, with 300.09 MiB at
start and 318.85 MiB at end. Historical hydration counters stayed zero. The
small positive trend does not establish all-day memory stability. Early
external iperf syscall tracing is disclosed in the report.


### R101 controlled AI slicing evidence (2026-10-27)

The controlled 50-UE probe completed five genuine AI evaluations: one concert-group QER intervention and four monitored holds. Fixed offered traffic was 100 kbps per UE per direction against a deliberately installed 50 kbps fixture. The six treatment UEs increased aggregate UL/DL receiver throughput from approximately 0.3168 to 0.6015 Mbps; all five net gains were 89.89–89.90 percentage points, and the largest protected-group regression was 0.018%. Original QER/PDR restoration, fixture release, generation-289 fleet zero, empty queue and clear quarantine were independently verified. This does not promote capacity or establish natural-congestion/application-SLA performance.

R98 stopped after three successful effect evaluations when accumulated closed traffic scratch exceeded the N6 64Mi EmptyDir limit. Recovery now supports exact terminal evicted receiver identities and excludes terminal historical Pods from live receiver selection. R101 retains the 64Mi limit and reclaims only already-closed reservations past their immutable collection deadline plus 60 seconds. All four N6 Pods stayed unchanged with zero restarts, peaked at no more than 1.56 MiB of scratch, and each reclaimed 35 reservations.

The original R101 runner summary remains failed: its final verifier assumed a nonpersistent passed outcome and immediate clean terminal UI. The live execution and cancelled-after-five-cycles receipts were not rewritten. A corrected strict verifier, separately captured read-only terminal UI, and independent cross-group audit accepted the same native evidence; original receipt SHA256 values were checked. See `.tmp/ai-slicing-20261027-r96/report.html`, `r101/acceptance-summary.json`, `r101/audit-receipts-v2/verification.json`, and `r101/final-readback.json`. The 43 persistent-verifier regression tests pass. The full batch suite still has the same 32 failures reproduced with the prior code; the separate health AI indicator remains unverified despite verified agent response provenance. These are not claims of complete production readiness.
