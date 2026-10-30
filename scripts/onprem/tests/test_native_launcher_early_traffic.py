"""CLI traffic authority must precede the first configured-driver import."""
import pytest

from scripts.onprem.tests.test_native_backend_launcher import launcher, setup


@pytest.mark.parametrize('enabled,inherited', [(True, 'false'), (False, 'true')])
def test_wsl_import_receives_explicit_cli_traffic_flag(monkeypatch, tmp_path, enabled, inherited):
    args, calls, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setenv('CITYVERSE_ONPREM_TRAFFIC_ENABLED', inherited)
    paths = (tmp_path/'kubeconfig', tmp_path/'python.exe',
             tmp_path/'bin/kubectl.exe', tmp_path/'bin/kubectl.exe.sha256', tmp_path)
    monkeypatch.setattr(launcher, 'resolve_runtime_paths', lambda _: paths)
    monkeypatch.setattr(launcher, 'resolve_cluster_target', lambda _: ('cityverse-kubeadm-lab', 'free5gc-onprem'))
    monkeypatch.setattr(launcher, 'validate_wsl_policy_paths', lambda *a, **k: None)
    monkeypatch.setattr(launcher, 'acquire_backend_lifetime_lock', lambda *a, **k: None)
    class ImportReached(Exception):
        pass
    def verify(_python, env, **_kwargs):
        assert env['CITYVERSE_ONPREM_TRAFFIC_ENABLED'] == str(enabled).lower()
        assert env['CITYVERSE_ONPREM_UE_BATCH_INTERVAL'] == '30'
        raise ImportReached
    monkeypatch.setattr(launcher, 'verify_wsl_python', verify)
    with pytest.raises(ImportReached):
        launcher.main(args + ['--profile', 'wsl-kubeadm'] + (['--enable-traffic'] if enabled else []))
    assert calls == []  # no cluster request or native process before the import
