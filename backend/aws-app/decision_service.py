from __future__ import annotations

from typing import Any

from models import EventConfig
from time_utils import TimeUtils


class AgentDecisionService:
    def build_decision(
        self,
        event_type: str,
        cfg: EventConfig,
        free5gc_result: dict[str, Any],
        environment_result: dict[str, Any],
        observed_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
        current_slices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        free5gc_action_status = "success" if free5gc_result.get("status") == "success" else "failed"
        environment_action_status = "success" if environment_result.get("status") == "success" else "failed"
        target_slice = next((item for item in current_slices if item["sst"] == cfg.slice_sst), None)
        target_load = target_slice["load"] if target_slice else 0
        observations = self.build_observations(event_type, cfg, observed_metrics, current_metrics, target_slice)
        hypotheses = self.build_hypotheses(event_type, cfg, observed_metrics, target_load)
        selected_plan, rejected_plans = self.choose_plan(event_type, cfg, observed_metrics, target_load)
        verification = self.build_verification_checks(cfg, observed_metrics, current_metrics, target_load)
        selected_plan["status"] = "executing" if environment_action_status == "success" else "degraded"

        return {
            "agentName": "Supervisor Agent",
            "riskLevel": cfg.risk,
            "decision": (
                f"{event_type} accepted after observing {observed_metrics.get('dataSource', 'unknown')} metrics. "
                f"The selected plan protects {cfg.slice_type} SST={cfg.slice_sst} SD={cfg.slice_sd} "
                f"with 5QI={cfg.five_qi} and verifies the result against throughput, latency, sessions, and slice load."
            ),
            "observations": observations,
            "hypotheses": hypotheses,
            "selectedPlan": selected_plan,
            "rejectedPlans": rejected_plans,
            "actions": [
                {
                    "type": "prometheus",
                    "description": "Observe current Prometheus/free5GC analytics before choosing an action",
                    "api": "Prometheus /api/v1/query + free5GC status",
                    "status": "success",
                    "httpStatus": 200,
                    "because": "Agent must establish a baseline before changing QoS or traffic generators",
                    "expectedImpact": "Creates a before-state for later verification",
                    "verificationMetric": "baseline captured",
                },
                {
                    "type": "free5gc_subscriber",
                    "description": f"Create/update free5GC subscriber QoS/NSSAI for {event_type}",
                    "api": "free5GC WebUI /api/subscriber",
                    "status": free5gc_action_status,
                    "httpStatus": free5gc_result.get("httpStatus", 0),
                    "because": f"{cfg.slice_type} traffic needs explicit NSSAI and 5QI policy before UEs attach",
                    "expectedImpact": f"UEs map to SST={cfg.slice_sst} SD={cfg.slice_sd} with 5QI={cfg.five_qi}",
                    "verificationMetric": "subscriber/profile upsert result",
                },
                {
                    "type": "ueransim",
                    "description": f"Start {cfg.ue_count} UE(s) and launch traffic profile: {cfg.traffic_profile}",
                    "api": "EKS Kubernetes API: UERANSIM + iperf3 Job",
                    "status": environment_action_status,
                    "httpStatus": environment_result.get("httpStatus", 500),
                    "because": selected_plan["rationale"],
                    "expectedImpact": selected_plan["expectedImpact"],
                    "verificationMetric": "PDU sessions and GTP packet rate",
                },
                {
                    "type": "prometheus",
                    "description": "Verify post-action network state against expected metrics",
                    "api": "Prometheus /api/v1/query",
                    "status": "success",
                    "httpStatus": 200,
                    "because": "Agent decisions should close the loop instead of stopping after orchestration",
                    "expectedImpact": "Confirms whether the selected plan improved the target indicators",
                    "verificationMetric": "latency / throughput / UPF CPU / slice load",
                },
            ],
            "verification": verification,
            "expectedOutcome": selected_plan["expectedImpact"],
            "startedAt": TimeUtils.now(),
        }

    def build_observations(
        self,
        event_type: str,
        cfg: EventConfig,
        observed_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
        target_slice: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        target_load = target_slice["load"] if target_slice else 0
        return [
            {
                "label": "Event intent",
                "value": f"{event_type} -> {cfg.slice_type} SST={cfg.slice_sst} 5QI={cfg.five_qi}",
                "severity": cfg.risk,
                "source": "event_config",
            },
            {
                "label": "Baseline throughput",
                "value": f"{observed_metrics.get('throughputMbps', 0)} Mbps",
                "severity": self.severity_for_threshold(observed_metrics.get("throughputMbps", 0), 500, 900),
                "source": observed_metrics.get("dataSource", "unknown"),
            },
            {
                "label": "Baseline latency",
                "value": f"{observed_metrics.get('latencyMs', 0)} ms",
                "severity": self.severity_for_threshold(observed_metrics.get("latencyMs", 0), 20, 50),
                "source": observed_metrics.get("dataSource", "unknown"),
            },
            {
                "label": "UPF headroom",
                "value": f"{observed_metrics.get('upfCpuPercent', 0)}% CPU across {observed_metrics.get('upfPodCount', 0)} pod(s)",
                "severity": self.severity_for_threshold(observed_metrics.get("upfCpuPercent", 0), 60, 80),
                "source": observed_metrics.get("dataSource", "unknown"),
            },
            {
                "label": "Observed target slice",
                "value": f"{cfg.slice_type} load {target_load}% with {current_metrics.get('pduSessionCount', 0)} PDU session(s)",
                "severity": self.severity_for_threshold(target_load, 70, 85),
                "source": current_metrics.get("dataSource", "unknown"),
            },
        ]

    def build_hypotheses(self, event_type: str, cfg: EventConfig, observed_metrics: dict[str, Any], target_load: int) -> list[str]:
        hypotheses = [
            f"If {cfg.slice_type} is not pinned to SST={cfg.slice_sst} and 5QI={cfg.five_qi}, traffic may fall back to a weaker default policy.",
            f"Observed {cfg.slice_type} load is {target_load}%, so the selected action should prioritize policy correctness before adding capacity.",
        ]
        if cfg.slice_type in ("URLLC", "V2X"):
            hypotheses.append("Latency-sensitive traffic is more likely to fail from QoS misclassification than from raw bandwidth shortage.")
        if event_type in ("concert", "iot_surge"):
            hypotheses.append("High aggregate traffic can push UPF or AMF resources before individual UE sessions report errors.")
        if observed_metrics.get("upfCpuPercent", 0) >= 70:
            hypotheses.append("UPF CPU is already elevated, so traffic generation should be paired with scale readiness and post-action verification.")
        return hypotheses

    def choose_plan(
        self,
        event_type: str,
        cfg: EventConfig,
        observed_metrics: dict[str, Any],
        target_load: int,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        latency_sensitive = cfg.slice_type in ("URLLC", "V2X")
        capacity_pressure = observed_metrics.get("upfCpuPercent", 0) >= 70 or target_load >= 85
        if latency_sensitive:
            return (
                {
                    "name": "QoS-first slice protection",
                    "rationale": "Latency-sensitive sessions need correct NSSAI/5QI policy before capacity changes can help.",
                    "expectedImpact": f"{cfg.slice_type} UEs attach with the intended 5QI and keep latency bounded during the event.",
                },
                [
                    {"name": "Scale-only response", "reason": "More pods do not fix a misclassified URLLC/V2X flow."},
                    {"name": "Best-effort load handling", "reason": "It would react to load without protecting the service class."},
                ],
            )
        if capacity_pressure:
            return (
                {
                    "name": "Capacity-aware traffic activation",
                    "rationale": "The event is throughput/session heavy and may consume UPF or AMF headroom.",
                    "expectedImpact": "Traffic starts with subscriber policy in place and Prometheus verification confirms whether scale-out is needed.",
                },
                [
                    {"name": "QoS-only response", "reason": "Policy alone cannot absorb sustained throughput or registration pressure."},
                    {"name": "No verification", "reason": "Capacity pressure must be checked after traffic starts."},
                ],
            )
        return (
            {
                "name": "Minimum viable orchestration",
                "rationale": "Current resource headroom is sufficient, so the least disruptive action is policy setup plus targeted traffic.",
                "expectedImpact": "Scenario traffic is represented without unnecessary scaling or policy churn.",
            },
            [
                {"name": "Immediate HPA expansion", "reason": "No current threshold justifies extra replicas."},
                {"name": "Traffic suppression", "reason": "The event can be admitted under current load."},
            ],
        )

    def build_verification_checks(
        self,
        cfg: EventConfig,
        observed_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
        target_load: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "metric": "throughputMbps",
                "before": observed_metrics.get("throughputMbps", 0),
                "target": current_metrics.get("throughputMbps", 0),
                "status": "pending",
                "passCondition": "actual throughput reaches the scenario profile without packet collapse",
            },
            {
                "metric": "latencyMs",
                "before": observed_metrics.get("latencyMs", 0),
                "target": self.latency_target_for_slice(cfg.slice_type),
                "status": "pending",
                "passCondition": f"{cfg.slice_type} latency remains within service target",
            },
            {
                "metric": "upfCpuPercent",
                "before": observed_metrics.get("upfCpuPercent", 0),
                "target": 75,
                "status": "pending",
                "passCondition": "UPF CPU stays below scale-out threshold or HPA adds capacity",
            },
            {
                "metric": "sliceLoad",
                "before": "baseline",
                "target": target_load,
                "status": "pending",
                "passCondition": "target slice absorbs the event while other slices remain available",
            },
        ]

    @staticmethod
    def severity_for_threshold(value: float, warn: float, critical: float) -> str:
        if value >= critical:
            return "critical"
        if value >= warn:
            return "high"
        return "low"

    @staticmethod
    def latency_target_for_slice(slice_type: str) -> int:
        if slice_type == "URLLC":
            return 10
        if slice_type == "V2X":
            return 20
        if slice_type == "mMTC":
            return 120
        return 50
