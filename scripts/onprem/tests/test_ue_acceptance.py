"""Offline adversarial tests for the live acceptance gate's public contract."""
from copy import deepcopy
import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("ue_acceptance", Path(__file__).parents[1] / "run_ue_acceptance.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
NOW = 1_800_000_000.0


def stamp(offset=0):
    return datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()


def complete_status(total=4):
    desired = gate.distribution(total)
    status = {"runtime": "kubeadm", "generation": 7, "status": "established",
              **dict.fromkeys(gate.COUNTERS, total), "failed": 0,
              "observedAt": stamp(), "stableSince": stamp(-65),
              "evidenceSources": ["ue", "amf", "smf", "upf", "traffic"],
              "errors": [], "slices": {}, "perUe": []}
    for index, (name, count) in enumerate(desired.items()):
        status["slices"][name] = {**dict.fromkeys(gate.COUNTERS, count), "failed": 0, "status": "established"}
        for number in range(count):
            ip = str(gate.POOLS[name].network_address + number + 1)
            status["perUe"].append({"supi": f"imsi-{208930100000001 + index * 250 + number}",
                                  "slice": name, "generation": 7, "registered": True, "pduEstablished": True,
                                  "trafficVerified": True, "ip": ip, "sessionId": "1",
                                  "podUid": f"pod-{name}", "interface": f"uesimtun{number}",
                                  "upf": f"upf-{name}", "endpoint": gate.ENDPOINTS[name],
                                  "observedAt": stamp()})
    return status


def validate(status, total=4):
    return gate.evidence_errors(status, gate.distribution(total), 7, now=NOW)


def test_fresh_repeated_observation_keeps_continuity_but_regression_breaks_it():
    status = complete_status(1)
    assert gate.evidence_errors(status, gate.distribution(1), 7, now=NOW + 5) == []
    assert gate.observation_sequence_errors(NOW, NOW) == []
    assert gate.observation_sequence_errors(NOW - 1, NOW)
    assert gate.observation_sequence_errors(NOW + 16, NOW)


def test_transient_unavailable_resets_gate_without_aborting_generation():
    transient = complete_status(1)
    transient.update(status="verification_unavailable", errors=["UPF evidence stale"])
    assert gate.evidence_errors(transient, gate.distribution(1), 7, now=NOW)
    assert not gate.terminal_gate_failure(transient)
    assert gate.terminal_gate_failure({"status": "failed"})
    assert validate(complete_status(1), 1) == []


def test_live_runner_waits_through_one_unavailable_poll_and_requires_stable_window(monkeypatch, tmp_path):
    clock = [0.0]
    baseline = complete_status(0)
    baseline.update(status="stopped", evidenceSources=["ue", "amf", "smf", "upf"])
    polls = ["requested", "verification_unavailable"]
    initial = [True]

    def request(_url, body=None):
        if body is not None:
            assert body == gate.distribution(1)
            return {"generation": 8}
        if initial[0]:
            initial[0] = False
            return baseline
        if not polls:
            status = complete_status(1)
            status["generation"] = 8
            status["observedAt"] = stamp(clock[0])
            for row in status["perUe"]:
                row["generation"] = 8
                row["observedAt"] = stamp(clock[0])
            return status
        status = complete_status(1)
        status.update(generation=8, status=polls.pop(0), errors=["UPF evidence stale"])
        return status

    monkeypatch.setattr(gate, "request", request)
    monkeypatch.setattr(gate.time, "time", lambda: NOW + clock[0])
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gate.time, "sleep", lambda amount: clock.__setitem__(0, clock[0] + amount))
    monkeypatch.setattr(sys, "argv", ["gate", "--base-url", "http://test", "--output", str(tmp_path),
                                      "--execute", "--gates", "1", "--timeout", "120", "--poll", "5"])
    assert gate.main() == 0
    assert clock[0] >= 60


@pytest.mark.parametrize("total", [1, 10, 50, 100, 500, 1000])
def test_exact_gate_contract(total):
    assert validate(complete_status(total), total) == []


def test_select_50_without_changing_runtime_capacity():
    assert gate.parse_gates("50") == (50,)
    assert gate.distribution(50) == {"embb": 13, "urllc": 13, "mmtc": 12, "v2x": 12}
    assert gate.parse_gates("1,10,50,1000") == (1, 10, 50, 1000)


@pytest.mark.parametrize("value", ["", "0", "1001", "50,50", "50,10", "-1", "1.5", "50,", "true"])
def test_invalid_gate_selection_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        gate.parse_gates(value)


