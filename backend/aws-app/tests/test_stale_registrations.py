from __future__ import annotations

from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings


class _FakeK8sClient:
    """Stand-in for EksKubernetesClient returning a fixed set of pods:
    2 Running UERANSIM UE pods (+1 gNB pod, which must not count as a UE),
    so eks_scaling_state()'s podComponents reflect a known "actual" UE count."""

    def __init__(self) -> None:
        self.request_count = 0

    def request(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, Any]:
        self.request_count += 1
        if "/pods" in path and "metrics.k8s.io" not in path:
            return 200, {
                "items": [
                    _pod("ueransim-city-ue-embb-1", "Running"),
                    _pod("ueransim-city-ue-urllc-1", "Running"),
                    _pod("ueransim-gnb-1", "Running"),
                ]
            }
        if "metrics.k8s.io" in path:
            return 200, {"items": []}
        if "horizontalpodautoscalers" in path:
            return 404, {}
        return 200, {}

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        return 404, ""


def _pod(name: str, phase: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {}},
        "status": {"phase": phase},
        "spec": {"containers": [{"name": "ueransim-ue"}]},
    }


def _build_app(monkeypatch) -> tuple[CityVerseBackendApp, _FakeK8sClient]:
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")

    backend = CityVerseBackendApp(AppSettings())
    backend.settings.baseline_traffic_enabled = False

    fake_client = _FakeK8sClient()
    monkeypatch.setattr(app_module, "get_eks_client", lambda cluster_name: fake_client)

    return backend, fake_client


def test_stale_registrations_is_difference_between_registered_and_active_ue_pods(monkeypatch) -> None:
    backend, _ = _build_app(monkeypatch)

    monkeypatch.setattr(
        backend.free5gc,
        "status_payload",
        lambda metrics=None, slices=None: {
            "connected": True,
            "source": "free5GC WebUI API",
            "subscriberCount": 55,
            "eventSubscriberCount": 55,
            "registeredUeCount": 55,
            "profileCount": 5,
            "subscribers": [],
            "eventSubscribers": [],
            "registeredUes": [],
            "profiles": [],
            "metrics": {**(metrics or {}), "registeredUeCount": 55, "pduSessionCount": 52, "dataSource": "free5gc"},
            "slices": slices or [],
            "checkedAt": "2026-07-05T00:00:00Z",
        },
    )

    status = backend.free5gc_status()

    assert status["metrics"]["ueransimActivePods"] == 2
    assert status["metrics"]["staleRegistrations"] == 53


def test_stale_registrations_is_zero_when_registered_matches_active_pods(monkeypatch) -> None:
    backend, _ = _build_app(monkeypatch)

    monkeypatch.setattr(
        backend.free5gc,
        "status_payload",
        lambda metrics=None, slices=None: {
            "connected": True,
            "source": "free5GC WebUI API",
            "subscriberCount": 2,
            "eventSubscriberCount": 2,
            "registeredUeCount": 2,
            "profileCount": 2,
            "subscribers": [],
            "eventSubscribers": [],
            "registeredUes": [],
            "profiles": [],
            "metrics": {**(metrics or {}), "registeredUeCount": 2, "pduSessionCount": 2, "dataSource": "free5gc"},
            "slices": slices or [],
            "checkedAt": "2026-07-05T00:00:00Z",
        },
    )

    status = backend.free5gc_status()

    assert status["metrics"]["ueransimActivePods"] == 2
    assert status["metrics"]["staleRegistrations"] == 0


def test_ueransim_active_ue_pod_count_excludes_gnb_pods() -> None:
    scaling_state = {
        "podComponents": [
            {
                "component": "UERANSIM",
                "pods": [
                    {"name": "ueransim-city-ue-embb-1", "phase": "Running"},
                    {"name": "ueransim-city-ue-urllc-1", "phase": "Running"},
                    {"name": "ueransim-gnb-1", "phase": "Running"},
                    {"name": "ueransim-city-ue-mmtc-1", "phase": "Pending"},
                ],
            }
        ]
    }

    assert CityVerseBackendApp.ueransim_active_ue_pod_count(scaling_state) == 2


def test_ueransim_active_ue_pod_count_handles_missing_pod_components() -> None:
    assert CityVerseBackendApp.ueransim_active_ue_pod_count({}) == 0
    assert CityVerseBackendApp.ueransim_active_ue_pod_count({"podComponents": "not-a-list"}) == 0
