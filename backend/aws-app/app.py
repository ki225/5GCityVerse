from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from agent_runtime.loop import AgentxGCoreLoop
from config import EVENT_CONFIG, AppSettings
from constants import ApiErrorCode, ApiRoute, DEFAULT_CORS_HEADERS, DynamoKeys, EvidenceLevel, SliceStrategy, WsMessageType
from decision_service import AgentDecisionService
from dynamodb_codec import DynamoDbCodec
from event_repository import EventRepository
from eks_kubernetes_client import EksKubernetesClient, get_eks_client
from free5gc_utils import Free5gcClient
from metrics_service import PrometheusMetricsService
from models import TriggerRequest, ValidationError
from scenario_environment import ScenarioEnvironmentService
from scale_model import expected_profile, target_mbps_for_ratio
from slice_catalog import SliceCatalog
from time_utils import TimeUtils
from websocket_service import WebSocketConnectionService


class CityVerseBackendApp:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()
        self.table = None
        self.websocket = None
        self.events = None
        if self.settings.dynamodb_table:
            dynamodb = boto3.resource("dynamodb")
            self.table = dynamodb.Table(self.settings.dynamodb_table)
            self.events = EventRepository(self.table)
            if self.settings.apigw_ws_endpoint:
                self.websocket = WebSocketConnectionService(
                    self.table,
                    boto3.client("apigatewaymanagementapi", endpoint_url=self.settings.apigw_ws_endpoint),
                )
        self.metrics = PrometheusMetricsService(self.settings.prometheus_url)
        self._free5gc_status_cache: dict[str, Any] | None = None
        self._free5gc_status_cache_at = 0.0
        self._free5gc_status_cache_ttl = float(os.environ.get("FREE5GC_STATUS_CACHE_TTL_SECONDS", "10"))
        self._metrics_cache: dict[str, Any] | None = None
        self._metrics_cache_at = 0.0
        self._scaling_state_cache: dict[str, Any] | None = None
        self._scaling_state_cache_at = 0.0
        # Must be smaller than tool_gateway's 5s SLA polling interval, otherwise
        # SLA re-checks can read stale cached metrics from before an adaptation.
        self._metrics_cache_ttl = float(os.environ.get("METRICS_CACHE_TTL_SECONDS", "3"))
        self._control_plane_log_window_seconds = int(os.environ.get("CONTROL_PLANE_LOG_WINDOW_SECONDS", "20"))
        self._scenario_traffic_log_window_seconds = int(os.environ.get("SCENARIO_TRAFFIC_LOG_WINDOW_SECONDS", "20"))
        self._gtp5g_metrics_max_age_seconds = int(os.environ.get("GTP5G_METRICS_MAX_AGE_SECONDS", "20"))
        self._baseline_reconcile_at = 0.0
        self.free5gc = Free5gcClient(
            self.settings.free5gc_webui_url,
            self.settings.free5gc_webui_username,
            self.settings.free5gc_webui_password,
            self.settings.free5gc_plmn_id,
            self.metrics,
        )
        self.environment = ScenarioEnvironmentService(
            self.settings.eks_cluster_name,
            self.settings.free5gc_namespace,
            self.settings.ueransim_ue_deployment,
            self.settings.ueransim_image,
            self.settings.iperf3_image,
        )
        self.decisions = AgentDecisionService()
        self.agentxg = AgentxGCoreLoop(
            self.metrics,
            self.free5gc,
            self.environment,
            self.current_metrics,
            self.current_slices,
            self.settings.runtime_subscriber_upsert_limit,
            {
                "activate_qos_policy": self.settings.nef_qos_lambda_name,
                "request_traffic_influence": self.settings.nef_traffic_influence_lambda_name,
                "create_pfd_rule": self.settings.nef_pfd_lambda_name,
                "patch_hpa": self.settings.hpa_update_lambda_name,
            },
            invalidate_metrics=self.invalidate_metrics_cache,
            record_nef_hit=self.events.record_nef_tool_hit if self.events else None,
        )

    def handle(self, event: dict[str, Any], _context: Any) -> dict[str, Any]:
        route_key = event.get("requestContext", {}).get("routeKey", "")
        connection_id = event.get("requestContext", {}).get("connectionId")
        if "actionGroup" in event and "apiPath" in event:
            return self.handle_agent_tool(event)
        if event.get("_cityverseInternalAction") == "reset":
            return self.handle_reset_worker(event)
        if connection_id and route_key in {ApiRoute.WS_CONNECT.value, ApiRoute.WS_DISCONNECT.value, ApiRoute.WS_DEFAULT.value}:
            return self.handle_ws(event, route_key)

        method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
        path = event.get("rawPath", event.get("path", "/"))

        try:
            if method == "OPTIONS":
                return self.response(204, {})
            if method == "POST" and (path.endswith("/events/trigger") or path.endswith("/api/scenario/trigger")):
                return self.handle_trigger(event, _context)
            if method == "POST" and path.endswith("/events/reset"):
                return self.handle_reset(event, _context)
            if method == "GET" and "/events/reset/" in path:
                return self.handle_reset_status(event, path.rsplit("/", 1)[-1])
            if method == "POST" and "/events/status/" in path and path.endswith("/traffic-rendered"):
                if not self.events:
                    return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured"})
                execution_id = path.rstrip("/").split("/")[-2]
                item = self.events.get_status(execution_id)
                if not item or (item.get("sessionId") and item.get("sessionId") != self.request_session_id(event)):
                    return self.response(404, {"error": ApiErrorCode.NOT_FOUND.value, "detail": "Execution not found in this browser session"})
                runtime_prime = item.get("runtimePrime") if isinstance(item.get("runtimePrime"), dict) else {}
                free5gc = item.get("free5gc") if isinstance(item.get("free5gc"), dict) else {}
                snapshot = free5gc.get("networkSnapshot") if isinstance(free5gc.get("networkSnapshot"), dict) else {}
                measured_edges = [
                    edge for edge in snapshot.get("edges", [])
                    if isinstance(edge, dict)
                    and edge.get("active")
                    and edge.get("plane") != "control"
                    and self.safe_float(edge.get("throughputMbps")) > 0
                ]
                if not runtime_prime.get("observedBeforePlanning") or not measured_edges:
                    return self.response(409, {"error": "TRAFFIC_NOT_READY", "detail": "Measured bearer/iperf edges are required before render acknowledgement"})
                rendered_at = TimeUtils.now()
                self.events.update_status(execution_id, {
                    "trafficRenderedAt": rendered_at,
                    "awaitingTrafficRenderAck": False,
                    "progressStage": "traffic_rendered",
                    "updated_at": rendered_at,
                })
                return self.response(200, {"executionId": execution_id, "trafficRenderedAt": rendered_at, "measuredEdgeCount": len(measured_edges)})
            if method == "GET" and "/events/status/" in path:
                if not self.events:
                    return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured"})
                execution_id = path.rsplit("/", 1)[-1]
                item = self.events.get_status(execution_id)
                if not item:
                    return self.response(404, {"error": ApiErrorCode.NOT_FOUND.value, "detail": "Execution not found"})
                if item.get("sessionId") and item.get("sessionId") != self.request_session_id(event):
                    return self.response(404, {"error": ApiErrorCode.NOT_FOUND.value, "detail": "Execution not found in this browser session"})
                return self.response(200, item)
            if method == "GET" and path.endswith("/free5gc/status"):
                return self.response(200, self.safe_free5gc_status())
            if method == "GET" and path.endswith("/network/slices"):
                busy = self.foreign_session_busy(event)
                if busy:
                    return self.response(409, busy)
                return self.response(200, self.current_slices())
            if method == "GET" and path.endswith("/metrics/current"):
                busy = self.foreign_session_busy(event)
                if busy:
                    return self.response(409, busy)
                return self.response(200, self.current_metrics())
            return self.response(404, {"error": ApiErrorCode.NOT_FOUND.value, "detail": f"Not found: {path}"})
        except Exception as exc:
            print(f"Unhandled backend error: {exc}")
            return self.response(500, {"error": ApiErrorCode.INTERNAL_ERROR.value, "detail": "Internal server error"})

    def handle_ws(self, event: dict[str, Any], route_key: str) -> dict[str, Any]:
        connection_id = event.get("requestContext", {}).get("connectionId", "")
        if route_key == ApiRoute.WS_CONNECT.value:
            if not self.websocket:
                return {"statusCode": 503}
            return self.websocket.connect(connection_id)
        if route_key == ApiRoute.WS_DISCONNECT.value:
            if not self.websocket:
                return {"statusCode": 503}
            return self.websocket.disconnect(connection_id)
        if not self.websocket:
            return {"statusCode": 503}
        return self.websocket.handle_default(connection_id, event)

    def handle_agent_tool(self, event: dict[str, Any]) -> dict[str, Any]:
        api_path = str(event.get("apiPath") or "/").strip("/")
        operation = api_path.replace("-", "_")
        if operation == "start_ueransim_profile":
            return self.bedrock_tool_response(
                event,
                403,
                {
                    "status": "failed",
                    "error": "start_ueransim_profile is event-runtime only; AI agents may orchestrate free5GC resources but must not generate real UE/session traffic.",
                },
            )
        params = self.bedrock_parameters(event)
        event_type = params.get("event_type") or params.get("eventType") or next(iter(EVENT_CONFIG))
        if event_type not in EVENT_CONFIG:
            return self.bedrock_tool_response(event, 400, {"status": "failed", "error": f"Unknown event_type: {event_type}"})
        execution_id = params.get("execution_id") or params.get("executionId") or f"tool-{uuid.uuid4()}"
        cfg, scenario_context = self.tool_config_for_execution(event_type, execution_id)
        intent = self.agentxg.intent_manager.build_intent(execution_id, event_type, cfg, scenario_context)
        result = self.agentxg.gateway.call(operation, params, cfg, intent)
        status_code = 200 if result.get("status") in {"success", "preseeded"} else 500
        return self.bedrock_tool_response(event, status_code, result)

    @staticmethod
    def bedrock_parameters(event: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for item in event.get("parameters") or []:
            name = item.get("name")
            if name:
                params[name] = item.get("value")
        content = ((event.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        for item in content.get("properties") or []:
            name = item.get("name")
            if name:
                params[name] = item.get("value")
        return params

    @staticmethod
    def bedrock_tool_response(event: dict[str, Any], status_code: int, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "messageVersion": event.get("messageVersion", "1.0"),
            "response": {
                "actionGroup": event.get("actionGroup", "5GCityVerseNetworkTools"),
                "apiPath": event.get("apiPath", "/"),
                "httpMethod": event.get("httpMethod", "GET"),
                "httpStatusCode": status_code,
                "responseBody": {
                    "application/json": {
                        "body": json.dumps(DynamoDbCodec.from_dynamodb(body)),
                    }
                },
            },
        }

    def handle_trigger(self, event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        try:
            return self._handle_trigger(event, context)
        except Exception as exc:
            try:
                body = json.loads(event.get("body") or "{}")
            except (json.JSONDecodeError, TypeError):
                body = {}
            if body.get("_async") and self.events:
                execution_id = str(body.get("execution_id") or "")
                session_id = self.request_session_id(event)
                if execution_id:
                    failed = {
                        "status": "SIMULATION_FAILED",
                        "progressStage": "execution_failed",
                        "sliceStrategy": str(body.get("slice_strategy") or SliceStrategy.NONE.value),
                        "error": "Simulation execution failed",
                        "detail": str(exc),
                        "completed_at": TimeUtils.now(),
                    }
                    self.events.update_status(execution_id, failed)
                    self.events.release_session_lease(session_id)
                    event_type = str(body.get("event_type") or "")
                    if self.settings.eks_cluster_name and event_type in EVENT_CONFIG:
                        try:
                            self.environment.cleanup_event_runtime(get_eks_client(self.settings.eks_cluster_name), event_type)
                        except Exception as cleanup_exc:
                            print(f"failed single-event cleanup skipped for {event_type}: {cleanup_exc}")
                    self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": execution_id, **failed}})
            raise

    def _handle_trigger(self, event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        session_id = self.request_session_id(event)
        try:
            body = json.loads(event.get("body") or "{}")
            locale = str(body.pop("locale", event.get("_locale", "en")) or "en")
            event["_locale"] = locale
            run_inline = bool(body.pop("_async", False))
            execution_id = str(body.pop("execution_id", "") or uuid.uuid4())
            inline_batch_scenarios = [str(item) for item in body.pop("batch_scenarios", []) if str(item) in EVENT_CONFIG]
            request = TriggerRequest(**body)
            if not run_inline and request.scenarios:
                return self.handle_batch_trigger(event, context, execution_id, request)
            if run_inline and request.scenarios:
                return self.handle_batch_execution(event, context, execution_id, request, locale)
            event_type = request.event_type.value if hasattr(request.event_type, "value") else request.event_type
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            return self.response(400, {"error": ApiErrorCode.INVALID_REQUEST.value, "detail": f"Invalid trigger request: {exc}"})

        if not event_type:
            return self.response(400, {"error": ApiErrorCode.INVALID_REQUEST.value, "detail": "event_type is required when scenarios is not provided"})
        if event_type not in EVENT_CONFIG:
            return self.response(400, {"error": ApiErrorCode.INVALID_REQUEST.value, "detail": f"Unknown event_type: {event_type}"})

        cfg, scenario_context = self.event_config_for_request(event_type, request.event_scale, request.city_residents)
        slice_strategy = self.slice_strategy_value(request.slice_strategy)
        scenario_context["sessionId"] = session_id
        scenario_context["locale"] = locale
        scenario_context["sliceStrategy"] = slice_strategy
        if run_inline and inline_batch_scenarios:
            scenario_context["batchScenarios"] = inline_batch_scenarios

        if not run_inline:
            if not self.events:
                return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured; async event queue is unavailable"})
            if not self.events.acquire_session_lease(session_id):
                return self.response(409, {"error": "SESSION_BUSY", "detail": "Another browser session is currently using the shared free5GC runtime. Wait for it to finish before starting a simulation."})
            started_epoch_millis = TimeUtils.epoch_millis()
            queued_item = {
                "pk": f"EVENT#{execution_id}",
                "sk": DynamoKeys.STATUS.value,
                "executionId": execution_id,
                "sessionId": session_id,
                "eventType": event_type,
                "status": "SIMULATION_QUEUED",
                "sliceStrategy": slice_strategy,
                "config": cfg.to_dict(),
                "scenarioContext": scenario_context,
                "started_at": TimeUtils.now(),
                "startedEpochMillis": started_epoch_millis,
            }
            self.events.put_status(queued_item)
            self.broadcast({"type": WsMessageType.EVENT_STARTED.value, "payload": {"executionId": execution_id, "eventType": event_type, "status": "SIMULATION_QUEUED", "sliceStrategy": slice_strategy}})
            try:
                self.invoke_trigger_async(event, context, execution_id, event_type, scenario_context)
            except Exception as exc:
                blocked = {
                    "status": "SIMULATION_FAILED",
                    "error": "Failed to enqueue async event execution",
                    "detail": str(exc),
                    "completed_at": TimeUtils.now(),
                }
                self.events.update_status(execution_id, blocked)
                self.events.release_session_lease(session_id)
                self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": execution_id, "eventType": event_type, **blocked}})
                return self.response(503, {"error": ApiErrorCode.EVENT_BLOCKED.value, "detail": {"executionId": execution_id, "eventType": event_type, **blocked}})
            return self.response(200, {"executionId": execution_id, "eventType": event_type, "status": "SIMULATION_QUEUED", "sliceStrategy": slice_strategy})

        if self.events:
            if self.event_cancelled_by_reset(execution_id):
                cancelled = {
                    "status": "SIMULATION_CANCELLED",
                    "reason": "Scenario was reset before this queued execution started.",
                    "completed_at": TimeUtils.now(),
                }
                self.events.update_status(execution_id, cancelled)
                self.events.release_session_lease(session_id)
                self.broadcast({"type": WsMessageType.EVENT_RESET.value, "payload": {"executionId": execution_id, "eventType": event_type, **cancelled}})
                return self.response(409, {"error": ApiErrorCode.EVENT_CANCELLED.value, "detail": {"executionId": execution_id, "eventType": event_type, **cancelled}})
            self.events.update_status(execution_id, {"status": "SIMULATION_RUNNING", "sliceStrategy": slice_strategy, "updated_at": TimeUtils.now()})
            self.broadcast({"type": WsMessageType.EVENT_STARTED.value, "payload": {"executionId": execution_id, "eventType": event_type, "status": "SIMULATION_RUNNING", "sliceStrategy": slice_strategy}})

        runtime_prime = self.prime_runtime_before_planning(execution_id, event_type, cfg, scenario_context, context)
        scenario_context["runtimePrimed"] = runtime_prime.get("status") == "success"
        scenario_context["runtimePrime"] = runtime_prime
        if runtime_prime.get("status") != "success" or not runtime_prime.get("observedBeforePlanning"):
            blocked = {
                "status": "SIMULATION_BLOCKED",
                "error": "Scenario traffic was not observed before planning",
                "detail": (
                    "Agent planning is blocked until UERANSIM/iperf3 runtime metrics show the triggered scenario. "
                    f"Expected={runtime_prime.get('expectedScenarios', [event_type])}; "
                    f"observed={runtime_prime.get('observedScenarios', [])}; "
                    f"missing={runtime_prime.get('missingScenarios', [])}."
                ),
                "runtimePrime": runtime_prime,
                "progressStage": "traffic_not_observed",
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, blocked)
                self.events.release_session_lease(session_id)
            self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": execution_id, "eventType": event_type, **blocked}})
            return self.response(503, {"error": ApiErrorCode.EVENT_BLOCKED.value, "detail": {"executionId": execution_id, "eventType": event_type, **blocked}})

        observed_metrics = self.current_metrics()
        observed_slices = self.current_slices()
        baseline_throughput = self.baseline_scenario_throughput(observed_metrics)
        if baseline_throughput is not None:
            scenario_context["baselineThroughputMbps"] = baseline_throughput
        free5gc_status = self.free5gc_status_for_trigger()
        if not free5gc_status.get("connected") and not self.can_continue_with_degraded_webui(free5gc_status):
            message = free5gc_status.get("error") or "free5GC is offline"
            blocked = {
                "status": "SIMULATION_BLOCKED",
                "error": "free5GC is offline; event trigger blocked",
                "detail": message,
                "free5gc": free5gc_status,
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, blocked)
                self.events.release_session_lease(session_id)
            self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": free5gc_status})
            self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": execution_id, "eventType": event_type, **blocked}})
            return self.response(
                503,
                {
                    "error": ApiErrorCode.EVENT_BLOCKED.value,
                    "detail": {
                        "executionId": execution_id,
                        "eventType": event_type,
                        **blocked,
                    },
                },
            )

        if slice_strategy != SliceStrategy.AI.value:
            applied_policy = self.non_ai_policy_evidence(slice_strategy)
            if self.events:
                self.events.update_status(execution_id, {
                    "status": "SIMULATION_RUNNING",
                    "progressStage": "traffic_observation",
                    "sliceStrategy": slice_strategy,
                    "appliedPolicy": applied_policy,
                    "scenarioContext": scenario_context,
                    "free5gc": free5gc_status,
                    "updated_at": TimeUtils.now(),
                })
            self.monitor_event_window(execution_id, event_type, cfg, scenario_context, context)
            if self.settings.eks_cluster_name:
                try:
                    self.environment.cleanup_event_runtime(get_eks_client(self.settings.eks_cluster_name), event_type)
                except Exception as exc:
                    print(f"non-AI event runtime cleanup skipped for {event_type}: {exc}")
            completed = {
                "status": "SIMULATION_COMPLETE",
                "progressStage": "complete",
                "sliceStrategy": slice_strategy,
                "appliedPolicy": applied_policy,
                "scenarioContext": scenario_context,
                "free5gc": free5gc_status,
                "environment": runtime_prime,
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, completed)
                self.events.release_session_lease(session_id)
            return self.response(200, {"executionId": execution_id, "eventType": event_type, **completed})

        agentxg_result = self.run_agentxg_loop(execution_id, event_type, cfg, observed_metrics, observed_slices, scenario_context)
        if self.event_cancelled_by_reset(execution_id):
            if self.settings.eks_cluster_name:
                try:
                    self.environment.cleanup_event_runtime(get_eks_client(self.settings.eks_cluster_name), event_type)
                except Exception as exc:
                    print(f"cancelled event runtime cleanup skipped for {event_type}: {exc}")
            cancelled = {
                "status": "SIMULATION_CANCELLED",
                "reason": "Scenario was reset during execution.",
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, cancelled)
                self.events.release_session_lease(session_id)
            self.broadcast({"type": WsMessageType.EVENT_RESET.value, "payload": {"executionId": execution_id, "eventType": event_type, **cancelled}})
            return self.response(409, {"error": ApiErrorCode.EVENT_CANCELLED.value, "detail": {"executionId": execution_id, "eventType": event_type, **cancelled}})
        decision = agentxg_result["agentDecision"]
        if self.events:
            self.events.update_status(
                execution_id,
                {
                    "status": "SIMULATION_RUNNING",
                    "progressStage": "decision_ready",
                    "scenarioContext": scenario_context,
                    "intent": agentxg_result.get("intent", {}),
                    "baseline": agentxg_result.get("baseline", {}),
                    "planner": agentxg_result.get("planner", {}),
                    "executor": agentxg_result.get("executor", {}),
                    "verification": agentxg_result.get("verification", {}),
                    "adaptation": agentxg_result.get("adaptation", {}),
                    "validationReport": agentxg_result.get("validationReport", {}),
                    "agentDecision": decision,
                    "free5gc": agentxg_result.get("free5gc", {}),
                    "updated_at": TimeUtils.now(),
                },
            )
        self.broadcast({"type": WsMessageType.AGENT_DECISION.value, "payload": {"executionId": execution_id, **decision}})
        self.monitor_event_window(execution_id, event_type, cfg, scenario_context, context)
        if self.settings.eks_cluster_name:
            try:
                self.environment.cleanup_event_runtime(get_eks_client(self.settings.eks_cluster_name), event_type)
            except Exception as exc:
                print(f"event runtime cleanup skipped for {event_type}: {exc}")
        if self.event_cancelled_by_reset(execution_id):
            cancelled = {
                "status": "SIMULATION_CANCELLED",
                "reason": "Scenario was reset during execution.",
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, cancelled)
                self.events.release_session_lease(session_id)
            self.broadcast({"type": WsMessageType.EVENT_RESET.value, "payload": {"executionId": execution_id, "eventType": event_type, **cancelled}})
            return self.response(409, {"error": ApiErrorCode.EVENT_CANCELLED.value, "detail": {"executionId": execution_id, "eventType": event_type, **cancelled}})
        free5gc_result = agentxg_result.get("free5gc", {})
        environment_result = agentxg_result.get("environment", {})
        verification_status = (agentxg_result.get("verification") or {}).get("status")
        execution_status = "SIMULATION_COMPLETE" if agentxg_result.get("status") == "success" else "SIMULATION_DEGRADED"
        if verification_status in {"failed", "degraded"}:
            execution_status = "SIMULATION_DEGRADED"
        item = {
            "pk": f"EVENT#{execution_id}",
            "sk": DynamoKeys.STATUS.value,
            "executionId": execution_id,
            "eventType": event_type,
            "status": execution_status,
            "sliceStrategy": slice_strategy,
            "config": cfg.to_dict(),
            "scenarioContext": scenario_context,
            "intent": agentxg_result.get("intent", {}),
            "baseline": agentxg_result.get("baseline", {}),
            "planner": agentxg_result.get("planner", {}),
            "executor": agentxg_result.get("executor", {}),
            "verification": agentxg_result.get("verification", {}),
            "adaptation": agentxg_result.get("adaptation", {}),
            "validationReport": agentxg_result.get("validationReport", {}),
            "agentDecision": decision,
            "started_at": TimeUtils.now(),
            "startedEpochMillis": scenario_context.get("startedEpochMillis") or TimeUtils.epoch_millis(),
            "mcp": {"terraform_mcp_used_for_iac": True, "free5gc_runtime_mode": "agentxg-tool-gateway"},
            "free5gc": free5gc_result,
            "environment": environment_result,
        }
        if self.events:
            self.events.put_status(item)
            session_id = str(scenario_context.get("sessionId") or "legacy-session")
            if not self.events.has_active_events(session_id):
                self.events.release_session_lease(session_id)

        post_action_free5gc_status = self.free5gc_status()
        self.broadcast({"type": WsMessageType.EVENT_STARTED.value, "payload": {"executionId": execution_id, "eventType": event_type}})
        self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": post_action_free5gc_status})
        return self.response(200, {"executionId": execution_id, "eventType": event_type, "environment": environment_result})

    def handle_batch_trigger(self, event: dict[str, Any], context: Any, batch_execution_id: str, request: TriggerRequest) -> dict[str, Any]:
        if not request.scenarios:
            return self.response(400, {"error": ApiErrorCode.INVALID_REQUEST.value, "detail": "scenarios must contain at least one event"})
        if not self.events:
            return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured; async event queue is unavailable"})
        session_id = self.request_session_id(event)
        slice_strategy = self.slice_strategy_value(request.slice_strategy)
        if not self.events.acquire_session_lease(session_id):
            return self.response(409, {"error": "SESSION_BUSY", "detail": "Another browser session is currently using the shared free5GC runtime. Wait for it to finish before starting a simulation."})

        queued: list[dict[str, Any]] = []
        batch_scenarios: list[str] = []
        for item in request.scenarios:
            raw_event_type = item.get("event_type") if isinstance(item, dict) else item.event_type
            raw_event_scale = item.get("event_scale", 0) if isinstance(item, dict) else item.event_scale
            event_type = raw_event_type.value if hasattr(raw_event_type, "value") else raw_event_type
            if event_type not in EVENT_CONFIG:
                self.events.release_session_lease(session_id)
                return self.response(400, {"error": ApiErrorCode.INVALID_REQUEST.value, "detail": f"Unknown event_type: {event_type}"})
            batch_scenarios.append(str(event_type))
            cfg, scenario_context = self.event_config_for_request(event_type, int(raw_event_scale or 0), request.city_residents)
            queued.append(
                {
                    "executionId": batch_execution_id,
                    "eventType": event_type,
                    "eventScale": scenario_context.get("eventScale"),
                    "eventDurationSeconds": scenario_context.get("eventDurationSeconds"),
                }
            )

        round_context = {
            "sessionId": session_id,
            "locale": str(event.get("_locale") or "en"),
            "batchExecutionId": batch_execution_id,
            "batchScenarios": batch_scenarios,
            "cityResidents": request.city_residents,
            "sliceStrategy": slice_strategy,
        }
        self.events.put_status({
            "pk": f"EVENT#{batch_execution_id}", "sk": DynamoKeys.STATUS.value,
            "executionId": batch_execution_id, "sessionId": session_id,
            "batchExecutionId": batch_execution_id, "eventType": "network_round",
            "status": "SIMULATION_QUEUED", "sliceStrategy": slice_strategy, "scenarioContext": round_context,
            "started_at": TimeUtils.now(), "startedEpochMillis": TimeUtils.epoch_millis(),
        })
        self.broadcast({"type": WsMessageType.EVENT_STARTED.value, "payload": {"executionId": batch_execution_id, "eventType": "network_round", "status": "SIMULATION_QUEUED", "sliceStrategy": slice_strategy, "batchExecutionId": batch_execution_id}})
        try:
            self.invoke_batch_async(event, context, batch_execution_id, request, round_context)
        except Exception as exc:
            failed = {
                "status": "SIMULATION_FAILED",
                "progressStage": "enqueue_failed",
                "sliceStrategy": slice_strategy,
                "error": "Failed to enqueue async simulation",
                "detail": str(exc),
                "completed_at": TimeUtils.now(),
            }
            self.events.update_status(batch_execution_id, failed)
            self.events.release_session_lease(session_id)
            self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": batch_execution_id, "eventType": "network_round", **failed}})
            return self.response(503, {"error": ApiErrorCode.EVENT_BLOCKED.value, "detail": {"executionId": batch_execution_id, **failed}})

        return self.response(
            200,
            {
                "executionId": batch_execution_id,
                "executionIds": [batch_execution_id],
                "events": queued,
                "status": "SIMULATION_QUEUED",
                "sliceStrategy": slice_strategy,
            },
        )

    def invoke_batch_async(self, event: dict[str, Any], context: Any, execution_id: str, request: TriggerRequest, round_context: dict[str, Any]) -> None:
        payload = dict(event)
        payload["body"] = json.dumps({
            "scenarios": [item.to_dict() if hasattr(item, "to_dict") else item for item in request.scenarios],
            "city_residents": request.city_residents,
            "slice_strategy": self.slice_strategy_value(request.slice_strategy),
            "_async": True,
            "execution_id": execution_id,
            "locale": round_context.get("locale") or "en",
        })
        function_name = getattr(context, "invoked_function_arn", "") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        if not function_name:
            raise RuntimeError("Cannot enqueue batch: Lambda function name is unavailable")
        boto3.client("lambda").invoke(FunctionName=function_name, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))

    def handle_batch_execution(self, event: dict[str, Any], context: Any, execution_id: str, request: TriggerRequest, locale: str) -> dict[str, Any]:
        session_id = self.request_session_id(event)
        slice_strategy = self.slice_strategy_value(request.slice_strategy)
        try:
            return self._handle_batch_execution(event, context, execution_id, request, locale)
        except Exception as exc:
            failed = {
                "status": "SIMULATION_FAILED",
                "progressStage": "execution_failed",
                "sliceStrategy": slice_strategy,
                "error": "Simulation execution failed",
                "detail": str(exc),
                "completed_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, failed)
                self.events.release_session_lease(session_id)
            self.cleanup_batch_runtime(request)
            self.invalidate_metrics_cache()
            self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"executionId": execution_id, "eventType": "network_round", **failed}})
            return self.response(500, failed)

    def _handle_batch_execution(self, event: dict[str, Any], context: Any, execution_id: str, request: TriggerRequest, locale: str) -> dict[str, Any]:
        slice_strategy = self.slice_strategy_value(request.slice_strategy)
        cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "before runtime priming")
        if cancelled:
            return cancelled
        scenario_runs: list[tuple[str, Any, dict[str, Any]]] = []
        for item in request.scenarios:
            raw_type = item.get("event_type") if isinstance(item, dict) else item.event_type
            event_type = raw_type.value if hasattr(raw_type, "value") else str(raw_type)
            raw_scale = item.get("event_scale", 0) if isinstance(item, dict) else item.event_scale
            cfg, scenario_context = self.event_config_for_request(event_type, int(raw_scale or 0), request.city_residents)
            scenario_runs.append((event_type, cfg, scenario_context))
        expected = [item[0] for item in scenario_runs]
        self.events.update_status(execution_id, {"status": "SIMULATION_RUNNING", "sliceStrategy": slice_strategy, "progressStage": "runtime_priming", "updated_at": TimeUtils.now()})
        minimum_mbps = {
            event_type: self.throughput_from_profile(cfg.traffic_profile) * 0.75
            for event_type, cfg, _ in scenario_runs
        }
        # Register representative UEs one at a time on the shared gNB.  Each
        # established UE keeps sending traffic while the next one registers,
        # so the final observer window still proves simultaneous multi-slice
        # traffic without nondeterministic NGAP/PDU registration collisions.
        environment_results: dict[str, Any] = {}
        staged_observed: set[str] = set()
        for event_type, cfg, _ in scenario_runs:
            result = self.environment.trigger(event_type, cfg, execution_id, wait_for_bearer=False)
            environment_results[event_type] = result
            if result.get("status") == "success":
                staged_observed.update(self.wait_for_runtime_scenarios(
                    [event_type],
                    context,
                    timeout_seconds=45,
                    minimum_mbps={event_type: minimum_mbps[event_type]},
                ))
        cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "during runtime priming")
        if cancelled:
            return cancelled
        failed = [name for name, result in environment_results.items() if result.get("status") != "success"]
        observed = sorted(staged_observed | set(self.wait_for_runtime_scenarios(
            expected, context, timeout_seconds=60, minimum_mbps=minimum_mbps,
        )))
        missing = sorted(set(expected) - set(observed))
        recovery_attempts: dict[str, Any] = {}
        if not failed and missing and self.remaining_lambda_millis(context) >= 180_000:
            # Concurrent multi-slice registration can leave one representative
            # UE without a PDU context even though the other slices are healthy.
            # Roll only the missing scenario deployment, then preserve the
            # evidence already collected for successful scenarios.
            scenario_by_type = {event_type: cfg for event_type, cfg, _ in scenario_runs}
            k8s = get_eks_client(self.settings.eks_cluster_name)
            for event_type in missing:
                # Re-applying an identical Deployment does not restart a UE
                # that failed registration.  Remove the missing scenario's
                # runtime first so this bounded recovery is a real rollout.
                self.environment.cleanup_event_runtime(k8s, event_type)
                retry_result = self.environment.trigger(
                    event_type,
                    scenario_by_type[event_type],
                    execution_id,
                    wait_for_bearer=False,
                )
                recovery_attempts[event_type] = retry_result
                environment_results[event_type]["recoveryAttempt"] = retry_result
                if retry_result.get("status") != "success":
                    failed.append(event_type)
            retry_observed = self.wait_for_runtime_scenarios(
                missing,
                context,
                timeout_seconds=45,
                minimum_mbps={name: minimum_mbps[name] for name in missing},
            )
            observed = sorted(set(observed) | set(retry_observed))
            missing = sorted(set(expected) - set(observed))
            cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "during runtime recovery")
            if cancelled:
                return cancelled
        if not failed and missing and self.remaining_lambda_millis(context) >= 180_000:
            # One additional bounded rollout handles a second transient NGAP /
            # PDU registration collision.  Success still requires a measured
            # TUN-bound iperf sample; Deployment readiness is not sufficient.
            scenario_by_type = {event_type: cfg for event_type, cfg, _ in scenario_runs}
            k8s = get_eks_client(self.settings.eks_cluster_name)
            for event_type in missing:
                self.environment.cleanup_event_runtime(k8s, event_type)
                retry_result = self.environment.trigger(
                    event_type,
                    scenario_by_type[event_type],
                    execution_id,
                    wait_for_bearer=False,
                )
                first_result = recovery_attempts.get(event_type)
                recovery_attempts[event_type] = {
                    "status": retry_result.get("status"),
                    "attempts": [first_result, retry_result],
                }
                environment_results[event_type]["recoveryAttempt"] = recovery_attempts[event_type]
                if retry_result.get("status") != "success":
                    failed.append(event_type)
            retry_observed = self.wait_for_runtime_scenarios(
                missing,
                context,
                timeout_seconds=45,
                minimum_mbps={name: minimum_mbps[name] for name in missing},
            )
            observed = sorted(set(observed) | set(retry_observed))
            missing = sorted(set(expected) - set(observed))
            cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "during second runtime recovery")
            if cancelled:
                return cancelled
        event_duration_seconds = max(
            int(item[2].get("eventDurationSeconds") or 180)
            for item in scenario_runs
        )
        primed_epoch_millis = TimeUtils.epoch_millis()
        runtime_prime = {
            "status": "success" if not failed and not missing else "traffic_not_observed",
            "sliceStrategy": slice_strategy,
            "observedBeforePlanning": not failed and not missing,
            "expectedScenarios": expected, "observedScenarios": observed, "missingScenarios": missing,
            "environmentResults": environment_results, "primedAt": TimeUtils.now(),
            "recoveryAttempts": recovery_attempts,
            "primedEpochMillis": primed_epoch_millis,
            "trafficStartedEpochMillis": primed_epoch_millis,
            "trafficEndsEpochMillis": primed_epoch_millis + event_duration_seconds * 1000,
        }
        # Persist the pre-plan runtime sample for polling clients. The single-
        # scenario path already does this; the batch path previously emitted a
        # one-shot WebSocket snapshot only, so a missed/disconnected socket did
        # not show traffic until the final agent result stored `free5gc`.
        if not failed and not missing:
            self._free5gc_status_cache = None
            self._free5gc_status_cache_at = 0.0
            self.invalidate_metrics_cache()
        primed_free5gc_status = self.free5gc_status() if not failed and not missing else None
        primed_update = {
            "runtimePrime": runtime_prime,
            "sliceStrategy": slice_strategy,
            "progressStage": "traffic_observed",
            "awaitingTrafficRenderAck": bool(primed_free5gc_status),
            "updated_at": TimeUtils.now(),
        }
        if primed_free5gc_status:
            primed_update["free5gc"] = primed_free5gc_status
        self.events.update_status(execution_id, primed_update)
        self.broadcast({"type": WsMessageType.RUNTIME_PRIMED.value, "payload": {"executionId": execution_id, "eventType": "network_round", "awaitingTrafficRenderAck": bool(primed_free5gc_status), **runtime_prime}})
        if primed_free5gc_status:
            self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": primed_free5gc_status})
        if failed or missing:
            blocked = {"status": "SIMULATION_BLOCKED", "sliceStrategy": slice_strategy, "error": "The complete traffic round was not observed", "runtimePrime": runtime_prime, "completed_at": TimeUtils.now()}
            self.events.update_status(execution_id, blocked)
            if self.settings.eks_cluster_name:
                k8s = get_eks_client(self.settings.eks_cluster_name)
                for event_type, _, _ in scenario_runs:
                    try:
                        self.environment.cleanup_event_runtime(k8s, event_type)
                    except Exception as exc:
                        print(f"blocked batch cleanup skipped for {event_type}: {exc}")
                try:
                    self.environment.recycle_session_state(k8s)
                except Exception as exc:
                    print(f"blocked batch session recycle skipped: {exc}")
            self.invalidate_metrics_cache()
            self.events.release_session_lease(self.request_session_id(event))
            return self.response(503, blocked)

        metrics = primed_free5gc_status.get("metrics", {}) if primed_free5gc_status else self.current_metrics()
        slices = primed_free5gc_status.get("slices", []) if primed_free5gc_status else self.current_slices()
        # The traffic sample that unlocks planning must also reach the browser.
        # Previously it was consumed only by the planner and the first visible
        # snapshot arrived after the scenario runtime had already been removed.
        self.broadcast({"type": WsMessageType.METRICS_UPDATE.value, "payload": metrics})
        self.broadcast({"type": WsMessageType.SLICE_UPDATE.value, "payload": slices})
        self.broadcast({"type": WsMessageType.NETWORK_SNAPSHOT.value, "payload": self.network_snapshot(metrics, slices)})
        if not self.wait_for_traffic_render_ack(execution_id, context):
            cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "while awaiting traffic rendering")
            if cancelled:
                return cancelled
            blocked = {
                "status": "SIMULATION_BLOCKED",
                "sliceStrategy": slice_strategy,
                "progressStage": "traffic_render_not_acknowledged",
                "error": "Measured traffic was not rendered before the simulation continued",
                "runtimePrime": runtime_prime,
                "completed_at": TimeUtils.now(),
            }
            self.events.update_status(execution_id, blocked)
            if self.settings.eks_cluster_name:
                k8s = get_eks_client(self.settings.eks_cluster_name)
                for event_type, _, _ in scenario_runs:
                    try:
                        self.environment.cleanup_event_runtime(k8s, event_type)
                    except Exception as exc:
                        print(f"render gate cleanup skipped for {event_type}: {exc}")
            self.invalidate_metrics_cache()
            self.events.release_session_lease(self.request_session_id(event))
            return self.response(503, blocked)
        slice_load = {int(item.get("sst") or 0): float(item.get("load") or 0) for item in slices}
        dominant_type, dominant_cfg, _ = max(scenario_runs, key=lambda item: slice_load.get(int(item[1].slice_sst), 0.0))
        round_context = {
            "sessionId": self.request_session_id(event), "locale": locale,
            "batchExecutionId": execution_id, "batchScenarios": expected,
            "networkRound": True, "dominantPressureScenario": dominant_type,
            "observedSliceLoads": slice_load, "cityResidents": request.city_residents,
            "eventScale": sum(int(item[2].get("eventScale") or 0) for item in scenario_runs),
            "runtimePrime": runtime_prime,
            "eventDurationSeconds": event_duration_seconds,
            "sliceStrategy": slice_strategy,
        }
        if slice_strategy != SliceStrategy.AI.value:
            applied_policy = self.non_ai_policy_evidence(slice_strategy)
            self.events.update_status(execution_id, {
                "status": "SIMULATION_RUNNING",
                "progressStage": "traffic_observation",
                "sliceStrategy": slice_strategy,
                "scenarioContext": round_context,
                "appliedPolicy": applied_policy,
                "updated_at": TimeUtils.now(),
            })
            self.monitor_event_window(execution_id, "network_round", dominant_cfg, round_context, context)
            completed = {
                "status": "SIMULATION_COMPLETE",
                "eventType": "network_round",
                "progressStage": "complete",
                "sliceStrategy": slice_strategy,
                "scenarioContext": round_context,
                "appliedPolicy": applied_policy,
                "free5gc": primed_free5gc_status or {},
                "environment": runtime_prime,
                "completed_at": TimeUtils.now(),
            }
            self.events.update_status(execution_id, completed)
            self.cleanup_batch_runtime(request)
            try:
                if self.settings.eks_cluster_name:
                    self.environment.recycle_session_state(get_eks_client(self.settings.eks_cluster_name))
            except Exception as exc:
                print(f"non-AI batch session recycle skipped: {exc}")
            self.invalidate_metrics_cache()
            self.events.release_session_lease(round_context["sessionId"])
            self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": self.free5gc_status()})
            return self.response(200, {"executionId": execution_id, **completed})

        self.events.update_status(execution_id, {"progressStage": "planning", "sliceStrategy": slice_strategy, "updated_at": TimeUtils.now()})
        result = self.run_agentxg_loop(execution_id, "network_round", dominant_cfg, metrics, slices, round_context)
        cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "during AI planning")
        if cancelled:
            return cancelled
        decision = result["agentDecision"]
        decision["networkRound"] = {"scenarios": expected, "dominantPressureScenario": dominant_type, "sliceLoads": slice_load}
        verification_status = (result.get("verification") or {}).get("status")
        final_status = "SIMULATION_COMPLETE" if verification_status not in {"failed", "degraded"} else "SIMULATION_DEGRADED"
        # Make the decision observable as soon as planning finishes. The event
        # traffic window remains active until trafficEndsEpochMillis; previously
        # the decision was withheld until after monitoring and cleanup, which
        # made the UI show an expired event with no AI result.
        decision_update = {
            "status": "SIMULATION_RUNNING", "eventType": "network_round", "sliceStrategy": slice_strategy, "scenarioContext": round_context,
            "progressStage": "decision_ready",
            "intent": result.get("intent", {}), "baseline": result.get("baseline", {}),
            "planner": result.get("planner", {}), "executor": result.get("executor", {}),
            "verification": result.get("verification", {}), "adaptation": result.get("adaptation", {}),
            "validationReport": result.get("validationReport", {}), "agentDecision": decision,
            "free5gc": result.get("free5gc", {}), "environment": runtime_prime, "updated_at": TimeUtils.now(),
        }
        self.events.update_status(execution_id, decision_update)
        self.broadcast({"type": WsMessageType.AGENT_DECISION.value, "payload": {"executionId": execution_id, **decision}})
        self.monitor_event_window(execution_id, "network_round", dominant_cfg, round_context, context)
        cancelled = self.cancel_batch_execution_if_reset(execution_id, request, event, "during the traffic observation window")
        if cancelled:
            return cancelled
        self.events.update_status(execution_id, {
            "status": final_status, "eventType": "network_round", "sliceStrategy": slice_strategy, "scenarioContext": round_context,
            "intent": result.get("intent", {}), "baseline": result.get("baseline", {}),
            "planner": result.get("planner", {}), "executor": result.get("executor", {}),
            "verification": result.get("verification", {}), "adaptation": result.get("adaptation", {}),
            "validationReport": result.get("validationReport", {}), "agentDecision": decision,
            "free5gc": result.get("free5gc", {}), "environment": runtime_prime, "completed_at": TimeUtils.now(),
        })
        k8s = get_eks_client(self.settings.eks_cluster_name)
        for event_type, _, _ in scenario_runs:
            try:
                self.environment.cleanup_event_runtime(k8s, event_type)
            except Exception as exc:
                print(f"batch cleanup skipped for {event_type}: {exc}")
        try:
            self.environment.recycle_session_state(k8s)
        except Exception as exc:
            print(f"batch session recycle skipped: {exc}")
        self.invalidate_metrics_cache()
        self.events.release_session_lease(round_context["sessionId"])
        self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": self.free5gc_status()})
        return self.response(200, {"executionId": execution_id, "status": final_status})

    def prime_runtime_before_planning(
        self,
        execution_id: str,
        event_type: str,
        cfg: Any,
        scenario_context: dict[str, Any],
        context: Any = None,
    ) -> dict[str, Any]:
        if not self.settings.eks_cluster_name:
            return {"status": "skipped", "reason": "EKS_CLUSTER_NAME is not configured"}
        if self.event_cancelled_by_reset(execution_id):
            return {"status": "cancelled", "reason": "Scenario was reset before runtime priming"}
        started_at = TimeUtils.now()
        if self.events:
            self.events.update_status(
                execution_id,
                {
                    "progressStage": "runtime_priming",
                    "runtimePrimingStartedAt": started_at,
                    "updated_at": started_at,
                },
            )
        self.broadcast(
            {
                "type": WsMessageType.RUNTIME_PRIMING.value,
                "payload": {
                    "executionId": execution_id,
                    "eventType": event_type,
                    "status": "running",
                    "startedAt": started_at,
                },
            }
        )
        try:
            result = self.environment.trigger(event_type, cfg, execution_id)
        except Exception as exc:
            result = {"status": "error", "error": str(exc), "httpStatus": 500}
        if result.get("status") != "success":
            return {**result, "primedAt": TimeUtils.now(), "observedBeforePlanning": False}

        expected = [event_type]
        observed = self.wait_for_runtime_scenarios(
            expected,
            context,
            minimum_mbps={event_type: self.throughput_from_profile(cfg.traffic_profile) * 0.75},
        )
        missing = sorted(set(expected) - set(observed))
        observed_before_planning = not missing
        primed_epoch_millis = TimeUtils.epoch_millis()
        event_duration_seconds = int(scenario_context.get("eventDurationSeconds") or 180)
        primed = {
            **result,
            "status": "success" if observed_before_planning else "traffic_not_observed",
            "primedAt": TimeUtils.now(),
            "primedEpochMillis": primed_epoch_millis,
            "trafficStartedEpochMillis": primed_epoch_millis,
            "trafficEndsEpochMillis": primed_epoch_millis + event_duration_seconds * 1000,
            "observedBeforePlanning": observed_before_planning,
            "expectedScenarios": expected,
            "observedScenarios": observed,
            "missingScenarios": missing,
        }
        self._free5gc_status_cache = None
        self._free5gc_status_cache_at = 0.0
        self.invalidate_metrics_cache()
        primed_free5gc_status = self.free5gc_status()
        if self.events:
            self.events.update_status(
                execution_id,
                {
                    "progressStage": "runtime_primed",
                    "runtimePrime": primed,
                    "free5gc": primed_free5gc_status,
                    "updated_at": TimeUtils.now(),
                },
            )
        self.broadcast({"type": WsMessageType.RUNTIME_PRIMED.value, "payload": {"executionId": execution_id, "eventType": event_type, **primed}})
        self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": primed_free5gc_status})
        return primed

    def wait_for_runtime_scenarios(
        self,
        expected: list[str],
        context: Any = None,
        timeout_seconds: int = 60,
        minimum_mbps: dict[str, float] | None = None,
    ) -> list[str]:
        expected_set = set(expected)
        observed: set[str] = set()
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.remaining_lambda_millis(context) < 90_000:
                break
            # Planning only needs measured EKS runtime evidence here. Going through
            # current_metrics() first performs many Prometheus queries; when that
            # endpoint is unavailable those timeouts can consume the whole evidence
            # window even though iperf traffic is already present in pod logs.
            scaling_state = self.eks_scaling_state(include_runtime_logs=True, include_hpa=False)
            metrics = scaling_state.get("runtimeMetrics") if isinstance(scaling_state, dict) else {}
            metrics = metrics if isinstance(metrics, dict) else {}
            traffic = metrics.get("scenarioTraffic") if isinstance(metrics.get("scenarioTraffic"), list) else []
            samples = {
                str(item.get("scenario") or "")
                for item in traffic
                if isinstance(item, dict)
                and self.safe_float(item.get("throughputMbps"))
                >= float((minimum_mbps or {}).get(str(item.get("scenario") or ""), 0.0))
            }
            # A Running UERANSIM pod proves only that traffic was requested. The
            # planner may unlock solely from a measured TUN-bound iperf sample.
            # Evidence may rotate out of the short Kubernetes log tail while a
            # long-running iperf client starts its next interval. Preserve every
            # scenario measured during this observer window instead of letting a
            # later transient sample erase earlier proof.
            observed.update(samples & expected_set)
            if expected_set and expected_set.issubset(observed):
                self.broadcast({"type": WsMessageType.METRICS_UPDATE.value, "payload": metrics})
                runtime_slices = self.slices_from_runtime_metrics(SliceCatalog.default_slices(), metrics)
                self.broadcast({"type": WsMessageType.SLICE_UPDATE.value, "payload": runtime_slices})
                self.broadcast({"type": WsMessageType.NETWORK_SNAPSHOT.value, "payload": self.network_snapshot(metrics, runtime_slices)})
                return sorted(observed)
            time.sleep(2)
        return sorted(observed)

    def wait_for_traffic_render_ack(self, execution_id: str, context: Any = None, timeout_seconds: int = 60) -> bool:
        """Keep the AI planner locked until the browser painted measured traffic."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.remaining_lambda_millis(context) < 60_000:
                return False
            item = self.events.get_status(execution_id) if self.events else None
            if isinstance(item, dict) and item.get("trafficRenderedAt"):
                return True
            if self.event_cancelled_by_reset(execution_id):
                return False
            time.sleep(0.25)
        return False

    def monitor_event_window(
        self,
        execution_id: str,
        event_type: str,
        cfg: Any,
        scenario_context: dict[str, Any],
        context: Any = None,
    ) -> None:
        duration = int(scenario_context.get("eventDurationSeconds") or 0)
        poll_interval = 30
        if duration <= 0:
            return
        runtime_prime = scenario_context.get("runtimePrime") if isinstance(scenario_context.get("runtimePrime"), dict) else {}
        traffic_ends_epoch_millis = int(
            scenario_context.get("trafficEndsEpochMillis")
            or runtime_prime.get("trafficEndsEpochMillis")
            or 0
        )
        deadline = traffic_ends_epoch_millis / 1000 if traffic_ends_epoch_millis > 0 else time.time() + duration
        round_index = 0
        while time.time() < deadline:
            if self.event_cancelled_by_reset(execution_id):
                return
            if self.remaining_lambda_millis(context) < 45_000:
                if self.events:
                    self.events.update_status(
                        execution_id,
                        {
                            "status": "SIMULATION_RUNNING",
                            "eventType": event_type,
                            "config": cfg.to_dict(),
                            "scenarioContext": scenario_context,
                            "progressStage": "event_window_poll_deferred",
                            "updated_at": TimeUtils.now(),
                        },
                    )
                break
            sleep_for = min(poll_interval, max(0, deadline - time.time()))
            if sleep_for > 0:
                time.sleep(sleep_for)
            if self.event_cancelled_by_reset(execution_id):
                return
            round_index += 1
            metrics = self.current_metrics()
            slices = self.current_slices()
            if self.events:
                self.events.update_status(
                    execution_id,
                    {
                        "status": "SIMULATION_RUNNING",
                        "eventType": event_type,
                        "config": cfg.to_dict(),
                        "scenarioContext": scenario_context,
                        "progressStage": "event_window_poll",
                        "plannerPoll": {
                            "round": round_index,
                            "throughputMbps": metrics.get("throughputMbps", 0),
                            "latencyMs": metrics.get("latencyMs", 0),
                            "pduSessionCount": metrics.get("pduSessionCount", 0),
                            "upfCpuPercent": metrics.get("upfCpuPercent", 0),
                        },
                        "updated_at": TimeUtils.now(),
                    },
                )
            self.broadcast({"type": WsMessageType.METRICS_UPDATE.value, "payload": metrics})
            self.broadcast({"type": WsMessageType.SLICE_UPDATE.value, "payload": slices})
            self.broadcast({"type": WsMessageType.NETWORK_SNAPSHOT.value, "payload": self.network_snapshot(metrics, slices)})

    @staticmethod
    def remaining_lambda_millis(context: Any) -> int:
        if context and hasattr(context, "get_remaining_time_in_millis"):
            try:
                return int(context.get_remaining_time_in_millis())
            except Exception:
                return 300_000
        return 300_000

    def tool_config_for_execution(self, event_type: str, execution_id: str) -> tuple[Any, dict[str, Any]]:
        """Resolve the (cfg, scenario_context) a Bedrock agent tool call must operate on.

        Agent tools operate against the same scaled event configuration that the
        trigger persisted. Real UE/session traffic is created only by the event
        runtime priming path, but NEF/free5GC tools still need the scaled cfg and
        execution_id so policy orchestration targets the active scenario.
        """
        base = EVENT_CONFIG[event_type]
        if self.events:
            try:
                item = self.events.get_status(execution_id)
            except Exception as exc:
                print(f"tool_config_for_execution status lookup skipped for {execution_id}: {exc}")
                item = None
            stored_config = (item or {}).get("config") if isinstance(item, dict) else None
            if isinstance(stored_config, dict):
                try:
                    cfg = type(base)(**stored_config)
                    scenario_context = item.get("scenarioContext") if isinstance(item.get("scenarioContext"), dict) else {}
                    return cfg, dict(scenario_context)
                except (ValidationError, TypeError, ValueError) as exc:
                    print(f"tool_config_for_execution could not rebuild scaled cfg for {execution_id}: {exc}")
        return base, {}

    def event_config_for_request(self, event_type: str, event_scale: int = 0, city_residents: int = 0) -> tuple[Any, dict[str, Any]]:
        base = EVENT_CONFIG[event_type]
        default_scale = {
            "concert": 80_000,
            "typhoon": 1_200_000,
            "accident": 1_800,
            "medical": 650,
            "iot_surge": 50_000,
        }.get(event_type, max(base.ue_count, 1))
        max_scale = {
            "concert": 120_000,
            "typhoon": 1_500_000,
            "accident": 5_000,
            "medical": 2_000,
            "iot_surge": 100_000,
        }.get(event_type, default_scale)
        # Every scenario uses one shared wall-clock window so side-by-side scenarios
        # start and finish together in both the runtime and the UI countdown.
        duration_seconds = 180
        safe_scale = max(1, min(int(event_scale or default_scale), max_scale))
        safe_residents = max(1, min(int(city_residents or 180_000), 1_500_000))
        scale_ratio = safe_scale / max(default_scale, 1)
        max_ues = 50 if event_type == "iot_surge" else 10 if event_type == "typhoon" else 5
        ue_count = max(1, min(max_ues, round(base.ue_count * max(scale_ratio, 0.5))))
        cfg_data = base.to_dict()
        cfg_data["ue_count"] = ue_count
        cfg_data["ue_ids"] = base.ue_ids[: max(1, min(len(base.ue_ids), ue_count))]
        cfg_data["traffic_profile"] = self.traffic_profile_for_scale(event_type, scale_ratio)
        iperf_target_mbps = target_mbps_for_ratio(event_type, scale_ratio)
        context = {
            "eventScale": safe_scale,
            "cityResidents": safe_residents,
            "defaultScale": default_scale,
            "maxScale": max_scale,
            "scaleRatio": round(scale_ratio, 6),
            "iperfTargetMbps": iperf_target_mbps,
            "trafficEvidenceSource": "eks-ueransim-tun-iperf3",
            "eventDurationSeconds": duration_seconds,
            "cooldownSeconds": 45,
        }
        return type(base)(**cfg_data), context

    @staticmethod
    def slice_strategy_value(strategy: Any) -> str:
        value = strategy.value if hasattr(strategy, "value") else str(strategy or SliceStrategy.NONE.value)
        if value not in {item.value for item in SliceStrategy}:
            raise ValueError("slice_strategy must be one of: none, static, ai")
        return value

    @staticmethod
    def non_ai_policy_evidence(slice_strategy: str) -> dict[str, Any]:
        if slice_strategy == SliceStrategy.STATIC.value:
            return {
                "mode": SliceStrategy.STATIC.value,
                "status": "observed_only",
                "applied": False,
                "actuator": "none",
                "requestedAllocationPercent": {"eMBB": 40, "URLLC": 30, "mMTC": 20, "V2X": 10},
                "reason": "No safe network slice quota actuator is configured; fixed shares are displayed as the requested teaching policy and were not claimed as an applied free5GC change.",
            }
        return {
            "mode": SliceStrategy.NONE.value,
            "status": "not_requested",
            "applied": False,
            "actuator": "none",
            "reason": "No slicing policy or AI/NEF orchestration was requested; the round only generated and measured scenario traffic.",
        }

    def cleanup_batch_runtime(self, request: TriggerRequest) -> None:
        if not self.settings.eks_cluster_name:
            return
        try:
            k8s = get_eks_client(self.settings.eks_cluster_name)
        except Exception as exc:
            print(f"batch runtime cleanup client unavailable: {exc}")
            return
        for item in request.scenarios:
            raw_type = item.get("event_type") if isinstance(item, dict) else item.event_type
            event_type = raw_type.value if hasattr(raw_type, "value") else str(raw_type)
            if event_type not in EVENT_CONFIG:
                continue
            try:
                self.environment.cleanup_event_runtime(k8s, event_type)
            except Exception as exc:
                print(f"batch runtime cleanup skipped for {event_type}: {exc}")

    def cancel_batch_execution_if_reset(
        self,
        execution_id: str,
        request: TriggerRequest,
        event: dict[str, Any],
        stage: str,
    ) -> dict[str, Any] | None:
        """Stop a late async batch and remove anything it may have created after reset."""
        if not self.event_cancelled_by_reset(execution_id):
            return None
        self.cleanup_batch_runtime(request)
        self.invalidate_metrics_cache()
        cancelled = {
            "status": "SIMULATION_CANCELLED",
            "reason": f"Scenario batch was reset {stage}.",
            "completed_at": TimeUtils.now(),
        }
        if self.events:
            self.events.update_status(execution_id, cancelled)
            self.events.release_session_lease(self.request_session_id(event))
        self.broadcast({
            "type": WsMessageType.EVENT_RESET.value,
            "payload": {"executionId": execution_id, "eventType": "network_round", **cancelled},
        })
        return self.response(409, {
            "error": ApiErrorCode.EVENT_CANCELLED.value,
            "detail": {"executionId": execution_id, "eventType": "network_round", **cancelled},
        })

    @staticmethod
    def traffic_profile_for_scale(event_type: str, scale_ratio: float) -> str:
        mbps = target_mbps_for_ratio(event_type, scale_ratio)
        if event_type == "iot_surge":
            parallel = max(4, min(24, round(12 * max(scale_ratio, 0.5))))
            per_stream_kbps = max(10, round((mbps * 1000) / parallel))
            return f"iperf3 UDP {per_stream_kbps}K x {parallel} parallel streams"
        rate = f"{mbps:.3f}".rstrip("0").rstrip(".")
        packet_length = expected_profile(event_type).packet_length
        return f"iperf3 UDP {rate}M, {packet_length}-byte population-proportional capped traffic"

    def free5gc_status_for_trigger(self) -> dict[str, Any]:
        status = self.free5gc_status()
        if status.get("connected"):
            return status
        for _ in range(2):
            time.sleep(1)
            status = self.free5gc_status()
            if status.get("connected"):
                return status
        return status

    @staticmethod
    def can_continue_with_degraded_webui(status: dict[str, Any]) -> bool:
        error = str(status.get("error") or "").lower()
        metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
        pod_counts = metrics.get("podCounts") if isinstance(metrics.get("podCounts"), dict) else {}
        core_components_ready = all(int(pod_counts.get(component, 0) or 0) >= 1 for component in ("AMF", "SMF", "UPF", "NEF", "PCF", "NSSF"))
        transient_webui_error = any(token in error for token in ("timed out", "timeout", "unreachable"))
        return core_components_ready and transient_webui_error

    def invoke_trigger_async(self, event: dict[str, Any], context: Any, execution_id: str, event_type: str, scenario_context: dict[str, Any]) -> None:
        payload = dict(event)
        payload["body"] = json.dumps(
            {
                "event_type": event_type,
                "event_scale": scenario_context.get("eventScale"),
                "city_residents": scenario_context.get("cityResidents"),
                "slice_strategy": scenario_context.get("sliceStrategy") or SliceStrategy.NONE.value,
                "batch_scenarios": scenario_context.get("batchScenarios") or [event_type],
                "_async": True,
                "execution_id": execution_id,
                "locale": scenario_context.get("locale") or "en",
            }
        )
        function_name = getattr(context, "invoked_function_arn", "") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        if not function_name:
            raise RuntimeError("Cannot enqueue trigger: Lambda function name is unavailable")
        boto3.client("lambda").invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )

    def run_agentxg_loop(
        self,
        execution_id: str,
        event_type: str,
        cfg: Any,
        observed_metrics: dict[str, Any],
        observed_slices: list[dict[str, Any]],
        scenario_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def publish_progress(progress: dict[str, Any]) -> None:
            decision = progress.get("agentDecision")
            updates = {
                "status": "SIMULATION_RUNNING",
                "eventType": event_type,
                "config": cfg.to_dict(),
                "intent": progress.get("intent", {}),
                "baseline": progress.get("baseline", {}),
                "planner": progress.get("planner", {}),
                "executor": progress.get("executor", {}),
                "agentDecision": decision,
                "progressStage": progress.get("stage", "running"),
                "updated_at": TimeUtils.now(),
            }
            if self.events:
                self.events.update_status(execution_id, updates)
            if decision and not self.event_cancelled_by_reset(execution_id):
                self.broadcast({"type": WsMessageType.AGENT_DECISION.value, "payload": {"executionId": execution_id, **decision}})

        try:
            result = self.agentxg.run(
                execution_id,
                event_type,
                cfg,
                observed_metrics,
                observed_slices,
                scenario_context or {},
                on_progress=publish_progress,
            )
            actions = result.get("executor", {}).get("actions", [])
            result["status"] = result.get("executor", {}).get("status", "success")
            result["free5gc"] = self.find_action_result(actions, "upsert_subscriber_profile") or {
                "status": "skipped",
                "reason": "upsert_subscriber_profile was not executed",
            }
            runtime_prime = scenario_context.get("runtimePrime") if isinstance(scenario_context, dict) else None
            result["environment"] = runtime_prime if isinstance(runtime_prime, dict) else {
                "status": "skipped",
                "reason": "Scenario runtime is managed by the event trigger before agent planning.",
            }
            return result
        except Exception as exc:
            print(f"AgentxG loop failed, falling back to rule-based decision: {exc}")
            free5gc_result = self.free5gc.upsert_subscribers(
                event_type,
                cfg,
                execution_id,
                self.settings.runtime_subscriber_upsert_limit,
            )
            runtime_prime = scenario_context.get("runtimePrime") if isinstance(scenario_context, dict) else None
            environment_result = runtime_prime if isinstance(runtime_prime, dict) else {
                "status": "skipped",
                "reason": "Fallback does not generate UE/session traffic; event runtime priming owns traffic generation.",
            }
            decision = self.decisions.build_decision(
                event_type,
                cfg,
                free5gc_result,
                environment_result,
                observed_metrics,
                observed_metrics,
                observed_slices,
            )
            decision["agentName"] = "Fallback Decision Service"
            return {
                "status": "fallback",
                "agentDecision": decision,
                "free5gc": free5gc_result,
                "environment": environment_result,
                "finalMetrics": self.current_metrics(),
                "finalSlices": self.current_slices(),
                "verification": {"status": "fallback", "error": str(exc)},
            }

    @staticmethod
    def find_action_result(actions: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
        for action in actions:
            if action.get("tool") == tool:
                result = action.get("result")
                return result if isinstance(result, dict) else action
        return None

    def handle_reset(self, event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
        session_id = self.request_session_id(event or {})
        if not self.events:
            return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured; async reset queue is unavailable"})
        lease = self.events.session_lease()
        if lease and lease.get("sessionId") != session_id:
            return self.response(409, {"error": "SESSION_BUSY", "detail": "This simulation belongs to another browser session and cannot be reset from this tab."})
        # Keep the shared-runtime lease beyond the Lambda's 900-second hard
        # timeout so another browser cannot start while a timed-out worker's
        # Kubernetes requests are still unwinding.
        if not self.events.acquire_session_lease(session_id, ttl_seconds=1200):
            return self.response(409, {"error": "SESSION_BUSY", "detail": "Another browser session is currently using the shared free5GC runtime."})

        reset_id = str(uuid.uuid4())
        job, created = self.events.begin_reset_job(session_id, reset_id, TimeUtils.now())
        reset_id = str(job["resetId"])
        if created:
            self.events.put_reset_marker(TimeUtils.epoch_millis(), TimeUtils.now())
            try:
                self.invoke_reset_async(context, session_id, reset_id)
            except Exception as exc:
                failed_at = TimeUtils.now()
                self.events.update_reset_job(session_id, reset_id, {
                    "status": "failed",
                    "progressStage": "enqueue_failed",
                    "progressPercent": 100,
                    "message": "Failed to enqueue reset cleanup",
                    "error": str(exc),
                    "completedAt": failed_at,
                    "updatedAt": failed_at,
                })
                self.events.release_session_lease(session_id)
                return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "Failed to enqueue async reset cleanup"})

        return self.response(202, {
            "resetId": reset_id,
            "status": job.get("status") or "queued",
            "progressStage": job.get("progressStage") or "queued",
            "progressPercent": int(job.get("progressPercent") or 0),
            "statusUrl": f"/events/reset/{reset_id}",
            "idempotentReplay": not created,
        })

    def handle_reset_status(self, event: dict[str, Any], reset_id: str) -> dict[str, Any]:
        if not self.events:
            return self.response(503, {"error": ApiErrorCode.SERVICE_UNAVAILABLE.value, "detail": "DYNAMODB_TABLE is not configured"})
        job = self.events.get_reset_job(self.request_session_id(event), reset_id)
        if not job:
            return self.response(404, {"error": ApiErrorCode.NOT_FOUND.value, "detail": "Reset job not found in this browser session"})
        deadline = int(job.get("deadlineEpochSeconds") or 0)
        if job.get("status") in {"queued", "running"} and deadline > 0 and deadline < int(time.time()):
            failed_at = TimeUtils.now()
            self.events.update_reset_job(self.request_session_id(event), reset_id, {
                "status": "failed",
                "progressStage": "timeout",
                "progressPercent": 100,
                "message": "Reset worker exceeded its execution deadline",
                "error": "Reset cleanup timed out before a terminal worker update",
                "completedAt": failed_at,
                "updatedAt": failed_at,
            })
            self.events.release_session_lease(self.request_session_id(event))
            job = self.events.get_reset_job(self.request_session_id(event), reset_id) or job
        return self.response(200, job)

    def invoke_reset_async(self, context: Any, session_id: str, reset_id: str) -> None:
        function_name = getattr(context, "invoked_function_arn", "") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        if not function_name:
            raise RuntimeError("Cannot enqueue reset: Lambda function name is unavailable")
        payload = {
            "_cityverseInternalAction": "reset",
            "sessionId": session_id,
            "resetId": reset_id,
        }
        response = boto3.client("lambda").invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        if int(response.get("StatusCode") or 0) != 202:
            raise RuntimeError(f"Lambda reset enqueue returned HTTP {response.get('StatusCode')}")

    def handle_reset_worker(self, event: dict[str, Any]) -> dict[str, Any]:
        session_id = str(event.get("sessionId") or "")[:128]
        reset_id = str(event.get("resetId") or "")
        if not self.events or not session_id or not reset_id:
            return {"status": "ignored", "reason": "invalid internal reset payload"}
        started_at = TimeUtils.now()
        if not self.events.claim_reset_job(session_id, reset_id, started_at):
            return {"status": "duplicate", "resetId": reset_id}

        environment_reset: dict[str, Any] | None = None
        session_recycle: dict[str, Any] | None = None
        try:
            if not self.settings.eks_cluster_name:
                raise RuntimeError("EKS_CLUSTER_NAME is not configured; runtime cleanup cannot be verified")
            k8s = get_eks_client(self.settings.eks_cluster_name)
            environment_reset = self.environment.cleanup_all_event_runtime(k8s)
            now = TimeUtils.now()
            self.events.update_reset_job(session_id, reset_id, {
                "progressStage": "session_recycle",
                "progressPercent": 50,
                "message": "Scenario traffic removed; recycling free5GC session state",
                "environment": environment_reset,
                "updatedAt": now,
            })
            session_recycle = self.environment.recycle_session_state(k8s)
            now = TimeUtils.now()
            self.events.update_reset_job(session_id, reset_id, {
                "progressStage": "status_refresh",
                "progressPercent": 90,
                "message": "Session state recycled; refreshing dashboard status",
                "sessionRecycle": session_recycle,
                "updatedAt": now,
            })
            self._free5gc_status_cache = None
            self._free5gc_status_cache_at = 0.0
            self.invalidate_metrics_cache()
            self.broadcast({"type": WsMessageType.FREE5GC_STATUS.value, "payload": self.safe_free5gc_status()})
            completed_at = TimeUtils.now()
            self.events.update_reset_job(session_id, reset_id, {
                "status": "success",
                "progressStage": "complete",
                "progressPercent": 100,
                "message": "Simulation runtime reset completed",
                "free5gc": {"status": "preserved", "reason": "Subscribers are deploy-seeded and reused across runtime scenarios."},
                "environment": environment_reset,
                "sessionRecycle": session_recycle,
                "completedAt": completed_at,
                "updatedAt": completed_at,
            })
            self.broadcast({"type": WsMessageType.EVENT_RESET.value, "payload": {"resetId": reset_id, "status": "success"}})
            return {"status": "success", "resetId": reset_id}
        except Exception as exc:
            completed_at = TimeUtils.now()
            self.events.update_reset_job(session_id, reset_id, {
                "status": "failed",
                "progressStage": "failed",
                "progressPercent": 100,
                "message": "Simulation runtime reset failed",
                "error": str(exc),
                "environment": environment_reset,
                "sessionRecycle": session_recycle,
                "completedAt": completed_at,
                "updatedAt": completed_at,
            })
            self.broadcast({"type": WsMessageType.EVENT_BLOCKED.value, "payload": {"resetId": reset_id, "status": "failed", "error": str(exc)}})
            return {"status": "failed", "resetId": reset_id, "error": str(exc)}
        finally:
            self.events.release_session_lease(session_id)

    @staticmethod
    def request_session_id(event: dict[str, Any]) -> str:
        headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
        return str(headers.get("x-session-id") or "legacy-session")[:128]

    def foreign_session_busy(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if not self.events:
            return None
        lease = self.events.session_lease()
        if lease and lease.get("sessionId") != self.request_session_id(event):
            return {"error": "SESSION_BUSY", "detail": "Another browser session is generating traffic on the shared free5GC runtime.", "retryAfterSeconds": max(1, int(lease.get("expiresAt") or 0) - int(time.time()))}
        return None

    def invalidate_metrics_cache(self) -> None:
        self._metrics_cache = None
        self._metrics_cache_at = 0.0
        self._scaling_state_cache = None
        self._scaling_state_cache_at = 0.0

    def broadcast(self, message: dict[str, Any]) -> None:
        if not self.websocket:
            return
        try:
            self.websocket.broadcast(message)
        except Exception as exc:
            print(f"WebSocket broadcast skipped: {exc}")

    def free5gc_status(self) -> dict[str, Any]:
        self.ensure_baseline_runtime()
        metrics = self.metrics.unavailable_metrics()
        slices = self.slices_from_runtime_metrics(SliceCatalog.default_slices(), metrics)
        scaling_state = {}
        if self.settings.eks_cluster_name:
            scaling_state = self.eks_scaling_state(include_runtime_logs=True, include_hpa=True)
            runtime_metrics = scaling_state.get("runtimeMetrics") or {}
            if runtime_metrics:
                metrics.update(runtime_metrics)
                slices = self.slices_from_runtime_metrics(slices, metrics)
            metrics.update(
                {
                    "upfPodCount": scaling_state.get("upfPodCount", metrics.get("upfPodCount", 0)),
                    "amfPodCount": scaling_state.get("amfPodCount", metrics.get("amfPodCount", 0)),
                    "podCounts": scaling_state.get("podCounts", {}),
                    "podComponents": scaling_state.get("podComponents", []),
                    "componentCpuPercent": scaling_state.get("componentCpuPercent", {}),
                    "scalingSource": "eks",
                }
            )
            metrics = self.apply_runtime_session_floor(metrics, scaling_state)
            slices = self.slices_from_runtime_metrics(SliceCatalog.default_slices(), metrics)
            if metrics.get("dataSource") == "unavailable" and scaling_state.get("podComponents"):
                metrics["dataSource"] = "eks"
        runtime_is_authoritative = self.eks_runtime_is_authoritative(scaling_state)
        status = self.free5gc.status_payload(metrics=metrics, slices=slices)
        if status.get("connected"):
            registered_ues = status.get("registeredUes") or []
            if isinstance(registered_ues, list):
                core_metrics = self.metrics_from_registered_ues(registered_ues, data_source="free5gc-oam")
                status_metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else metrics
                if not runtime_is_authoritative and (
                    core_metrics.get("registeredUeCount", 0) > status_metrics.get("registeredUeCount", 0)
                    or core_metrics.get("pduSessionCount", 0) > status_metrics.get("pduSessionCount", 0)
                ):
                    throughput_metrics = {
                        key: status_metrics[key]
                        for key in (
                            "throughputMbps",
                            "uplinkMbps",
                            "downlinkMbps",
                            "iperf3Mbps",
                            "iperf3",
                            "scenarioTraffic",
                            "latencyMs",
                            "gtpPacketsPerSec",
                            "ueTunProbe",
                            "tunErrorCount",
                        )
                        if key in status_metrics and status_metrics.get(key)
                    }
                    status_metrics.update(core_metrics)
                    status_metrics.update(throughput_metrics)
                    if throughput_metrics.get("iperf3Mbps"):
                        status_metrics["dataSource"] = "free5gc-oam+iperf3"
                    status["metrics"] = status_metrics
                    status["registeredUeCount"] = status_metrics.get("registeredUeCount", status.get("registeredUeCount", 0))
        if not status.get("connected") and self.runtime_bearer_is_healthy(metrics):
            status.update(
                {
                    "connected": True,
                    "source": "EKS UE TUN probe; free5GC WebUI API degraded",
                    "registeredUeCount": metrics.get("registeredUeCount", 0),
                    "warning": status.get("error") or "free5GC WebUI API degraded",
                }
            )
            status.pop("error", None)
        if self.settings.eks_cluster_name and scaling_state.get("podComponents"):
            status_metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else metrics
            ueransim_active_pods = self.ueransim_active_ue_pod_count(scaling_state)
            registered_count = int(status_metrics.get("registeredUeCount") or 0)
            stale = max(0, registered_count - ueransim_active_pods)
            status_metrics["ueransimActivePods"] = ueransim_active_pods
            status_metrics["staleRegistrations"] = stale
        slice_base = SliceCatalog.default_slices() if runtime_is_authoritative else status.get("slices", [])
        status["slices"] = self.slices_from_runtime_metrics(slice_base, status.get("metrics", metrics))
        network_snapshot = self.network_snapshot(status.get("metrics", metrics), status["slices"])
        network_snapshot.pop("metrics", None)
        network_snapshot.pop("slices", None)
        status["networkSnapshot"] = network_snapshot
        if self.settings.status_include_eks:
            if scaling_state:
                status["podComponents"] = scaling_state.get("podComponents", [])
        return status

    def safe_free5gc_status(self) -> dict[str, Any]:
        now = time.time()
        if self._free5gc_status_cache and now - self._free5gc_status_cache_at <= self._free5gc_status_cache_ttl:
            cached = dict(self._free5gc_status_cache)
            cached["cache"] = {
                "status": "hit",
                "ageSeconds": round(now - self._free5gc_status_cache_at, 3),
                "ttlSeconds": self._free5gc_status_cache_ttl,
            }
            return cached
        try:
            status = self.free5gc_status()
            if status.get("connected"):
                self._free5gc_status_cache = status
                self._free5gc_status_cache_at = now
            status["cache"] = {"status": "miss", "ttlSeconds": self._free5gc_status_cache_ttl}
            return status
        except Exception as exc:
            print(f"free5GC status degraded: {exc}")
            if self._free5gc_status_cache:
                cached = dict(self._free5gc_status_cache)
                cached.update(
                    {
                        "cache": {
                            "status": "stale",
                            "ageSeconds": round(now - self._free5gc_status_cache_at, 3),
                            "ttlSeconds": self._free5gc_status_cache_ttl,
                        },
                        "warning": f"serving last known free5GC status after refresh failure: {exc}",
                    }
                )
                return cached
            metrics = self.metrics.unavailable_metrics()
            slices = self.slices_from_runtime_metrics(SliceCatalog.default_slices(), metrics)
            network_snapshot = self.network_snapshot(metrics, slices)
            network_snapshot.pop("metrics", None)
            network_snapshot.pop("slices", None)
            return {
                "connected": False,
                "source": "backend status fallback",
                "error": str(exc),
                "subscribers": [],
                "eventSubscribers": [],
                "registeredUes": [],
                "profiles": [],
                "metrics": metrics,
                "slices": slices,
                "networkSnapshot": network_snapshot,
                "checkedAt": TimeUtils.now(),
            }

    def network_snapshot(self, metrics: dict[str, Any], slices: list[dict[str, Any]]) -> dict[str, Any]:
        timestamp = int(metrics.get("timestamp") or TimeUtils.epoch_millis())
        scenario_edges = self.scenario_edges_from_metrics(metrics)
        control_edges = self.control_plane_edges_from_metrics(metrics)
        if scenario_edges:
            return {
                "id": f"free5gc-{timestamp}",
                "timestamp": timestamp,
                "source": metrics.get("dataSource") or "unknown",
                "metrics": metrics,
                "slices": slices,
                "edges": scenario_edges + control_edges,
            }
        if metrics.get("activeScenarios"):
            return {
                "id": f"free5gc-{timestamp}",
                "timestamp": timestamp,
                "source": metrics.get("dataSource") or "unknown",
                "metrics": metrics,
                "slices": slices,
                "edges": control_edges,
            }
        throughput = self.safe_float(metrics.get("throughputMbps"))
        uplink = self.safe_float(metrics.get("uplinkMbps"), throughput / 2)
        downlink = self.safe_float(metrics.get("downlinkMbps"), max(throughput - uplink, 0))
        latency = max(self.safe_float(metrics.get("latencyMs"), 1), 1)
        congestion = self.safe_float(metrics.get("upfCpuPercent"))
        active = bool(
            throughput > 0
            or self.safe_float(metrics.get("gtpPacketsPerSec")) > 0
        )
        slice_type = self.dominant_slice_type(slices)
        five_qi = {"eMBB": 9, "URLLC": 1, "mMTC": 79, "V2X": 79}.get(slice_type, 9)
        edge_base = {
            "sliceType": slice_type,
            "active": active,
            "throughputMbps": round(throughput, 2),
            "uplinkMbps": round(uplink, 2),
            "downlinkMbps": round(downlink, 2),
            "latencyMs": round(latency, 2),
            "packetLossPercent": self.safe_float(metrics.get("packetLossPercent")),
            "upfCongestionPercent": round(congestion, 2),
            "fiveQi": five_qi,
        }
        edges = []
        if active:
            gnb = self.gnb_for_source_node("residential")
            edges = [
                {"id": f"live-residential-{gnb}", "sourceNodeId": "residential", "targetNodeId": gnb, **edge_base},
                {"id": f"live-{gnb}-upf", "sourceNodeId": gnb, "targetNodeId": "upf", **edge_base},
                {"id": "live-upf-dn", "sourceNodeId": "upf", "targetNodeId": "dn", **edge_base},
            ]
        edges.extend(control_edges)
        return {
            "id": f"free5gc-{timestamp}",
            "timestamp": timestamp,
            "source": metrics.get("dataSource") or "unknown",
            "metrics": metrics,
            "slices": slices,
            "edges": edges,
        }

    def control_plane_edges_from_metrics(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        traffic = metrics.get("controlPlaneTraffic")
        if not isinstance(traffic, list):
            return []
        now_millis = TimeUtils.epoch_millis()
        max_age_millis = max(self._control_plane_log_window_seconds * 1000, 1000)
        edges: list[dict[str, Any]] = []
        for index, item in enumerate(traffic):
            if not isinstance(item, dict):
                continue
            source = str(item.get("sourceNodeId") or "")
            target = str(item.get("targetNodeId") or "")
            protocol = str(item.get("protocol") or "")
            evidence_count = int(self.safe_float(item.get("evidenceCount"), 1))
            if not source or not target or not protocol or evidence_count <= 0:
                continue
            observed_at = int(self.safe_float(item.get("lastObservedEpochMillis")))
            if observed_at > 0 and now_millis - observed_at > max_age_millis:
                continue
            intensity = min(max(evidence_count * 0.2, 0.2), 8.0)
            edges.append(
                {
                "id": str(item.get("id") or f"cp-actual-{source}-{target}-{index}"),
                    "sourceNodeId": source,
                    "targetNodeId": target,
                    "sliceType": str(item.get("sliceType") or "eMBB"),
                    "active": True,
                    "plane": "control",
                    "protocol": protocol,
                    "scenario": str(item.get("scenario") or "control-signaling"),
                    "throughputMbps": round(intensity, 3),
                    "uplinkMbps": round(intensity, 3),
                    "downlinkMbps": 0.0,
                    "latencyMs": max(self.safe_float(item.get("latencyMs"), self.safe_float(metrics.get("latencyMs"), 1)), 1),
                    "packetLossPercent": 0,
                    "upfCongestionPercent": round(self.safe_float(metrics.get("upfCpuPercent")), 2),
                    "fiveQi": int(self.safe_float(item.get("fiveQi"), 9)),
                    "evidenceCount": evidence_count,
                    "evidence": item.get("evidence") or [],
                    "lastObservedAt": item.get("lastObservedAt"),
                    "lastObservedEpochMillis": observed_at or None,
                }
            )
        return edges

    @staticmethod
    def baseline_scenario_throughput(metrics: dict[str, Any]) -> float | None:
        """Pull the citizen baseline sample's throughput from scenarioTraffic, if
        observed before this trigger, so intent_manager can set a real protection
        floor instead of falling back to the conservative default."""
        traffic = metrics.get("scenarioTraffic")
        if not isinstance(traffic, list):
            return None
        for sample in traffic:
            if (
                isinstance(sample, dict)
                and str(sample.get("scenario") or "") == "baseline"
                and sample.get("transport") == "free5gc-tun"
            ):
                throughput = CityVerseBackendApp.safe_float(sample.get("throughputMbps"))
                if throughput > 0:
                    return throughput
        return None

    def scenario_edges_from_metrics(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        traffic = metrics.get("scenarioTraffic")
        if not isinstance(traffic, list):
            return []
        samples = [sample for sample in traffic if isinstance(sample, dict)]
        active_scenarios = {str(item) for item in (metrics.get("activeScenarios") or []) if str(item) in EVENT_CONFIG}
        if active_scenarios:
            samples = [sample for sample in samples if str(sample.get("scenario") or "") in active_scenarios]
        edges: list[dict[str, Any]] = []
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                continue
            # A Kubernetes pod-to-Service iperf sample is useful load evidence,
            # but it did not traverse uesimtun0/UPF. Never draw it as a real UE
            # user-plane path; only TUN-bound samples may create RAN/N3/N6 edges.
            if sample.get("transport") != "free5gc-tun":
                continue
            scenario = str(sample.get("scenario") or "baseline")
            throughput = self.safe_float(sample.get("throughputMbps"))
            if throughput <= 0:
                continue
            source_node, slice_type, five_qi = self.scenario_flow_metadata(scenario)
            latency = max(self.safe_float(sample.get("jitterMs"), self.safe_float(metrics.get("latencyMs"), 1)), 1)
            packet_loss = self.safe_float(sample.get("lostPercent"), self.safe_float(metrics.get("packetLossPercent")))
            edge_base = {
                "sliceType": slice_type,
                "scenario": scenario,
                "active": True,
                "throughputMbps": round(throughput, 2),
                "uplinkMbps": round(self.safe_float(sample.get("uplinkMbps"), throughput), 2),
                "downlinkMbps": round(self.safe_float(sample.get("downlinkMbps")), 2),
                "latencyMs": round(latency, 2),
                "packetLossPercent": round(packet_loss, 2),
                "upfCongestionPercent": round(self.safe_float(metrics.get("upfCpuPercent")), 2),
                "fiveQi": five_qi,
            }
            prefix = f"live-{scenario}-{index}"
            gnb = self.gnb_for_source_node(source_node)
            edges.extend(
                [
                    {"id": f"{prefix}-ran", "sourceNodeId": source_node, "targetNodeId": gnb, **edge_base},
                    {"id": f"{prefix}-n3", "sourceNodeId": gnb, "targetNodeId": "upf", **edge_base},
                    {"id": f"{prefix}-n6", "sourceNodeId": "upf", "targetNodeId": "dn", **edge_base},
                ]
            )
        return edges

    @staticmethod
    def throughput_from_profile(profile: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG])", str(profile or ""), re.IGNORECASE)
        if not match:
            return 1.0
        value = float(match.group(1))
        unit = match.group(2).upper()
        if unit == "K":
            return round(value / 1000, 3)
        if unit == "G":
            return round(value * 1000, 3)
        return round(value, 3)

    @staticmethod
    def scenario_flow_metadata(scenario: str) -> tuple[str, str, int]:
        mapping = {
            "baseline": ("residential", "eMBB", 9),
            "baseline-embb": ("residential", "eMBB", 9),
            "concert": ("mall", "eMBB", 9),
            "medical": ("hospital", "URLLC", 1),
            "typhoon": ("hospital", "URLLC", 2),
            "iot_surge": ("factory", "mMTC", 79),
            "accident": ("highway", "V2X", 79),
        }
        return mapping.get(scenario, ("residential", "eMBB", 9))

    @staticmethod
    def gnb_for_source_node(source_node: str) -> str:
        """All simulated UE sources attach to the single real UERANSIM gNB."""
        return "gnb1"

    @staticmethod
    def dominant_slice_type(slices: list[dict[str, Any]]) -> str:
        if not slices:
            return "eMBB"
        def score(item: dict[str, Any]) -> float:
            return CityVerseBackendApp.safe_float(item.get("throughputMbps"), CityVerseBackendApp.safe_float(item.get("load")))
        return str(max(slices, key=score).get("type") or "eMBB")

    @staticmethod
    def safe_float(value: Any, fallback: float = 0.0) -> float:
        try:
            number = float(value)
            if number != number:
                return fallback
            return number
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def runtime_bearer_is_healthy(metrics: dict[str, Any]) -> bool:
        return (
            str(metrics.get("dataSource") or "").startswith("eks+ue-tun-probe")
            and (metrics.get("throughputMbps", 0) > 0 or metrics.get("latencyMs", 0) > 0)
        )

    def current_slices(self) -> list[dict[str, Any]]:
        metrics = self.current_metrics(include_eks=True, include_oam=True)
        # In the deployed EKS topology, UERANSIM/TUN/iperf3 runtime logs are the
        # authoritative source. Do not make a failing Prometheus endpoint a
        # prerequisite for AI detection or slice construction.
        slices = self.free5gc.current_slices(
            metrics=metrics,
            query_prometheus=not bool(self.settings.eks_cluster_name),
        )
        return self.slices_from_runtime_metrics(slices, metrics)

    def current_metrics(self, include_eks: bool = True, include_oam: bool = True) -> dict[str, Any]:
        cacheable = include_eks is True and include_oam is True
        if cacheable:
            now = time.time()
            if self._metrics_cache is not None and now - self._metrics_cache_at <= self._metrics_cache_ttl:
                return dict(self._metrics_cache)
        metrics = self._current_metrics_uncached(include_eks, include_oam)
        if cacheable:
            self._metrics_cache = metrics
            self._metrics_cache_at = time.time()
        return metrics

    def _current_metrics_uncached(self, include_eks: bool = True, include_oam: bool = True) -> dict[str, Any]:
        self.ensure_baseline_runtime()
        scaling_state = self.eks_scaling_state() if include_eks else {}
        runtime_authoritative = self.eks_runtime_is_authoritative(scaling_state)
        # Avoid a dozen Prometheus socket attempts before reading the EKS source
        # that actually carries the scenario. Besides adding latency, the broken
        # endpoint intermittently raised Errno 16 and could consume the Lambda's
        # detection/planning budget.
        metrics = self.metrics.unavailable_metrics() if runtime_authoritative else self.metrics.current_metrics()
        if scaling_state:
            runtime_metrics = scaling_state.get("runtimeMetrics") or {}
            if (
                runtime_metrics.get("pduSessionCount", 0) > metrics.get("pduSessionCount", 0)
                or runtime_metrics.get("throughputMbps", 0) > 0
                or runtime_metrics.get("latencyMs", 0) > 0
            ):
                metrics.update(runtime_metrics)
                runtime_source = metrics.get("dataSource")
            metrics.update(
                {
                    "upfPodCount": scaling_state.get("upfPodCount", metrics.get("upfPodCount", 0)),
                    "amfPodCount": scaling_state.get("amfPodCount", metrics.get("amfPodCount", 0)),
                    "podCounts": scaling_state.get("podCounts", {}),
                    "podComponents": scaling_state.get("podComponents", []),
                    "componentCpuPercent": scaling_state.get("componentCpuPercent", {}),
                    "hpaStatus": scaling_state.get("hpaStatus", {}),
                    "scalingSource": "eks",
                    "dataSource": "eks+prometheus" if metrics.get("dataSource") == "prometheus" else runtime_source if "runtime_source" in locals() else "eks",
                    "evidenceLevel": EvidenceLevel.MEASURED.value,
                }
            )
            metrics = self.apply_runtime_session_floor(metrics, scaling_state)
        if include_oam and self.settings.free5gc_webui_url and not self.eks_runtime_is_authoritative(scaling_state):
            core_metrics = self.free5gc_oam_metrics()
            if (
                core_metrics.get("registeredUeCount", 0) > metrics.get("registeredUeCount", 0)
                or core_metrics.get("pduSessionCount", 0) > metrics.get("pduSessionCount", 0)
            ):
                throughput_metrics = {
                    key: metrics[key]
                    for key in (
                        "throughputMbps",
                        "uplinkMbps",
                        "downlinkMbps",
                        "iperf3Mbps",
                        "iperf3",
                        "scenarioTraffic",
                        "latencyMs",
                        "gtpPacketsPerSec",
                        "ueTunProbe",
                    )
                    if key in metrics and metrics.get(key)
                }
                metrics.update(core_metrics)
                metrics.update(throughput_metrics)
                if throughput_metrics.get("iperf3Mbps"):
                    metrics["dataSource"] = "free5gc-oam+iperf3"
        return metrics

    @staticmethod
    def eks_runtime_is_authoritative(scaling_state: dict[str, Any]) -> bool:
        pod_counts = scaling_state.get("podCounts") if isinstance(scaling_state, dict) else {}
        if not isinstance(pod_counts, dict):
            return False
        return int(pod_counts.get("UERANSIM") or 0) > 0

    def event_cancelled_by_reset(self, execution_id: str) -> bool:
        if not self.events:
            return False
        status = self.events.get_status(execution_id)
        if not status:
            return False
        try:
            started_epoch = int(status.get("startedEpochMillis") or 0)
        except (TypeError, ValueError):
            started_epoch = 0
        if started_epoch <= 0:
            return False
        return self.events.latest_reset_epoch_millis() >= started_epoch

    @staticmethod
    def apply_runtime_session_floor(metrics: dict[str, Any], scaling_state: dict[str, Any]) -> dict[str, Any]:
        pod_components = scaling_state.get("podComponents") if isinstance(scaling_state, dict) else []
        if not isinstance(pod_components, list):
            return metrics
        city_ue_running = False
        for component in pod_components:
            if component.get("component") != "UERANSIM":
                continue
            for pod in component.get("pods") or []:
                name = pod.get("name") or ""
                if name.startswith("ueransim-city-ue") and pod.get("phase") == "Running":
                    city_ue_running = True
                    break
        if not city_ue_running:
            return metrics
        updated = dict(metrics)
        slice_sessions = dict(updated.get("sliceSessions") or {})
        slice_sessions[1] = max(int(slice_sessions.get(1) or slice_sessions.get("1") or 0), 1)
        total_sessions = sum(int(value or 0) for value in slice_sessions.values())
        updated["sliceSessions"] = slice_sessions
        updated["registeredUeCount"] = max(int(updated.get("registeredUeCount") or 0), 1)
        updated["pduSessionCount"] = max(int(updated.get("pduSessionCount") or 0), total_sessions, 1)
        return updated

    @staticmethod
    def ueransim_active_ue_pod_count(scaling_state: dict[str, Any]) -> int:
        """Count Running UERANSIM UE pods (excluding gNB pods) from EKS pod data.

        Used as ground truth for how many UEs can plausibly still be attached,
        since deleting a UE pod does not make free5GC deregister it (no such
        API exists; AMF only deregisters on its own timers). See D5 in the
        Fable honesty audit.
        """
        pod_components = scaling_state.get("podComponents") if isinstance(scaling_state, dict) else []
        if not isinstance(pod_components, list):
            return 0
        count = 0
        for component in pod_components:
            if component.get("component") != "UERANSIM":
                continue
            for pod in component.get("pods") or []:
                name = str(pod.get("name") or "")
                if pod.get("phase") == "Running" and "gnb" not in name.lower():
                    count += 1
        return count

    def ensure_baseline_runtime(self) -> None:
        if not self.settings.baseline_traffic_enabled or not self.settings.eks_cluster_name:
            return
        now = time.time()
        if now - self._baseline_reconcile_at < self.settings.baseline_reconcile_interval_seconds:
            return
        self._baseline_reconcile_at = now
        try:
            k8s = get_eks_client(self.settings.eks_cluster_name)
            if self.environment.has_event_runtime(k8s):
                return
            self.environment.ensure_baseline_traffic(k8s)
        except Exception as exc:
            print(f"baseline traffic reconcile skipped: {exc}")

    def free5gc_oam_metrics(self) -> dict[str, Any]:
        try:
            registered_ues = self.free5gc.registered_ues()
        except Exception as exc:
            print(f"free5GC OAM metrics query failed: {exc}")
            return {}
        return self.metrics_from_registered_ues(registered_ues, data_source="free5gc-oam")

    @staticmethod
    def metrics_from_registered_ues(registered_ues: list[dict[str, Any]], data_source: str) -> dict[str, Any]:
        pdu_sessions = 0
        slice_sessions: dict[int, int] = {}
        for ue in registered_ues:
            sessions = ue.get("PduSessions") or ue.get("pduSessions") or []
            if not isinstance(sessions, list):
                continue
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                pdu_sessions += 1
                raw_sst = session.get("Sst") or session.get("sst") or (session.get("SNssai") or {}).get("sst")
                try:
                    sst = int(raw_sst)
                except (TypeError, ValueError):
                    continue
                slice_sessions[sst] = slice_sessions.get(sst, 0) + 1
        return {
            "registeredUeCount": len(registered_ues),
            "pduSessionCount": pdu_sessions,
            "sliceSessions": slice_sessions,
            "timestamp": TimeUtils.epoch_millis(),
            "dataSource": data_source,
        }

    def eks_scaling_state(self, include_runtime_logs: bool = True, include_hpa: bool = True) -> dict[str, Any]:
        cacheable = include_runtime_logs is True and include_hpa is True
        if cacheable:
            now = time.time()
            if self._scaling_state_cache is not None and now - self._scaling_state_cache_at <= self._metrics_cache_ttl:
                return dict(self._scaling_state_cache)
        result = self._eks_scaling_state_uncached(include_runtime_logs, include_hpa)
        if cacheable:
            self._scaling_state_cache = result
            self._scaling_state_cache_at = time.time()
        return result

    def _eks_scaling_state_uncached(self, include_runtime_logs: bool = True, include_hpa: bool = True) -> dict[str, Any]:
        if not self.settings.eks_cluster_name:
            return {}
        try:
            k8s = get_eks_client(self.settings.eks_cluster_name)
            _, pod_data = k8s.request("GET", f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods")
            items = pod_data.get("items", []) if isinstance(pod_data, dict) else []
            pod_counts: dict[str, int] = {}
            pod_components: dict[str, list[dict[str, str]]] = {}
            active_scenarios: set[str] = set()
            for pod in items:
                phase = ((pod.get("status") or {}).get("phase") or "Unknown")
                labels = (pod.get("metadata") or {}).get("labels") or {}
                component = self.component_from_pod(pod)
                if not component:
                    continue
                scenario = labels.get("5gcityverse.io/scenario")
                if phase == "Running" and scenario in EVENT_CONFIG:
                    active_scenarios.add(str(scenario))
                pod_components.setdefault(component, []).append(
                    {
                        "name": ((pod.get("metadata") or {}).get("name") or ""),
                        "phase": phase,
                        "reason": self.pod_reason(pod),
                        "scenario": str(scenario or ""),
                    }
                )
                if phase == "Running":
                    pod_counts[component] = pod_counts.get(component, 0) + 1

            component_cpu_percent = self.component_cpu_from_metrics_api(k8s)

            hpa_status = {}
            if include_hpa:
                for component, hpa_name in {
                    "UPF-eMBB": "upf-embb-hpa",
                    "UPF-URLLC": "upf-urllc-hpa",
                    "UPF-mMTC": "upf-mmtc-hpa",
                    "UPF-V2X": "upf-v2x-hpa",
                    "AMF": "amf-hpa",
                    "SMF": "smf-hpa",
                    "PCF": "pcf-hpa",
                    "NEF": "nef-hpa",
                }.items():
                    status, hpa = k8s.request(
                        "GET",
                        f"/apis/autoscaling/v2/namespaces/{self.settings.free5gc_namespace}/horizontalpodautoscalers/{hpa_name}",
                        ignore_404=True,
                    )
                    if status >= 300 or not isinstance(hpa, dict):
                        continue
                    spec = hpa.get("spec") or {}
                    current = hpa.get("status") or {}
                    hpa_status[component] = {
                        "name": hpa_name,
                        "minReplicas": spec.get("minReplicas"),
                        "maxReplicas": spec.get("maxReplicas"),
                        "currentReplicas": current.get("currentReplicas"),
                        "desiredReplicas": current.get("desiredReplicas"),
                    }

            runtime_metrics = self.ueransim_runtime_metrics(k8s, items) if include_runtime_logs else {}
            iperf3_metrics = self.iperf3_runtime_metrics(k8s, items) if include_runtime_logs else {}
            if iperf3_metrics:
                runtime_metrics.update(iperf3_metrics)
            # TCP iperf3 has no datagram counter and therefore deliberately marks
            # GTP pkt/s unavailable.  The exporter reads the actual upfgtp
            # interface, so its fresh samples are the authoritative replacement.
            gtp5g_metrics = self.gtp5g_metrics_exporter_runtime_metrics(k8s, items) if include_runtime_logs else {}
            if gtp5g_metrics:
                runtime_metrics.update(gtp5g_metrics)
            control_plane_metrics = self.control_plane_runtime_metrics(k8s, items) if include_runtime_logs else {}
            if control_plane_metrics:
                runtime_metrics.update(control_plane_metrics)
            if active_scenarios:
                runtime_metrics["activeScenarios"] = sorted(active_scenarios)
            return {
                "podCounts": pod_counts,
                    "podComponents": [
                        {"component": component, "pods": pods, "desired": pod_counts.get(component, 0)}
                        for component, pods in sorted(pod_components.items())
                    ],
                "componentCpuPercent": component_cpu_percent,
                "hpaStatus": hpa_status,
                "upfPodCount": pod_counts.get("UPF", 0),
                "amfPodCount": pod_counts.get("AMF", 0),
                "runtimeMetrics": runtime_metrics,
            }
        except Exception as exc:
            print(f"EKS scaling state query failed: {exc}")
            return {}

    def component_cpu_from_metrics_api(self, k8s: EksKubernetesClient) -> dict[str, float]:
        status, data = k8s.request("GET", f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.settings.free5gc_namespace}/pods", ignore_404=True)
        if status >= 300 or not isinstance(data, dict):
            return {}
        usage_by_component: dict[str, float] = {}
        for pod in data.get("items", []):
            component = self.component_from_pod(pod)
            if not component:
                continue
            total_cores = 0.0
            for container in (pod.get("containers") or []):
                usage = (container.get("usage") or {}).get("cpu")
                total_cores += self.cpu_quantity_to_cores(str(usage or "0"))
            usage_by_component[component] = usage_by_component.get(component, 0.0) + total_cores
        return {component: round(cores * 100, 1) for component, cores in usage_by_component.items()}

    @staticmethod
    def cpu_quantity_to_cores(value: str) -> float:
        value = value.strip()
        if not value:
            return 0.0
        multipliers = {
            "n": 1e-9,
            "u": 1e-6,
            "m": 1e-3,
        }
        suffix = value[-1]
        if suffix in multipliers:
            try:
                return float(value[:-1]) * multipliers[suffix]
            except ValueError:
                return 0.0
        try:
            return float(value)
        except ValueError:
            return 0.0

    @staticmethod
    def component_from_pod(pod: dict[str, Any]) -> str | None:
        labels = (pod.get("metadata") or {}).get("labels") or {}
        name = ((pod.get("metadata") or {}).get("name") or "").lower()
        label_text = " ".join(str(value).lower() for value in labels.values())
        haystack = f"{name} {label_text}"
        for component in ("UPF", "AMF", "SMF", "NEF", "PCF", "NSSF", "NRF", "UDR", "AUSF", "UERANSIM", "IPERF3"):
            if component.lower() in haystack:
                return component
        if "ue" in haystack or "gnb" in haystack:
            return "UERANSIM"
        return None

    @staticmethod
    def control_plane_node_for_component(component: str | None) -> str | None:
        mapping = {
            "AMF": "amf",
            "SMF": "smf",
            "PCF": "pcf",
            "NRF": "nrf",
            "NSSF": "nssf",
            "AUSF": "udm",
            "UDM": "udm",
            "UDR": "udr",
            "NEF": "nef",
            "UPF": "upf",
        }
        return mapping.get(str(component or "").upper())

    @staticmethod
    def protocol_for_nf_path(path: str, target_component: str) -> str | None:
        lower_path = path.lower()
        target = target_component.upper()
        ignored_prefixes = (
            "/namf-oam/",
            "/nsmf-oam/",
            "/npcf-oam/",
            "/nnef-oam/",
            "/nudm-oam/",
            "/nudr-oam/",
            "/nnrf-oam/",
            "/upi/",
        )
        if lower_path == "/" or any(lower_path.startswith(prefix) for prefix in ignored_prefixes):
            return None
        if lower_path.startswith("/nausf-auth/") and target == "AUSF":
            return "Nausf UE authentication"
        if lower_path.startswith("/nnssf-nsselection/") and target == "NSSF":
            return "Nnssf slice selection"
        if lower_path.startswith("/nsmf-pdusession/") and target == "SMF":
            return "Nsmf PDU session"
        if lower_path.startswith("/npcf-smpolicycontrol/") and target == "PCF":
            return "Npcf SM policy"
        if lower_path.startswith("/nudm-sdm/") and target == "UDM":
            return "Nudm subscriber data"
        if lower_path.startswith("/nudr-dr/v2/policy-data") and target == "UDR":
            return "Nudr policy data"
        if lower_path.startswith("/nudr-dr/v2/application-data") and target == "UDR":
            return "Nudr app influence data"
        if lower_path.startswith("/nudr-dr/v2/subscription-data") and target == "UDR":
            return "Nudr subscription data"
        if lower_path.startswith("/nnrf-") and target == "NRF":
            return "Nnrf discovery/management"
        if lower_path.startswith("/nnef-") and target == "NEF":
            return "Nnef exposure"
        return None

    @staticmethod
    def add_control_observation(
        observations: dict[tuple[str, str, str], dict[str, Any]],
        source_node: str | None,
        target_node: str | None,
        protocol: str | None,
        evidence: str,
    ) -> None:
        if not source_node or not target_node or not protocol or source_node == target_node:
            return
        key = (source_node, target_node, protocol)
        entry = observations.setdefault(
            key,
            {
                "sourceNodeId": source_node,
                "targetNodeId": target_node,
                "protocol": protocol,
                "evidenceCount": 0,
                "evidence": [],
            },
        )
        entry["evidenceCount"] = int(entry.get("evidenceCount") or 0) + 1
        samples = entry.setdefault("evidence", [])
        if len(samples) < 3:
            samples.append(evidence[:220])

    def control_plane_runtime_metrics(self, k8s: EksKubernetesClient, pods: list[dict[str, Any]]) -> dict[str, Any]:
        ip_to_node: dict[str, str] = {}
        control_pods: list[dict[str, str]] = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            status = pod.get("status") or {}
            component = self.component_from_pod(pod)
            node_id = self.control_plane_node_for_component(component)
            name = metadata.get("name") or ""
            pod_ip = status.get("podIP") or ""
            phase = status.get("phase") or "Unknown"
            if node_id and pod_ip:
                ip_to_node[str(pod_ip)] = node_id
            if node_id and name and phase == "Running":
                control_pods.append({"name": str(name), "component": str(component or ""), "nodeId": node_id})

        if not control_pods:
            return {}

        observations: dict[tuple[str, str, str], dict[str, Any]] = {}
        access_log_pattern = re.compile(
            r"\|\s*(?P<status>\d{3})\s*\|\s*(?P<client>\d{1,3}(?:\.\d{1,3}){3})\s*\|\s*(?P<method>[A-Z]+)\s*\|\s*(?P<path>/[^|\s]*)"
        )
        for pod in control_pods:
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{pod['name']}/log?sinceSeconds={self._control_plane_log_window_seconds}&tailLines=500",
                ignore_404=True,
            )
            if status >= 300 or not log_text:
                continue
            target_node = pod["nodeId"]
            target_component = pod["component"]
            for line in log_text.splitlines():
                match = access_log_pattern.search(line)
                if match:
                    protocol = self.protocol_for_nf_path(match.group("path"), target_component)
                    source_node = ip_to_node.get(match.group("client"))
                    if protocol and source_node:
                        evidence = f"{pod['name']} {match.group('method')} {match.group('path')} from {match.group('client')} -> {match.group('status')}"
                        self.add_control_observation(observations, source_node, target_node, protocol, evidence)
                    continue
                lower_line = line.lower()
                if target_component == "AMF" and (
                    "initial registration is successful" in lower_line
                    or "handle registration" in lower_line
                    or "initialuemessage" in lower_line
                ):
                    self.add_control_observation(observations, self.gnb_for_source_node(""), "amf", "N2/NAS registration", f"{pod['name']} {line.strip()}")
                if target_component == "SMF" and "pfcp" in lower_line and (
                    "session establishment" in lower_line or "send" in lower_line or "handle" in lower_line
                ):
                    self.add_control_observation(observations, "smf", "upf", "N4 PFCP session", f"{pod['name']} {line.strip()}")

        # NEF's own pod log never emits an access-log line for its northbound (Nnef)
        # API: free5GC's NEF only logs Main/CFG/CTX/SBI startup lines, so the
        # access_log_pattern parsing above can never observe AS-session-with-QoS or
        # PFD management calls. Use ToolGateway's recorded successful NEF tool
        # invocations as the observation source instead. These are read from
        # DynamoDB (not the in-process nef_tool_hits list) because event execution
        # runs in a separate async Lambda container from the one serving this status
        # read, so the two never share memory.
        nef_node_id = self.control_plane_node_for_component("NEF")
        if nef_node_id and self.events and any(pod["nodeId"] == nef_node_id for pod in control_pods):
            for hit in self.events.recent_nef_tool_hits():
                evidence = f"tool_gateway {hit['tool']} -> {hit['api']} succeeded at {hit['observedAt']}"
                self.add_control_observation(observations, nef_node_id, "pcf", hit["protocol"], evidence)

        traffic = []
        for index, entry in enumerate(observations.values()):
            traffic.append(
                {
                    "id": f"cp-actual-{entry['sourceNodeId']}-{entry['targetNodeId']}-{index}",
                    **entry,
                    "lastObservedAt": TimeUtils.now(),
                    "lastObservedEpochMillis": TimeUtils.epoch_millis(),
                }
            )
        return {"controlPlaneTraffic": traffic} if traffic else {}

    @staticmethod
    def pod_reason(pod: dict[str, Any]) -> str:
        status = pod.get("status") or {}
        if status.get("reason"):
            return str(status.get("reason"))
        container_statuses = status.get("containerStatuses") or []
        reasons: list[str] = []
        for container in container_statuses:
            state = container.get("state") or {}
            for state_name in ("waiting", "terminated", "running"):
                detail = state.get(state_name)
                if not isinstance(detail, dict):
                    continue
                reason = detail.get("reason")
                message = detail.get("message")
                if reason or message:
                    reasons.append(": ".join(str(item) for item in (reason, message) if item))
        if reasons:
            return "; ".join(reasons[:2])
        conditions = status.get("conditions") or []
        for condition in conditions:
            if condition.get("status") == "False" and condition.get("reason"):
                return str(condition.get("reason"))
        return ""

    def ueransim_runtime_metrics(self, k8s: EksKubernetesClient, pods: list[dict[str, Any]]) -> dict[str, Any]:
        registered: set[str] = set()
        pdu_sessions: set[str] = set()
        attributed_pdu_sessions: set[str] = set()
        slice_sessions: dict[int, int] = {}
        tun_error_count = 0
        probe_samples: list[dict[str, Any]] = []
        citizen_probe_sample: dict[str, Any] | None = None
        for pod in pods:
            metadata = pod.get("metadata") or {}
            labels = metadata.get("labels") or {}
            name = metadata.get("name") or ""
            phase = ((pod.get("status") or {}).get("phase") or "Unknown")
            if phase not in {"Running", "Succeeded"} or "ueransim" not in name.lower() or "gnb" in name.lower():
                continue
            container_names = [
                container.get("name", "")
                for container in (((pod.get("spec") or {}).get("containers") or []))
                if container.get("name")
            ]
            ue_container = "ueransim-ue" if "ueransim-ue" in container_names else container_names[0] if container_names else ""
            if not ue_container:
                continue
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{name}/log?container={ue_container}&tailLines=1000",
                ignore_404=True,
            )
            scenario = labels.get("5gcityverse.io/scenario") or self.scenario_from_pod_name(name)
            cfg = EVENT_CONFIG.get(scenario or "")
            sst = cfg.slice_sst if cfg else self.sst_from_runtime_label(str(scenario or ""))
            if status < 300 and log_text:
                tun_error_count += log_text.count("TUN allocation failure")
                # UERANSIM prefixes log lines with "[<imsi>|nas]" only when a single
                # container multiplexes several UEs. Single-UE containers (the common
                # case for our per-scenario pods) just log "[nas]" with no index, so
                # the numeric prefix must be optional; fall back to the pod name as
                # the dedup key in that case.
                for imsi in re.findall(r"\[(?:(\d+)\|)?nas\].*Initial Registration is successful", log_text):
                    registered.add(imsi or name)
                for imsi in re.findall(r"\[(?:(\d+)\|)?nas\].*PDU Session establishment is successful", log_text):
                    session_key = imsi or name
                    pdu_sessions.add(session_key)
                    if sst is not None and session_key not in attributed_pdu_sessions:
                        attributed_pdu_sessions.add(session_key)
                        slice_sessions[sst] = slice_sessions.get(sst, 0) + 1
            if "ue-tun-probe" in container_names:
                probe_status, probe_log = k8s.request_text(
                    "GET",
                    f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{name}/log?container=ue-tun-probe&tailLines=20",
                    ignore_404=True,
                )
                if probe_status < 300 and probe_log:
                    sample = self.latest_ue_tun_probe_sample(probe_log)
                    if sample:
                        probe_samples.append(sample)
                        if name.startswith(f"{self.settings.ueransim_ue_deployment}-"):
                            citizen_probe_sample = sample
        if not registered and not pdu_sessions and not tun_error_count and not probe_samples:
            return {}
        throughput = sum(float(sample.get("throughputMbps") or 0.0) for sample in probe_samples)
        latency_values = [float(sample.get("latencyMs") or 0.0) for sample in probe_samples if float(sample.get("latencyMs") or 0.0) > 0]
        received_packets = sum(int(sample.get("receivedPackets") or 0) for sample in probe_samples)
        return {
            "registeredUeCount": len(registered),
            "pduSessionCount": len(pdu_sessions),
            "sliceSessions": slice_sessions,
            "unassignedSessionCount": max(0, len(pdu_sessions) - len(attributed_pdu_sessions)),
            "tunErrorCount": tun_error_count,
            "throughputMbps": round(throughput, 3),
            "latencyMs": round(sum(latency_values) / len(latency_values), 2) if latency_values else 0.0,
            "gtpPacketsPerSec": received_packets,
            "ueTunProbe": citizen_probe_sample or {},
            "timestamp": TimeUtils.epoch_millis(),
            "dataSource": "eks+ue-tun-probe" if citizen_probe_sample else "eks+ueransim-logs",
            "evidenceLevel": EvidenceLevel.MEASURED.value,
        }

    def iperf3_runtime_metrics(self, k8s: EksKubernetesClient, pods: list[dict[str, Any]]) -> dict[str, Any]:
        client_candidates: list[dict[str, Any]] = []
        server_candidates: list[dict[str, Any]] = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            labels = metadata.get("labels") or {}
            name = metadata.get("name") or ""
            phase = ((pod.get("status") or {}).get("phase") or "Unknown")
            # Completed event jobs remain in Kubernetes briefly for TTL/log inspection.
            # They are historical evidence, not current traffic, and must never leak
            # into the live network snapshot after a round has ended.
            if phase != "Running":
                continue
            container_names = [
                container.get("name", "")
                for container in (((pod.get("spec") or {}).get("containers") or []))
                if container.get("name")
            ]
            component = self.component_from_pod(pod)
            resident_baseline = "resident-baseline-iperf3" in container_names
            if component != "IPERF3" and "iperf3-client" not in container_names and "iperf3-server" not in container_names and not resident_baseline:
                continue
            started_at = ((pod.get("status") or {}).get("startTime") or "")
            item = {
                "name": name,
                "scenario": labels.get("5gcityverse.io/scenario") or ("baseline" if resident_baseline else None),
                "startedAt": started_at,
                "phase": phase,
                "containers": container_names,
                "transport": "free5gc-tun" if (
                    resident_baseline
                    or ("ueransim-ue" in container_names and "iperf3-client" in container_names)
                ) else "cluster-network",
                "interface": "uesimtun0" if resident_baseline or "ueransim-ue" in container_names else None,
            }
            if "server" in name.lower() or "iperf3-server" in container_names:
                server_candidates.append(item)
            else:
                client_candidates.append(item)

        scenario_samples: list[dict[str, Any]] = []
        sorted_clients = sorted(
            client_candidates,
            key=lambda entry: (1 if entry.get("phase") == "Running" else 0, str(entry.get("startedAt") or "")),
            reverse=True,
        )
        for item in sorted_clients:
            container = (
                "resident-baseline-iperf3"
                if "resident-baseline-iperf3" in item.get("containers", [])
                else "iperf3-client" if "iperf3-client" in item.get("containers", []) else ""
            )
            query = f"container={container}&" if container else ""
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{item['name']}/log?{query}sinceSeconds={self._scenario_traffic_log_window_seconds}&tailLines=1200",
                ignore_404=True,
            )
            if status >= 300 or not log_text:
                continue
            sample = self.parse_iperf3_sample(log_text)
            if not sample:
                continue
            throughput_mbps = round(float(sample.get("bitsPerSecond") or 0.0) / 1_000_000, 3)
            if throughput_mbps <= 0:
                continue
            if item.get("scenario") != "baseline" and item.get("transport") != "free5gc-tun":
                continue
            scenario_samples.append(self.iperf3_scenario_sample(item, sample, throughput_mbps, "client-json"))
        for item in sorted(server_candidates, key=lambda entry: str(entry.get("startedAt") or ""), reverse=True):
            container = "iperf3-server" if "iperf3-server" in item.get("containers", []) else ""
            query = f"container={container}&" if container else ""
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{item['name']}/log?{query}sinceSeconds={self._scenario_traffic_log_window_seconds}&tailLines=1200",
                ignore_404=True,
            )
            if status >= 300 or not log_text:
                continue
            sample = self.parse_iperf3_server_sample(log_text)
            if not sample:
                continue
            throughput_mbps = round(float(sample.get("bitsPerSecond") or 0.0) / 1_000_000, 3)
            if throughput_mbps <= 0:
                continue
            scenario_samples.append(self.iperf3_scenario_sample(item, sample, throughput_mbps, "server-log"))
        if not scenario_samples:
            return {}
        by_scenario: dict[str, dict[str, Any]] = {}
        for sample in scenario_samples:
            scenario = str(sample.get("scenario") or "baseline")
            if scenario in by_scenario and str(sample.get("source")) == "server-log":
                continue
            by_scenario[scenario] = sample
        samples = list(by_scenario.values())
        total_mbps = round(sum(float(sample.get("throughputMbps") or 0.0) for sample in samples), 3)
        user_plane_mbps = round(sum(
            float(sample.get("throughputMbps") or 0.0)
            for sample in samples
            if sample.get("transport") == "free5gc-tun"
        ), 3)
        latest = samples[0]
        # iperf3 only reports a datagram/packets count for UDP tests; TCP tests (the
        # common case here) never populate packetsPerSecond, so a naive sum defaults
        # to 0 and falsely implies zero GTP packet activity alongside real throughput.
        # Surface that as missing evidence instead of a fabricated zero.
        has_packet_evidence = any(sample.get("packetsPerSecond") is not None for sample in samples)
        result = {
            "throughputMbps": total_mbps,
            "uplinkMbps": total_mbps,
            "iperf3Mbps": total_mbps,
            "iperf3": latest,
            "scenarioTraffic": samples,
            "userPlaneThroughputMbps": user_plane_mbps,
            "nonUserPlaneThroughputMbps": round(max(0.0, total_mbps - user_plane_mbps), 3),
            "dataSource": "eks+iperf3",
            "evidenceLevel": EvidenceLevel.MEASURED.value,
            "timestamp": TimeUtils.epoch_millis(),
        }
        if has_packet_evidence:
            result["gtpPacketsPerSec"] = round(
                sum(float(sample.get("packetsPerSecond") or 0.0) for sample in samples), 3
            )
        else:
            result["gtpPacketsPerSec"] = None
            result["gtpPacketsSource"] = "unavailable"
        return result
        return {}

    def gtp5g_metrics_exporter_runtime_metrics(
        self, k8s: EksKubernetesClient, pods: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Read fresh, measured packet counters emitted by the gtp5g exporter.

        A DaemonSet can briefly have overlapping exporter pods during a rollout.
        Samples are therefore de-duplicated by the observed UPF pod and slice,
        rather than by exporter pod, before totals are calculated.
        """
        samples: list[dict[str, Any]] = []
        log_window_seconds = max(self._gtp5g_metrics_max_age_seconds + 10, 30)
        for exporter in pods:
            metadata = exporter.get("metadata") or {}
            labels = metadata.get("labels") or {}
            phase = ((exporter.get("status") or {}).get("phase") or "Unknown")
            name = str(metadata.get("name") or "")
            if phase != "Running" or labels.get("app.kubernetes.io/name") != "gtp5g-metrics-exporter" or not name:
                continue
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.settings.free5gc_namespace}/pods/{name}/log?"
                f"sinceSeconds={log_window_seconds}&tailLines=100",
                ignore_404=True,
            )
            if status >= 300 or not log_text:
                continue
            samples.extend(
                self.parse_gtp5g_metrics_samples(
                    log_text,
                    max_age_seconds=self._gtp5g_metrics_max_age_seconds,
                )
            )

        latest_by_upf_slice: dict[tuple[str, str], dict[str, Any]] = {}
        for sample in samples:
            key = (sample["pod"], sample["slice"])
            previous = latest_by_upf_slice.get(key)
            if previous is None or sample["timestampEpochMs"] > previous["timestampEpochMs"]:
                latest_by_upf_slice[key] = sample
        current_samples = list(latest_by_upf_slice.values())
        if not current_samples:
            return {}

        metric_names = (
            "rxPackets",
            "txPackets",
            "rxPps",
            "txPps",
            "gtpPacketsPerSec",
            "rxDropsDelta",
            "txDropsDelta",
            "rxErrorsDelta",
            "txErrorsDelta",
        )

        def aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                metric: round(sum(float(entry[metric]) for entry in entries), 3)
                for metric in metric_names
            }

        samples_by_slice: dict[str, list[dict[str, Any]]] = {}
        for sample in current_samples:
            samples_by_slice.setdefault(sample["slice"], []).append(sample)
        total = aggregate(current_samples)
        latest_timestamp_ms = max(int(sample["timestampEpochMs"]) for sample in current_samples)
        return {
            "gtpPacketsPerSec": total["gtpPacketsPerSec"],
            "gtpPacketsSource": "gtp5g-upfgtp-interface",
            "gtpMetrics": {
                "source": "gtp5g-upfgtp-interface",
                "timestamp": latest_timestamp_ms,
                "sampleCount": len(current_samples),
                "total": total,
                "perSlice": {
                    slice_name: aggregate(entries)
                    for slice_name, entries in sorted(samples_by_slice.items())
                },
            },
        }

    @staticmethod
    def parse_gtp5g_metrics_samples(
        log_text: str,
        *,
        now_epoch_seconds: float | None = None,
        max_age_seconds: int = 20,
    ) -> list[dict[str, Any]]:
        """Parse only complete, recent, non-negative exporter JSON samples."""
        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        numeric_fields = (
            "rxPackets",
            "txPackets",
            "rxPps",
            "txPps",
            "gtpPacketsPerSec",
            "rxDropsDelta",
            "txDropsDelta",
            "rxErrorsDelta",
            "txErrorsDelta",
        )
        parsed: list[dict[str, Any]] = []
        for line in log_text.splitlines():
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("source") != "gtp5g-upfgtp-interface":
                continue
            pod = payload.get("pod")
            slice_name = payload.get("slice")
            if not isinstance(pod, str) or not pod.strip() or not isinstance(slice_name, str) or not slice_name.strip():
                continue
            timestamp = payload.get("timestamp")
            try:
                if isinstance(timestamp, bool):
                    raise ValueError("boolean timestamp")
                if isinstance(timestamp, (int, float)):
                    timestamp_seconds = float(timestamp)
                    if timestamp_seconds > 10_000_000_000:
                        timestamp_seconds /= 1000.0
                elif isinstance(timestamp, str):
                    parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed_timestamp.tzinfo is None:
                        parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
                    timestamp_seconds = parsed_timestamp.astimezone(timezone.utc).timestamp()
                else:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            age_seconds = now - timestamp_seconds
            if not math.isfinite(timestamp_seconds) or age_seconds < -5 or age_seconds > max_age_seconds:
                continue
            normalized: dict[str, Any] = {
                "timestamp": timestamp,
                "timestampEpochMs": int(timestamp_seconds * 1000),
                "pod": pod.strip(),
                "slice": slice_name.strip(),
                "source": "gtp5g-upfgtp-interface",
            }
            valid = True
            for field in numeric_fields:
                value = payload.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    valid = False
                    break
                numeric_value = float(value)
                if not math.isfinite(numeric_value) or numeric_value < 0:
                    valid = False
                    break
                normalized[field] = value
            if valid:
                parsed.append(normalized)
        return parsed

    @staticmethod
    def iperf3_scenario_sample(item: dict[str, Any], sample: dict[str, Any], throughput_mbps: float, source: str) -> dict[str, Any]:
        return {
            "pod": item["name"],
            "scenario": sample.get("scenario") or item.get("scenario") or "baseline",
            "source": source,
            "transport": item.get("transport") or "unknown",
            "interface": item.get("interface"),
            "throughputMbps": throughput_mbps,
            "uplinkMbps": throughput_mbps,
            "downlinkMbps": 0.0,
            "bitsPerSecond": sample.get("bitsPerSecond"),
            "packetsPerSecond": sample.get("packetsPerSecond"),
            "jitterMs": sample.get("jitterMs"),
            "lostPercent": sample.get("lostPercent"),
        }

    @staticmethod
    def parse_iperf3_sample(log_text: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        payloads: list[dict[str, Any]] = []
        index = 0
        while index < len(log_text):
            start = log_text.find("{", index)
            if start < 0:
                break
            try:
                payload, end = decoder.raw_decode(log_text[start:])
            except json.JSONDecodeError:
                index = start + 1
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
            index = start + max(end, 1)
        for payload in reversed(payloads):
            end_data = payload.get("end") if isinstance(payload, dict) else {}
            if not isinstance(end_data, dict):
                continue
            for key in ("sum", "sum_received", "sum_sent"):
                sample = end_data.get(key)
                if isinstance(sample, dict) and sample.get("bits_per_second") is not None:
                    seconds = float(sample.get("seconds") or 0.0)
                    packets = int(sample.get("packets") or 0)
                    return {
                        "bitsPerSecond": sample.get("bits_per_second"),
                        "packetsPerSecond": round(packets / seconds, 3) if seconds > 0 and packets > 0 else None,
                        "jitterMs": sample.get("jitter_ms"),
                        "lostPercent": sample.get("lost_percent"),
                    }
        return CityVerseBackendApp.parse_iperf3_interval_sample(log_text)

    @staticmethod
    def parse_iperf3_server_sample(log_text: str) -> dict[str, Any] | None:
        return CityVerseBackendApp.parse_iperf3_interval_sample(log_text)

    @staticmethod
    def parse_iperf3_interval_sample(log_text: str) -> dict[str, Any] | None:
        samples: list[dict[str, Any]] = []
        rate_units = {"Kbits/sec": 1_000, "Mbits/sec": 1_000_000, "Gbits/sec": 1_000_000_000}
        interval_pattern = re.compile(
            r"\[\s*(?P<stream_id>\d+|SUM)\]\s+(?P<start>\d+(?:\.\d+)?)-\s*(?P<end>\d+(?:\.\d+)?)\s+sec\s+"
            r"(?P<transfer>[\d.]+)\s+\w+Bytes\s+"
            r"(?P<rate>[\d.]+)\s+(?P<unit>[KMG]bits/sec)"
            r"(?:\s+(?P<jitter>[\d.]+)\s+ms\s+(?P<lost>\d+)/(?P<total>\d+)\s+\((?P<lost_percent>[\d.]+)%\))?"
        )
        scenario_pattern = re.compile(r"(?:5gcityverse\.io/scenario|SCENARIO)[=:](?P<scenario>[a-z0-9_-]+)")
        for line in log_text.splitlines():
            match = interval_pattern.search(line)
            if not match:
                continue
            unit = match.group("unit")
            bits_per_second = float(match.group("rate")) * rate_units[unit]
            lost_percent = match.group("lost_percent")
            total_packets = match.group("total")
            interval_seconds = max(float(match.group("end")) - float(match.group("start")), 0.001)
            samples.append(
                {
                    "bitsPerSecond": bits_per_second,
                    "streamId": match.group("stream_id"),
                    "jitterMs": float(match.group("jitter")) if match.group("jitter") else None,
                    "lostPercent": float(lost_percent) if lost_percent is not None else None,
                    "packetsPerSecond": round(int(total_packets) / interval_seconds, 3) if total_packets else None,
                }
            )
        if not samples:
            return None
        # A long UDP run can emit a short zero-rate tail while its server is being
        # reconciled. Keep the newest measured interval instead of treating that
        # transient tail as proof that the entire scenario produced no traffic.
        sample = next(
            (candidate for candidate in reversed(samples) if float(candidate.get("bitsPerSecond") or 0) > 0),
            samples[-1],
        )
        scenario_match = scenario_pattern.search(log_text)
        if scenario_match:
            sample["scenario"] = scenario_match.group("scenario")
        return sample

    @staticmethod
    def latest_ue_tun_probe_sample(log_text: str) -> dict[str, Any] | None:
        for line in reversed(log_text.splitlines()):
            marker = "UE_TUN_METRICS "
            if marker not in line:
                continue
            raw = line.split(marker, 1)[1].strip()
            try:
                sample = json.loads(raw)
            except json.JSONDecodeError:
                continue
            return sample if sample.get("ready") is True else None
        return None

    def slices_from_runtime_metrics(self, slices: list[dict[str, Any]], metrics: dict[str, Any]) -> list[dict[str, Any]]:
        slice_sessions = metrics.get("sliceSessions") or {}
        if not slice_sessions:
            return slices
        updated = [dict(item) for item in slices]
        total_sessions = sum(int(value or 0) for value in slice_sessions.values()) or int(metrics.get("pduSessionCount") or 0) or 0
        source = metrics.get("dataSource")
        if source and source != "unavailable":
            for item in updated:
                item["dataSource"] = source
                item["loadSource"] = "eks-runtime-logs"
                item["evidenceLevel"] = EvidenceLevel.MEASURED.value
        by_sst = {item.get("sst"): item for item in updated}
        for raw_sst, sessions in slice_sessions.items():
            try:
                sst = int(raw_sst)
            except (TypeError, ValueError):
                continue
            item = by_sst.get(sst)
            if not item:
                continue
            item["sessions"] = sessions
            if total_sessions > 0:
                item["load"] = round(int(sessions or 0) / total_sessions * 100)
            item["trend"] = "up"
            item["dataSource"] = source or "eks+ueransim-logs"
            item["loadSource"] = "eks-runtime-logs"
            item["evidenceLevel"] = EvidenceLevel.MEASURED.value
            if int(sessions or 0) > 0 or int(item.get("load") or 0) > 0:
                item["selectionStage"] = "active-session"
        return updated

    @staticmethod
    def scenario_from_pod_name(name: str) -> str | None:
        if "baseline-embb" in name:
            return "baseline-embb"
        if "baseline-mmtc" in name:
            return "baseline-mmtc"
        if "baseline-urllc" in name:
            return "baseline-urllc"
        if "baseline-v2x" in name:
            return "baseline-v2x"
        if "iot" in name:
            return "iot_surge"
        if "typhoon" in name:
            return "typhoon"
        return None

    @staticmethod
    def sst_from_runtime_label(label: str) -> int | None:
        return {
            "baseline-embb": 1,
            "baseline-urllc": 2,
            "baseline-mmtc": 3,
            "baseline-v2x": 4,
        }.get(label)

    @staticmethod
    def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
        return {"statusCode": status, "headers": DEFAULT_CORS_HEADERS, "body": json.dumps(DynamoDbCodec.from_dynamodb(body))}
