import copy
import inspect
import json
from pathlib import Path

import pytest

import scripts.onprem.commission_n6_reaper as commission
from scripts.onprem.commission_n6_reaper import (
    KUBECONFIG,
    KUBECTL_EXE,
    NAMESPACE,
    OLD_COMMANDS,
    REAPER_COMMAND,
    TRAFFIC_IMAGE,
    Blocked,
    command_delta_only,
    command_patch_ops,
    desired_replicas,
    scale_delta_only,
    scale_patch_ops,
    security_fingerprint,
)


def deployment(command=None, *, replicas=1):
    return {
        "metadata": {"name": "n6-embb", "uid": "uid-1", "resourceVersion": "42"},
        "spec": {"replicas": replicas, "template": {"metadata": {}, "spec": {"containers": [
            {"name": "upf", "image": "upf"},
            {"name": "traffic", "image": TRAFFIC_IMAGE, "command": command or OLD_COMMANDS[0],
             "securityContext": {"runAsNonRoot": True, "runAsUser": 1337, "allowPrivilegeEscalation": False,
                                 "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
             "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
             "volumeMounts": [{"name": "traffic-tmp", "mountPath": "/tmp"}],
             "readinessProbe": {"exec": {"command": ["iperf3", "--version"]}, "periodSeconds": 10}},
        ]}}},
        "status": {"readyReplicas": replicas},
    }


def callback_pods():
    return {"items": [{"metadata": {"name": "cityverse-af-callback-a", "uid": "cb"},
                       "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}]}


def zero_observations(**body_overrides):
    def upf(name):
        body = {"available": True, "errors": [], "source": "gtp5g-netlink", "upf": "upf-" + name,
                "sessions": [], "residuals": [], "pdrs": [], "qers": [],
                "observedAt": "2026-09-29T07:40:00+00:00"}
        body.update(body_overrides)
        return {"status": 200, "collectorNow": 1790667605.0, "body": body}
    return {"amf": {"status": 200, "body": [], "collectorNow": 1790667605.0},
            "upfs": {name: upf(name) for name in ("embb", "urllc", "mmtc", "v2x")}}


def test_kubectl_argv_is_exact_local_lab_command():
    argv = commission.kubectl_argv(["get", "pods"])
    assert argv[:8] == [str(KUBECTL_EXE), "--kubeconfig", str(KUBECONFIG),
                        "--context", "cityverse-kubeadm-lab", "--request-timeout=15s", "-n", NAMESPACE]
    assert argv[-2:] == ["get", "pods"]
    assert Path(argv[0]).as_posix().endswith("/.cache/kubeadm-lab/bin/kubectl.exe")


def test_patch_ops_pin_uid_rv_image_name_and_old_command_before_replace():
    ops = command_patch_ops(deployment())
    assert ops[:-1] == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
        {"op": "test", "path": "/spec/template/spec/containers/1/name", "value": "traffic"},
        {"op": "test", "path": "/spec/template/spec/containers/1/image", "value": TRAFFIC_IMAGE},
        {"op": "test", "path": "/spec/template/spec/containers/1/command", "value": OLD_COMMANDS[0]},
    ]
    assert ops[-1] == {"op": "replace", "path": "/spec/template/spec/containers/1/command", "value": REAPER_COMMAND}


def test_already_reaper_has_no_patch_ops():
    assert command_patch_ops(deployment(REAPER_COMMAND)) == []


def test_unexpected_old_command_blocks():
    with pytest.raises(Blocked):
        command_patch_ops(deployment(["sh", "-c", "sleep 1"]))


def test_command_delta_only_accepts_only_traffic_command_change():
    before = deployment()
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "43"
    after["spec"]["template"]["spec"]["containers"][1]["command"] = REAPER_COMMAND
    assert command_delta_only(before, after)
    after["spec"]["template"]["spec"]["containers"][1]["securityContext"]["runAsUser"] = 0
    assert not command_delta_only(before, after)


def test_security_fingerprint_excludes_command_but_includes_security_resources_probe():
    before = deployment()
    after = copy.deepcopy(before)
    after["spec"]["template"]["spec"]["containers"][1]["command"] = REAPER_COMMAND
    assert security_fingerprint(before) == security_fingerprint(after)


def test_zero_replicas_are_not_coerced_to_one():
    assert desired_replicas(deployment(replicas=0)) == 0


def test_scale_patch_is_uid_rv_and_old_value_guarded():
    ops = scale_patch_ops(deployment(replicas=0), 1)
    assert ops == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
        {"op": "test", "path": "/spec/replicas", "value": 0},
        {"op": "replace", "path": "/spec/replicas", "value": 1},
    ]
    before = deployment(replicas=0)
    after = copy.deepcopy(before)
    after["spec"]["replicas"] = 1
    assert scale_delta_only(before, after, 1)
    after["spec"]["template"]["spec"]["containers"][0]["image"] = "changed"
    assert not scale_delta_only(before, after, 1)


def test_scale_patch_blocks_nonzero_or_missing_old_replicas():
    with pytest.raises(Blocked):
        scale_patch_ops(deployment(replicas=1), 1)
    missing = deployment(replicas=0)
    del missing["spec"]["replicas"]
    with pytest.raises(Blocked):
        scale_patch_ops(missing, 1)


def test_zero_baseline_uses_callback_and_requires_strict_upf_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(commission, "kjson", lambda args, **kwargs: callback_pods())
    monkeypatch.setattr(commission, "run_kubectl", lambda args, **kwargs: json.dumps(zero_observations()))
    assert commission.verify_zero_baseline(tmp_path, "pre")["upfsEmptyFresh"] is True
    assert (tmp_path / "pre-zero-observations.json").exists()


@pytest.mark.parametrize("override", [
    {"qers": None},
    {"available": False},
    {"errors": ["netlink failed"]},
    {"source": "fixture"},
    {"upf": "upf-wrong"},
    {"observedAt": "2026-09-29T04:59:00+00:00"},
])
def test_zero_baseline_rejects_missing_stale_or_nonempty_upf_evidence(monkeypatch, tmp_path, override):
    monkeypatch.setattr(commission, "kjson", lambda args, **kwargs: callback_pods())
    monkeypatch.setattr(commission, "run_kubectl", lambda args, **kwargs: json.dumps(zero_observations(**override)))
    with pytest.raises(Blocked):
        commission.verify_zero_baseline(tmp_path, "pre")


def test_skip_zero_baseline_flag_is_removed():
    with pytest.raises(SystemExit):
        commission.argparse.ArgumentParser().parse_args(["--skip-zero-baseline"])
    assert "--skip-zero-baseline" not in Path(commission.__file__).read_text(encoding="utf-8")


def test_fixture_is_25_ue_pairs_and_requires_exact_reaping_proofs():
    source = inspect.getsource(commission.run_fixture)
    assert "seq 1 25" in commission.FIXTURE
    assert "uplink downlink" in commission.FIXTURE
    assert "live_before_parent_death=50" in source
    assert "untracked_children_before_parent_death=0" in source
    assert "adopted_to_pid1=50" in source
    assert "supervisor_signal_rc=137" in source


def test_supervisorloss_fixture_does_not_spawn_extra_untracked_child():
    assert "else sleep 20" not in commission.FIXTURE
    assert "untracked_child_count()" in commission.FIXTURE
    assert 'echo "untracked_children_before_parent_death=$(untracked_child_count "$supervisor")"' in commission.FIXTURE
    assert '[ "$(untracked_child_count "$supervisor")" = 0 ]' in commission.FIXTURE
