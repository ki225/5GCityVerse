from __future__ import annotations

import json
from typing import Any

from app import CityVerseBackendApp
from config import AppSettings

# Real iperf3 --json TCP client summary: the "end.sum" object has no "packets"
# field for TCP tests, only for UDP. throughput is still reported correctly.
TCP_CLIENT_LOG = json.dumps(
    {
        "end": {
            "sum_sent": {"bits_per_second": 554_000_000, "seconds": 10.0},
            "sum_received": {"bits_per_second": 554_000_000, "seconds": 10.0},
        }
    }
)

# Real iperf3 --json UDP client summary: "end.sum" includes "packets", so a
# real packets/sec figure can be derived.
UDP_CLIENT_LOG = json.dumps(
    {
        "end": {
            "sum": {"bits_per_second": 2_000_000, "seconds": 10.0, "packets": 5000},
        }
    }
)


def _iperf3_pod(name: str = "iperf3-city-concert-abcde", scenario: str = "concert") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"5gcityverse.io/scenario": scenario},
        },
        "status": {"phase": "Running", "startTime": "2026-07-04T00:00:00Z"},
        "spec": {"containers": [{"name": "ueransim-ue"}, {"name": "iperf3-client"}]},
    }


class _FakeK8sClient:
    def __init__(self, log_text: str) -> None:
        self._log_text = log_text

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        return 200, self._log_text


def _build_app() -> CityVerseBackendApp:
    return CityVerseBackendApp(AppSettings())


def test_tcp_throughput_without_packet_evidence_reports_gtp_packets_as_unavailable() -> None:
    """TCP iperf3 tests never report a packets/sec figure. Regression test for the
    dashboard showing "554 Mbps throughput, 0 GTP pkt/s" -- summing None samples
    used to silently default to 0.0, which looks like real (and contradictory)
    telemetry. Missing packet evidence must surface as None/"unavailable", not 0.
    """
    backend = _build_app()
    k8s = _FakeK8sClient(TCP_CLIENT_LOG)

    metrics = backend.iperf3_runtime_metrics(k8s, [_iperf3_pod()])

    assert metrics["throughputMbps"] == 554.0
    assert metrics["gtpPacketsPerSec"] is None
    assert metrics["gtpPacketsSource"] == "unavailable"


def test_udp_throughput_with_packet_evidence_reports_real_packets_per_sec() -> None:
    """UDP iperf3 tests do report a packets count, so a real pkt/s figure should
    still be computed and surfaced (no gtpPacketsSource marker needed)."""
    backend = _build_app()
    k8s = _FakeK8sClient(UDP_CLIENT_LOG)

    metrics = backend.iperf3_runtime_metrics(k8s, [_iperf3_pod()])

    assert metrics["throughputMbps"] == 2.0
    assert metrics["gtpPacketsPerSec"] == 500.0
    assert "gtpPacketsSource" not in metrics


def test_completed_event_job_is_not_reported_as_current_traffic() -> None:
    """Succeeded jobs are retained briefly by Kubernetes, but their logs are
    historical and must not keep a finished scenario visible on the live map."""
    backend = _build_app()
    k8s = _FakeK8sClient(UDP_CLIENT_LOG)
    pod = _iperf3_pod()
    pod["status"]["phase"] = "Succeeded"

    assert backend.iperf3_runtime_metrics(k8s, [pod]) == {}


def test_interval_parser_keeps_latest_nonzero_measurement_when_tail_is_zero() -> None:
    log_text = "\n".join([
        "[  5]   8.00-9.00   sec  1.19 MBytes  10.0 Mbits/sec  6244",
        "[  5]   9.00-10.00  sec  0.00 Bytes  0.00 bits/sec  0",
    ])

    sample = CityVerseBackendApp.parse_iperf3_interval_sample(log_text)

    assert sample is not None
    assert sample["bitsPerSecond"] == 10_000_000


def test_runtime_wait_reads_eks_traffic_directly_and_broadcasts_it() -> None:
    backend = _build_app()
    metrics = {
        "scenarioTraffic": [{"scenario": "medical", "throughputMbps": 10.0, "transport": "free5gc-tun"}],
        "throughputMbps": 10.0,
        "dataSource": "eks+iperf3",
    }
    backend.eks_scaling_state = lambda **_kwargs: {"runtimeMetrics": metrics}  # type: ignore[method-assign]
    backend.current_metrics = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Prometheus path must not run"))  # type: ignore[method-assign]
    broadcasts: list[dict[str, Any]] = []
    backend.broadcast = broadcasts.append  # type: ignore[method-assign]

    assert backend.wait_for_runtime_scenarios(["medical"], timeout_seconds=1) == ["medical"]
    assert any(message.get("type") == "metrics_update" and message.get("payload") == metrics for message in broadcasts)
    snapshot = next(message["payload"] for message in broadcasts if message.get("type") == "network_snapshot")
    assert any(edge.get("scenario") == "medical" for edge in snapshot["edges"])


