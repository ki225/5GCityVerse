import importlib.util
import json
from pathlib import Path
import sys

from backend.onprem.operations.diagnostics.network_telemetry_bundle import capture_bundle as real_capture_bundle
from test.subsystems.onprem.test_onprem_network_telemetry_bundle import config as telemetry_config, executors as telemetry_executors, receiver_result as telemetry_receiver_result
from test.subsystems.onprem.test_onprem_network_telemetry_cli import Clock


SCRIPT = Path(__file__).resolve().parents[1] / "run_network_throughput_operator.py"
SPEC = importlib.util.spec_from_file_location("throughput_operator", SCRIPT)
operator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = operator
SPEC.loader.exec_module(operator)


def plan(_):
    return {"schemaVersion": operator.SCHEMA, "mode": "observe", "readOnly": True,
            "commandsExecuted": 0, "sequence": ["readiness", "before", "traffic", "after",
                                                       "functional", "assemble"]}


IDENTITY = {"workloadId": "fixed-functional-10m", "supi": "imsi-001",
            "pduSessionId": 10, "ueIp": "10.0.0.1", "tun": "uesimtun0",
            "receiverId": "receiver-uid", "vmEpoch": "upf-vm-epoch-1",
            "interfaceEpoch": "upf-if-epoch-1"}


def preflight(_config, _run_id):
    return "epoch-1", dict(IDENTITY)


def throughput_result(run_id):
    return {"profile": "network-throughput", "status": "complete",
            "benchmarkComplete": True, "runId": run_id, "identity": dict(IDENTITY),
            "source": "native-iperf3-receiver", "receiverGoodputBps": 8_000_000}


def raw_traffic(run_id):
    return {"status": "complete", "runId": run_id, "epoch": "epoch-1",
            "identity": dict(IDENTITY),
            "reports": [{"direction": "ul"}, {"direction": "dl"}]}


def lifecycle_receipts(run_id, traffic_result):
    return {"schemaVersion": "cityverse.network-lifecycle-receipts/v1",
            "runId": run_id, "epoch": traffic_result["epoch"],
            "identity": traffic_result["identity"],
            "status": "complete", "complete": True, "residuals": [],
            "pduSessionEvidence": {"source": "cityverse-native-session-readback/v1"},
            "cleanupEvidence": {"source": "cityverse-owned-resource-cleanup/v1"}}


def telemetry(phase, run_id, before, receiver):
    if phase == "before":
        identity = {"sessionEpoch": "epoch-1", "pduSessionId": 10}
        return {"status": "complete", "phase": phase, "runId": run_id,
                "sides": {role: {"snapshot": {"identity": dict(identity)}}
                          for role in ("ran", "upf")}}
    assert before is not None and receiver == throughput_result(run_id)
    return {"status": "complete", "phase": phase, "runId": run_id,
            "receiverResult": receiver}


def live_config(tmp_path):
    return {"telemetry": telemetry_config(), "runner": {
        "context": "dedicated-kubeadm", "kubeconfig": "lab.kubeconfig",
        "namespace": "free5gc-onprem", "owner": "onprem-network-throughput-benchmark",
        "uePod": "cv-ue-g1-0", "ueUid": "ue-uid", "receiverPod": "n6-embb",
        "receiverUid": "receiver-uid", "supi": "imsi-001", "pduSessionId": "10",
        "ueIp": "10.0.0.1", "tun": "uesimtun0", "endpoint": "10.0.0.2",
        "port": 15201, "rateMbps": 100, "durationSeconds": 20, "flowCount": 4,
        "packetBytes": 1200, "manifest": str(tmp_path / "manifest.json"),
        "identityEvidence": str(tmp_path / "identity.json"),
        "artifactRoot": str(tmp_path / "benchmark")}}


def test_dry_run_executes_zero_adapters_and_writes_nothing(tmp_path):
    calls = []
    result = operator.orchestrate({}, planner=plan, ownership_preflight=preflight,
                                  capture=lambda *args: calls.append(args),
                                  traffic=lambda *args: calls.append(args),
                                  functional_traffic=lambda *args: calls.append(args),
                                  acceptance=lambda *args: calls.append(args))
    assert result["readOnly"] is True and result["commandsExecuted"] == 0
    assert calls == [] and list(tmp_path.iterdir()) == []


