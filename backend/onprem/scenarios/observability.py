"""Read-only display projections of durable round and Agent evidence.

These are historical observations, never admission/installation/effect proofs.
No endpoint in this module invokes a native collector, model or tool.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from backend.onprem.capacity.verdict import capacity_verdict
from backend.onprem.scenarios.contracts import SCENARIO_SLICES
from backend.onprem.scenarios.presentation import _action, _object, _scalar


SCHEMA = "cityverse.round-observability/v1"


@dataclass(frozen=True)
class _ObservationProjection:
    """Request-owned display data, never native acceptance or mutation proof."""
    snapshot: dict
    samples: list
    performance: dict
    changes: list


def _display_changes(snapshot):
    changes = []
    for action in snapshot.get("actions", []):
        if (isinstance(action, dict) and any(token in str(action.get("kind", "")) for token in ("qer", "policy", "pod", "scale"))
                and action.get("kind") != "no_additional_policy"):
            row = _action(action)
            changes.append({k: row.get(k) for k in ("id", "kind", "requested", "accepted", "installed", "effectVerified", "profileId", "policyRevision")}
                           | {"podChange": "unknown", "before": None, "after": None})
    return changes


def _derive_observation(snapshot):
    # Complete reports remain available until all original pure verdicts derive.
    samples, performance = measurement_samples(snapshot), capacity_verdict(snapshot)
    scope = {key: snapshot[key] for key in
             ("roundId", "generation", "sequence", "observedAt", "agentRuntime", "perUe") if key in snapshot}
    cleanup_fields = ("kind", "cleanupVerified", "reconciliationRequired", "policyActionIds", "finishedAt")
    scope['actions'] = [{key: action[key] for key in cleanup_fields if key in action}
                        for action in snapshot.get('actions', []) if isinstance(action, dict)]
    return scope, samples, performance, _display_changes(snapshot)


def page_arguments(after=0, limit=100):
    """Reject malformed/unbounded cursors before any evidence read."""
    def integer(value):
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
            return int(value)
        raise ValueError("after and limit must be unsigned decimal integers")
    after, limit = integer(after), integer(limit)
    if not 0 <= after < 2**63 or not 1 <= limit <= 200:
        raise ValueError("after must be nonnegative; limit must be between 1 and 200")
    return after, limit


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _time(value):
    if isinstance(value, str):
        return value
    value = _number(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _measurement_interval(report):
    """Explicit partial/invalid effective clocks cannot fall back to wrapper clocks."""
    effective = "effectiveStartedAt" in report or "effectiveFinishedAt" in report
    prefix = "effective" if effective else ""
    started = _number(report.get(prefix + "StartedAt" if effective else "startedAt"))
    finished = _number(report.get(prefix + "FinishedAt" if effective else "finishedAt"))
    return (started, finished) if started is not None and finished is not None and started < finished else (None, None)


def _loss_counts(reports):
    """Use the pinned receiver's sender denominator when raw iperf provides it."""
    from backend.onprem.traffic.capability import receiver_summary, sender_summary
    denominators, losses = [], []
    for report in reports:
        packets, lost = report.get("packets"), report.get("lostPackets")
        if type(packets) is not int or packets <= 0 or type(lost) is not int or not 0 <= lost <= packets:
            return None, None
        denominator = packets
        if "client" in report or "server" in report:
            try:
                receiver = report["server"] if report["direction"] == "ul" else report["client"]
                sender = report["client"] if report["direction"] == "ul" else report["server"]
                received, sent = receiver_summary(receiver), sender_summary(sender)
                if received["packets"] != packets or received["lost_packets"] != lost:
                    return None, None
                local = receiver.get("end", {}).get("sum_sent", {}).get("packets", 0)
                if type(local) is not int or local < 0 or (local and local != sent["packets"]):
                    return None, None
                denominator = local or packets
                reported = received.get("lost_percent")
                if type(reported) not in (int, float) or not math.isfinite(reported) or not math.isclose(reported, 100 * lost / denominator, rel_tol=1e-6, abs_tol=1e-6):
                    return None, None
            except Exception:
                return None, None
        denominators.append(denominator)
        losses.append(lost)
    return sum(denominators), sum(losses)


