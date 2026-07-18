from __future__ import annotations

from typing import Any, Callable

from agent_runtime.executor import NetworkExecutor
from agent_runtime.intent_manager import IntentManager
from agent_runtime.planner import NetworkPlanner
from agent_runtime.scenario_validation import ScenarioValidationReporter
from agent_runtime.sla_verifier import SlaVerifier
from agent_runtime.tool_gateway import ToolGateway
from agent_runtime.data_plane_evidence_reader import KubernetesDataPlaneEvidenceReader
from models import EventConfig
from time_utils import TimeUtils


class AgentxGCoreLoop:
    def __init__(
        self,
        metrics: Any,
        free5gc: Any,
        environment: Any,
        current_metrics: Any,
        current_slices: Any,
        runtime_subscriber_upsert_limit: int,
        lambda_function_names: dict[str, str] | None = None,
        invalidate_metrics: Callable[[], None] | None = None,
        record_nef_hit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.intent_manager = IntentManager()
        self.planner = NetworkPlanner()
        self.validation_reporter = ScenarioValidationReporter()
        self.verifier = SlaVerifier()
        evidence_reader = None
        if getattr(environment, "cluster_name", "") and getattr(environment, "namespace", ""):
            evidence_reader = KubernetesDataPlaneEvidenceReader(environment.cluster_name, environment.namespace)
        self.gateway = ToolGateway(
            metrics,
            free5gc,
            environment,
            current_metrics,
            current_slices,
            runtime_subscriber_upsert_limit,
            lambda_function_names or {},
            record_hit=record_nef_hit,
            evidence_reader=evidence_reader,
        )
        self.executor = NetworkExecutor(self.gateway)
        self.current_metrics = current_metrics
        self.current_slices = current_slices
        self.invalidate_metrics = invalidate_metrics

    def run(
        self,
        execution_id: str,
        event_type: str,
        cfg: EventConfig,
        baseline_metrics: dict[str, Any],
        baseline_slices: list[dict[str, Any]],
        scenario_context: dict[str, Any] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        intent = self.intent_manager.build_intent(execution_id, event_type, cfg, scenario_context)
        target_slice = next((item for item in baseline_slices if item.get("sst") == cfg.slice_sst), None)
        target_load = int((target_slice or {}).get("load") or 0)
        baseline = {"metrics": baseline_metrics, "slices": baseline_slices}
        planner = self.planner.build_plan(intent, cfg, baseline, target_load)
        if on_progress:
            planned_executor = {"approved": True, "status": "running", "actions": self.pending_actions(planner)}
            on_progress(
                {
                    "stage": "planned",
                    "intent": intent,
                    "baseline": baseline,
                    "planner": planner,
                    "executor": planned_executor,
                    "agentDecision": self.decision_payload(intent, planner, planned_executor, {"status": "running", "checks": []}, cfg),
                }
            )

        def emit_action_progress(actions: list[dict[str, Any]], _item: dict[str, Any]) -> None:
            if not on_progress:
                return
            executor_snapshot = {
                "approved": True,
                "status": "running",
                "actions": self.actions_with_pending(planner, actions),
            }
            on_progress(
                {
                    "stage": "action",
                    "intent": intent,
                    "baseline": baseline,
                    "planner": planner,
                    "executor": executor_snapshot,
                    "agentDecision": self.decision_payload(intent, planner, executor_snapshot, {"status": "running", "checks": []}, cfg),
                }
            )

        executor = self.executor.execute(planner, cfg, intent, on_action=emit_action_progress)
        if executor.get("status") == "failed":
            final_metrics = self.current_metrics(include_oam=False)
            final_slices = baseline_slices
            failed_actions = [action for action in executor.get("actions", []) if action.get("status") == "failed"]
            verification = {
                "status": "degraded",
                "adaptationRequired": False,
                "checks": [],
                "reason": "Blocking orchestration tool failed before post-action SLA verification.",
                "failedActions": [
                    {
                        "tool": action.get("tool"),
                        "error": (action.get("result") or {}).get("error"),
                    }
                    for action in failed_actions
                ],
            }
            adaptation = {
                "round": 0,
                "maxRounds": 1,
                "executed": False,
                "reason": "Skipped because the orchestration plan did not complete.",
            }
            decision = self.decision_payload(intent, planner, executor, verification, cfg)
            decision["adaptation"] = adaptation
            validation_report = self.validation_reporter.build(
                event_type,
                intent,
                baseline,
                executor,
                verification,
                final_metrics,
                final_slices,
            )
            decision["validationReport"] = validation_report
            return {
                "intent": intent,
                "baseline": baseline,
                "planner": planner,
                "executor": executor,
                "verification": verification,
                "adaptation": adaptation,
                "validationReport": validation_report,
                "agentDecision": decision,
                "finalMetrics": final_metrics,
                "finalSlices": final_slices,
            }
        final_metrics = self.current_metrics()
        final_slices = self.current_slices()
        verification = self.verifier.verify(intent, final_metrics, final_slices)
        adaptation = self.adapt_once_if_needed(intent, cfg, verification)
        if adaptation.get("executed"):
            final_metrics = self.current_metrics()
            final_slices = self.current_slices() or final_slices
            verification = self.verifier.verify(intent, final_metrics, final_slices)
            if adaptation.get("effective") is False:
                verification = {
                    **verification,
                    "note": "state unchanged; re-verification reflects pre-adaptation state",
                }
            adaptation["postAdaptationVerification"] = verification
        decision = self.decision_payload(intent, planner, executor, verification, cfg)
        decision["adaptation"] = adaptation
        validation_report = self.validation_reporter.build(
            event_type,
            intent,
            baseline,
            executor,
            verification,
            final_metrics,
            final_slices,
        )
        decision["validationReport"] = validation_report
        return {
            "intent": intent,
            "baseline": baseline,
            "planner": planner,
            "executor": executor,
            "verification": verification,
            "adaptation": adaptation,
            "validationReport": validation_report,
            "agentDecision": decision,
            "finalMetrics": final_metrics,
            "finalSlices": final_slices,
        }

    def pending_actions(self, planner: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": NetworkExecutor.action_type(str(item.get("tool") or "")),
                "tool": item.get("tool"),
                "description": item.get("reason", item.get("tool")),
                "api": item.get("tool"),
                "status": "pending",
                "because": item.get("reason", ""),
                "expectedImpact": NetworkExecutor.expected_impact(str(item.get("tool") or "")),
                "verificationMetric": NetworkExecutor.verification_metric(str(item.get("tool") or "")),
            }
            for item in planner.get("plan", [])
        ]

    def actions_with_pending(self, planner: dict[str, Any], completed_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending = self.pending_actions(planner)
        merged = [dict(action) for action in completed_actions]
        return merged + pending[len(merged):]

    def adapt_once_if_needed(self, intent: dict[str, Any], cfg: EventConfig, verification: dict[str, Any]) -> dict[str, Any]:
        if not verification.get("adaptationRequired"):
            return {"round": 0, "maxRounds": 1, "executed": False, "reason": "SLA verification passed"}
        result = self.gateway.call(
            "patch_hpa",
            {"component": "UPF", "targetReplicas": 4, "executionId": intent.get("executionId"), "eventType": intent.get("eventType")},
            cfg,
            intent,
        )
        if result.get("status") != "failed" and self.invalidate_metrics:
            self.invalidate_metrics()
        adaptation = {
            "round": 1,
            "maxRounds": 1,
            "executed": True,
            "reason": "SLA verification requested bounded adaptation",
            "action": result,
        }
        if result.get("status") in {"skipped", "no-op", "noop"}:
            adaptation["effective"] = False
            adaptation["reason"] = "no actionable lever available (HPA runtime-managed; traffic profile fixed at trigger time)"
        return adaptation

    def decision_payload(
        self,
        intent: dict[str, Any],
        planner: dict[str, Any],
        executor: dict[str, Any],
        verification: dict[str, Any],
        cfg: EventConfig,
    ) -> dict[str, Any]:
        actions = executor.get("actions", [])
        actuator_action = next(
            (
                action
                for action in reversed(actions)
                if action.get("tool") == "activate_qos_policy"
            ),
            None,
        )
        actuator_result = (actuator_action or {}).get("result") or {}
        evidence_fallback = (
            {
                "status": "not-applicable",
                "reason": "No PFCP QER actuation was requested for this decision.",
            }
            if actuator_action is None
            else {
                "status": "unavailable",
                "reason": "PFCP QER actuation was requested but this evidence level was not returned.",
            }
        )
        data_plane_evidence = {
            key: actuator_result.get(key, dict(evidence_fallback))
            for key in ("pfcpEvidence", "kernelEvidence", "effectEvidence")
        }
        verification_checks = [
            {
                "metric": check.get("metric"),
                "before": check.get("actual"),
                "target": check.get("target"),
                "status": check.get("status"),
                "passCondition": check.get("passCondition"),
                **data_plane_evidence,
            }
            for check in verification.get("checks", [])
        ]
        success_count = len([item for item in actions if item.get("status") == "success"])
        baseline_phrase = (
            "Event runtime primed real scenario traffic before planner read live network state"
            if intent.get("runtimePrimed")
            else "Planner read live network state"
        )
        zh = intent.get("locale") == "zh-TW"
        decision_text = (
            f"後端已先產生並觀測真實情境流量，再由 Planner 讀取即時網路狀態與事件規模 {intent.get('eventScale')}，選擇「{planner.get('strategy')}」。"
            f"Executor 完成 {success_count}/{len(actions)} 個受限工具步驟；SLA 驗證結果為 {verification.get('status')}。"
            if zh else
            f"{baseline_phrase} and event scale {intent.get('eventScale')} before selecting {planner.get('strategy')}. Executor ran {success_count}/{len(actions)} bounded tool step(s). SLA verification is {verification.get('status')}; continuous events are re-polled every {(intent.get('controlLoop') or {}).get('pollIntervalSeconds', 30)}s."
        )
        return {
            "agentName": "Planner + Executor Agents",
            "riskLevel": intent.get("riskLevel", cfg.risk),
            "decision": decision_text,
            "intent": intent,
            "observations": planner.get("observations", []),
            "hypotheses": ([
                "Planner 必須比較事件規模、城市人口、切片工作階段、吞吐量、延遲與 UPF CPU，才能選擇動作。",
                "Executor 只能使用有界限的工具，並在宣告穩定前驗證調度後指標。",
            ] if zh else [
                "Planner must compare event scale, city population, slice sessions, throughput, latency, and UPF CPU before choosing actions.",
                "Executor must apply only bounded tools, then verify post-action metrics before declaring the loop stable.",
            ]),
            "selectedPlan": {
                "name": planner.get("strategy", "AgentxG Core plan"),
                "rationale": "Planner 依即時觀測、事件時間、事件規模與切片 SLA 目標選擇此順序。" if zh else "Planner selected this sequence from live observations, event duration, event scale, and slice SLA targets.",
                "expectedImpact": "事件執行層負責產生 UE／session 流量，free5GC 調度則準備目標切片。" if zh else "The target slice is prepared by free5GC orchestration while event runtime owns UE/session traffic generation.",
                "status": executor.get("status", "unknown"),
            },
            "rejectedPlans": ([
                {"name": "直接進行封包層級排程", "reason": "高速資料路徑仍由 UPF、SMF、PCF 與 Kubernetes 控制器負責。"},
                {"name": "無上限的自主重試", "reason": "為確保網路操作安全，調適次數必須設有上限。"},
            ] if zh else [
                {"name": "Direct packet-level scheduling", "reason": "Fast path control remains with UPF, SMF, PCF, and Kubernetes controllers."},
                {"name": "Unbounded autonomous retries", "reason": "Adaptation must be bounded for safe network operations."},
            ]),
            "actions": actions,
            "verification": verification_checks,
            "verificationSummary": verification,
            "dataPlaneActuation": {
                "status": actuator_result.get("actuatorStatus", "not-requested"),
                **data_plane_evidence,
            },
            "expectedOutcome": "針對選定的 5GC 切片，完成意圖驅動、工具式的閉迴路調度。" if zh else "Intent-driven, tool-based, closed-loop orchestration for the selected 5GC slice.",
            "startedAt": TimeUtils.now(),
            "completedAt": TimeUtils.now(),
        }
