from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.onprem import check_fru_native_zero as gate


def status(generation=224):
    return {'generation': generation, 'status': 'stopped', 'errors': [], 'perUe': [],
            **dict.fromkeys(gate.ZERO_FIELDS, 0)}


def proof(generation=224, *, age=0):
    now = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    return {'generation': generation, 'observedAt': now, 'ownedPods': [], 'rows': [],
            'amfRegistered': 0, 'smfSessions': 0, 'upfPfcpRows': 0,
            'unattributedUpfRows': 0, 'gnbUeCounts': dict.fromkeys(gate.SLICES, 0),
            'gnbSource': 'free-ran-ue-gnb-api-ue-count',
            'gnbReadouts': {name: {'observedAt': now, 'podUid': 'current-' + name}
                            for name in gate.SLICES},
            'subscriberVerifiedCount': 1000, 'subscriberCredentialsMatched': True,
            'sources': {
                'amf': {'available': True, 'observedAt': now},
                'smf': {'available': True, 'observedAt': now, 'inventoryComplete': True},
                'upf': {'available': True, 'observedAt': now,
                        'source': 'gtp5g-netlink:four-upf',
                        'epoch': json.dumps({name: 'current-epoch' for name in gate.SLICES})},
            }}


def intent(generation=224):
    zero = dict.fromkeys(gate.SLICES, 0)
    return {'before': {'generation': generation - 1}, 'desired': zero,
            'request': zero, 'httpStatus': 202, 'intent': {'generation': generation}}


def test_current_native_zero_passes():
    assert gate.validate_zero(status(), proof(), now=datetime.now(timezone.utc)) == 224


def test_owned_zero_receipt_rejects_wrong_request_or_generation(tmp_path):
    receipt = tmp_path / 'intent.json'
    receipt.write_text(json.dumps(intent()), encoding='utf-8')
    assert gate.owned_zero_generation(receipt) == 224
    wrong = intent()
    wrong['request']['embb'] = 1
    receipt.write_text(json.dumps(wrong), encoding='utf-8')
    with pytest.raises(RuntimeError):
        gate.owned_zero_generation(receipt)


def test_live_database_is_unchanged_by_scratch_snapshot(tmp_path):
    source, target = tmp_path / 'live.sqlite3', tmp_path / 'scratch.sqlite3'
    with sqlite3.connect(source) as database:
        database.execute('CREATE TABLE proof (generation INTEGER)')
        database.execute('INSERT INTO proof VALUES (224)')
    before = source.read_bytes()
    gate.snapshot_database(source, target)
    assert source.read_bytes() == before
    with sqlite3.connect(target) as database:
        assert database.execute('SELECT generation FROM proof').fetchone() == (224,)


@pytest.mark.parametrize('fault', [
    'ue_count', 'ue_per_ue', 'generation', 'stale', 'amf', 'smf', 'smf_inventory',
    'upf', 'upf_epoch_missing', 'upf_epoch_wrong', 'gnb', 'gnb_stale',
    'gnb_uid', 'gnb_source', 'owned_pod',
])
def test_any_missing_or_residual_native_zero_fails(fault):
    ue, native = status(), proof()
    if fault == 'ue_count': ue['pduEstablished'] = 1
    elif fault == 'ue_per_ue': ue['perUe'] = [{'supi': 'residual'}]
    elif fault == 'generation': native['generation'] -= 1
    elif fault == 'stale': native = proof(age=31)
    elif fault == 'amf': native['amfRegistered'] = 1
    elif fault == 'smf': native['smfSessions'] = 1
    elif fault == 'smf_inventory': native['sources']['smf']['inventoryComplete'] = False
    elif fault == 'upf': native['upfPfcpRows'] = 1
    elif fault == 'upf_epoch_missing': native['sources']['upf'].pop('epoch')
    elif fault == 'upf_epoch_wrong': native['sources']['upf']['epoch'] = json.dumps({'embb': 'only-one'})
    elif fault == 'gnb': native['gnbUeCounts']['mmtc'] = 1
    elif fault == 'gnb_stale':
        native['gnbReadouts']['mmtc']['observedAt'] = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
    elif fault == 'gnb_uid': native['gnbReadouts']['mmtc']['podUid'] = ''
    elif fault == 'gnb_source': native['gnbSource'] = 'unknown'
    elif fault == 'owned_pod': native['ownedPods'] = [{'name': 'cv-ue-residual'}]
    with pytest.raises(RuntimeError):
        gate.validate_zero(ue, native, now=datetime.now(timezone.utc))


def test_read_only_gate_runs_observer_and_fences_generation(tmp_path, monkeypatch):
    (tmp_path / '.tmp').mkdir()
    inputs = [tmp_path / name for name in ('kubectl.exe', 'kubeconfig', 'groups.json', 'python.exe', 'ue.sqlite3')]
    for path in inputs:
        path.touch()
    inputs[4].with_suffix('.oam.sqlite3').touch()
    receipt = tmp_path / 'intent.json'
    receipt.write_text(json.dumps(intent()), encoding='utf-8')
    args = SimpleNamespace(kubectl=inputs[0], kubeconfig=inputs[1], credentials=inputs[2],
                           python=inputs[3], state=inputs[4], output=tmp_path / 'report.json',
                           api_url='http://127.0.0.1:8000', context='cityverse-kubeadm-lab',
                           namespace='free5gc-onprem', intent=receipt, wait_seconds=0)
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    monkeypatch.setattr(gate, 'uuid4', lambda: SimpleNamespace(hex='test'))
    monkeypatch.setattr(gate, 'read_smf_inventory_token', lambda *_: 'private-token')
    monkeypatch.setattr(gate, 'snapshot_database', lambda source, target: target.touch())
    observed = []
    monkeypatch.setattr(gate, 'get_status', lambda *_: observed.append(1) or status())

    def run(command, **kwargs):
        assert command[1] == str(tmp_path / 'scripts/onprem/observe_ue_native.py')
        assert kwargs['env']['CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN'] == 'private-token'
        Path(command[-1]).write_text(json.dumps(proof()), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, 'run', run)
    assert gate.check(args)['status'] == 'passed'
    assert len(observed) == 2
    assert json.loads(args.output.read_text())['generation'] == 224


def test_generation_change_after_observation_blocks_report(tmp_path, monkeypatch):
    (tmp_path / '.tmp').mkdir()
    inputs = [tmp_path / name for name in ('kubectl.exe', 'kubeconfig', 'groups.json', 'python.exe', 'ue.sqlite3')]
    for path in inputs:
        path.touch()
    inputs[4].with_suffix('.oam.sqlite3').touch()
    receipt = tmp_path / 'intent.json'
    receipt.write_text(json.dumps(intent()), encoding='utf-8')
    args = SimpleNamespace(kubectl=inputs[0], kubeconfig=inputs[1], credentials=inputs[2],
                           python=inputs[3], state=inputs[4], output=tmp_path / 'report.json',
                           api_url='http://127.0.0.1:8000', context='cityverse-kubeadm-lab',
                           namespace='free5gc-onprem', intent=receipt, wait_seconds=0)
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    monkeypatch.setattr(gate, 'uuid4', lambda: SimpleNamespace(hex='changed'))
    monkeypatch.setattr(gate, 'read_smf_inventory_token', lambda *_: 'private-token')
    monkeypatch.setattr(gate, 'snapshot_database', lambda source, target: target.touch())
    replies = iter((status(), status(225)))
    monkeypatch.setattr(gate, 'get_status', lambda *_: next(replies))

    def run(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(proof()), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, 'run', run)
    with pytest.raises(RuntimeError):
        gate.check(args)
    assert not args.output.exists()
