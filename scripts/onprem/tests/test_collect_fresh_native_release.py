from datetime import datetime, timedelta, timezone
import json

import pytest

from scripts.onprem import collect_fresh_native_release as helper


def proof_at(now):
    stamp = now.isoformat()
    zero = {name: 0 for name in helper.SLICES}
    return {
        'generation': 245, 'observedAt': stamp, 'ownedPods': [], 'rows': [],
        'amfRegistered': 0, 'smfSessions': 0, 'upfPfcpRows': 0,
        'unattributedUpfRows': 0, 'gnbUeCounts': zero,
        'subscriberVerifiedCount': 1000, 'subscriberCredentialsMatched': True,
        'gnbSource': 'free-ran-ue-gnb-api-ue-count',
        'gnbReadouts': {name: {'observedAt': stamp, 'podUid': f'uid-{name}'} for name in zero},
        'sources': {
            'amf': {'available': True, 'observedAt': stamp},
            'smf': {'available': True, 'observedAt': stamp, 'inventoryComplete': True},
            'upf': {'available': True, 'observedAt': stamp, 'source': 'gtp5g-netlink:four-upf',
                    'epoch': json.dumps({name: f'epoch-{name}' for name in zero})},
        },
    }


def test_output_directory_is_unique_and_preserves_prior_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, 'ROOT', tmp_path)
    (tmp_path / '.tmp').mkdir()
    first = helper.new_output_dir()
    (first / 'native-proof.json').write_text('prior evidence', encoding='utf-8')
    second = helper.new_output_dir()
    assert first != second and first.is_dir() and second.is_dir()
    assert (first / 'native-proof.json').read_text(encoding='utf-8') == 'prior evidence'


def test_fresh_native_zero_schema_and_age_are_required():
    now = datetime(2026, 10, 4, 11, 0, tzinfo=timezone.utc)
    proof = proof_at(now)
    helper.validate_proof(proof, 245, now=now)
    with pytest.raises(RuntimeError, match='stale'):
        helper.validate_proof(proof, 245, now=now + timedelta(seconds=31))
    proof['gnbReadouts'].pop('v2x')
    with pytest.raises(RuntimeError, match='Four fresh gNB'):
        helper.validate_proof(proof, 245, now=now)


def test_upf_epoch_and_gnb_identity_are_required():
    now = datetime(2026, 10, 4, 11, 0, tzinfo=timezone.utc)
    proof = proof_at(now)
    proof['sources']['upf']['epoch'] = json.dumps({'embb': 'only-one'})
    with pytest.raises(RuntimeError, match='Four UPF epochs'):
        helper.validate_proof(proof, 245, now=now)
    proof = proof_at(now)
    proof['gnbReadouts']['embb']['podUid'] = ''
    with pytest.raises(RuntimeError, match='gNB Pod identity'):
        helper.validate_proof(proof, 245, now=now)
