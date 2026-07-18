from __future__ import annotations

from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings


class _FakeK8sClient:
    """Stand-in for EksKubernetesClient that never touches the network.

    Every call to `request` is counted so tests can assert whether
    `eks_scaling_state` actually reached the "K8s API" or served from cache.
    Always returns an empty pod list, which short-circuits the log-scraping
    helpers (ueransim_runtime_metrics/iperf3_runtime_metrics/
    control_plane_runtime_metrics) into no-ops without further requests.
    """

    def __init__(self) -> None:
        self.request_count = 0

    def request(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, Any]:
        self.request_count += 1
        if "/pods" in path and "metrics.k8s.io" not in path:
            return 200, {"items": []}
        if "metrics.k8s.io" in path:
            return 200, {"items": []}
        if "horizontalpodautoscalers" in path:
            return 404, {}
        return 200, {}


def _build_app(monkeypatch) -> tuple[CityVerseBackendApp, _FakeK8sClient]:
    # No DYNAMODB_TABLE / EKS_CLUSTER_NAME / PROMETHEUS_URL / FREE5GC_WEBUI_URL
    # means __init__ never touches boto3 or the network.
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")

    backend = CityVerseBackendApp(AppSettings())

    fake_client = _FakeK8sClient()
    monkeypatch.setattr(app_module, "get_eks_client", lambda cluster_name: fake_client)
    # Baseline traffic reconcile would also call get_eks_client via ensure_baseline_runtime;
    # disable it so metrics calls only exercise eks_scaling_state.
    backend.settings.baseline_traffic_enabled = False

    return backend, fake_client


def test_current_metrics_second_call_within_ttl_does_not_hit_k8s_again(monkeypatch) -> None:
    backend, fake_client = _build_app(monkeypatch)

    backend.current_metrics()
    count_after_first = fake_client.request_count
    assert count_after_first > 0

    backend.current_metrics()

    assert fake_client.request_count == count_after_first


def test_current_metrics_refetches_after_ttl_expires(monkeypatch) -> None:
    backend, fake_client = _build_app(monkeypatch)
    backend._metrics_cache_ttl = 5.0

    fake_time = {"t": 1000.0}
    monkeypatch.setattr(app_module.time, "time", lambda: fake_time["t"])

    backend.current_metrics()
    count_after_first = fake_client.request_count
    assert count_after_first > 0

    fake_time["t"] += 6.0  # past the 5s TTL
    backend.current_metrics()

    assert fake_client.request_count > count_after_first


def test_reset_invalidates_metrics_and_scaling_state_cache(monkeypatch) -> None:
    backend, fake_client = _build_app(monkeypatch)

    class _ResetEvents:
        def claim_reset_job(self, *_args):
            return True

        def update_reset_job(self, *_args):
            return True

        def release_session_lease(self, *_args):
            return True

    backend.events = _ResetEvents()
    monkeypatch.setattr(backend.environment, "cleanup_all_event_runtime", lambda _client: {"status": "ok"})
    monkeypatch.setattr(backend.environment, "recycle_session_state", lambda _client: {"status": "ok"})
    monkeypatch.setattr(backend, "safe_free5gc_status", lambda: {})
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    backend.current_metrics()
    count_after_first = fake_client.request_count
    assert count_after_first > 0

    backend.handle_reset_worker({"sessionId": "browser-a", "resetId": "reset-1"})

    assert backend._metrics_cache is None
    assert backend._metrics_cache_at == 0.0
    assert backend._scaling_state_cache is None
    assert backend._scaling_state_cache_at == 0.0

    backend.current_metrics()

    assert fake_client.request_count > count_after_first


def test_invalidate_metrics_cache_clears_all_cache_fields(monkeypatch) -> None:
    """Directly exercise the cache hook used by the async reset worker."""
    backend, fake_client = _build_app(monkeypatch)

    backend.current_metrics()
    assert backend._metrics_cache is not None
    count_after_first = fake_client.request_count

    backend.invalidate_metrics_cache()

    assert backend._metrics_cache is None
    assert backend._metrics_cache_at == 0.0
    assert backend._scaling_state_cache is None
    assert backend._scaling_state_cache_at == 0.0

    backend.current_metrics()

    assert fake_client.request_count > count_after_first


def test_eks_scaling_state_shares_cache_between_default_and_explicit_true_args(monkeypatch) -> None:
    backend, fake_client = _build_app(monkeypatch)

    backend.eks_scaling_state()
    count_after_first = fake_client.request_count
    assert count_after_first > 0

    # free5gc_status() calls eks_scaling_state(include_runtime_logs=True, include_hpa=True),
    # which matches current_metrics()'s defaults and should hit the same cache entry.
    backend.eks_scaling_state(include_runtime_logs=True, include_hpa=True)

    assert fake_client.request_count == count_after_first


def test_eks_scaling_state_non_default_args_bypass_cache(monkeypatch) -> None:
    backend, fake_client = _build_app(monkeypatch)

    backend.eks_scaling_state()
    count_after_first = fake_client.request_count
    assert count_after_first > 0

    backend.eks_scaling_state(include_runtime_logs=False, include_hpa=False)

    assert fake_client.request_count > count_after_first