def _same_scope(row, snapshot):
    return (isinstance(row, dict) and row.get("roundId") == snapshot.get("roundId")
            and row.get("generation") == snapshot.get("generation"))


def _iso_seconds(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def _sample_rtt(snapshot, action, scenario, owners):
    observation = snapshot.get("rttObservations", {}).get(action.get("id"))
    if (not isinstance(observation, dict) or not _same_scope(observation, snapshot)
            or observation.get("source") != "native-ue-tun-icmp" or observation.get("statistic") != "mean"
            or observation.get("relation") != "adjacent-probe-window" or observation.get("sameTrafficWindow") is not False):
        return None, None
    expected = {s: r for s, r in owners.items() if r.get("scenario") == scenario}
    rows = [r for r in observation.get("rows", []) if isinstance(r, dict) and r.get("scenario") == scenario]
    usable = []
    for row in rows:
        owner = expected.get(row.get("supi"))
        if (not owner or any(row.get(k) != owner.get(k) for k in ("sessionId", "podUid", "ueIp", "tun", "upf", "upfEpoch"))
                or type(row.get("transmitted")) is not int or type(row.get("received")) is not int
                or not 0 <= row["received"] <= row["transmitted"] <= 3
                or _iso_seconds(row.get("startedAt")) is None or _iso_seconds(row.get("finishedAt")) is None
                or _iso_seconds(row["startedAt"]) >= _iso_seconds(row["finishedAt"])
                or (row["received"] > 0 and _number(row.get("meanMs")) is None)):
            continue
        usable.append(row)
    unique = {r["supi"] for r in usable}
    complete = len(usable) == len(unique) == len(expected) and unique == set(expected)
    valid = sum(r["received"] > 0 for r in usable)
    tx, rx = sum(r["transmitted"] for r in usable), sum(r["received"] for r in usable)
    observed = observation.get("status") == "observed" and complete and valid == len(expected) and bool(expected)
    metadata = {"status": "observed" if observed else observation.get("status") if observation.get("status") in {"missing", "timeout", "unsupported"} else "missing",
        "source": "native-ue-tun-icmp", "statistic": "mean", "sampleCount": rx, "transmitted": tx, "received": rx,
        "validUeCount": valid, "expectedUeCount": len(expected),
        "startedAt": _time(min(_iso_seconds(r["startedAt"]) for r in usable)) if usable else _time(_iso_seconds(observation.get("startedAt"))),
        "finishedAt": _time(max(_iso_seconds(r["finishedAt"]) for r in usable)) if usable else _time(_iso_seconds(observation.get("finishedAt"))),
        "reason": observation.get("reason") if observation.get("reason") else None if observed else "rtt.incomplete_cohort",
        "relation": "adjacent-probe-window", "sameTrafficWindow": False}
    mean = sum(r["meanMs"] * r["received"] for r in usable) / rx if observed and rx else None
    return mean, metadata


def _qer_change_summaries(installed, receipt, snapshot):
    """Project historical settings without treating a planned profile as readback.

    qer_before is the Validator's saved native session inventory. The actual
    GroupQerAdapter.view receipt proves the installed profile and affected
    cohort but deliberately contains no rates; after rates remain unknown.
    """
    from backend.onprem.policy.group_qer import GroupQerBlocked, _ambr_qer
    selected = installed.get("selected", {})
    if not isinstance(selected, dict):
        return None, None
    supis = selected.get("treatmentSupis")
    if (not isinstance(supis, list) or not 1 <= len(supis) <= 100 or
            any(not isinstance(s, str) for s in supis) or len(set(supis)) != len(supis)):
        return None, None
    native = installed.get("qer_before")
    before = None
    if isinstance(native, dict) and set(supis) <= native.keys():
        try:
            rows = [_ambr_qer(native[s]["qers"])["mbrKbps"] for s in supis]
        except (GroupQerBlocked, KeyError, TypeError, ValueError):
            rows = []
        if rows:
            before = {"profileId": None, "ueCount": len(supis), "source": "native-qer-before", "state": "observed",
                "uplinkKbps": {"min": min(r["ul"] for r in rows), "max": max(r["ul"] for r in rows)},
                "downlinkKbps": {"min": min(r["dl"] for r in rows), "max": max(r["dl"] for r in rows)}}
    after = None
    if (receipt.get("state") == "installed" and receipt.get("installed") is True
            and receipt.get("roundId") == snapshot.get("roundId")
            and isinstance(receipt.get("profileId"), str) and 1 <= len(receipt["profileId"]) <= 64
            and receipt["profileId"] == selected.get("profileId")
            and receipt.get("eventId") in SCENARIO_SLICES and receipt.get("eventId") == selected.get("eventId")
            and isinstance(receipt.get("treatmentSupis"), list) and receipt["treatmentSupis"] == sorted(supis)):
        after = {"profileId": receipt["profileId"], "ueCount": len(supis), "source": "installed-policy-receipt",
                 "state": "installed", "uplinkKbps": None, "downlinkKbps": None}
    return before, after


def measurement_samples(snapshot):
    """One sample per accepted action/window and scenario; old windows survive polling.

    Shards appear only through their accepted aggregate. The cursor uses durable
    action order, so a later result cannot replace an earlier sample. Packet
    counts come from receiver reports in this same window, never percentages
    averaged across directions/UEs. Missing RTT remains missing.
    """
    actions = snapshot.get("actions", [])
    index = {a.get("id"): a for a in actions if isinstance(a, dict)}
    owners = {r.get("supi"): r for r in snapshot.get("perUe", []) if isinstance(r, dict)}
    samples = []
    for ordinal, action in enumerate(actions, 1):
        if not isinstance(action, dict) or action.get("accepted") is not True:
            continue
        if action.get("roundId", snapshot.get("roundId")) != snapshot.get("roundId") or action.get("generation", snapshot.get("generation")) != snapshot.get("generation"):
            continue
        kind = action.get("kind")
        if kind == "traffic_aggregate":
            ids = action.get("shardActionIds", [])
            windows = [index.get(i, {}) for i in ids]
            if not ids or any(w.get("accepted") is not True for w in windows):
                continue
        elif kind == "traffic_window" and action.get("shardCount", 1) == 1:
            windows = [action]
        else:
            continue
        grouped = defaultdict(list)
        for window in windows:
            for report in window.get("reports", []):
                if not isinstance(report, dict):
                    continue
                binding = report.get("binding", {})
                if not isinstance(binding, dict):
                    continue
                owner = owners.get(binding.get("supi"), {})
                # Explicit foreign bindings are never merged into this round.
                if (binding.get("roundId", snapshot.get("roundId")) != snapshot.get("roundId")
                        or binding.get("generation", snapshot.get("generation")) != snapshot.get("generation")
                        or not owner or any(binding.get(k) != owner.get(k) for k in ("sessionId", "podUid", "upf"))):
                    continue
                scenario = binding.get("scenario", owner.get("scenario"))
                if scenario == owner.get("scenario") and scenario in SCENARIO_SLICES and report.get("direction") in ("ul", "dl"):
                    grouped[scenario].append(report)
        for scenario_index, scenario in enumerate(SCENARIO_SLICES, 1):
            reports = grouped.get(scenario)
            if not reports:
                continue
            identities = sorted({(r["binding"].get("supi"), r["binding"].get("sessionId"),
                                  r["binding"].get("podUid"), r["binding"].get("upf")) for r in reports})
            paired = {(r["binding"].get("supi"), r["direction"]) for r in reports}
            expected = {s for s, row in owners.items() if row.get("scenario") == scenario}
            complete = (len(reports) == len(paired) == len(identities) * 2
                        and paired == {(s, d) for s in expected for d in ("ul", "dl")})
            values = {}
            for direction, key in (("ul", "receivedUplinkMbps"), ("dl", "receivedDownlinkMbps")):
                rows = [r for r in reports if r["direction"] == direction]
                values[key] = (sum(r["receivedMbps"] for r in rows) if complete and rows
                               and all(_number(r.get("receivedMbps")) is not None for r in rows) else None)
            packets, lost = _loss_counts(reports) if complete else (None, None)
            intervals = [_measurement_interval(r) for r in reports]
            starts, ends = [r[0] for r in intervals], [r[1] for r in intervals]
            # A sharded aggregate owns the common receiver interval. Wrapper
            # clocks include setup/cleanup and are only a legacy fallback.
            has_aggregate_interval = kind == "traffic_aggregate" and any(k in action for k in ("effectiveStartedAt", "effectiveFinishedAt"))
            if has_aggregate_interval:
                started, finished = _measurement_interval(action)
                observed = finished
            else:
                started = min(starts) if all(v is not None for v in starts) else None
                finished = max(ends) if all(v is not None for v in ends) else None
                observed = min(ends) if all(v is not None for v in ends) else None
            temporal_valid = started is not None and finished is not None and all(a is not None and b is not None for a, b in intervals)
            if not temporal_valid:
                values = {key: None for key in values}
                packets = lost = None
            fingerprint = None
            if complete and temporal_valid and all(_number(r.get("offeredTargetMbps")) is not None for r in reports):
                offered = sorted((r["binding"].get("supi"), r["direction"], r["offeredTargetMbps"]) for r in reports)
                fingerprint = _digest([snapshot.get("roundId"), snapshot.get("generation"), scenario, identities, offered])
            policy_revisions = {r["binding"].get("policyRevision") for r in reports}
            mean_rtt, rtt = _sample_rtt(snapshot, action, scenario, owners)
            sample = {"id": f"{action.get('id', ordinal)}:{scenario}", "sequence": ordinal * 16 + scenario_index,
                "roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
                "scenario": scenario, "slice": SCENARIO_SLICES[scenario], "stage": action.get("stage"),
                "startedAt": _time(started), "finishedAt": _time(finished), "observedAt": _time(observed),
                **values, "rttMs": mean_rtt, "rttP95Ms": None, "rtt": rtt,
                "packets": packets, "lostPackets": lost, "lossPercent": 100 * lost / packets if packets else None,
                "sampleCount": len(identities), "source": "native-iperf3-receiver",
                "quality": "observed" if complete and temporal_valid and all(v is not None for v in values.values()) else "missing",
                "comparisonFingerprint": fingerprint,
                "policyRevision": next(iter(policy_revisions)) if len(policy_revisions) == 1 else None}
            from backend.onprem.scenarios.objectives import sample_objectives
            sample["targets"], sample["assessment"] = sample_objectives(snapshot, sample)
            samples.append(sample)
    return samples


def measurement_page(snapshot, *, after=0, limit=100, scenario=None):
    after, limit = page_arguments(after, limit)
    if scenario is not None and scenario not in SCENARIO_SLICES:
        raise ValueError("Unknown scenario")
    derived = snapshot.samples if isinstance(snapshot, _ObservationProjection) else measurement_samples(snapshot)
    samples = [r for r in derived if r["sequence"] > after
               and (scenario is None or r["scenario"] == scenario)]
    page = samples[:limit]
    return {"samples": page, "nextCursor": page[-1]["sequence"] if page else after,
            "hasMore": len(samples) > limit, "gap": False}


class _GraphObservationViews:
    """Bounded pure display projections keyed by freshly read artifact bytes."""

    def __init__(self):
        from collections import OrderedDict
        from threading import Lock
        self._entries, self._lock = OrderedDict(), Lock()

    def read(self, thread, ref, encoded):
        from copy import deepcopy
        key = (thread, ref, hashlib.sha256(encoded.encode()).digest())
        with self._lock:
            if key not in self._entries:
                source = json.loads(encoded)
                if not isinstance(source, dict):
                    raise ValueError('Invalid historical graph observation')
                latest = {sample['scenario']: sample['id'] for sample in measurement_samples(source)}
                projection = ({**{k: source[k] for k in ('roundId', 'generation', 'observedAt') if k in source},
                               '_displaySampleIds': list(latest.values())} if source else {})
                self._entries[key] = projection
                if len(self._entries) > 16:
                    self._entries.popitem(last=False)
            self._entries.move_to_end(key)
            return deepcopy(self._entries[key])


_GRAPH_OBSERVATION_VIEWS = _GraphObservationViews()


def observation_artifact_view(db, thread, ref):
    """Fresh thread/ref lookup; retain only historical UI scope and sample IDs.

    Agent reads and independent evidence verification use the full artifact.
    This projection carries no native freshness or execution authority.
    """
    if not isinstance(ref, str):
        return {}
    row = db.execute('SELECT value FROM graph_artifacts WHERE thread_id=? AND ref=?', (thread, ref)).fetchone()
    return _GRAPH_OBSERVATION_VIEWS.read(thread, ref, row[0]) if row else {}


def graph_views(path, snapshot):
    """Read private refs only inside the requested thread; project bounded fields."""
    if path is None or not Path(path).is_file():
        return [], []
    path = Path(path)
    if path.is_symlink():
        return [], []
    thread = f"{snapshot['roundId']}:{snapshot['generation']}"
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"graph_artifacts", "graph_outcomes"} <= tables:
            return [], []
        def artifact(ref, *, observation=False):
            if observation:
                return observation_artifact_view(db, thread, ref)
            row = db.execute("SELECT value FROM graph_artifacts WHERE thread_id=? AND ref=?", (thread, ref)).fetchone()
            return json.loads(row[0]) if row else {}
        def sample_ids(observation):
            return observation.get('_displaySampleIds', [])
        companions = {}
        for encoded, in db.execute("SELECT value FROM graph_artifacts WHERE thread_id=? AND ref LIKE 'cycle_observability:%' ORDER BY rowid DESC LIMIT 200", (thread,)):
            display = json.loads(encoded)
            if type(display.get("cycle")) is int:
                companions.setdefault(display["cycle"], display)
        decisions, changes = [], []
        for cycle, encoded in db.execute("SELECT cycle,value FROM graph_outcomes WHERE thread_id=? ORDER BY cycle DESC LIMIT 200", (thread,)):
            outcome = {**json.loads(encoded), **companions.get(cycle, {})}
            plan = artifact(outcome.get("planRef"))
            before = artifact(outcome.get("beforeRef"), observation=True)
            after = artifact(outcome.get("afterRef"), observation=True)
            if before and not _same_scope(before, snapshot):
                continue
            if after and not _same_scope(after, snapshot):
                continue
            reason = plan.get("reason")
            decision = {"id": f"{thread}:{cycle}", "cycle": cycle,
                "action": plan.get("action", outcome.get("action")),
                "reason": reason[:1000] if isinstance(reason, str) else None,
                "outcome": _scalar(outcome.get("outcome")), "improvementPercent": _scalar(outcome.get("improvementPercent")),
                **{k: _scalar(outcome.get(k)) for k in ("beforeRef", "afterRef", "actionRef")},
                "scenario": _scalar(plan.get("group_id")), "profileId": _scalar(plan.get("profile_id")),
                "at": _scalar(after.get("observedAt") or before.get("observedAt")),
                "beforeSampleIds": sample_ids(before), "afterSampleIds": sample_ids(after)}
            decisions.append(decision)
            installed = artifact(outcome.get("actionRef"))
            receipt = installed.get("receipt", {})
            if installed and isinstance(receipt, dict) and decision["action"] == "apply":
                before_summary, after_summary = _qer_change_summaries(installed, receipt, snapshot)
                cleanup = next((a for a in reversed(snapshot.get("actions", []))
                    if isinstance(a, dict) and a.get("kind") == "adaptive_qer_cleanup"
                    and a.get("cleanupVerified") is True and a.get("reconciliationRequired") is False
                    and isinstance(a.get("policyActionIds"), list) and receipt.get("id") in a["policyActionIds"]), None)
                changes.append({"id": _scalar(receipt.get("id")) or decision["id"], "kind": "qer_policy",
                    "requested": True, "accepted": receipt.get("accepted"), "installed": receipt.get("installed"),
                    "effectVerified": (True if outcome.get("outcome") == "effect_verified" else
                                       False if outcome.get("outcome") in {"regressed", "no_effect"} else None),
                    "profileId": _scalar(installed.get("selected", {}).get("profileId")),
                    "scenario": _scalar(installed.get("selected", {}).get("eventId")),
                    "policyRevision": _scalar(receipt.get("policyRevision")), "podChange": "unknown",
                    "before": before_summary, "after": after_summary,
                    "state": "restored" if cleanup else "installed" if after_summary else "unknown",
                    "restorationVerified": True if cleanup else None,
                    "restoredAt": _time(cleanup.get("finishedAt")) if cleanup else None,
                    **{key: decision.get(key) for key in ("cycle", "at", "beforeSampleIds", "afterSampleIds")},
                    "decisionId": decision["id"]})
        if "graph_events" in tables:
            # Show a proposed decision while precheck/apply is still running.
            completed = {d["cycle"] for d in decisions}
            for encoded, in db.execute("SELECT value FROM graph_events WHERE thread_id=? ORDER BY ordinal DESC LIMIT 200", (thread,)):
                event = json.loads(encoded)
                if event.get("kind") != "decision" or event.get("phase") != "plan" or event.get("cycle") in completed:
                    continue
                cycle = event.get("cycle")
                if type(cycle) is not int:
                    continue
                plan = artifact(event.get("plan_ref"))
                before = artifact(event.get("before_ref"), observation=True)
                if not plan or not _same_scope(before, snapshot):
                    continue
                reason = plan.get("reason")
                decisions.append({"id": f"{thread}:{cycle}", "cycle": cycle,
                    "action": _scalar(plan.get("action")), "reason": reason[:1000] if isinstance(reason, str) else None,
                    "outcome": "proposed", "improvementPercent": None, "beforeRef": event.get("before_ref"),
                    "afterRef": None, "actionRef": None, "scenario": _scalar(plan.get("group_id")),
                    "profileId": _scalar(plan.get("profile_id")), "at": _time(event.get("at")),
                    "beforeSampleIds": sample_ids(before), "afterSampleIds": []})
                completed.add(cycle)
    return sorted(decisions, key=lambda d: d["cycle"]), list(reversed(changes))


