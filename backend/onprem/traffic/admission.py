"""Operator-defined generator ceiling, never a network capacity certificate."""
import os

from backend.onprem.scenarios.contracts import RoundUnavailable, SCENARIO_SLICES
from backend.onprem.traffic.planning import QOS_MAX_KBPS, _PROFILES

ENVIRONMENT_KEY = "CITYVERSE_ONPREM_QOS_MAX_OFFERED_KBPS"


class TrafficAdmissionBlocked(RoundUnavailable):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def admit_traffic(body, environ=None):
    if body.get("trafficMode", "functional") != "qos":
        return
    environment = os.environ if environ is None else environ
    raw = environment.get(ENVIRONMENT_KEY, "10000")
    if (not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal()
            or len(raw) > 6 or not 1 <= int(raw) <= QOS_MAX_KBPS):
        raise TrafficAdmissionBlocked("TRAFFIC_GENERATOR_LIMIT_UNAVAILABLE",
                                      "QoS generator limit configuration is invalid")
    counts = {"web_browsing": body["baselineUes"], **body["events"]}
    if "serviceRequirements" in body:
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        resolved = ServiceRequirementCompiler.compile(body["serviceRequirements"], body)
        requested = max(sum(r[key] for r in resolved["slices"].values())
                        for key in ("offeredUplinkKbps", "offeredDownlinkKbps"))
    else:
        requested = sum(counts[key] * _PROFILES[key][1] for key in SCENARIO_SLICES)
    if not 1 <= sum(counts.values()) <= 100 or requested > int(raw):
        raise TrafficAdmissionBlocked("TRAFFIC_GENERATOR_LIMIT_EXCEEDED",
            f"QoS requested {requested} kbps per direction; generator limit {int(raw)}; no load was reduced")
