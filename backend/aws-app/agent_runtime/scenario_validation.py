from __future__ import annotations

from typing import Any


NF_COMPONENTS = ("AMF", "SMF", "UPF", "PCF", "NEF", "UDM", "AUSF")


SCENARIO_SPECS: dict[str, dict[str, Any]] = {
    "concert": {
        "phase": "1",
        "name": "AR Concert",
        "steps": [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "create_pfd_rule",
            "verify_sla",
        ],
        "nefApis": ["PFD Management"],
        "improvements": [
            "Report throughput delta instead of treating 500 Mbps as a binary pass/fail target.",
            "Verify other slices do not degrade by more than 10% during eMBB pressure.",
            "Classify AR application flow with PFD before QoS verification.",
        ],
    },
    "typhoon": {
        "phase": "2",
        "name": "Typhoon",
        "steps": [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "activate_qos_policy",
            "verify_sla",
        ],
        "nefApis": ["AS-Session-with-QoS"],
        "improvements": [
            "Expose URLLC preemption by comparing eMBB throughput before and after QoS.",
            "Show dual gNB URLLC traffic for disaster resilience.",
            "Capture AMF registration pressure from emergency re-registration.",
        ],
    },
    "accident": {
        "phase": "3",
        "name": "Traffic Accident",
        "steps": [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "create_pfd_rule",
            "activate_qos_policy",
            "request_traffic_influence",
            "verify_sla",
        ],
        "nefApis": ["PFD Management", "AS-Session-with-QoS", "Traffic Influence"],
        "improvements": [
            "Enforce NEF ordering: PFD -> AS-QoS -> Traffic Influence.",
            "Confirm 5QI=79 V2X mapping before SLA verification.",
            "Treat MEC path change as required evidence, not just API success.",
        ],
    },
    "medical": {
        "phase": "4",
        "name": "ER Surge",
        "steps": [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "activate_qos_policy",
            "request_traffic_influence",
            "verify_sla",
        ],
        "nefApis": ["AS-Session-with-QoS", "Traffic Influence"],
        "improvements": [
            "Require verify_sla to complete instead of leaving ER Surge at 6/7 steps.",
            "Flag 0 ms latency from local EKS measurement as a loopback artifact.",
            "Treat 5QI=1 as the strongest preemption profile.",
        ],
    },
    "iot_surge": {
        "phase": "5",
        "name": "IoT Surge",
        "steps": [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "verify_sla",
        ],
        "nefApis": [],
        "improvements": [
            "Use 5QI=79 for mMTC in this free5GC deployment because 5QI=6 did not establish the required IoT PDU sessions.",
            "Verify IoT Surge with its own SLA result, not a residual ER Surge result.",
            "Differentiate control-plane storm scaling from UPF data-plane scaling.",
        ],
    },
}


