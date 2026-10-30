"""Evidence-only topology, explicit tool targets and saved workload log pages."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from backend.onprem.agents.action_observability import _readonly, _tables, _time, _text
from backend.onprem.agents.contracts import _seconds
from backend.onprem.scenarios.contracts import SCENARIO_SLICES

NODE_IDS = ("gnb", "amf", "smf", "pcf", "nssf", "udm-ausf", "nrf", "upf", "dn")
NF_NODES = {"amf": "amf", "smf": "smf", "pcf": "pcf", "nssf": "nssf",
            "udm": "udm-ausf", "ausf": "udm-ausf", "nrf": "nrf", "upf": "upf"}
ROLES = {"planner", "executor", "validator"}
TARGET_KEYS = {"mapNodeId", "scenario", "slice", "workloadRef", "podUid", "shared"}


def clean_targets(value):
    """The event channel accepts identifiers, never model text or arbitrary keys."""
    if not isinstance(value, list) or len(value) > 8:
        return []
    result = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != TARGET_KEYS
                or row.get("mapNodeId") not in NODE_IDS or type(row.get("shared")) is not bool
                or any(row.get(k) is not None and _text(row[k]) is None
                       for k in TARGET_KEYS - {"mapNodeId", "shared"})):
            return []
        if row not in result:
            result.append(dict(row))
    return result


def evidence_targets(evidence, snapshot=None):
    """NF roles and dedicated slice attribution come from captured identities."""
    result = []
    parts = evidence.get("parts", []) if evidence.get("kind") == "overview" else [evidence]
    for part in parts:
        for row in part.get("workloads", []) + part.get("metrics", []):
            node = NF_NODES.get(row.get("nfType"))
            if node is None or not _text(row.get("podUid")) or not _text(row.get("workloadRef")):
                continue
            bound = [b for b in part.get("bindings", []) if b.get("status") == "bound"
                     and b.get("podUid") == row.get("podUid") and b.get("workloadRef") == row.get("workloadRef")]
            scopes = bound if node == "upf" and bound else [{}]
            for scope in scopes:
                scenarios = sorted({r.get("scenario", r.get("eventId")) for r in (snapshot or {}).get("perUe", [])
                    if r.get("slice") == scope.get("slice") and r.get("upf") == scope.get("upf")
                    and r.get("upfEpoch") == scope.get("upfEpoch") and _text(r.get("scenario", r.get("eventId")))})
                for scenario in scenarios or [None]:
                    result.append({"mapNodeId": node, "scenario": scenario, "slice": _text(scope.get("slice")),
                        "workloadRef": _text(row.get("workloadRef")), "podUid": _text(row.get("podUid")),
                        "shared": not bool(scope) or not bool(scenarios)})
    return clean_targets(result[:8])


def resolve_targets(snapshot, arguments, known=()):
    """Resolve explicit arguments only; tool names never determine target nodes."""
    result = []
    target = arguments.get("target")
    if target is not None:
        result.extend(row for row in known if target in (row.get("workloadRef"), row.get("mapNodeId"))
                      or NF_NODES.get(target) == row.get("mapNodeId"))
    scenario = arguments.get("group_id")
    slice_id = arguments.get("slice_id") or SCENARIO_SLICES.get(scenario)
    if slice_id:
        owners = [r for r in snapshot.get("perUe", []) if r.get("slice") == slice_id
                  and (not scenario or r.get("scenario", r.get("eventId")) == scenario) and r.get("upf") and r.get("upfEpoch")]
        if owners:
            matching = [r for r in known if r.get("mapNodeId") == "upf" and r.get("slice") == slice_id]
            result.extend([{**r, "scenario": _text(scenario)} for r in matching] or [{
                "mapNodeId": "upf", "scenario": _text(scenario), "slice": slice_id,
                "workloadRef": None, "podUid": None, "shared": True}])
    return clean_targets(result[:8])


def redact_log_lines(raw, *, limit=100):
    """Bound and redact at collection AND publication; never retain raw secrets."""
    raw = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[redacted-private-key]", str(raw)[-65536:])
    result = []
    for line in raw.splitlines()[-min(limit, 100):]:
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        # A credential value may be quoted, escaped or contain whitespace. Keep
        # the diagnostic prefix and discard the entire remaining field suffix;
        # tokenizing at whitespace would retain part of a real password.
        line = re.sub(r"(?i)(\b(?:authorization|api[_-]?key|token|password|secret|credential|private[_-]?key)['\"]?\s*[:=]\s*).*$", r"\1[redacted]", line)
        line = re.sub(r"(?i)(\bbearer\s+)[^\s,;\"']+", r"\1[redacted]", line)
        line = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[redacted]@", line)
        line = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted-token]", line)
        line = re.sub(r"(?i)\b(?:imsi-|supi-|msisdn-)?\d{10,16}\b", "[redacted-id]", line)
        line = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[redacted-ip]", line)
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line)
        result.append(line[:4096])
    return result


def saved_tools(snapshot, graph_path):
    thread = f"{snapshot.get('roundId')}:{snapshot.get('generation')}"
    try:
        with _readonly(graph_path) as db:
            if "graph_artifacts" not in _tables(db):
                return []
            rows = db.execute("SELECT ref,json_remove(value,'$.savedLogs') FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'tool_observability:%' AND json_extract(value,'$.kind') IN ('resource','overview') ORDER BY rowid DESC LIMIT 400", (thread,))
            return [(ref, e) for ref, raw in rows if isinstance(e := json.loads(raw), dict)
                    and (e.get("roundId"), e.get("generation")) == (snapshot.get("roundId"), snapshot.get("generation"))]
    except (sqlite3.Error, ValueError, TypeError):
        return []


def node_states(snapshot, graph_path, *, now=None):
    """Latest captured workload state, scoped independently from agent activity."""
    now = time.time() if now is None else now
    states = {}
    for ref, evidence in saved_tools(snapshot, graph_path):
        for part in evidence.get("parts", []) if evidence.get("kind") == "overview" else [evidence]:
            stamp = _time(part.get("observedAt"))
            for row in part.get("workloads", []):
                node = NF_NODES.get(row.get("nfType"))
                if node is None:
                    continue
                targets = [r for r in evidence_targets(part, snapshot) if r.get("podUid") == row.get("podUid")]
                for target in targets:
                    key = (node, target["slice"], target["podUid"])
                    if key in states:
                        continue
                    fresh = stamp is not None and 0 <= now - _seconds(stamp) <= 15
                    status, reason = "unknown", "pod_state_missing"
                    if not fresh or part.get("status") == "stale":
                        reason = "stale_evidence" if stamp else "timestamp_missing"
                    elif part.get("status") not in {"available", "partial"}:
                        reason = "source_unavailable"
                    elif row.get("phase") == "Failed" or row.get("waitingReasons"):
                        status, reason = "error", "pod_failed_or_waiting"
                    elif row.get("phase") == "Running" and row.get("ready") is True and row.get("deleting") is not True:
                        status, reason = "running", "pod_ready"
                    elif row.get("phase") == "Running" and row.get("ready") is False:
                        status, reason = "error", "pod_not_ready"
                    elif row.get("deleting") is True:
                        reason = "pod_terminating"
                    states[key] = {**target, "status": status, "reason": reason, "observedAt": stamp,
                        "source": "saved-pod-state", "evidenceRef": ref, "callId": evidence.get("callId"), "cycle": evidence.get("cycle")}
    represented = {r["mapNodeId"] for r in states.values()}
    return list(states.values())[:100] + [{"mapNodeId": node, "scenario": None, "slice": None,
        "workloadRef": None, "podUid": None, "shared": True, "status": "unknown", "reason": "no_saved_evidence",
        "observedAt": None, "source": None, "evidenceRef": None, "callId": None, "cycle": None}
        for node in NODE_IDS if node not in represented]


def action_logs(snapshot, *, graph_path=None, journal_path=None, cycle=None, action_id=None,
                call_id=None, role=None, after=0, limit=100):
    from backend.onprem.agents.action_observability import action_evidence
    answer = {"schemaVersion": "cityverse.action-logs/v1", "roundId": snapshot.get("roundId"),
        "generation": snapshot.get("generation"), "cycle": cycle, "actionId": action_id, "callId": call_id,
        "role": role, "status": "not_captured", "reason": "log_text_not_captured", "relation": "action-time",
        "sources": [], "lines": [], "nextCursor": after, "hasMore": False, "gap": False}
    detail = action_evidence(snapshot, graph_path=graph_path, journal_path=journal_path,
        cycle=cycle, action_id=action_id, call_id=call_id)
    if detail.get("qer", {}).get("reason") in {"call_not_found", "action_not_found"}:
        answer.update(status="unavailable", reason=detail["qer"]["reason"])
        return answer
    selected_cycle = detail.get("cycle")
    if selected_cycle is None:
        return answer
    thread = f"{snapshot.get('roundId')}:{snapshot.get('generation')}"
    lines, sources, scanned = [], [], after
    try:
        with _readonly(graph_path) as db:
            if "graph_artifacts" not in _tables(db):
                return answer
            query = ("SELECT rowid,ref,value FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'tool_observability:%' "
                     "AND rowid>=? AND json_type(value,'$.savedLogs')='object' AND json_extract(value,'$.cycle')=? "
                     "AND json_extract(value,'$.roundId')=? AND json_extract(value,'$.generation')=?")
            params = [thread, after // 100, selected_cycle, snapshot.get("roundId"), snapshot.get("generation")]
            for key, value in (("callId", call_id), ("role", role)):
                if value is not None:
                    query += " AND json_extract(value,'$." + key + "')=?"
                    params.append(value)
            rows = db.execute(query + " ORDER BY rowid LIMIT 102", params).fetchall()
        for ordinal, source_ref, raw in rows[:101]:
            item = json.loads(raw)
            log = item["savedLogs"]
            if not _text(log.get("podUid")) or not _text(log.get("workloadRef")):
                continue
            sources.append({"sourceRef": source_ref, "evidenceRef": source_ref,
                "workloadRef": _text(log.get("workloadRef")), "podUid": _text(log.get("podUid")),
                "container": _text(log.get("container")), "observedAt": _time(log.get("observedAt"))})
            for index, text in enumerate(redact_log_lines("\n".join(log.get("lines", [])[:100])), 1):
                sequence = ordinal * 100 + index
                if sequence > after:
                    lines.append({"sequence": sequence, "at": _time(log.get("observedAt")), "text": text, "sourceRef": source_ref})
            scanned = max(scanned, ordinal * 100 + 100)
            if len(lines) > limit:
                break
        if rows:
            selected = lines[:limit]
            selected_refs = {r["sourceRef"] for r in selected}
            answer.update(status="captured", reason=None, sources=[s for s in sources if s["sourceRef"] in selected_refs][:100],
                lines=selected, nextCursor=selected[-1]["sequence"] if selected else scanned,
                hasMore=len(lines) > limit or len(rows) > 101)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        answer.update(status="unavailable", reason="log_evidence_invalid_or_unavailable")
    return answer
