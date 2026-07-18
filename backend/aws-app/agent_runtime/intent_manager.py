from __future__ import annotations

from typing import Any

from models import EventConfig
from scale_model import sla_mbps_for_ratio


class IntentManager:
    def build_intent(
        self,
        execution_id: str,
        event_type: str,
        cfg: EventConfig,
        scenario_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scenario_context = scenario_context or {}
        risk = self.enum_value(cfg.risk)
        slice_type = self.enum_value(cfg.slice_type)
        latency_target = self.latency_target_for_slice(slice_type)
        raw_scale_ratio = scenario_context.get("scaleRatio")
        # `is not None` (not truthy-or): a legitimate tiny ratio can round to 0.0,
        # and `0.0 or 1.0` would silently disable SLA scaling entirely.
        sla_scale_ratio = float(raw_scale_ratio) if raw_scale_ratio is not None else 1.0
        throughput_target = self.throughput_target_for_event(event_type, sla_scale_ratio)
        event_scale = int(scenario_context.get("eventScale") or cfg.ue_count or 0)
        city_residents = int(scenario_context.get("cityResidents") or 0)
        baseline_protection = self.build_baseline_protection(scenario_context)
        batch_scenarios = scenario_context.get("batchScenarios") or []
        # Runtime traffic uses one real representative UE per selected scenario
        # plus the two permanent resident bearers (eMBB and mMTC).  Affected
        # population changes the measured traffic rate, not the number of
        # fabricated PDU sessions.
        expected_runtime_sessions = 2 + len(batch_scenarios) if batch_scenarios else 3
        return {
            "executionId": execution_id,
            "eventType": event_type,
            "intentType": "continuous_control" if risk in ("high", "critical") else "one_shot",
            "eventScale": event_scale,
            "cityResidents": city_residents,
            "controlLoop": {
                "pollIntervalSeconds": 30,
                "eventDurationSeconds": int(scenario_context.get("eventDurationSeconds") or 120),
                "cooldownSeconds": int(scenario_context.get("cooldownSeconds") or 45),
            },
            "riskLevel": risk,
            "targetSlice": {
                "sst": cfg.slice_sst,
                "sd": cfg.slice_sd,
                "name": slice_type,
                "fiveQi": cfg.five_qi,
                "dnn": cfg.dnn,
            },
            "sla": {
                "latencyMsMax": latency_target,
                "minThroughputMbps": throughput_target,
                "minPduSessions": expected_runtime_sessions,
                "maxUpfCpuPercent": 75,
                "slaScaleRatio": round(sla_scale_ratio, 6),
                "baselineProtection": baseline_protection,
            },
            "trafficProfile": cfg.traffic_profile,
            "ueIds": cfg.ue_ids,
            "runtimePrimed": bool(scenario_context.get("runtimePrimed")),
            "runtimePrime": scenario_context.get("runtimePrime") or {},
            "batchScenarios": batch_scenarios,
            "locale": scenario_context.get("locale") or "en",
        }

    @staticmethod
    def latency_target_for_slice(slice_type: str) -> int:
        if slice_type == "URLLC":
            return 10
        if slice_type == "V2X":
            return 50
        if slice_type == "mMTC":
            return 120
        return 50

    @staticmethod
    def throughput_target_for_event(event_type: str, scale_ratio: float = 1.0) -> float:
        # The SLA follows the same bounded population model as the TUN-bound iperf3
        # target, with 20% headroom for UDP/IP/GTP and sampling overhead.
        if event_type == "network_round":
            return 1.0
        return sla_mbps_for_ratio(event_type, scale_ratio)

    # The resident UE sidecar intentionally emits a low, non-saturating nominal
    # 1 Mbps teaching baseline.  Allow 5% for iperf/protocol and sampling
    # overhead; otherwise a healthy 0.998 Mbps bearer is falsely degraded.
    DEFAULT_BASELINE_FLOOR_MBPS = 0.95
    BASELINE_FLOOR_RATIO = 0.8

    @staticmethod
    def build_baseline_protection(scenario_context: dict[str, Any]) -> dict[str, Any]:
        """Protect citizen (eMBB) baseline traffic: floor = pre-event observed
        baseline throughput * 0.8, or a conservative default when no pre-event
        baseline sample was available (e.g. this trigger's first observation)."""
        raw_baseline_throughput = scenario_context.get("baselineThroughputMbps")
        if raw_baseline_throughput is not None:
            try:
                observed = float(raw_baseline_throughput)
            except (TypeError, ValueError):
                observed = None
        else:
            observed = None
        if observed is not None and observed > 0:
            return {
                "sliceType": "eMBB",
                "floorMbps": round(observed * IntentManager.BASELINE_FLOOR_RATIO, 3),
                "floorSource": "pre-event-observed",
            }
        return {
            "sliceType": "eMBB",
            "floorMbps": IntentManager.DEFAULT_BASELINE_FLOOR_MBPS,
            "floorSource": "default",
        }

    @staticmethod
    def enum_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value
