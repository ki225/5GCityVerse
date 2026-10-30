"""Typed single-PDU cohort adapter for the existing native scheduled shard engine.

The scheduling projection remains current-state/v2. Physical device summaries
are never interpreted as PDU identities. Every guard rejoins the original held
nonces and current full native device/session census before delegating execution.
"""
from copy import deepcopy
import hashlib
import json

from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS as SESSION_FIELDS, session_rows
from backend.onprem.runtime.device_sessions import DeviceAllocation
from backend.onprem.runtime.ue import EvidenceVerifier, seconds, timestamp
from backend.onprem.runtime.ue_reservation import HeldUEAllocation, ReservedUE
from backend.onprem.runtime.errors import RoundRuntimeBlocked
from .batch import NativeRoundBatch


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def wire_binding(row, *, preserve_upf=False):
    """Canonical existing worker text encoding, without changing the native V2 row."""
    if type(row.get('sessionId')) is not int or not 1 <= row['sessionId'] <= 15:
        raise RoundRuntimeBlocked('Typed worker binding requires native integer PDU ID')
    sd = row.get('sd')
    if type(sd) is int and 0 <= sd <= 0xffffff:sd = f'{sd:06x}'
    import re
    if type(sd) is not str or not re.fullmatch('[0-9a-f]{6}', sd):
        raise RoundRuntimeBlocked('Typed worker binding requires canonical native SD')
    seid = row.get('pfcpSeid')
    if type(seid) is int and 1 <= seid <= 2**64-1:seid = str(seid)
    if type(seid) is not str or not re.fullmatch('[1-9][0-9]*', seid) or int(seid) > 2**64-1:
        raise RoundRuntimeBlocked('Typed worker binding requires canonical native PFCP SEID')
    upf=row.get('upf')
    targets={'embb':'upf-embb','urllc':'upf-urllc','mmtc':'upf-mmtc','v2x':'upf-v2x'}
    if not preserve_upf and upf==row.get('slice') and upf in targets:
        # Native V2 stores the configured UPF target role; the legacy worker
        # contract spells the same fixed role as its deployment alias. Encode
        # a copy only after full typed/native validation, never rewrite evidence.
        from uuid import UUID
        from backend.onprem.runtime.current_sessions import canonical_session_key
        key=canonical_session_key(row.get('sessionKey'))
        proof=row.get('proof') or {}
        try:valid_id=str(UUID(row.get('nativeUpfId')))==row['nativeUpfId']
        except (ValueError,TypeError,AttributeError):valid_id=False
        if (not valid_id or key.supi!=row.get('supi') or key.pdu_session_id!=row.get('pduSessionId')
                or key.established_epoch!=row.get('smContextRef')
                or proof.get('source')!='native-free-ran-ue-single-session/v2'
                or any(proof.get(k)!=row.get(k) or not row.get(k) for k in
                    ('sessionKey','podUid','containerId','processEpoch','upfEpoch'))
                or any(proof.get(k) is not True for k in
                    ('amf_registered','smf_active','pfcp_kernel_verified','tun_present','connectivity_verified'))):
            raise RoundRuntimeBlocked('Typed UPF target wire encoding requires exact native PDU proof')
        upf=targets[upf]
    return dict(row, sessionId=str(row['sessionId']), pfcpSeid=seid, sd=sd,upf=upf)