class ScenarioValidationReporter:
    def build(
        self,
        event_type: str,
        intent: dict[str, Any],
        baseline: dict[str, Any],
        executor: dict[str, Any],
        verification: dict[str, Any],
        final_metrics: dict[str, Any],
        final_slices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        spec = SCENARIO_SPECS.get(event_type, {})
        actions = executor.get("actions") or []
        completed = len([item for item in actions if item.get("status") == "success"])
        required = len(spec.get("steps") or actions)
        baseline_metrics = baseline.get("metrics") or {}
        baseline_slices = baseline.get("slices") or []
        return {
            "scenario": spec.get("name", event_type),
            "phase": spec.get("phase", ""),
            "baseline_captured": {
                "source": baseline_metrics.get("dataSource", "unknown"),
                "per_slice_throughput_mbps": self.per_slice_throughput(baseline_slices),
                "total_pdu_sessions": baseline_metrics.get("pduSessionCount", 0),
                "upf_cpu_percent": baseline_metrics.get("upfCpuPercent", 0),
            },
            "steps_completed": f"{completed}/{required}",
            "required_steps": spec.get("steps", []),
            "nef_apis_called": self.nef_apis_called(actions),
            "nef_apis_required": spec.get("nefApis", []),
            "sla_result": self.sla_result(intent, baseline_metrics, verification, final_metrics, baseline_slices, final_slices),
            "k8s_scaling_observed": self.k8s_scaling_observed(final_metrics),
            "improvements_vs_previous": spec.get("improvements", []),
            "remaining_issues": self.remaining_issues(event_type, spec, actions, verification, baseline_metrics, final_metrics),
        }

    @staticmethod
    def per_slice_throughput(slices: list[dict[str, Any]]) -> dict[str, float]:
        result: dict[str, float] = {}
        for item in slices:
            key = str(item.get("type") or item.get("sst") or "unknown")
            result[key] = float(item.get("throughputMbps") or 0)
        return result

    @staticmethod
    def nef_apis_called(actions: list[dict[str, Any]]) -> list[str]:
        mapping = {
            "create_pfd_rule": "PFD Management",
            "activate_qos_policy": "AS-Session-with-QoS",
            "request_traffic_influence": "Traffic Influence",
        }
        called: list[str] = []
        for action in actions:
            label = mapping.get(str(action.get("tool") or ""))
            if label and label not in called and action.get("status") == "success":
                called.append(label)
        return called

    def sla_result(
        self,
        intent: dict[str, Any],
        baseline_metrics: dict[str, Any],
        verification: dict[str, Any],
        final_metrics: dict[str, Any],
        baseline_slices: list[dict[str, Any]],
        final_slices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sla = intent.get("sla") or {}
        latency = float(final_metrics.get("latencyMs") or 0)
        throughput = float(final_metrics.get("throughputMbps") or 0)
        target_slice = (intent.get("targetSlice") or {}).get("sst")
        delta = throughput - float(baseline_metrics.get("throughputMbps") or 0)
        isolation = self.max_slice_degradation_percent(target_slice, baseline_slices, final_slices)
        return {
            "latency_ms": {
                "value": latency,
                "threshold": sla.get("latencyMsMax", 0),
                "passed": latency <= float(sla.get("latencyMsMax") or 0),
            },
            "throughput_mbps": {
                "value": throughput,
                "threshold": sla.get("minThroughputMbps", 0),
                "passed": self.check_status(verification, "throughputMbps") in {"passed", "degraded"},
                "delta_from_baseline": round(delta, 3),
            },
            "isolation_check": {
                "max_degradation_percent": isolation,
                "passed": isolation <= 10,
            },
            "status": verification.get("status", "unknown"),
            "data_source": verification.get("dataSource", final_metrics.get("dataSource", "unknown")),
        }

    @staticmethod
    def check_status(verification: dict[str, Any], metric: str) -> str:
        for check in verification.get("checks", []):
            if check.get("metric") == metric:
                return str(check.get("status") or "unknown")
        return "unknown"

    @staticmethod
    def max_slice_degradation_percent(target_sst: Any, baseline_slices: list[dict[str, Any]], final_slices: list[dict[str, Any]]) -> float:
        final_by_sst = {item.get("sst"): item for item in final_slices}
        degradations: list[float] = []
        for before in baseline_slices:
            sst = before.get("sst")
            if sst == target_sst:
                continue
            after = final_by_sst.get(sst) or {}
            before_value = float(before.get("throughputMbps") or before.get("sessions") or 0)
            after_value = float(after.get("throughputMbps") or after.get("sessions") or 0)
            if before_value <= 0:
                continue
            degradations.append(max(0.0, (before_value - after_value) / before_value * 100))
        return round(max(degradations or [0.0]), 2)

    @staticmethod
    def k8s_scaling_observed(metrics: dict[str, Any]) -> dict[str, int]:
        counts = metrics.get("podCounts") if isinstance(metrics.get("podCounts"), dict) else {}
        return {component: int(counts.get(component, 0) or 0) for component in NF_COMPONENTS}

    def remaining_issues(
        self,
        event_type: str,
        spec: dict[str, Any],
        actions: list[dict[str, Any]],
        verification: dict[str, Any],
        baseline_metrics: dict[str, Any],
        final_metrics: dict[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        completed_tools = [str(action.get("tool") or "") for action in actions if action.get("status") == "success"]
        for required in spec.get("steps", []):
            if required not in completed_tools:
                issues.append(f"Required step did not complete: {required}")
        for required_api in spec.get("nefApis", []):
            if required_api not in self.nef_apis_called(actions):
                issues.append(f"Required NEF API not confirmed: {required_api}")
        if verification.get("status") in {"failed", "degraded"}:
            issues.append(f"SLA verification is {verification.get('status')}")
        if event_type == "medical" and str(baseline_metrics.get("dataSource")) == "eks+iperf3" and float(baseline_metrics.get("latencyMs") or 0) == 0:
            issues.append("Baseline latency is 0 ms from EKS; treat as local loopback artifact, not real forwarding latency.")
        if event_type == "iot_surge" and float(final_metrics.get("throughputMbps") or 0) > 10:
            issues.append("IoT Surge data-plane throughput exceeded 10 Mbps; control-plane storm signal may be obscured.")
        return issues