def test_accepts_actual_backend_verifier_public_contract():
    from backend.onprem.runtime import ue as backend
    desired = gate.distribution(1)
    allocation = backend.allocate(desired)
    generation = {"generation": 7, "desired": desired, "allocation": allocation, "created": NOW - 70}
    verifier = backend.EvidenceVerifier()
    for offset in range(-60, 1, 10):
        observed = stamp(offset)
        evidence = {"generation": 7, "observedAt": observed,
                    "sources": {source: {"available": True, "source": source, "observedAt": observed} for source in backend.SOURCES},
                    "endpoints": backend.ENDPOINTS,
                    "ues": [{"supi": allocation[0]["supi"], "generation": 7,
                             "amf": {"registered": True, "observedAt": observed},
                             "smf": {"sessionId": "1", "sessionCount": 1, "established": True, "sst": 1, "sd": "000001", "dnn": "internet", "upf": "upf-embb", "ip": "10.101.0.1", "observedAt": observed},
                             "ue": {"running": True, "podUid": "pod-a", "tun": "uesimtun0", "ip": "10.101.0.1", "sessionId": "1", "observedAt": observed},
                             "upf": {"upf": "upf-embb", "sessionId": "1", "ip": "10.101.0.1", "pfcpSeid": "1", "uplinkTeid": 2, "downlinkTeid": 3, "observedAt": observed},
                             "traffic": {"success": True, "interface": "uesimtun0", "podUid": "pod-a", "sourceIp": "10.101.0.1", "sessionId": "1", "endpoint": backend.ENDPOINTS["embb"], "received": 1, "transmitted": 1, "observedAt": observed}}]}
        status = verifier.evaluate(generation, evidence, now=NOW + offset)
    assert status["status"] == "established"
    assert validate(status, 1) == []


@pytest.mark.parametrize("bad", [None, [], "bad", "2026-01-01", stamp(-16), stamp(2)])
def test_rejects_stale_future_naive_and_malformed_timestamps(bad):
    status = complete_status()
    status["observedAt"] = bad
    assert validate(status)
    status = complete_status()
    status["perUe"][0]["observedAt"] = bad
    assert validate(status)


@pytest.mark.parametrize("field,bad", [("slices", None), ("slices", []), ("perUe", {}),
                                      ("perUe", [None] * 4), ("perUe", [False] * 4),
                                      ("evidenceSources", "amf"), ("evidenceSources", ["amf"]),
                                      ("evidenceSources", [None] * 5), ("failed", False),
                                      ("requested", True), ("generation", True),
                                      ("errors", ["Residual UE"]), ("stableSince", stamp(-59))])
def test_malformed_or_partial_contract_cannot_pass(field, bad):
    status = complete_status()
    status[field] = bad
    assert validate(status)


@pytest.mark.parametrize("field,bad", [("slice", []), ("podUid", []), ("interface", {}),
                                      ("sessionId", True), ("sessionId", []), ("upf", "upf-other"),
                                      ("endpoint", []), ("endpoint", "wrong-n6-target"), ("generation", 0),
                                      ("ip", []), ("ip", "10.101.0.0"),
                                      ("ip", "10.101.7.255"), ("ip", "10.102.0.1"),
                                      ("trafficVerified", 1)])
def test_malformed_per_ue_fields_fail_closed(field, bad):
    status = complete_status()
    status["perUe"][0][field] = bad
    assert validate(status)


def test_no_sampling_or_duplicate_supis_interfaces_or_addresses():
    for mutate in (
        lambda s: s["perUe"].pop(),
        lambda s: s["perUe"][1].update(supi=s["perUe"][0]["supi"]),
        lambda s: s["perUe"][1].update(podUid=s["perUe"][0]["podUid"], interface=s["perUe"][0]["interface"]),
        lambda s: s["perUe"][1].update(ip=s["perUe"][0]["ip"]),
    ):
        status = complete_status()
        mutate(status)
        assert validate(status)


def test_997_of_1000_is_partial():
    status = complete_status(1000)
    status["registered"] = 997
    assert validate(status, 1000)


@pytest.mark.parametrize("state", ["not_configured", "verification_unavailable", "failed", "requested", "starting"])
def test_unready_initial_status_prevents_any_execute_write(monkeypatch, tmp_path, state):
    status = complete_status()
    status["status"] = state
    calls = []
    def request(base_url, body=None):
        calls.append(body)
        return deepcopy(status)
    monkeypatch.setattr(gate, "request", request)
    monkeypatch.setattr(gate.time, "time", lambda: NOW)
    monkeypatch.setattr(sys, "argv", ["gate", "--base-url", "http://test", "--output", str(tmp_path), "--execute"])
    with pytest.raises(RuntimeError, match="configured kubeadm"):
        gate.main()
    assert calls == [None]
    assert len(list(tmp_path.glob("*/initial.json"))) == 1


def test_ready_requires_current_evidence_and_read_only_never_mutates(monkeypatch, tmp_path):
    status = complete_status()
    assert gate.ready_to_execute(status, NOW)
    assert not gate.ready_to_execute(status, NOW + 16)
    assert not gate.ready_to_execute(status, NOW - 2)
    calls = []
    monkeypatch.setattr(gate, "request", lambda url, body=None: calls.append(body) or status)
    monkeypatch.setattr(sys, "argv", ["gate", "--base-url", "http://test", "--output", str(tmp_path)])
    assert gate.main() == 0
    assert calls == [None]


def test_stopped_zero_release_baseline_requires_four_sources_and_exact_zero():
    status = complete_status(0)
    status.update(status="stopped", evidenceSources=["ue", "amf", "smf", "upf"])
    assert gate.ready_to_execute(status, NOW)
    status["requested"] = 1
    assert not gate.ready_to_execute(status, NOW)
    status["requested"] = 0
    status["evidenceSources"].pop()
    assert not gate.ready_to_execute(status, NOW)
    status["evidenceSources"].append("upf")
    status["perUe"] = [{"supi": "residual"}]
    assert not gate.ready_to_execute(status, NOW)