def typed_sampling_view(snapshot, generation, held, *, observed=None):
    """Bind an internal scheduling view to the complete exact typed allocation."""
    def need(condition):
        if not condition:
            raise RoundRuntimeBlocked('V2 batch requires complete held device/nonce/native PDU ownership')
    need(snapshot.get('schemaVersion') == V2 and type(held) is HeldUEAllocation and bool(held.devices))
    need(isinstance(generation, dict) and generation.get('generation') == snapshot.get('generation')
         and generation.get('desired') == dict(held.counts) and held.request_id == snapshot.get('requestId'))
    owners = session_rows(snapshot)
    need(1 <= len(owners) <= 100 and len(snapshot['devices']) == len(owners) == len(held.devices)
         and len(held.allocation) == len(owners) and snapshot.get('allocation', {}).get('total') == len(owners))
    original_devices = {d.device_id: d for d in held.devices}
    reserved = {(r.supi, r.session_id): r for r in held.allocation}
    allocations = generation.get('allocation')
    need(isinstance(allocations, list) and len(allocations) == len(owners))
    if held.runtime_policy_digest:
        need(all(a.get('runtimePolicyDigest') == held.runtime_policy_digest for a in allocations))
    original = {}
    for allocation in allocations:
        device = DeviceAllocation.from_dict(allocation['device'])
        need(original_devices.get(device.device_id) == device and len(device.sessions) == 1
             and device.device_id not in original)
        rows = [ReservedUE(**r) for r in allocation.get('reservedSessions', ())]
        need(len(rows) == 1 and reserved.get((rows[0].supi, rows[0].session_id)) == rows[0]
             and rows[0].supi == device.supi and rows[0].session_id == device.sessions[0].session_id)
        original[device.device_id] = (device, rows[0], allocation)
    need(set(original) == set(original_devices))
    for owner in owners:
        device, reservation, allocation = original[owner['deviceId']]
        spec = device.sessions[0]
        need(owner['supi'] == reservation.supi and owner['pduSessionId'] == reservation.pdu_session_id
             and owner.get('logicalSessionId') == reservation.session_id and owner['dnn'] == spec.dnn
             and owner['snssai'] == spec.snssai and owner.get('service_type') == spec.service_type
             and owner.get('region') == spec.region)
        access = allocation.get('spatialAccess') or {}
        radio = owner.get('proof', {}).get('spatialRan') or {}
        need(bool(access) and all(radio.get(k) == access.get(v) for k,v in (
            ('gnbId', 'gnbId'), ('servingCellId', 'cellId'), ('gnbPodUid', 'gnbPodUid'),
            ('gnbSearchEndpoint', 'endpoint')))
             and owner.get('sourceLocation') == allocation.get('sourceLocation')
             and owner.get('demandGroupId') == allocation.get('demandGroupId'))
    if observed is not None:
        need(observed.get('schemaVersion') == V2 and observed.get('generation') == snapshot['generation']
             and observed.get('populationRevision', 0) == snapshot.get('populationRevision', 0)
             and observed.get('status') == 'established' and not observed.get('errors'))
        live = {r['sessionKey']: r for r in session_rows(observed)}
        need(len(live) == len(owners) and set(live) == {r['sessionKey'] for r in owners})
        for owner in owners:
            row = live[owner['sessionKey']]
            need(all(row.get(k) == owner.get(k) for k in SESSION_FIELDS + ('pfcpSeid',))
                 and all(row.get('proof', {}).get(k) is True for k in
                         ('amf_registered', 'smf_active', 'pfcp_kernel_verified', 'tun_present', 'connectivity_verified'))
                 and row.get('proof', {}).get('spatialRan', {}).get('gnbPodUid')
                     == owner.get('proof', {}).get('spatialRan', {}).get('gnbPodUid'))
    proof = dict(schema='cityverse.typed-measurement-cohort/v1', bindingSchema=V2,
        roundId=snapshot['roundId'], generation=snapshot['generation'],
        populationRevision=snapshot.get('populationRevision', 0), requestId=held.request_id,
        deviceCount=len(owners), sessionCount=len(owners),
        heldAllocationRef=_digest(held.to_dict()), allocationDigest=_digest(allocations),
        sessionDigest=_digest([{k:r.get(k) for k in SESSION_FIELDS + ('pfcpSeid',)}
                              for r in sorted(owners, key=lambda r:r['sessionKey'])]))
    proof['cohortDigest'] = _digest(proof)
    view = deepcopy(snapshot)
    view['perUe'] = [dict(row, interface=row['tun'], ip=row['ueIp']) for row in owners]
    view['typedCohortProof'] = proof
    return view


