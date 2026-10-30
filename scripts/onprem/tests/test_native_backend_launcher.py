"""Offline launcher tests: no live subprocess or Kubernetes mutation."""
import hashlib
import base64
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('native_launcher', Path(__file__).parents[1] / 'start_native_backend.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)




def test_observation_reads_current_generation_without_starting_controller(tmp_path):
    path = tmp_path / 'ue-state.sqlite3'
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE generations (id INTEGER PRIMARY KEY)')
        connection.executemany('INSERT INTO generations(id) VALUES (?)', [(199,), (200,)])
        connection.commit()
    finally:
        connection.close()
    assert launcher.current_ue_generation(path) == 200


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'LAB', tmp_path)
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin/kubectl.exe').write_bytes(b'test fixture')
    (tmp_path / 'bin/kubectl.exe.sha256').write_text(hashlib.sha256(b'test fixture').hexdigest())
    for filename in ('kubeconfig', 'groups.json', 'python.exe'):
        (tmp_path / filename).write_text('fixture')
    args = ['--kubeconfig', str(tmp_path / 'kubeconfig'), '--credentials', str(tmp_path / 'groups.json'), '--python', str(tmp_path / 'python.exe'), '--check-only']
    services = {'items': [{'metadata': {'name': service}, 'spec': {'ports': [{'port': remote}]}} for service, remote, _ in {**launcher.FORWARDS, **launcher.POLICY_FORWARDS}.values()]}
    calls = []
    def get(command, **kwargs):
        calls.append(command)
        if 'get-contexts' in command:
            return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:
            return json.dumps({'metadata': {'name': 'free5gc-onprem'}})
        if any(str(part).endswith('pfcp_preflight.py') for part in command):
            return json.dumps({'associations': {name: {'uid': name} for name in ('upf-embb','upf-urllc','upf-mmtc','upf-v2x')}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess, 'check_output', get)
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('check-only may not start any process'))
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, level, option, value):
            assert (level, option, value) == (launcher.socket.SOL_SOCKET, launcher.socket.SO_REUSEADDR, 1)
        def bind(self, address): assert address[0] == '127.0.0.1'
    monkeypatch.setattr(launcher.socket, 'socket', Socket)
    return args, calls, services


