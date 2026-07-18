import json
from typing import Any

from app import CityVerseBackendApp
from config import AppSettings


def _app(monkeypatch) -> CityVerseBackendApp:
    for name in ("DYNAMODB_TABLE", "APIGW_WS_ENDPOINT", "PROMETHEUS_URL", "FREE5GC_WEBUI_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")
    return CityVerseBackendApp(AppSettings())


def _http(method: str, path: str, session_id: str = "browser-a") -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"x-session-id": session_id},
    }


class RecordingResetEvents:
    def __init__(self) -> None:
        self.job: dict[str, Any] | None = None
        self.created = True
        self.claimed = True
        self.released: list[str] = []
        self.marker_count = 0
        self.updates: list[dict[str, Any]] = []

    def session_lease(self):
        return None

    def acquire_session_lease(self, _session_id: str, ttl_seconds: int = 600) -> bool:
        assert ttl_seconds == 1200
        return True

    def begin_reset_job(self, session_id: str, reset_id: str, queued_at: str):
        if self.job is None:
            self.job = {
                "resetId": reset_id,
                "sessionId": session_id,
                "status": "queued",
                "progressStage": "queued",
                "progressPercent": 0,
                "queuedAt": queued_at,
            }
        return dict(self.job), self.created

    def put_reset_marker(self, _epoch: int, _at: str) -> None:
        self.marker_count += 1

    def get_reset_job(self, session_id: str, reset_id: str | None = None):
        if not self.job or self.job.get("sessionId") != session_id:
            return None
        if reset_id and self.job.get("resetId") != reset_id:
            return None
        return dict(self.job)

    def claim_reset_job(self, _session_id: str, _reset_id: str, _started_at: str) -> bool:
        return self.claimed

    def update_reset_job(self, _session_id: str, _reset_id: str, updates: dict[str, Any]) -> bool:
        self.updates.append(dict(updates))
        assert self.job is not None
        self.job.update(updates)
        return True

    def release_session_lease(self, session_id: str) -> bool:
        self.released.append(session_id)
        return True


def test_post_reset_returns_202_and_enqueues_worker(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    backend.events = events
    invoked: list[tuple[str, str]] = []
    monkeypatch.setattr(backend, "invoke_reset_async", lambda _ctx, session, reset: invoked.append((session, reset)))

    response = backend.handle(_http("POST", "/events/reset"), object())
    body = json.loads(response["body"])

    assert response["statusCode"] == 202
    assert body == {
        "resetId": events.job["resetId"],
        "status": "queued",
        "progressStage": "queued",
        "progressPercent": 0,
        "statusUrl": f"/events/reset/{events.job['resetId']}",
        "idempotentReplay": False,
    }
    assert invoked == [("browser-a", events.job["resetId"])]
    assert events.marker_count == 1
    assert events.released == []


def test_duplicate_post_reuses_active_reset_without_enqueuing(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.created = False
    events.job = {
        "resetId": "reset-existing",
        "sessionId": "browser-a",
        "status": "running",
        "progressStage": "session_recycle",
        "progressPercent": 50,
    }
    backend.events = events
    monkeypatch.setattr(backend, "invoke_reset_async", lambda *_args: (_ for _ in ()).throw(AssertionError("must not enqueue")))

    response = backend.handle(_http("POST", "/events/reset"), object())
    body = json.loads(response["body"])

    assert response["statusCode"] == 202
    assert body["resetId"] == "reset-existing"
    assert body["status"] == "running"
    assert body["progressPercent"] == 50
    assert body["idempotentReplay"] is True
    assert events.marker_count == 0


def test_reset_worker_runs_cleanup_then_recycle_and_releases_lease(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.job = {"resetId": "reset-1", "sessionId": "browser-a", "status": "queued"}
    backend.events = events
    order: list[str] = []
    fake_k8s = object()
    monkeypatch.setattr("app.get_eks_client", lambda _name: fake_k8s)
    monkeypatch.setattr(backend.environment, "cleanup_all_event_runtime", lambda client: order.append("cleanup") or {"status": "reset", "client": client is fake_k8s})
    monkeypatch.setattr(backend.environment, "recycle_session_state", lambda client: order.append("recycle") or {"status": "success", "client": client is fake_k8s})
    monkeypatch.setattr(backend, "safe_free5gc_status", lambda: {"connected": True})
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    result = backend.handle({"_cityverseInternalAction": "reset", "sessionId": "browser-a", "resetId": "reset-1"}, None)

    assert result == {"status": "success", "resetId": "reset-1"}
    assert order == ["cleanup", "recycle"]
    assert events.job["status"] == "success"
    assert events.job["progressStage"] == "complete"
    assert events.job["progressPercent"] == 100
    assert events.released == ["browser-a"]


def test_reset_worker_persists_failure_and_releases_lease(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.job = {"resetId": "reset-1", "sessionId": "browser-a", "status": "queued"}
    backend.events = events
    monkeypatch.setattr("app.get_eks_client", lambda _name: object())
    monkeypatch.setattr(backend.environment, "cleanup_all_event_runtime", lambda _client: (_ for _ in ()).throw(RuntimeError("cleanup failed")))
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    result = backend.handle({"_cityverseInternalAction": "reset", "sessionId": "browser-a", "resetId": "reset-1"}, None)

    assert result["status"] == "failed"
    assert events.job["status"] == "failed"
    assert events.job["progressStage"] == "failed"
    assert events.job["error"] == "cleanup failed"
    assert events.released == ["browser-a"]


def test_reset_status_is_scoped_to_browser_session(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.job = {"resetId": "reset-1", "sessionId": "browser-a", "status": "running", "progressPercent": 50}
    backend.events = events

    own = backend.handle(_http("GET", "/events/reset/reset-1", "browser-a"), None)
    foreign = backend.handle(_http("GET", "/events/reset/reset-1", "browser-b"), None)

    assert own["statusCode"] == 200
    assert json.loads(own["body"])["status"] == "running"
    assert foreign["statusCode"] == 404


def test_reset_status_converts_expired_worker_to_failed_and_releases_lease(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.job = {
        "resetId": "reset-1",
        "sessionId": "browser-a",
        "status": "running",
        "progressPercent": 50,
        "deadlineEpochSeconds": 1,
    }
    backend.events = events

    response = backend.handle(_http("GET", "/events/reset/reset-1", "browser-a"), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "failed"
    assert body["progressStage"] == "timeout"
    assert body["progressPercent"] == 100
    assert events.released == ["browser-a"]


def test_duplicate_async_delivery_does_not_release_active_worker_lease(monkeypatch) -> None:
    backend = _app(monkeypatch)
    events = RecordingResetEvents()
    events.claimed = False
    events.job = {"resetId": "reset-1", "sessionId": "browser-a", "status": "running"}
    backend.events = events

    result = backend.handle({"_cityverseInternalAction": "reset", "sessionId": "browser-a", "resetId": "reset-1"}, None)

    assert result == {"status": "duplicate", "resetId": "reset-1"}
    assert events.released == []
