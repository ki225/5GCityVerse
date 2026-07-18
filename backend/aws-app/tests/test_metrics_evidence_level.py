from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from metrics_service import PrometheusMetricsService


def test_unavailable_metrics_evidence_level_is_fallback() -> None:
    """No Prometheus URL / no data at all -> fallback tier, not a real measurement."""
    service = PrometheusMetricsService("")

    metrics = service.unavailable_metrics()

    assert metrics["dataSource"] == "unavailable"
    assert metrics["evidenceLevel"] == "fallback"


def test_get_real_metrics_evidence_level_is_measured(monkeypatch) -> None:
    """Prometheus queries returning real values -> measured tier."""
    service = PrometheusMetricsService("http://prometheus.local")
    monkeypatch.setattr(service, "query", lambda promql: 10.0)

    metrics = service.get_real_metrics()

    assert metrics is not None
    assert metrics["dataSource"] == "prometheus"
    assert metrics["evidenceLevel"] == "measured"


def test_get_real_metrics_returns_none_when_all_queries_fail(monkeypatch) -> None:
    """When every Prometheus query fails, current_metrics() falls back to
    unavailable_metrics(), which must still carry the fallback evidence level."""
    service = PrometheusMetricsService("http://prometheus.local")
    monkeypatch.setattr(service, "query", lambda promql: None)

    assert service.get_real_metrics() is None

    metrics = service.current_metrics()

    assert metrics["dataSource"] == "unavailable"
    assert metrics["evidenceLevel"] == "fallback"


def test_metrics_from_free5gc_evidence_level_is_estimated() -> None:
    """Registered-UE-count-derived metrics (no real traffic measurement) -> estimated tier."""
    service = PrometheusMetricsService("")
    registered_ues = [{"PduSessions": [{"SNssai": {"Sst": 1}}]}]

    metrics = service.metrics_from_free5gc(registered_ues)

    assert metrics["dataSource"] == "free5gc"
    assert metrics["evidenceLevel"] == "estimated"
