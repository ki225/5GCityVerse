"""Classify native device sessions from current sealed configuration and proof.

The trusted device observer proves the launched process and PDU/TUN lineage.
Applications declared in configuration do not constitute observed flow evidence.
"""
from copy import deepcopy
import hashlib
import json
import math
import time

from .domain import ContractError
from .service_classification import configured_profile
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows
from backend.onprem.runtime.device_sessions import DeviceAllocation
from backend.onprem.runtime.device_verification import NATIVE_DEVICE_SOURCES, verified_device_state
from backend.onprem.runtime.ue import seconds


class NativeDeviceClassificationReader:
    def __init__(self, *, status_reader, generation_reader, legacy_reader=None,
                 catalogue=(), maximum_age_seconds=15, clock=time.time):
        if not callable(status_reader) or not callable(generation_reader):
            raise ContractError('native_device_classification_readers_required')
        self.status_reader, self.generation_reader = status_reader, generation_reader
        self.legacy_reader = legacy_reader
        self.catalogue = tuple(catalogue)
        self.maximum_age, self.clock = maximum_age_seconds, clock

    def current_classifications(self, raw, native_rows):
        if raw.get('schemaVersion') != V2:
            if self.legacy_reader is not None:
                return self.legacy_reader.current_classifications(raw, native_rows)
            result = {}
            for owner in session_rows(raw):
                matches = [r for r in self.catalogue if r.get('supi') == owner.get('supi')
                           and str(r.get('pdu_session_id')) == str(owner.get('sessionId'))
                           and r.get('native_generation') == raw.get('generation')]
                if len(matches) != 1:
                    raise ContractError('native_ue_classification_not_verified')
                result[(owner['supi'], str(owner['sessionId']))] = matches[0]
            return result
        try:
            before = deepcopy(self.generation_reader())
            if before['generation'] != raw['generation']:
                raise ValueError('Generation changed')
            actual = self.status_reader()
            if actual.get('source') not in NATIVE_DEVICE_SOURCES or actual.get('errors'):
                raise ValueError('Trusted device observer required')
            verified_device_state(before, actual, now=self.clock(), maximum_age=self.maximum_age)
            actual_rows = {r['sessionKey']: r for r in session_rows(actual)}
            owners = session_rows(raw)
            if {r['sessionKey'] for r in owners} != set(actual_rows):
                raise ValueError('Current native cohort changed')
            allocation = {r['device']['device_id']: DeviceAllocation.from_dict(r['device'])
                          for r in before['allocation']}
            digest = hashlib.sha256(json.dumps(before['allocation'], sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
            result = {}
            for owner in owners:
                row = actual_rows[owner['sessionKey']]
                if any(owner.get(k) != row.get(k) for k in IDENTITY_FIELDS):
                    raise ValueError('Current native incarnation changed')
                device = allocation[row['deviceId']]
                specs = [s for s in device.sessions if s.session_id == row.get('logicalSessionId')]
                if len(specs) != 1:
                    raise ValueError('Logical session not commissioned')
                spec = specs[0]
                native = native_rows[(row['supi'], str(row['pduSessionId']))]
                proof = row['proof']
                created = native.get('created_at')
                if (native.get('session_key') != row['sessionKey']
                        or native.get('supi') != row['supi']
                        or type(native.get('sessionId')) is not int or native['sessionId'] != row['pduSessionId']
                        or native.get('smContextRef') != row['establishedEpoch']
                        or native.get('establishedEpoch') != row['establishedEpoch']
                        or native.get('networkScope') != device.network_scope
                        or native.get('upf_id') != row['nativeUpfId']
                        or native.get('upfEpoch') != row['upfEpoch']
                        or not isinstance(proof.get('smfBootEpoch'), str)
                        or proof['smfBootEpoch'].lower() in {'unknown', 'none', 'null', ''}
                        or proof['smfBootEpoch'] != native.get('smf_boot_epoch')
                        or (native.get('dnn'), f"{native['sst']}-{native['sd']}") != (spec.dnn, spec.snssai)
                        or (row['dnn'], row['snssai']) != (spec.dnn, spec.snssai)
                        or type(created) not in (int, float) or not math.isfinite(created)
                        or not before['created'] <= created <= self.clock()
                        or not 0 <= self.clock() - seconds(proof['observedAt']) <= self.maximum_age):
                    raise ValueError('Native session proof differs')
                result[(row['supi'], str(row['pduSessionId']))] = dict(
                    source='ue-configuration',
                    configuration_id=f"{row['podUid']}:{row['containerId']}:{row['processEpoch']}:{digest}:{spec.session_id}",
                    supi=row['supi'], pdu_session_id=row['pduSessionId'], native_generation=raw['generation'],
                    s_nssai=spec.snssai, dnn=spec.dnn, region=spec.region, service_type=spec.service_type,
                    service_profile_id=configured_profile(spec.session_id, spec.service_type, row['slice']))
            after = self.generation_reader()
            if any(after.get(k) != before.get(k) for k in ('generation', 'created', 'allocation')):
                raise ValueError('Generation changed during classification')
            return result
        except Exception:
            raise ContractError('native_device_classification_not_verified') from None
