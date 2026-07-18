"""Regression tests for event-owned runtime traffic.

Bedrock/free5GC agent tools must not generate real UERANSIM/iperf3 traffic. The
event trigger primes runtime traffic before agent planning; agent tools only use
the stored scaled event config for policy orchestration.
"""

from __future__ import annotations

import json
from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings
from scenario_environment import ScenarioEnvironmentService
from tests.test_scenario_environment import FakeK8sClient


def _build_app(monkeypatch) -> CityVerseBackendApp:
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    return CityVerseBackendApp(AppSettings())


class _RecordingEvents:
    """EventRepository stand-in returning one stored (scaled) execution record."""

    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self._records = records

    def get_status(self, execution_id: str) -> dict[str, Any] | None:
        return self._records.get(execution_id)


def _bedrock_event(api_path: str, params: dict[str, str]) -> dict[str, Any]:
    return {
        "actionGroup": "5GCityVerseNetworkTools",
        "apiPath": f"/{api_path}",
        "httpMethod": "POST",
        "messageVersion": "1.0",
        "parameters": [{"name": key, "value": value} for key, value in params.items()],
    }


def _iperf_command(k8s: FakeK8sClient) -> str:
    creations = [
        c for c in k8s.created
        if "/deployments" in c["path"] and c["body"].get("metadata", {}).get("name") == "iperf3-concert"
    ]
    assert len(creations) == 1
    manifest = creations[0]["body"]
    return manifest["spec"]["template"]["spec"]["containers"][0]["command"][2]


def test_recreate_iperf3_job_uses_80M_for_unscaled_base_cfg() -> None:
    """The raw base concert cfg produces the bounded default -b 80M. This is exactly
    the wrong manifest the live bug emitted; the fix must NOT feed this cfg here."""
    from config import EVENT_CONFIG

    service = ScenarioEnvironmentService("c", "free5gc", "ueransim-city-ue")
    k8s = FakeK8sClient()

    service.recreate_iperf3_job(k8s, "concert", EVENT_CONFIG["concert"], execution_id="exec-real")

    assert " -b 80M " in _iperf_command(k8s)


def test_agent_tool_cannot_start_ueransim_profile(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    k8s = FakeK8sClient()
    backend.environment.cluster_name = "test-cluster"
    monkeypatch.setattr(app_module, "get_eks_client", lambda cluster_name: k8s, raising=False)
    monkeypatch.setattr(
        backend.environment, "trigger",
        lambda event_type, cfg, execution_id=None: (_ for _ in ()).throw(AssertionError("agent tool must not trigger runtime")),
    )

    response = backend.handle_agent_tool(
        _bedrock_event("start_ueransim_profile", {"event_type": "concert", "execution_id": "concert-exec-123"})
    )

    assert response["response"]["httpStatusCode"] == 403
    body = json.loads(response["response"]["responseBody"]["application/json"]["body"])
    assert body["status"] == "failed"
    assert "event-runtime only" in body["error"]
    assert k8s.created == []


def test_tool_config_for_execution_returns_scaled_cfg_from_dynamo(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    scaled_cfg, scenario_context = backend.event_config_for_request("concert", event_scale=3)
    execution_id = "exec-scaled"
    backend.events = _RecordingEvents(
        {execution_id: {"config": scaled_cfg.to_dict(), "scenarioContext": scenario_context}}
    )

    cfg, context = backend.tool_config_for_execution("concert", execution_id)

    assert cfg.traffic_profile == scaled_cfg.traffic_profile
    assert "80M" not in cfg.traffic_profile
    assert context.get("scaleRatio") == scenario_context.get("scaleRatio")


def test_tool_config_for_execution_falls_back_to_base_when_no_record(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    backend.events = _RecordingEvents({})

    cfg, context = backend.tool_config_for_execution("concert", "tool-generated-uuid")

    # No stored record (e.g. a fresh tool-<uuid>): base config is used, context empty.
    assert cfg.traffic_profile == "iperf3 UDP 80M, 1400-byte packets"
    assert context == {}
