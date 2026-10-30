"""Trusted, frozen scenario objectives; offered load and QER limits are not SLA."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path

from backend.onprem.scenarios.contracts import SCENARIO_SLICES

SCHEMA = "cityverse.qos-objectives/v1"
ENVIRONMENT_KEY = "CITYVERSE_ONPREM_QOS_OBJECTIVES_FILE"
METRICS = {"receivedUplinkMbps": ("minUplinkMbps", "min"),
           "receivedDownlinkMbps": ("minDownlinkMbps", "min"),
           "rttMs": ("maxRttMs", "max"), "lossPercent": ("maxLossPercent", "max")}
STATISTICS = {"scope": "scenario-total", "throughputStatistic": "sum",
              "rttStatistic": "mean", "lossStatistic": "packet-ratio"}


def validate_objectives(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "revision", "source", "objectives"}
            or value["schemaVersion"] != SCHEMA or not isinstance(value["objectives"], list)
            or len(value["objectives"]) > len(SCENARIO_SLICES)
            or any(not isinstance(value.get(k), str) or not 1 <= len(value[k]) <= 128 for k in ("revision", "source"))):
        raise ValueError("Invalid trusted QoS objectives")
    seen = set()
    allowed = {"scenario", "slice", *STATISTICS, *(v[0] for v in METRICS.values())}
    for row in value["objectives"]:
        if (not isinstance(row, dict) or set(row) != allowed or row.get("scenario") not in SCENARIO_SLICES
                or row["scenario"] in seen or row.get("slice") != SCENARIO_SLICES[row["scenario"]]
                or any(row.get(k) != expected for k, expected in STATISTICS.items())):
            raise ValueError("Invalid QoS objective scope or statistic")
        seen.add(row["scenario"])
        for key, _direction in METRICS.values():
            number = row[key]
            if number is not None and (type(number) not in (int, float) or not math.isfinite(number)
                    or not 0 <= number <= (100 if key == "maxLossPercent" else 1_000_000)):
                raise ValueError("Invalid bounded QoS objective")
    return deepcopy(value)


def configured_objectives(environ=None):
    filename = (os.environ if environ is None else environ).get(ENVIRONMENT_KEY)
    if filename is None:
        return None
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Trusted QoS objectives require an existing absolute file")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Trusted QoS objectives exceed size bound")
    return validate_objectives(json.loads(raw))


def freeze_objectives(value, allocation):
    value = validate_objectives(value)
    if value is None:
        return None
    counts = {"web_browsing": allocation["baselineUes"], **allocation["events"]}
    value["objectives"] = [{**row, "cohortSize": counts[row["scenario"]]}
                           for row in value["objectives"] if counts[row["scenario"]]]  # Immutable round cohort.
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    value["contractSha256"] = hashlib.sha256(encoded).hexdigest()
    return value


def sample_objectives(snapshot, sample):
    """Only the round's frozen contract can annotate this historical sample."""
    if "serviceRequirements" in snapshot:
        return None, {"status": "unknown", "reason": "use_service_sla_assessment",
                      "metrics": {key: "unknown" for key in METRICS}}
    frozen = snapshot.get("qosObjectives")
    states = {key: "unknown" for key in METRICS}
    assessment = {"status": "unknown", "reason": "objectives_not_configured", "metrics": states}
    if not isinstance(frozen, dict) or frozen.get("schemaVersion") != SCHEMA:
        return None, assessment
    rows = [r for r in frozen.get("objectives", []) if isinstance(r, dict) and r.get("scenario") == sample.get("scenario")]
    if len(rows) != 1:
        assessment["reason"] = "objective_scope_missing"
        return None, assessment
    row = rows[0]
    if (row.get("slice") != sample.get("slice") or any(row.get(k) != v for k, v in STATISTICS.items())
            or any(not isinstance(frozen.get(k), str) or not 1 <= len(frozen[k]) <= 128 for k in ("revision", "source"))):
        assessment["reason"] = "objective_scope_or_statistic_mismatch"
        return None, assessment
    targets = {"revision": frozen.get("revision"), "source": frozen.get("source"),
               **{k: row.get(k) for k in ("scenario", "slice", *STATISTICS, *(v[0] for v in METRICS.values()))}}
    if row.get("cohortSize") != sample.get("sampleCount"):
        assessment["reason"] = "objective_scope_or_statistic_mismatch"
        return targets, assessment
    if sample.get("quality") != "observed":
        assessment["reason"] = "measurement_incomplete"
        return targets, assessment
    reasons, configured = [], []
    for metric, (target_key, direction) in METRICS.items():
        target, measured = row.get(target_key), sample.get(metric)
        if target is None:
            continue
        configured.append(metric)
        if metric == "rttMs" and (sample.get("rtt") or {}).get("sameTrafficWindow") is not True:
            reasons.append("rtt_window_mismatch")
            continue
        if type(target) not in (int, float) or not math.isfinite(target) or type(measured) not in (int, float) or not math.isfinite(measured):
            reasons.append("metric_missing")
            continue
        states[metric] = "met" if (measured >= target if direction == "min" else measured <= target) else "not_met"
    assessment["status"] = ("not_met" if any(states[k] == "not_met" for k in configured) else
                            "met" if configured and all(states[k] == "met" for k in configured) else "unknown")
    assessment["reason"] = reasons[0] if reasons else "criteria_evaluated" if configured else "objective_metrics_not_configured"
    return targets, assessment


def round_sla_assessment(snapshot):
    """Assess the latest accepted result window, independently of policy effect.

    This never turns a sparse window into a time-compliance guarantee, renews
    evidence timestamps, or fills missing scenarios from an older window.
    """
    if "serviceRequirements" in snapshot:
        from backend.onprem.scenarios.sla import SlaEvaluator
        return SlaEvaluator(snapshot).evaluate()
    result = {"status": "unknown", "reason": "objectives_not_configured",
              "scope": "last-result-window", "historical": True,
              "windowId": None, "scenarios": []}
    if not isinstance(snapshot.get("qosObjectives"), dict):
        return result
    windows = [a for a in snapshot.get("actions", []) if isinstance(a, dict)
               and a.get("accepted") is True and a.get("stage") == "measuring_result"
               and (a.get("kind") == "traffic_aggregate" or
                    a.get("kind") == "traffic_window" and a.get("shardCount", 1) == 1)]
    if not windows:
        return {**result, "reason": "result_window_unavailable"}
    window_id = windows[-1].get("id")
    result["windowId"] = window_id
    from backend.onprem.scenarios.observability import measurement_samples
    samples = {row["scenario"]: row for row in measurement_samples(snapshot)
               if row["id"] == f"{window_id}:{row['scenario']}"}
    allocation = snapshot.get("allocation", {})
    counts = {"web_browsing": allocation.get("baselineUes", 0), **allocation.get("events", {})}
    for scenario in SCENARIO_SLICES:
        if not counts.get(scenario, 0):
            continue
        sample = samples.get(scenario)
        assessment = sample.get("assessment", {}) if sample else {}
        result["scenarios"].append({"scenario": scenario, "sampleId": sample["id"] if sample else None,
            "status": assessment.get("status", "unknown"),
            "reason": assessment.get("reason", "measurement_incomplete")})
    states = [row["status"] for row in result["scenarios"]]
    result["status"] = ("not_met" if "not_met" in states else
                        "met" if states and all(s == "met" for s in states) else "unknown")
    result["reason"] = "criteria_evaluated" if result["status"] != "unknown" else "objectives_or_measurements_incomplete"
    return result
