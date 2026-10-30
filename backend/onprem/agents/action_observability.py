"""Bounded action evidence captured by tools and read without native probes.

Display evidence never grants installation, cleanup or execution authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

from backend.onprem.agents.contracts import AgentLoopBlocked, _seconds
from backend.onprem.agents.validator import ValidatorAgent
from backend.onprem.policy.group_qer import GroupQerBlocked, _ambr_qer


SCHEMA = "cityverse.action-evidence/v1"
RESOURCE_TOOLS = {"get_k8s_workloads", "get_nf_inventory", "get_nf_health",
                  "get_k8s_resource_usage", "get_nf_resource_usage", "get_n6_status"}
DIAGNOSTIC_TOOLS = {"get_nf_log_excerpt", "get_k8s_events", "get_control_plane_failures"}
STATUSES = {"available", "partial", "stale", "unavailable", "not_configured", "unsupported"}
CATEGORIES = {"authentication", "registration", "pfcp", "ngap", "timeout", "error", "warning", "oom"}
IDENTITY = ("supi", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf", "sst", "sd")
COHORT = ("supi", "scenario", "slice", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "upf", "sst", "sd", "ueIp")


def _text(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value) else None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _time(value):
    try:
        return datetime.fromtimestamp(_seconds(value), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@contextmanager
def _readonly(path):
    """Never initialize storage, change journal mode or keep Windows handles."""
    db = None
    try:
        path = Path(path) if path is not None else None
        if path is not None and path.is_absolute() and path.is_file() and not path.is_symlink():
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        yield db
    finally:
        if db is not None:
            db.close()


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} if db else set()


def _artifact(db, thread, ref):
    if not isinstance(ref, str):
        return {}
    row = db.execute("SELECT value FROM graph_artifacts WHERE thread_id=? AND ref=?", (thread, ref)).fetchone()
    value = json.loads(row[0]) if row else {}
    return value if isinstance(value, dict) else {}


def _display_records(db, thread):
    records, seen = [], set()
    for ref, encoded in db.execute("SELECT ref,value FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'cycle_observability:%' ORDER BY rowid DESC LIMIT 200", (thread,)):
        value = json.loads(encoded)
        if type(value.get("cycle")) is int and value["cycle"] not in seen:
            records.append((ref, value))
            seen.add(value["cycle"])
    if "graph_events" in _tables(db):
        pending = {}
        for encoded, in db.execute("SELECT value FROM graph_events WHERE thread_id=? AND json_extract(value,'$.kind')='decision' ORDER BY ordinal DESC LIMIT 400", (thread,)):
            event = json.loads(encoded)
            cycle = event.get("cycle")
            if type(cycle) is not int or cycle in seen or event.get("phase") not in {"plan", "apply"}:
                continue
            value = pending.setdefault(cycle, {"cycle": cycle})
            for field in ("plan", "before", "action"):
                if event.get(field + "_ref"):
                    value.setdefault(field + "Ref", event[field + "_ref"])
        records.extend((value.get("actionRef") or value.get("planRef"), value) for value in pending.values())
    return records


def _rates_summary(inventory, supis):
    rates = [_ambr_qer(inventory[s]["qers"])["mbrKbps"] for s in supis]
    return {"ueCount": len(rates), "uplinkKbps": {"min": min(r["ul"] for r in rates), "max": max(r["ul"] for r in rates)},
            "downlinkKbps": {"min": min(r["dl"] for r in rates), "max": max(r["dl"] for r in rates)}} if rates else None


def qer_readback(snapshot, installed, before, *, journal_path=None):
    """Join original action/fence to already-saved kernel QER readbacks.

    installed is the immutable graph action artifact, not today's adapter view.
    The existing pure Validator.installation checks every treatment and holdout.
    """
    result = {"status": "missing", "consistency": "unknown", "requested": None, "before": None,
              "readback": None, "observedAt": None, "capturedAt": None, "receiptId": None,
              "reason": "historical_readback_missing", "source": "native-upf-qer", "historical": True}
    try:
        selected, receipt, baseline = installed["selected"], installed["receipt"], installed["qer_before"]
        action_id = receipt["id"]
        original = before.get("_actuatorSnapshot", before)
        if (not _text(action_id) or receipt.get("roundId") != snapshot.get("roundId")
                or (before.get("roundId"), before.get("generation")) != (snapshot.get("roundId"), snapshot.get("generation"))):
            return {**result, "reason": "action_scope_mismatch"}
        treatment = selected["treatmentSupis"]
        result["before"] = _rates_summary(baseline, treatment)
        owners = sorted(({key: row.get(key) for key in COHORT} for row in original["perUe"]), key=lambda row: str(row["supi"]))
        cohort_hash = _hash(owners)
        with _readonly(journal_path) as db:
            if not {"group_qer_actions", "native_qer_receipts"} <= _tables(db):
                return result
            row = db.execute("SELECT record FROM group_qer_actions WHERE id=? AND round_id=?", (action_id, snapshot["roundId"])).fetchone()
            if not row:
                return result
            record = json.loads(row[0])
            fence = record.get("fence", {})
            if (any(fence.get(k) != original.get(k) for k in ("roundId", "generation", "sequence", "policyRevision"))
                    or fence.get("fleetLease") != original.get("fleetLease") or fence.get("perUe") != original.get("perUe")
                    or record.get("profileId") != selected.get("profileId") or record.get("eventId") != selected.get("eventId")
                    or record.get("treatmentSupis") != sorted(treatment)
                    or record.get("holdoutSupis") != sorted(selected["holdoutSupis"])):
                return {**result, "reason": "action_fence_mismatch"}
            requested = record.get("profileRates")
            if (not isinstance(requested, dict) or set(requested) != {"ul", "dl"}
                    or any(type(requested[d]) is not int or not 1 <= requested[d] <= 500000 for d in requested)):
                return {**result, "reason": "requested_rates_missing"}
            result["requested"] = {"profileId": selected["profileId"], "uplinkKbps": requested["ul"], "downlinkKbps": requested["dl"]}
            # Exact action selection is bounded; phase alone is never trusted.
            rows = db.execute("SELECT id,receipt FROM native_qer_receipts WHERE round_id=? AND generation=? "
                "AND json_extract(receipt,'$.actionId')=? AND json_extract(receipt,'$.phase')='installed' ORDER BY id DESC LIMIT 512",
                (snapshot["roundId"], snapshot["generation"], action_id)).fetchall()
            partial = None
            for receipt_id, encoded in rows:
                saved = json.loads(encoded)
                if (saved.get("cohortSha256") != cohort_hash or any(saved.get(k) != original.get(k)
                        for k in ("roundId", "generation", "sequence", "policyRevision"))):
                    continue
                proof = saved.get("proof", {})
                sessions = proof.get("sessions", [])
                if proof.get("source") != "native-upf-qer" or not isinstance(sessions, list):
                    continue
                observed, captured = _time(proof.get("observedAt")), _time(saved.get("capturedAt"))
                if observed is None or captured is None or not -1 <= _seconds(saved["capturedAt"]) - _seconds(proof["observedAt"]) <= 15:
                    continue
                actual = {r.get("supi"): r for r in sessions if isinstance(r, dict)}
                if len(actual) != len(sessions) or set(actual) != set(baseline):
                    continue
                if any(any(actual[s].get(k) != baseline[s].get(k) for k in IDENTITY) for s in baseline):
                    continue
                checker = ValidatorAgent(SimpleNamespace(profiles={selected["profileId"]: requested},
                    total_ues=len(original["perUe"]), maximum_evidence_age_seconds=15), lambda _: proof,
                    clock=lambda: _seconds(saved["capturedAt"]))
                # qer_inventory is reused with a saved proof and its original
                # acquisition clock; this lambda performs no collector calls.
                checker.qer_inventory(original, sealed_rows={r["supi"]: r for r in original["perUe"]})
                view = {**result, "readback": _rates_summary(actual, treatment), "observedAt": observed,
                        "capturedAt": captured, "receiptId": receipt_id}
                try:
                    checker.installation(receipt, selected, baseline, actual)
                except AgentLoopBlocked:
                    partial = partial or {**view, "status": "partial", "consistency": "mismatch", "reason": "incomplete_installation_readback"}
                    continue
                return {**view, "status": "observed", "consistency": "consistent", "reason": None}
            return partial or result
    except (KeyError, TypeError, ValueError, sqlite3.Error, GroupQerBlocked, AgentLoopBlocked):
        return {**result, "status": "unavailable", "reason": "evidence_invalid_or_unavailable"}


def _containers(rows, *, usage=False, identified=False):
    from backend.onprem.agents.observations import _quantity
    result = []
    for row in rows[:16] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        requests, limits = row.get("requests", {}), row.get("limits", {})
        item = {"name": _text(row.get("name")),
                "requests": {"cpuCores": _number(_quantity(requests.get("cpu"), cpu=True)), "memoryBytes": _number(_quantity(requests.get("memory")))},
                "limits": {"cpuCores": _number(_quantity(limits.get("cpu"), cpu=True)), "memoryBytes": _number(_quantity(limits.get("memory")))}}
        if usage:
            item.update(cpuCores=_number(row.get("cpu_cores")) if identified else None,
                        memoryBytes=_number(row.get("memory_bytes")) if identified else None)
        result.append(item)
    return result


def _bindings(snapshot, workloads, observed=None):
    # No slice inference from an NF role or workload/pod name. A native epoch
    # is not a Pod UID; unfenced/shared relationships stay explicitly unknown.
    bindings = {}
    for row in snapshot.get("perUe", []):
        key = tuple(_text(row.get(k)) for k in ("slice", "upf", "upfEpoch"))
        if not all(key):
            continue
        bindings[key] = {"slice": key[0], "upf": key[1], "upfEpoch": key[2],
                         "podUid": None, "workloadRef": None, "status": "unknown", "sharing": "unknown"}
    for binding in bindings.values():
        source = next((b for b in observed or [] if isinstance(b, dict) and
            (b.get("slice"), b.get("upf"), b.get("upfEpoch")) == (binding["slice"], binding["upf"], binding["upfEpoch"])), None)
        if source and source.get("status") == "bound" and source.get("sharing") == "dedicated":
            matched = next((w for w in workloads if w.get("podUid") == source.get("podUid") and w.get("workloadRef") == source.get("workloadRef")), None)
            if matched:
                binding.update(status="bound", sharing="dedicated", podUid=matched["podUid"],
                    workloadRef=matched["workloadRef"], observedAt=_time(source.get("observedAt")), source="pod-native-exporter+incarnation")
        elif source and source.get("sharing") == "shared":
            binding["sharing"] = "shared"
        if binding["status"] == "bound":
            binding["sharing"] = "shared" if sum(b["podUid"] == binding["podUid"] for b in bindings.values()) > 1 else "dedicated"
    return list(bindings.values())[:32]


def project_tool_evidence(tool, result, snapshot, arguments=None):
    """Whitelist normalized observation outputs at the existing tool boundary."""
    if tool not in RESOURCE_TOOLS | DIAGNOSTIC_TOOLS | {"get_environment_overview"} or not isinstance(result, dict):
        return None
    if tool == "get_environment_overview":
        nested = [project_tool_evidence(k, v, snapshot, arguments) for k, v in (result.get("data") or {}).items() if k in RESOURCE_TOOLS]
        return {"kind": "overview", "status": result.get("status") if result.get("status") in STATUSES else "unavailable",
                "observedAt": _time(result.get("observed_at")), "parts": [p for p in nested if p]}
    evidence = {"kind": "resource" if tool in RESOURCE_TOOLS else "diagnostic", "source": tool,
                "status": result.get("status") if result.get("status") in STATUSES else "unavailable",
                "observedAt": _time(result.get("observed_at")),
                "collectedAt": _time(result.get("collected_finished_at")), "workloads": [], "metrics": [], "diagnostics": []}
    arguments = arguments or {}
    evidence.update(slice=arguments.get("slice_id") if arguments.get("slice_id") in {"embb", "urllc", "mmtc", "v2x"} else None,
                    target=_text(arguments.get("target")))
    data = result.get("data")
    if tool == "get_n6_status":
        data = data.get("workloads", []) if isinstance(data, dict) else []
    if tool in RESOURCE_TOOLS:
        for row in data[:100] if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            common = {"workloadRef": _text(row.get("workload_ref")), "podUid": _text(row.get("pod_uid")),
                      "nfType": row.get("nf_type") if row.get("nf_type") in {"amf", "smf", "upf", "nrf", "nssf", "pcf", "ausf", "udm", "udr", "nef", "chf"} else None,
                      "sharing": "unknown" if row.get("nf_type") == "upf" else "shared"}
            if tool in {"get_k8s_resource_usage", "get_nf_resource_usage"}:
                identified = row.get("identity_status") in {"uid_matched", "creation_time_fenced"} and bool(common["podUid"])
                timestamp = _time(row.get("observed_at"))
                usable = identified and bool(timestamp) and evidence["status"] in {"available", "partial"}
                evidence["metrics"].append({**common, "observedAt": timestamp,
                    "identityStatus": row["identity_status"] if identified else "unverified", "status": "observed" if usable else "stale" if evidence["status"] == "stale" else "missing",
                    "containers": _containers(row.get("containers", []), usage=True, identified=usable)})
            else:
                evidence["workloads"].append({**common, "ready": row.get("ready") if type(row.get("ready")) is bool else None,
                    "readyPods": int(row["ready"]) if type(row.get("ready")) is bool else None,
                    "desiredPods": _number(row.get("desired_pods")), "restarts": _number(row.get("restarts")),
                    "ownerReadyPods": _number(row.get("owner_ready_pods")), "replicaScope": "controller" if row.get("replica_scope") == "controller" else "unknown",
                    "phase": row.get("phase") if row.get("phase") in {"Pending", "Running", "Succeeded", "Failed", "Unknown"} else None,
                    "deleting": row.get("deleting") if type(row.get("deleting")) is bool else None,
                    "waitingReasons": [v for v in row.get("waiting_reasons", [])[:8] if v in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "RunContainerError"}],
                    "ownerRefs": [{"kind": o.get("kind") if o.get("kind") in {"ReplicaSet", "Deployment", "StatefulSet", "DaemonSet", "Job"} else "unknown", "uid": _text(o.get("uid"))}
                                  for o in row.get("owner_refs", [])[:4] if isinstance(o, dict)],
                    "containers": _containers(row.get("containers", []))})
        evidence["bindings"] = _bindings(snapshot, evidence["workloads"] + evidence["metrics"], result.get("upf_bindings"))
    elif tool == "get_nf_log_excerpt" and isinstance(data, dict):
        evidence["diagnostics"] = [{"workloadRef": _text(data.get("workload_ref")), "rawTextIncluded": False,
            "categories": {k: min(v, 1000000) for k, v in data.get("categories", {}).items() if k in CATEGORIES and type(v) is int and v >= 0}}]
    elif tool == "get_k8s_events" and isinstance(data, list):
        known = {"BackOff", "FailedScheduling", "Unhealthy", "OOMKilled", "Evicted", "FailedMount", "Killing", "Started", "Created", "Pulled", "Scheduled"}
        evidence["diagnostics"] = [{"workloadRef": _text(r.get("workload_ref")), "category": r.get("reason") if r.get("reason") in known else "unclassified",
                                    "count": _number(r.get("count")), "observedAt": _time(r.get("observed_at"))} for r in data[:100] if isinstance(r, dict)]
    elif tool == "get_control_plane_failures" and isinstance(data, dict):
        known = {"ue.registration_rejected", "ue.pdu_rejected", "ue.authentication_failed", "unclassified"}
        evidence["diagnostics"] = [{"categories": {k: min(v, 1000000) for k, v in data.get("observed_failure_codes", {}).items() if k in known and type(v) is int and v >= 0}, "rawTextIncluded": False}]
    return evidence


def action_evidence(snapshot, *, graph_path=None, journal_path=None, cycle=None, action_id=None, call_id=None):
    """Pure reader for one cycle/action; no providers, tools or model calls."""
    if cycle is not None and (type(cycle) is not int or not 0 <= cycle <= 10000):
        raise ValueError("Invalid action cycle")
    if action_id is not None and not _text(action_id):
        raise ValueError("Invalid action id")
    if call_id is not None and not _text(call_id):
        raise ValueError("Invalid call id")
    answer = {"schemaVersion": SCHEMA, "roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
              "cycle": cycle, "actionId": action_id, "callId": call_id, "qer": {"status": "missing", "consistency": "unknown", "reason": "action_evidence_missing"},
              "resources": [], "diagnostics": [], "toolEvidence": []}
    thread = f"{snapshot.get('roundId')}:{snapshot.get('generation')}"
    try:
        with _readonly(graph_path) as db:
            if "graph_artifacts" not in _tables(db):
                return answer
            installed, before = {}, {}
            if call_id is not None:
                row = db.execute("SELECT value FROM graph_events WHERE thread_id=? AND json_extract(value,'$.call_id')=? ORDER BY ordinal DESC LIMIT 1", (thread, call_id)).fetchone() if "graph_events" in _tables(db) else None
                actual_cycle = json.loads(row[0]).get("cycle") if row else None
                if type(actual_cycle) is not int or cycle is not None and actual_cycle != cycle:
                    answer["qer"]["reason"] = "call_not_found"
                    return answer
                cycle = actual_cycle
                answer["cycle"] = cycle
            for _, display in _display_records(db, thread):
                if cycle is not None and display.get("cycle") != cycle:
                    continue
                action = _artifact(db, thread, display.get("actionRef"))
                identity = action.get("receipt", {}).get("id")
                if action_id is not None and identity != action_id:
                    continue
                cycle = display.get("cycle")
                installed = action
                before = _artifact(db, thread, action.get("before_ref", display.get("beforeRef")))
                answer.update(cycle=cycle, actionId=identity)
                break
            if action_id is not None and not installed:
                answer["qer"]["reason"] = "action_not_found"
                return answer
            if installed:
                answer["qer"] = qer_readback(snapshot, installed, before, journal_path=journal_path)
            elif cycle is not None:
                answer["qer"]["reason"] = "no_policy_write_or_pending_action"
            rows = db.execute("SELECT ref,json_remove(value,'$.savedLogs') FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'tool_observability:%' ORDER BY rowid DESC LIMIT 400", (thread,)).fetchall()
            for ref, encoded in reversed(rows):
                evidence = json.loads(encoded)
                if evidence.get("roundId") != snapshot.get("roundId") or evidence.get("generation") != snapshot.get("generation"):
                    continue
                if cycle is None or evidence.get("cycle") != cycle:
                    continue
                if call_id is not None and evidence.get("callId") != call_id:
                    continue
                # Saved log bodies have their own bounded, scoped page endpoint.
                view = {**{k: v for k, v in evidence.items() if k != "savedLogs"}, "evidenceRef": ref, "actionId": answer["actionId"], "relation": "cycle"}
                answer["toolEvidence"].append(view)
                parts = evidence.get("parts", []) if evidence.get("kind") == "overview" else [evidence]
                for part in parts:
                    item = {**view, **{k: v for k, v in part.items() if k != "savedLogs"}}
                    if part.get("kind") == "resource":
                        answer["resources"].append(item)
                    elif part.get("kind") == "diagnostic":
                        answer["diagnostics"].append(item)
            if cycle is not None and "graph_events" in _tables(db):
                events = []
                query = "SELECT value FROM graph_events WHERE thread_id=? AND json_extract(value,'$.cycle')=?"
                parameters = [thread, cycle]
                if call_id is not None:
                    query += " AND json_extract(value,'$.call_id')=?"
                    parameters.append(call_id)
                for encoded, in db.execute(query + " ORDER BY ordinal DESC LIMIT 200", parameters):
                    event = json.loads(encoded)
                    events.append({k: event.get(k) for k in ("role", "phase", "tool", "status", "cycle", "kind")}
                        | {"callId": _text(event.get("call_id")), "at": _time(event.get("at")), "durationMs": _number(event.get("duration_ms"))})
                answer["events"] = list(reversed(events))
            return answer
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        answer["qer"] = {"status": "unavailable", "consistency": "unknown", "reason": "evidence_invalid_or_unavailable"}
        return answer


def action_summaries(snapshot, *, graph_path=None, journal_path=None):
    """Small selectors for cycle decisions and actual tool calls; no log bodies."""
    from backend.onprem.scenarios.observability import observation_artifact_view
    summaries, by_cycle = [], {}
    thread = f"{snapshot.get('roundId')}:{snapshot.get('generation')}"
    try:
        with _readonly(graph_path) as db:
            if "graph_artifacts" not in _tables(db):
                return []
            for ref, display in _display_records(db, thread):
                cycle = display.get("cycle")
                if type(cycle) is not int:
                    continue
                action = _artifact(db, thread, display.get("actionRef"))
                receipt = action.get("receipt", {})
                plan = _artifact(db, thread, display.get("planRef"))
                before = observation_artifact_view(db, thread, display.get("beforeRef"))
                if (before.get("roundId"), before.get("generation")) != (snapshot.get("roundId"), snapshot.get("generation")):
                    continue
                identity = _text(receipt.get("id"))
                by_cycle[cycle] = identity
                summaries.append({"id": f"{thread}:{cycle}", "decisionId": f"{thread}:{cycle}", "cycle": cycle,
                    "actionId": identity, "callId": None, "at": _time(before.get("observedAt")), "type": "decision",
                    "action": plan.get("action") if plan.get("action") in {"apply", "hold"} else None,
                    "target": _text(plan.get("profile_id")), "scenario": _text(plan.get("group_id")), "slice": None,
                    "evidenceRef": ref, "relation": "action" if identity else "cycle", "targetRefs": [], "role": "planner"})
            for ref, encoded in db.execute("SELECT ref,json_remove(value,'$.savedLogs') FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'tool_observability:%' ORDER BY rowid DESC LIMIT 400", (thread,)):
                evidence = json.loads(encoded)
                if (evidence.get("roundId"), evidence.get("generation")) != (snapshot.get("roundId"), snapshot.get("generation")):
                    continue
                cycle, call = evidence.get("cycle"), _text(evidence.get("callId"))
                if type(cycle) is not int or not call:
                    continue
                summaries.append({"id": "call:" + call, "decisionId": f"{thread}:{cycle}", "cycle": cycle,
                    "actionId": by_cycle.get(cycle), "callId": call, "at": _time(evidence.get("observedAt")), "type": "tool",
                    "action": None, "target": _text(evidence.get("target")) or _text(evidence.get("tool")), "tool": _text(evidence.get("tool")),
                    "scenario": None, "slice": evidence.get("slice"), "evidenceRef": ref, "relation": "call",
                    "targetRefs": evidence.get("targetRefs", []), "role": evidence.get("role")})
            if "graph_events" in _tables(db):
                known_calls = {s["callId"] for s in summaries if s["callId"]}
                for encoded, in db.execute("SELECT value FROM graph_events WHERE thread_id=? AND json_extract(value,'$.kind')='tool' AND json_extract(value,'$.status') IN ('started','completed','failed') ORDER BY ordinal DESC LIMIT 400", (thread,)):
                    event = json.loads(encoded)
                    cycle, call = event.get("cycle"), _text(event.get("call_id"))
                    if type(cycle) is not int or not call or call in known_calls:
                        continue
                    known_calls.add(call)
                    summaries.append({"id": "call:" + call, "decisionId": f"{thread}:{cycle}", "cycle": cycle,
                        "actionId": by_cycle.get(cycle), "callId": call, "at": _time(event.get("at")), "type": "tool",
                        "action": None, "target": _text(event.get("tool")), "tool": _text(event.get("tool")),
                        "scenario": None, "slice": None, "evidenceRef": None, "relation": "call",
                        "targetRefs": event.get("targetRefs", []), "role": event.get("role")})
            return sorted(summaries, key=lambda r: (r["cycle"], r["at"] or "", r["id"]))[-200:]
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return []
