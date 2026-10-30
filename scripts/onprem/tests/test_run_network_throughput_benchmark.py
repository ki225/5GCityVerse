import importlib.util
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "run_network_throughput_benchmark.py"
SPEC = importlib.util.spec_from_file_location("network_benchmark_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def report(*, receiver=True, goodput=90_000_000, sender=98_000_000):
    return {
        "start": {"connected": [{"local_host": "10.101.0.2", "remote_host": "172.22.61.20"}],
                  "test_start": {"protocol": "UDP", "blksize": 1200}},
        "end": {
            "sum_sent": {"sender": True, "bits_per_second": sender, "seconds": 10,
                         "bytes": 125_000_000, "packets": 100_000},
            "sum_received": {"sender": False, "bits_per_second": goodput, "seconds": 10,
                             "bytes": 112_500_000, "packets": 100_000,
                             "lost_packets": 100, "out_of_order": 2},
        } if receiver else {
            "sum_sent": {"sender": True, "bits_per_second": sender, "seconds": 10,
                         "bytes": 125_000_000, "packets": 100_000}},
    }


def pod(name, uid, *, ue):
    labels = ({"cityverse.io/managed-by": "onprem-ue-controller",
               "cityverse.io/generation": "18"} if ue else
              {"app.kubernetes.io/name": "n6-embb", "cityverse.io/runtime": "kubeadm",
               "cityverse.io/role": "upf"})
    annotations = ({"cityverse.io/supis": json.dumps(["imsi-208930000000001"])} if ue else
                   {"k8s.v1.cni.cncf.io/network-status": json.dumps([
                       {"name": "n6-embb", "interface": "n6", "ips": ["172.22.61.20/24"]}])})
    containers = [{"name": "traffic", "image": runner.TRAFFIC_IMAGE}]
    if not ue:
        containers.append({"name": "n6-embb", "image": "n6-fixture@sha256:" + "a" * 64})
    return {"metadata": {"name": name, "namespace": runner.NAMESPACE,
                         "uid": uid, "labels": labels, "annotations": annotations},
            "spec": {"containers": containers}}


class FixtureKubectl:
    def __init__(self, *, sender_only=False, identity_mismatch=False,
                 partial=False, residual=False, timeout=False,
                 latency_empty=False, latency_no_overlap=False, latency_failure=False,
                 receiver_network_mismatch=False, missing_reorder=False,
                 cleanup_failure=False):
        self.sender_only = sender_only
        self.identity_mismatch = identity_mismatch
        self.partial = partial
        self.residual = residual
        self.timeout = timeout
        self.latency_empty = latency_empty
        self.latency_no_overlap = latency_no_overlap
        self.latency_failure = latency_failure
        self.receiver_network_mismatch = receiver_network_mismatch
        self.missing_reorder = missing_reorder
        self.cleanup_failure = cleanup_failure
        self.calls = []
        self.client_done = threading.Event()

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        command = argv[argv.index("-n") + 2:]
        if command[:2] == ["get", "pod"]:
            name = command[2]
            if name == "cv-ue-g1-0":
                value = pod(name, "ue-uid", ue=True)
                if self.identity_mismatch:
                    value["metadata"]["uid"] = "replacement"
            else:
                value = pod(name, "receiver-uid", ue=False)
                if self.receiver_network_mismatch:
                    value["metadata"]["annotations"]["k8s.v1.cni.cncf.io/network-status"] = "[]"
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if "ip" in command and "addr" in command:
            receiver = command[1].startswith("n6-")
            value = [{"ifname": "n6" if receiver else "uesimtun0", "addr_info":
                      [{"family": "inet", "local": "172.22.61.20" if receiver else "10.101.0.2"}]}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if "--version" in command:
            return subprocess.CompletedProcess(argv, 0, "iperf 3 fixture", "")
        if "ps -eo args" in " ".join(command):
            failed = self.cleanup_failure and command[1] == "cv-ue-g1-0"
            return subprocess.CompletedProcess(argv, 1 if failed else 0,
                                               "residual\n" if self.residual else "", "")
        if "ping" in command:
            if self.latency_failure:
                return subprocess.CompletedProcess(argv, 127, "", "ping unavailable")
            if not self.latency_no_overlap:
                self.client_done.wait(1)
                # Keep the fixture collector alive beyond the client return so
                # the production overlap check is deterministic under load.
                time.sleep(0.1)
            raw = "" if self.latency_empty else "\n".join(
                f"64 bytes from 172.22.61.20: time={value}.0 ms" for value in range(1, 11))
            return subprocess.CompletedProcess(argv, 0, raw, "")
        if "-c" in command and "--get-server-output" in command:
            if self.timeout:
                self.client_done.set()
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            self.client_done.set()
            time.sleep(0.002)
            return subprocess.CompletedProcess(argv, 0,
                                               json.dumps(report(receiver=not self.sender_only)), "")
        if "-s" in command:
            if self.partial:
                return subprocess.CompletedProcess(argv, 1, "", "server failed")
            value = report(receiver=not self.sender_only)
            if self.missing_reorder and not self.sender_only:
                value["end"]["sum_received"].pop("out_of_order")
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        raise AssertionError(command)


def request(tmp_path, **receipt_changes):
    manifest = runner.build_manifest(
        image="free5gc-upf@sha256:" + "a" * 64, kernel="6.8.0-test", qemu="8.2.2",
        vcpus=2, memory_mib=2048, cni="flannel+multus:fixture", mtu=1500,
        offload={"gro": False, "gso": False, "tso": False}, workload="fixture-workload-v1",
        flow_count=1, packet_bytes=1200, offered_rate_bps=100_000_000,
        duration_seconds=10, warmup_seconds=0, window_seconds=10, queue_count=1)
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"schemaVersion": runner.MANIFEST_SCHEMA, "inputs": {
        "image": manifest.image, "kernel": manifest.kernel, "qemu": manifest.qemu,
        "vcpus": manifest.vcpus, "memoryMiB": manifest.memory_mib, "cni": manifest.cni,
        "mtu": manifest.mtu, "offload": dict(manifest.offload), "workload": manifest.workload,
        "flowCount": manifest.flow_count, "packetBytes": manifest.packet_bytes,
        "offeredRateBps": manifest.offered_rate_bps, "durationSeconds": manifest.duration_seconds,
        "warmupSeconds": manifest.warmup_seconds, "windowSeconds": manifest.window_seconds,
        "queueCount": manifest.queue_count, "profile": manifest.profile},
        "manifestHash": manifest.manifest_hash,
        "frozenInputsHash": manifest.frozen_inputs_hash}), encoding="utf-8")
    receipt = {"schemaVersion": runner.IDENTITY_SCHEMA, "source": runner.IDENTITY_SOURCE,
               "manifestHash": manifest.manifest_hash,
               "observedAt": time.time(), "generation": 18,
               "upfPodUid": "upf-uid", "sessionEpoch": "session-epoch-1",
               "vmEpoch": "vm-epoch-1", "interfaceEpoch": "if-epoch-1",
               "workloadId": "fixture-workload-v1", "receiverId": "receiver-uid",
               "profile": runner.PROFILE, "owner": runner.OWNER,
               "namespace": runner.NAMESPACE, "uePod": "cv-ue-g1-0",
               "uePodUid": "ue-uid", "receiverPod": "n6-embb-fixture",
               "receiverPodUid": "receiver-uid", "supi": "imsi-208930000000001",
               "pduSessionId": "pdu-01", "ueIp": "10.101.0.2",
               "tun": "uesimtun0", "endpoint": "172.22.61.20",
               "pduHealthy": True, "sessionHealthy": True}
    receipt.update(receipt_changes)
    evidence = tmp_path / "identity.json"
    evidence.write_text(json.dumps(receipt), encoding="utf-8")
    return runner.Request("kubeadm-lab", "fixture.conf", runner.NAMESPACE,
                          runner.OWNER, "cv-ue-g1-0", "ue-uid",
                          "n6-embb-fixture", "receiver-uid",
                          "imsi-208930000000001", "pdu-01", "10.101.0.2",
                          "uesimtun0", "172.22.61.20", 15201, 100, 10, 1, 1200,
                          manifest_file, evidence, tmp_path / "artifacts")


