import copy
import pytest

from scripts.onprem.render_gnb_canary import render_canary


OLD = "docker.io/cityverse/free-ran-ue@sha256:" + "a" * 64
NEW = "docker.io/cityverse/free-ran-ue@sha256:" + "b" * 64


def deployment(image):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "gnb-embb", "namespace": "free5gc-onprem"},
            "spec": {"replicas": 1, "template": {"metadata": {"annotations": {"checksum/ran-evidence": "frozen"}},
                     "spec": {"containers": [{"name": "gnb-embb", "image": image},
                                             {"name": "gnb-evidence", "command": ["old-sidecar"]}]}}}}


def test_canary_changes_only_live_gnb_image():
    live, rendered = deployment(OLD), deployment(NEW)
    rendered["spec"]["template"]["spec"]["containers"][1]["command"] = ["new-sidecar"]
    output = render_canary(live, rendered)
    expected = copy.deepcopy(live)
    expected["spec"]["template"]["spec"]["containers"][0]["image"] = NEW
    assert output == expected
    assert live["spec"]["template"]["spec"]["containers"][0]["image"] == OLD


@pytest.mark.parametrize("candidate", [None, "cityverse/free-ran-ue:latest",
                                          "docker.io/other/free-ran-ue@sha256:" + "b" * 64])
def test_canary_rejects_unpinned_candidate(candidate):
    with pytest.raises(ValueError, match="immutable"):
        render_canary(deployment(OLD), deployment(candidate))
