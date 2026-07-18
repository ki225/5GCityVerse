from __future__ import annotations

from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings


class _FakeEventRepository:
    """Stand-in for EventRepository's NEF-hits persistence, used because
    control_plane_runtime_metrics now reads recorded hits from DynamoDB (via
    self.events) instead of the in-process ToolGateway.nef_tool_hits list."""

    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self._hits = hits or []

    def recent_nef_tool_hits(self) -> list[dict[str, Any]]:
        return list(self._hits)


class _FakeK8sClientWithNefPod:
    """Simulates the confirmed live behavior: the NEF pod is Running, but its log
    never contains a [GIN] access-log line (free5GC's NEF only logs
    Main/CFG/CTX/SBI startup lines), so access_log_pattern can never observe
    northbound (Nnef) traffic from pod logs alone."""

    def __init__(self, nef_log_text: str = "") -> None:
        self.nef_log_text = nef_log_text

    def request(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, Any]:
        if "/pods" in path and "metrics.k8s.io" not in path:
            return 200, {
                "items": [
                    {
                        "metadata": {"name": "free5gc-free5gc-nef-nef-abc123", "labels": {}},
                        "status": {"phase": "Running", "podIP": "10.60.1.50"},
                    }
                ]
            }
        if "metrics.k8s.io" in path:
            return 200, {"items": []}
        if "horizontalpodautoscalers" in path:
            return 404, {}
        return 200, {}

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        if "/log" in path:
            return 200, self.nef_log_text
        return 200, ""


def _build_app(monkeypatch, nef_log_text: str = "") -> tuple[CityVerseBackendApp, _FakeK8sClientWithNefPod]:
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")

    backend = CityVerseBackendApp(AppSettings())
    backend.settings.baseline_traffic_enabled = False

    fake_client = _FakeK8sClientWithNefPod(nef_log_text)
    monkeypatch.setattr(app_module, "get_eks_client", lambda cluster_name: fake_client)

    return backend, fake_client


REAL_NEF_STARTUP_LOG = (
    '2026-07-04T23:38:06.740077622Z[36m [INFO][NEF][Main] [0mNEF version:\n'
    '2026-07-04T23:38:06.741470619Z[36m [INFO][NEF][CFG] [0m==================\n'
    '2026-07-04T23:38:06.742479497Z[36m [INFO][NEF][SBI] [0mStart SBI server (listen on 0.0.0.0:8080)\n'
    '2026-07-04T23:38:06.810390583Z[36m [INFO][NEF][Main] [0mregister to NRF successfully\n'
)


def test_control_plane_runtime_metrics_has_no_nnef_entry_without_tool_hits(monkeypatch) -> None:
    """Reproduces the reported bug: with only NEF's real (GIN-less) startup log
    and no recorded tool_gateway hits, no Nnef observation is produced."""
    backend, fake_client = _build_app(monkeypatch, REAL_NEF_STARTUP_LOG)

    metrics = backend.eks_scaling_state(include_runtime_logs=True, include_hpa=False)

    traffic = metrics.get("runtimeMetrics", {}).get("controlPlaneTraffic", [])
    assert not any("Nnef" in str(entry.get("protocol")) for entry in traffic)


def test_control_plane_runtime_metrics_emits_nnef_entry_from_recorded_tool_hit(monkeypatch) -> None:
    """The fix: a successful NEF-backed tool call recorded in DynamoDB (via
    EventRepository.record_nef_tool_hit) surfaces as a controlPlaneTraffic entry
    even though the NEF pod log itself has no parseable access-log line, and even
    though event execution and this status read run in separate Lambda containers
    that share no in-process memory."""
    backend, fake_client = _build_app(monkeypatch, REAL_NEF_STARTUP_LOG)
    backend.events = _FakeEventRepository(
        [
            {
                "tool": "create_pfd_rule",
                "protocol": "Nnef PFD management",
                "api": "create_pfd_rule",
                "observedAt": "2026-07-05T00:00:00Z",
            }
        ]
    )

    metrics = backend.eks_scaling_state(include_runtime_logs=True, include_hpa=False)

    traffic = metrics.get("runtimeMetrics", {}).get("controlPlaneTraffic", [])
    nnef_entries = [entry for entry in traffic if entry.get("protocol") == "Nnef PFD management"]
    assert len(nnef_entries) == 1
    entry = nnef_entries[0]
    assert entry["sourceNodeId"] == "nef"
    assert entry["targetNodeId"] == "pcf"
    assert entry["evidenceCount"] == 1


def test_control_plane_runtime_metrics_ignores_tool_hits_when_nef_pod_absent(monkeypatch) -> None:
    """Guards against fabricating NEF signaling on the map when no NEF pod is
    actually deployed/running in the cluster."""
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")
    backend = CityVerseBackendApp(AppSettings())
    backend.settings.baseline_traffic_enabled = False

    class _NoNefK8sClient:
        def request(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, Any]:
            if "/pods" in path and "metrics.k8s.io" not in path:
                return 200, {"items": []}
            if "metrics.k8s.io" in path:
                return 200, {"items": []}
            if "horizontalpodautoscalers" in path:
                return 404, {}
            return 200, {}

        def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
            return 200, ""

    monkeypatch.setattr(app_module, "get_eks_client", lambda cluster_name: _NoNefK8sClient())
    backend.events = _FakeEventRepository(
        [
            {
                "tool": "create_pfd_rule",
                "protocol": "Nnef PFD management",
                "api": "create_pfd_rule",
                "observedAt": "2026-07-05T00:00:00Z",
            }
        ]
    )

    metrics = backend.eks_scaling_state(include_runtime_logs=True, include_hpa=False)

    assert metrics.get("runtimeMetrics", {}).get("controlPlaneTraffic", []) == []