def only_run_dir(tmp_path):
    return next((tmp_path / "artifacts").iterdir())


def test_cli_defaults_to_read_only_dry_run(capsys):
    assert runner.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry-run" and output["readOnly"] is True
    assert output["functionalContractChanged"] is False


def test_normal_execution_uses_receiver_goodput_and_immutable_raw_artifacts(tmp_path):
    result, run_dir = runner.execute(request(tmp_path), runner=FixtureKubectl(), sleep=lambda _: None)
    assert result["schemaVersion"] == "cityverse.network-throughput-benchmark-result/v1"
    assert result["status"] == "complete"
    assert result["benchmarkComplete"] is True
    assert result["incompleteReasons"] == []
    assert result["senderBps"] == 98_000_000
    assert result["offeredBps"] == 100_000_000 and result["senderPps"] == 10_000
    assert result["metricSources"] == {
        "offeredBps": "requested-rate", "senderBps": "native-iperf3-sender",
        "senderPps": "native-iperf3-sender",
        "receiverGoodputBps": "native-iperf3-receiver",
        "receiverPps": "native-iperf3-receiver"}
    assert result["receiverGoodputBps"] == 90_000_000
    assert result["pps"] == pytest.approx(9990)
    assert result["receiverPps"] == pytest.approx(9990)
    assert result["reorderedPackets"] == 2
    assert result["latency"] == {"status": "available", "source": "icmp-rtt-over-ue-tun",
                                 "sampleCount": 10, "p99Ms": 10.0,
                                 "overlapsLoadWindow": True, "reason": None}
    assert result["cleanupVerified"] is True
    copied_manifest = json.loads((run_dir / "manifest.raw.json").read_text())
    assert result["manifestHash"] == copied_manifest["manifestHash"]
    assert result["identity"]["vmEpoch"] == "vm-epoch-1"
    assert result["startedAt"] < result["finishedAt"]
    assert len(result["rawArtifactRefs"]) == 9
    assert all("#sha256:" in ref for ref in result["rawArtifactRefs"])
    assert result["cleanupArtifactRef"] in result["rawArtifactRefs"]
    assert {p.name for p in run_dir.iterdir()} == {
        "plan.json", "manifest.raw.json", "identity.raw.json", "sender.raw.json",
        "sender.command.json", "receiver.raw.json", "receiver.command.json",
        "latency.raw.txt.json", "latency.command.json", "cleanup.raw.json", "result.json"}
    with pytest.raises(FileExistsError):
        runner._write_new(run_dir / "result.json", {})


