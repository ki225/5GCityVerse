"""Read-only verifier for one captured adaptive AI round of1..100 UEs.

The browser capture and private QER journal are independent inputs. A policy
attempt, model response, or capacity manifest alone cannot pass this check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
from contextlib import closing

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.capacity.performance import _flow, sharded_capacity_windows
from backend.onprem.agents.native_observation import native_observation
from backend.onprem.traffic.planning import plan_workload, plan_probe_workload
from backend.onprem.traffic.workload import workload_metadata, workload_metadata_for_snapshot
from backend.onprem.scenarios.contracts import EVENT_SLICES
from backend.onprem.runtime.ue import seconds
from scripts.onprem.run_browser_scenario_matrix import released, round_resources_released
from backend.onprem.agents.contracts import _digest, ScenarioContract


def _cohort(rows):
    stable = ("supi", "scenario", "slice", "sessionId", "smContextRef", "pfcpSeid",
              "upfEpoch", "upf", "sst", "sd", "ueIp")
    return hashlib.sha256(json.dumps(sorted(
        ({key: row.get(key) for key in stable} for row in rows),
        key=lambda row: str(row["supi"])), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_qer_journal(path: Path, round_id: str):
    """Only read an existing private journal; never create or migrate it."""
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        actions = [json.loads(row[0]) for row in db.execute(
            "SELECT record FROM group_qer_actions WHERE round_id=? ORDER BY rowid", (round_id,))]
        reads = [{**json.loads(row[1]), "journalId": row[0]} for row in db.execute(
            "SELECT id, receipt FROM native_qer_receipts WHERE round_id=? ORDER BY id", (round_id,))]
    return actions, reads


def configured_journal(config_path: Path, expected_sha256: str) -> Path:
    """Bind acceptance to the backend's pinned private startup configuration."""
    if (not config_path.is_absolute() or config_path.is_symlink()
            or not isinstance(expected_sha256, str) or len(expected_sha256) != 64):
        raise ValueError("Pinned private AI100 configuration is required")
    raw = config_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("AI100 startup configuration digest differs")
    config = json.loads(raw)
    path = Path(config["journalPath"])
    if (config.get("schemaVersion") not in {"cityverse.adaptive-ai100-startup/v1",
                                              "cityverse.adaptive-ai100-trial/v1"}
            or not path.is_absolute() or path.is_symlink() or not path.is_file()):
        raise ValueError("Configured private QER journal is unavailable")
    if os.name == "posix":
        for item in (config_path, path):
            details = item.stat()
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_mode & 0o077):
                raise ValueError("AI100 configuration or QER journal is not owner-private")
    return path.resolve(strict=True)


