from __future__ import annotations

from app import CityVerseBackendApp
from config import AppSettings


def _build_app(monkeypatch) -> CityVerseBackendApp:
    """A backend instance with no DynamoDB/EKS/free5GC wiring, so free5gc_status()
    never touches boto3 or the network (mirrors tests/test_app_error_shapes.py)."""
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    return CityVerseBackendApp(AppSettings())


def test_free5gc_status_network_snapshot_has_no_duplicated_metrics_or_slices(monkeypatch) -> None:
    backend = _build_app(monkeypatch)

    status = backend.free5gc_status()

    assert "metrics" in status
    assert "slices" in status
    snapshot = status["networkSnapshot"]
    assert "metrics" not in snapshot
    assert "slices" not in snapshot
    # Other snapshot fields remain intact.
    assert "edges" in snapshot
    assert "id" in snapshot
    assert "timestamp" in snapshot


def test_safe_free5gc_status_fallback_snapshot_has_no_duplicated_metrics_or_slices(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    monkeypatch.setattr(backend, "free5gc_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    status = backend.safe_free5gc_status()

    assert "metrics" in status
    assert "slices" in status
    snapshot = status["networkSnapshot"]
    assert "metrics" not in snapshot
    assert "slices" not in snapshot
    assert "edges" in snapshot
