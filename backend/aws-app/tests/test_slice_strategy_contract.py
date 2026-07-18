from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings
from models import TriggerRequest


class RecordingEvents:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.released: list[str] = []

    def acquire_session_lease(self, _session_id: str) -> bool:
        return True

    def put_status(self, item: dict[str, Any]) -> None:
        self.puts.append(dict(item))

    def update_status(self, _execution_id: str, updates: dict[str, Any]) -> None:
        self.updates.append(dict(updates))

    def get_status(self, _execution_id: str) -> dict[str, Any] | None:
        return None

    def latest_reset_epoch_millis(self) -> int:
        return 0

    def release_session_lease(self, session_id: str) -> bool:
        self.released.append(session_id)
        return True


def build_app(monkeypatch) -> CityVerseBackendApp:
    for name in ("DYNAMODB_TABLE", "APIGW_WS_ENDPOINT", "EKS_CLUSTER_NAME", "PROMETHEUS_URL", "FREE5GC_WEBUI_URL"):
        monkeypatch.delenv(name, raising=False)
    backend = CityVerseBackendApp(AppSettings())
    backend.events = RecordingEvents()
    backend.broadcast = lambda _message: None  # type: ignore[method-assign]
    return backend


def test_render_ack_gate_accepts_ack_after_api_cold_start(monkeypatch) -> None:
    backend = build_app(monkeypatch)
    clock = {"seconds": 0.0}
    backend.events = SimpleNamespace(
        get_status=lambda _execution_id: (
            {"trafficRenderedAt": "2026-07-16T00:00:00Z"} if clock["seconds"] >= 20 else {}
        )
    )
    backend.remaining_lambda_millis = lambda _context: 900_000  # type: ignore[method-assign]
    backend.event_cancelled_by_reset = lambda _execution_id: False  # type: ignore[method-assign]
    monkeypatch.setattr(app_module.time, "time", lambda: clock["seconds"])
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: clock.__setitem__("seconds", clock["seconds"] + seconds))

    assert backend.wait_for_traffic_render_ack("exec-1") is True
    assert clock["seconds"] >= 20


@pytest.mark.parametrize("strategy", ["none", "static", "ai"])
def test_trigger_request_accepts_all_slice_strategies(strategy: str) -> None:
    request = TriggerRequest(scenarios=[{"event_type": "concert", "event_scale": 6000}], slice_strategy=strategy)
    assert CityVerseBackendApp.slice_strategy_value(request.slice_strategy) == strategy


def test_trigger_request_defaults_to_safe_non_ai_and_rejects_unknown() -> None:
    assert CityVerseBackendApp.slice_strategy_value(TriggerRequest().slice_strategy) == "none"
    with pytest.raises((ValueError, TypeError)):
        TriggerRequest(slice_strategy="automatic")


def test_batch_async_payload_preserves_slice_strategy(monkeypatch) -> None:
    backend = build_app(monkeypatch)
    request = TriggerRequest(
        scenarios=[{"event_type": "concert", "event_scale": 6000}],
        city_residents=180000,
        slice_strategy="static",
    )
    captured: dict[str, Any] = {}

    class LambdaClient:
        def invoke(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(app_module.boto3, "client", lambda _service: LambdaClient())
    context = type("Context", (), {"invoked_function_arn": "arn:test:function"})()

    backend.invoke_batch_async({}, context, "exec-1", request, {"locale": "zh"})

    payload = json.loads(captured["Payload"].decode("utf-8"))
    body = json.loads(payload["body"])
    assert body["slice_strategy"] == "static"


@pytest.mark.parametrize("strategy", ["none", "static"])
def test_non_ai_batch_never_invokes_agent_or_emits_decision(monkeypatch, strategy: str) -> None:
    backend = build_app(monkeypatch)
    request = TriggerRequest(
        scenarios=[{"event_type": "concert", "event_scale": 6000}],
        city_residents=180000,
        slice_strategy=strategy,
    )
    backend.environment.trigger = lambda *_args, **_kwargs: {"status": "success", "actions": []}  # type: ignore[method-assign]
    backend.wait_for_runtime_scenarios = lambda expected, *_args, **_kwargs: list(expected)  # type: ignore[method-assign]
    status = {
        "connected": True,
        "metrics": {"scenarioTraffic": [{"scenario": "concert", "throughputMbps": 6.0, "transport": "free5gc-tun"}]},
        "slices": [{"sst": 1, "load": 100}],
        "networkSnapshot": {"edges": [{"active": True, "throughputMbps": 6.0}]},
    }
    backend.free5gc_status = lambda: status  # type: ignore[method-assign]
    backend.wait_for_traffic_render_ack = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    backend.monitor_event_window = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    backend.cleanup_batch_runtime = lambda _request: None  # type: ignore[method-assign]
    backend.run_agentxg_loop = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("non-AI strategy invoked the agent"))  # type: ignore[method-assign]

    response = backend.handle_batch_execution(
        {"headers": {"x-session-id": "session-1"}}, None, "exec-1", request, "zh"
    )

    assert response["statusCode"] == 200
    assert backend.events.updates[-1]["status"] == "SIMULATION_COMPLETE"
    assert backend.events.updates[-1]["sliceStrategy"] == strategy
    assert "agentDecision" not in backend.events.updates[-1]
    policy = backend.events.updates[-1]["appliedPolicy"]
    assert policy["applied"] is False
    assert policy["actuator"] == "none"
    assert backend.events.released == ["session-1"]


