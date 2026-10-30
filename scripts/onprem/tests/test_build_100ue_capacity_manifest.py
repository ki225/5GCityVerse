"""Synthetic receipt tests only; no cluster, live traffic, or E2E claim."""
from copy import deepcopy
import hashlib
import json

import pytest

from test.subsystems.onprem.test_onprem_capacity_100_shards import manifest_bundle
from scripts.onprem.build_100ue_capacity_manifest import CASE_IDS, build_manifest
from backend.onprem.capacity.receipts import inspect_manifest
from backend.onprem.capacity.promotion import CapacityPromotionRejected, verify_capacity_promotion


def fixture_bundle(tmp_path):
    mixed = ['web_browsing', 'realtime_control', 'connected_traffic', 'factory_iot', 'video_streaming']
    groups = [[scenario] * 100 for scenario in ('video_streaming', 'realtime_control', 'connected_traffic', 'factory_iot')]
    groups.append([mixed[index % len(mixed)] for index in range(100)])
    fixture, artifacts = manifest_bundle(groups)
    for index, case_id in enumerate(CASE_IDS):
        folder = tmp_path / case_id
        folder.mkdir()
        entry = fixture['receipts'][index]
        for kind, name in (('result', 'result.json'), ('rendered', 'rendered-result.json'), ('cleanup', 'cleanup.json')):
            (folder / name).write_bytes(artifacts[entry[kind]['ref']])
        (folder / 'capture-times.json').write_text(json.dumps({
            'resultCapturedAt': entry['resultCapturedAt'],
            'uiCapturedAt': entry['uiCapturedAt']}))
        (folder / 'cleanup-capture.json').write_text(json.dumps({
            'cleanupCapturedAt': entry['cleanupCapturedAt']}))
    runtime = fixture['runtime']
    digest = hashlib.sha256(json.dumps(runtime, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return runtime, digest


def test_builder_creates_verifiable_five_case_manifest_without_rewriting_receipts(tmp_path):
    runtime, digest = fixture_bundle(tmp_path)
    before = {(case_id, name): (tmp_path / case_id / name).read_bytes()
              for case_id in CASE_IDS for name in ('result.json', 'rendered-result.json', 'cleanup.json',
                                                   'capture-times.json', 'cleanup-capture.json')}
    raw = build_manifest(tmp_path, runtime, digest)
    manifest = json.loads(raw)
    assert manifest['version'] == 2 and manifest['candidateCeiling'] == 100
    assert len(manifest['receipts']) == 5
    assert inspect_manifest(raw, lambda ref: (tmp_path / ref).read_bytes()).coverage_complete
    assert {(case_id, name): (tmp_path / case_id / name).read_bytes()
            for case_id in CASE_IDS for name in ('result.json', 'rendered-result.json', 'cleanup.json',
                                                 'capture-times.json', 'cleanup-capture.json')} == before


def test_post_build_receipt_byte_change_breaks_manifest_hash(tmp_path):
    runtime, digest = fixture_bundle(tmp_path)
    raw = build_manifest(tmp_path, runtime, digest)
    target = tmp_path / CASE_IDS[0] / 'result.json'
    target.write_bytes(target.read_bytes() + b' ')
    with pytest.raises(CapacityPromotionRejected):
        verify_capacity_promotion(manifest_bytes=raw,
            read_receipt=lambda ref: (tmp_path / ref).read_bytes(),
            expected_runtime_fingerprint=digest)


@pytest.mark.parametrize('fault', ['missing_case', 'altered_result', 'cross_round', 'wrong_runtime',
                                    'wrong_image', 'missing_shard', 'bad_capture_time'])
def test_builder_rejects_incomplete_or_altered_original_receipts(tmp_path, fault):
    runtime, digest = fixture_bundle(tmp_path)
    if fault == 'missing_case':
        (tmp_path / CASE_IDS[-1] / 'result.json').unlink()
    elif fault == 'altered_result':
        path = tmp_path / CASE_IDS[0] / 'result.json'
        path.write_bytes(path.read_bytes() + b' ')
        # Whitespace alone changes the original bytes; it remains valid if
        # both hash and evidence are derived from these exact bytes.
        value = json.loads(path.read_bytes())
        value['measurements'][0]['receivedUplinkMbps'] = 0
        path.write_text(json.dumps(value))
    elif fault == 'cross_round':
        first = json.loads((tmp_path / CASE_IDS[0] / 'result.json').read_bytes())
        path = tmp_path / CASE_IDS[1] / 'result.json'
        second = json.loads(path.read_bytes())
        second['roundId'] = first['roundId']
        path.write_text(json.dumps(second))
    elif fault == 'wrong_runtime':
        runtime = deepcopy(runtime)
        runtime['context'] = 'different-cluster'
    elif fault == 'wrong_image':
        runtime = deepcopy(runtime)
        runtime['images']['upf'] = 'upf@sha256:' + 'b' * 64
    elif fault == 'missing_shard':
        path = tmp_path / CASE_IDS[0] / 'result.json'
        value = json.loads(path.read_bytes())
        value['actions'].remove(next(a for a in value['actions'] if a.get('kind') == 'traffic_window'
             and a.get('stage') == 'measuring_result'))
        path.write_text(json.dumps(value))
    else:
        path = tmp_path / CASE_IDS[0] / 'capture-times.json'
        value = json.loads(path.read_bytes())
        value['resultCapturedAt'] = '2000-01-01T00:00:00+00:00'
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, FileNotFoundError)):
        build_manifest(tmp_path, runtime, digest)
