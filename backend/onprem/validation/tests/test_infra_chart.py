"""Independent contracts for the pure Helm IaC and backend test fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
CHART = ROOT / "infrastructure/onprem"
ARTIFACTS = ROOT / "artifacts/onprem-helm-cleanup-final"


def rendered(path=None):
    path = path or Path(os.environ.get("CITYVERSE_HELM_MANIFEST", ARTIFACTS / "runtime.yaml"))
    if not path.exists():
        pytest.skip("run infrastructure/onprem/scripts/render.sh to produce the integration artifact")
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d}


def test_infra_contains_only_iac_docs_and_small_bash_scripts():
    allowed = {".yaml", ".json", ".tpl", ".md", ".sh"}
    assert not (CHART / "kubeadm").exists()
    assert {p.name for p in CHART.rglob("*.sh")} == {"render.sh", "post-render.sh", "host-network.sh", "n3-delay.sh"}
    for path in CHART.rglob("*"):
        if path.is_file():
            assert path.name == ".helmignore" or path.suffix in allowed, path
    dependency = yaml.safe_load((CHART / "Chart.yaml").read_text())["dependencies"]
    assert dependency == [{"name": "free5gc", "version": "1.1.7", "repository": "file://../upstream/charts/free5gc"}]


def test_rendered_city_has_matching_ngap_n2_n3_and_smf_slice_paths():
    docs = rendered()
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    city = values["city"]
    gnb_deployments = [d for (kind, name), d in docs.items() if kind == "Deployment" and name.startswith("gnb-")]
    assert len(gnb_deployments) == 7
    assert sum(s["tier"] == "distributed" for s in city["sites"].values()) == 3
    assert sum(s["tier"] == "central" for s in city["sites"].values()) == 1
    n2, n3, identities = set(), set(), set()
    for gnb in city["gnbs"]:
        name, slice_name = gnb["workload"], gnb["slice"]
        config = json.loads(docs["ConfigMap", name + "-config"]["data"]["gnb.json"])["gnb"]
        pod = docs["Deployment", name]["spec"]["template"]
        networks = json.loads(pod["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"])
        assert networks[0]["ips"] == [config["ranN2Ip"] + "/24"]
        assert networks[1]["ips"] == [config["ranN3Ip"] + "/24"]
        assert config["upfN3Ip"] == city["slices"][slice_name]["n3Subnet"] + ".10"
        assert config["snssai"] == {"sst": str(city["slices"][slice_name]["sst"]), "sd": city["slices"][slice_name]["sd"]}
        n2.add(config["ranN2Ip"]); n3.add(config["ranN3Ip"]); identities.add(config["gnbId"])
    assert (len(n2), len(n3), len(identities)) == (7, 7, 7)
    smf = yaml.safe_load(docs["ConfigMap", "smf-config"]["data"]["smfcfg.yaml"])["configuration"]["userplaneInformation"]
    assert smf["upNodes"]["ran-access"] == {"type": "AN"}
    assert {link["B"] for link in smf["links"]} == {"upf-" + name for name in city["slices"]}


def test_no_embedded_application_sources_and_all_images_are_pinned():
    for (kind, _), doc in rendered().items():
        assert doc["metadata"].get("namespace") == "free5gc-onprem"
        if kind == "ConfigMap":
            for key, value in doc.get("data", {}).items():
                assert not key.endswith(".py")
                assert "import os" not in value and "import json" not in value
        if kind in {"Deployment", "StatefulSet"}:
            pod = doc["spec"]["template"]["spec"]
            for container in pod.get("containers", []) + pod.get("initContainers", []):
                assert "@sha256:" in container["image"]
            if kind == "Deployment" and "cityverse.io/runtime" in doc["spec"]["selector"]["matchLabels"]:
                assert "cityverse.io/site" not in doc["spec"]["selector"]["matchLabels"]
    amf = next(d for (kind, _), d in rendered().items() if kind == "Deployment" and d["metadata"].get("labels", {}).get("nf") == "amf")
    assert amf["spec"]["strategy"] == {"type": "Recreate"}
    env = {e["name"]: e for e in amf["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AMF_NF_INSTANCE_ID"]["value"] == "23e12894-5c6a-4fb4-8bd0-010000000001"
    assert env["GIN_MODE"]["value"] == "release"


def test_n6_test_fixtures_are_the_actual_helm_pod_contract():
    from test.subsystems.onprem.native_round_fixtures import rendered_n6_pods
    from backend.onprem.runtime.ue import SLICES
    from backend.onprem.runtime.round_resources import native_receiver_pods
    docs = rendered()
    pods = rendered_n6_pods()
    assert [p["metadata"]["labels"]["app.kubernetes.io/name"] for p in pods] == ["n6-" + name for name in SLICES]
    assert set(native_receiver_pods(pods, "free5gc-onprem")) == set(SLICES)
    for pod in pods:
        name = pod["metadata"]["name"].removesuffix("-replica")
        assert pod["spec"] == docs["Deployment", name]["spec"]["template"]["spec"]


def test_resizing_one_upf_does_not_roll_other_nfs():
    resized = ROOT / "artifacts/onprem-helm-cleanup-resize/runtime.yaml"
    if not resized.exists():
        pytest.skip("one-UPF resize render artifact not configured")
    baseline, variant = rendered(), rendered(resized)
    changed = []
    for key, before in baseline.items():
        if key[0] == "Deployment" and before != variant[key]:
            changed.append(key[1])
    assert changed == ["upf-embb"]
    assert variant["Deployment", "upf-embb"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "3"
    assert baseline["ConfigMap", "smf-config"] == variant["ConfigMap", "smf-config"]
    assert baseline["ConfigMap", "nssf-config"] == variant["ConfigMap", "nssf-config"]


def test_helm_rejects_duplicate_access_addresses(tmp_path):
    package = ARTIFACTS / "cityverse-onprem-0.2.0.tgz"
    if not package.exists():
        pytest.skip("Helm integration package unavailable")
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    values["city"]["gnbs"][0]["networkIndex"] = values["city"]["gnbs"][1]["networkIndex"]
    bad = tmp_path / "bad-values.yaml"
    bad.write_text(yaml.safe_dump(values), encoding="utf-8")
    result = subprocess.run(["helm", "template", "test", str(package), "-f", str(bad)], capture_output=True, text=True)
    assert result.returncode != 0 and "duplicate gNB identity or N2/N3 address" in result.stderr
