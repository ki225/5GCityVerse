from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

import boto3

from models import EventConfig
from agent_runtime.data_plane_evidence import DataPlaneEvidence
from agent_runtime.sla_verifier import SlaVerifier
from eks_kubernetes_client import get_eks_client
from time_utils import TimeUtils


class ToolGateway:
    # NEF-backed tools: successful calls are the only evidence NEF's northbound
    # (Nnef) API was exercised, since the NEF pod's own logs never emit an access-log
    # line for these calls (see control_plane_runtime_metrics in app.py).
    NEF_TOOL_PROTOCOLS = {
        "activate_qos_policy": "Nnef AS-session-with-QoS",
        "request_traffic_influence": "Nnef AS-session-with-QoS",
        "create_pfd_rule": "Nnef PFD management",
    }
    NEF_TOOL_HITS_LIMIT = 20

    def __init__(
        self,
        metrics: Any,
        free5gc: Any,
        environment: Any,
        current_metrics: Callable[[], dict[str, Any]],
        current_slices: Callable[[], list[dict[str, Any]]],
        runtime_subscriber_upsert_limit: int,
        lambda_function_names: dict[str, str] | None = None,
        max_hpa_replicas: int = 4,
        lambda_client: Any | None = None,
        record_hit: Callable[[dict[str, Any]], None] | None = None,
        evidence_reader: Any | None = None,
        qer_actuator: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.metrics = metrics
        self.free5gc = free5gc
        self.environment = environment
        self.current_metrics = current_metrics
        self.current_slices = current_slices
        self.runtime_subscriber_upsert_limit = runtime_subscriber_upsert_limit
        self.lambda_function_names = lambda_function_names or {}
        self.lambda_client = lambda_client
        self.max_hpa_replicas = max_hpa_replicas
        self.verifier = SlaVerifier()
        self.nef_tool_hits: list[dict[str, Any]] = []
        # Persists hits to DynamoDB (see EventRepository.record_nef_tool_hit) so
        # control_plane_runtime_metrics can read them from the API Lambda container,
        # since event execution runs in a separate async Lambda container that does
        # not share this in-process nef_tool_hits list.
        self.record_hit = record_hit
        self.evidence_reader = evidence_reader
        self.qer_actuator = qer_actuator or self.actuate_pfcp_qer

    def call(self, tool: str, params: dict[str, Any], cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        tool = self.normalize_tool_name(tool)
        handlers = {
            "get_network_analytics": self.get_network_analytics,
            "list_subscribers": self.list_subscribers,
            "upsert_subscriber_profile": self.upsert_subscriber_profile,
            "activate_qos_policy": self.activate_qos_policy,
            "request_traffic_influence": self.request_traffic_influence,
            "create_pfd_rule": self.create_pfd_rule,
            "patch_hpa": self.patch_hpa,
            "verify_sla": self.verify_sla,
        }
        handler = handlers.get(tool)
        if not handler:
            return {"status": "failed", "error": f"Unknown tool: {tool}", "tool": tool}
        started_at = TimeUtils.now()
        try:
            result = handler(params, cfg, intent)
            result.setdefault("status", "success")
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
        result.update({"tool": tool, "startedAt": started_at, "completedAt": TimeUtils.now()})
        if result.get("status") == "success" and tool in self.NEF_TOOL_PROTOCOLS:
            self.record_nef_tool_hit(tool, result, intent)
        return result

    def record_nef_tool_hit(self, tool: str, result: dict[str, Any], intent: dict[str, Any]) -> None:
        hit = {
            "tool": tool,
            "protocol": self.NEF_TOOL_PROTOCOLS[tool],
            "api": result.get("api", tool),
            "observedAt": result.get("completedAt") or TimeUtils.now(),
        }
        self.nef_tool_hits.append(hit)
        if len(self.nef_tool_hits) > self.NEF_TOOL_HITS_LIMIT:
            del self.nef_tool_hits[: len(self.nef_tool_hits) - self.NEF_TOOL_HITS_LIMIT]
        if self.record_hit:
            self.record_hit(
                {
                    "protocol": hit["protocol"],
                    "tool": tool,
                    "api": hit["api"],
                    "executionId": intent.get("executionId"),
                    "observedAt": hit["observedAt"],
                    "at": TimeUtils.epoch_millis(),
                }
            )

    @staticmethod
    def normalize_tool_name(tool: str) -> str:
        aliases = {
            "activate_qos_subscription": "activate_qos_policy",
            "scale_upf_pods": "patch_hpa",
        }
        return aliases.get(tool.strip("/").replace("-", "_"), tool.strip("/").replace("-", "_"))

    def get_network_analytics(self, _params: dict[str, Any], _cfg: EventConfig, _intent: dict[str, Any]) -> dict[str, Any]:
        """Return current network metrics/slices.

        Naming note: despite the tool name, this is an in-app heuristic derived from
        Prometheus/K8s/pod-log observations (see current_metrics/current_slices), not
        a call to a 3GPP-standard NWDAF Network Function. No NWDAF is deployed in this
        project.
        """
        metrics = self.current_metrics()
        slices = self.current_slices()
        return {
            "status": "success",
            "metrics": metrics,
            "slices": slices,
            "analyticsSource": "in-app heuristic (Prometheus-derived); not a 3GPP NWDAF NF",
        }

    def list_subscribers(self, _params: dict[str, Any], _cfg: EventConfig, _intent: dict[str, Any]) -> dict[str, Any]:
        subscribers = self.free5gc.list_subscribers()
        cityverse = [item for item in subscribers if self.free5gc.is_cityverse_subscriber(item)]
        return {
            "status": "success",
            "subscriberCount": len(subscribers),
            "cityverseSubscriberCount": len(cityverse),
            "subscribers": cityverse,
        }

    def upsert_subscriber_profile(self, params: dict[str, Any], cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        execution_id = params.get("executionId") or intent.get("executionId") or ""
        event_type = params.get("eventType") or intent.get("eventType") or ""
        return self.free5gc.upsert_subscribers(
            event_type,
            cfg,
            execution_id,
            self.runtime_subscriber_upsert_limit,
        )

    def activate_qos_policy(self, params: dict[str, Any], cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        before_metrics = self.current_metrics()
        evidence_not_before = TimeUtils.now()
        payload = {
            "event_type": intent.get("eventType"),
            "ue_ipv4": params.get("ue_ipv4", "10.0.0.1"),
            "dnn": cfg.dnn,
            "slice_sst": cfg.slice_sst,
            "slice_sd": cfg.slice_sd,
            "five_qi": cfg.five_qi,
            "mbr": cfg.mbr.to_dict() if hasattr(cfg.mbr, "to_dict") else dict(cfg.mbr),
            "gbr": cfg.gbr.to_dict() if hasattr(cfg.gbr, "to_dict") else dict(cfg.gbr),
        }
        result = self.invoke_tool_lambda("activate_qos_policy", payload)
        result["controlPlaneStatus"] = result.get("status", "failed")
        before_mbps = self.measured_before_mbps(before_metrics, intent)
        configured_mbps = self.bandwidth_mbps(self.bandwidth_direction(cfg.mbr, "uplink"))
        probe_mbps = max(0.05, round(before_mbps * 0.5, 3)) if before_mbps > 0 else configured_mbps
        pfcp_actuation = self.qer_actuator(
            cfg,
            uplink_mbps=probe_mbps,
            downlink_mbps=probe_mbps,
        )
        nested = result.get("result") if isinstance(result.get("result"), dict) else {}
        if isinstance(nested.get("dataPlaneEvidence"), dict):
            result["claimedEvidence"] = nested.get("dataPlaneEvidence")
        trusted_evidence: dict[str, Any] = {}
        if self.evidence_reader is not None:
            try:
                read_evidence = getattr(self.evidence_reader, "read_until", self.evidence_reader.read)
                trusted_evidence = read_evidence(
                    str(params.get("executionId") or intent.get("executionId") or ""),
                    cfg.slice_sst,
                    cfg.slice_sd,
                    cfg.dnn,
                    evidence_not_before,
                    probe_mbps,
                    before_mbps,
                )
            except Exception as exc:
                result["evidenceReaderError"] = str(exc)
        restoration = self.qer_actuator(cfg)
        result["pfcpActuation"] = {
            **pfcp_actuation,
            "probeMbrMbps": probe_mbps,
            "restoration": restoration,
        }
        result["evidenceReaderStatus"] = "correlated" if trusted_evidence else "unavailable"
        evidence = DataPlaneEvidence.assess(trusted_evidence, before_metrics, self.current_metrics())
        if (
            result["controlPlaneStatus"] != "success"
            or pfcp_actuation.get("status") != "applied"
            or restoration.get("status") != "applied"
        ):
            evidence["actuatorStatus"] = "failed"
        result.update(evidence)
        # Fail closed: a northbound/SM-policy success is not a PFCP QER success.
        if evidence["actuatorStatus"] != "confirmed":
            if result["controlPlaneStatus"] == "success" and pfcp_actuation.get("status") == "applied":
                result["status"] = "unsupported"
                result["reason"] = "PFCP QER actuation is not proven by signaling, kernel, and measured-effect evidence"
            else:
                result["status"] = "failed"
        return result

    def actuate_pfcp_qer(
        self,
        cfg: EventConfig,
        *,
        uplink_mbps: float | None = None,
        downlink_mbps: float | None = None,
    ) -> dict[str, Any]:
        cluster_name = str(getattr(self.environment, "cluster_name", "") or "").strip()
        namespace = str(getattr(self.environment, "namespace", "") or "").strip()
        token = os.environ.get("SMF_QER_ACTUATOR_TOKEN", "").strip()
        if not cluster_name or not namespace:
            return {"status": "failed", "error": "EKS actuator target is not configured"}
        if not token:
            return {"status": "failed", "error": "SMF QER actuator token is unavailable"}

        configured_uplink = self.bandwidth_mbps(self.bandwidth_direction(cfg.mbr, "uplink"))
        configured_downlink = self.bandwidth_mbps(self.bandwidth_direction(cfg.mbr, "downlink"))
        uplink_kbps = max(1, min(500000, round(
            (configured_uplink if uplink_mbps is None else uplink_mbps) * 1000
        )))
        downlink_kbps = max(1, min(500000, round(
            (configured_downlink if downlink_mbps is None else downlink_mbps) * 1000
        )))
        service = os.environ.get("FREE5GC_SMF_SERVICE_NAME", "free5gc-free5gc-smf-service").strip()
        path = (
            f"/api/v1/namespaces/{namespace}/services/http:{service}:8080/proxy"
            "/nsmf-oam/v1/qer-actuation"
        )
        ue_ids = list(getattr(cfg, "ue_ids", []) or [])
        selector = {"supi": str(ue_ids[0])} if ue_ids else {
            "sst": int(cfg.slice_sst),
            "sd": str(cfg.slice_sd),
        }
        status, response = get_eks_client(cluster_name).request(
            "POST",
            path,
            {
                **selector,
                "uplinkMbrKbps": uplink_kbps,
                "downlinkMbrKbps": downlink_kbps,
            },
            extra_headers={"X-SMF-QER-Actuator-Token": token},
        )
        if status not in {200, 207}:
            return {"status": "failed", "httpStatus": status, "response": response}
        if not isinstance(response, dict) or int(response.get("appliedSessions") or 0) < 1:
            return {"status": "failed", "httpStatus": status, "response": response}
        return {
            "status": "applied",
            "httpStatus": status,
            "appliedSessions": response.get("appliedSessions"),
            "matchedSessions": response.get("matchedSessions"),
            "uplinkMbrKbps": uplink_kbps,
            "downlinkMbrKbps": downlink_kbps,
        }

    @staticmethod
    def bandwidth_direction(value: Any, direction: str) -> str:
        if isinstance(value, dict):
            return str(value.get(direction) or "")
        return str(getattr(value, direction, "") or "")

    @staticmethod
    def bandwidth_mbps(value: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG])?", str(value or ""), re.IGNORECASE)
        if not match:
            return 0.0
        amount = float(match.group(1))
        unit = (match.group(2) or "M").upper()
        return amount * {"K": 0.001, "M": 1.0, "G": 1000.0}[unit]

    @staticmethod
    def measured_before_mbps(metrics: dict[str, Any], intent: dict[str, Any]) -> float:
        iperf = metrics.get("iperf3") if isinstance(metrics.get("iperf3"), dict) else {}
        expected_scenarios = intent.get("batchScenarios") or [intent.get("eventType")]
        if isinstance(expected_scenarios, str):
            expected_scenarios = [expected_scenarios]
        if str(iperf.get("scenario") or "") not in {str(item) for item in expected_scenarios if item}:
            return -1.0
        source = str(iperf.get("source") or "")
        transport = str(iperf.get("transport") or "")
        if source not in {"server-log", "free5gc-tun", "iperf3"} and not (
            source == "client-json" and transport == "free5gc-tun"
        ):
            return -1.0
        try:
            return float(iperf.get("throughputMbps"))
        except (TypeError, ValueError):
            return -1.0

    def request_traffic_influence(self, params: dict[str, Any], cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "slice_sst": cfg.slice_sst,
            "af_service_id": params.get("af_service_id", f"5gcityverse-{intent.get('eventType', 'event')}"),
            "ue_ipv4": params.get("ue_ipv4", ""),
        }
        return self.invoke_tool_lambda("request_traffic_influence", payload)

    def create_pfd_rule(self, params: dict[str, Any], cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "app_id": params.get("app_id", f"5gcityverse-{intent.get('eventType', 'event')}"),
            "slice_sst": cfg.slice_sst,
            "flow_descriptions": params.get("flow_descriptions", self.flow_descriptions_for_slice(cfg.slice_sst)),
        }
        return self.invoke_tool_lambda("create_pfd_rule", payload)

    def patch_hpa(self, params: dict[str, Any], _cfg: EventConfig, _intent: dict[str, Any]) -> dict[str, Any]:
        component = str(params.get("component", "UPF")).upper()
        target_replicas = int(params.get("targetReplicas", params.get("target_replicas", 1)))
        if component not in {"UPF", "AMF", "SMF"}:
            return {"status": "failed", "error": f"Unsupported HPA component: {component}"}
        if target_replicas < 1 or target_replicas > self.max_hpa_replicas:
            return {
                "status": "failed",
                "error": f"targetReplicas must be between 1 and {self.max_hpa_replicas}",
                "component": component,
                "targetReplicas": target_replicas,
            }
        if not self.lambda_function_names.get("patch_hpa", ""):
            metrics = self.current_metrics()
            return {
                "status": "skipped",
                "operation": "runtime_managed_hpa",
                "component": component,
                "targetReplicas": target_replicas,
                "hpaStatus": (metrics.get("hpaStatus") or {}).get(component, {}),
                "reason": "HPA is managed by event runtime; no separate patch_hpa Lambda is configured.",
            }
        result = self.invoke_tool_lambda("patch_hpa", {"component": component, "target_replicas": target_replicas})
        result.setdefault("component", component)
        result.setdefault("targetReplicas", target_replicas)
        return result

    def verify_sla(self, _params: dict[str, Any], _cfg: EventConfig, intent: dict[str, Any]) -> dict[str, Any]:
        metrics = self.wait_for_sla_metrics(intent, _cfg)
        slices = self.current_slices()
        result = self.verifier.verify(intent, metrics, slices)
        result["slaStatus"] = result.get("status")
        result["status"] = "success"
        return result

    def wait_for_sla_metrics(self, intent: dict[str, Any], cfg: EventConfig, timeout_seconds: int = 45) -> dict[str, Any]:
        event_type = str(intent.get("eventType") or "")
        target = float((intent.get("sla") or {}).get("minThroughputMbps") or 0)
        expected = self.expected_profile_mbps(cfg)
        target_sst = (intent.get("targetSlice") or {}).get("sst", cfg.slice_sst)
        best_metrics: dict[str, Any] = {}
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            metrics = self.current_metrics()
            best_metrics = metrics
            iperf3 = metrics.get("iperf3") if isinstance(metrics.get("iperf3"), dict) else {}
            scenario = str(iperf3.get("scenario") or "")
            source = str(iperf3.get("source") or "")
            throughput = float(metrics.get("throughputMbps") or 0)
            if scenario == event_type:
                return metrics
            if source == "server-log" and self.throughput_matches_profile(throughput, expected, target):
                return metrics
            if self.scenario_slice_sessions_present(metrics, target_sst):
                return metrics
            time.sleep(5)
        return best_metrics or self.current_metrics()

    @staticmethod
    def scenario_slice_sessions_present(metrics: dict[str, Any], target_sst: Any) -> bool:
        """True once the scenario's own SST shows up in sliceSessions.

        While a UE bearer is still being verified (see
        scenario_environment.wait_for_ue_bearer's scaled timeout), pduSessionCount
        checks can otherwise wait the full timeout with no other readiness signal.
        Treating the target SST's appearance in sliceSessions as "ready" lets
        verify_sla proceed as soon as at least one PDU session for this scenario
        is observed, instead of always burning the full wait.
        """
        slice_sessions = metrics.get("sliceSessions")
        if not isinstance(slice_sessions, dict) or target_sst is None:
            return False
        for sst, count in slice_sessions.items():
            try:
                matches_sst = int(sst) == int(target_sst)
            except (TypeError, ValueError):
                matches_sst = False
            if matches_sst and SlaVerifier.number(count) > 0:
                return True
        return False

    @staticmethod
    def throughput_matches_profile(throughput_mbps: float, expected_mbps: float, target_mbps: float) -> bool:
        if throughput_mbps <= 0:
            return False
        if expected_mbps > 0:
            return expected_mbps * 0.5 <= throughput_mbps <= expected_mbps * 1.5
        return target_mbps <= 0 or throughput_mbps >= target_mbps * 0.7

    @staticmethod
    def expected_profile_mbps(cfg: EventConfig) -> float:
        profile = str(getattr(cfg, "traffic_profile", "") or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG])", profile, re.IGNORECASE)
        if not match:
            return 0.0
        value = float(match.group(1))
        unit = match.group(2).upper()
        multiplier = {"K": 0.001, "M": 1.0, "G": 1000.0}[unit]
        parallel_match = re.search(r"x\s*(\d+)", profile, re.IGNORECASE)
        parallel = int(parallel_match.group(1)) if parallel_match else 1
        return value * multiplier * parallel

    def invoke_tool_lambda(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        function_name = self.lambda_function_names.get(tool, "")
        if not function_name:
            return {
                "status": "failed",
                "reason": "not_configured",
                "tool": tool,
                "error": f"{tool} Lambda function name is not configured",
            }
        payload = {
            "messageVersion": "1.0",
            "actionGroup": "5GCityVerseNetworkTools",
            "function": tool,
            "parameters": [{"name": key, "value": value} for key, value in params.items() if value is not None],
        }
        if self.lambda_client is None:
            self.lambda_client = boto3.client("lambda")
        response = self.lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        raw_payload = response.get("Payload").read().decode("utf-8") if response.get("Payload") else "{}"
        parsed = json.loads(raw_payload or "{}")
        result = self.extract_bedrock_tool_result(parsed)
        function_error = response.get("FunctionError")
        success = bool(result.get("success")) and not function_error
        return {
            "status": "success" if success else "failed",
            "operation": "lambda_invoke",
            "functionName": function_name,
            "lambdaStatusCode": response.get("StatusCode"),
            "functionError": function_error,
            "api": result.get("api_endpoint", tool),
            "result": result,
        }

    @staticmethod
    def extract_bedrock_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
        body = (
            (((payload.get("response") or {}).get("functionResponse") or {}).get("responseBody") or {})
            .get("TEXT", {})
            .get("body", "{}")
        )
        if isinstance(body, dict):
            return body
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError:
            return {"success": False, "raw": body}

    @staticmethod
    def flow_descriptions_for_slice(sst: int) -> list[str]:
        return {
            1: ["permit in udp from any to any", "permit out udp from any to any"],
            2: ["permit in ip from any to any", "permit out ip from any to any"],
            3: ["permit in udp from any to any 5683", "permit out udp from any 5683 to any"],
            4: ["permit out udp from any to 224.0.0.0/4 5000", "permit in udp from 224.0.0.0/4 5000 to any"],
        }.get(sst, ["permit in ip from any to any"])
