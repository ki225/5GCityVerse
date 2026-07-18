from __future__ import annotations

from typing import Any

from app import CityVerseBackendApp
from config import AppSettings

# Real UERANSIM log format for a single-UE container (e.g. ueransim-city-mmtc):
# no numeric "<imsi>|nas" index prefix, just a bare "[nas]" tag.
SINGLE_UE_LOG = (
    "[2026-07-04 15:16:38.704] [nas] [info] Initial Registration is successful\n"
    "[2026-07-04 15:16:39.033] [nas] [info] PDU Session establishment is successful PSI[1]\n"
)

# Multi-UE container log format, where UERANSIM prefixes each line with the
# UE's numeric index so several UEs sharing one container can be told apart.
MULTI_UE_LOG = (
    "[1|nas] [info] Initial Registration is successful\n"
    "[1|nas] [info] PDU Session establishment is successful PSI[1]\n"
    "[2|nas] [info] Initial Registration is successful\n"
    "[2|nas] [info] PDU Session establishment is successful PSI[1]\n"
)


def _mmtc_pod(name: str = "ueransim-city-mmtc-69c8698f8b-zpjlw") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"5gcityverse.io/scenario": "baseline-mmtc"},
        },
        "status": {"phase": "Running"},
        "spec": {"containers": [{"name": "ueransim-ue"}, {"name": "ue-tun-probe"}]},
    }


class _FakeK8sClient:
    def __init__(self, log_text: str) -> None:
        self._log_text = log_text

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        if "container=ueransim-ue" in path:
            return 200, self._log_text
        # ue-tun-probe log query; no probe samples needed for this test.
        return 404, ""


def _build_app() -> CityVerseBackendApp:
    return CityVerseBackendApp(AppSettings())


def test_ueransim_runtime_metrics_counts_single_ue_container_log_format() -> None:
    """Regression test for the mMTC pod's PDU session never being counted.

    ueransim-city-mmtc runs a single-UE ueransim-ue container, which logs
    "[nas] ... PDU Session establishment is successful" with no numeric
    "<imsi>|nas" prefix. The old regex required that prefix, so it never
    matched real single-UE logs and slice_sessions for mMTC (sst=3) stayed
    empty even though the session was actually established.
    """
    backend = _build_app()
    k8s = _FakeK8sClient(SINGLE_UE_LOG)

    metrics = backend.ueransim_runtime_metrics(k8s, [_mmtc_pod()])

    assert metrics["registeredUeCount"] == 1
    assert metrics["pduSessionCount"] == 1
    assert metrics["sliceSessions"] == {3: 1}


def test_ueransim_runtime_metrics_still_counts_multi_ue_container_log_format() -> None:
    """The original "<imsi>|nas" prefixed format (multiple UEs sharing one
    container) must keep working and keep deduping per-IMSI."""
    backend = _build_app()
    k8s = _FakeK8sClient(MULTI_UE_LOG)

    metrics = backend.ueransim_runtime_metrics(k8s, [_mmtc_pod()])

    assert metrics["registeredUeCount"] == 2
    assert metrics["pduSessionCount"] == 2
    assert metrics["sliceSessions"] == {3: 2}


def test_latest_ue_tun_probe_does_not_reuse_an_old_success_after_latest_failure() -> None:
    backend = _build_app()
    log = (
        'UE_TUN_METRICS {"ready":true,"receivedPackets":5,"packetLossPercent":0}\n'
        'UE_TUN_METRICS {"ready":false,"reason":"no_echo_reply","receivedPackets":0,"packetLossPercent":100}\n'
    )

    assert backend.latest_ue_tun_probe_sample(log) is None


def test_citizen_experience_uses_only_the_resident_ue_probe() -> None:
    backend = _build_app()
    citizen = {
        "metadata": {"name": "ueransim-city-ue-abc", "labels": {}},
        "status": {"phase": "Running"},
        "spec": {"containers": [{"name": "ue"}, {"name": "ue-tun-probe"}]},
    }
    mmtc = _mmtc_pod()

    class ProbeClient:
        def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
            if "container=ue-tun-probe" in path:
                if "ueransim-city-ue-abc" in path:
                    return 200, 'UE_TUN_METRICS {"ready":true,"throughputMbps":0.01,"latencyMs":5,"packetLossPercent":0,"receivedPackets":5}'
                return 200, 'UE_TUN_METRICS {"ready":true,"throughputMbps":0,"latencyMs":0,"packetLossPercent":100,"receivedPackets":0}'
            return 200, SINGLE_UE_LOG

    metrics = backend.ueransim_runtime_metrics(ProbeClient(), [mmtc, citizen])

    assert metrics["ueTunProbe"]["packetLossPercent"] == 0
    assert metrics["ueTunProbe"]["receivedPackets"] == 5
    assert metrics["pduSessionCount"] == sum(metrics["sliceSessions"].values()) + metrics["unassignedSessionCount"]
    assert metrics["unassignedSessionCount"] == 1