def test_identity_receipt_mismatch_starts_no_cluster_command(tmp_path):
    fake = FixtureKubectl()
    with pytest.raises(runner.BenchmarkBlocked, match="receipt mismatch"):
        runner.execute(request(tmp_path, pduSessionId="foreign"), runner=fake, sleep=lambda _: None)
    assert fake.calls == []


def test_stale_native_identity_receipt_starts_no_cluster_command(tmp_path):
    fake = FixtureKubectl()
    with pytest.raises(runner.BenchmarkBlocked, match="stale"):
        runner.execute(request(tmp_path, observedAt=time.time() - 60), runner=fake,
                       sleep=lambda _: None)
    assert fake.calls == []


def test_tampered_manifest_starts_no_cluster_command(tmp_path):
    req = request(tmp_path)
    raw = json.loads(req.manifest_file.read_text(encoding="utf-8"))
    raw["manifestHash"] = "0" * 64
    req.manifest_file.write_text(json.dumps(raw), encoding="utf-8")
    fake = FixtureKubectl()
    with pytest.raises(runner.BenchmarkBlocked, match="manifest hash mismatch"):
        runner.execute(req, runner=fake, sleep=lambda _: None)
    assert fake.calls == []


def test_live_identity_mismatch_fails_before_iperf(tmp_path):
    fake = FixtureKubectl(identity_mismatch=True)
    with pytest.raises(runner.BenchmarkBlocked, match="Pod UID changed"):
        runner.execute(request(tmp_path), runner=fake, sleep=lambda _: None)
    assert not any("iperf3" in call for call in fake.calls)
    assert (only_run_dir(tmp_path) / "failure.json").exists()


def test_real_n6_network_status_mismatch_fails_before_iperf(tmp_path):
    fake = FixtureKubectl(receiver_network_mismatch=True)
    with pytest.raises(runner.BenchmarkBlocked, match="network-status mismatch"):
        runner.execute(request(tmp_path), runner=fake, sleep=lambda _: None)
    assert not any("iperf3" in call for call in fake.calls)


