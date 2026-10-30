"""City geometry must agree with the actual per-peer kernel plan."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import yaml
import pytest

from backend.onprem.operations.validation import transport_probe
from backend.onprem.operations.validation.transport_probe import delay_us, validate_plan

ROOT = Path(__file__).resolve().parents[4]


def test_helm_delay_plan_is_symmetric_and_uses_geometry_and_tier():
    city = yaml.safe_load((ROOT / "infrastructure/onprem/values.yaml").read_text())["city"]
    manifest = Path(os.environ.get("CITYVERSE_TRANSPORT_MANIFEST", ROOT / "artifacts/transport-delay-render/runtime.yaml"))
    docs = yaml.safe_load_all(manifest.read_text())
    plan = json.loads(next(d for d in docs if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cityverse-transport-delay")["data"]["n3-delay.json"])["endpoints"]
    assert len(plan) == 11
    for gnb in city["gnbs"]:
        selected = city["slices"][gnb["slice"]]
        expected = delay_us(city, gnb, city["sites"][selected["site"]])
        uplink = plan[gnb["workload"]]["peers"]
        assert len(uplink) == 1
        assert uplink[0]["delayUs"] == expected
        assert uplink[0]["ip"] == selected["n3Subnet"] + ".10"
        downlink = next(p for p in plan["upf-" + gnb["slice"]]["peers"] if p["ip"] == selected["n3Subnet"] + "." + str(gnb["n3Host"]))
        assert downlink["delayUs"] == expected
    assert len(plan["upf-embb"]["peers"]) == 3
    assert plan["gnb-mmtc"]["peers"][0]["delayUs"] > plan["gnb-concert"]["peers"][0]["delayUs"]
    validate_plan(city, plan)
    plan["gnb-concert"]["peers"][0]["delayUs"] += 100
    with pytest.raises(ValueError, match="differs from current city"):
        validate_plan(city, plan)


def test_delay_plan_does_not_change_nf_workloads():
    def docs(path):
        return {(d["kind"],d["metadata"]["name"]):d for d in yaml.safe_load_all(path.read_text()) if d}
    before = docs(ROOT / "artifacts/onprem-helm-cleanup-final/runtime.yaml")
    after = docs(ROOT / "artifacts/transport-delay-physical/runtime.yaml")
    assert set(after) - set(before) == {("ConfigMap", "cityverse-transport-delay")}
    assert [key for key,value in before.items() if after[key] != value] == [("ConfigMap", "cityverse-city-inventory")]


@pytest.mark.parametrize("fault,check", [("teid", "gtpuTeidsMatchNgapAndPfcp"), ("capture", "captureNoKernelDrops")])
def test_protocol_acceptance_rejects_broken_live_evidence(tmp_path, monkeypatch, fault, check):
    source = ROOT / "artifacts/transport-video_streaming-20261029-lowload/transport"
    if not (source / "n3-decoded.tsv").exists():
        pytest.skip("run the live transport acceptance to capture protocol fixtures")
    for name in ("n2", "n3-ran", "n3", "n4", "n6"):
        shutil.copyfile(source / (name + "-capture.log"), tmp_path / (name + "-capture.log"))
    if fault == "capture":
        (tmp_path / "n3-capture.log").write_text("1 packets dropped by kernel\n")

    def replay(*args):
        capture = Path(args[args.index("-r") + 1]).stem
        lines = (source / (capture + "-decoded.tsv")).read_text().splitlines(keepends=True)[1:]
        output = "".join(lines)
        if fault == "teid" and capture == "n3":
            output = output.replace("0x0000000e", "0xdeadbeef")
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(transport_probe, "local", replay)
    probe = transport_probe.TransportProbe.__new__(transport_probe.TransportProbe)
    probe.output = tmp_path
    probe.report = json.loads((source / "result.json").read_text())
    with pytest.raises(RuntimeError, match="protocol acceptance failed"):
        probe.decode("10.101.0.4")
    assert probe.report["protocols"]["checks"][check] is False
