"""Deterministic per-UE/service/slice SLA evaluation with missing-data bounds."""
import hashlib
import math

from backend.onprem.evidence.sla import NativeSlaObservationAdapter, number
from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler, METRICS


def combine(states):
    states = list(states)
    return "not_met" if "not_met" in states else "met" if states and all(s == "met" for s in states) else "unknown"


def bounds(passed, failed, expected, threshold):
    if expected <= 0:
        return {"status": "unknown", "lowerBound": 0., "upperBound": 1., "coverageRatio": 0.}
    lower, upper = passed / expected, (expected - failed) / expected
    state = "met" if lower + 1e-9 >= threshold else "not_met" if upper + 1e-9 < threshold else "unknown"
    return {"status": state, "lowerBound": lower, "upperBound": upper,
            "coverageRatio": min(1., (passed + failed) / expected)}


class SlaEvaluator:
    """Pure evaluator: LLM/model output cannot supply a verdict or renew evidence."""
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.contract = ServiceRequirementCompiler.from_snapshot(snapshot)
        self.observations = NativeSlaObservationAdapter(snapshot)

    def _metric(self, supis, key, start, end, step, target):
        if key == "rttMs":
            # Adjacent probes never prove this traffic interval's latency.
            return None, "same_window_rtt_unavailable"
        directions = ("ul",) if key == "receivedUplinkMbps" else ("dl",) if key == "receivedDownlinkMbps" else ("ul", "dl")
        flows = [self.observations.flow(s, d, start, end, step) for s in supis for d in directions]
        if not flows or any(f is None for f in flows):
            return None, "interval_evidence_missing"
        if key.startswith("received"):
            offered = [f["offeredObservedMbps"] for f in flows]
            if any(v is None for v in offered) or sum(offered) + 1e-9 < target:
                return None, "offered_load_insufficient"
            return sum(f["receivedMbps"] for f in flows), "criteria_evaluated"
        if any(f["packets"] is None or f["lostPackets"] is None for f in flows):
            return None, "packet_denominator_missing"
        packets = sum(f["packets"] for f in flows)
        if packets <= 0:
            return None, "packet_denominator_missing"
        return sum(f["lostPackets"] for f in flows) / packets * 100, "criteria_evaluated"

    def _time(self, supis, key, target, evaluation, start, end):
        step = evaluation["sampleIntervalSeconds"]
        passed = failed = observed = value_sum = 0.
        reasons = set()
        for index in self.observations.indices(supis, start, end, step):
            a = start + index * step
            b = min(end, a + step)
            if b <= a:
                continue
            value, reason = self._metric(supis, key, a, b, step, target)
            if value is None:
                reasons.add(reason)
                continue
            duration = b - a
            observed += duration
            value_sum += value * duration
            met = value >= target if key.startswith("received") else value <= target
            if met:
                passed += duration
            else:
                failed += duration
        duration = end - start
        result = bounds(passed, failed, duration, evaluation["minTimeCompliance"])
        result.update(target=target, observedMean=value_sum / observed if observed else None,
                      passedSeconds=passed, failedSeconds=failed, unknownSeconds=max(0., duration - observed),
                      expectedSeconds=duration, requiredTimeCompliance=evaluation["minTimeCompliance"])
        result["reason"] = ("criteria_evaluated" if result["status"] != "unknown" else
                            "same_window_rtt_unavailable" if key == "rttMs" else
                            sorted(reasons)[0] if reasons else "interval_evidence_missing")
        return result

    @staticmethod
    def _cohort(states, expected, threshold):
        return bounds(states.count("met"), states.count("not_met"), expected, threshold)

    def _item(self, scope, sla, evaluation, supis, expected, start, end, *, details):
        metrics = {key: target for key, name in METRICS.items() if (target := sla.get(name)) is not None}
        result = {"scope": scope, "status": "unknown", "reason": "cohort_evidence_incomplete",
                  "expectedUes": expected, "observedUes": len(supis), "sla": sla, "evaluation": evaluation,
                  "metrics": {}, "missingUes": max(0, expected - len(supis))}
        if len(supis) > expected or len(self.observations.owners) != len(self.snapshot.get("perUe", [])):
            return {**result, "reason": "cohort_scope_mismatch"}
        if scope == "per-ue":
            rows = []
            for supi in supis:
                values = {key: self._time([supi], key, target, evaluation, start, end) for key, target in metrics.items()}
                rows.append({"ueRef": hashlib.sha256((self.snapshot["roundId"] + ':' + supi).encode()).hexdigest()[:12],
                             "status": combine(v["status"] for v in values.values()), "metrics": values})
            ratio = evaluation["minUeCompliance"]
            result.update(self._cohort([r["status"] for r in rows], expected, ratio))
            for key, target in metrics.items():
                metric = self._cohort([r["metrics"][key]["status"] for r in rows], expected, ratio)
                means = [r['metrics'][key]['observedMean'] for r in rows if r['metrics'][key]['observedMean'] is not None]
                reasons = sorted({r['metrics'][key]['reason'] for r in rows if r['metrics'][key]['status'] == 'unknown'})
                result['metrics'][key] = {**metric, 'target': target, 'requiredUeCompliance': ratio,
                    'observedMin': min(means) if means else None, 'observedMax': max(means) if means else None,
                    'reason': 'criteria_evaluated' if metric['status'] != 'unknown' else
                        'same_window_rtt_unavailable' if key == 'rttMs' else reasons[0] if reasons else 'interval_evidence_missing'}
            result.update(passedUes=sum(r["status"] == "met" for r in rows),
                          failedUes=sum(r["status"] == "not_met" for r in rows),
                          unknownUes=expected - sum(r["status"] != "unknown" for r in rows))
            if details:
                result["perUe"] = rows
        else:
            if len(supis) == expected:
                result["metrics"] = {key: self._time(supis, key, target, evaluation, start, end) for key, target in metrics.items()}
                result["status"] = combine(v["status"] for v in result["metrics"].values())
        result["reason"] = "criteria_evaluated" if result["status"] != "unknown" else "objectives_or_measurements_incomplete"
        return result

    def _assessment(self, *, whole=False):
        source = self.observations
        result = {"status": "unknown", "reason": "measurement_window_unavailable", "historical": True,
                  "scope": "whole-service-span" if whole else "latest-complete-evaluation-window",
                  "complete": self.snapshot.get("stage") == "completed" if whole else False,
                  "services": [], "slices": [], "startedAt": None, "finishedAt": None}
        if not source.spans or source.latest_window_unavailable:
            return result
        origin = min(a for a, b in source.spans)
        finish = max(b for a, b in source.spans)
        result.update(startedAt=origin, finishedAt=finish)
        items = [(name, r["sla"], r["evaluation"], r["ueCount"], source.cohort(scenario=name), "service")
                 for name, r in self.contract["services"].items() if r["sla"] is not None]
        items += [(name, r["sla"], r["evaluation"], self.contract["slices"][name]["ueCount"], source.cohort(slice_name=name), "slice")
                  for name, r in self.contract["sliceObjectives"].items()]
        for name, sla, evaluation, expected, supis, kind in items:
            window = evaluation["windowSeconds"]
            count = math.floor((finish - origin + .002) / window)
            complete = count > 0
            start, end = (origin, finish) if whole else (origin + max(0, count - 1) * window, origin + max(1, count) * window)
            row = self._item(sla["scope"], sla, evaluation, supis, expected, start, end, details=not whole)
            row.update({"scenario" if kind == "service" else "slice": name,
                        "startedAt": start, "finishedAt": end, "complete": complete})
            if not complete:
                row.update(status="unknown", reason="evaluation_window_incomplete")
            result["services" if kind == "service" else "slices"].append(row)
        result["status"] = combine(r["status"] for r in result["services"] + result["slices"])
        if not whole and items:
            result.update(startedAt=min(r['startedAt'] for r in result['services'] + result['slices']),
                          finishedAt=max(r['finishedAt'] for r in result['services'] + result['slices']))
        result["complete"] = result["complete"] if whole else bool(items) and all(r["complete"] for r in result["services"] + result["slices"])
        result["reason"] = "criteria_evaluated" if result["status"] != "unknown" else "objectives_or_measurements_incomplete"
        if whole and not result["complete"]:
            result.update(status="unknown", reason="round_in_progress")
        return result

    def evaluate(self):
        latest = self._assessment()
        whole = self._assessment(whole=True)
        return {"schemaVersion": "cityverse.service-sla/v1", "revision": self.contract["revision"],
                "status": latest["status"], "reason": latest["reason"], "scope": latest["scope"],
                "historical": True, "latestWindow": latest, "wholeRound": whole,
                "measurementCapabilities": ServiceRequirementCompiler.capabilities()}
