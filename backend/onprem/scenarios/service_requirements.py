"""Compile user service intent into an immutable, versioned round contract.

This is demand validation and arithmetic, not a network capacity certificate.
Native adapters, ownership checks and observed evidence retain their authority.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math

from backend.onprem.scenarios.contracts import SCENARIO_SLICES, ScenarioRequestError
from backend.onprem.scenarios.profiles import DEFAULT_TRAFFIC

SCHEMA = "cityverse.service-requirements/v2"
RESOLVED_SCHEMA = "cityverse.resolved-service-requirements/v2"
METRICS = {"receivedUplinkMbps": "minUplinkMbps", "receivedDownlinkMbps": "minDownlinkMbps",
           "rttMs": "maxMeanRttMs", "lossPercent": "maxLossPercent"}
EVALUATION = {"windowSeconds", "sampleIntervalSeconds", "minTimeCompliance", "minUeCompliance"}
MAX_RATE_KBPS = 4000  # Existing native UDP compiler capability, advertised below.


def _invalid(detail):
    raise ScenarioRequestError("INVALID_SERVICE_REQUIREMENTS", detail)


def _number(value, low, high, name):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        _invalid(f"{name} must be a finite number in [{low}, {high}]")
    return value


def rate_kbps(value):
    _number(value, .001, MAX_RATE_KBPS / 1000, "Offered Mbps per UE")
    kbps = Decimal(str(value)) * 1000
    if kbps != kbps.to_integral_value():
        _invalid("Offered rates must be representable as integer kbps")
    return int(kbps)


class ServiceRequirementCompiler:
    @staticmethod
    def capabilities():
        return {"schemaVersion": SCHEMA, "protocols": ["udp"],
                "maxOfferedKbpsPerUePerDirection": MAX_RATE_KBPS,
                "scopes": ["per-ue", "service-total", "slice-total"],
                "maxEvaluationWindowSeconds": 3600, "minSampleIntervalSeconds": 1,
                "trafficSamples": "native-iperf3-receiver-intervals",
                "rtt": {"statistic": "mean", "sameTrafficWindowAvailable": False,
                        "reason": "Current native probe is adjacent to the traffic window"}}

    @staticmethod
    def _evaluation(value):
        if not isinstance(value, dict) or set(value) != EVALUATION:
            _invalid("Provide all four evaluation settings")
        window, interval = value["windowSeconds"], value["sampleIntervalSeconds"]
        if (type(window) is not int or type(interval) is not int
                or not 1 <= interval <= window <= 3600 or window % interval):
            _invalid("Evaluation window must be 1..3600 seconds and a multiple of the integer sample interval")
        for key in ("minTimeCompliance", "minUeCompliance"):
            _number(value[key], 0, 1, key)
            if value[key] == 0:
                _invalid(f"{key} must be greater than zero")

    @staticmethod
    def _sla(value, scopes):
        if (not isinstance(value, dict) or value.get("scope") not in scopes
                or set(value) - {"scope", *METRICS.values()}):
            _invalid("Invalid SLA scope or metric")
        configured = False
        for key in METRICS.values():
            target = value.get(key)
            if target is not None:
                _number(target, 0, 100 if key == "maxLossPercent" else 1_000_000, key)
                configured = True
        if not configured:
            _invalid("An SLA must configure at least one metric; omit SLA for observation only")

    @classmethod
    def compile(cls, value, allocation, *, traffic_mode="qos"):
        if traffic_mode != "qos":
            _invalid("Service requirements require qos mode with unscaled offered load")
        if (not isinstance(value, dict) or not {"schemaVersion", "services"} <= set(value)
                or set(value) - {"schemaVersion", "services", "sliceObjectives"}
                or value["schemaVersion"] != SCHEMA or not isinstance(value["services"], dict)
                or not value["services"] or set(value["services"]) - set(SCENARIO_SLICES)):
            _invalid("Invalid service requirement schema or services")
        counts = {"web_browsing": allocation["baselineUes"], **allocation["events"]}
        services, warnings = {}, []
        # Unspecified active services keep their existing scenario traffic profile.
        slices = {s: {"ueCount": 0, "offeredUplinkKbps": 0, "offeredDownlinkKbps": 0,
                      "requiredUplinkKbps": 0, "requiredDownlinkKbps": 0} for s in set(SCENARIO_SLICES.values())}
        for scenario, count in counts.items():
            if not count:
                if scenario in value["services"]:
                    _invalid("Service requirements must refer to a selected nonzero cohort")
                continue
            item = value["services"].get(scenario)
            if item is None:
                traffic = {key: value for key, value in DEFAULT_TRAFFIC[scenario].items() if key != 'slice'}
                item = {"traffic": traffic}
                warnings.append({"scenario": scenario, "code": "sla_not_requested"})
            if (not isinstance(item, dict) or "traffic" not in item
                    or set(item) - {"traffic", "sla", "evaluation"}):
                _invalid("Each service needs traffic and optional SLA/evaluation")
            traffic = item["traffic"]
            if (not isinstance(traffic, dict) or set(traffic) != {"protocol", "offeredUplinkMbpsPerUe", "offeredDownlinkMbpsPerUe"}
                    or traffic["protocol"] != "udp"):
                _invalid("Current native generator supports UDP and explicit UL/DL rates only")
            ul, dl = (rate_kbps(traffic[key]) for key in ("offeredUplinkMbpsPerUe", "offeredDownlinkMbpsPerUe"))
            if ("sla" in item) != ("evaluation" in item):
                _invalid("SLA and evaluation settings must be provided together")
            sla = item.get("sla")
            required = {"ul": 0, "dl": 0}
            if 'sla' in item:
                cls._sla(sla, {"per-ue", "service-total"})
                cls._evaluation(item["evaluation"])
                factor = count if sla["scope"] == "per-ue" else 1
                for direction, key, offered in (("ul", "minUplinkMbps", ul), ("dl", "minDownlinkMbps", dl)):
                    required[direction] = (sla.get(key) or 0) * 1000 * factor
                    if required[direction] > offered * count + 1e-9:
                        _invalid("Minimum throughput exceeds declared offered load")
                if sla.get("maxMeanRttMs") is not None:
                    warnings.append({"scenario": scenario, "code": "same_window_rtt_unavailable"})
            elif not any(w.get('scenario') == scenario and w.get('code') == 'sla_not_requested' for w in warnings):
                warnings.append({'scenario': scenario, 'code': 'sla_not_requested'})
            row = {"scenario": scenario, "slice": SCENARIO_SLICES[scenario], "ueCount": count,
                   "offeredUplinkKbpsPerUe": ul, "offeredDownlinkKbpsPerUe": dl,
                   "sla": deepcopy(sla), "evaluation": deepcopy(item.get("evaluation"))}
            services[scenario] = row
            total = slices[row["slice"]]
            for key, increment in (("ueCount", count), ("offeredUplinkKbps", ul * count),
                    ("offeredDownlinkKbps", dl * count), ("requiredUplinkKbps", required["ul"]),
                    ("requiredDownlinkKbps", required["dl"])):
                total[key] += increment
        objectives = value.get("sliceObjectives", {})
        if not isinstance(objectives, dict) or set(objectives) - set(slices):
            _invalid("Unknown slice objective")
        for name, objective in objectives.items():
            if not slices[name]["ueCount"] or not isinstance(objective, dict) or set(objective) != {"sla", "evaluation"}:
                _invalid("Slice objective requires an active slice, SLA and evaluation")
            cls._sla(objective["sla"], {"slice-total"})
            cls._evaluation(objective["evaluation"])
            for target, offered in (("minUplinkMbps", "offeredUplinkKbps"), ("minDownlinkMbps", "offeredDownlinkKbps")):
                if (objective["sla"].get(target) or 0) * 1000 > slices[name][offered] + 1e-9:
                    _invalid("Slice minimum throughput exceeds declared offered load")
            for metric, required in (('minUplinkMbps', 'requiredUplinkKbps'), ('minDownlinkMbps', 'requiredDownlinkKbps')):
                slices[name][required] = max(slices[name][required], (objective['sla'].get(metric) or 0) * 1000)
            if objective["sla"].get("maxMeanRttMs") is not None:
                warnings.append({"slice": name, "code": "same_window_rtt_unavailable"})
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        revision = hashlib.sha256(canonical.encode()).hexdigest()
        return {"schemaVersion": RESOLVED_SCHEMA, "revision": revision, "source": "user-request",
                "services": services, "slices": dict(sorted(slices.items())),
                "sliceObjectives": deepcopy(objectives), "warnings": warnings}

    @classmethod
    def from_snapshot(cls, snapshot):
        raw = snapshot.get("serviceRequirements")
        if raw is None:
            return None
        compiled = cls.compile(raw, snapshot["allocation"], traffic_mode=snapshot.get("trafficMode"))
        if snapshot.get("resolvedRequirements") != compiled:
            _invalid("Frozen service requirements no longer match the round")
        return compiled