def test_cli_default_is_observe_only_and_creates_no_artifact(tmp_path, capsys):
    config = {"telemetry": telemetry_config(), "runner": {
        "manifest": "manifest.json", "identityEvidence": "identity.json",
        "artifactRoot": "artifacts"}}
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert operator.main([str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "observe" and result["commandsExecuted"] == 0
    capability = result["liveCapability"]
    assert capability["status"] == "available-with-live-prerequisites"
    assert capability["throughputRunner"]["profile"] == "network-throughput"
    assert capability["functionalTrafficReports"] == {
        "status": "available",
        "producer": "backend.onprem.operations.validation.functional_traffic_adapter.run_functional_traffic",
        "profile": {"directions": ["ul", "dl"], "rateMbps": 10,
                    "packetBytes": 1200, "flowCount": 1, "gso": False},
        "requirements": ["fresh-native-pdu-tun-guard", "exact-ue-pod-uid",
                         "exact-n6-pod-uid", "pinned-traffic-sidecars"]}
    assert capability["missingRequiredAdapters"] == []
    assert capability["livePrerequisites"][0] == "existing-owned-resource-registry"
    assert {item.name for item in tmp_path.iterdir()} == {"operator.json"}


def test_missing_live_adapter_fails_readiness_before_any_command(tmp_path):
    calls = []
    result = operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                                  ownership_preflight=preflight,
                                  capture=lambda *args: calls.append(args),
                                  run_id_factory=lambda: "operator-run-1")
    assert result["status"] == "incomplete" and result["stage"] == "readiness"
    assert result["commandsExecuted"] == 0 and calls == []
    run_dir = tmp_path / "operator-run-1"
    assert {path.name for path in run_dir.iterdir()} == {"00-readiness.json", "failure.json"}


def test_ordered_complete_fixture_keeps_one_run_id_and_immutable_steps(tmp_path):
    calls = []
    def capture(phase, run_id, before, receiver):
        calls.append(phase)
        assert run_id == "operator-run-2"
        assert (before is None) is (phase == "before")
        return telemetry(phase, run_id, before, receiver)
    def traffic(run_id):
        calls.append("traffic")
        return throughput_result(run_id)
    def lifecycle(run_id, traffic_result):
        calls.append("lifecycle")
        return lifecycle_receipts(run_id, traffic_result)
    def acceptance(run_id, traffic_result, receipt_result):
        calls.append("acceptance")
        assert traffic_result["runId"] == run_id
        assert receipt_result["runId"] == run_id
        return {"status": "complete", "benchmarkRunId": run_id,
                "cleanupVerified": True}
    result = operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                                  ownership_preflight=preflight,
                                  capture=capture, traffic=traffic, lifecycle=lifecycle,
                                  functional_traffic=lambda run_id: (calls.append("functional") or raw_traffic(run_id)),
                                  acceptance=acceptance,
                                  run_id_factory=lambda: "operator-run-2")
    assert result["status"] == "ready-for-assembly"
    assert calls == ["before", "traffic", "after", "functional", "lifecycle", "acceptance"]
    names = {path.name for path in (tmp_path / "operator-run-2").iterdir()}
    assert names == {"00-readiness.json", "10-telemetry-before.json", "20-throughput.json",
                     "30-telemetry-after.json", "40-functional-traffic.json",
                     "50-lifecycle-receipts.json", "60-functional-acceptance.json",
                     "result.json"}


def test_incomplete_before_prevents_traffic_and_retains_failure(tmp_path):
    calls = []
    def capture(phase, run_id, before, receiver):
        calls.append(phase)
        return {"status": "incomplete", "phase": phase}
    result = operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                                  ownership_preflight=preflight,
                                  capture=capture, traffic=lambda _: calls.append("traffic"),
                                  lifecycle=lambda *_: calls.append("lifecycle"),
                                  functional_traffic=lambda *_: calls.append("functional"),
                                  acceptance=lambda *_: calls.append("acceptance"),
                                  run_id_factory=lambda: "operator-run-3")
    assert result["status"] == "incomplete"
    assert calls == ["before"]
    assert (tmp_path / "operator-run-3" / "10-telemetry-before.json").exists()
    assert (tmp_path / "operator-run-3" / "failure.json").exists()


