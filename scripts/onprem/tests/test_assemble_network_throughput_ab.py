from copy import deepcopy
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from backend.onprem.evidence.throughput import build_manifest
from test.subsystems.onprem.test_onprem_network_telemetry import complete_run
from backend.onprem.operations.validation.functional_traffic_acceptance import assemble_functional_acceptance
from test.subsystems.onprem.test_onprem_functional_traffic_acceptance import fixture as functional_fixture
from scripts.onprem.tests.test_run_network_throughput_benchmark import (
    FixtureKubectl, request as runner_request)
from scripts.onprem import run_network_throughput_benchmark as live_runner


SCRIPT = Path(__file__).resolve().parents[1] / "assemble_network_throughput_ab.py"
SPEC = importlib.util.spec_from_file_location("throughput_ab_assembler", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def manifest(queues):
    value = build_manifest(
        image="free5gc-upf@sha256:" + "a" * 64, kernel="6.8.0-test", qemu="8.2.2",
        vcpus=2, memory_mib=2048, cni="flannel+multus:fixture", mtu=1500,
        offload={"gro": False, "gso": False, "tso": False}, workload="fixture-workload-v1",
        flow_count=1, packet_bytes=1200, offered_rate_bps=100_000_000,
        duration_seconds=30, warmup_seconds=5, window_seconds=20, queue_count=queues)
    inputs = asdict(value)
    inputs = {"image": value.image, "kernel": value.kernel, "qemu": value.qemu,
              "vcpus": value.vcpus, "memoryMiB": value.memory_mib, "cni": value.cni,
              "mtu": value.mtu, "offload": dict(value.offload), "workload": value.workload,
              "flowCount": value.flow_count, "packetBytes": value.packet_bytes,
              "offeredRateBps": value.offered_rate_bps, "durationSeconds": value.duration_seconds,
              "warmupSeconds": value.warmup_seconds, "windowSeconds": value.window_seconds,
              "queueCount": value.queue_count, "profile": value.profile}
    return value, {"schemaVersion": module.MANIFEST_SCHEMA, "inputs": inputs,
                   "manifestHash": value.manifest_hash,
                   "frozenInputsHash": value.frozen_inputs_hash}


def telemetry(run_id, queues, received_bps, p99=2.5):
    value = complete_run()
    value["runId"] = run_id
    value["configuration"]["requestedQueues"] = queues
    value["identity"]["pduSessionId"] = "10"
    for phase in ("before", "after"):
        value[phase]["identity"]["pduSessionId"] = "10"
        for row in value[phase]["observations"]:
            if row["kind"] == "queue_inventory":
                row["stdout"] = "".join(f"rx-{i}\ntx-{i}\n" for i in range(queues))
            elif row["kind"] == "queue_capability":
                row["stdout"] = ("Pre-set maximums:\nCombined: 2\n"
                                 f"Current hardware settings:\nCombined: {queues}\n")
            elif row["kind"] == "qemu":
                parsed = json.loads(row["stdout"])
                parsed["frontendQueues"] = parsed["backendQueues"] = queues
                row["stdout"] = json.dumps(parsed)
    value["receiverResult"].update(
        receivedBps=received_bps, receivedPps=9000, lossPercent=.1,
        reorderedPackets=2, latencyLabel="icmp-rtt-over-ue-tun", p99LatencyMs=p99)
    value["receiverResult"]["receiverId"] = "receiver-uid"
    return value


def identity():
    return {"workloadId": "fixture-workload-v1", "supi": "imsi-208930000000001",
            "pduSessionId": "10", "ueIp": "10.101.0.2", "tun": "uesimtun0",
            "receiverId": "receiver-uid", "vmEpoch": "vm-epoch-1",
            "interfaceEpoch": "if-epoch-1"}


def sample(case_manifest, run_id, queues, received_bps):
    tele = telemetry(run_id, queues, received_bps)
    benchmark = {
        "profile": "network-throughput", "status": "complete", "benchmarkComplete": True,
        "runId": run_id, "manifestHash": case_manifest.manifest_hash,
        "frozenInputsHash": case_manifest.frozen_inputs_hash, "identity": identity(),
        "startedAt": 102, "finishedAt": 118, "senderBps": 98_000_000,
        "offeredBps": 100_000_000, "senderPps": 10_000,
        "receiverGoodputBps": received_bps, "receiverPps": 9000, "pps": 9000,
        "deliveredPackets": 90_000, "lossPercent": .1, "reorderedPackets": 2,
        "latency": {"status": "available", "source": "icmp-rtt-over-ue-tun",
                    "overlapsLoadWindow": True, "sampleCount": 100, "p99Ms": 2.5},
        "cleanupVerified": True, "residualResources": [],
        "metricSources": {"offeredBps": "requested-rate",
                          "senderBps": "native-iperf3-sender",
                          "senderPps": "native-iperf3-sender",
                          "receiverGoodputBps": "native-iperf3-receiver",
                          "receiverPps": "native-iperf3-receiver"},
        "rawArtifactRefs": [f"{run_id}/sender.raw.json#sha256:" + "a" * 64,
                            f"{run_id}/receiver.raw.json#sha256:" + "b" * 64,
                            f"{run_id}/latency.raw.txt.json#sha256:" + "c" * 64,
                            f"{run_id}/cleanup.raw.json#sha256:" + "d" * 64],
    }
    functional = {
        "schemaVersion": module.FUNCTIONAL_SCHEMA, "benchmarkRunId": run_id,
        "profile": "functional-traffic", "accepted": True, "pduHealthy": True,
        "sessionHealthy": True, "cleanupVerified": True, "residualResources": [],
        "identity": identity(), "finishedAt": 130,
        "settings": {"rateMbps": 10, "packetBytes": 1200, "flowCount": 1, "gso": False},
    }
    benchmark["cleanupArtifactRef"] = benchmark["rawArtifactRefs"][-1]
    return {"benchmark": benchmark, "telemetry": tele, "functional": functional}


def ab_fixture():
    base, base_raw = manifest(1)
    candidate, candidate_raw = manifest(2)
    return {"baseline": {"manifest": base_raw,
                         "runs": [sample(base, f"base-{i}", 1, 8_000_000) for i in range(3)]},
            "candidate": {"manifest": candidate_raw,
                          "runs": [sample(candidate, f"candidate-{i}", 2, 9_200_000)
                                   for i in range(3)]}}


def test_complete_3_plus_3_artifacts_produce_comparator_input_and_verdict():
    result = module.assemble_ab(ab_fixture())
    assert result["status"] == "improvement_proven"
    assert result["improvementProven"] is True
    assert result["comparison"]["baseline_samples"] == 3
    assert result["comparison"]["candidate_samples"] == 3
    assert result["comparison"]["baseline_medians"]["sender_bps"] == 98_000_000
    assert result["comparison"]["baseline_medians"]["kernel_drops"] > 0
    assert result["comparison"]["baseline_medians"]["cpu_per_delivered_packet"] == pytest.approx(.32 / 90_000)
    assert "whole-UPF-guest" in result["metricDefinitions"]["cpuSeconds"]
    assert "not unique dropped packets" in result["metricDefinitions"]["kernelDrops"]


@pytest.mark.parametrize("fault,expected", [
    ("missing_cpu", "telemetry evidence incomplete"),
    ("identity", "functional identity mismatch"),
    ("receiver", "receiver result cross-check mismatch"),
    ("functional", "functional acceptance incomplete"),
    ("manifest", "immutable manifest hash mismatch"),
])
def test_missing_or_mismatched_evidence_stays_incomplete(fault, expected):
    value = ab_fixture()
    target = value["candidate"]["runs"][1]
    if fault == "missing_cpu":
        for phase in ("before", "after"):
            target["telemetry"][phase]["observations"] = [
                row for row in target["telemetry"][phase]["observations"]
                if row["kind"] != "cgroup_cpu"]
    elif fault == "identity":
        target["functional"]["identity"]["supi"] = "imsi-208930000000999"
    elif fault == "receiver":
        target["telemetry"]["receiverResult"]["receivedBps"] += 1
    elif fault == "functional":
        target["functional"]["accepted"] = False
    else:
        value["candidate"]["manifest"]["manifestHash"] = "0" * 64
    result = module.assemble_ab(value)
    assert result["status"] == "incomplete"
    assert result["improvementProven"] is False
    assert any(expected in error for error in result["errors"])


def test_fewer_than_three_runs_never_reaches_comparator():
    value = ab_fixture()
    value["baseline"]["runs"].pop()
    result = module.assemble_ab(value)
    assert result["status"] == "incomplete"
    assert any("at least three runs" in error for error in result["errors"])


def test_cli_reads_artifacts_and_only_emits_stdout(tmp_path, capsys):
    value = ab_fixture()
    spec = {}
    for case in ("baseline", "candidate"):
        manifest_path = tmp_path / f"{case}-manifest.json"
        manifest_path.write_text(json.dumps(value[case]["manifest"]), encoding="utf-8")
        rows = []
        for index, sample_value in enumerate(value[case]["runs"]):
            row = {}
            for name, artifact in sample_value.items():
                path = tmp_path / f"{case}-{index}-{name}.json"
                path.write_text(json.dumps(artifact), encoding="utf-8")
                row[name] = path.name
            rows.append(row)
        spec[case] = {"manifest": manifest_path.name, "runs": rows}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    before = {path.name for path in tmp_path.iterdir()}
    assert module.main([str(spec_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "improvement_proven"
    assert {path.name for path in tmp_path.iterdir()} == before


def test_actual_runner_result_satisfies_assembler_benchmark_contract(tmp_path):
    result, run_dir = live_runner.execute(
        runner_request(tmp_path), runner=FixtureKubectl(), sleep=lambda _: None)
    case_manifest = module.load_manifest(json.loads(
        (run_dir / "manifest.raw.json").read_text(encoding="utf-8")))
    contract = module.validate_benchmark_artifact(case_manifest, result)
    assert contract["runId"] == result["runId"]
    assert contract["identity"]["interfaceEpoch"] == "if-epoch-1"
    assert len(contract["rawArtifactRefs"]) == 9


def test_functional_generator_output_is_consumed_directly_by_assemble_run():
    functional_raw = functional_fixture(now=130)
    functional = assemble_functional_acceptance(functional_raw, now=130)
    identity_value = functional["identity"]
    case_manifest = build_manifest(
        image="free5gc-upf@sha256:" + "a" * 64, kernel="6.8.0-test", qemu="8.2.2",
        vcpus=2, memory_mib=2048, cni="flannel+multus:fixture", mtu=1500,
        offload={"gro": False, "gso": False, "tso": False},
        workload=identity_value["workloadId"], flow_count=1, packet_bytes=1200,
        offered_rate_bps=100_000_000, duration_seconds=30, warmup_seconds=5,
        window_seconds=20, queue_count=2)
    bundled = sample(case_manifest, functional["benchmarkRunId"], 2, 8_000_000)
    bundled["benchmark"]["identity"] = deepcopy(identity_value)
    tele = bundled["telemetry"]
    for target in (tele["identity"], tele["before"]["identity"], tele["after"]["identity"]):
        target["pduSessionId"] = identity_value["pduSessionId"]
        target["vmEpoch"] = identity_value["vmEpoch"]
        target["interfaceEpoch"] = identity_value["interfaceEpoch"]
    tele["receiverResult"]["receiverId"] = identity_value["receiverId"]
    run = module.assemble_run(case_manifest, bundled["benchmark"], tele, functional)
    assert run.functional_acceptance_passed is True
    assert run.identity.workload_id == "fixed-functional-10m"