def read_graph_evidence(journal: Path, round_id: str, generation: int, *, include_artifacts=False):
    """Read the exact round's private graph databases without creating tables."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    thread = f"{round_id}:{generation}"
    values = {}
    for suffix in ("graph-evidence", "graph-checkpoints"):
        path = journal.with_name(journal.stem + "." + suffix + ".db")
        if not path.is_file() or path.is_symlink():
            raise ValueError("LangGraph evidence is missing")
        if os.name == "posix" and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077):
            raise ValueError("LangGraph evidence is not owner-private")
        with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
            if suffix == "graph-evidence":
                values["events"] = [json.loads(row[0]) for row in db.execute(
                    "SELECT value FROM graph_events WHERE thread_id=? ORDER BY ordinal", (thread,))]
                values["outcomes"] = [json.loads(row[0]) for row in db.execute(
                    "SELECT value FROM graph_outcomes WHERE thread_id=? ORDER BY cycle", (thread,))]
                values["operations"] = [json.loads(row[0]) for row in db.execute(
                    "SELECT value FROM graph_operations WHERE thread_id=?", (thread,))]
                if include_artifacts:
                    values["artifacts"] = {row[0]: json.loads(row[1]) for row in db.execute(
                        "SELECT ref,value FROM graph_artifacts WHERE thread_id=?", (thread,))}
            else:
                row = db.execute("SELECT type,checkpoint FROM checkpoints WHERE thread_id=? "
                                 "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1", (thread,)).fetchone()
                if row is None:
                    raise ValueError("LangGraph checkpoint is missing")
                # Only deserialize the existing checkpoint; do not call setup/get_tuple,
                # whose PRAGMA/schema setup is inappropriate for a read-only verifier.
                values["state"] = SqliteSaver(db).serde.loads_typed((row[0], row[1]))["channel_values"]
    return values


def _validation_track(track):
    if track not in {"effect", "normal", "constrained"}:
        raise ValueError("validation_track.invalid")


def _constrained_claim(result, receipt, config_sha256):
    from backend.onprem.validation.normal import cohort_digest
    from backend.onprem.validation.constrained import probe_workload_contract_digest
    if not isinstance(receipt, dict) or receipt.get("state") != "released" or receipt.get("writePossible") is not False:
        raise ValueError("Controlled fixture is not durably restored and released")
    manifest, claim = receipt.get("manifest"), receipt.get("claim")
    if (not isinstance(manifest, dict) or not isinstance(claim, dict)
            or set(manifest) != {"schema", "purpose", "track", "requestId", "events", "configSha256",
                "workloadContractDigest", "profileId", "fixtureCapKbps", "probeKbpsPerDirection", "zeroSource", "quarantineRegistry"}
            or not isinstance(manifest.get("zeroSource"), dict)
            or set(manifest["zeroSource"]) != {"roundSha256", "ueStatusSha256"}
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
                   for v in manifest["zeroSource"].values())
            or not isinstance(manifest.get("quarantineRegistry"), str) or not manifest["quarantineRegistry"].startswith("/")):
        raise ValueError("Controlled fixture manifest or owner claim is missing")
    owner = receipt.get("ownerSnapshot")
    if (not isinstance(owner, dict) or any(owner.get(k) != result.get(k) for k in ("roundId", "generation", "requestId", "fleetLease"))
            or cohort_digest(owner.get("perUe")) != cohort_digest(result["perUe"])):
        raise ValueError("Controlled fixture durable native owner differs from the visible round")
    published = [a for a in result.get("actions", []) if a.get("kind") == "scenario_validation_track"]
    if (len(published) != 1 or published[0].get("accepted") is not True or published[0].get("fixtureClaim") != claim
            or manifest.get("schema") != "cityverse.scenario-constrained-fixture/v1"
            or manifest.get("purpose") != "controlled_qer_fixture" or manifest.get("track") != "constrained"
            or receipt.get("manifestDigest") != _digest(manifest) or claim.get("manifestDigest") != receipt["manifestDigest"]
            or receipt.get("guardId") != _digest({"requestId": result.get("requestId"), "manifestDigest": receipt["manifestDigest"]})
            or any(claim.get(k) != manifest.get(k) for k in ("schema", "purpose", "track", "requestId", "events",
                "configSha256", "workloadContractDigest", "profileId", "fixtureCapKbps", "probeKbpsPerDirection"))
            or claim.get("requestId") != result.get("requestId") or claim.get("roundId") != result.get("roundId")
            or type(claim.get("generation")) is not int or claim["generation"] != result.get("generation")
            or claim.get("events") != result.get("allocation", {}).get("events")
            or claim.get("configSha256") != config_sha256
            or claim.get("fleetLeaseDigest") != _digest(result.get("fleetLease"))
            or claim.get("cohortSha256") != cohort_digest(result["perUe"])
            or claim.get("workloadContractDigest") != probe_workload_contract_digest(claim.get("events"), claim.get("probeKbpsPerDirection"))
            or not isinstance(receipt.get("guardId"), str) or len(receipt["guardId"]) != 64
            or any(c not in "0123456789abcdef" for c in receipt["guardId"])):
        raise ValueError("Controlled fixture request, backend claim, and private receipt differ")
    # Reuse the strict producer's exact claim and uniform frozen-load validation.
    workload_metadata_for_snapshot(result)
    return claim


def _constrained_flow(job, report, stage, actions):
    """Keep raw UDP accounting; capped receiver delivery is a different objective."""
    sample = _flow(job, report, stage)
    installed = False
    if stage == "measuring_result":
        for action in actions:
            attempts = action.get("attempts", [])
            applied = [p.get("observedAt") for p in attempts if p.get("phase") == "apply"]
            restored = [p.get("observedAt") for p in attempts if p.get("phase") == "restore"]
            if (job.supi in action.get("treatmentSupis", []) and applied and restored
                    and all(type(t) in (int, float) and math.isfinite(t) for t in applied + restored)
                    and max(applied) <= report["effectiveStartedAt"] <= report["effectiveFinishedAt"] <= min(restored)):
                installed = True
    if installed:
        if not sample.passed:
            raise ValueError("Uncapped treatment failed the unchanged healthy delivery contract")
    elif not (.8 <= sample.offered_applied_ratio <= 1.2
              and 0 < sample.received_applied_ratio < 1
              and sample.conservative_unique_applied_ratio > 0):
        raise ValueError("Controlled baseline or holdout is not a positive, actually constrained receiver measurement")
    return sample


def _verify_constrained_restoration(result, receipt, claim, actions, qualified_windows):
    from backend.onprem.validation.native import validate_native_receipt, same_rule_state
    if len(actions) > 1:
        raise ValueError("fixture.multiple_interventions_unsupported")
    if len(actions) != 1 or receipt.get("modelActionId") != actions[0].get("id"):
        raise ValueError("Controlled fixture must bind one genuine model effect action")
    stages = (("originalNative", "original"), ("fixtureInstalledNative", "fixture_installed"),
              ("innerRestoredNative", "inner_restored"), ("originalRestoredNative", "original_restored"))
    receipts = [validate_native_receipt(receipt.get(key), result, claim, phase) for key, phase in stages]
    stamps = [seconds(r["observedAt"]) for r in receipts]
    if any(not math.isfinite(t) for t in stamps) or stamps != sorted(stamps):
        raise ValueError("Controlled fixture native seals are not ordered")
    original, installed, inner, restored = [r["native"] for r in receipts]
    if not same_rule_state(original["perUe"], restored["perUe"]):
        raise ValueError("Controlled fixture did not restore every original QER and PDR")
    for name, qos in original["qosBySlice"].items():
        if any(restored["qosBySlice"].get(name, {}).get(k) != qos.get(k) for k in ("epoch", "enabled", "readback")):
            raise ValueError("Controlled fixture changed original QoS state or epoch")
    if not same_rule_state(installed["perUe"], inner["perUe"]):
        raise ValueError("Model cleanup did not restore the exact installed fixture before outer restoration")
    if any(not stamps[1] <= report["effectiveStartedAt"] <= report["effectiveFinishedAt"] <= stamps[2]
           for window in qualified_windows for _, _, reports in window.values() for _, report in reports.values()):
        raise ValueError("Controlled traffic is outside the independently sealed inner fixture lifetime")
    original_rates = {r["supi"]: r["originalMbrKbps"] for r in original["perUe"]}
    attempts = receipt.get("acceptedSmfAttempts")
    if (not isinstance(attempts, list) or len(attempts) != 2 * len(original_rates)
            or any(not isinstance(a, dict) or set(a) != {"intentId", "supi", "phase", "rates", "httpStatus",
                "pfcpAccepted", "observedAt", "uncertain"} for a in attempts)
            or any(not isinstance(a["intentId"], str) or not a["intentId"] for a in attempts)
            or len({a["intentId"] for a in attempts}) != 2 * len(original_rates)):
        raise ValueError("Controlled fixture durable SMF intents and results are incomplete")
    for phase, lower, upper in (("fixture_install", stamps[0], stamps[1]), ("fixture_restore", stamps[2], stamps[3])):
        rows = [a for a in attempts if a["phase"] == phase]
        if len(rows) != len(original_rates) or {a["supi"] for a in rows} != set(original_rates):
            raise ValueError("Controlled fixture SMF phase does not cover the exact cohort once")
        for attempt in rows:
            expected = ({"ul": claim["fixtureCapKbps"], "dl": claim["fixtureCapKbps"]}
                        if phase == "fixture_install" else original_rates[attempt["supi"]])
            rates = attempt["rates"]
            if (not isinstance(rates, dict) or set(rates) != {"ul", "dl"}
                    or any(type(v) is not int for v in rates.values()) or rates != expected
                    or type(attempt["httpStatus"]) is not int or attempt["httpStatus"] != 200
                    or attempt["pfcpAccepted"] is not True or attempt["uncertain"] is not False
                    or not isinstance(attempt["observedAt"], str)):
                raise ValueError("Controlled fixture accepted SMF result differs from its immutable intent")
            observed = seconds(attempt["observedAt"])
            if not math.isfinite(observed) or not lower <= observed <= upper:
                raise ValueError("Controlled fixture accepted SMF result is outside its native seal interval")
    for action in actions:
        if action.get("profileId") != "event-2mbps" or action.get("profileRates") != {"ul": 2000, "dl": 2000}:
            raise ValueError("Controlled effect is not the real commissioned 2Mbps model action")
        for prior in action["baseline"].values():
            bounded = [q for q in prior["qers"] if any(q["mbrKbps"].values())]
            if len(bounded) != 1 or bounded[0]["mbrKbps"] != {"ul": claim["fixtureCapKbps"], "dl": claim["fixtureCapKbps"]}:
                raise ValueError("Model action did not begin from the installed fixture cap")
        for attempt in action["attempts"]:
            if not stamps[1] <= attempt["observedAt"] <= stamps[2]:
                raise ValueError("Model policy action is outside the inner fixture lifetime")
    return {"validationTrack": "constrained", "successType": "controlled_qer_effect_verified",
            "qerEffectVerified": True, "fixtureRestored": True, "manifestDigest": claim["manifestDigest"],
            "workloadKind": "diagnostic_probe", "profileId": claim["profileId"],
            "fixtureCapKbps": claim["fixtureCapKbps"], "probeKbpsPerDirection": claim["probeKbpsPerDirection"]}


def _normal_observation(result, observation):
    from backend.onprem.validation.normal import cohort_digest
    try:
        snapshot = observation["_actuatorSnapshot"]
        if (native_observation(snapshot) != observation or cohort_digest(snapshot["perUe"]) != cohort_digest(result["perUe"])
                or _digest(snapshot.get("fleetLease")) != _digest(result.get("fleetLease"))
                or _published_workload(snapshot)["comparisonFingerprint"] != _published_workload(result)["comparisonFingerprint"]):
            raise ValueError("Normal observation differs from its owned native snapshot")
        stage = snapshot["stage"]
        aggregates = [a for a in snapshot["actions"] if a.get("kind") == "traffic_aggregate" and a.get("stage") == stage]
        aggregate = aggregates[-1]
        published = {a.get("id"): a for a in result["actions"] if a.get("id")}
        if published.get(aggregate["id"]) != aggregate:
            raise ValueError("Normal result window is not independently published")
        reports = {}
        for identifier in aggregate["shardActionIds"]:
            window = next(a for a in snapshot["actions"] if a.get("id") == identifier)
            if published.get(identifier) != window:
                raise ValueError("Normal shard window differs from published evidence")
            for report in window["reports"]:
                key = (report["binding"]["supi"], report["direction"])
                if key in reports:
                    raise ValueError("Normal observation repeats a directional report")
                reports[key] = report
        if len(reports) != 2 * len(result["perUe"]):
            raise ValueError("Normal observation has no complete native receiver window")
        for row in observation["perUe"]:
            for direction, metric in (("ul", "receivedUplinkMbps"), ("dl", "receivedDownlinkMbps")):
                if row[metric] != reports[(row["supi"], direction)]["receivedMbps"]:
                    raise ValueError("Normal metric differs from its actual receiver report")
        return aggregate["id"]
    except (KeyError, TypeError, IndexError, StopIteration) as exc:
        raise ValueError("Normal observation lacks a fresh complete native result") from exc


def _published_workload(snapshot):
    """The native ledger freezes plans on traffic actions, not a top-level field."""
    expected = workload_metadata_for_snapshot(snapshot)
    plans = [a.get("workload") for a in snapshot.get("actions", []) if a.get("kind") == "traffic_window"]
    if (not plans or any(_digest(plan) != _digest(expected) for plan in plans)
            or "workload" in snapshot and _digest(snapshot["workload"]) != _digest(expected)):
        raise ValueError("Published native traffic actions do not freeze one exact workload")
    return expected


def _successful_graph_stop(state, outcomes):
    if state.get('stop_reason') == 'scenario_ended':
        return True
    cycle = state.get('cycle')
    return (state.get('stop_reason') == 'cycle_limit_reached'
            and type(cycle) is int and cycle > 0 and isinstance(outcomes, list)
            and len(outcomes) == cycle
            and all(isinstance(row, dict) and type(row.get('iteration')) is int
                    and row['iteration'] == index for index, row in enumerate(outcomes)))


def _normal_graph_evidence(result, actions, graph, native_reads):
    state, events = graph.get("state", {}), graph.get("events", [])
    expected = next(a["outcomes"] for a in result["actions"] if a.get("kind") == "adaptive_ai_outcomes")
    if (actions or graph.get("operations") != [] or not expected or graph.get("outcomes") != expected
            or state.get("thread_id") != f"{result['roundId']}:{result['generation']}"
            or state.get("phase") != "completed" or state.get("error_code")
            or not state.get("contract_digest") or not _successful_graph_stop(state, graph.get('outcomes'))):
        raise ValueError("Normal graph completion or no-write provenance is invalid")
    from backend.onprem.agents.roles import PHASE_ROLES
    completed = [e for e in events if e.get("status") == "completed"]
    # Agent entry/model wait/preload/decision metadata is not a completed tool
    # response and does not carry model provenance. Only completed tools can
    # authorize the linked hold proof; failures anywhere still reject the run.
    if (any(e.get("status") == "failed" for e in events)
            or any(e.get("role") != PHASE_ROLES.get(e.get("phase"))
                   or not e.get("model") or not e.get("response_id") for e in completed)):
        raise ValueError("Normal graph role provenance is invalid")
    responses = {role: sorted({e["response_id"] for e in events if e.get("status") == "completed" and e["role"] == role})
                 for role in ("planner", "executor", "validator")}
    if any(not ids for ids in responses.values()) or len(set().union(*map(set, responses.values()))) != sum(map(len, responses.values())):
        raise ValueError("Normal graph does not have independent role responses")
    artifacts = graph.get("artifacts", {})
    def get(ref, kind):
        value = artifacts.get(ref)
        if not isinstance(value, dict) or ref != kind + ":" + _digest(value):
            raise ValueError("Normal graph artifact reference is invalid")
        return value
    def event(role, phase, tool, response, cycle):
        if len([e for e in events if (e.get("role"), e.get("phase"), e.get("tool"),
                e.get("response_id"), e.get("cycle"), e.get("status"))
                == (role, phase, tool, response, cycle, "completed")
                and type(e.get("cycle")) is int]) != 1:
            raise ValueError("Normal graph terminal decision linkage is unverified")
    if not isinstance(native_reads, list) or len(native_reads) < 3:
        raise ValueError("Normal graph has no independent native inventory receipts")
    def native_match(sequence, digest):
        return any(r.get("sequence") == sequence and _digest({s["supi"]: s for s in r["proof"]["sessions"]}) == digest
                   for r in native_reads)
    native_digest = None
    for outcome in expected:
        cycle = outcome.get("iteration")
        if (type(cycle) is not int or cycle < 0 or outcome.get("outcome") != "no_op"
                or outcome.get("verificationKind") != "policy_maintained"
                or outcome.get("candidateId") is not None or outcome.get("improvementPercent") is not None):
            raise ValueError("Normal graph outcome is not a verified hold")
        try:
            plan = get(outcome["planRef"], "plan")
            proof = get(outcome["validatedRef"], "validated")
            verdict = get(outcome["verificationRef"], "verdict")
            before, after = get(outcome["beforeRef"], "observation"), get(outcome["afterRef"], "observation")
            confirmation = proof["hold_confirmation"]
            seal = proof.get("hold_seal", {})
            initial = get(confirmation.get("validated_ref", confirmation.get("validated_plan_ref")), "validated")
            if (plan.get("action") != "hold" or plan.get("group_id") is not None or plan.get("profile_id") is not None
                    or plan.get("response_id") != outcome.get("responseId")
                    or plan.get("before_ref") != outcome["beforeRef"]
                    or proof.get("plan_ref") != outcome["planRef"] or proof.get("before_ref") != outcome["beforeRef"]
                    or proof.get("fresh_hold") is not True or proof.get("selected") is not None or proof.get("replace")
                    or initial != {k: v for k, v in proof.items() if k != "hold_confirmation"}
                    or verdict.get("hold_verified") is not True or verdict.get("outcome") != "no_op"
                    or verdict.get("restore") is not False or verdict.get("improvement_percent") is not None
                    or verdict.get("after_ref") != outcome["afterRef"]
                    or not isinstance(proof.get("qer_before"), dict) or len(proof["qer_before"]) != len(result["perUe"])
                    or verdict.get("native_digest") != _digest(proof["qer_before"])
                    or seal.get("schema") != "cityverse.validated-traffic-before/v1"
                    or seal.get("before_ref") != outcome["beforeRef"] or seal.get("round_id") != before["roundId"]
                    or seal.get("generation") != before["generation"] or seal.get("sequence") != before["sequence"]
                    or seal.get("rows_digest") != _digest(before["perUe"])
                    or type(seal.get("validated_at")) not in (int, float)
                    or not -1 <= seal["validated_at"] - seconds(before["observedAt"]) <= ScenarioContract.maximum_evidence_age_seconds
                    or any(o.get("roundId") != result["roundId"] or o.get("generation") != result["generation"]
                           for o in (before, after))
                    or type(before.get("sequence")) is not int or type(after.get("sequence")) is not int
                    or after["sequence"] <= before["sequence"]
                    or after.get("policyRevision") != before.get("policyRevision")):
                raise ValueError("Normal graph hold references or native result are invalid")
            event("planner", "plan", "propose_policy", plan["response_id"], cycle)
            event("validator", "precheck", "check_proposal", proof["precheck_response_id"], cycle)
            event("executor", "apply", "execute_decision", confirmation["executor_response_id"], cycle)
            event("validator", "postcheck", "check_effect", verdict["validator_response_id"], cycle)
            _check_lesson_event(events, cycle)
            if native_digest is not None and native_digest != verdict["native_digest"]:
                raise ValueError("Normal graph native inventory changed between cycles")
            native_digest = verdict["native_digest"]
            if not native_match(before["sequence"], native_digest) or not native_match(after["sequence"], native_digest):
                raise ValueError("Normal graph native inventory differs from independent readback")
            if _normal_observation(result, before) == _normal_observation(result, after):
                raise ValueError("Normal hold reused its pre-decision traffic window")
        except (KeyError, TypeError) as exc:
            raise ValueError("Normal graph hold proof is incomplete") from exc
    if len({o["iteration"] for o in expected}) != len(expected):
        raise ValueError("Normal graph repeats a hold cycle")
    if any(_digest({s["supi"]: s for s in r["proof"]["sessions"]}) != native_digest for r in native_reads):
        raise ValueError("Normal graph original, result and final native readbacks differ")
    cleanups = [v for ref, v in artifacts.items() if ref.startswith("hold_cleanup:")
                and ref == "hold_cleanup:" + _digest(v) and v.get("cleanup_verified") is True
                and v.get("verified_after_ref") == expected[-1]["afterRef"] and v.get("native_digest") == native_digest]
    if len(cleanups) != 1:
        raise ValueError("Normal graph final native inventory is unverified")
    final = get(cleanups[0]["after_ref"], "observation")
    _normal_observation(result, final)
    last_read = max(native_reads, key=lambda r: r["journalId"])
    verified_reads = [r["journalId"] for r in native_reads if r.get("sequence") == after["sequence"]]
    if (final["sequence"] < after["sequence"] or last_read.get("sequence") != final["sequence"]
            or not verified_reads or max(verified_reads) > last_read["journalId"]
            or min(verified_reads) >= last_read["journalId"]):
        raise ValueError("Normal graph final readback does not follow its verified result")
    completed = [e for e in events if e.get("status") == "completed"]
    if any(e.get("role") == "executor" and e.get("tool") not in {"get_validated_decision", "get_action_status", "execute_decision"}
           for e in completed):
        raise ValueError("Normal graph contains an unexpected executor operation")
    return {"agentRoles": responses,
            "graphPhase": "completed", "graphCycles": state.get("cycle"),
            "graphOutcomes": len(expected), "graphOperations": 0}


def _verify_normal(result, actions, reads, decisions, restoration, receipt, config_sha256):
    import re
    from backend.onprem.validation.normal import cohort_digest, workload_contract_digest, fleet_ownership_valid
    fields = {"schema", "purpose", "track", "requestId", "events", "configSha256",
              "workloadContractDigest", "manifestDigest", "roundId", "generation", "fleetLeaseDigest",
              "comparisonFingerprint", "cohortSha256"}
    published = [a for a in result["actions"] if a.get("kind") == "scenario_validation_track"]
    events = result["allocation"]["events"]
    contract = workload_contract_digest(events)
    if (not isinstance(receipt, dict) or set(receipt) != fields or len(published) != 1
            or published[0].get("accepted") is not True or published[0].get("fixtureClaim") != receipt
            or receipt.get("schema") != "cityverse.scenario-fixture/v1"
            or receipt.get("purpose") != "normal_no_write" or receipt.get("track") != "normal"
            or receipt.get("requestId") != result["requestId"] or receipt.get("roundId") != result["roundId"]
            or type(receipt.get("generation")) is not int or receipt["generation"] != result["generation"]
            or receipt.get("events") != events or not re.fullmatch("[0-9a-f]{64}", config_sha256 or "")
            or receipt.get("configSha256") != config_sha256
            or receipt.get("workloadContractDigest") != contract
            or receipt.get("comparisonFingerprint") != _published_workload(result)["comparisonFingerprint"]
            or receipt.get("cohortSha256") != cohort_digest(result["perUe"])
            or not fleet_ownership_valid(result)
            or receipt.get("fleetLeaseDigest") != _digest(result["fleetLease"])
            or any(not isinstance(receipt.get(k), str) or not re.fullmatch("[0-9a-f]{64}", receipt[k])
                   for k in ("manifestDigest", "fleetLeaseDigest"))):
        raise ValueError("Normal requested track, backend receipt and private manifest differ")
    if (actions != [] or restoration.get("policyActionIds") != [] or not isinstance(decisions, list) or not decisions
            or any(d.get("outcome") != "no_op" or d.get("verificationKind") != "policy_maintained"
                   or d.get("candidateId") is not None or d.get("improvementPercent") is not None
                   or not isinstance(d.get("responseId"), str) or not d["responseId"] for d in decisions)):
        raise ValueError("Normal round is not a no-write verified hold")
    owners = {r["supi"]: r for r in result["perUe"]}
    inventories, identifiers = [], set()
    for read in reads:
        try:
            proof, inventory = read["proof"], {}
            if (read.get("roundId") != result["roundId"] or read.get("generation") != result["generation"]
                    or read.get("cohortSha256") != _cohort(result["perUe"])
                    or type(read.get("journalId")) is not int or read["journalId"] in identifiers
                    or read.get("actionId") is not None or read.get("phase") is not None
                    or proof.get("source") != "native-upf-qer" or len(proof["sessions"]) != len(result["perUe"])
                    or type(read.get("capturedAt")) not in (int, float) or not math.isfinite(read["capturedAt"])
                    or not -1 <= read["capturedAt"] - seconds(proof["observedAt"]) <= 15):
                raise ValueError("Normal native inventory receipt is unavailable or stale")
            identifiers.add(read["journalId"])
            for session in proof["sessions"]:
                supi = session["supi"]
                if supi not in owners or supi in inventory or any(session.get(k) != owners[supi].get(k) for k in
                        ("sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf", "sst", "sd")):
                    raise ValueError("Normal native inventory differs from the exact owned PDU")
                qers = session["qers"]
                if (not isinstance(qers, list) or not qers or len({q["id"] for q in qers}) != len(qers)
                        or any(type(q["id"]) is not int or not isinstance(q["pdrIds"], list) or not q["pdrIds"]
                               or any(type(i) is not int for i in q["pdrIds"])
                               or not isinstance(q["mbrKbps"], dict) or set(q["mbrKbps"]) != {"ul", "dl"}
                               or any(type(v) is not int or not 0 <= v <= 500000 for v in q["mbrKbps"].values())
                               for q in qers)):
                    raise ValueError("Normal native QER inventory is malformed")
                bounded = [q for q in qers if any(q["mbrKbps"].values())]
                if len(bounded) != 1 or any(v <= 0 for v in bounded[0]["mbrKbps"].values()):
                    raise ValueError("Normal native AMBR QER is ambiguous")
                inventory[supi] = session
            inventories.append(inventory)
        except (KeyError, TypeError) as exc:
            raise ValueError("Normal native inventory proof is incomplete") from exc
    if len(inventories) < 3 or any(i != inventories[0] for i in inventories):
        raise ValueError("Normal original, result and final native inventories are not unchanged")
    return {"roundId": result["roundId"], "generation": result["generation"], "ueCount": len(result["perUe"]),
            "modelResponses": [d["responseId"] for d in decisions], "qerActions": 0,
            "nativeQerReads": len(inventories), "released": True, "validationTrack": "normal",
            "successType": "policy_maintained_verified", "qerEffectVerified": False,
            "manifestDigest": receipt["manifestDigest"]}


def _check_lesson_event(events, cycle):
    matching = [event for event in events if event.get("tool") == "record_lesson"
                and event.get("status") == "completed" and event.get("cycle") == cycle
                and type(event.get("cycle")) is int]
    # Replaying a Validator node after a crash can finish the same idempotent
    # lesson tool again before the outer node checkpoint is committed.
    if (not matching or any(event.get("role") != "validator"
                           or event.get("phase") != "learn" for event in matching)):
        raise ValueError("LangGraph cycle has no independent Validator memory record")


def verify_graph_evidence(result, actions, graph, *, validation_track="effect", native_reads=None):
    """Require actual three-role terminal tool calls and matching durable outcomes."""
    _validation_track(validation_track)
    if validation_track == "normal":
        return _normal_graph_evidence(result, actions, graph, native_reads)
    from backend.onprem.agents.roles import PHASE_ROLES
    state = graph.get("state", {})
    thread = f"{result['roundId']}:{result['generation']}"
    if (state.get("thread_id") != thread or state.get("phase") != "completed"
            or state.get("error_code") or not state.get("contract_digest")
            or not _successful_graph_stop(state, graph.get('outcomes'))):
        raise ValueError("LangGraph round did not complete successfully")
    events = graph.get("events", [])
    completed = [e for e in events if e.get("status") == "completed"]
    required = {("planner", "plan", "propose_policy"), ("validator", "precheck", "check_proposal"),
                ("executor", "apply", "execute_decision"), ("validator", "postcheck", "check_effect"),
                ("validator", "learn", "record_lesson")}
    if not required <= {(e.get("role"), e.get("phase"), e.get("tool")) for e in completed}:
        raise ValueError("Three LLM role tools are not independently evidenced")
    responses = {role: sorted({e.get("response_id") for e in completed if e.get("role") == role
                              and isinstance(e.get("response_id"), str) and e["response_id"]})
                 for role in ("planner", "executor", "validator")}
    if (any(not ids for ids in responses.values()) or any(e.get("status") == "failed" for e in events)
            or any(e.get("role") != PHASE_ROLES.get(e.get("phase")) or not e.get("model")
                   or not e.get("response_id") for e in completed)):
        raise ValueError("LangGraph role or model provenance is invalid")
    expected = next(a["outcomes"] for a in result["actions"] if a.get("kind") == "adaptive_ai_outcomes")
    if graph.get("outcomes") != expected:
        raise ValueError("LangGraph outcomes differ from the published round")
    for outcome in expected:
        _check_lesson_event(events, outcome.get("iteration"))
    operations = graph.get("operations", [])
    if (not operations or any(op.get("state") != "completed" for op in operations)
            or {op.get("receipt", {}).get("id") for op in operations} != {a["id"] for a in actions}
            or any(a.get("modelResponseId") not in responses["planner"] for a in actions)):
        raise ValueError("LangGraph operations differ from native QER journal")
    for action in actions:
        matching = [d for d in expected if d.get("candidateId") == action.get("candidateId")
                    and d.get("responseId") == action.get("modelResponseId")
                    and d.get("outcome") == "effect_verified"]
        if (not isinstance(action.get("candidateId"), str) or not action["candidateId"]
                or len(matching) != 1 or type(matching[0].get("iteration")) is not int
                or matching[0]["iteration"] < 0 or not any(
                    e.get("role") == "planner" and e.get("phase") == "plan"
                    and e.get("tool") == "propose_policy"
                    and e.get("response_id") == action.get("modelResponseId")
                    and type(e.get("cycle")) is int and e["cycle"] == matching[0]["iteration"]
                    for e in completed)):
            raise ValueError("LangGraph action decision linkage is unverified")
    if validation_track == "constrained":
        _constrained_graph_seals(result, actions, graph)
    return {"agentRoles": responses, "graphPhase": state["phase"], "graphCycles": state.get("cycle"),
            "graphOutcomes": len(expected), "graphOperations": len(operations)}


def _constrained_graph_seals(result, actions, graph):
    """Historical evidence is accepted only through the real precheck artifact chain."""
    artifacts = graph.get("artifacts", {})
    def get(ref, kind):
        value = artifacts.get(ref)
        if not isinstance(value, dict) or ref != kind + ":" + _digest(value):
            raise ValueError("Controlled graph artifact reference is invalid")
        return value
    for action in actions:
        decision = next(d for d in graph["outcomes"] if d.get("candidateId") == action.get("candidateId")
                        and d.get("responseId") == action.get("modelResponseId") and d.get("outcome") == "effect_verified")
        installed = get(decision.get("actionRef"), "action")
        if installed.get("receipt", {}).get("id") != action["id"]:
            raise ValueError("Controlled graph has no exact immutable native action")
        before = get(installed.get("before_ref"), "observation")
        proof = get(installed.get("validated_ref"), "validated")
        plan = get(proof.get("plan_ref"), "plan")
        seal = installed.get("before_seal")
        cycle = decision["iteration"]
        if (not isinstance(seal, dict) or seal != proof.get("hold_seal")
                or seal.get("schema") != "cityverse.validated-traffic-before/v1"
                or seal.get("before_ref") != installed.get("before_ref")
                or seal.get("round_id") != result["roundId"] or seal.get("generation") != result["generation"]
                or type(seal.get("generation")) is not int or type(seal.get("sequence")) is not int
                or seal["sequence"] != before["sequence"] or seal.get("rows_digest") != _digest(before["perUe"])
                or type(seal.get("validated_at")) not in (int, float) or not math.isfinite(seal["validated_at"])
                or not -1 <= seal["validated_at"] - seconds(before["observedAt"]) <= 120
                or before["roundId"] != result["roundId"] or before["generation"] != result["generation"]
                or proof.get("before_ref") != installed.get("before_ref")
                or plan.get("before_ref") != installed.get("before_ref") or plan.get("action") != "apply"
                or plan.get("group_id") != action.get("eventId") or plan.get("profile_id") != action.get("profileId")
                or plan.get("response_id") != action.get("modelResponseId")
                or proof.get("selected") != installed.get("selected")
                or installed.get("selected", {}).get("id") != action.get("candidateId")
                or any(installed.get("selected", {}).get(k) != action.get(v) for k, v in (
                    ("eventId", "eventId"), ("profileId", "profileId"), ("treatmentSupis", "treatmentSupis"), ("holdoutSupis", "holdoutSupis")))
                or not isinstance(proof.get("qer_before"), dict) or len(proof["qer_before"]) != len(result["perUe"])
                or installed.get("qer_before") != proof["qer_before"]
                or set(proof["qer_before"]) != set(action["baseline"])
                or any(row.get("qers") != action["baseline"][supi].get("qers") for supi, row in proof["qer_before"].items())
                or not any(e.get("role") == "validator" and e.get("phase") == "precheck"
                    and e.get("tool") == "check_proposal" and e.get("status") == "completed"
                    and e.get("response_id") == proof.get("precheck_response_id")
                    and type(e.get("cycle")) is int and e["cycle"] == cycle for e in graph.get("events", []))
                or not any(e.get("role") == "executor" and e.get("phase") == "apply"
                    and e.get("tool") == "execute_decision" and e.get("status") == "completed"
                    and e.get("response_id") == installed.get("executor_response_id")
                    and type(e.get("cycle")) is int and e["cycle"] == cycle for e in graph.get("events", []))):
            raise ValueError("Controlled graph historical before was not validated by the actual LLM precheck")
        _normal_observation(result, before)


def _persistent_completion_exact(result, cleanup, proof):
    """Recognize an explicitly stopped, already-completed five-cycle session.

    This changes no stored outcome. Every native effect, rollback, rendered
    outcome and fleet-release check below still consumes the original records.
    """
    if type(proof) is not dict or set(proof) != {'held', 'stop'}:
        return False
    held, stop = proof['held'], proof['stop']
    if type(held) is not dict or type(stop) is not dict or result != cleanup:
        return False
    if (result.get('stage') != 'completed' or result.get('executionOutcome') != 'cancelled'
            or result.get('cleanupStatus') != 'verified'
            or result.get('errors') != ['Execution blocked: Round cancelled']
            or not round_resources_released(result)):
        return False
    if any(held.get(k) != result.get(k) for k in
           ('roundId', 'generation', 'requestId', 'allocation', 'perUe', 'strategy')):
        return False
    life, prior = result.get('lifecycle', {}), held.get('lifecycle', {})
    common = {'schemaVersion': 'cityverse.scenario-lifecycle/v1',
              'evaluationState': 'completed', 'maximumCycles': 5,
              'cyclesAttempted': 5, 'cyclesCompleted': 5,
              'stopReason': 'cycle_limit_reached'}
    if (any(life.get(k) != v or prior.get(k) != v for k, v in common.items())
            or life.get('scenarioState') != 'stopped' or life.get('resourceState') != 'released'
            or prior.get('scenarioState') != 'running' or prior.get('resourceState') != 'held'
            or held.get('executionOutcome') is not None or held.get('errors') != []
            or type(held.get('sequence')) is not int or type(result.get('sequence')) is not int
            or held['sequence'] >= result['sequence']):
        return False
    agent = held.get('agentRuntime', {})
    at, done = stop.get('at'), agent.get('updatedAt')
    if (stop.get('roundId') != result.get('roundId')
            or stop.get('reason') != 'five-cycles-then-persistent-hold-observed'
            or agent.get('roundId') != result.get('roundId')
            or agent.get('generation') != result.get('generation')
            or agent.get('phase') != 'completed' or agent.get('status') != 'completed'
            or type(at) not in (int, float) or type(done) not in (int, float)
            or not math.isfinite(at) or not math.isfinite(done) or at < done + 15):
        return False
    return True


def verify(result, rendered, cleanup, intent, actions, reads, workload_dom, fleet_release=None, *,
           validation_track="effect", fixture_receipt=None, config_sha256=None, expected_ues=100,
           persistent_completion=None, terminal_observation=None):
    """Return verified summary or raise ValueError on any missing exact proof."""
    _validation_track(validation_track)
    if type(expected_ues) is not int or not 1 <= expected_ues <= 100:
        raise ValueError("Expected UE population must be an integer in1..100")
    rid = result.get("roundId")
    persistent_stopped = _persistent_completion_exact(result, cleanup, persistent_completion)
    expected_outcome = 'cancelled' if persistent_stopped else 'passed'
    terminal = result.get('stage') == 'completed'
    terminal_exact = (terminal and result == cleanup and round_resources_released(result)
                      and result.get('executionOutcome') == expected_outcome)
    # Terminal UI corroborates historical evidence, never current traffic.
    resolved_probes = {'Cleanup probe: release.fleet_identity_changed',
                       'Cleanup probe: release.known_resources_remain'}
    result_errors = result.get('errors') or []
    errors_resolved = (terminal_exact and isinstance(result_errors, list)
                       and (persistent_stopped or all(error in resolved_probes for error in result_errors)))
    generation = result.get("generation")
    allocation = result.get("allocation", {})
    event_counts = allocation.get("events") if isinstance(allocation, dict) else None
    if (not isinstance(rid, str) or not rid or type(generation) is not int
            or (result.get("stage") != "results_ready" and not terminal_exact) or result.get("executionOutcome") != expected_outcome
            or result.get("strategy") != "ai" or allocation.get("baselineUes") != 0
            or allocation.get("total") != expected_ues or not isinstance(event_counts, dict)
            or set(event_counts) != set(EVENT_SLICES)
            or any(type(count) is not int or count < 0 for count in event_counts.values())
            or sum(event_counts.values()) != expected_ues
            or not 1 <= len([v for v in event_counts.values() if v]) <= 4
            or intent.get("request", {}).get("events") != event_counts
            or (result_errors and not errors_resolved) or intent.get("status") != 202
            or intent.get("request", {}).get("strategy") != "ai"
            or intent.get("response", {}).get("roundId") != rid
            or intent["request"].get("requestId") != result.get("requestId")):
        raise ValueError("AI result or submitted intent differs from the exact requested UE round")
    owners = result.get("perUe")
    if (not isinstance(owners, list) or len(owners) != expected_ues
            or len({row.get("supi") for row in owners if isinstance(row, dict)}) != expected_ues):
        raise ValueError("Exact native UE cohort is unavailable")
    observed = {event: 0 for event in EVENT_SLICES}
    for row in owners:
        event_id = row.get("scenario")
        if event_id not in EVENT_SLICES or row.get("slice") != EVENT_SLICES[event_id]:
            raise ValueError("Owned UE event and slice differ from the submitted AI intent")
        observed[event_id] += 1
    if observed != event_counts:
        raise ValueError("Owned UE event and slice differ from the submitted AI intent")
    constrained_claim = (_constrained_claim(result, fixture_receipt, config_sha256)
                         if validation_track == "constrained" else None)
    plan = (plan_probe_workload(owners, probe_kbps=constrained_claim["probeKbpsPerDirection"])
            if constrained_claim else plan_workload(owners))
    metadata = workload_metadata_for_snapshot(result) if constrained_claim else workload_metadata(owners)
    panels = workload_dom.get("workload") if isinstance(workload_dom, dict) else None
    fields = ("profileId", "comparisonFingerprint", "budgetKbpsPerDirection",
              "requestedUplinkKbps", "requestedDownlinkKbps", "requestedCombinedKbps",
              "appliedUplinkKbps", "appliedDownlinkKbps", "appliedCombinedKbps")
    if constrained_claim:
        fields += ("workloadKind", "probeKbpsPerDirection")
    if (not isinstance(panels, list) or len(panels) != 1
            or any(panels[0].get(key) != str(metadata[key]) for key in fields)
            or panels[0].get("qosEffectClaimed") != "false"):
        raise ValueError("Visible frozen workload differs from the owned native plan")
    if (_published_workload(result)["comparisonFingerprint"] != plan.comparison_fingerprint
            or cleanup.get("roundId") != rid or cleanup.get("generation") != generation
            or (cleanup.get("sequence", -1) <= result.get("sequence", -1) and not terminal_exact)
            or cleanup.get("executionOutcome") != expected_outcome or not round_resources_released(cleanup)):
        raise ValueError("Frozen workload or exact owned cleanup is unverified")
    if not released(cleanup):
        proof = fleet_release if isinstance(fleet_release, dict) else {}
        zero = proof.get("ue", {})
        if (proof.get("roundId") != rid or proof.get("sourceGeneration") != generation
                or proof.get("podsAbsent") is not True or zero.get("status") != "stopped"
                or type(zero.get("generation")) is not int or zero["generation"] <= generation
                or any(zero.get(key) != 0 for key in (
                    "requested", "allocated", "registered", "pduEstablished", "trafficVerified", "failed"))
                or zero.get("errors") != []):
            raise ValueError("Retained fleet lacks independent zero-UE and Pod release proof")
    shown = rendered.get("evidence", [])
    if (rendered.get("alerts") or len(shown) != 1
            or shown[0].get("roundId") != rid
            or shown[0].get("generation") != str(generation)
            or shown[0].get("sequence") != str(result.get("sequence"))
            or shown[0].get("policyRevision") != str(result.get("policyRevision"))
            or shown[0].get("observedAt") != result.get("observedAt")
            or shown[0].get("stage") != result.get('stage')
            or (not terminal and shown[0].get("fresh") != "true")
            or shown[0].get("visibleMeasurements") != ('0' if terminal else str(expected_ues))
            or any(shown[0].get(key) != str(expected_ues) for key in
                   ("allocated", "registered", "pduEstablished", "trafficVerified"))
            or shown[0].get("trafficVisible") != ('false' if terminal else 'true')
            or (terminal and rendered.get('outcome') != [{'executionOutcome':expected_outcome,'cleanupStatus':'verified'}])):
        raise ValueError("Rendered AI result does not corroborate the native round")
    audit = result.get("actions", [])
    completed_actions = cleanup.get("actions")
    if (not isinstance(audit, list) or not isinstance(completed_actions, list)
            or completed_actions[:len(audit)] != audit):
        raise ValueError("AI result actions differ from the released round")
    aggregates = [a for a in audit if a.get("kind") == "traffic_aggregate"]
    baseline = [a for a in aggregates if a.get("stage") == "measuring_baseline"]
    result_windows = [a for a in aggregates if a.get("stage") == "measuring_result"]
    if len(baseline) != 1 or not result_windows:
        raise ValueError("Native before/after traffic aggregates are missing")
    qualified_windows = []
    for target in result_windows:
        shard_ids = set(baseline[0].get("shardActionIds", [])) | set(target.get("shardActionIds", []))
        projected = {**result, "actions": [a for a in audit if a is baseline[0]
                      or a is target or a.get("id") in shard_ids or a.get("kind") == "scenario_validation_track"]}
        try:
            windows = sharded_capacity_windows(projected, expected_ues=expected_ues)
            for stage, (_, _, reports) in windows.items():
                for job, report in reports.values():
                    if constrained_claim:
                        _constrained_flow(job, report, stage, actions)
                    elif not _flow(job, report, stage).passed:
                        raise ValueError("Native flow failed the receiver performance contract")
            qualified_windows.append(windows)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Native requested UE bidirectional traffic proof is incomplete") from exc
    try:
        if persistent_stopped and result.get("measurements") == []:
            if _normal_observation(projected, terminal_observation) != target.get("id"):
                raise ValueError("Terminal observation is not the exact last native window")
        else:
            native_observation({**projected, "stage": "measuring_result"})
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Published native result differs from its last traffic window") from exc
    commissioned = [a for a in audit if a.get("kind") == "adaptive_qer_commissioning"]
    outcomes = [a for a in audit if a.get("kind") == "adaptive_ai_outcomes"]
    restored = [a for a in cleanup.get("actions", []) if a.get("kind") == "adaptive_qer_cleanup"]
    if (len(commissioned) != 1 or commissioned[0].get("accepted") is not True
            or len(outcomes) != 1 or outcomes[0].get("accepted") is not True
            or outcomes[0].get("cleanupVerified") is not (False if persistent_stopped else True)
            or len(restored) != 1 or restored[0].get("cleanupVerified") is not True
            or restored[0].get("reconciliationRequired") is not False):
        raise ValueError("Adaptive commissioning, outcomes, or restoration is unverified")
    decisions = outcomes[0].get("outcomes")
    if validation_track == "normal":
        return _verify_normal(result, actions, reads, decisions, restored[0], fixture_receipt, config_sha256)
    if (not isinstance(decisions, list) or not any(
            d.get("outcome") == "effect_verified" and isinstance(d.get("responseId"), str)
            and bool(d["responseId"]) for d in decisions)):
        raise ValueError("No model-linked verified effect")
    if (not isinstance(actions, list) or not actions or
            any(a.get("roundId") != rid or a.get("state") != "restored"
                or not a.get("attemptedSupis") or not a.get("treatmentSupis")
                or not a.get("holdoutSupis") or a.get("uncertain")
                or a.get("fence", {}).get("roundId") != rid
                or a.get("fence", {}).get("generation") != generation
                or _cohort(a.get("snapshot", {}).get("perUe", [])) != _cohort(owners)
                for a in actions)):
        raise ValueError("SMF QER action and restoration journal is incomplete")
    if sorted(a["id"] for a in actions) != restored[0].get("policyActionIds"):
        raise ValueError("QER cleanup action does not identify the journal writes")
    fingerprint = _cohort(owners)
    owner_by_supi = {row["supi"]: row for row in owners}
    valid = []
    for read in reads:
        proof = read.get("proof", {})
        sessions = proof.get("sessions")
        if (read.get("roundId") != rid or read.get("generation") != generation
                or read.get("cohortSha256") != fingerprint
                or proof.get("source") != "native-upf-qer"
                or not isinstance(sessions, list) or len(sessions) != expected_ues
                or {s.get("supi") for s in sessions if isinstance(s, dict)}
                   != {row["supi"] for row in owners}):
            raise ValueError("Independent native QER receipt differs from the exact cohort")
        identity_fields = ("sessionId", "smContextRef", "pfcpSeid", "upfEpoch",
                           "slice", "upf", "sst", "sd")
        if any(any(session.get(key) != owner_by_supi[session["supi"]].get(key)
                       for key in identity_fields) for session in sessions):
            raise ValueError("Native QER/PDR session identity differs from its owned PDU")
        valid.append(read)
    if len(valid) < 3:
        raise ValueError("Native before, installed, and restored QER reads are missing")
    for action in actions:
        treatment = set(action["treatmentSupis"])
        holdout = set(action["holdoutSupis"])
        baseline = action.get("baseline", {})
        matching_decisions = [d for d in decisions if d.get("candidateId") == action.get("candidateId")
                              and d.get("responseId") == action.get("modelResponseId")
                              and d.get("outcome") == "effect_verified"]
        if (not isinstance(action.get("candidateId"), str) or not action["candidateId"]
                or len(matching_decisions) != 1):
            raise ValueError("QER action is not linked to an exact model decision")
        event_cohort = {supi for supi, owner in owner_by_supi.items()
                        if owner.get("scenario") == action.get("eventId")}
        if (treatment & holdout or treatment | holdout != event_cohort
                or set(baseline) != set(owner_by_supi)):
            raise ValueError("SMF action cohort differs from its baseline")
        attempts = action.get("attempts")
        if (not isinstance(attempts, list) or
                any(not any(p.get("supi") == supi and p.get("phase") == phase
                            and p.get("httpStatus") == 200 and p.get("pfcpAccepted") is True
                            for p in attempts) for supi in treatment for phase in ("apply", "restore"))):
            raise ValueError("SMF treatment write or restoration was not accepted")
        targets = {}
        for supi, prior in baseline.items():
            bounded = [q for q in prior.get("qers", [])
                       if q.get("mbrKbps") != {"ul": 0, "dl": 0}]
            if (len(bounded) != 1 or set(bounded[0].get("mbrKbps", {})) != {"ul", "dl"}
                    or any(type(v) is not int or not 1 <= v <= 500_000
                           for v in bounded[0]["mbrKbps"].values())):
                raise ValueError("Native AMBR QER target is ambiguous")
            targets[supi] = bounded[0]["id"]
        if not any(q.get("mbrKbps") != action.get("profileRates")
                   for supi in treatment for q in baseline[supi].get("qers", [])
                   if q["id"] == targets[supi]):
            raise ValueError("SMF QER profile does not change the treatment")
        def matches(read, installed):
            sessions = {row["supi"]: row for row in read["proof"]["sessions"]}
            for supi in baseline:
                prior = baseline[supi]
                if not isinstance(prior, dict) or sessions[supi].get("qers") is None:
                    return False
                old = sorted(prior["qers"], key=lambda q: q["id"])
                new = sorted(sessions[supi]["qers"], key=lambda q: q["id"])
                if [(q["id"], q["pdrIds"]) for q in old] != [(q["id"], q["pdrIds"]) for q in new]:
                    return False
                expected = action["profileRates"] if installed and supi in treatment else None
                if any(q["mbrKbps"] != (expected if expected and q["id"] == targets[supi]
                                        else old[i]["mbrKbps"])
                       for i, q in enumerate(new)):
                    return False
            return True
        action_reads = [r for r in valid if r.get("actionId") == action["id"]]
        apply_times = [p.get("observedAt") for p in attempts if p.get("phase") == "apply"]
        restore_times = [p.get("observedAt") for p in attempts if p.get("phase") == "restore"]
        if (not apply_times or not restore_times
                or any(type(t) not in (int, float) or not math.isfinite(t)
                       for t in apply_times + restore_times)
                or any(type(r.get("journalId")) is not int or type(r.get("capturedAt")) not in (int, float)
                       or not math.isfinite(r["capturedAt"]) for r in action_reads)):
            raise ValueError("QER action readback has no ordered attempt fence")
        first_apply, last_apply = min(apply_times), max(apply_times)
        first_restore, last_restore = min(restore_times), max(restore_times)
        if first_restore < last_apply:
            raise ValueError("QER restoration started before installation completed")
        try:
            for read in action_reads:
                sampled_at = seconds(read["proof"]["observedAt"])
                if not -1 <= read["capturedAt"] - sampled_at <= 15:
                    raise ValueError("Native UPF sample time differs from journal capture")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Native UPF sample time is invalid") from exc
        old_before = [r for r in action_reads if r.get("phase") == "baseline"
                      and r["capturedAt"] <= first_apply and matches(r, False)]
        changed = [r for r in action_reads if r.get("phase") == "installed"
                   and last_apply <= r["capturedAt"] <= first_restore
                   and seconds(r["proof"]["observedAt"]) >= last_apply - 1
                   and matches(r, True)]
        old_after = [r for r in action_reads if r.get("phase") == "restored"
                     and last_restore <= r["capturedAt"]
                     and seconds(r["proof"]["observedAt"]) >= last_restore - 1
                     and matches(r, False)]
        if not any(b["journalId"] < i["journalId"] < r["journalId"]
                   for b in old_before for i in changed for r in old_after):
            raise ValueError("Native QER baseline, installation, or rollback readback is absent")
        def effect(windows):
            first = windows["measuring_baseline"][2]
            last = windows["measuring_result"][2]
            def change(group):
                before = sum(first[(supi, "ul")][1]["receivedMbps"] for supi in group) / len(group)
                after = sum(last[(supi, "ul")][1]["receivedMbps"] for supi in group) / len(group)
                if before <= 0:
                    raise ValueError("Treatment baseline is nonpositive")
                return (after - before) / before * 100
            treatment_change = change(treatment)
            control_change = change(holdout)
            return treatment_change - control_change, control_change
        # The rate summaries cover each flow's interval. The common 8-second
        # intersection alone cannot prove that those summaries were measured
        # while this exact action was installed.
        action_windows = [windows for windows in qualified_windows
            if all(report["effectiveFinishedAt"] <= first_apply
                   for _, report in windows["measuring_baseline"][2].values())
            and all(last_apply <= report["effectiveStartedAt"] <= report["effectiveFinishedAt"] <= first_restore
                    for _, report in windows["measuring_result"][2].values())]
        decision = matching_decisions[0]
        if not any(control >= -5 and net >= 5
                and type(decision.get("improvementPercent")) in (int, float)
                and math.isfinite(decision["improvementPercent"])
                and abs(decision["improvementPercent"] - net) <= .2
                for net, control in map(effect, action_windows)):
            raise ValueError("Native treatment/holdout effect does not match the model-linked outcome")
    constrained_proof = (_verify_constrained_restoration(result, fixture_receipt, constrained_claim, actions, qualified_windows)
                         if constrained_claim else {})
    return {"roundId": rid, "generation": generation, "ueCount": expected_ues, "modelResponses":
            [d["responseId"] for d in decisions], "qerActions": len(actions),
            "nativeQerReads": len(valid), "released": True, **constrained_proof}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--ai100-config", type=Path, required=True)
    parser.add_argument("--ai100-config-sha256", required=True)
    parser.add_argument("--require-langgraph", action="store_true")
    parser.add_argument("--validation-track", choices=("effect", "normal", "constrained"), default="effect")
    parser.add_argument("--ues", type=int, choices=range(1, 101), default=100)
    args = parser.parse_args(argv)
    _validation_track(args.validation_track)
    if args.validation_track in {"normal", "constrained"} and not args.require_langgraph:
        parser.error("Explicit validation tracks require the three-role LangGraph proof")
    folder = args.receipts
    load = lambda name: json.loads((folder / name).read_bytes())
    result = load("result.json")
    journal = configured_journal(args.ai100_config, args.ai100_config_sha256)
    actions, reads = read_qer_journal(journal, result["roundId"])
    fleet_path = folder / "fleet-release.json"
    receipt = None
    if args.validation_track == "normal":
        from backend.onprem.validation.normal import read_normal_receipt
        record = read_normal_receipt(journal, result["requestId"])
        if not isinstance(record, dict) or record.get("state") != "released":
            raise ValueError("Normal private manifest has not been independently released")
        receipt = record["claim"]
    elif args.validation_track == "constrained":
        from backend.onprem.validation.constrained import read_constrained_receipt
        receipt = read_constrained_receipt(journal, result["requestId"])
    proof = verify(result, load("rendered-result.json"), load("cleanup.json"),
                            load("intent.json"), actions, reads, load("rendered-calibration.json"),
                             load("fleet-release.json") if fleet_path.exists() else None, expected_ues=args.ues,
                            **({"validation_track": args.validation_track, "fixture_receipt": receipt,
                                "config_sha256": args.ai100_config_sha256} if args.validation_track != "effect" else {}))
    if args.require_langgraph:
        proof.update(verify_graph_evidence(result, actions, read_graph_evidence(journal, result["roundId"], result["generation"],
            include_artifacts=args.validation_track != "effect"),
            **({"validation_track": args.validation_track, "native_reads": reads} if args.validation_track != "effect" else {})))
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