def native_session_bindings(snapshot, generation, held, observed):
    """Public bounded projection only after full held/nonces and closing native fences."""
    if not isinstance(observed, dict):
        raise RoundRuntimeBlocked('V2 native binding projection requires current native census')
    view = typed_sampling_view(snapshot, generation, held, observed=observed)
    fence = observed.get('observationFence') or {}
    if (fence.get('source') != 'native-free-ran-ue-cohort-fence/v1'
            or fence.get('generation') != snapshot['generation']
            or fence.get('allocationDigest') != _digest(generation['allocation'])
            or fence.get('deviceCount') != len(view['perUe']) or fence.get('sessionCount') != len(view['perUe'])
            or not fence.get('openingCensusDigest')
            or fence.get('openingCensusDigest') != fence.get('closingCensusDigest')):
        raise RoundRuntimeBlocked('V2 native binding projection requires complete closing census fence')
    at = seconds(observed.get('observedAt'))
    import math
    import re
    opened, closed = fence.get('openedAt'), fence.get('closedAt')
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (opened, closed))
            or not at - 15 <= opened <= closed <= at + 1
            or not re.fullmatch('[0-9a-f]{64}', fence['openingCensusDigest'])):
        raise RoundRuntimeBlocked('V2 native binding projection has invalid original census clocks')
    from backend.onprem.runtime.round_resources import fresh
    if not fresh(observed.get('observedAt')):
        raise RoundRuntimeBlocked('V2 native binding projection has stale native source')
    nonce = {(r.supi, r.pdu_session_id): _digest(r.nonce) for r in held.allocation}
    owners = {r['sessionKey']: r for r in view['perUe']}
    bindings = []
    for row in sorted(session_rows(observed), key=lambda r:r['sessionKey']):
        bindings.append({k:deepcopy(row[k]) for k in SESSION_FIELDS + ('sst', 'sd', 'pfcpSeid', 'proof') if k in row}
                        | {k:deepcopy(owners[row['sessionKey']][k]) for k in
                           ('scenario', 'logicalSessionId', 'service_type', 'region', 'sourceLocation', 'demandGroupId', 'servingCellId')
                           if k in owners[row['sessionKey']]}
                        | {'nonceDigest': nonce[(row['supi'], row['pduSessionId'])]})
    return dict(schemaVersion='cityverse.native-session-bindings/v1',
        source='native-free-ran-ue-typed-cohort/v1', roundId=snapshot['roundId'],
        generation=snapshot['generation'], populationRevision=snapshot.get('populationRevision', 0),
        observedAt=observed['observedAt'], validUntil=timestamp(at + 15),
        openedAt=fence['openedAt'], closedAt=fence['closedAt'],
        openingCensusDigest=fence['openingCensusDigest'], closingCensusDigest=fence['closingCensusDigest'],
        deviceCount=len(bindings), sessionCount=len(bindings),
        cohortDigest=view['typedCohortProof']['cohortDigest'],
        allocationDigest=view['typedCohortProof']['allocationDigest'],
        heldAllocationRef=view['typedCohortProof']['heldAllocationRef'],
        bindingsDigest=_digest(bindings), bindings=bindings)


