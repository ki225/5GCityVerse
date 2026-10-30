from pathlib import Path

import pytest

from scripts.onprem import capture_ai100_runtime as tool


def test_exact_target_capture_uses_read_only_runtime_validator(tmp_path, monkeypatch):
    kubectl, kubeconfig = tmp_path / "kubectl", tmp_path / "admin.conf"
    kubectl.write_text("test")
    kubeconfig.write_text("test")
    observed = []
    def validator(driver, traffic_image):
        observed.append((driver.context, driver.namespace, driver.image,
                         driver.init_image, traffic_image))
        return {"mode": "kubeadm", "context": driver.context,
                "namespace": driver.namespace, "images": {}}
    monkeypatch.setattr(tool, "_runtime", validator)
    result = tool.capture(kubectl, kubeconfig, "kubernetes-admin@cityverse-wsl", "free5gc-onprem")
    assert result["context"] == "kubernetes-admin@cityverse-wsl"
    assert observed[0][-1] == tool.TRAFFIC_IMAGE
    with pytest.raises(ValueError, match="GET only"):
        tool.ReadOnlyDriver(kubectl, kubeconfig, result["context"], result["namespace"]).kubectl("delete", "pods")


@pytest.mark.parametrize("context,namespace", [
    ("kind-pr36-wsl", "free5gc-onprem"),
    ("kubernetes-admin@cityverse-wsl", "default")])
def test_runtime_capture_rejects_other_clusters(tmp_path, context, namespace):
    kubectl, kubeconfig = tmp_path / "kubectl", tmp_path / "admin.conf"
    kubectl.touch()
    kubeconfig.touch()
    with pytest.raises(ValueError, match="Exact WSL"):
        tool.capture(kubectl, kubeconfig, context, namespace)