def test_ai_batch_keeps_agent_decision_flow(monkeypatch) -> None:
    backend = build_app(monkeypatch)
    request = TriggerRequest(
        scenarios=[{"event_type": "concert", "event_scale": 6000}],
        city_residents=180000,
        slice_strategy="ai",
    )
    backend.environment.trigger = lambda *_args, **_kwargs: {"status": "success", "actions": []}  # type: ignore[method-assign]
    backend.wait_for_runtime_scenarios = lambda expected, *_args, **_kwargs: list(expected)  # type: ignore[method-assign]
    status = {
        "connected": True,
        "metrics": {"scenarioTraffic": [{"scenario": "concert", "throughputMbps": 6.0, "transport": "free5gc-tun"}]},
        "slices": [{"sst": 1, "load": 100}],
        "networkSnapshot": {"edges": [{"active": True, "throughputMbps": 6.0}]},
    }
    backend.free5gc_status = lambda: status  # type: ignore[method-assign]
    backend.wait_for_traffic_render_ack = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    backend.monitor_event_window = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    backend.environment.cleanup_event_runtime = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    backend.environment.recycle_session_state = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    monkeypatch.setattr(app_module, "get_eks_client", lambda _name: object())
    calls = {"agent": 0}

    def run_agent(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls["agent"] += 1
        return {
            "agentDecision": {"decision": "measured plan"},
            "verification": {"status": "passed"},
            "free5gc": {},
            "intent": {},
            "baseline": {},
            "planner": {},
            "executor": {},
            "adaptation": {},
            "validationReport": {},
        }

    backend.run_agentxg_loop = run_agent  # type: ignore[method-assign]

    response = backend.handle_batch_execution(
        {"headers": {"x-session-id": "session-1"}}, None, "exec-1", request, "zh"
    )

    assert response["statusCode"] == 200
    assert calls["agent"] == 1
    assert backend.events.updates[-1]["status"] == "SIMULATION_COMPLETE"
    assert backend.events.updates[-1]["sliceStrategy"] == "ai"
    assert backend.events.updates[-1]["agentDecision"]["decision"] == "measured plan"


def test_batch_enqueue_failure_persists_terminal_and_releases_lease(monkeypatch) -> None:
    backend = build_app(monkeypatch)
    request = TriggerRequest(
        scenarios=[{"event_type": "concert", "event_scale": 6000}],
        slice_strategy="ai",
    )
    backend.invoke_batch_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("enqueue failed"))  # type: ignore[method-assign]

    response = backend.handle_batch_trigger(
        {"headers": {"x-session-id": "session-1"}}, None, "exec-1", request
    )

    assert response["statusCode"] == 503
    assert backend.events.updates[-1]["status"] == "SIMULATION_FAILED"
    assert backend.events.updates[-1]["progressStage"] == "enqueue_failed"
    assert backend.events.released == ["session-1"]


def test_unhandled_async_batch_failure_persists_terminal_and_releases_lease(monkeypatch) -> None:
    backend = build_app(monkeypatch)
    request = TriggerRequest(
        scenarios=[{"event_type": "concert", "event_scale": 6000}],
        slice_strategy="ai",
    )
    backend._handle_batch_execution = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runtime failed"))  # type: ignore[method-assign]
    backend.cleanup_batch_runtime = lambda _request: None  # type: ignore[method-assign]

    response = backend.handle_batch_execution(
        {"headers": {"x-session-id": "session-1"}}, None, "exec-1", request, "zh"
    )

    assert response["statusCode"] == 500
    assert backend.events.updates[-1]["status"] == "SIMULATION_FAILED"
    assert backend.events.updates[-1]["progressStage"] == "execution_failed"
    assert backend.events.released == ["session-1"]