def event_page(path, snapshot, *, after=0, limit=100, cycle=None, action_id=None, call_id=None, role=None, journal_path=None):
    from backend.onprem.agents.persistence import GraphArtifacts
    after, limit = page_arguments(after, limit)
    if path is None:
        return {"events": [], "nextCursor": after, "hasMore": False, "gap": False}
    cycles = None
    if action_id is not None:
        from backend.onprem.agents.action_observability import action_summaries
        matches = [r for r in action_summaries(snapshot, graph_path=path, journal_path=journal_path) if r.get("actionId") == action_id]
        cycles = {r["cycle"] for r in matches}
        if cycle is not None:
            cycles &= {cycle}
    result = GraphArtifacts.read_events(Path(path), f"{snapshot['roundId']}:{snapshot['generation']}",
        after=after, limit=limit, role=role, cycle=cycle, call_id=call_id, cycles=cycles)
    aliases = {"ordinal": "sequence", "call_id": "callId", "started_at": "startedAt", "ended_at": "endedAt",
               "duration_ms": "durationMs", "input_summary": "inputSummary", "output_summary": "outputSummary",
               "plan_ref": "planRef", "before_ref": "beforeRef", "after_ref": "afterRef", "action_ref": "actionRef"}
    aliases["display_ref"] = "evidenceRef"
    fields = ("ordinal", "role", "phase", "tool", "status", "cycle", "at", "kind", "call_id", "turn",
              "started_at", "ended_at", "duration_ms", "input_summary", "output_summary", "code",
              "action", "plan_ref", "before_ref", "after_ref", "action_ref", "display_ref", "targetRefs")
    events = []
    for event in result["events"]:
        projected = {aliases.get(k, k): event.get(k) for k in fields if k in event}
        from backend.onprem.agents.topology_observability import clean_targets
        projected["targetRefs"] = clean_targets(event.get("targetRefs", []))
        for key in ("inputSummary", "outputSummary"):
            if key in projected:
                projected[key] = _object(projected[key], ("type", "fieldCount", "itemCount"))
        for key in ("at", "startedAt", "endedAt"):
            if key in projected:
                projected[key] = _time(projected[key])
        events.append(projected)
    return {**result, "events": events}