def test_runner_failure_prevents_after_and_functional(tmp_path):
    calls = []
    def capture(phase, run_id, before, receiver):
        calls.append(phase)
        return telemetry(phase, run_id, before, receiver)
    def traffic(_):
        calls.append("traffic")
        raise RuntimeError("fixture runner failed")
    result = operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                                  ownership_preflight=preflight,
                                  capture=capture, traffic=traffic,
                                  lifecycle=lambda *_: calls.append("lifecycle"),
                                  functional_traffic=lambda *_: calls.append("functional"),
                                  acceptance=lambda *_: calls.append("acceptance"),
                                  run_id_factory=lambda: "operator-run-4")
    assert result["status"] == "incomplete" and result["artifactsRetained"] is True
    assert calls == ["before", "traffic"]


def test_preflight_requires_exact_existing_registry_before_live_adapters(tmp_path):
    run_id = "operator-run-preflight"
    registry = tmp_path / "owned.json"
    registry.write_text(json.dumps({"runs": {run_id: {
        "epoch": "epoch-1", "identity": IDENTITY, "nativePfcpSeid": "77",
    }}}), encoding="utf-8")
    config = {"producerEnvironment": {
        "CITYVERSE_OWNED_RESOURCE_REGISTRY": str(registry)}}
    calls = []
    result = operator.orchestrate(
        config, execute=True, output_root=tmp_path, planner=plan,
        capture=lambda *args: calls.append("capture"), traffic=throughput_result,
        functional_traffic=raw_traffic, lifecycle=lifecycle_receipts,
        acceptance=lambda *_: {}, run_id_factory=lambda: run_id)
    assert calls == ["capture"]
    assert result["stage"] == "telemetry-before"

    for fault in ("missing-run", "wrong-epoch", "extra-identity", "missing-pfcp"):
        value = {"runs": {run_id: {"epoch": "epoch-1", "identity": dict(IDENTITY),
                                   "nativePfcpSeid": "77"}}}
        if fault == "missing-run":
            value["runs"] = {}
        elif fault == "wrong-epoch":
            value["runs"][run_id]["epoch"] = ""
        elif fault == "extra-identity":
            value["runs"][run_id]["identity"]["foreign"] = "value"
        else:
            value["runs"][run_id].pop("nativePfcpSeid")
        registry.write_text(json.dumps(value), encoding="utf-8")
        fault_root = tmp_path / fault
        calls.clear()
        blocked = operator.orchestrate(
            config, execute=True, output_root=fault_root, planner=plan,
            capture=lambda *args: calls.append("capture"), traffic=throughput_result,
            functional_traffic=raw_traffic, lifecycle=lifecycle_receipts,
            acceptance=lambda *_: {}, run_id_factory=lambda: run_id)
        assert blocked["stage"] == "readiness"
        assert blocked["commandsExecuted"] == 0 and calls == []


def test_execute_requires_explicit_preallocated_run_id(tmp_path):
    try:
        operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan)
    except operator.OperatorBlocked as exc:
        assert "explicit preallocated runId" in str(exc)
    else:
        raise AssertionError("generated runId was accepted")


def test_runner_incomplete_stops_before_after_capture(tmp_path):
    calls = []
    def capture(phase, run_id, before, receiver):
        calls.append(phase)
        return telemetry(phase, run_id, before, receiver)
    def traffic(run_id):
        calls.append("traffic")
        return {**throughput_result(run_id), "status": "incomplete"}
    result = operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                                  ownership_preflight=preflight,
                                  capture=capture, traffic=traffic,
                                  lifecycle=lambda *_: calls.append("lifecycle"),
                                  functional_traffic=lambda *_: calls.append("functional"),
                                  acceptance=lambda *_: calls.append("acceptance"),
                                  run_id_factory=lambda: "operator-run-incomplete")
    assert result["status"] == "incomplete"
    assert calls == ["before", "traffic"]
    assert not (tmp_path / "operator-run-incomplete" / "30-telemetry-after.json").exists()


def test_preallocated_run_id_cannot_overwrite_existing_run(tmp_path):
    existing = tmp_path / "operator-run-5"
    existing.mkdir()
    try:
        operator.orchestrate({}, execute=True, output_root=tmp_path, planner=plan,
                             ownership_preflight=preflight,
                             capture=lambda *_: {}, traffic=lambda _: {},
                             lifecycle=lambda *_: {}, functional_traffic=lambda *_: {},
                             acceptance=lambda *_: {},
                             run_id_factory=lambda: "operator-run-5")
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing run directory was accepted")