def test_checks_explicit_context_service_ports_without_launch(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    launcher.main(args)
    service_call = next(call for call in calls if call[-4:] == ['get', 'services', '-o', 'json'])
    assert service_call[1:5] == ['--kubeconfig', str(tmp_path / 'kubeconfig'), '--context', 'cityverse-kubeadm-lab']


def test_checksum_mismatch_prevents_any_api_call(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    (tmp_path / 'bin/kubectl.exe').write_bytes(b'corrupt')
    with pytest.raises(SystemExit, match='checksum mismatch'): launcher.main(args)
    assert calls == []


def test_wsl_profile_requires_explicit_secure_inputs_before_cluster_access(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    base = [arg for arg in args if arg not in ('--check-only', '--kubeconfig',
            str(tmp_path / 'kubeconfig'), '--python', str(tmp_path / 'python.exe'))]
    with pytest.raises(SystemExit, match='explicit --kubeconfig'):
        launcher.main(base + ['--profile', 'wsl-kubeadm'])
    with pytest.raises(SystemExit, match='explicit --kubectl-checksum'):
        launcher.main(base + ['--profile', 'wsl-kubeadm', '--kubeconfig', str(tmp_path / 'kubeconfig'),
                            '--kubectl', str(tmp_path / 'bin/kubectl.exe')])
    with pytest.raises(SystemExit, match='explicit --context and --namespace'):
        launcher.main(base + ['--profile', 'wsl-kubeadm', '--kubeconfig', str(tmp_path / 'kubeconfig'),
                            '--kubectl', str(tmp_path / 'bin/kubectl.exe'), '--kubectl-checksum',
                            str(tmp_path / 'bin/kubectl.exe.sha256')])
    assert calls == []


def test_cluster_context_mismatch_fails_closed(monkeypatch, tmp_path):
    args, _, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher.subprocess, 'check_output', lambda *a, **k: 'other-context\n')
    with pytest.raises(SystemExit, match='context unavailable'):
        launcher.main(args)


def test_inventory_rejects_running_smf_image_digest_mismatch(monkeypatch, tmp_path):
    args, _, services = setup(monkeypatch, tmp_path)
    bearer = base64.b64encode(b'a' * 64).decode()
    def get(command, **kwargs):
        if 'get-contexts' in command:
            return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:
            return json.dumps({'metadata': {'name': 'free5gc-onprem'}})
        if any(str(part).endswith('pfcp_preflight.py') for part in command):
            return json.dumps({'associations': {name: {} for name in
                ('upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x')}})
        if 'deployment' in command:
            return json.dumps({'spec': {'template': {'spec': {'containers': [
                {'image': 'free5gc/smf:v4.2.2'}]}}}})
        if 'secret' in command:
            return json.dumps({'data': {'token': bearer}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess, 'check_output', get)
    with pytest.raises(SystemExit, match='immutable digest'):
        launcher.main(args + ['--enable-smf-inventory'])


def test_wsl_policy_paths_require_absolute_clear_evidence_without_creating_quarantine(tmp_path):
    journal = tmp_path / 'policy.sqlite3'
    connection = sqlite3.connect(journal)
    try:
        connection.execute('CREATE TABLE policy_actions (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        connection.execute('INSERT INTO policy_actions VALUES (?, ?)',
                           ('done', json.dumps({'state': 'released'})))
        connection.commit()
    finally:
        connection.close()
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'version': 1, 'journals': [str(journal)]}), encoding='utf-8')
    quarantine = tmp_path / 'absent-quarantine.json'
    assert launcher.validate_wsl_policy_paths(inventory, quarantine) == (inventory, quarantine)
    assert not quarantine.exists()
    env = {'CITYVERSE_ONPREM_ROUND_STATE_FILE': 'poisoned-parent-round.db',
           'CITYVERSE_LOCAL_STATE_FILE': 'poisoned-parent-state.json'}
    launcher.apply_wsl_environment(env, (inventory, quarantine), tmp_path)
    assert env == {
        'CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY': str(inventory),
        'CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY': str(quarantine),
        'CITYVERSE_ONPREM_ROUND_STATE_FILE': str(tmp_path / 'onprem-rounds.sqlite3'),
        'CITYVERSE_LOCAL_STATE_FILE': str(tmp_path / 'cityverse-onprem-state.json'),
    }
    with pytest.raises(SystemExit, match='absolute'):
        launcher.validate_wsl_policy_paths(Path('relative.json'), quarantine)
    inventory.write_text(json.dumps({'version': 1, 'journals': []}), encoding='utf-8')
    with pytest.raises(SystemExit, match='missing or unresolved'):
        launcher.validate_wsl_policy_paths(inventory, quarantine)


def test_posix_sigterm_is_converted_to_keyboard_interrupt(monkeypatch):
    installed = []
    def set_signal(kind, handler):
        installed.append((kind, handler))
        return 'previous-handler'
    monkeypatch.setattr(launcher.signal, 'signal', set_signal)
    assert launcher.install_posix_sigterm_handler(True) == 'previous-handler'
    assert installed[0][0] == launcher.signal.SIGTERM
    with pytest.raises(KeyboardInterrupt):
        installed[0][1](launcher.signal.SIGTERM, None)


