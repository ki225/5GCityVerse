"""Translate a completed native receiver window into an adaptive observation.

The ledger owns UE identities and accepted traffic windows.  This adapter does
not infer measurements or refresh their timestamps.
"""
from __future__ import annotations

from copy import deepcopy
import math

from backend.onprem.agents.contracts import AgentLoopBlocked
from backend.onprem.scenarios.contracts import SCENARIO_SLICES as EVENT_SLICES
from backend.onprem.traffic.planning import valid_rate, direction_rate


def native_observation(snapshot: dict) -> dict:
    owners = snapshot.get("perUe")
    measurements = snapshot.get("measurements")
    actions = snapshot.get("actions")
    if (not isinstance(owners, list) or not owners
            or not isinstance(measurements, list) or len(measurements) != len(owners)
            or not isinstance(actions, list)):
        raise AgentLoopBlocked("Native receiver cohort is incomplete")
    by_supi = {row.get("supi"): row for row in owners if isinstance(row, dict)}
    measured = {row.get("supi"): row for row in measurements if isinstance(row, dict)}
    if (len(by_supi) != len(owners) or len(measured) != len(owners)
            or set(by_supi) != set(measured) or None in by_supi):
        raise AgentLoopBlocked("Native receiver identities differ from round ownership")
    stage = snapshot.get("stage")
    if stage not in {"measuring_baseline", "measuring_result"}:
        raise AgentLoopBlocked("No completed native measurement stage")
    # A sharded window ends at its accepted aggregate.  Select only its exact
    # shard action IDs, so an earlier window cannot supply offered load.
    aggregates = [a for a in actions if a.get("kind") == "traffic_aggregate"
                  and a.get("stage") == stage]
    if aggregates:
        aggregate = aggregates[-1]
        ids = aggregate.get("shardActionIds")
        windows = [a for a in actions if a.get("id") in ids] if isinstance(ids, list) else []
        if (aggregate.get("accepted") is not True or not isinstance(ids, list)
                or len(windows) != len(ids) or [a.get("id") for a in windows] != ids):
            raise AgentLoopBlocked("Native aggregate does not own exact traffic shards")
    else:
        windows = [a for a in actions if a.get("kind") == "traffic_window"
                   and a.get("stage") == stage]
        if not windows:
            raise AgentLoopBlocked("Native traffic window is ambiguous")
        if len(windows) > 1:
            # Repeated evaluations append windows. Select the newest intent,
            # never fall back to an older accepted result after a failed window.
            from backend.onprem.agents.contracts import _digest
            latest = windows[-1]
            reports = latest.get('reports')
            measured_reports = [report for row in measurements for report in row.get('windows', [])]
            if (not isinstance(reports, list) or not reports
                    or sorted(_digest(r) for r in reports) != sorted(_digest(r) for r in measured_reports)):
                raise AgentLoopBlocked('Native latest window differs from current receiver rows')
            windows = [latest]
    offered = {}
    for window in windows:
        rates = window.get("offeredBySupi")
        if (window.get("kind") != "traffic_window" or window.get("stage") != stage
                or window.get("accepted") is not True or window.get("cleanupVerified") is not True
                or not isinstance(rates, dict) or set(offered) & set(rates)):
            raise AgentLoopBlocked("Native offered load is unverified")
        offered.update(rates)
    if set(offered) != set(by_supi):
        raise AgentLoopBlocked("Native offered load does not cover the round")
    rows = []
    for supi, owner in by_supi.items():
        received = measured[supi]
        event = owner.get("scenario")
        rate = offered[supi]
        if (event not in EVENT_SLICES or owner.get("slice") != EVENT_SLICES[event]
                or received.get("source") != "receiver"
                or received.get("roundId") != snapshot.get("roundId")
                or received.get("generation") != snapshot.get("generation")
                or received.get("sessionId") != owner.get("sessionId")
                or received.get("upfEpoch") != owner.get("upfEpoch")
                or not valid_rate(rate)):
            raise AgentLoopBlocked("Native receiver row is not bound to its PDU session")
        if any(type(received.get(key)) not in (int, float)
               or not math.isfinite(received[key]) or received[key] < 0
               for key in ("receivedUplinkMbps", "receivedDownlinkMbps")):
            raise AgentLoopBlocked("Native receiver throughput is incomplete")
        rows.append({**deepcopy(owner),
                     "eventId": event, "requestedSlice": EVENT_SLICES[event],
                     "offeredUplinkMbps": direction_rate(rate, "ul") / 1000,
                     "offeredDownlinkMbps": direction_rate(rate, "dl") / 1000,
                     **{key: received.get(key) for key in
                        ("receivedUplinkMbps", "receivedDownlinkMbps", "rttMs", "lossPercent")}})
    service_sla = {}
    if "serviceRequirements" in snapshot:
        from backend.onprem.scenarios.objectives import round_sla_assessment
        service_sla["sla"] = round_sla_assessment(snapshot)
    return {**deepcopy(snapshot), **service_sla, "_actuatorSnapshot": deepcopy(snapshot), "perUe": rows,
            "scenarioActive": True,
            "network": {"throughputMbps": sum(row["receivedUplinkMbps"]
                                               + row["receivedDownlinkMbps"] for row in rows),
                        "activePduSessions": len(rows)},
            "observedAt": snapshot.get("observedAt")}


def commissioned_candidates(snapshot: dict, profile_ids: tuple[str, ...]) -> list[dict]:
    """Offer exact event cohorts with a stable treatment/holdout split."""
    if not profile_ids or len(set(profile_ids)) != len(profile_ids):
        raise AgentLoopBlocked("Commissioned QER profiles are unavailable")
    rows = snapshot.get("perUe")
    if not isinstance(rows, list):
        raise AgentLoopBlocked("Native event cohort is unavailable")
    candidates = []
    for event in EVENT_SLICES:
        supis = sorted(row["supi"] for row in rows if row.get("eventId") == event)
        if not supis:
            continue
        if len(set(supis)) != len(supis):
            raise AgentLoopBlocked("Native event cohort contains duplicate identities")
        split = max(1, len(supis) // 2)
        for profile_id in profile_ids:
            candidates.append({"id": f"{event}:{profile_id}", "eventId": event,
                               "profileId": profile_id, "treatmentSupis": supis[:split],
                               "holdoutSupis": supis[split:]})
    if not candidates or len(candidates) > 18:
        raise AgentLoopBlocked("Commissioned candidate set exceeds planner limit")
    return candidates