def test_runtime_wait_accumulates_scenarios_across_rotating_log_windows(monkeypatch) -> None:
    backend = _build_app()
    observed_per_poll = iter([
        [{"scenario": "concert", "throughputMbps": 800.0}],
        [{"scenario": "typhoon", "throughputMbps": 5.0}],
        [{"scenario": "iot_surge", "throughputMbps": 2.4}],
    ])

    def scaling_state(**_kwargs):
        traffic = next(observed_per_poll)
        return {"runtimeMetrics": {"scenarioTraffic": traffic, "throughputMbps": traffic[0]["throughputMbps"]}}

    backend.eks_scaling_state = scaling_state  # type: ignore[method-assign]
    backend.broadcast = lambda _message: None  # type: ignore[method-assign]
    fake_now = {"value": 1_000.0}
    monkeypatch.setattr("app.time.time", lambda: fake_now["value"])
    monkeypatch.setattr("app.time.sleep", lambda seconds: fake_now.__setitem__("value", fake_now["value"] + seconds))

    observed = backend.wait_for_runtime_scenarios(["concert", "typhoon", "iot_surge"], timeout_seconds=10)

    assert observed == ["concert", "iot_surge", "typhoon"]


def test_runtime_wait_rejects_partial_first_interval_below_scaled_target(monkeypatch) -> None:
    backend = _build_app()
    samples = iter([5.14, 11.9])

    def scaling_state(**_kwargs):
        throughput = next(samples)
        return {
            "runtimeMetrics": {
                "scenarioTraffic": [{"scenario": "concert", "throughputMbps": throughput}],
                "throughputMbps": throughput,
            }
        }

    backend.eks_scaling_state = scaling_state  # type: ignore[method-assign]
    backend.broadcast = lambda _message: None  # type: ignore[method-assign]
    fake_now = {"value": 1_000.0}
    monkeypatch.setattr("app.time.time", lambda: fake_now["value"])
    monkeypatch.setattr("app.time.sleep", lambda seconds: fake_now.__setitem__("value", fake_now["value"] + seconds))

    observed = backend.wait_for_runtime_scenarios(
        ["concert"],
        timeout_seconds=10,
        minimum_mbps={"concert": 9.0},
    )

    assert observed == ["concert"]


def test_current_metrics_skips_prometheus_when_eks_runtime_is_authoritative() -> None:
    backend = _build_app()
    backend.ensure_baseline_runtime = lambda: None  # type: ignore[method-assign]
    runtime_metrics = {
        "scenarioTraffic": [{"scenario": "concert", "throughputMbps": 12.0}],
        "throughputMbps": 12.0,
        "dataSource": "eks+iperf3",
    }
    backend.eks_scaling_state = lambda **_kwargs: {  # type: ignore[method-assign]
        "podCounts": {"UERANSIM": 1},
        "runtimeMetrics": runtime_metrics,
    }
    backend.metrics.current_metrics = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("Prometheus must not run before authoritative EKS metrics")
    )

    metrics = backend._current_metrics_uncached(include_eks=True, include_oam=False)

    assert metrics["throughputMbps"] == 12.0
    assert metrics["scenarioTraffic"] == runtime_metrics["scenarioTraffic"]


def test_current_slices_disables_prometheus_in_eks_topology() -> None:
    backend = _build_app()
    backend.settings.eks_cluster_name = "test-cluster"
    backend.current_metrics = lambda **_kwargs: {  # type: ignore[method-assign]
        "scenarioTraffic": [{"scenario": "concert", "throughputMbps": 12.0}],
        "throughputMbps": 12.0,
        "dataSource": "eks+iperf3",
    }
    calls = []

    def current_slices(**kwargs):
        calls.append(kwargs)
        return []

    backend.free5gc.current_slices = current_slices  # type: ignore[method-assign]

    backend.current_slices()

    assert calls[0]["query_prometheus"] is False