def test_missing_pfcp_association_prevents_launch(monkeypatch, tmp_path):
    args, _, services = setup(monkeypatch, tmp_path)
    def get(command, **kwargs):
        if 'get-contexts' in command:
            return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:
            return json.dumps({'metadata': {'name': 'free5gc-onprem'}})
        if any(str(part).endswith('pfcp_preflight.py') for part in command):
            return json.dumps({'associations': {}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess, 'check_output', get)
    with pytest.raises(RuntimeError, match='four'):
        launcher.main(args)


def test_archived_pfcp_proof_is_exactly_pinned_to_current_epochs(tmp_path):
    started = '2026-10-08T09:35:00Z'
    before = {'smf': {'startedAt': started}}
    associations = {}
    for index, name in enumerate(('upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x')):
        identity = {'pod': name + '-pod', 'uid': name + '-uid', 'node': 'node',
                    'restartCount': 0, 'startedAt': '2026-10-08T09:00:00Z'}
        before[name] = identity
        associations[name] = {**identity, 'acceptedAt': '2026-10-08T09:36:15Z',
                              'upfCreatedAt': '2026-10-08T09:36:14Z'}
    path = tmp_path / 'proof.json'
    raw = json.dumps({'associations': associations}, sort_keys=True).encode()
    path.write_bytes(raw)
    pfcp = SimpleNamespace(
        UPFS={name: 'unused' for name in associations},
        timestamp=lambda value: launcher.importlib.import_module('datetime').datetime.fromisoformat(
            value.replace('Z', '+00:00')))
    proofs = launcher.verify_archived_pfcp_proof(
        path, hashlib.sha256(raw).hexdigest(), before, '2026-10-08T09:40:00Z',
        lambda name: '2026-10-08T09:39:59Z heartbeat', pfcp)
    assert set(proofs) == set(associations)
    assert all(row['source'].startswith('operator-pinned') for row in proofs.values())


@pytest.mark.parametrize('fault', ['hash', 'upf_uid', 'before_smf', 'invalidation'])
def test_archived_pfcp_proof_fails_closed(tmp_path, fault):
    from datetime import datetime
    names = ('upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x')
    before = {'smf': {'startedAt': '2026-10-08T09:35:00Z'}}
    rows = {}
    for name in names:
        before[name] = {'pod': name, 'uid': name + '-uid', 'node': 'node',
                        'restartCount': 0, 'startedAt': '2026-10-08T09:00:00Z'}
        rows[name] = {**before[name], 'acceptedAt': '2026-10-08T09:36:15Z',
                      'upfCreatedAt': '2026-10-08T09:36:14Z'}
    if fault == 'upf_uid': rows['upf-v2x']['uid'] = 'old'
    if fault == 'before_smf': rows['upf-v2x']['acceptedAt'] = '2026-10-08T09:34:59Z'
    path = tmp_path / 'proof.json'; raw = json.dumps({'associations': rows}).encode(); path.write_bytes(raw)
    digest = ('0' * 64) if fault == 'hash' else hashlib.sha256(raw).hexdigest()
    logs = ('2026-10-08T09:37:00Z handleAssociationReleaseRequest' if fault == 'invalidation'
            else '2026-10-08T09:39:59Z heartbeat')
    pfcp = SimpleNamespace(UPFS={name: 'unused' for name in names},
                           timestamp=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')))
    with pytest.raises(RuntimeError):
        launcher.verify_archived_pfcp_proof(
            path, digest, before, '2026-10-08T09:40:00Z', lambda name: logs, pfcp)


def test_absent_service_stops_without_launch(monkeypatch, tmp_path):
    args, _, services = setup(monkeypatch, tmp_path)
    services['items'].pop(0)
    with pytest.raises(SystemExit, match='service/port absent'): launcher.main(args)


def test_unbounded_or_invalid_attempt_timeout_is_rejected(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    with pytest.raises(SystemExit): launcher.main(args + ['--ue-timeout-seconds', '0'])
    assert calls == []


def test_policy_forwards_use_actual_service_ports_and_require_both(monkeypatch, tmp_path):
    args, _, services = setup(monkeypatch, tmp_path)
    assert launcher.POLICY_FORWARDS['pcf'] == ('cityverse-free5gc-pcf-service', 8080, 18082)
    assert launcher.POLICY_FORWARDS['callback'] == ('cityverse-af-callback', 9082, 19092)
    launcher.main(args + ['--policy-forwards', '--enable-traffic'])
    services['items'].pop()
    with pytest.raises(SystemExit, match='cityverse-af-callback:9082'):
        launcher.main(args + ['--policy-forwards'])


@pytest.mark.parametrize('enabled', [False, True])
def test_traffic_flag_explicit_no_secret_or_policy_mutation(monkeypatch, tmp_path, enabled):
    args, _, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setenv('CITYVERSE_ONPREM_ROUND_STATE_FILE', 'poisoned-parent-round.db')
    monkeypatch.setenv('CITYVERSE_LOCAL_STATE_FILE', 'poisoned-parent-state.json')
    captured = []
    def start(command, **kwargs):
        captured.append((command, kwargs['env']))
        raise KeyboardInterrupt  # bounded test, no real child process
    monkeypatch.setattr(launcher.subprocess, 'Popen', start)
    launcher.main([arg for arg in args if arg != '--check-only'] + (['--enable-traffic', '--policy-forwards'] if enabled else []))
    command, env = captured[0]
    assert env['CITYVERSE_ONPREM_TRAFFIC_ENABLED'] == str(enabled).lower()
    assert env['CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED'] == 'false'
    assert env['CITYVERSE_ONPREM_RAN_BACKEND'] == 'free-ran-ue'
    # Test booleans so assertion introspection cannot print the complete env.
    for key in ('CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY',
                'CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY',
                'CITYVERSE_ONPREM_ROUND_STATE_FILE', 'CITYVERSE_LOCAL_STATE_FILE'):
        present = key in env
        assert present is False, key
    assert command[-5:-3] == ['port-forward', '--address']
    assert '127.0.0.1' in command
    assert not any('token' in part.lower() or '/admin/' in part for part in command)


def test_smf_inventory_cannot_be_enabled_by_inherited_environment(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    for key, value in {
            'CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED': 'true',
            'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE': 'untrusted-image',
            'CITYVERSE_ONPREM_SMF_INVENTORY_URL': 'http://untrusted',
            'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN': 'inherited-secret',
    }.items():
        monkeypatch.setenv(key, value)
    child_env = []
    def start(command, **kwargs):
        child_env.append(kwargs['env'])
        raise KeyboardInterrupt
    monkeypatch.setattr(launcher.subprocess, 'Popen', start)
    launcher.main([arg for arg in args if arg != '--check-only'])
    assert child_env
    assert not any('secret' in command for command in calls)
    assert all(key not in child_env[0] for key in (
        'CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED',
        'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE',
        'CITYVERSE_ONPREM_SMF_INVENTORY_URL',
        'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN'))


def test_authoritative_smf_inventory_token_is_secret_backed_and_not_logged(monkeypatch, tmp_path, capsys):
    args, _, services = setup(monkeypatch, tmp_path)
    bearer = 'a' * 64
    commands = []
    def get(command, **kwargs):
        commands.append(command)
        if 'get-contexts' in command:
            return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:
            return json.dumps({'metadata': {'name': 'free5gc-onprem'}})
        if 'deployment' in command:
            return json.dumps({'spec': {'template': {'spec': {'containers': [
                {'image': launcher.SMF_INVENTORY_IMAGE}]}}}})
        if any(str(part).endswith('pfcp_preflight.py') for part in command):
            return json.dumps({'associations': {name: {'uid': name} for name in ('upf-embb','upf-urllc','upf-mmtc','upf-v2x')}})
        if 'secret' in command:
            return json.dumps({'data': {'token': base64.b64encode(bearer.encode()).decode()}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess, 'check_output', get)
    launcher.main(args + ['--enable-smf-inventory'])
    assert commands[-1][-5:] == ['get', 'secret', 'cityverse-smf-inventory', '-o', 'json']
    assert bearer not in capsys.readouterr().out
    assert all(bearer not in str(command) for command in commands)


def test_single_500_launcher_passes_prior_proof_and_batch_250_only_to_private_worker(
        monkeypatch, tmp_path):
    args, _, services = setup(monkeypatch, tmp_path)
    monkeypatch.setenv('CITYVERSE_ONPREM_RAN_BACKEND', 'free-ran-ue')
    prior = tmp_path / 'prior-100'
    prior.mkdir()
    def get(command, **kwargs):
        if 'get-contexts' in command:
            return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:
            return json.dumps({'metadata': {'name': 'free5gc-onprem'}})
        if 'deployment' in command:
            return json.dumps({'spec': {'template': {'spec': {'containers': [
                {'image': launcher.SMF_INVENTORY_IMAGE}]}}}})
        if any(str(part).endswith('pfcp_preflight.py') for part in command):
            return json.dumps({'associations': {name: {'uid': name} for name in
                         ('upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x')}})
        if 'secret' in command:
            return json.dumps({'data': {'token': base64.b64encode(b'a' * 64).decode()}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess, 'check_output', get)

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    class Process:
        def __init__(self, worker): self.returncode = 0 if worker else None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode

    launched = []
    def start(command, **kwargs):
        worker = 'run_ue_capacity_private.py' in ' '.join(str(part) for part in command)
        launched.append((command, kwargs['env'].copy()))
        return Process(worker)
    monkeypatch.setattr(launcher.socket, 'create_connection', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(launcher.subprocess, 'Popen', start)
    launcher.main([arg for arg in args if arg != '--check-only'] +
                  ['--enable-mutations', '--enable-traffic', '--enable-smf-inventory',
                   '--capacity-gates', '500', '--capacity-prior-100', str(prior)])
    worker, env = launched[-1]
    assert worker[-4:] == ['--gates', '500', '--prior-100-run', str(prior)]
    assert env['CITYVERSE_ONPREM_UE_BATCH_SIZE'] == '250'
    assert env['CITYVERSE_ONPREM_RAN_BACKEND'] == 'free-ran-ue'
    assert len(launched) == 9  # eight pinned forwards, then one private worker; no backend


def test_single_500_launcher_rejects_missing_or_ambiguous_prior_before_kubectl(monkeypatch, tmp_path):
    args, calls, _ = setup(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match='requires --capacity-prior-100'):
        launcher.main(args + ['--capacity-gates', '500'])
    with pytest.raises(SystemExit, match='other modes must not supply'):
        launcher.main(args + ['--capacity-gates', '100', '--capacity-prior-100', str(tmp_path)])
    assert calls == []