def test_default_adapters_forward_fixed_receipt_provenance_to_acceptance(tmp_path, monkeypatch):
    calls = []
    producer_environment = {"CITYVERSE_OWNED_RESOURCE_REGISTRY": "existing.json"}

    def collect(**kwargs):
        calls.append(("collect", kwargs))
        traffic_result = raw_traffic(kwargs["run_id"])
        return lifecycle_receipts(kwargs["run_id"], traffic_result)

    def accept(raw, *, now):
        calls.append(("accept", raw, now))
        assert raw["pduSessionEvidence"]["source"] == "cityverse-native-session-readback/v1"
        assert raw["cleanupEvidence"]["source"] == "cityverse-owned-resource-cleanup/v1"
        return {"status": "complete", "benchmarkRunId": raw["runId"],
                "cleanupVerified": True}

    monkeypatch.setattr(operator, "collect_network_lifecycle_receipts", collect)
    monkeypatch.setattr(operator, "assemble_functional_acceptance", accept)
    result = operator.orchestrate(
        {"producerEnvironment": producer_environment}, execute=True,
        output_root=tmp_path, planner=plan, ownership_preflight=preflight,
        capture=telemetry, traffic=throughput_result, functional_traffic=raw_traffic,
        run_id_factory=lambda: "operator-run-default-adapters")
    assert result["status"] == "ready-for-assembly"
    collect_call = calls[0][1]
    assert collect_call == {
        "run_id": "operator-run-default-adapters", "epoch": "epoch-1",
        "identity": raw_traffic("ignored")["identity"],
        "producer_environment": producer_environment}
    assert calls[1][0] == "accept"


def test_default_lifecycle_adapter_fails_closed_without_registry_config(tmp_path):
    calls = []
    result = operator.orchestrate(
        {}, execute=True, output_root=tmp_path, planner=plan,
        capture=lambda *args: calls.append("capture"), traffic=throughput_result,
        functional_traffic=raw_traffic,
        run_id_factory=lambda: "operator-run-no-registry")
    assert result["status"] == "incomplete" and result["stage"] == "readiness"
    assert result["commandsExecuted"] == 0
    assert "ownership registry" in result["reason"]
    assert calls == []


def test_lane_identity_mismatch_blocks_cleanup_and_acceptance(tmp_path):
    calls = []

    def wrong_functional(run_id):
        result = raw_traffic(run_id)
        result["identity"] = {**result["identity"], "ueIp": "10.0.0.99"}
        return result

    result = operator.orchestrate(
        {}, execute=True, output_root=tmp_path, planner=plan,
        ownership_preflight=preflight, capture=telemetry,
        traffic=throughput_result, functional_traffic=wrong_functional,
        lifecycle=lambda *_: calls.append("cleanup"),
        acceptance=lambda *_: calls.append("acceptance"),
        run_id_factory=lambda: "operator-run-foreign-functional")
    assert result["status"] == "incomplete"
    assert result["stage"] == "functional-traffic-runner"
    assert "functional traffic result incomplete" in result["error"]
    assert calls == []


def test_functional_session_epoch_mismatch_blocks_cleanup(tmp_path):
    def wrong_epoch(run_id):
        return {**raw_traffic(run_id), "epoch": "foreign-epoch"}

    result = operator.orchestrate(
        {}, execute=True, output_root=tmp_path, planner=plan,
        ownership_preflight=preflight, capture=telemetry,
        traffic=throughput_result, functional_traffic=wrong_epoch,
        lifecycle=lambda *_: {}, acceptance=lambda *_: {},
        run_id_factory=lambda: "operator-run-foreign-epoch")
    assert result["status"] == "incomplete"
    assert "functional traffic result incomplete" in result["error"]


def test_throughput_result_never_needs_functional_reports(tmp_path):
    result = operator.orchestrate(
        {}, execute=True, output_root=tmp_path, planner=plan,
        ownership_preflight=preflight, capture=telemetry,
        traffic=throughput_result, functional_traffic=raw_traffic,
        lifecycle=lifecycle_receipts,
        acceptance=lambda run_id, functional, lifecycle: {
            "status": "complete", "benchmarkRunId": run_id, "cleanupVerified": True},
        run_id_factory=lambda: "operator-run-separated-lanes")
    assert result["status"] == "ready-for-assembly"
    throughput = json.loads((tmp_path / "operator-run-separated-lanes" /
                             "20-throughput.json").read_text(encoding="utf-8"))
    assert "reports" not in throughput


