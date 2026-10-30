"""Read-only builder uses actual V2 shape and original sealed held/nonces."""
from copy import deepcopy
import hashlib
import json

import pytest

from backend.onprem.runtime.ue import EvidenceVerifier
from backend.onprem.runtime.ue_reservation import HeldUERepository
from scripts.onprem.commission_free_ran_profile import build_profile
from test.intents.test_free_ran_device_observer import build


def seal(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def fixture(tmp_path):
    observer, generation, census, _, _, _, _, pod = build(tmp_path)
    evidence = observer.observe(generation['generation'])
    status = EvidenceVerifier(stable_seconds=0).evaluate(generation, evidence, now=110)
    capture = {'status': status, 'pods': {pod['metadata']['name']: pod}}
    document = {'native_census': census,
                'bootstrap_ue_evidence': {'before': deepcopy(capture), 'after': deepcopy(capture)},
                'bootstrap_device_admission': {'generation': generation,
                    'driver_configuration_digest': observer.driver.reservation_configuration_digest,
                    'held': HeldUERepository(observer.store).current().to_dict()}}
    reseal(document)
    return observer, document


def reseal(document):
    document['bootstrap_ue_evidence_sha256'] = seal(document['bootstrap_ue_evidence'])
    document['bootstrap_device_admission_sha256'] = seal(document['bootstrap_device_admission'])


def derive(observer, document):
    return build_profile({'session_inventory': {'network_scope': 'network'}}, observer.driver, document,
        supi=document['native_census']['sessions'][0]['supi'], service_type='video', region='stadium',
        logical_session_id='video_streaming', evidence_reference='test-original-sealed-canary', now=110)


def test_v2_single_canary_profile_derives_actual_pdu_and_pins_held_nonce_receipt(tmp_path):
    observer, document = fixture(tmp_path)
    profile = derive(observer, document)
    assert profile.source == 'native-ue-device-launch-profile/v2'
    assert profile.device_session_ids == (('embb', 'video_streaming', 1),)
    assert profile.configuration_digest == observer.driver.reservation_configuration_digest
    assert 'bootstrap-device-sha256=' + document['bootstrap_device_admission_sha256'] in profile.evidence_reference
    assert document['bootstrap_ue_evidence']['before']['status']['perUe'][0].get('sessionId') is None


@pytest.mark.parametrize('damage', ['missing_held', 'wrong_nonce', 'missing_proof', 'wrong_session',
                                   'wrong_pod', 'changed_after_process', 'wrong_initializer', 'stale',
                                   'different_driver_digest', 'wrong_process_proof', 'wrong_smf_epoch'])
def test_v2_missing_or_changed_last_native_binding_cannot_build_profile(tmp_path, damage):
    observer, document = fixture(tmp_path)
    before = document['bootstrap_ue_evidence']['before']
    if damage == 'missing_held':document.pop('bootstrap_device_admission')
    if damage == 'wrong_nonce':document['bootstrap_device_admission']['generation']['allocation'][0]['reservedSessions'][0]['nonce'] = 'changed'
    if damage == 'missing_proof':before['status']['perSession'][0].pop('proof')
    if damage == 'wrong_session':before['status']['perSession'][0]['sessionId'] = 4
    if damage == 'wrong_pod':before['status']['perSession'][0]['podUid'] = 'foreign'
    if damage == 'changed_after_process':
        list(document['bootstrap_ue_evidence']['after']['pods'].values())[0]['status']['containerStatuses'][0]['containerID'] = 'changed'
    if damage == 'wrong_initializer':
        list(before['pods'].values())[0]['spec']['initContainers'][0]['command'][-1] += '\n# changed'
    if damage == 'stale':before['status']['observedAt'] = '1969-12-31T23:00:00+00:00'
    if damage == 'different_driver_digest':document['bootstrap_device_admission']['driver_configuration_digest'] = '0'*64
    if damage == 'wrong_process_proof':before['status']['perSession'][0]['proof']['processEpoch'] = 'foreign'
    if damage == 'wrong_smf_epoch':before['status']['perSession'][0]['proof']['smfBootEpoch'] = 'foreign'
    document['bootstrap_ue_evidence_sha256'] = seal(document['bootstrap_ue_evidence'])
    if 'bootstrap_device_admission' in document:
        document['bootstrap_device_admission_sha256'] = seal(document['bootstrap_device_admission'])
    with pytest.raises((ValueError, KeyError)):
        derive(observer, document)
