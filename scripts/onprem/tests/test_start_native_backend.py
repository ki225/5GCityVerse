import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import pytest

source = Path(__file__).resolve().parents[1] / 'start_native_backend.py'
spec = importlib.util.spec_from_file_location('native_helper_pfcp_test', source)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_helper_pfcp_preflight_uses_exact_context_and_all_four_proofs():
    expected = {name: {'uid': name} for name in ('upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x')}
    with patch.object(helper.subprocess, 'check_output', return_value=json.dumps({'associations': expected})) as call:
        result = helper.verify_pfcp_associations('pinned-kubectl', 'explicit-config', 'cityverse-kubeadm-lab', 'free5gc-onprem')
    assert result['associations'] == expected
    args = call.call_args.args[0]
    assert args[args.index('--context') + 1] == 'cityverse-kubeadm-lab'
    assert args[args.index('--kubectl') + 1] == 'pinned-kubectl'


def test_helper_pfcp_missing_or_failed_association_cannot_be_ignored():
    with patch.object(helper.subprocess, 'check_output', return_value='{"associations": {}}'):
        with pytest.raises(RuntimeError, match='four'):
            helper.verify_pfcp_associations('kubectl', 'config', 'context', 'namespace')
    with patch.object(helper.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, 'preflight')):
        with pytest.raises(subprocess.CalledProcessError):
            helper.verify_pfcp_associations('kubectl', 'config', 'context', 'namespace')


def test_optional_policy_forwards_keep_actual_pcf_port_and_no_collision():
    assert helper.POLICY_FORWARDS['pcf'] == ('cityverse-free5gc-pcf-service', 8080, 18082)
    ports = [row[2] for row in {**helper.FORWARDS, **helper.POLICY_FORWARDS}.values()]
    assert len(ports) == len(set(ports)) == 10


def test_smf_inventory_image_override_requires_exact_immutable_cityverse_digest():
    original = helper.SMF_INVENTORY_IMAGE
    candidate = 'docker.io/cityverse/free5gc-smf@sha256:' + 'a' * 64
    assert helper.validate_smf_inventory_image(original) == original
    assert helper.validate_smf_inventory_image(candidate) == candidate
    for invalid in ('cityverse/free5gc-smf:latest', 'docker.io/cityverse/free5gc-smf:latest',
                    'docker.io/other/free5gc-smf@sha256:' + 'a' * 64,
                    candidate.upper(), candidate + '/suffix'):
        with pytest.raises(SystemExit, match='immutable'):
            helper.validate_smf_inventory_image(invalid)

@pytest.mark.skipif(helper.fcntl is None, reason='requires POSIX flock')
def test_wsl_launcher_lifetime_lock_excludes_offline_owner(tmp_path):
    first = helper.acquire_backend_lifetime_lock(tmp_path, profile='wsl-kubeadm')
    try:
        with pytest.raises(SystemExit, match='Offline maintenance owns'):
            helper.acquire_backend_lifetime_lock(tmp_path, profile='wsl-kubeadm')
    finally:
        first.close()


def test_legacy_launcher_does_not_use_wsl_lifetime_lock(tmp_path):
    assert helper.acquire_backend_lifetime_lock(tmp_path, profile='legacy-windows') is None


def test_detached_batch_launcher_flag_is_wsl_batch_scenario_only():
    assert helper.validate_detached_batch_mode(
        profile='wsl-kubeadm', detached=True, batch=True, scenario=True) is True
    assert helper.validate_detached_batch_mode(
        profile='legacy-windows', detached=False, batch=False, scenario=False) is False
    for values in (
            dict(profile='legacy-windows', detached=True, batch=True, scenario=True),
            dict(profile='wsl-kubeadm', detached=True, batch=False, scenario=True),
            dict(profile='wsl-kubeadm', detached=True, batch=True, scenario=False)):
        with pytest.raises(SystemExit, match='WSL profile'):
            helper.validate_detached_batch_mode(**values)


def test_formal_chart_and_native_inventory_launcher_pin_the_same_smf_image():
    import yaml
    values = yaml.safe_load((source.parents[2] / 'infrastructure/onprem/values.yaml').read_text())
    assert values['images']['smf'] == helper.SMF_INVENTORY_IMAGE