def test_default_telemetry_and_throughput_adapters_call_existing_apis(tmp_path, monkeypatch):
    calls = []

    def capture_bundle(**kwargs):
        calls.append(("capture", kwargs))
        return telemetry(kwargs["phase"], "operator-run-live-defaults",
                         kwargs["before"], kwargs["receiver_result"])

    def execute(request, *, run_id):
        calls.append(("execute", request, run_id))
        return throughput_result(run_id), tmp_path / "benchmark" / run_id

    monkeypatch.setattr(operator, "capture_bundle", capture_bundle)
    monkeypatch.setattr(operator.benchmark_runner, "execute", execute)
    result = operator.orchestrate(
        live_config(tmp_path), execute=True, output_root=tmp_path,
        ownership_preflight=preflight,
        functional_traffic=raw_traffic, lifecycle=lifecycle_receipts,
        acceptance=lambda run_id, functional, lifecycle: {
            "status": "complete", "benchmarkRunId": run_id, "cleanupVerified": True},
        run_id_factory=lambda: "operator-run-live-defaults")
    assert result["status"] == "ready-for-assembly"
    assert [row[0] for row in calls] == ["capture", "execute", "capture"]
    request = calls[1][1]
    assert request.context == "dedicated-kubeadm"
    assert request.manifest_file == tmp_path / "manifest.json"
    assert calls[0][1]["run_id"] == "operator-run-live-defaults"
    assert calls[2][1]["run_id"] == "operator-run-live-defaults"
    assert calls[2][1]["receiver_result"] == throughput_result("operator-run-live-defaults")


def test_real_telemetry_bundle_accepts_receiver_only_with_exact_run_id():
    before = real_capture_bundle(
        phase="before", config=telemetry_config(),
        executors=telemetry_executors(), clock=Clock())
    receiver = telemetry_receiver_result()
    rejected = real_capture_bundle(
        phase="after", config=telemetry_config(), before=before,
        receiver_result=receiver, executors=telemetry_executors(after=True),
        clock=Clock(120))
    accepted = real_capture_bundle(
        phase="after", config=telemetry_config(), before=before,
        receiver_result=receiver, run_id="run-1",
        executors=telemetry_executors(after=True), clock=Clock(120))
    assert rejected["status"] == "incomplete"
    assert "receiver run ID mismatch" in rejected["reasons"]
    assert accepted["status"] == "complete"
    assert accepted["receiverResult"]["runId"] == "run-1"


def test_default_throughput_adapter_rejects_incomplete_config_before_capture(tmp_path):
    calls = []
    config = live_config(tmp_path)
    config["runner"].pop("ueUid")
    result = operator.orchestrate(
        config, execute=True, output_root=tmp_path,
        ownership_preflight=preflight,
        functional_traffic=raw_traffic, lifecycle=lifecycle_receipts,
        capture=None, run_id_factory=lambda: "operator-run-bad-live-config")
    assert result["status"] == "incomplete" and result["stage"] == "readiness"
    assert result["commandsExecuted"] == 0
    assert "ueUid" in result["reason"] and calls == []


def test_default_adapters_still_fail_readiness_without_safe_functional_guard(tmp_path):
    result = operator.orchestrate(
        live_config(tmp_path), execute=True, output_root=tmp_path,
        ownership_preflight=preflight,
        run_id_factory=lambda: "operator-run-no-functional-guard")
    assert result["status"] == "incomplete" and result["stage"] == "readiness"
    assert result["missingAdapters"] == ["functional_traffic"]
    assert result["commandsExecuted"] == 0


def test_default_functional_adapter_receives_pinned_run_epoch_and_identity(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(operator, "validate_functional_traffic_config", lambda config: None)
    def functional(config, *, run_id, epoch, identity):
        calls.append((config, run_id, epoch, identity))
        return raw_traffic(run_id)
    monkeypatch.setattr(operator, "run_functional_traffic", functional)
    config = live_config(tmp_path)
    result = operator.orchestrate(
        config, execute=True, output_root=tmp_path, planner=plan,
        ownership_preflight=preflight,
        capture=telemetry, traffic=throughput_result, lifecycle=lifecycle_receipts,
        acceptance=lambda run_id, functional, lifecycle: {
            "status": "complete", "benchmarkRunId": run_id, "cleanupVerified": True},
        run_id_factory=lambda: "operator-run-default-functional")
    assert result["status"] == "ready-for-assembly"
    assert calls == [(config, "operator-run-default-functional", "epoch-1",
                      throughput_result("ignored")["identity"])]
