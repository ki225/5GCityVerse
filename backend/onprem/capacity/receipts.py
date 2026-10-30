"""Inspect versioned calibration bundles for capacity.promotion.

The caller supplies immutable receipt bytes; inspection performs no network,
ledger or capability writes. SHA256 establishes integrity, while the startup
caller verifies runtime identity and durable local ledger origin separately.
This inspector does not itself grant capacity. Historical v1 connectivity and
v2 per-flow capacity evidence retain their recorded capture times.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import re

from backend.onprem.traffic.protocol import build_jobs, validate_socket_pair
from backend.onprem.traffic.capability import receiver_summary, sender_summary
from backend.onprem.runtime.round_resources import validate_window
from backend.onprem.traffic.planning import PROFILE_ID, plan_workload
from backend.onprem.capacity.performance import audit_capacity, criteria_metadata, sharded_capacity_windows
from backend.onprem.evidence.scenario_receipts import result_errors, released


_STAGES = (0, 1, 5, 10, 25, 50, 100, 250, 500)
_RELEASE = ("trafficStopped", "policiesAbsent", "rulesRestored", "uesReleased", "podsAbsent")


@dataclass(frozen=True)
class CalibrationInspection:
    manifest_sha256: str
    integrity_verified: bool
    coverage_complete: bool
    candidate_ceiling: int
    declared_prior_ceiling: int
    covered: tuple[str, ...]
    errors: tuple[str, ...]
    authenticity: str = "not-established-by-file-inspection"
    promotion_eligible: bool = False
    capacity_criteria_checked: bool = False
    protocol_predeclaration_verified: bool = False


def _json(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 16 * 1024 * 1024:
        raise ValueError("Receipt must be bounded immutable bytes")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value
    def nonfinite(_):
        raise ValueError("Nonfinite JSON value")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite JSON value")
        return number
    try:
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite, parse_float=finite_float)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Malformed receipt") from exc
    if not isinstance(result, dict):
        raise ValueError("Receipt must be a JSON object")
    return result


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing recorded capture timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Capture timestamp requires timezone")
    return parsed.timestamp()


def _native_and_windows(result, entry, candidate):
    case = {**result["allocation"], "strategy": "none"}
    captured = _stamp(entry["resultCapturedAt"])
    if case["total"] != candidate or result.get("strategy") != "none":
        raise ValueError("Candidate population or strategy mismatch")
    if result_errors(result, case, captured):
        raise ValueError("Existing native/result contract failed")
    identity_fields = ("supi", "scenario", "slice", "ueIp", "tun", "podUid", "sessionId",
                       "smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upf", "upfEpoch")
    if sorted(tuple(row[k] for k in identity_fields) for row in result["perUe"]) != sorted(
            tuple(row[k] for k in identity_fields) for row in result["connectivityEvidence"]["identities"]):
        raise ValueError("Full native identity differs from the 60-second stability proof")
    plan = plan_workload(result["perUe"])
    if entry.get("workloadFingerprint") != plan.comparison_fingerprint:
        raise ValueError("Workload comparison profile differs")
    if candidate == 100:
        stages = sharded_capacity_windows(result)
        expected_rates = {row.supi: row.applied_kbps for row in plan.per_ue}
        for windows, _, _ in stages.values():
            observed_rates = {supi: rate for window in windows
                              for supi, rate in window["offeredBySupi"].items()}
            if observed_rates != expected_rates:
                raise ValueError("Sharded workload differs from frozen planner")
        _, aggregate, reports = stages["measuring_result"]
        if (not 0 <= captured - aggregate["effectiveFinishedAt"] < 35
                or not 0 <= captured - aggregate["finishedAt"] < 15):
            raise ValueError("Result shard aggregate is stale or future-dated")
        public = {row["supi"]: row for row in result["measurements"]}
        if len(public) != 100:
            raise ValueError("Incomplete public result measurements")
        for (supi, direction), (_, report) in reports.items():
            metric = "receivedUplinkMbps" if direction == "ul" else "receivedDownlinkMbps"
            if (public[supi][metric] != report["receivedMbps"]
                    or not aggregate["effectiveStartedAt"] <= _stamp(public[supi]["observedAt"]) <= captured + 1):
                raise ValueError("Public result differs from native shard receiver")
        if not aggregate["effectiveStartedAt"] <= _stamp(result["observedAt"]) <= captured + 1:
            raise ValueError("Snapshot timestamp differs from result shard interval")
        slices = {row["slice"] for row in result["perUe"]}
        if len(slices) == 1:
            return "concentrated:" + next(iter(slices))
        required_groups = {"web_browsing", "video_streaming", "realtime_control", "connected_traffic", "factory_iot"}
        if slices == {"embb", "urllc", "mmtc", "v2x"} and {row["scenario"] for row in result["perUe"]} == required_groups:
            return "mixed"
        raise ValueError("Trial does not cover a declared calibration distribution")
    windows = [a for a in result["actions"] if a.get("kind") == "traffic_window"
               and a.get("stage") == "measuring_result"]
    if len(windows) != 1:
        raise ValueError("One exact final traffic receipt required")
    window = windows[0]
    if (window.get("mode") != "batch" or any(window.get(k) is not True for k in
            ("accepted", "installed", "effectVerified", "cleanupVerified"))):
        raise ValueError("Batch result or worker cleanup unverified")
    rates = {row.supi: row.applied_kbps for row in plan.per_ue}
    if window.get("offeredBySupi") != rates or window.get("aggregateKbps") != plan.applied_ul_kbps:
        raise ValueError("Applied workload does not match frozen planner")
    jobs = build_jobs(window["bindings"], aggregate_kbps=plan.applied_ul_kbps, offered_by_supi=rates)
    if window.get("jobs") != [asdict(job) for job in jobs]:
        raise ValueError("Persisted jobs differ from immutable native bindings")
    owners = {row["supi"]: row for row in result["perUe"]}
    reports = window["reports"]
    if not isinstance(reports, list) or len(reports) != len(jobs):
        raise ValueError("Incomplete bidirectional receiver windows")
    by_key = {(r["binding"]["supi"], r["direction"]): r for r in reports}
    if len(by_key) != len(jobs):
        raise ValueError("Duplicate receiver window")
    starts, finishes = [], []
    for job in jobs:
        owner = owners[job.supi]
        report = by_key[(job.supi, job.direction)]
        binding = {**owner, "ip": owner["ueIp"], "generation": result["generation"]}
        if (job.ue_ip != owner["ueIp"] or job.ue_uid != owner["podUid"] or job.tun != owner["tun"]
                or job.generation != result["generation"] or job.session_id != owner["sessionId"]
                or job.pfcp_seid != owner["pfcpSeid"] or job.sm_context_ref != owner["smContextRef"]
                or job.upf_epoch != owner["upfEpoch"] or job.upf_name != owner["upf"]
                or (job.dnn, job.sst, job.sd) != (owner["dnn"], owner["sst"], owner["sd"])):
            raise ValueError("Native PDU identity changed")
        validate_window({"binding": binding, "direction": job.direction, "receiver_uid": job.receiver_uid,
                         "endpoint": job.endpoint, "port": job.port, "duration": 5,
                         "offered_mbps": job.offered_kbps / 1000}, report, now=captured)
        validate_socket_pair(job, report["client"], report["server"])
        receiver = receiver_summary(report["server"] if job.direction == "ul" else report["client"])
        sender = sender_summary(report["client"] if job.direction == "ul" else report["server"])
        if (not 4 <= receiver["seconds"] <= 7 or not 4 <= sender["seconds"] <= 7
                or not .8 * job.offered_kbps * 1000 <= sender["bits_per_second"] <= 1.2 * job.offered_kbps * 1000
                or report["receivedMbps"] != receiver["bits_per_second"] / 1e6):
            raise ValueError("Actual receiver workload differs")
        for key in ("finishedAt", "receiverStartedAt", "receiverFinishedAt"):
            if type(report.get(key)) not in (int, float) or not math.isfinite(report[key]):
                raise ValueError("Missing actual receiver worker clock")
        if (not 0 <= captured - report["finishedAt"] < 15
                or not 0 <= captured - report["receiverFinishedAt"] < 15
                or not 4 <= report["finishedAt"] - report["startedAt"] <= 8
                or not 4 <= report["receiverFinishedAt"] - report["receiverStartedAt"] <= 32
                or report["receiverStartedAt"] > report["startedAt"] + 1
                or not report["finishedAt"] - 1 <= report["receiverFinishedAt"] <= report["finishedAt"] + 3):
            raise ValueError("Actual receiver clock is future/stale/misaligned")
        public = next(m for m in result["measurements"] if m["supi"] == job.supi)
        metric = "receivedUplinkMbps" if job.direction == "ul" else "receivedDownlinkMbps"
        if public[metric] != report["receivedMbps"]:
            raise ValueError("Public measurement differs from raw receiver")
        if _stamp(public["observedAt"]) > min(report["finishedAt"], report["receiverFinishedAt"]) + .000001:
            raise ValueError("Public measurement timestamp freshens receiver evidence")
        starts.append(report["startedAt"])
        finishes.append(min(report["finishedAt"], report["receiverFinishedAt"]))
    if min(finishes) - max(starts) < 4:
        raise ValueError("All bidirectional windows must overlap")
    if _stamp(result["observedAt"]) > min(finishes) + .000001:
        raise ValueError("Snapshot timestamp freshens oldest receiver evidence")
    slices = {row["slice"] for row in result["perUe"]}
    if len(slices) == 1:
        return "concentrated:" + next(iter(slices))
    required_groups = {"web_browsing", "video_streaming", "realtime_control", "connected_traffic", "factory_iot"}
    if slices == {"embb", "urllc", "mmtc", "v2x"} and (candidate < 6 or
            {row["scenario"] for row in result["perUe"]} == required_groups):
        return "mixed"
    raise ValueError("Trial does not cover a declared calibration distribution")


def _ui_and_cleanup(result, rendered, cleanup, entry):
    rows = rendered.get("evidence")
    if rendered.get("alerts") != [] or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Missing unique clean rendered evidence panel")
    row = rows[0]
    if (any(row.get(k) != str(result[k]) for k in ("roundId", "generation", "policyRevision"))
            or int(row["sequence"]) < result["sequence"] or row.get("fresh") != "true"
            or row.get("stage") != "results_ready"
            or not 0 <= _stamp(entry["uiCapturedAt"]) - _stamp(row["observedAt"]) < 15):
        raise ValueError("Rendered native result differs or is stale")
    proof = cleanup.get("releaseProof", {})
    if (not released(cleanup) or cleanup.get("executionOutcome") != "passed"
            or cleanup.get("roundId") != result["roundId"] or cleanup.get("generation") != result["generation"]
            or type(cleanup.get("sequence")) is not int or cleanup["sequence"] <= result["sequence"]
            or any(proof.get(k) is not True for k in _RELEASE)
            or proof.get("scope") != "known-round-and-managed-runtime"
            or not 0 <= _stamp(entry["cleanupCapturedAt"]) - _stamp(proof.get("observedAt")) < 15
            or _stamp(proof["observedAt"]) < _stamp(entry["resultCapturedAt"])):
        raise ValueError("Exact round owned-scope cleanup is not proven")


def inspect_manifest(manifest_bytes, read_receipt):
    """Inspect declared receipts; never return authority to promote a ceiling.

    Manifest: version/profileId/candidateCeiling/priorCeiling/runtime/receipts;
    version 2 also requires exact capacityCriteria from criteria_metadata().
    Runtime declares mode/context/namespace and digest-pinned ue/init/traffic/upf
    images. Each trial references result/rendered/cleanup as {ref, sha256}, its
    three capture times, and workloadFingerprint. Paths are relative identifiers
    resolved solely by the supplied reader. Every hash is checked before content.
    Candidate 1 needs four concentrated runs; >=5 additionally needs mixed four
    slices (all six scenario groups for >=10). No previous ceiling is certified.
    """
    manifest = _json(manifest_bytes)
    candidate, prior = manifest.get("candidateCeiling"), manifest.get("priorCeiling")
    if (manifest.get("version") not in (1, 2) or type(manifest.get("version")) is not int
            or manifest.get("profileId") != PROFILE_ID or type(candidate) is not int
            or candidate not in _STAGES[1:] or type(prior) is not int
            or prior != _STAGES[_STAGES.index(candidate) - 1] or not callable(read_receipt)):
        raise ValueError("Invalid calibration version/profile/stage chain")
    capacity_required = manifest["version"] == 2
    if capacity_required and json.dumps(manifest.get("capacityCriteria"), sort_keys=True) != json.dumps(criteria_metadata(), sort_keys=True):
        raise ValueError("Exact versioned capacity criteria required")
    runtime = manifest.get("runtime", {})
    if (not isinstance(runtime, dict) or runtime.get("mode") != "kubeadm"
            or any(not isinstance(runtime.get(k), str) or not runtime[k] for k in ("context", "namespace"))
            or not isinstance(runtime.get("images"), dict) or set(runtime["images"]) != {"ue", "init", "traffic", "upf"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", v)
                   for v in runtime["images"].values())):
        raise ValueError("Exact declared runtime and image pins required")
    trials = manifest.get("receipts")
    if not isinstance(trials, list) or not 1 <= len(trials) <= 16:
        raise ValueError("One to sixteen bounded calibration trials required")
    total_bytes, covered, errors, rounds, references = 0, set(), [], set(), set()
    for index, entry in enumerate(trials):
        if not isinstance(entry, dict):
            raise ValueError("Trial must be a receipt descriptor object")
        contents = {}
        for name in ("result", "rendered", "cleanup"):
            descriptor = entry.get(name)
            if not isinstance(descriptor, dict):
                raise ValueError("Missing receipt descriptor")
            ref, digest = descriptor.get("ref"), descriptor.get("sha256")
            if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", ref)
                    or ref.startswith("/") or any(part in ("", ".", "..") for part in ref.split("/"))
                    or ref in references or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("Unsafe, duplicate or malformed receipt reference")
            references.add(ref)
            raw = read_receipt(ref)
            if not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024:
                raise ValueError("Receipt byte limit exceeded")
            total_bytes += len(raw)
            if total_bytes > 128 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("Receipt integrity or bundle byte limit failed")
            contents[name] = _json(raw)
        try:
            result = contents["result"]
            if result["roundId"] in rounds:
                raise ValueError("Duplicate round receipt")
            rounds.add(result["roundId"])
            coverage = _native_and_windows(result, entry, candidate)
            _ui_and_cleanup(result, contents["rendered"], contents["cleanup"], entry)
            if capacity_required and not audit_capacity(result).passed:
                raise ValueError("Per-flow baseline/result capacity criteria rejected")
            covered.add(coverage)
        except Exception:
            # Public diagnostics must not echo arbitrary artifact content.
            errors.append(f"trial[{index}]: native/window/UI/release/capacity contract rejected")
    required = {"concentrated:" + name for name in ("embb", "urllc", "mmtc", "v2x")}
    if candidate > 1:
        required.add("mixed")
    return CalibrationInspection(hashlib.sha256(manifest_bytes).hexdigest(), True,
        not errors and required <= covered, candidate, prior, tuple(sorted(covered)), tuple(errors),
        capacity_criteria_checked=capacity_required)