class V2NativeRoundBatch(NativeRoundBatch):
    """Same scheduled barrier/interval engine, with strict V2 binding guards."""
    typed_enabled = True
    # Detached transport retains its existing two-or-more shard contract.
    # Ten typed devices therefore use two five-device shards, one common start.
    minimum_shards = 2
    minimum_shard_population = 10

    def sampling_view(self, snapshot, *, observed=None):
        generation = self.resources.current_generation()
        held = self.ledger.intent_traffic_held(snapshot['roundId'])
        return typed_sampling_view(snapshot, generation, held, observed=observed)

    def _bindings(self, snapshot):
        if snapshot.get('schemaVersion') != V2:
            return super()._bindings(snapshot)
        generation = self.resources.current_generation()
        observed = self.resources.controller.adapter.observe(snapshot['generation'])
        status = EvidenceVerifier(stable_seconds=0).evaluate(generation, deepcopy(observed))
        self._require_snapshot(snapshot, status)
        view = self.sampling_view(snapshot, observed=status)
        pods = self.resources._pods()
        result = []
        for row in view['perUe']:
            candidates = [p for p in pods if p.get('metadata', {}).get('uid') == row['podUid']]
            if len(candidates) != 1:
                raise RoundRuntimeBlocked('V2 batch requires exact current UE Pod UID')
            receiver, endpoint, route = self.resources.service_receiver(snapshot, row, pods)
            result.append(dict(wire_binding(row,preserve_upf=bool(route)), pod=candidates[0]['metadata']['name'],
                receiverPod=receiver['metadata']['name'], receiverUid=receiver['metadata']['uid'],
                **({'serviceRoute':route} if route else {})))
        self._require_snapshot(snapshot, status)
        if self.sampling_view(snapshot)['typedCohortProof'] != view['typedCohortProof']:
            raise RoundRuntimeBlocked('V2 batch typed allocation changed during binding reads')
        scoped=getattr(self.resources,'scoped_capacity',None)
        if scoped is not None and snapshot.get('_scopedCapacityWindowOrigin') is not None:
            snapshot['_scopedOpeningCaptureDescriptor']=scoped.record(snapshot,'opening-native',dict(
                generation=generation,held=self.ledger.intent_traffic_held(snapshot['roundId']).to_dict(),native=status))
        return result

    def _guard_from_observation(self, jobs, snapshot, key, status, *, shard, diagnostic=None):
        if snapshot.get('schemaVersion') != V2:
            return super()._guard_from_observation(jobs, snapshot, key, status, shard=shard, diagnostic=diagnostic)
        try:
            view = self.sampling_view(snapshot, observed=status)
            if snapshot.get('typedCohortProof') != view['typedCohortProof']:
                raise RoundRuntimeBlocked('V2 batch cohort seal differs')
            native = deepcopy(status)
            routed={job.supi for job in jobs if job.route_binding_id}
            native['perUe'] = [dict(wire_binding(r,preserve_upf=r['supi'] in routed), interface=r['tun'], ip=r['ueIp'])
                               for r in session_rows(status)]
            return super()._guard_from_observation(jobs, view, key, native, shard=shard, diagnostic=diagnostic)
        except Exception:
            if diagnostic is not None:diagnostic.update(failedConjuncts=['typed_device_nonce_or_native_session'])
            return False

    def _sample_shards(self, snapshot, reserve_shard, cancelled):
        if snapshot.get('schemaVersion') != V2:
            return super()._sample_shards(snapshot, reserve_shard, cancelled)
        view = self.sampling_view(snapshot)
        result = super()._sample_shards(view, reserve_shard, cancelled)
        key = self._native_guard_scope(view)
        generation = self.resources.current_generation()
        if key is None or generation is None:
            raise RoundRuntimeBlocked('V2 batch owner changed before final native closing observation')
        status = self._native_guard_observation(key, generation, view, refresh=True)
        result['nativeSessionBindings'] = native_session_bindings(view, generation,
            self.ledger.intent_traffic_held(view['roundId']), status)
        from backend.onprem.evidence.native_binding_publication import NativeBindingPublication
        current=self.ledger.native_publication_scope()
        result['_nativeBindingPublication']=NativeBindingPublication.create(current,generation,
            self.ledger.intent_traffic_held(view['roundId']),status)
        scoped = getattr(self.resources, 'scoped_capacity', None)
        if scoped is not None and view.get('_scopedCapacityWindowOrigin') is not None:
            result['_scopedOpeningCaptureDescriptor']=view.get('_scopedOpeningCaptureDescriptor')
            result['_scopedNativeCaptureDescriptor'] = scoped.record(view, 'closing-native', dict(
                generation=generation,held=self.ledger.intent_traffic_held(view['roundId']).to_dict(),native=status))
        return result
