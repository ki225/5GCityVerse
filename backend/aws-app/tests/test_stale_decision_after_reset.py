from __future__ import annotations

from typing import Any

from app import CityVerseBackendApp
from config import AppSettings, EVENT_CONFIG


def _build_app(monkeypatch) -> CityVerseBackendApp:
    """A backend instance with no DynamoDB/EKS/free5GC wiring (mirrors
    tests/test_app_error_shapes.py's _build_app)."""
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    return CityVerseBackendApp(AppSettings())


class _RecordingEvents:
    """Minimal EventRepository stand-in that records update_status calls."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def get_status(self, execution_id: str) -> dict[str, Any] | None:
        return None

    def update_status(self, execution_id: str, updates: dict[str, Any]) -> None:
        self.updates.append(dict(updates))

    def put_status(self, item: dict[str, Any]) -> None:
        pass

    def release_session_lease(self, session_id: str) -> bool:
        return True


class _StubAgentxgRunsProgressThenFails:
    """Stand-in for AgentxGCoreLoop that emits one progress callback (with a
    decision) and then raises, so run_agentxg_loop's try/except doesn't need
    a full result dict for this test."""

    def __init__(self, progress_payload: dict[str, Any]) -> None:
        self._progress_payload = progress_payload

    def run(self, *_args: Any, on_progress: Any = None, **_kwargs: Any) -> dict[str, Any]:
        if on_progress:
            on_progress(self._progress_payload)
        raise RuntimeError("stop after emitting progress; test only cares about the broadcast gate")


def test_publish_progress_does_not_broadcast_agent_decision_after_reset(monkeypatch) -> None:
    """B: run_agentxg_loop's publish_progress callback must re-check
    event_cancelled_by_reset immediately before broadcasting agent_decision,
    so a decision that finishes computing after a reset is never surfaced."""
    backend = _build_app(monkeypatch)
    backend.events = _RecordingEvents()
    broadcasts: list[dict[str, Any]] = []
    monkeypatch.setattr(backend, "broadcast", lambda message: broadcasts.append(message))
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: True)
    backend.agentxg = _StubAgentxgRunsProgressThenFails({"stage": "planned", "agentDecision": {"decision": "should not be broadcast"}})

    cfg = EVENT_CONFIG["concert"]
    backend.run_agentxg_loop("exec-reset-1", "concert", cfg, {}, [])

    decision_broadcasts = [m for m in broadcasts if m.get("type") == "agent_decision"]
    assert decision_broadcasts == []
    # Status updates (DynamoDB) may still happen; only the WS broadcast is gated.
    assert any(update.get("progressStage") == "planned" for update in backend.events.updates)


def test_publish_progress_broadcasts_agent_decision_when_not_reset(monkeypatch) -> None:
    """Control case: without a reset, the same progress callback must still broadcast."""
    backend = _build_app(monkeypatch)
    backend.events = _RecordingEvents()
    broadcasts: list[dict[str, Any]] = []
    monkeypatch.setattr(backend, "broadcast", lambda message: broadcasts.append(message))
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: False)
    backend.agentxg = _StubAgentxgRunsProgressThenFails({"stage": "planned", "agentDecision": {"decision": "safe to broadcast"}})

    cfg = EVENT_CONFIG["concert"]
    backend.run_agentxg_loop("exec-live-1", "concert", cfg, {}, [])

    decision_broadcasts = [m for m in broadcasts if m.get("type") == "agent_decision"]
    assert len(decision_broadcasts) == 1
    assert decision_broadcasts[0]["payload"]["decision"] == "safe to broadcast"


def test_handle_trigger_skips_final_agent_decision_when_reset_during_monitor_window(monkeypatch) -> None:
    """B: even if run_agentxg_loop's own agentxg_result was cancelled-checked
    already, a reset landing during monitor_event_window must still stop the
    final agent_decision broadcast/persist after the window ends."""
    backend = _build_app(monkeypatch)
    backend.events = _RecordingEvents()
    broadcasts: list[dict[str, Any]] = []
    monkeypatch.setattr(backend, "broadcast", lambda message: broadcasts.append(message))

    cfg, scenario_context = backend.event_config_for_request("concert")
    agentxg_result = {
        "agentDecision": {"decision": "stale decision computed before reset landed"},
        "free5gc": {"status": "success"},
        "environment": {"status": "success"},
        "verification": {"status": "passed"},
        "status": "success",
        "intent": {},
        "baseline": {},
        "planner": {},
        "executor": {"actions": []},
        "adaptation": {},
        "validationReport": {},
    }
    monkeypatch.setattr(backend, "run_agentxg_loop", lambda *a, **k: agentxg_result)
    monkeypatch.setattr(backend, "monitor_event_window", lambda *a, **k: None)
    monkeypatch.setattr(backend, "current_metrics", lambda *a, **k: {})
    monkeypatch.setattr(backend, "current_slices", lambda *a, **k: [])
    monkeypatch.setattr(backend, "free5gc_status_for_trigger", lambda: {"connected": True})
    monkeypatch.setattr(backend, "prime_runtime_before_planning", lambda *a, **k: {"status": "success", "observedBeforePlanning": True})

    # First call (pre-loop) reports not cancelled; second call (post monitor window) reports cancelled.
    calls = {"count": 0}

    def fake_cancelled(_execution_id: str) -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    monkeypatch.setattr(backend, "event_cancelled_by_reset", fake_cancelled)

    response = backend.handle_trigger(
        {"body": '{"event_type": "concert", "slice_strategy": "ai", "_async": true, "execution_id": "exec-mid-reset"}'},
        None,
    )

    assert response["statusCode"] == 409
    decision_broadcasts = [m for m in broadcasts if m.get("type") == "agent_decision"]
    assert decision_broadcasts == []
    reset_broadcasts = [m for m in broadcasts if m.get("type") == "event_reset"]
    assert len(reset_broadcasts) == 1


def test_late_async_batch_does_not_create_runtime_after_reset(monkeypatch) -> None:
    """A reset marker can win the race before the queued batch Lambda starts."""
    backend = _build_app(monkeypatch)
    backend.events = _RecordingEvents()
    broadcasts: list[dict[str, Any]] = []
    trigger_calls: list[str] = []
    monkeypatch.setattr(backend, "broadcast", lambda message: broadcasts.append(message))
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: True)
    monkeypatch.setattr(backend, "cleanup_batch_runtime", lambda _request: None)
    monkeypatch.setattr(backend, "invalidate_metrics_cache", lambda: None)
    monkeypatch.setattr(
        backend.environment,
        "trigger",
        lambda event_type, *_args, **_kwargs: trigger_calls.append(event_type),
    )

    response = backend.handle_trigger(
        {
            "headers": {"x-session-id": "batch-reset-session"},
            "body": '{"scenarios":[{"event_type":"concert","event_scale":50},{"event_type":"iot_surge","event_scale":50}],"city_residents":100000,"slice_strategy":"ai","_async":true,"execution_id":"batch-reset-exec"}',
        },
        None,
    )

    assert response["statusCode"] == 409
    assert trigger_calls == []
    assert backend.events.updates[-1]["status"] == "SIMULATION_CANCELLED"
    assert any(message.get("type") == "event_reset" for message in broadcasts)
