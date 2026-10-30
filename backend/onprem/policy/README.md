# Policy

Current policy adapters and safety checks. AI reasoning and workflow live in `agents/`; traffic measurement lives in `traffic/`.

| File | Responsibility |
| --- | --- |
| `group_qer.py` | `GroupQerAdapter` binds UEs for the current round, records policy intent, and applies, reads back, and restores QER. |
| `group_qer_provider.py` | `OnpremGroupQerProvider` connects to real SMF and UPF services and provides commissioned capabilities and native observations. |
| `identity.py` | Canonical JSON and fleet lease identity validation shared by production and legacy adapters. |
| `errors.py` | Shared `PolicyBlocked` and `InvalidPolicyDecision` errors without importing legacy policy implementations. |
| `safety.py` | Checks persisted policy inventory and quarantine; missing or corrupt records are not treated as safe. |
| `absence.py` | Read-only PCF absence checks for known native bindings; historical incident proof delegates on demand to `archive/legacy/policy/pcf_history.py`. |

Legacy N5, oracle, and commissioning logic moved to `archive/legacy/policy/`. Normal and constrained acceptance fixtures live in `validation/`; AF callback persistence lives in `storage/callbacks.py`.

`application/providers.py` and `agents/startup.py` assemble these adapters. Controllers do not create policy adapters directly. This module has no standalone public HTTP API.
