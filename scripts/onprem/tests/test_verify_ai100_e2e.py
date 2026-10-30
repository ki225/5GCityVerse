"""Synthetic AI receipts; these do not establish a live model or QER result."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from backend.onprem.traffic.planning import plan_workload
from backend.onprem.traffic.workload import workload_metadata
from test.subsystems.onprem.test_onprem_capacity_100_shards import shard_snapshot
from scripts.onprem.verify_ai100_e2e import _cohort, configured_journal, read_qer_journal, verify


def evidence(events=None, *, snapshot_factory=shard_snapshot):
    result = snapshot_factory(events or ["video_streaming"] * 100)
    treated_supi = result["perUe"][0]["supi"]
    treated_baseline_mbps = None
    for window in result["actions"]:
        if window.get("kind") != "traffic_window" or window.get("stage") != "measuring_baseline":
            continue
        for report in window["reports"]:
            if report["binding"]["supi"] != treated_supi or report["direction"] != "ul":
                continue
            received = report["server"]["end"]["sum_received"]
            lost = received["packets"] // 20
            received["bytes"] -= lost * 1200
            received["bits_per_second"] = received["bytes"] * 8 / received["seconds"]
            received["lost_packets"] = lost
            received["lost_percent"] = lost * 100 / received["packets"]
            report["receivedBytes"] = received["bytes"]
            report["receivedMbps"] = received["bits_per_second"] / 1e6
            report["lostPackets"] = lost
            treated_baseline_mbps = report["receivedMbps"]
    treated_result_mbps = next(row["receivedUplinkMbps"] for row in result["measurements"]
                               if row["supi"] == treated_supi)
    improvement = (treated_result_mbps / treated_baseline_mbps - 1) * 100
    result.update(strategy="ai", requestId="request-1",
                  workload=workload_metadata(result["perUe"]))
    result["actions"].extend([
        {"kind": "adaptive_qer_commissioning", "accepted": True},
        {"kind": "adaptive_ai_outcomes", "accepted": True, "cleanupVerified": True,
         "outcomes": [{"iteration": 0, "candidateId": "c1", "outcome": "effect_verified",
                       "improvementPercent": improvement, "responseId": "resp_test"}]},
    ])
    first = result["perUe"][0]
    second = result["perUe"][1]
    old = {"id": 7, "pdrIds": [10], "mbrKbps": {"ul": 1000, "dl": 1000}}
    baseline = {r["supi"]: {"qers": [deepcopy(old)]} for r in result["perUe"]}
    action = {"id": "qer-1", "roundId": result["roundId"], "state": "restored",
              "eventId": first["scenario"], "profileId": "event-2mbps",
              "profileRates": {"ul": 2000, "dl": 2000},
              "modelResponseId": "resp_test", "candidateId": "c1",
              "treatmentSupis": [first["supi"]],
              "holdoutSupis": [r["supi"] for r in result["perUe"]
                               if r["scenario"] == first["scenario"] and r["supi"] != first["supi"]],
              "attemptedSupis": [first["supi"]], "uncertain": False, "baseline": baseline,
              "fence": {"roundId": result["roundId"], "generation": 18},
              "snapshot": {"perUe": deepcopy(result["perUe"])},
              "attempts": [{"supi": first["supi"], "phase": phase,
                            "httpStatus": 200, "pfcpAccepted": True, "observedAt": stamp}
                           for phase, stamp in (("apply", 135.0), ("restore", 180.0))]}
    # The minimal positive fixture models all 100 native sessions. Only the
    # treatment cohort changes; unrelated and holdout sessions remain equal.
    def read(sequence, installed, phase, captured_at):
        sessions = [{**{key: owner[key] for key in
                     ("supi", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf", "sst", "sd")},
                     "qers": [{**old, "mbrKbps": ({"ul": 2000, "dl": 2000}
                               if installed and owner["supi"] == first["supi"]
                               else old["mbrKbps"])}]}
                    for owner in result["perUe"]]
        return {"roundId": result["roundId"], "generation": result["generation"],
                "sequence": sequence, "policyRevision": 0,
                "journalId": sequence - 4, "actionId": action["id"],
                "phase": phase, "capturedAt": captured_at,
                "cohortSha256": _cohort(result["perUe"]),
                "proof": {"source": "native-upf-qer",
                          "observedAt": datetime.fromtimestamp(captured_at, timezone.utc).isoformat(),
                          "sessions": sessions}}
    rendered = {"alerts": [], "evidence": [{"roundId": result["roundId"],
        "generation": "18", "sequence": "10", "policyRevision": "0",
        "observedAt": result["observedAt"], "stage": "results_ready", "fresh": "true",
        "visibleMeasurements": str(len(result["perUe"])), "trafficVisible": "true",
        **{k: str(len(result["perUe"])) for k in ("allocated", "registered", "pduEstablished", "trafficVerified")}}]}
    cleanup = {"roundId": result["roundId"], "generation": 18,
        "sequence": 11, "executionOutcome": "passed", "stage": "completed",
        "cleanupStatus": "verified", "releaseProof": {key: True for key in
            ("trafficStopped", "policiesAbsent", "rulesRestored", "uesReleased", "podsAbsent")},
        "actions": [*result["actions"], {"kind": "adaptive_qer_cleanup", "cleanupVerified": True,
                    "reconciliationRequired": False, "policyActionIds": ["qer-1"]}]}
    intent = {"status": 202, "request": {"requestId": "request-1", "strategy": "ai",
                                            "events": {event: sum(r["scenario"] == event for r in result["perUe"])
                                                       for event in ("video_streaming", "realtime_control", "connected_traffic", "realtime_control", "factory_iot")}},
              "response": {"roundId": result["roundId"]}}
    metadata = workload_metadata(result["perUe"])
    panel = {key: str(value) for key, value in metadata.items() if key in (
        "profileId", "comparisonFingerprint", "budgetKbpsPerDirection",
        "requestedUplinkKbps", "requestedDownlinkKbps", "requestedCombinedKbps",
        "appliedUplinkKbps", "appliedDownlinkKbps", "appliedCombinedKbps")}
    panel["qosEffectClaimed"] = "false"
    return result, rendered, cleanup, intent, [action], [
        read(5, False, "baseline", 130.0), read(6, True, "installed", 140.0),
        read(7, False, "restored", 190.0)], {"workload": [panel]}


def test_exact_ai100_native_qer_model_and_cleanup_receipts_pass():
    assert verify(*evidence())["qerActions"] == 1


@pytest.mark.parametrize("foreign", ["candidate", "response"])
def test_effect_cannot_borrow_improvement_from_another_decision(foreign):
    args = evidence()
    decisions = next(a["outcomes"] for a in args[0]["actions"] if a.get("kind") == "adaptive_ai_outcomes")
    actual = decisions[0]["improvementPercent"]
    decisions[0]["improvementPercent"] = 999.
    other = {**decisions[0], "iteration": 1, "improvementPercent": actual}
    other["candidateId" if foreign == "candidate" else "responseId"] = "foreign"
    decisions.append(other)
    with pytest.raises(ValueError, match="treatment/holdout effect"):
        verify(*args)


def test_duplicate_action_decision_is_ambiguous():
    args = evidence()
    decisions = next(a["outcomes"] for a in args[0]["actions"] if a.get("kind") == "adaptive_ai_outcomes")
    decisions.append({**decisions[0], "iteration": 1})
    with pytest.raises(ValueError, match="exact model decision"):
        verify(*args)


def test_unrelated_noop_outcome_does_not_replace_the_action_decision():
    args = evidence()
    decisions = next(a["outcomes"] for a in args[0]["actions"] if a.get("kind") == "adaptive_ai_outcomes")
    decisions.append({"iteration": 1, "candidateId": None, "responseId": "resp-noop",
                      "outcome": "no_op", "improvementPercent": None})
    assert verify(*args)["qerActions"] == 1


def set_action_lifetime(args, first_apply, first_restore):
    for attempt in args[4][0]["attempts"]:
        attempt["observedAt"] = first_apply if attempt["phase"] == "apply" else first_restore
    for read, stamp in zip(args[5], [first_apply - 5, first_apply + 1, first_restore + 5]):
        read["capturedAt"] = stamp
        read["proof"]["observedAt"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@pytest.mark.parametrize("lifetime", [(151., 180.), (135., 165.), (20., 40.), (110., 180.)],
                         ids=["result_before_apply", "result_after_restore", "historical_restored", "baseline_after_apply"])
def test_effect_requires_complete_baseline_and_result_lifetime(lifetime):
    args = evidence()
    set_action_lifetime(args, *lifetime)
    with pytest.raises(ValueError, match="treatment/holdout effect"):
        verify(*args)


def test_common_intersection_inside_lifetime_cannot_hide_one_early_flow():
    args = evidence()
    set_action_lifetime(args, 147., 180.)
    selected = next(a for a in args[0]["actions"] if a.get("kind") == "traffic_window"
                    and a.get("stage") == "measuring_result")
    report = selected["reports"][0]
    for field in ("startedAt", "finishedAt", "receiverStartedAt", "receiverFinishedAt", "effectiveStartedAt", "effectiveFinishedAt"):
        report[field] -= 5
    report["client"]["start"]["timestamp"]["timesecs"] -= 5
    report["client"]["start"]["timestamp"]["timemillisecs"] -= 5000
    aggregate = next(a for a in args[0]["actions"] if a.get("kind") == "traffic_aggregate"
                     and a.get("stage") == "measuring_result")
    reports = [r for a in args[0]["actions"] if a.get("kind") == "traffic_window"
               and a.get("stage") == "measuring_result" for r in a["reports"]]
    aggregate["effectiveFinishedAt"] = min(r["effectiveFinishedAt"] for r in reports)
    assert aggregate["effectiveStartedAt"] >= 147.
    assert aggregate["effectiveFinishedAt"] <= 180.
    assert report["effectiveStartedAt"] == 145.
    with pytest.raises(ValueError, match="treatment/holdout effect"):
        verify(*args)


def test_private_qer_journal_is_read_only_and_round_scoped(tmp_path):
    result, _, _, _, actions, reads, _ = evidence()
    path = tmp_path / "group-qer.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE group_qer_actions (id TEXT PRIMARY KEY, round_id TEXT, record TEXT)")
        db.execute("CREATE TABLE native_qer_receipts (id INTEGER PRIMARY KEY, round_id TEXT, receipt TEXT)")
        db.execute("INSERT INTO group_qer_actions VALUES (?, ?, ?)",
                   (actions[0]["id"], result["roundId"], json.dumps(actions[0])))
        for index, read in enumerate(reads):
            db.execute("INSERT INTO native_qer_receipts VALUES (?, ?, ?)",
                       (index + 1, result["roundId"], json.dumps(read)))
    before = path.read_bytes()
    actual_actions, actual_reads = read_qer_journal(path, result["roundId"])
    assert actual_actions == actions and actual_reads == reads and path.read_bytes() == before
    assert read_qer_journal(path, "another-round") == ([], [])


def test_verifier_requires_exact_private_startup_journal_and_digest(tmp_path):
    journal = tmp_path / "group-qer.db"
    journal.write_bytes(b"journal")
    config = tmp_path / "ai100.json"
    config.write_text(json.dumps({"schemaVersion": "cityverse.adaptive-ai100-startup/v1",
                                  "journalPath": str(journal)}))
    config.chmod(0o600)
    journal.chmod(0o600)
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    assert configured_journal(config, digest) == journal.resolve()
    with pytest.raises(ValueError, match="digest"):
        configured_journal(config, "0" * 64)
    foreign = tmp_path / "foreign.db"
    foreign.write_bytes(b"foreign")
    config.write_text(json.dumps({"schemaVersion": "cityverse.adaptive-ai100-startup/v1",
                                  "journalPath": str(foreign)}))
    with pytest.raises(ValueError, match="digest"):
        configured_journal(config, digest)


@pytest.mark.parametrize("fault", ["round", "cohort", "model", "effect", "write", "installed",
                                    "restore", "release", "workload", "traffic", "fake_effect",
                                    "qer_identity", "smf_ack", "model_action_link",
                                    "intent_events", "owner_event", "owner_slice",
                                    "old_action_read", "stale_installed", "early_restore",
                                    "stale_upf_sample"])
def test_missing_or_cross_round_ai_receipt_fails_closed(fault):
    result, rendered, cleanup, intent, actions, reads, workload = evidence()
    if fault == "round":
        reads[1]["roundId"] = "other"
    elif fault == "cohort":
        reads[1]["cohortSha256"] = "0" * 64
    elif fault == "model":
        result["actions"][-1]["outcomes"][0]["responseId"] = ""
    elif fault == "model_action_link":
        actions[0]["modelResponseId"] = "another-response"
    elif fault == "intent_events":
        intent["request"]["events"] = {"realtime_control": 100}
    elif fault == "owner_event":
        result["perUe"][0]["scenario"] = "realtime_control"
    elif fault == "owner_slice":
        result["perUe"][0]["slice"] = "urllc"
    elif fault == "old_action_read":
        for read in reads:
            read["actionId"] = "prior-action-same-round"
    elif fault == "stale_installed":
        reads[1]["capturedAt"] = 19.0
    elif fault == "early_restore":
        reads[2]["capturedAt"] = 39.0
    elif fault == "stale_upf_sample":
        reads[1]["proof"]["observedAt"] = "1970-01-01T00:00:18+00:00"
    elif fault == "effect":
        result["actions"][-1]["outcomes"][0]["outcome"] = "no_effect"
    elif fault == "write":
        actions[0]["attemptedSupis"] = []
    elif fault == "smf_ack":
        actions[0]["attempts"][0]["pfcpAccepted"] = False
    elif fault == "installed":
        reads.pop(1)
    elif fault == "restore":
        reads.pop()
    elif fault == "release":
        cleanup["releaseProof"]["podsAbsent"] = False
    elif fault == "workload":
        result["workload"]["comparisonFingerprint"] = "wrong"
    elif fault == "qer_identity":
        reads[1]["proof"]["sessions"][0]["pfcpSeid"] = "foreign"
    elif fault == "fake_effect":
        result["actions"][-1]["outcomes"][0]["improvementPercent"] = 40
    else:
        result["actions"].remove(next(a for a in result["actions"]
            if a.get("kind") == "traffic_window" and a.get("stage") == "measuring_result"))
    with pytest.raises(ValueError):
        verify(result, rendered, cleanup, intent, actions, reads, workload)
