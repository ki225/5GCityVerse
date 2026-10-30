"""Read-only compatibility for the historical V2 spatial device-proof shape.

This establishes exact ownership scope only. Native PCF absence must still be
queried independently, and no durable round or historical proof is rewritten.
"""
from backend.onprem.runtime.device_sessions import DeviceAllocation
from backend.onprem.runtime.ue_reservation import HeldUEAllocation, ReservedUE


def verify_spatial_device_scope(snapshot, sessions, record, held):
    def need(condition):
        if not condition:
            raise ValueError('Incomplete historical spatial device/session ownership')

    proof = snapshot.get('connectivityEvidence') or {}
    devices, summaries = snapshot.get('devices'), snapshot.get('perUe')
    need(type(held) is HeldUEAllocation and bool(held.devices) and isinstance(record, dict))
    need(held.request_id == snapshot.get('requestId')
         and record.get('generation') == snapshot.get('generation')
         and held.baseline_generation < record['generation']
         and record.get('desired') == dict(held.counts))
    need(isinstance(summaries, list) and summaries and proof.get('identities') == summaries)
    need(isinstance(devices, list) and len(devices) == len(summaries) == len(held.devices))
    actual = record.get('allocation')
    need(isinstance(actual, list) and len(actual) == len(held.devices))
    from backend.onprem.runtime.current_sessions import IDENTITY_FIELDS, session_rows
    from backend.onprem.runtime.device_verification import NATIVE_DEVICE_SOURCES
    archived = record.get('result') or {}
    need(archived.get('source') in NATIVE_DEVICE_SOURCES
         and archived.get('schemaVersion') == 'cityverse.current-state/v2'
         and archived.get('generation') == record['generation'] and archived.get('errors') == [])
    archived_sessions = {r['sessionKey']: r for r in session_rows(archived)}
    need(set(archived_sessions) == {r['sessionKey'] for r in sessions})
    allocations = [a for a in snapshot.get('actions', []) if a.get('kind') == 'ue_allocation']
    need(len(allocations) == 1 and allocations[0].get('accepted') is True
         and allocations[0].get('generation') == record['generation']
         and allocations[0].get('counts') == record['desired'])
    spatial = snapshot.get('spatialDemand') or {}
    from backend.onprem.scenarios.spatial import validate_spatial_request
    requested_groups = spatial.get('requestedGroups')
    need(isinstance(requested_groups, list) and bool(requested_groups))
    validate_spatial_request(dict(snapshot['allocation'], topologyRevision=spatial.get('topologyRevision'),
        demandGroups=requested_groups))
    groups = {r['groupId']: r for r in requested_groups}
    radio = proof.get('spatialRan') or {}
    need(radio.get('source') == 'native-free-ran-ue-context+reciprocal-ngap-sctp'
         and radio.get('generation') == record['generation']
         and radio.get('topologyRevision') == spatial.get('topologyRevision')
         and type(radio.get('topologyRevision')) is int)
    bindings = radio.get('bindings')
    need(isinstance(bindings, list) and len(bindings) == len(devices)
         and spatial.get('actualBindings') == bindings)
    need(len({d.get('deviceId') for d in summaries}) == len(summaries))
    expected_devices = {d.device_id: d for d in held.devices}
    expected_reserved = {(r.supi, r.session_id): r for r in held.allocation}
    need(len(expected_reserved) == len(held.allocation) == len(sessions))
    seen = set()
    for allocation in actual:
        device = DeviceAllocation.from_dict(allocation['device'])
        need(expected_devices.get(device.device_id) == device)
        reserved = [ReservedUE(**r) for r in allocation.get('reservedSessions', [])]
        need(len(reserved) == len(device.sessions)
             and all(expected_reserved.get((r.supi, r.session_id)) == r for r in reserved))
        summaries_for_device = [d for d in summaries if d.get('deviceId') == device.device_id]
        observed_devices = [d for d in devices if d.get('deviceId') == device.device_id]
        need(len(summaries_for_device) == len(observed_devices) == 1)
        summary, observed = summaries_for_device[0], observed_devices[0]
        identity_fields = ('deviceId', 'supi', 'podUid', 'containerId', 'processEpoch', 'generation')
        need(all(summary.get(k) == observed.get(k) for k in identity_fields)
             and summary.get('supi') == device.supi)
        cohort = [s for s in sessions if s['deviceId'] == device.device_id]
        need(len(cohort) == len(reserved) == summary.get('sessionCount')
             and set(summary.get('sessionKeys', [])) == {s['sessionKey'] for s in cohort}
             and all(summary.get(k) is True for k in ('registered', 'pduEstablished', 'trafficVerified')))
        access = allocation.get('spatialAccess') or {}
        matching_radio = [r for r in bindings if r.get('supi') == device.supi]
        need(len(matching_radio) == 1 and summary.get('ranBinding') == matching_radio[0])
        bound = matching_radio[0]
        need(all(isinstance(access.get(k), str) and access[k] for k in
                 ('gnbId', 'cellId', 'groupId', 'gnbPodUid', 'endpoint')))
        demand = groups.get(access['groupId'])
        need(isinstance(demand, dict) and demand['sourceLocation'] == allocation.get('sourceLocation')
             and allocation.get('scenario') == summary.get('scenario') == demand['profileId'])
        need(all(bound.get(k) == access[v] for k, v in
                 (('gnbId', 'gnbId'), ('servingCellId', 'cellId'),
                  ('demandGroupId', 'groupId'), ('gnbPodUid', 'gnbPodUid'))))
        need(summary.get('gnbPodUid') == access['gnbPodUid']
             and summary.get('gnbSearchEndpoint') == access['endpoint']
             and summary.get('servingCellId') == access['cellId']
             and summary.get('demandGroupId') == access['groupId']
             and summary.get('sourceLocation') == allocation.get('sourceLocation')
             and isinstance(allocation.get('sourceLocation'), dict))
        for session in cohort:
            matches = [r for r in reserved if r.session_id == session.get('logicalSessionId')]
            specs = [s for s in device.sessions if s.session_id == session.get('logicalSessionId')]
            need(len(matches) == len(specs) == 1)
            reserved_session, specification = matches[0], specs[0]
            original = archived_sessions[session['sessionKey']]
            native_proof = original.get('proof') or {}
            need(all(session.get(k) == original.get(k) for k in IDENTITY_FIELDS)
                 and native_proof.get('source') in NATIVE_DEVICE_SOURCES
                 and native_proof.get('sessionKey') == session['sessionKey']
                 and all(native_proof.get(k) == session.get(k) for k in ('podUid', 'containerId', 'processEpoch'))
                 and isinstance(native_proof.get('smfBootEpoch'), str) and bool(native_proof['smfBootEpoch'])
                 and all(original.get(k) is True for k in ('registered', 'pduEstablished', 'trafficVerified'))
                 and original.get('sourceLocation') == allocation['sourceLocation']
                 and original.get('demandGroupId') == access['groupId']
                 and original.get('servingCellId') == access['cellId'])
            need(reserved_session.pdu_session_id == session['pduSessionId']
                 and all(session.get(k) == summary.get(k) for k in identity_fields)
                 and (session['dnn'], session['snssai'], session.get('service_type'), session.get('region'))
                    == (specification.dnn, specification.snssai, specification.service_type, specification.region))
            need(session['sessionKey'] not in seen)
            seen.add(session['sessionKey'])
    need(seen == {s['sessionKey'] for s in sessions})
    return True
