from __future__ import annotations

import json
from typing import Any

import app as app_module
from app import CityVerseBackendApp
from config import AppSettings


SOURCE = "gtp5g-upfgtp-interface"
NOW = 1_784_188_800.0  # 2026-07-16T08:00:00Z


def _sample(
    pod: str,
    slice_name: str,
    timestamp: str = "2026-07-16T08:00:00Z",
    *,
    gtp_pps: float = 100.0,
    rx_pps: float = 60.0,
    tx_pps: float = 40.0,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "pod": pod,
        "slice": slice_name,
        "rxPackets": 1000,
        "txPackets": 800,
        "rxPps": rx_pps,
        "txPps": tx_pps,
        "gtpPacketsPerSec": gtp_pps,
        "rxDropsDelta": 1,
        "txDropsDelta": 2,
        "rxErrorsDelta": 0,
        "txErrorsDelta": 0,
        "source": SOURCE,
    }


def _exporter_pod(name: str, phase: str = "Running") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {"app.kubernetes.io/name": "gtp5g-metrics-exporter"}},
        "status": {"phase": phase},
        "spec": {"containers": [{"name": "exporter"}]},
    }


class _FakeK8s:
    def __init__(self, logs: dict[str, str]) -> None:
        self.logs = logs
        self.log_paths: list[str] = []

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        self.log_paths.append(path)
        for pod_name, log_text in self.logs.items():
            if f"/pods/{pod_name}/log?" in path:
                return 200, log_text
        return 404, ""


def _app() -> CityVerseBackendApp:
    backend = CityVerseBackendApp(AppSettings())
    backend._gtp5g_metrics_max_age_seconds = 20
    return backend


def test_parser_accepts_only_complete_fresh_reasonable_json_samples() -> None:
    fresh = _sample("upf-embb-abc", "eMBB", "2026-07-16T07:59:55Z")
    stale = _sample("upf-old", "eMBB", "2026-07-16T07:59:30Z")
    future = _sample("upf-future", "eMBB", "2026-07-16T08:00:06Z")
    negative = _sample("upf-negative", "eMBB")
    negative["rxDropsDelta"] = -1
    wrong_source = _sample("upf-wrong", "eMBB")
    wrong_source["source"] = "estimated"
    missing = _sample("upf-missing", "eMBB")
    del missing["txPps"]
    log_text = "\n".join(
        [
            "not-json",
            json.dumps(stale),
            json.dumps(future),
            json.dumps(negative),
            json.dumps(wrong_source),
            json.dumps(missing),
            json.dumps(fresh),
        ]
    )

    parsed = CityVerseBackendApp.parse_gtp5g_metrics_samples(
        log_text, now_epoch_seconds=NOW, max_age_seconds=20
    )

    assert len(parsed) == 1
    assert parsed[0]["pod"] == "upf-embb-abc"
    assert parsed[0]["timestampEpochMs"] == 1_784_188_795_000


def test_reader_deduplicates_rollout_samples_and_aggregates_total_and_per_slice(monkeypatch) -> None:
    monkeypatch.setattr("app.time.time", lambda: NOW)
    older_embb = _sample("upf-embb-abc", "eMBB", "2026-07-16T07:59:55Z", gtp_pps=90)
    latest_embb = _sample("upf-embb-abc", "eMBB", gtp_pps=120, rx_pps=70, tx_pps=50)
    urllc = _sample("upf-urllc-def", "URLLC", gtp_pps=30, rx_pps=20, tx_pps=10)
    k8s = _FakeK8s(
        {
            "exporter-old": "\n".join((json.dumps(older_embb), json.dumps(urllc))),
            "exporter-new": json.dumps(latest_embb),
        }
    )

    metrics = _app().gtp5g_metrics_exporter_runtime_metrics(
        k8s,
        [_exporter_pod("exporter-old"), _exporter_pod("exporter-new"), _exporter_pod("stopped", "Succeeded")],
    )

    assert metrics["gtpPacketsPerSec"] == 150.0
    assert metrics["gtpPacketsSource"] == SOURCE
    assert metrics["gtpMetrics"]["sampleCount"] == 2
    assert metrics["gtpMetrics"]["total"]["rxPps"] == 90.0
    assert metrics["gtpMetrics"]["total"]["txDropsDelta"] == 4.0
    assert metrics["gtpMetrics"]["perSlice"]["eMBB"]["gtpPacketsPerSec"] == 120.0
    assert metrics["gtpMetrics"]["perSlice"]["URLLC"]["gtpPacketsPerSec"] == 30.0
    assert len(k8s.log_paths) == 2
    assert all("sinceSeconds=30&tailLines=100" in path for path in k8s.log_paths)


def test_reader_returns_empty_without_a_fresh_sample(monkeypatch) -> None:
    monkeypatch.setattr("app.time.time", lambda: NOW)
    stale_log = json.dumps(_sample("upf-embb-abc", "eMBB", "2026-07-16T07:59:00Z"))

    metrics = _app().gtp5g_metrics_exporter_runtime_metrics(
        _FakeK8s({"exporter": stale_log}), [_exporter_pod("exporter")]
    )

    assert metrics == {}


def test_scaling_state_exporter_overwrites_tcp_unavailable_gtp_fields(monkeypatch) -> None:
    monkeypatch.setattr("app.time.time", lambda: NOW)
    backend = _app()
    backend.settings.eks_cluster_name = "test-cluster"
    backend.settings.free5gc_namespace = "free5gc"
    exporter = _exporter_pod("exporter")

    class _ScalingK8s(_FakeK8s):
        def request(self, method: str, path: str, **_kwargs: Any) -> tuple[int, Any]:
            if path == "/api/v1/namespaces/free5gc/pods":
                return 200, {"items": [exporter]}
            return 404, {}

    k8s = _ScalingK8s({"exporter": json.dumps(_sample("upf-embb-abc", "eMBB", gtp_pps=321))})
    monkeypatch.setattr(app_module, "get_eks_client", lambda _cluster: k8s)
    backend.component_cpu_from_metrics_api = lambda _k8s: {}  # type: ignore[method-assign]
    backend.ueransim_runtime_metrics = lambda _k8s, _pods: {}  # type: ignore[method-assign]
    backend.iperf3_runtime_metrics = lambda _k8s, _pods: {  # type: ignore[method-assign]
        "throughputMbps": 554.0,
        "gtpPacketsPerSec": None,
        "gtpPacketsSource": "unavailable",
    }
    backend.control_plane_runtime_metrics = lambda _k8s, _pods: {}  # type: ignore[method-assign]

    state = backend._eks_scaling_state_uncached(include_runtime_logs=True, include_hpa=False)

    runtime = state["runtimeMetrics"]
    assert runtime["throughputMbps"] == 554.0
    assert runtime["gtpPacketsPerSec"] == 321.0
    assert runtime["gtpPacketsSource"] == SOURCE
    assert runtime["gtpMetrics"]["perSlice"]["eMBB"]["gtpPacketsPerSec"] == 321.0
