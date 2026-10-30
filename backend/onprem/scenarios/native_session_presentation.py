"""Bounded display of the trusted producer's complete typed session fence.

This reader grants no admission or write authority and never reconstructs a
missing nonce/fence. Source clocks and hashes stay unchanged.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
import time

from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows
from backend.onprem.runtime.ue import seconds
from backend.onprem.scenarios.contracts import SCENARIO_SLICES

SCHEMA = 'cityverse.native-session-bindings/v1'
SOURCE = 'native-free-ran-ue-typed-cohort/v1'
NATIVE = 'native-free-ran-ue-single-session/v2'
FLAGS = ('amf_registered', 'smf_active', 'pfcp_kernel_verified', 'tun_present', 'connectivity_verified')
FIELDS = set(IDENTITY_FIELDS) | {'sst', 'sd', 'pfcpSeid', 'proof', 'scenario', 'logicalSessionId',
    'service_type', 'region', 'sourceLocation', 'demandGroupId', 'servingCellId', 'nonceDigest'}
PROOF_FIELDS = {'source', 'observedAt', 'sessionKey', *FLAGS, 'mm_registration_source',
    'smfBootEpoch', 'upfEpoch', 'podUid', 'containerId', 'processEpoch', 'spatialRan', 'gnbSearchEndpoint', 'gnbPodUid'}
SPATIAL_FIELDS = {'source', 'supi', 'demandGroupId', 'gnbId', 'servingCellId', 'gnbPodUid', 'gnbSearchEndpoint',
    'gnbN3Address', 'pdr_far_gnb_verified', 'observedAt', 'vendor', 'gnbIncarnation', 'amfIncarnation', 'peer'}
WRAPPER_FIELDS = {'schemaVersion', 'source', 'roundId', 'generation', 'populationRevision', 'observedAt', 'validUntil',
    'openedAt', 'closedAt', 'openingCensusDigest', 'closingCensusDigest', 'deviceCount', 'sessionCount',
    'cohortDigest', 'allocationDigest', 'heldAllocationRef', 'bindingsDigest', 'bindings'}


def project_native_session_bindings(snapshot, *, now=None):
    """Unknown/expired proof stays unknown, even when legacy-looking rows exist."""
    unavailable = {'schemaVersion': SCHEMA, 'available': False, 'reason': 'native_session_evidence_unavailable'}
    now = time.time() if now is None else now
    def need(value):
        if not value:
            raise ValueError('Incomplete typed display proof')
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    def sealed(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
    def fresh(value):
        stamp = value if type(value) in (int, float) else seconds(value)
        need(math.isfinite(stamp) and -1 <= now - stamp <= 15)
        return stamp
    try:
        value = snapshot.get('nativeSessionBindings')
        need(isinstance(value, dict) and set(value) == WRAPPER_FIELDS)
        need(value['schemaVersion'] == SCHEMA and value['source'] == SOURCE and snapshot.get('schemaVersion') == V2)
        need(value['roundId'] == snapshot['roundId'] and type(value['generation']) is int
             and value['generation'] == snapshot['generation'] and type(value['populationRevision']) is int
             and value['populationRevision'] >= 0 and value['populationRevision'] == snapshot.get('populationRevision', 0))
        count = snapshot['allocation']['total']
        need(type(count) is int and 1 <= count <= 100 and type(value['deviceCount']) is int
             and type(value['sessionCount']) is int and value['deviceCount'] == value['sessionCount'] == count)
        observed = fresh(value['observedAt'])
        until = seconds(value['validUntil'])
        need(math.isfinite(until) and 0 < until - now and abs(until - observed - 15) < .001)
        opened, closed = fresh(value['openedAt']), fresh(value['closedAt'])
        need(opened <= closed <= observed + 1)
        need(all(sealed(value[k]) for k in ('openingCensusDigest', 'closingCensusDigest', 'cohortDigest',
            'allocationDigest', 'heldAllocationRef', 'bindingsDigest')))
        need(value['openingCensusDigest'] == value['closingCensusDigest'])
        bindings = value['bindings']
        need(isinstance(bindings, list) and len(bindings) == count and len(json.dumps(bindings)) <= 1024 * 1024)
        need(digest(bindings) == value['bindingsDigest'])
        native = {r['sessionKey']: r for r in session_rows(snapshot)}
        need(len(native) == count == len(snapshot['devices']))
        need([r['sessionKey'] for r in bindings] == sorted(native))
        need(len({r['deviceId'] for r in bindings}) == len({r['supi'] for r in bindings})
             == len({r['podUid'] for r in bindings}) == count)
        scenarios = {r['supi']: r.get('scenario') for r in snapshot['perUe']}
        need(len(scenarios) == count and set(scenarios) == {r['supi'] for r in bindings})
        for row in bindings:
            need(set(row) <= FIELDS and set(IDENTITY_FIELDS) <= set(row) and sealed(row.get('nonceDigest')))
            actual = native[row['sessionKey']]
            need(all(row.get(k) == actual.get(k) for k in (*IDENTITY_FIELDS, 'pfcpSeid')))
            need(row.get('scenario') in SCENARIO_SLICES and row.get('scenario') == scenarios.get(row['supi']))
            need(isinstance(row.get('pfcpSeid'), str) and 0 < len(row['pfcpSeid']) <= 256)
            need(all(row.get(k) == actual.get(k) for k in ('sourceLocation', 'demandGroupId', 'servingCellId')))
            proof = row['proof']
            need(isinstance(proof, dict) and set(proof) <= PROOF_FIELDS and proof.get('source') == NATIVE)
            fresh(proof.get('observedAt'))
            need(all(proof.get(k) is True for k in FLAGS) and proof.get('sessionKey') == row['sessionKey'])
            need(all(proof.get(k) == row[k] for k in ('podUid', 'containerId', 'processEpoch', 'upfEpoch')))
            need(isinstance(proof.get('smfBootEpoch'), str) and proof['smfBootEpoch'].lower() not in ('', 'unknown', 'none', 'null'))
            spatial = proof.get('spatialRan')
            need(isinstance(spatial, dict) and set(spatial) <= SPATIAL_FIELDS and spatial.get('source') == NATIVE
                 and spatial.get('supi') == row['supi'] and spatial.get('vendor') == 'free-ran-ue'
                 and spatial.get('pdr_far_gnb_verified') is True and spatial.get('gnbPodUid') == proof.get('gnbPodUid'))
            fresh(spatial.get('observedAt'))
            for k in ('gnbIncarnation', 'amfIncarnation'):
                need(isinstance(spatial.get(k), dict) and set(spatial[k]) <= {'podUid', 'containerId', 'imageId', 'restartCount'})
            peer = spatial.get('peer')
            need(isinstance(peer, dict) and set(peer) <= {'address', 'port', 'gnbAddress', 'gnbPort',
                'gnbAssociationId', 'amfAssociationId', 'ngSetupState'} and peer.get('ngSetupState') == 'CONNECTED')
            need(proof.get('mm_registration_source') == 'native-amf-current-registration+free-ran-gnb-api')
            # No unbounded arbitrary nested objects are forwarded through the display API.
            location = row.get('sourceLocation', {})
            need(isinstance(location, dict) and set(location) <= {'type', 'position', 'buildingId'})
            if location:
                need(location.get('type') == 'point' and set(location.get('position', {})) == {'x', 'y'})
                need(all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 24 for v in location['position'].values()))
        return {**deepcopy(value), 'available': True}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        return unavailable