def observability(snapshot, *, graph_path=None, journal_path=None, runtime_status=None, capabilities=None):
    projection = snapshot if isinstance(snapshot, _ObservationProjection) else None
    if projection is not None:
        snapshot = projection.snapshot
    decisions, changes = graph_views(graph_path, snapshot)
    from backend.onprem.agents.action_observability import action_summaries
    from backend.onprem.agents.topology_observability import clean_targets, node_states
    actions = action_summaries(snapshot, graph_path=graph_path, journal_path=journal_path)
    samples = projection.samples if projection is not None else measurement_samples(snapshot)
    latest = {s["scenario"]: s for s in samples}
    status = runtime_status if _same_scope(runtime_status, snapshot) else snapshot.get("agentRuntime")
    if status is not None and not _same_scope(status, snapshot):
        status = None
    agent = _object(status, ("role", "phase", "tool", "status", "cycle", "callId", "startedAt", "updatedAt"))
    if agent is not None:
        agent.update(callId=_scalar(status.get("call_id", status.get("callId"))),
                     startedAt=_time(status.get("started_at", status.get("startedAt"))),
                     updatedAt=_time(status.get("updatedAt", status.get("at"))))
        agent["targetRefs"] = clean_targets(status.get("targetRefs", []))
    if agent is None and graph_path is not None:
        # Resume after restart from the last durable event, preserving its clock.
        path = Path(graph_path)
        event = None
        if path.is_file() and not path.is_symlink():
            with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("PRAGMA query_only=ON")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='graph_events'").fetchone():
                    row = db.execute("SELECT value FROM graph_events WHERE thread_id=? ORDER BY ordinal DESC LIMIT 1",
                        (f"{snapshot['roundId']}:{snapshot['generation']}",)).fetchone()
                    event = json.loads(row[0]) if row else None
        if event:
            agent = {k: _scalar(event.get(k)) for k in ("role", "phase", "tool", "status", "cycle")}
            agent.update(callId=event.get("call_id"), startedAt=_time(event.get("started_at")), updatedAt=_time(event.get("at")))
            agent["targetRefs"] = clean_targets(event.get("targetRefs", []))
    changes.extend(projection.changes if projection is not None else _display_changes(snapshot))
    return {"schemaVersion": SCHEMA, "roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
            "observedAt": snapshot.get("observedAt"), "agent": agent, "decisions": decisions,
            "changes": changes[-200:], "latestQos": list(latest.values()), "actions": actions,
            "nodeStates": node_states(snapshot, graph_path),
            "capabilities": {"podScaling": False, "qerPolicy": (capabilities or {}).get("aiPolicy", False)},
            "performance": projection.performance if projection is not None else capacity_verdict(snapshot)}
