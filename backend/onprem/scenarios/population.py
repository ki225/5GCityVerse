"""Trusted, current-only population transactions over the existing UE writer.

Native admission and policy ports are mandatory. Reserving a delta never grants
permission to sample new sessions; only a native proof and inherited policies do.
An interrupted external phase is read back by its owner, never blindly replayed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import time

from backend.onprem.scenarios.contracts import RoundConflict
from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease, read_lease, lease_identity, lease_renewal_follows
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows
from backend.onprem.storage import live_state


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {'expected_population_revision', 'add_counts', 'remove_device_ids'}:
        raise ValueError('Exact population delta fields required')
    from backend.onprem.runtime.ue import validate_counts
    counts = validate_counts(value['add_counts'])
    revision, removes = value['expected_population_revision'], value['remove_device_ids']
    if (type(revision) is not int or revision < 0 or not isinstance(removes, list)
            or len(removes) > 100 or any(not isinstance(d, str) or not 0 < len(d) <= 256 for d in removes)
            or len(set(removes)) != len(removes) or not (any(counts.values()) or removes)):
        raise ValueError('Invalid population delta')
    return dict(expected_population_revision=revision, add_counts=counts, remove_device_ids=sorted(removes))


def _install(db):
    db.execute('CREATE TABLE IF NOT EXISTS scenario_population_operations '
        '(operation_id TEXT PRIMARY KEY, round_id TEXT NOT NULL, owner_id TEXT NOT NULL, '
        'fingerprint TEXT NOT NULL, record TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS scenario_population_round ON scenario_population_operations(round_id)')


def _authority(db, snapshot, lease, record=None):
    entry = read_lease(db, snapshot.get('roundId'))
    current = ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
    previous = ServiceIntentTrafficLease.from_dict(record.get('authority_lease', record['lease'])) if record else lease
    if (type(lease) is not ServiceIntentTrafficLease or current is None
            or not lease_renewal_follows(lease, current) or not lease_renewal_follows(previous, current)
            or entry['state'] != 'active' or not current.active(time.time())
            or snapshot.get('requestId') != lease.request_id or snapshot.get('schemaVersion') != V2
            or snapshot.get('stage') in {'cleaning', 'completed'}):
        raise RoundConflict('Population does not own an active formal v2 fleet')
    return current


def _current(db, lease):
    snapshot = live_state.read(db)
    _authority(db, snapshot, lease)
    session_rows(snapshot)
    return snapshot


def reserve(ledger, lease, operation_id, request, idempotency_key):
    request = validate_request(request)
    if any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in (operation_id, idempotency_key)):
        raise ValueError('Exact population operation and idempotency key required')
    fingerprint = hashlib.sha256(_encoded([lease_identity(lease), request, idempotency_key]).encode()).hexdigest()
    with ledger.connection() as db:
        _install(db)
        snapshot = _current(db, lease)
        old = db.execute('SELECT fingerprint,record FROM scenario_population_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise RoundConflict('Population operation input changed')
            record = json.loads(old[1])
            record['authority_lease'] = _authority(db, snapshot, lease, record).to_dict()
            db.execute('UPDATE scenario_population_operations SET record=? WHERE operation_id=?', (_encoded(record), operation_id))
            return record
        pending = snapshot.get('populationPending')
        if pending or snapshot.get('populationRevision', 0) != request['expected_population_revision']:
            raise RoundConflict('Population revision is superseded or busy')
        device_ids = {r['deviceId'] for r in snapshot['devices']}
        if not set(request['remove_device_ids']) <= device_ids:
            raise RoundConflict('Removal is outside this owned fleet')
        record = dict(operation_id=operation_id, round_id=snapshot['roundId'], owner_id=lease.owner_id,
            generation=snapshot['generation'], lease=lease.to_dict(), request=request,
            idempotency_key=idempotency_key, phase='reserved', receipts={}, delta=None, pending=True,
            reservation_dispatch_started=False)
        db.execute('INSERT INTO scenario_population_operations VALUES(?,?,?,?,?)',
            (operation_id, snapshot['roundId'], lease.owner_id, fingerprint, _encoded(record)))
        snapshot['populationPending'] = dict(operationId=operation_id, baseRevision=request['expected_population_revision'])
        snapshot.setdefault('populationRevision', 0)
        snapshot['sequence'] += 1
        db.execute('UPDATE rounds SET snapshot=? WHERE round_id=?', (_encoded(snapshot), snapshot['roundId']))
        return record


def read(ledger, operation_id, owner):
    with ledger._read_connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scenario_population_operations'").fetchone():
            return None
        row = db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND owner_id=?', (operation_id, owner)).fetchone()
        return json.loads(row[0]) if row else None


def completed_publication(ledger,lease,operation_id,request,idempotency_key):
    """Exact completed acknowledgement is readback, never new launch authority.

    The original immutable lease/round/cohort must still be current. Its timer
    or restoring state cannot make an already committed publication unknowable.
    """
    request=validate_request(request)
    if any(not isinstance(v,str) or not 0<len(v)<=256 for v in (operation_id,idempotency_key)):
        raise ValueError('Exact population operation and idempotency key required')
    fingerprint=hashlib.sha256(_encoded([lease_identity(lease),request,idempotency_key]).encode()).hexdigest()
    with ledger._read_connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scenario_population_operations'").fetchone():return None
        row=db.execute('SELECT round_id,owner_id,fingerprint,record FROM scenario_population_operations WHERE operation_id=?',
            (operation_id,)).fetchone()
        if row is None:return None
        if row[1]!=lease.owner_id or row[2]!=fingerprint:
            raise RoundConflict('Population operation input changed')
        record=json.loads(row[3])
        if record.get('phase')!='completed' or record.get('pending') is not False:return None
        snapshot=live_state.read(db)
        entry=read_lease(db,snapshot.get('roundId'))
        current=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
        original=ServiceIntentTrafficLease.from_dict(record['lease'])
        watermark=ServiceIntentTrafficLease.from_dict(record.get('authority_lease',record['lease']))
        delta=record.get('delta',{})
        if (current is None or not lease_renewal_follows(original,current) or not lease_renewal_follows(lease,current)
                or not lease_renewal_follows(watermark,current)
                or snapshot.get('roundId')!=row[0] or snapshot.get('requestId')!=lease.request_id
                or snapshot.get('schemaVersion')!=V2 or snapshot.get('generation')!=record.get('generation')
                or record.get('owner_id')!=lease.owner_id or record.get('operation_id')!=operation_id
                or snapshot.get('populationRevision')!=delta.get('next_revision')
                or snapshot.get('populationPending')):
            raise RoundConflict('Completed population publication lineage changed')
        return record


def checkpoint(ledger, lease, operation_id, expected_phase, phase, *, delta=None, receipt=None, exclude=None):
    with ledger.connection() as db:
        snapshot = _current(db, lease)
        row = db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND owner_id=?', (operation_id, lease.owner_id)).fetchone()
        if row is None:
            raise RoundConflict('Population operation unavailable')
        record = json.loads(row[0])
        record['authority_lease'] = _authority(db, snapshot, lease, record).to_dict()
        if (record['phase'] != expected_phase or snapshot.get('populationPending', {}).get('operationId') != operation_id
                or record['generation'] != snapshot['generation']):
            raise RoundConflict('Population checkpoint superseded')
        record['phase'] = phase
        if delta is not None:
            record['delta'] = deepcopy(delta)
        if receipt is not None:
            record['receipts'][phase] = deepcopy(receipt)
        if exclude is not None:
            snapshot['populationSamplingExcluded'] = sorted(exclude)
            snapshot['sequence'] += 1
            db.execute('UPDATE rounds SET snapshot=? WHERE round_id=?', (_encoded(snapshot), snapshot['roundId']))
        db.execute('UPDATE scenario_population_operations SET record=? WHERE operation_id=?', (_encoded(record), operation_id))
        return record


def reservation_dispatch(ledger,lease,operation_id):
    with ledger.connection() as db:
        snapshot=_current(db,lease)
        row=db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND owner_id=?',
            (operation_id,lease.owner_id)).fetchone()
        record=json.loads(row[0]) if row else None
        if (not record or record['phase']!='reserved' or record['delta'] is not None
                or snapshot.get('populationPending',{}).get('operationId')!=operation_id):
            raise RoundConflict('Population reservation dispatch superseded')
        record['reservation_dispatch_started']=True
        db.execute('UPDATE scenario_population_operations SET record=? WHERE operation_id=?',(_encoded(record),operation_id))


def commit(ledger, lease, operation_id, native):
    from backend.onprem.runtime.population import PopulationReadback, HeldPopulationDelta
    if type(native) is not PopulationReadback or native.ready is not True or native.removed_native_absent is not True:
        raise RoundConflict('Complete native population barrier required')
    target = native.current_snapshot
    target_rows = session_rows(target)
    from backend.onprem.runtime.round_resources import fresh
    if target.get('schemaVersion') != V2 or not fresh(target.get('observedAt')):
        raise RoundConflict('Fresh native v2 cohort required')
    with ledger.connection() as db:
        snapshot = _current(db, lease)
        record = json.loads(db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND owner_id=?', (operation_id, lease.owner_id)).fetchone()[0])
        record['authority_lease'] = _authority(db, snapshot, lease, record).to_dict()
        delta = HeldPopulationDelta.from_dict(record['delta'])
        if (record['phase'] != 'native_ready' or snapshot.get('populationPending', {}).get('operationId') != operation_id
                or native.generation != snapshot['generation'] or target.get('generation') != native.generation
                or native.population_revision != delta.next_revision
                or snapshot.get('populationRevision', 0) != delta.base_revision):
            raise RoundConflict('Population commit fence changed')
        before = {r['sessionKey']: r for r in session_rows(snapshot)}
        after = {r['sessionKey']: r for r in target_rows}
        removed = {k for k, r in before.items() if r['deviceId'] in delta.removed_device_ids}
        retained = set(before) - removed
        expected_added = {d.device_id for d in delta.added_devices}
        if (not retained <= set(after) or removed.intersection(after)
                or {r['deviceId'] for k, r in after.items() if k not in retained} != expected_added
                or any(any(before[k].get(f) != after[k].get(f) for f in IDENTITY_FIELDS) for k in retained)):
            raise RoundConflict('Population commit changed surviving native identity')
        if expected_added and record['receipts'].get('inherited', {}).get('session_keys') != sorted(set(after) - retained):
            raise RoundConflict('New native sessions lack exact policy inheritance')
        if any(a.get('kind') == 'traffic_window' and a.get('cleanupVerified') is not True
               for a in snapshot.get('actions', ())):
            raise RoundConflict('Population publication requires drained owned windows')
        # Only native current data crosses the publication boundary. No old
        # receiver window is evidence for the new population revision.
        for field in ('devices', 'perSession', 'perUe', 'counts', 'observedAt','spatialRan'):
            if field in target:
                snapshot[field] = deepcopy(target[field])
        if snapshot.get('spatialDemand'):
            spatial_proof=deepcopy(target.get('spatialRan',{}))
            if target_rows and (spatial_proof.get('source') not in {'native-ueransim-single-commissioned-gnb/v1','native-free-ran-ue-single-session/v2'}
                    or spatial_proof.get('generation')!=native.generation
                    or {r.get('supi') for r in spatial_proof.get('bindings',())}!={r['supi'] for r in target_rows}
                    or any(r.get('pdr_far_gnb_verified') is not True for r in spatial_proof.get('bindings',()))):
                raise RoundConflict('Fresh same-vendor geographic native attachment required for population publication')
            if not target_rows:
                spatial_proof=dict(source=(snapshot.get('spatialRan') or {}).get('source','native-free-ran-ue-single-session/v2'),generation=native.generation,
                    observedAt=target.get('observedAt'),bindings=[])
            spatial_proof['roundId']=snapshot['roundId']
            snapshot['spatialRan']=spatial_proof
            snapshot.setdefault('connectivityEvidence',{})['spatialRan']=deepcopy(spatial_proof)
            snapshot['spatialDemand']['actualBindings']=deepcopy(spatial_proof['bindings'])
        snapshot.update(populationRevision=delta.next_revision, measurements=[], observedAt=None)
        snapshot.pop('nativeEmptyPopulation', None)
        marker = target.get('nativeEmptyPopulation')
        if not target_rows and marker is not None:
            if (marker.get('source') != 'native-owned-empty-population/v1'
                    or marker.get('generation') != native.generation
                    or marker.get('populationRevision') != delta.next_revision
                    or not fresh(marker.get('observedAt'))):
                raise RoundConflict('Native empty population proof changed')
            snapshot['nativeEmptyPopulation'] = dict(deepcopy(marker), operationId=operation_id,
                deltaId=delta.delta_id, deltaVersion=delta.version)
        snapshot.pop('populationPending', None)
        snapshot.pop('populationSamplingExcluded', None)
        db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_window_archive '
            '(round_id TEXT,action_id TEXT,receipt TEXT,PRIMARY KEY(round_id,action_id))')
        for owned in snapshot.get('actions', ()):
            if owned.get('kind') in {'traffic_window', 'traffic_aggregate', 'traffic_failure'}:
                db.execute('INSERT OR IGNORE INTO intent_traffic_window_archive VALUES(?,?,?)',
                    (snapshot['roundId'], owned['id'], _encoded(owned)))
                from backend.onprem.evidence import batch_archive
                batch_archive.transfer(db, snapshot, owned)
        snapshot['actions'] = [a for a in snapshot.get('actions', ()) if a.get('kind') not in {'traffic_window', 'traffic_aggregate', 'traffic_failure'}]
        snapshot['sequence'] += 1
        record.update(phase='completed', pending=False, population_revision=delta.next_revision)
        db.execute('UPDATE rounds SET snapshot=? WHERE round_id=?', (_encoded(snapshot), snapshot['roundId']))
        db.execute('UPDATE scenario_population_operations SET record=? WHERE operation_id=?', (_encoded(record), operation_id))
        return record


async def _call(callback, *args, **kwargs):
    value = callback(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def current_authority(ledger, lease, operation_id):
    """Persist the observed authority high-water mark before external work."""
    with ledger.connection() as db:
        snapshot = _current(db, lease)
        row = db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND owner_id=?',
            (operation_id, lease.owner_id)).fetchone()
        if row is None:
            raise RoundConflict('Population operation unavailable')
        record = json.loads(row[0])
        current = _authority(db, snapshot, lease, record)
        record['authority_lease'] = current.to_dict()
        db.execute('UPDATE scenario_population_operations SET record=? WHERE operation_id=?', (_encoded(record), operation_id))
        return current


def _addition_barrier(old, native, delta):
    from backend.onprem.runtime.round_resources import fresh
    if (not isinstance(native, dict) or native.get('schemaVersion') != V2
            or native.get('generation') != delta.generation or not fresh(native.get('observedAt'))):
        raise RoundConflict('Fresh addition native proof required before policy inheritance')
    before = {r['sessionKey']: r for r in session_rows(old)}
    after = {r['sessionKey']: r for r in session_rows(native)}
    if (not set(before) <= set(after) or any(any(before[k].get(f) != after[k].get(f)
            for f in IDENTITY_FIELDS) for k in before)):
        raise RoundConflict('Surviving native path changed before inheritance')
    added = [r for k, r in after.items() if k not in before]
    expected = {(r.supi, r.pdu_session_id) for r in delta.reserved_sessions}
    if ({(r['supi'], r['pduSessionId']) for r in added} != expected
            or {r['deviceId'] for r in added} != {d.device_id for d in delta.added_devices}):
        raise RoundConflict('Added native sessions differ from exact reservation')
    return sorted(r['sessionKey'] for r in added)


class ScenarioPopulationAdapter:
    """Private resumable orchestration; no API payload can supply native ports."""
    def __init__(self, coordinator, *, profile_reader=None, capacity_admission=None,
                 install_pending_admissions=None, inherit_member_policies=None,
                 withdraw_member_policies=None, commissioned=False):
        self.coordinator, self.ledger = coordinator, coordinator.ledger
        self.controller = coordinator.resources.controller
        self.profile_reader, self.capacity_admission = profile_reader, capacity_admission
        self.install_pending_admissions = install_pending_admissions
        self.inherit_member_policies, self.withdraw_member_policies = inherit_member_policies, withdraw_member_policies
        self.configured = (all(callable(p) for p in (profile_reader, capacity_admission, install_pending_admissions,
            inherit_member_policies, withdraw_member_policies)) and all(callable(getattr(self.controller, name, None))
                for name in ('reserve_population', 'consume_population', 'retire_population', 'population_readback',
                             'quiesce_population', 'confirm_population_stopped')))
        self.commissioned = commissioned is True and self.configured
        self._lock = asyncio.Lock()
        self._delta = None
        self._native = None
        coordinator.population_refresh = self.refresh
        coordinator.resources.population_status_reader = self.status

    @property
    def capabilities(self):
        return dict(fleet_join=self.commissioned, fleet_leave=self.commissioned,
            configured=self.configured, reason='native_v2_population_not_commissioned' if not self.commissioned else None)

    def readback(self, operation_id, owner):
        return read(self.ledger, operation_id, owner)

    def status(self):
        return deepcopy(self._native)

    def current_empty_proof(self, raw):
        """Read only the exact current committed delta; never an old window.

        Cleanup may inspect its own expired/restoring lease. This grants no
        native mutation authority and cannot revive a population command.
        """
        from backend.onprem.runtime.population import HeldPopulationDelta
        from backend.onprem.runtime.round_resources import fresh
        def joined():
            with self.ledger.connection() as db:
                current=live_state.read(db)
                marker=current.get('nativeEmptyPopulation',{})
                if (current.get('schemaVersion')!=V2 or session_rows(current)
                        or any(current.get(k)!=raw.get(k) for k in ('roundId','generation','populationRevision'))
                        or not marker.get('operationId')):
                    raise RoundConflict('Current owned empty population unavailable')
                row=db.execute('SELECT record FROM scenario_population_operations WHERE operation_id=? AND round_id=?',
                    (marker['operationId'],current['roundId'])).fetchone()
                record=json.loads(row[0]) if row else None
                entry=read_lease(db,current['roundId'])
                lease=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
                original=ServiceIntentTrafficLease.from_dict(record['lease']) if record else None
                floor=ServiceIntentTrafficLease.from_dict(record.get('authority_lease',record['lease'])) if record else None
                delta=HeldPopulationDelta.from_dict(record['delta']) if record and record.get('delta') else None
                if (not record or record.get('phase')!='completed' or record.get('pending') is not False
                        or lease is None or not lease_renewal_follows(original,lease) or not lease_renewal_follows(floor,lease)
                        or current.get('requestId')!=lease.request_id or delta is None
                        or delta.delta_id!=marker.get('deltaId') or delta.version!=marker.get('deltaVersion')
                        or delta.generation!=current['generation'] or delta.next_revision!=current.get('populationRevision')):
                    raise RoundConflict('Current empty population ownership changed')
                return current,delta,lease
        before,delta,lease=joined()
        reader=getattr(self.controller,'population_empty_readback',None)
        if not callable(reader): raise RoundConflict('Native empty population reader not configured')
        proof=reader(delta)
        snapshot=proof.current_snapshot
        marker=snapshot.get('nativeEmptyPopulation',{}) if isinstance(snapshot,dict) else {}
        after,after_delta,after_lease=joined()
        if (after_delta!=delta or not lease_renewal_follows(lease,after_lease)
                or proof.ready is not True or proof.removed_native_absent is not True
                or session_rows(snapshot) or snapshot.get('generation')!=delta.generation
                or snapshot.get('populationRevision')!=delta.next_revision
                or marker.get('source')!='native-owned-empty-population/v1'
                or marker.get('generation')!=delta.generation or marker.get('populationRevision')!=delta.next_revision
                or not fresh(marker.get('observedAt'))):
            raise RoundConflict('Fresh native empty population proof unavailable')
        return dict(marker,roundId=before['roundId'],operationId=before['nativeEmptyPopulation']['operationId'],
            deltaId=delta.delta_id,deltaVersion=delta.version)

    async def refresh(self):
        if self._delta is None:
            # Restart recovery reads exactly the current owner/operation; it
            # never discovers population authority by scanning old commands.
            from backend.onprem.storage.current_state import current_reader
            current = await asyncio.to_thread(current_reader(self.ledger))
            pending = current.get('populationPending')
            if pending:
                entry = await asyncio.to_thread(self.coordinator._intent_lease)
                if entry is None:
                    raise RoundConflict('Pending population traffic lease missing')
                record = await asyncio.to_thread(read, self.ledger, pending['operationId'], entry['lease']['owner_id'])
                if record is None or record['delta'] is None:
                    return None
                from backend.onprem.runtime.population import HeldPopulationDelta
                self._delta = HeldPopulationDelta.from_dict(record['delta'])
        if self._delta is not None:
            proof = await self.controller.population_readback(self._delta)
            self._native = deepcopy(proof.committed_snapshot)
        return self.status()

    async def _phase(self, record, name, callback, flag, *, intent, lease, delta, **kwargs):
        phase = record['phase']
        pending = name + '_pending'
        recover = phase == pending
        if not recover:
            record = await asyncio.to_thread(checkpoint, self.ledger, lease, record['operation_id'], phase, pending)
        lease = await asyncio.to_thread(current_authority, self.ledger, lease, record['operation_id'])
        receipt = await _call(callback, intent=intent, lease=lease, delta=delta,
            operation_id=record['operation_id'], recover=recover, **kwargs)
        if (not isinstance(receipt, dict) or receipt.get(flag) is not True
                or receipt.get('delta_id') != delta.delta_id or receipt.get('delta_version') != delta.version
                or receipt.get('operation_id') != record['operation_id']):
            raise RoundConflict('Authoritative population phase proof unavailable: ' + name)
        if len(_encoded(receipt).encode()) > 256 * 1024:
            raise RoundConflict('Population phase receipt exceeds current-state bound')
        if 'session_keys' in kwargs and receipt.get('session_keys') != sorted(kwargs['session_keys']):
            raise RoundConflict('Population policy proof membership differs')
        return await asyncio.to_thread(checkpoint, self.ledger, lease, record['operation_id'], pending, name, receipt=receipt)

    async def change(self, intent, owner, operation_id, request, *, idempotency_key):
        if not self.configured:
            raise RoundConflict('Population native capacity/policy/admission ports not configured')
        async with self._lock:
            entry = await asyncio.to_thread(self.coordinator._intent_lease)
            lease = ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
            if (lease is None or lease.owner_id != owner or lease.intent_id != intent.intent_id
                    or type(intent.revision) is not int or intent.revision > lease.intent_revision):
                raise RoundConflict('Population owner or intent revision changed')
            completed=await asyncio.to_thread(completed_publication,self.ledger,lease,operation_id,request,idempotency_key)
            if completed is not None:
                confirm=getattr(self,'confirm_population_publication',None)
                if callable(confirm):
                    from backend.onprem.runtime.population import HeldPopulationDelta
                    await _call(confirm,intent=intent,lease=lease,
                        delta=HeldPopulationDelta.from_dict(completed['delta']),operation_id=operation_id)
                self._delta = self._native = None
                return completed
            dispatch=getattr(self,'assert_population_dispatch',None)
            if callable(dispatch):await _call(dispatch,lease,operation_id)
            if intent.revision != lease.intent_revision:
                # A durable operation may resume its original intent snapshot
                # after trusted renewal. A stale snapshot cannot create a new
                # population command; current authority still owns every phase.
                prior = await asyncio.to_thread(read, self.ledger, operation_id, owner)
                original = ServiceIntentTrafficLease.from_dict(prior['lease']) if prior else None
                if (original is None or intent.revision < original.intent_revision
                        or not lease_renewal_follows(original, lease)):
                    raise RoundConflict('Population stale intent has no current operation authority')
            record = await asyncio.to_thread(reserve, self.ledger, lease, operation_id, request, idempotency_key)
            if not record['pending']:
                confirm=getattr(self,'confirm_population_publication',None)
                if callable(confirm):
                    from backend.onprem.runtime.population import HeldPopulationDelta
                    await _call(confirm,intent=intent,lease=lease,
                        delta=HeldPopulationDelta.from_dict(record['delta']),operation_id=operation_id)
                self._delta = self._native = None
                return record
            from backend.onprem.runtime.population import HeldPopulationDelta
            if record['delta'] is None:
                lease = await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                profile = await _call(self.profile_reader, intent=intent, lease=lease)
                replacement_reader=getattr(self,'replacement_reader',None)
                replacement=await _call(replacement_reader,intent=intent,lease=lease,operation_id=operation_id) if callable(replacement_reader) else None
                replacement_profile_reader=getattr(self,'replacement_profile_reader',None)
                if replacement is not None and callable(replacement_profile_reader):
                    profile=await _call(replacement_profile_reader,intent=intent,lease=lease)
                lease = await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                await asyncio.to_thread(reservation_dispatch,self.ledger,lease,operation_id)
                delta = await self.controller.reserve_population(record['request']['add_counts'], record['request']['remove_device_ids'],
                    lease=lease, expected_population_revision=record['request']['expected_population_revision'],
                    operation_id=operation_id, profile=profile, idempotency_key=idempotency_key, valid_until=lease.valid_until,
                    **({'replacement_constraint':replacement} if replacement is not None else {}))
                record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id, 'reserved', 'held', delta=delta.to_dict())
            delta = HeldPopulationDelta.from_dict(record['delta'])
            self._delta = delta
            common = dict(intent=intent, lease=lease, delta=delta)
            if record['phase'] in {'held', 'capacity_pending'}:
                record = await self._phase(record, 'capacity', self.capacity_admission, 'admitted', **common)
            if record['phase'] in {'capacity', 'admissions_pending'}:
                record = await self._phase(record, 'admissions', self.install_pending_admissions, 'installed', **common)
            if record['phase'] == 'admissions':
                record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id, 'admissions', 'consume_pending')
                lease = await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                if delta.added_devices:
                    await self.controller.consume_population(delta)
                record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id, 'consume_pending', 'adding')
            # consume_pending after crash is intentionally only inspected. The
            # existing writer's durable command decides if dispatch occurred.
            if record['phase'] in {'consume_pending', 'adding', 'inherited_pending'}:
                proof = await self.controller.population_readback(delta)
                if record['phase'] == 'consume_pending' and delta.added_devices and proof.state == 'reserved':
                    # Queue authorization is a CAS in the sole writer, not a
                    # Kubernetes create. A fresh reserved-state proof permits
                    # recovery of the dispatch-before-journal crash window.
                    await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                    await self.controller.consume_population(delta)
                    proof = await self.controller.population_readback(delta)
                self._native = deepcopy(proof.committed_snapshot)
                addition_snapshot = getattr(proof, 'addition_snapshot', None) or proof.current_snapshot
                if delta.added_devices and (not (getattr(proof, 'additions_ready', False) or proof.ready) or addition_snapshot is None):
                    return record
                old = await asyncio.to_thread(self.ledger.current_service_scope)
                added = _addition_barrier(old, addition_snapshot, delta) if delta.added_devices else []
                record = await self._phase(record, 'inherited', self.inherit_member_policies, 'inherited',
                    **common, readback=proof, session_keys=added)
            if record['phase'] in {'inherited', 'withdrawn_pending'}:
                # Draining this lock waits for all owned workers in the bounded
                # window. Subsequent windows contain only surviving devices.
                async with self.coordinator._population_window_lock:
                    current = await asyncio.to_thread(self.ledger.current_service_scope)
                    removed_keys = sorted(r['sessionKey'] for r in session_rows(current) if r['deviceId'] in delta.removed_device_ids)
                    record = await self._phase(record, 'withdrawn', self.withdraw_member_policies, 'withdrawn',
                        **common, session_keys=removed_keys)
                    record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id,
                        'withdrawn', 'retire_pending', exclude=delta.removed_device_ids)
                    await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                    await self.controller.retire_population(delta)
                    record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id, 'retire_pending', 'retiring')
            if record['phase'] in {'retire_pending', 'retiring', 'native_ready'}:
                proof = await self.controller.population_readback(delta)
                if record['phase'] == 'retire_pending' and proof.state in {'reserved', 'added_ready'}:
                    await asyncio.to_thread(current_authority, self.ledger, lease, operation_id)
                    await self.controller.retire_population(delta)
                    proof = await self.controller.population_readback(delta)
                self._native = deepcopy(proof.committed_snapshot)
                if proof.ready is not True or proof.removed_native_absent is not True:
                    return record
                async with self.coordinator._population_window_lock:
                    if record['phase'] != 'native_ready':
                        record = await asyncio.to_thread(checkpoint, self.ledger, lease, operation_id, record['phase'], 'native_ready')
                    finalize=getattr(self,'finalize_member_departure',None)
                    if delta.removed_device_ids and callable(finalize):
                        lease=await asyncio.to_thread(current_authority,self.ledger,lease,operation_id)
                        await _call(finalize,intent=intent,lease=lease,delta=delta,operation_id=operation_id,
                            readback=proof,session_keys=record['receipts']['withdrawn']['session_keys'])
                    before_publish=getattr(self,'before_population_publication',None)
                    if callable(before_publish):
                        await _call(before_publish,intent=intent,lease=lease,delta=delta,
                            operation_id=operation_id,readback=proof)
                    record = await asyncio.to_thread(commit, self.ledger, lease, operation_id, proof)
                    confirm=getattr(self,'confirm_population_publication',None)
                    if callable(confirm):
                        await _call(confirm,intent=intent,lease=lease,delta=delta,operation_id=operation_id)
                    self._delta = self._native = None
            return record
