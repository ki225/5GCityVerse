"""Strict projection of a trusted native device observer; no model evidence."""
import json

from backend.onprem.intents.domain import ContractError
from .device_sessions import DeviceAllocation, session_rows

SOURCE = 'native-ueransim-device-sessions/v1'
FREE_RAN_SOURCE = 'native-free-ran-ue-single-session/v2'
NATIVE_DEVICE_SOURCES = frozenset((SOURCE,FREE_RAN_SOURCE))


def verified_device_state(generation, evidence, *, now, maximum_age):
    from .ue import EvidenceUnavailable, blank_status, seconds
    expected = {d.device_id: d for d in (DeviceAllocation.from_dict(r['device']) for r in generation['allocation'])}
    reserved={r['device']['device_id']:{s['session_id']:s['pdu_session_id'] for s in r.get('reservedSessions',[])}
              for r in generation['allocation']}
    source=evidence.get('source')
    if (source not in NATIVE_DEVICE_SOURCES or evidence.get('schemaVersion') != 'cityverse.current-state/v2' or evidence.get('generation') != generation['generation']
            or type(evidence.get('populationRevision',0)) is not int
            or evidence.get('populationRevision',0)!=generation.get('populationRevision',0)):
        raise EvidenceUnavailable('Native device observer schema or generation mismatch')
    def fresh(value):
        at = seconds(value)
        if not generation['created'] <= at <= now+1 or now-at > maximum_age:
            raise EvidenceUnavailable('Native device proof stale or predates generation')
    fresh(evidence.get('observedAt'))
    try:
        rows = session_rows(evidence)
    except (ContractError, ValueError, KeyError, TypeError) as exc:
        raise EvidenceUnavailable('Native device session coverage malformed') from exc
    devices = {d['deviceId']: d for d in evidence['devices']}
    if set(devices) != set(expected):
        raise EvidenceUnavailable('Native physical device coverage incomplete')
    epoch = []
    for device_id, device in expected.items():
        actual = devices[device_id]
        if actual.get('supi') != device.supi or any(not actual.get(k) for k in ('podUid', 'containerId', 'processEpoch')):
            raise EvidenceUnavailable('Native device incarnation missing')
        current = [r for r in rows if r['deviceId'] == device_id]
        if source==FREE_RAN_SOURCE and len(device.sessions)!=1:
            raise EvidenceUnavailable('FreeRAN device observer cannot attest multiple PDU sessions')
        specs = {s.session_id: s for s in device.sessions}
        if len(current) != len(specs) or {r.get('logicalSessionId') for r in current} != set(specs):
            raise EvidenceUnavailable('Native device logical session coverage incomplete')
        for row in current:
            spec, proof = specs[row['logicalSessionId']], row.get('proof', {})
            if reserved[device_id].get(spec.session_id) != row['pduSessionId']:
                raise EvidenceUnavailable('Native PDU differs from exact reserved admission identity')
            if (row.get('dnn'),row.get('snssai')) != (spec.dnn,spec.snssai):
                raise EvidenceUnavailable('Native PDU does not match commissioned device session')
            if proof.get('source') != source or any(proof.get(flag) is not True for flag in
                    ('amf_registered','smf_active','pfcp_kernel_verified','tun_present','connectivity_verified')):
                raise EvidenceUnavailable('Native device establishment proof incomplete')
            if source==FREE_RAN_SOURCE:
                attachment=proof.get('spatialRan',{})
                if (attachment.get('source')!=source or attachment.get('vendor')!='free-ran-ue'
                        or attachment.get('supi')!=device.supi or attachment.get('pdr_far_gnb_verified') is not True
                        or attachment.get('gnbPodUid')!=proof.get('gnbPodUid') or not attachment.get('gnbIncarnation')
                        or not attachment.get('amfIncarnation') or attachment.get('peer',{}).get('ngSetupState')!='CONNECTED'
                        or proof.get('mm_registration_source')!='native-amf-current-registration+free-ran-gnb-api'):
                    raise EvidenceUnavailable('FreeRAN current reciprocal native attachment missing')
            fresh(proof.get('observedAt'))
            if proof.get('sessionKey') != row['sessionKey'] or not proof.get('smfBootEpoch') or proof.get('upfEpoch') != row['upfEpoch']:
                raise EvidenceUnavailable('Native device SMF or UPF incarnation mismatch')
            if any(proof.get(k) != actual[k] for k in ('podUid','containerId','processEpoch')):
                raise EvidenceUnavailable('Native PDU belongs to a different process')
            epoch.append((row['sessionKey'],proof['smfBootEpoch'],row['upfEpoch'],actual['podUid'],actual['containerId'],actual['processEpoch']))
    result = blank_status(generation['generation'], generation['desired'])
    # Legacy aggregate counters count physical UEs. Exact PDU totals have their
    # own fields; two PDU sessions never become two registrations.
    for field in ('allocated','registered','pduEstablished','trafficVerified'):
        result[field] = len(devices)
    devices=[dict(d,slice=expected[d['deviceId']].access_slice,registered=True,pduEstablished=True,trafficVerified=True) for d in evidence['devices']]
    rows=[dict(r,registered=True,pduEstablished=True,trafficVerified=True) for r in rows]
    result.update(schemaVersion='cityverse.current-state/v2', source=source,
        populationRevision=evidence.get('populationRevision',0),
        devices=devices,perUe=devices,perSession=rows,
        requestedSessions=sum(len(d.sessions) for d in expected.values()),
        establishedSessions=len(rows),observedAt=evidence['observedAt'],errors=[],
        evidenceSources=[source])
    for name, values in result['slices'].items():
        count = sum(d.access_slice == name for d in expected.values())
        for field in ('allocated','registered','pduEstablished','trafficVerified'):
            values[field] = count
    for name in ('sources','gnb','subscriberPool','ranHealth','spatialRan','observationFence'):
        if name in evidence:
            result[name] = evidence[name]
    return result, json.dumps(sorted(epoch),separators=(',',':'))
