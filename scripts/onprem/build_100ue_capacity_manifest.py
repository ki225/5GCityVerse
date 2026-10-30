"""Build and verify a 100 UE capacity manifest from existing operator receipts.

This tool reads five case directories and writes only the resulting manifest.
It never starts a round, changes receipt capture times, or claims live E2E.
The supplied runtime fingerprint must come from the target live deployment.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.capacity.performance import criteria_metadata
from backend.onprem.capacity.receipts import inspect_manifest
from backend.onprem.capacity.promotion import verify_capacity_promotion
from backend.onprem.traffic.planning import PROFILE_ID, plan_workload
from backend.onprem.scenarios.contracts import EVENT_SLICES
EVENTS = tuple(EVENT_SLICES)


def cases():
    concentrated = (('concentrated-embb', 'video_streaming'), ('concentrated-urllc', 'realtime_control'),
                    ('concentrated-v2x', 'connected_traffic'), ('concentrated-mmtc', 'factory_iot'))
    rows = [{'id': name, 'baselineUes': 0,
             'events': {event: 100 if event == group else 0 for event in EVENTS}, 'total': 100}
            for name, group in concentrated]
    groups = ['web_browsing', 'realtime_control', 'connected_traffic', 'factory_iot', 'video_streaming']
    rows.append({'id': 'mixed', 'baselineUes': sum(groups[index % len(groups)] == 'web_browsing' for index in range(100)),
                 'events': {event: sum(groups[index % len(groups)] == event for index in range(100)) for event in EVENTS},
                 'total': 100})
    return rows


CASE_IDS = tuple(case['id'] for case in cases())
RECEIPTS = {'result': 'result.json', 'rendered': 'rendered-result.json', 'cleanup': 'cleanup.json'}


def _object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate receipt field')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite receipt value')))
    if not isinstance(value, dict):
        raise ValueError('Receipt must be an object')
    return value


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing recorded capture time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Capture time lacks timezone')
    return value


def _read_case(root: Path, case: dict) -> dict:
    folder = root / case['id']
    captures = _object((folder / 'capture-times.json').read_bytes())
    cleanup_capture = _object((folder / 'cleanup-capture.json').read_bytes())
    result_raw = (folder / RECEIPTS['result']).read_bytes()
    result = _object(result_raw)
    if (result.get('allocation') != {key: case[key] for key in ('baselineUes', 'events', 'total')}
            or result.get('strategy') != 'none'):
        raise ValueError(case['id'] + ': allocation or strategy differs from planned case')
    fingerprint = plan_workload(result['perUe']).comparison_fingerprint
    entry = {'resultCapturedAt': _stamp(captures.get('resultCapturedAt')),
             'uiCapturedAt': _stamp(captures.get('uiCapturedAt')),
             'cleanupCapturedAt': _stamp(cleanup_capture.get('cleanupCapturedAt')),
             'workloadFingerprint': fingerprint}
    for kind, filename in RECEIPTS.items():
        raw = (folder / filename).read_bytes()
        _object(raw)
        entry[kind] = {'ref': case['id'] + '/' + filename,
                       'sha256': hashlib.sha256(raw).hexdigest()}
    return entry


def build_manifest(receipts_root: Path, runtime: dict, expected_runtime_fingerprint: str) -> bytes:
    """Return bytes only after the complete original receipt set passes verification."""
    root = receipts_root.resolve(strict=True)
    manifest = {'version': 2, 'profileId': PROFILE_ID, 'candidateCeiling': 100,
        'priorCeiling': 50, 'capacityCriteria': criteria_metadata(), 'runtime': runtime,
        'receipts': [_read_case(root, case) for case in cases()]}
    raw = (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    def read_receipt(ref):
        target = (root / ref).resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError('Receipt escapes root')
        return target.read_bytes()
    inspection = inspect_manifest(raw, read_receipt)
    if not inspection.coverage_complete or inspection.candidate_ceiling != 100 or inspection.errors:
        raise ValueError('100 UE original receipts fail capacity, UI, or cleanup verification')
    grant = verify_capacity_promotion(manifest_bytes=raw, read_receipt=read_receipt,
                                      expected_runtime_fingerprint=expected_runtime_fingerprint)
    if grant.verified_max_ues != 100:
        raise ValueError('Verified capacity differs from 100 UE')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts-root', type=Path, required=True)
    parser.add_argument('--runtime-json', type=Path, required=True)
    parser.add_argument('--expected-runtime-sha256', required=True,
                        help='SHA256 of target live runtime JSON in canonical sorted compact form')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    runtime = _object(args.runtime_json.read_bytes())
    raw = build_manifest(args.receipts_root, runtime, args.expected_runtime_sha256)
    with args.output.open('xb') as target:
        target.write(raw)
    print(json.dumps({'manifest': str(args.output), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'candidateCeiling': 100, 'receiptCases': len(CASE_IDS),
                      'liveE2e': 'not-established'}))


if __name__ == '__main__':
    main()
