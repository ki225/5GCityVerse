from __future__ import annotations

import datetime as dt

from agent_runtime.data_plane_evidence_reader import KubernetesDataPlaneEvidenceReader


def _artifact(execution_id: str = "exec-nonce-1", kernel_teid: str = "0xabc") -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    collected = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    not_before = (now - dt.timedelta(seconds=10)).isoformat()
    text = f"""BEGIN_ARTIFACT executionId={execution_id} sst=3 sd=000004 dnn=iot collectedAt={collected}
BEGIN_KERNEL
PDR
PDR-ID: 1 SEID=101 TEID: {kernel_teid}
FAR
FAR-ID: 2
QER
QER-ID: 9
END_KERNEL
BEGIN_PFCP
PFCP Session Modification Request SEID=101 TEID=0xabc QER ID=9 SST=3 SD=000004 DNN=iot
PFCP Session Modification Response SEID=101 TEID=0xabc accepted SST=3 SD=000004 DNN=iot
END_PFCP
BEGIN_IPERF
PROVENANCE pod=iperf3-iot-abc source=iperf3
[  5] 0.00-1.00 sec 0.12 MBytes 1.0 Mbits/sec receiver
END_IPERF
END_ARTIFACT"""
    return text, not_before


def test_reader_correlates_nonce_slice_teid_and_iperf_provenance() -> None:
    reader = KubernetesDataPlaneEvidenceReader("cluster", "free5gc")
    text, not_before = _artifact()
    result = reader.correlate(text, "exec-nonce-1", 3, "000004", "iot", not_before, 1.0, 5.0)
    assert result["correlation"]["sharedTeids"] == ["2748"]
    assert result["correlation"]["sharedSeids"] == ["101"]
    assert result["effect"]["measurementSource"] == "ue-tun-iperf3"
    assert result["effect"]["afterMbps"] == 1.0


def test_reader_correlates_hex_pfcp_with_decimal_kernel_json() -> None:
    text, not_before = _artifact(kernel_teid="2748")
    reader = KubernetesDataPlaneEvidenceReader("cluster", "free5gc")

    result = reader.correlate(text, "exec-nonce-1", 3, "000004", "iot", not_before, 1.0, 5.0)

    assert result["correlation"]["sharedTeids"] == ["2748"]


def test_reader_rejects_wrong_nonce_or_pfcp_kernel_teid_mismatch() -> None:
    reader = KubernetesDataPlaneEvidenceReader("cluster", "free5gc")
    text, not_before = _artifact()
    assert reader.correlate(text, "forged", 3, "000004", "iot", not_before, 1.0, 5.0) == {}
    mismatch, not_before = _artifact(kernel_teid="0xdef")
    assert reader.correlate(mismatch, "exec-nonce-1", 3, "000004", "iot", not_before, 1.0, 5.0) == {}


def test_reader_rejects_wrong_snssai_or_dnn() -> None:
    reader = KubernetesDataPlaneEvidenceReader("cluster", "free5gc")
    text, not_before = _artifact()
    assert reader.correlate(text, "exec-nonce-1", 2, "000004", "iot", not_before, 1.0, 5.0) == {}
    assert reader.correlate(text, "exec-nonce-1", 3, "000004", "citizen", not_before, 1.0, 5.0) == {}


class _FakeK8sClient:
    def __init__(self, log: str) -> None:
        self.log = log

    def request(self, method: str, path: str):
        assert method == "GET"
        assert "pfcp-evidence-collector" in path
        return 200, {"items": [{"metadata": {"name": "collector-job-1"}, "status": {"startTime": "2026-01-01T00:00:00Z"}}]}

    def request_text(self, method: str, path: str):
        assert method == "GET"
        assert path.endswith("/pods/collector-job-1/log")
        return 200, self.log


def test_reader_consumes_collector_pod_log_via_read_only_kubernetes_api() -> None:
    text, not_before = _artifact()
    client = _FakeK8sClient(text)
    reader = KubernetesDataPlaneEvidenceReader("cluster", "free5gc", client_factory=lambda _name: client)
    result = reader.read("exec-nonce-1", 3, "000004", "iot", not_before, 1.0, 5.0)
    assert result["reader"] == "kubernetes-job-log"
    assert result["collectorPod"] == "collector-job-1"
