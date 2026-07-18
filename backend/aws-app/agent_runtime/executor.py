from __future__ import annotations

from typing import Any, Callable

from models import EventConfig


class NetworkExecutor:
    RUNTIME_ONLY_TOOLS = {"start_ueransim_profile"}

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def execute(
        self,
        plan: dict[str, Any],
        cfg: EventConfig,
        intent: dict[str, Any],
        on_action: Callable[[list[dict[str, Any]], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        actions = []
        for item in plan.get("plan", []):
            tool = item.get("tool", "")
            params = dict(item.get("params") or {})
            params.setdefault("executionId", intent.get("executionId"))
            params.setdefault("eventType", intent.get("eventType"))
            if tool in self.RUNTIME_ONLY_TOOLS:
                result = {
                    "status": "skipped",
                    "operation": "runtime_managed_by_event_trigger",
                    "reason": "Real UE/session traffic is primed before agent planning; the executor only orchestrates free5GC resources.",
                }
            else:
                result = self.gateway.call(tool, params, cfg, intent)
            if result.get("status") in {"success", "preseeded"}:
                status = "success"
            elif result.get("status") == "skipped":
                status = "skipped"
            else:
                status = "failed"
            actions.append(
                {
                    "type": self.action_type(tool),
                    "tool": tool,
                    "description": item.get("reason", tool),
                    "api": result.get("api") or tool,
                    "status": status,
                    "httpStatus": result.get("httpStatus", 200 if status in {"success", "skipped"} else 500),
                    "because": item.get("reason", ""),
                    "expectedImpact": self.expected_impact(tool, str(intent.get("locale") or "en")),
                    "verificationMetric": self.verification_metric(tool),
                    "result": result,
                }
            )
            if on_action:
                on_action(actions, item)
            if status == "failed" and self.is_blocking_tool(tool):
                return {"approved": True, "status": "failed", "actions": actions}
        blocking_failures = [
            action
            for action in actions
            if action.get("status") == "failed" and self.is_blocking_tool(str(action.get("tool") or ""))
        ]
        overall_status = "failed" if blocking_failures else "success"
        return {"approved": True, "status": overall_status, "actions": actions}

    @staticmethod
    def is_blocking_tool(tool: str) -> bool:
        return tool in {"get_network_analytics", "list_subscribers"}

    @staticmethod
    def action_type(tool: str) -> str:
        mapping = {
            "get_network_analytics": "prometheus",
            "list_subscribers": "free5gc_subscriber",
            "upsert_subscriber_profile": "free5gc_subscriber",
            "activate_qos_policy": "nef_qos",
            "request_traffic_influence": "nef_traffic_influence",
            "create_pfd_rule": "nef_pfd",
            "patch_hpa": "k8s_hpa",
            "verify_sla": "prometheus",
        }
        return mapping.get(tool, "prometheus")

    @staticmethod
    def expected_impact(tool: str, locale: str = "en") -> str:
        if locale == "zh-TW":
            impacts_zh = {
                "get_network_analytics": "建立可觀測的網路基線。",
                "list_subscribers": "確認目前的訂閱用戶狀態。",
                "upsert_subscriber_profile": "將 UE 對應到預期的切片與 QoS 設定檔。",
                "activate_qos_policy": "保護對延遲敏感的流量。",
                "request_traffic_influence": "準備邊緣路徑導流。",
                "create_pfd_rule": "分類應用流量，供策略控制使用。",
                "patch_hpa": "讓容量擴縮維持在設定範圍內。",
                "verify_sla": "以調度後 SLA 檢查完成閉迴路。",
            }
            return impacts_zh.get(tool, "推進網路調度意圖。")
        impacts = {
            "get_network_analytics": "Creates an observable baseline.",
            "list_subscribers": "Confirms the current subscriber surface.",
            "upsert_subscriber_profile": "Maps UEs to the intended slice and QoS profile.",
            "activate_qos_policy": "Protects latency-sensitive traffic.",
            "request_traffic_influence": "Prepares edge path steering.",
            "create_pfd_rule": "Classifies application flows for policy control.",
            "patch_hpa": "Keeps capacity scaling inside configured bounds.",
            "verify_sla": "Closes the loop with post-action SLA checks.",
        }
        return impacts.get(tool, "Advances the network intent.")

    @staticmethod
    def verification_metric(tool: str) -> str:
        metrics = {
            "get_network_analytics": "baseline captured",
            "list_subscribers": "subscriber count",
            "upsert_subscriber_profile": "subscriber/profile upsert result",
            "activate_qos_policy": "latency and QoS target",
            "request_traffic_influence": "traffic path state",
            "create_pfd_rule": "flow classification state",
            "patch_hpa": "HPA desired/current replicas",
            "verify_sla": "latency / throughput / sessions / UPF CPU",
        }
        return metrics.get(tool, "tool result")