def test_multiple_flows_are_rejected_before_cluster_io(tmp_path):
    req = replace(request(tmp_path), flow_count=2)
    fake = FixtureKubectl()
    with pytest.raises(runner.BenchmarkBlocked, match="exactly one flow"):
        runner.execute(req, runner=fake, sleep=lambda _: None)
    assert fake.calls == []


def test_sender_only_result_is_rejected_and_raw_evidence_is_retained(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="receiver summary unavailable"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(sender_only=True), sleep=lambda _: None)
    run_dir = only_run_dir(tmp_path)
    assert (run_dir / "sender.raw.json").exists()
    assert (run_dir / "receiver.raw.json").exists()
    assert (run_dir / "failure.json").exists()


def test_missing_reorder_counter_is_not_fabricated_as_zero(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="out_of_order"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(missing_reorder=True),
                       sleep=lambda _: None)
    assert (only_run_dir(tmp_path) / "receiver.raw.json").exists()


def test_partial_result_retains_sender_raw_and_failure(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="receiver command failed"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(partial=True), sleep=lambda _: None)
    run_dir = only_run_dir(tmp_path)
    assert (run_dir / "sender.raw.json").exists()
    assert not (run_dir / "receiver.raw.json").exists()
    assert (run_dir / "failure.json").exists()


def test_cleanup_probe_failure_retains_raw_receipts_and_has_no_result(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="residual probes incomplete"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(cleanup_failure=True),
                       sleep=lambda _: None)
    run_dir = only_run_dir(tmp_path)
    cleanup = json.loads((run_dir / "cleanup.raw.json").read_text())
    assert cleanup["complete"] is False and cleanup["residualResources"] is None
    assert len(cleanup["observations"]) == 2
    assert all({"command", "exitCode", "stderr", "timestamp", "epoch"} <= set(row)
               for row in cleanup["observations"])
    assert not (run_dir / "result.json").exists()


def test_residual_is_failure_with_explicit_result(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="residual detected"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(residual=True), sleep=lambda _: None)
    result = json.loads((only_run_dir(tmp_path) / "result.json").read_text())
    assert result["cleanupVerified"] is False
    assert result["residualResources"] == ["cv-ue-g1-0", "n6-embb-fixture"]


def test_timeout_is_bounded_and_failure_artifact_survives(tmp_path):
    with pytest.raises(runner.BenchmarkBlocked, match="timed out"):
        runner.execute(request(tmp_path), runner=FixtureKubectl(timeout=True), sleep=lambda _: None)
    failure = json.loads((only_run_dir(tmp_path) / "failure.json").read_text())
    assert failure["status"] == "failed" and failure["rawEvidenceRetained"] is True


def test_missing_latency_samples_marks_throughput_incomplete(tmp_path):
    result, run_dir = runner.execute(request(tmp_path), runner=FixtureKubectl(latency_empty=True),
                                     sleep=lambda _: None)
    assert result["status"] == "incomplete" and result["benchmarkComplete"] is False
    assert result["latency"] == {"status": "unavailable", "source": "icmp-rtt-over-ue-tun",
                                 "sampleCount": 0, "p99Ms": None,
                                 "overlapsLoadWindow": False, "reason": "insufficient_samples"}
    assert (run_dir / "latency.raw.txt.json").exists()


def test_unproven_latency_overlap_marks_throughput_incomplete(tmp_path):
    result, _ = runner.execute(request(tmp_path), runner=FixtureKubectl(latency_no_overlap=True),
                               sleep=lambda _: time.sleep(0.02))
    assert result["status"] == "incomplete"
    assert result["latency"]["sampleCount"] == 10
    assert result["latency"]["p99Ms"] is None
    assert result["latency"]["reason"] == "overlap_unproven"


def test_missing_ping_capability_keeps_throughput_but_marks_incomplete(tmp_path):
    result, run_dir = runner.execute(request(tmp_path), runner=FixtureKubectl(latency_failure=True),
                                     sleep=lambda _: None)
    assert result["status"] == "incomplete"
    assert result["latency"]["reason"] == "collector_failed"
    assert "latency.error.json" in result["rawArtifacts"]
    assert (run_dir / "latency.error.json").exists()
