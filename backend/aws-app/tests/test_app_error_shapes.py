from __future__ import annotations

import json
from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings
from constants import ApiErrorCode


def _build_app(monkeypatch) -> CityVerseBackendApp:
    """A backend instance with no DynamoDB/EKS/free5GC wiring, so __init__
    never touches boto3 or the network (mirrors tests/test_metrics_ttl_cache.py)."""
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    return CityVerseBackendApp(AppSettings())


def _http_event(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _assert_error_shape(response: dict[str, Any], status: int, code: ApiErrorCode) -> dict[str, Any]:
    assert response["statusCode"] == status
    parsed = json.loads(response["body"])
    assert parsed["error"] == code.value
    assert "detail" in parsed
    return parsed


class _StubEvents:
    """Minimal EventRepository stand-in that always reports "no such execution"."""

    def get_status(self, execution_id: str) -> dict[str, Any] | None:
        return None

    def update_status(self, execution_id: str, updates: dict[str, Any]) -> None:
        pass

    def release_session_lease(self, session_id: str) -> bool:
        return True


def test_get_status_without_dynamodb_table_returns_service_unavailable(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    assert backend.events is None

    response = backend.handle(_http_event("GET", "/events/status/exec-1"), None)

    _assert_error_shape(response, 503, ApiErrorCode.SERVICE_UNAVAILABLE)


def test_get_status_unknown_execution_returns_not_found(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    backend.events = _StubEvents()

    response = backend.handle(_http_event("GET", "/events/status/does-not-exist"), None)

    _assert_error_shape(response, 404, ApiErrorCode.NOT_FOUND)


def test_unknown_route_returns_not_found(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    response = backend.handle(_http_event("GET", "/not/a/real/route"), None)

    parsed = _assert_error_shape(response, 404, ApiErrorCode.NOT_FOUND)
    assert "/not/a/real/route" in parsed["detail"]


def test_unhandled_exception_returns_internal_error(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    monkeypatch.setattr(backend, "safe_free5gc_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    response = backend.handle(_http_event("GET", "/free5gc/status"), None)

    _assert_error_shape(response, 500, ApiErrorCode.INTERNAL_ERROR)


def test_trigger_with_invalid_json_body_returns_invalid_request(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    event = _http_event("POST", "/events/trigger")
    event["body"] = "{not-json"

    response = backend.handle(event, None)

    _assert_error_shape(response, 400, ApiErrorCode.INVALID_REQUEST)


def test_trigger_without_event_type_or_scenarios_returns_invalid_request(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    response = backend.handle(_http_event("POST", "/events/trigger", {"_async": True}), None)

    _assert_error_shape(response, 400, ApiErrorCode.INVALID_REQUEST)


def test_trigger_with_unknown_event_type_returns_invalid_request(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    response = backend.handle(
        _http_event("POST", "/events/trigger", {"_async": True, "event_type": "not_a_real_event"}),
        None,
    )

    _assert_error_shape(response, 400, ApiErrorCode.INVALID_REQUEST)


def test_async_trigger_without_dynamodb_table_returns_service_unavailable(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    assert backend.events is None

    response = backend.handle(
        _http_event("POST", "/events/trigger", {"event_type": "concert"}),
        None,
    )

    _assert_error_shape(response, 503, ApiErrorCode.SERVICE_UNAVAILABLE)


def test_batch_trigger_with_no_scenarios_returns_invalid_request(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    response = backend.handle(
        _http_event("POST", "/api/scenario/trigger", {"scenarios": []}),
        None,
    )

    # Pydantic's default_factory yields an empty list, which fails the
    # `not event_type` check first via handle_trigger's own validation path.
    _assert_error_shape(response, 400, ApiErrorCode.INVALID_REQUEST)


def test_inline_trigger_blocked_when_traffic_not_observed_nests_state_in_detail(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    monkeypatch.setattr(backend, "prime_runtime_before_planning", lambda *a, **k: {"status": "failed", "observedBeforePlanning": False})
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    response = backend.handle(
        _http_event("POST", "/events/trigger", {"event_type": "concert", "_async": True}),
        None,
    )

    parsed = _assert_error_shape(response, 503, ApiErrorCode.EVENT_BLOCKED)
    detail = parsed["detail"]
    assert isinstance(detail, dict)
    assert detail["executionId"]
    assert detail["eventType"] == "concert"
    assert detail["status"] == "SIMULATION_BLOCKED"
    # Old shape spread these fields at the top level of the body; they must
    # now live only inside detail.
    assert "status" not in parsed
    assert "executionId" not in parsed


def test_inline_trigger_cancelled_before_start_nests_state_in_detail(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    backend.events = _StubEvents()
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: True)
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    response = backend.handle(
        _http_event("POST", "/events/trigger", {"event_type": "concert", "_async": True}),
        None,
    )

    parsed = _assert_error_shape(response, 409, ApiErrorCode.EVENT_CANCELLED)
    detail = parsed["detail"]
    assert isinstance(detail, dict)
    assert detail["eventType"] == "concert"
    assert detail["status"] == "SIMULATION_CANCELLED"
    assert "status" not in parsed


def test_reset_endpoint_without_dynamodb_returns_service_unavailable(monkeypatch) -> None:
    """Async reset requires durable job state and must not pretend success."""
    backend = _build_app(monkeypatch)

    response = backend.handle(_http_event("POST", "/events/reset"), None)

    _assert_error_shape(response, 503, ApiErrorCode.SERVICE_UNAVAILABLE)
