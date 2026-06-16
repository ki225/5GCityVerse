from __future__ import annotations

import json
import uuid
from typing import Any

import boto3

from config import EVENT_CONFIG, AppSettings
from constants import ApiRoute, DEFAULT_CORS_HEADERS, DynamoKeys
from decision_service import AgentDecisionService
from dynamodb_codec import DynamoDbCodec
from event_repository import EventRepository
from free5gc_utils import Free5gcClient
from metrics_service import PrometheusMetricsService
from models import TriggerRequest, ValidationError
from scenario_environment import ScenarioEnvironmentService
from slice_catalog import SliceCatalog
from time_utils import TimeUtils
from websocket_service import WebSocketConnectionService


class CityVerseBackendApp:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()
        dynamodb = boto3.resource("dynamodb")
        self.table = dynamodb.Table(self.settings.dynamodb_table)
        self.websocket = WebSocketConnectionService(
            self.table,
            boto3.client("apigatewaymanagementapi", endpoint_url=self.settings.apigw_ws_endpoint),
        )
        self.events = EventRepository(self.table)
        self.metrics = PrometheusMetricsService(self.settings.prometheus_url)
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
        )
        self.decisions = AgentDecisionService()

    def handle(self, event: dict[str, Any], _context: Any) -> dict[str, Any]:
        route_key = event.get("requestContext", {}).get("routeKey", "")
        connection_id = event.get("requestContext", {}).get("connectionId")
        if connection_id and route_key in {ApiRoute.WS_CONNECT.value, ApiRoute.WS_DISCONNECT.value, ApiRoute.WS_DEFAULT.value}:
            return self.handle_ws(event, route_key)

        method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
        path = event.get("rawPath", event.get("path", "/"))

        try:
            if method == "OPTIONS":
                return self.response(204, {})
            if method == "POST" and path.endswith("/events/trigger"):
                return self.handle_trigger(event)
            if method == "POST" and path.endswith("/events/reset"):
                return self.handle_reset()
            if method == "GET" and "/events/status/" in path:
                execution_id = path.rsplit("/", 1)[-1]
                item = self.events.get_status(execution_id)
                if not item:
                    return self.response(404, {"error": "Execution not found"})
                return self.response(200, item)
            if method == "GET" and path.endswith("/free5gc/status"):
                return self.response(200, self.free5gc.status_payload())
            if method == "GET" and path.endswith("/network/slices"):
                return self.response(200, self.free5gc.current_slices())
            if method == "GET" and path.endswith("/metrics/current"):
                return self.response(200, self.metrics.current_metrics())
            return self.response(404, {"error": "Not found", "path": path})
        except Exception as exc:
            print(f"Unhandled backend error: {exc}")
            return self.response(500, {"error": "Internal server error"})

    def handle_ws(self, event: dict[str, Any], route_key: str) -> dict[str, Any]:
        connection_id = event.get("requestContext", {}).get("connectionId", "")
        if route_key == ApiRoute.WS_CONNECT.value:
            return self.websocket.connect(connection_id)
        if route_key == ApiRoute.WS_DISCONNECT.value:
            return self.websocket.disconnect(connection_id)
        return self.websocket.handle_default(connection_id, event)

    def handle_trigger(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            body = json.loads(event.get("body") or "{}")
            request = TriggerRequest(**body)
            event_type = request.event_type.value if hasattr(request.event_type, "value") else request.event_type
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            return self.response(400, {"error": f"Invalid trigger request: {exc}"})

        if event_type not in EVENT_CONFIG:
            return self.response(400, {"error": f"Unknown event_type: {event_type}"})

        execution_id = str(uuid.uuid4())
        cfg = EVENT_CONFIG[event_type]
        observed_metrics = self.metrics.current_metrics()
        projected_metrics = self.event_metrics(event_type)
        projected_slices = SliceCatalog.event_slices(event_type)
        free5gc_result = self.free5gc.upsert_subscribers(
            event_type,
            cfg,
            execution_id,
            self.settings.runtime_subscriber_upsert_limit,
        )
        environment_result = self.environment.trigger(event_type, cfg)
        decision = self.decisions.build_decision(
            event_type,
            cfg,
            free5gc_result,
            environment_result,
            observed_metrics,
            projected_metrics,
            projected_slices,
        )
        item = {
            "pk": f"EVENT#{execution_id}",
            "sk": DynamoKeys.STATUS.value,
            "executionId": execution_id,
            "eventType": event_type,
            "status": "AGENT_COMPLETE",
            "config": cfg.to_dict(),
            "agentDecision": decision,
            "started_at": TimeUtils.now(),
            "mcp": {"terraform_mcp_used_for_iac": True, "free5gc_runtime_mode": "eks-webui-api"},
            "free5gc": free5gc_result,
            "environment": environment_result,
        }
        self.events.put_status(item)

        self.websocket.broadcast({"type": "event_started", "payload": {"executionId": execution_id, "eventType": event_type}})
        self.websocket.broadcast({"type": "agent_decision", "payload": decision})
        self.websocket.broadcast({"type": "metrics_update", "payload": projected_metrics})
        self.websocket.broadcast({"type": "slice_update", "payload": projected_slices})
        self.websocket.broadcast({"type": "free5gc_status", "payload": self.free5gc.status_payload()})
        self.websocket.broadcast(
            {
                "type": "pod_event",
                "payload": {
                    "event": "ADDED",
                    "pod": "upf-aws-demo-2",
                    "phase": "Running",
                    "component": "UPF",
                    "namespace": self.settings.free5gc_namespace,
                    "timestamp": TimeUtils.now(),
                },
            }
        )
        return self.response(200, {"executionId": execution_id, "eventType": event_type, "environment": environment_result})

    def handle_reset(self) -> dict[str, Any]:
        free5gc_reset = self.free5gc.reset_subscribers()
        self.websocket.broadcast({"type": "slice_update", "payload": SliceCatalog.default_slices()})
        self.websocket.broadcast({"type": "metrics_update", "payload": self.metrics.default_metrics()})
        self.websocket.broadcast({"type": "free5gc_status", "payload": self.free5gc.status_payload()})
        return self.response(200, {"status": "reset", "free5gc": free5gc_reset})

    def event_metrics(self, event_type: str) -> dict[str, Any]:
        base = self.metrics.default_metrics()
        cfg = EVENT_CONFIG[event_type]
        base.update(
            {
                "upfCpuPercent": 72.0 if event_type == "concert" else 46.0,
                "upfPodCount": 2,
                "amfPodCount": 2 if event_type in ("typhoon", "iot_surge") else 1,
                "gtpPacketsPerSec": 2200,
                "pduSessionCount": cfg.ue_count,
                "throughputMbps": 820.0 if event_type == "concert" else 260.0,
                "timestamp": TimeUtils.epoch_millis(),
            }
        )
        return base

    @staticmethod
    def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
        return {"statusCode": status, "headers": DEFAULT_CORS_HEADERS, "body": json.dumps(DynamoDbCodec.from_dynamodb(body))}

