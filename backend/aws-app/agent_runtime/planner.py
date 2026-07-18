from __future__ import annotations

from typing import Any
import math

from agent_runtime.scenario_validation import SCENARIO_SPECS
from models import EventConfig


class NetworkPlanner:
    def build_plan(
        self,
        intent: dict[str, Any],
        cfg: EventConfig,
        baseline: dict[str, Any],
        observed_target_load: int,
    ) -> dict[str, Any]:
        event_type = intent.get("eventType", "")
        locale = str(intent.get("locale") or "en")
        metrics = baseline.get("metrics") or {}
        if event_type == "network_round":
            plan = self.network_round_plan(intent, metrics, baseline.get("slices") or [], locale)
        else:
            plan = self.spec_plan(event_type, locale)
        if self.should_prepare_capacity(metrics, observed_target_load):
            insertion_index = next((index for index, item in enumerate(plan) if item.get("tool") == "verify_sla"), len(plan))
            plan.insert(
                insertion_index,
                self.step(
                    insertion_index + 1,
                    "patch_hpa",
                    "Validate HPA scaling bounds before post-orchestration SLA verification.",
                    {"component": "UPF", "targetReplicas": self.capacity_target(metrics, observed_target_load)},
                ),
            )
            for index, item in enumerate(plan):
                item["step"] = index + 1
        return {
            "riskLevel": intent.get("riskLevel", cfg.risk),
            "observations": self.observations(intent, metrics, observed_target_load),
            "plan": plan,
            "strategy": self.strategy_name(cfg, metrics, observed_target_load, locale),
        }

    def network_round_plan(self, intent: dict[str, Any], metrics: dict[str, Any], slices: list[dict[str, Any]], locale: str) -> list[dict[str, Any]]:
        max_load = max((float(item.get("load") or 0) for item in slices), default=0.0)
        latency = float(metrics.get("latencyMs") or 0)
        tools = ["get_network_analytics", "list_subscribers", "upsert_subscriber_profile"]
        if max_load >= 60 or latency >= 10:
            tools.append("activate_qos_policy")
        if max_load >= 75 or float(metrics.get("upfCpuPercent") or 0) >= 65:
            tools.append("request_traffic_influence")
        tools.append("verify_sla")
        return [self.step(index + 1, tool, self.reason_for_tool(tool, "network_round", locale)) for index, tool in enumerate(tools)]

    @staticmethod
    def capacity_target(metrics: dict[str, Any], observed_target_load: int) -> int:
        pressure = max(float(metrics.get("upfCpuPercent") or 0), float(observed_target_load))
        return max(2, min(4, int(math.ceil(pressure / 35.0))))

    def spec_plan(self, event_type: str, locale: str = "en") -> list[dict[str, Any]]:
        steps = SCENARIO_SPECS.get(event_type, {}).get("steps") or [
            "get_network_analytics",
            "list_subscribers",
            "upsert_subscriber_profile",
            "verify_sla",
        ]
        filtered_steps = [tool for tool in steps if tool != "start_ueransim_profile"]
        return [self.step(index + 1, tool, self.reason_for_tool(tool, event_type, locale)) for index, tool in enumerate(filtered_steps)]

    @staticmethod
    def reason_for_tool(tool: str, event_type: str, locale: str = "en") -> str:
        if locale == "zh-TW":
            reasons_zh = {
                "get_network_analytics": "修改網路策略前，先擷取即時基線。",
                "list_subscribers": "變更設定檔前，先確認 CityVerse 訂閱用戶狀態。",
                "upsert_subscriber_profile": "確認 UE 的 NSSAI、DNN、AMBR 與 5QI 設定符合意圖。",
                "create_pfd_rule": "進行 QoS 或路徑調度前，先分類應用流量。",
                "activate_qos_policy": "透過明確的 PCF QoS 策略保護或搶占目標流量。",
                "request_traffic_influence": "將流量導向預期的邊緣節點或受保護路徑。",
                "verify_sla": "驗證調度後指標、差異、隔離效果與完成狀態。",
            }
            if event_type == "accident" and tool == "create_pfd_rule":
                return "先分類 V2X 流量；必要的 NEF 順序為 PFD → AS-QoS → Traffic Influence。"
            return reasons_zh.get(tool, tool)
        reasons = {
            "get_network_analytics": "Capture live baseline before modifying network policy.",
            "list_subscribers": "Confirm CityVerse subscriber state before profile changes.",
            "upsert_subscriber_profile": "Ensure UE NSSAI, DNN, AMBR, and 5QI profile match the intent.",
            "create_pfd_rule": "Classify application flows before QoS or steering orchestration.",
            "activate_qos_policy": "Protect or preempt target flows with explicit PCF QoS policy.",
            "request_traffic_influence": "Steer traffic toward the expected edge or protected path.",
            "verify_sla": "Verify post-action metrics, delta, isolation, and completion status.",
        }
        if event_type == "accident" and tool == "create_pfd_rule":
            return "Classify V2X flows first; required NEF order is PFD -> AS-QoS -> Traffic Influence."
        return reasons.get(tool, tool)

    @staticmethod
    def should_prepare_capacity(metrics: dict[str, Any], observed_target_load: int) -> bool:
        return float(metrics.get("upfCpuPercent") or 0) >= 70 or observed_target_load >= 85

    @staticmethod
    def strategy_name(cfg: EventConfig, metrics: dict[str, Any], observed_target_load: int, locale: str = "en") -> str:
        zh = locale == "zh-TW"
        if NetworkPlanner.enum_value(cfg.slice_type) in {"URLLC", "V2X"}:
            return "QoS 優先的閉迴路保護" if zh else "QoS-first closed-loop protection"
        if NetworkPlanner.should_prepare_capacity(metrics, observed_target_load):
            return "容量感知的准入控制" if zh else "Capacity-aware admission"
        return "最小可行調度" if zh else "Minimum viable orchestration"

    @staticmethod
    def step(step: int, tool: str, reason: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"step": step, "tool": tool, "reason": reason, "params": params or {}}

    @staticmethod
    def enum_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    @staticmethod
    def observations(intent: dict[str, Any], metrics: dict[str, Any], observed_target_load: int) -> list[dict[str, Any]]:
        target_slice = intent.get("targetSlice") or {}
        concurrent_scenarios = [
            scenario for scenario in (intent.get("batchScenarios") or []) if scenario != intent.get("eventType")
        ]
        baseline_label = "Pre-event snapshot (includes concurrent scenarios)" if concurrent_scenarios else "Baseline metrics"
        return [
            {
                "label": "Intent",
                "value": f"{intent.get('eventType')} -> {target_slice.get('name')} SST={target_slice.get('sst')} 5QI={target_slice.get('fiveQi')}",
                "severity": intent.get("riskLevel", "medium"),
                "source": "intent_manager",
            },
            {
                "label": "Event scale",
                "value": f"{intent.get('eventScale', 0)} affected / {intent.get('cityResidents', 0)} residents; loop every {(intent.get('controlLoop') or {}).get('pollIntervalSeconds', 30)}s",
                "severity": intent.get("riskLevel", "medium"),
                "source": "planner_event_context",
            },
            {
                "label": baseline_label,
                "value": f"{metrics.get('throughputMbps', 0)} Mbps, {metrics.get('latencyMs', 0)} ms, UPF {metrics.get('upfCpuPercent', 0)}%",
                "severity": "high" if float(metrics.get("upfCpuPercent") or 0) >= 70 else "low",
                "source": metrics.get("dataSource", "unknown"),
                "concurrentScenarios": concurrent_scenarios,
            },
            {
                "label": "Observed target slice",
                "value": f"load {observed_target_load}%",
                "severity": "critical" if observed_target_load >= 90 else "high" if observed_target_load >= 70 else "low",
                "source": metrics.get("dataSource", "unknown"),
            },
            NetworkPlanner.baseline_protection_observation(intent, metrics),
        ]

    @staticmethod
    def baseline_protection_observation(intent: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
        baseline_protection = (intent.get("sla") or {}).get("baselineProtection") or {}
        floor_mbps = baseline_protection.get("floorMbps", 0)
        traffic = metrics.get("scenarioTraffic") if isinstance(metrics.get("scenarioTraffic"), list) else []
        baseline_sample = next(
            (item for item in traffic if isinstance(item, dict) and str(item.get("scenario") or "") == "baseline"),
            None,
        )
        observed_value = f"{baseline_sample.get('throughputMbps', 0)} Mbps" if baseline_sample else "unavailable"
        return {
            "label": "Citizen baseline protection",
            "value": f"Citizen baseline: {observed_value} observed; protection floor {floor_mbps:g} Mbps",
            "severity": "low",
            "source": baseline_protection.get("floorSource", "unknown"),
        }
