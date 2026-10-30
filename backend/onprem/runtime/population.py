"""Current, owned v2 population journal in the existing generation database.

Reservations allocate identities, never Kubernetes resources. Only the existing
UEController reconciliation loop may dispatch native changes. A dispatched but
unconfirmed write is inspected after restart; it is never blindly repeated.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import time
from uuid import uuid4

from backend.onprem.intents.domain import ContractError, required
from .device_sessions import DeviceAllocation
from .ue_reservation import HeldUERepository, ReservedUE, UEAdmissionLaunchProfile
from .errors import EvidenceUnavailable


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def birth_proof(snapshot,device_id):
    """Pinned native incarnation and PDU path, never a desired-state identity."""
    rows=[r for r in snapshot.get('devices',[]) if r.get('deviceId')==device_id]
    if len(rows)!=1:
        raise ContractError('population_birth_native_proof_missing')
    device=rows[0]
    return {'device':{**{k:device.get(k) for k in ('deviceId','supi','podUid','containerId','processEpoch')},
                      'sessionKeys':sorted(device.get('sessionKeys',[]))},
        'sessions':sorted(({k:r.get(k) for k in ('sessionKey','smContextRef','establishedEpoch','nativeUpfId','upfEpoch','ueIp','tun','dnn','snssai')}
                         for r in snapshot.get('perSession',[]) if r.get('deviceId')==device_id),key=lambda r:r['sessionKey'])}


def owns_lease(recorded,lease):
    """Renewal may change validity/revision, never native fleet ownership."""
    from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,lease_renewal_follows
    return lease_renewal_follows(ServiceIntentTrafficLease.from_dict(recorded),lease)


@dataclass(frozen=True)
class HeldPopulationDelta:
    delta_id: str
    version: str
    lease_id: str
    owner_id: str
    start_operation_id: str
    request_id: str
    operation_id: str
    generation: int
    base_revision: int
    next_revision: int
    state: str
    created_at: float
    valid_until: float
    added_devices: tuple
    reserved_sessions: tuple
    removed_device_ids: tuple
    added_records: tuple
    removed_records: tuple
    fingerprint: str

    def __post_init__(self):
        for name in ('delta_id', 'version', 'lease_id', 'owner_id', 'start_operation_id', 'request_id', 'operation_id', 'fingerprint'):
            required(getattr(self, name), name)
        if (type(self.generation) is not int or self.generation < 1 or type(self.base_revision) is not int
                or self.base_revision < 0 or self.next_revision != self.base_revision + 1):
            raise ContractError('invalid_population_revision')
        if self.state not in {'reserved', 'adding', 'added_ready', 'retiring', 'committed', 'expired', 'recovery_required','quiesced'}:
            raise ContractError('invalid_population_state')
        if not 0 < self.created_at < self.valid_until:
            raise ContractError('invalid_population_validity')
        if len(set(self.removed_device_ids)) != len(self.removed_device_ids):
            raise ContractError('duplicate_population_removal')
        if any(type(d) is not DeviceAllocation for d in self.added_devices) or any(type(r) is not ReservedUE for r in self.reserved_sessions):
            raise ContractError('typed_population_reservations_required')
        if {d.supi for d in self.added_devices} != {r.supi for r in self.reserved_sessions}:
            raise ContractError('population_reserved_sessions_incomplete')
        for field in ('added_records','removed_records'):
            object.__setattr__(self,field,tuple(json.loads(json.dumps(getattr(self,field),allow_nan=False))))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields = dict(value)
        fields['added_devices'] = tuple(DeviceAllocation.from_dict(d) for d in fields['added_devices'])
        fields['reserved_sessions'] = tuple(ReservedUE(**r) for r in fields['reserved_sessions'])
        for key in ('removed_device_ids', 'added_records', 'removed_records'):
            fields[key] = tuple(fields[key])
        return cls(**fields)


@dataclass(frozen=True)
class PopulationReadback:
    generation: int
    population_revision: int
    state: str
    ready: bool
    removed_native_absent: bool
    current_snapshot: dict | None
    committed_snapshot: dict | None
    addition_snapshot: dict | None = None
    additions_ready: bool = False

    def to_dict(self):
        return asdict(self)


class PopulationRepository:
    """One bounded pending command and birth/retirement records for current fleet."""
    def __init__(self, store, clock=None):
        self.store, self.clock = store, time.time if clock is None else clock

    @staticmethod
    def initialize(db):
        db.execute('CREATE TABLE IF NOT EXISTS ue_population_current (slot INTEGER PRIMARY KEY CHECK(slot=1), value TEXT NOT NULL)')

    @staticmethod
    def _read(db):
        row = db.execute('SELECT value FROM ue_population_current WHERE slot=1').fetchone()
        if row is None:
            return None
        record=json.loads(row[0])
        if record.get('journal_schema')!='current-population/v2':
            PopulationRepository._compact(record)
            # One-time current-journal migration. The compare-and-swap prevents
            # a reader from overwriting a concurrent population command.
            value=json.dumps(record,allow_nan=False)
            if db.execute('UPDATE ue_population_current SET value=? WHERE slot=1 AND value=?',
                          (value,row[0])).rowcount!=1:
                raise ContractError('population_journal_changed_during_compaction')
        return record

    @staticmethod
    def _compact(record):
        """Keep current identities and a fixed subscriber quarantine, not history."""
        from .ue import BASE_IMSI
        schema=record.get('journal_schema')
        if schema not in (None,'current-population/v2'):
            raise ContractError('unsupported_population_journal_schema')
        slots=record.get('quarantined_subscriber_slots',[])
        if (not isinstance(slots,list) or len(slots)>1000
                or any(type(i) is not int or not 0<=i<1000 for i in slots)
                or len(set(slots))!=len(slots)):
            raise ContractError('invalid_population_subscriber_quarantine')
        used=set(slots)
        rows=record['births']+record['tombstones']+list((record.get('delta') or {}).get('added_records',()))
        if not record.get('network_scope'):
            scopes={r['device']['network_scope'] for r in rows}
            if len(scopes)!=1:
                raise ContractError('population_network_scope_or_subscriber_changed')
            record['network_scope']=scopes.pop()
        for row in rows:
            supi=row['supi']
            try:
                slot=int(supi.removeprefix('imsi-'))-BASE_IMSI
            except (AttributeError,ValueError):
                raise ContractError('invalid_population_subscriber_quarantine') from None
            if (not 0<=slot<1000 or supi!=f'imsi-{BASE_IMSI+slot}'
                    or row['device']['network_scope']!=record['network_scope']):
                raise ContractError('population_network_scope_or_subscriber_changed')
            used.add(slot)
        record['quarantined_subscriber_slots']=sorted(used)
        # The latest completed departure is enough: each prior departure already
        # crossed native absence before the next delta was admitted. Its SUPI
        # remains quarantined for the entire fleet, including expired reserves.
        record['tombstones']=record['tombstones'][-100:]
        record['journal_schema']='current-population/v2'

    @staticmethod
    def _save(db, record):
        PopulationRepository._compact(record)
        if record.get('delta'):
            value = dict(record['delta'])
            value.pop('version', None)
            record['delta']['version'] = digest(value)
        db.execute('INSERT OR REPLACE INTO ue_population_current VALUES (1,?)', (json.dumps(record, allow_nan=False),))

    def current(self):
        with self.store.connect() as db:
            self.initialize(db)
            return self._read(db)

    @staticmethod
    def assert_no_pending(db,allow_stop=False):
        PopulationRepository.initialize(db)
        value = PopulationRepository._read(db)
        if value and value.get('delta') and value['delta']['state'] not in {'committed', 'expired'} and not (allow_stop and value['delta']['state']=='quiesced'):
            raise ContractError('pending_population_owns_generation')

    def _transaction(self, db):
        db.execute('BEGIN IMMEDIATE')
        self.initialize(db)
        self.store._assert_recovery_clear(db, require_fresh=True)

    def quiesce(self,lease):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,lease_identity
        if type(lease) is not ServiceIntentTrafficLease:
            raise ContractError('typed_population_lease_required')
        with self.store.connect() as db:
            self._transaction(db)
            record=self._read(db)
            if not record or not record.get('delta'):
                return
            if not owns_lease(record.get('cleanup_lease',record['lease']),lease):
                raise ContractError('foreign_population_lease')
            record['cleanup_lease']=lease.to_dict()
            if record['delta']['state'] not in {'committed','expired'}:
                record['delta']['state']='quiesced'
                self._save(db,record)

    def confirm_stopped(self,lease,proof):
        """Close a quiesced journal only after actual whole-fleet native absence."""
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        from .ue import seconds
        if type(lease) is not ServiceIntentTrafficLease:
            raise ContractError('typed_population_lease_required')
        if (not isinstance(proof,dict) or proof.get('source')!='native-round-release/v1'
                or proof.get('scope')!='known-round-and-managed-runtime'
                or proof.get('globalSmfInventoryComplete') is not True
                or any(proof.get(k) is not True for k in ('trafficStopped','policiesAbsent','rulesRestored','uesReleased','podsAbsent'))
                or not -1<=self.clock()-seconds(proof.get('observedAt'))<=15):
            raise ContractError('population_requires_complete_native_stop_proof')
        with self.store.connect() as db:
            self._transaction(db)
            record=self._read(db)
            if not record or not record.get('delta'):
                return
            if not owns_lease(record.get('cleanup_lease',record['lease']),lease):
                raise ContractError('foreign_population_lease')
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            already_empty=current and current['generation']==record['generation'] and not record['births'] and record['revision']>0
            if (not current or type(proof.get('generation')) is not int or proof['generation']!=current['generation']
                    or (current['generation']<=record['generation'] and not already_empty)
                    or current['allocation'] or any(current['desired'].values())):
                raise ContractError('population_stop_generation_changed')
            if record['delta']['state'] not in {'quiesced','expired','committed'}:
                raise ContractError('population_must_quiesce_before_stop_confirmation')
            record['delta']['state']='expired'
            record['stop_proof']=proof
            self._save(db,record)

    def reserve(self, add_counts, remove_device_ids, *, lease, expected_population_revision,
                operation_id, profile, idempotency_key, valid_until,baseline_snapshot=None,replacement_constraint=None):
        from .ue import validate_counts, BASE_IMSI, SLICES
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        if type(lease) is not ServiceIntentTrafficLease or not lease.active(self.clock()):
            raise ContractError('active_typed_population_lease_required')
        if type(profile) is not UEAdmissionLaunchProfile or not profile.device_sessions or profile.valid_until <= self.clock():
            raise ContractError('commissioned_device_population_profile_required')
        counts = validate_counts(add_counts)
        if replacement_constraint is not None:
            from backend.onprem.intents.session_replacement import ExactSessionReplacement
            if type(replacement_constraint) is not ExactSessionReplacement:
                raise ContractError('typed_exact_replacement_required')
            replacement_constraint.validate_profile(profile)
            original=replacement_constraint.original_record['device']
            if (sum(counts.values())!=1 or counts.get(original['access_slice'])!=1 or remove_device_ids
                    or replacement_constraint.parent['intent_id']!=lease.intent_id):
                raise ContractError('replacement_population_scope_changed')
        removes = tuple(remove_device_ids)
        if any(not isinstance(d, str) or not d for d in removes) or len(set(removes)) != len(removes):
            raise ContractError('exact_device_removals_required')
        if not any(counts.values()) and not removes:
            raise ContractError('empty_population_delta')
        if type(expected_population_revision) is not int or expected_population_revision < 0:
            raise ContractError('invalid_population_revision')
        required(operation_id, 'operation_id'); required(idempotency_key, 'idempotency_key')
        expiry = min(float(valid_until), lease.valid_until, profile.valid_until)
        if not self.clock() < expiry:
            raise ContractError('expired_population_reservation')
        from backend.onprem.scenarios.intent_traffic import lease_identity
        fingerprint = digest({'lease': lease_identity(lease), 'counts': counts, 'remove': sorted(removes),
                              'base': expected_population_revision, 'profile': profile.to_dict(),
                              **({'replacement':replacement_constraint.to_dict()} if replacement_constraint else {})})
        with self.store.connect() as db:
            self._transaction(db)
            current = self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            if replacement_constraint is not None and (not current or replacement_constraint.parent['generation']!=current['generation']):
                raise ContractError('replacement_generation_changed')
            held = HeldUERepository._read(db)
            record = self._read(db)
            owned_empty=(current and record and record['generation']==current['generation']
                and (record.get('delta') or {}).get('state')=='committed' and not record['births']
                and bool(record['tombstones']) and record['revision']>0)
            if (not current or (not current['allocation'] and not owned_empty) or any('device' not in row for row in current['allocation'])
                    or not held or held.state != 'consumed' or held.consumed_generation != current['generation']
                    or not held.devices
                    or (held.owner_id, held.operation_id, held.request_id) != (lease.owner_id, lease.operation_id, lease.request_id)):
                raise ContractError('population_requires_original_owned_device_fleet')
            if record and record['generation'] != current['generation']:
                # An unresolved old command cannot be hidden by a new fleet.
                if record.get('delta') and record['delta']['state'] not in {'committed', 'expired'}:
                    raise ContractError('population_generation_changed')
                record = None
            if record is None:
                births = []
                for index, row in enumerate(current['allocation']):
                    births.append(dict(row, podIndex=index, deviceBirthId=digest([current['generation'], index, row['device']]),
                                       deviceBornAt=current['created'],nativeBirth=birth_proof(baseline_snapshot or {},row['device']['device_id'])))
                record = {'generation': current['generation'], 'revision': 0, 'lease': lease.to_dict(),
                          'network_scope':held.network_scope,'births': births, 'next_index': len(births), 'tombstones': [], 'delta': None, 'writes': {}}
            if not owns_lease(record['lease'],lease):
                raise ContractError('foreign_population_lease')
            previous = record.get('delta')
            if previous and record.get('idempotency_key') == idempotency_key:
                recorded_fingerprint=record.get('request_fingerprint',previous['fingerprint'])
                if recorded_fingerprint!=fingerprint and 'request_fingerprint' not in record:
                    # Read compatible pre-fix reservations without changing the
                    # immutable delta/nonce handle held by the scenario journal.
                    legacy=digest({'lease':record['lease'],'counts':counts,'remove':sorted(removes),
                        'base':expected_population_revision,'profile':profile.to_dict(),'valid_until':previous['valid_until']})
                    if previous['fingerprint']==legacy:
                        recorded_fingerprint=fingerprint
                if recorded_fingerprint != fingerprint or previous['operation_id'] != operation_id:
                    raise ContractError('population_idempotency_conflict')
                if previous['valid_until']<=self.clock() and previous['state']!='committed':
                    raise ContractError('expired_population_delta_cannot_reactivate')
                record['lease']=lease.to_dict()
                record['request_fingerprint']=fingerprint
                self._save(db,record)
                return HeldPopulationDelta.from_dict(previous)
            if previous and previous['state'] not in {'committed', 'expired'}:
                if previous['state'] == 'reserved' and previous['valid_until'] <= self.clock():
                    previous['state'] = 'expired'
                else:
                    raise ContractError('pending_population_owns_generation')
            if record['revision'] != expected_population_revision:
                raise ContractError('stale_population_revision')
            record['lease']=lease.to_dict()
            for row in record['births']:
                if row.get('nativeBirth')!=birth_proof(baseline_snapshot or {},row['device']['device_id']):
                    raise ContractError('population_birth_incarnation_changed')
            by_id = {row['device']['device_id']: row for row in record['births']}
            if replacement_constraint is not None:
                replacement_constraint.validate_retired(record['tombstones'])
            if any(d not in by_id for d in removes):
                raise ContractError('foreign_or_partial_device_removal')
            # No arbitrary SUPI supplied by caller. Credential slots remain
            # quarantined for this fleet, even after absence, until fleet ends.
            self._compact(record)
            used=set(record['quarantined_subscriber_slots'])
            candidates = [BASE_IMSI + index for index in range(1000) if index not in used]
            if len(candidates) < sum(counts.values()) or len(by_id) + sum(counts.values()) > 100:
                raise ContractError('population_subscriber_pool_exhausted')
            catalog = dict(profile.device_sessions)
            pdu_ids = {(group, logical): pdu for group, logical, pdu in profile.device_session_ids}
            devices, sessions, additions = [], [], []
            scope=record.get('network_scope',held.network_scope)
            if scope!=held.network_scope or any(row['device']['network_scope']!=scope for row in record['births']+record['tombstones']):
                raise ContractError('population_network_scope_changed')
            record['network_scope']=scope
            for group, count in counts.items():
                if count and group not in catalog:
                    raise ContractError('population_profile_group_missing')
                for _ in range(count):
                    number = candidates.pop(0)
                    supi = f'imsi-{number}'
                    device = DeviceAllocation('device-' + supi, supi, (number - BASE_IMSI) // 250, group, scope, catalog[group])
                    reserved = tuple(ReservedUE(supi, pdu_ids[(group, s.session_id)], group, device.credential_group,
                                               uuid4().hex, s.session_id) for s in device.sessions)
                    index = record['next_index']; record['next_index'] += 1
                    additions.append({'supi': supi, 'slice': group, 'credentialGroup': device.credential_group,
                        'device': device.to_dict(), 'reservedSessions': [asdict(s) for s in reserved],
                        'podIndex': index, 'deviceBirthId': str(uuid4()), 'deviceBornAt': self.clock()})
                    if replacement_constraint is not None:
                        additions[-1].update(replacement_constraint.added_metadata())
                    devices.append(device); sessions.extend(reserved)
            delta = HeldPopulationDelta(str(uuid4()), 'new', lease.lease_id, lease.owner_id, lease.operation_id,
                lease.request_id, operation_id, current['generation'], record['revision'], record['revision'] + 1,
                'reserved', self.clock(), expiry, tuple(devices), tuple(sessions), removes, tuple(additions),
                tuple(by_id[d] for d in removes), fingerprint)
            if replacement_constraint is not None:replacement_constraint.validate_delta(delta)
            record.update(delta=delta.to_dict(), idempotency_key=idempotency_key,request_fingerprint=fingerprint,writes={})
            if not baseline_snapshot:
                raise ContractError('population_requires_fresh_native_baseline')
            record['baseline_snapshot']=baseline_snapshot
            # Seal stable pod indices onto original records exactly once. No
            # Pod spec changes: these metadata fields are controller-private.
            db.execute('UPDATE generations SET allocation=? WHERE id=?', (json.dumps(record['births']), current['generation']))
            self._save(db, record)
            return HeldPopulationDelta.from_dict(record['delta'])

    def _owned(self, db, handle, *, exact_version=False):
        if type(handle) is not HeldPopulationDelta:
            raise ContractError('typed_population_delta_required')
        record = self._read(db)
        if (not record or not record.get('delta') or any(record['delta'][field] != getattr(handle, field)
                for field in ('delta_id', 'lease_id', 'owner_id', 'start_operation_id', 'request_id', 'operation_id', 'generation', 'fingerprint'))):
            raise ContractError('foreign_population_delta')
        for field in ('added_devices','reserved_sessions','removed_device_ids','added_records','removed_records'):
            if HeldPopulationDelta.from_dict(record['delta']).__dict__[field]!=getattr(handle,field):
                raise ContractError('population_handle_content_changed')
        if exact_version and record['delta']['version'] != handle.version:
            raise ContractError('stale_population_delta')
        last = db.execute('SELECT MAX(id) FROM generations').fetchone()[0]
        if last != handle.generation:
            raise ContractError('population_generation_changed')
        return record

    def authorize(self, handle, phase):
        with self.store.connect() as db:
            self._transaction(db)
            record = self._owned(db, handle)
            state = record['delta']['state']
            if phase == 'adding' and state in {'adding', 'added_ready', 'retiring', 'committed'}:
                return record
            if phase == 'retiring' and state in {'retiring', 'committed'}:
                return record
            if state=='reserved':
                self._owned(db, handle, exact_version=True)
            if self.clock() >= record['delta']['valid_until']:
                raise ContractError('expired_population_delta')
            if phase == 'adding':
                if state != 'reserved':
                    raise ContractError('population_not_reserved')
                allocation = record['births'] + record['delta']['added_records']
                self._allocation(db, handle.generation, allocation)
            elif phase == 'retiring':
                if state not in {'reserved', 'added_ready'} or (record['delta']['added_devices'] and state != 'added_ready'):
                    raise ContractError('population_additions_not_proved')
            else:
                raise ContractError('invalid_population_authorization')
            record['delta']['state'] = phase
            self._save(db, record)
            return record


    @staticmethod
    def _allocation(db, generation, rows):
        from .ue import SLICES
        counts = {name: sum(row['slice'] == name for row in rows) for name in SLICES}
        db.execute('UPDATE generations SET allocation=?,desired=? WHERE id=?', (json.dumps(rows), json.dumps(counts), generation))

    def mark_write(self, handle, key, phase, **evidence):
        """Journal intent BEFORE dispatch. Repeated dispatch requests are refused."""
        with self.store.connect() as db:
            self._transaction(db)
            record = self._owned(db, handle)
            if self.clock()>=record['delta']['valid_until'] or record['delta']['state'] not in {'adding','retiring'}:
                raise ContractError('population_native_dispatch_not_authorized')
            entry = record['writes'].setdefault(key, {})
            if phase in entry:
                return False
            entry[phase] = {'at': self.clock(), **evidence}
            self._save(db, record)
            return True

    def update(self, handle, *, state=None, committed=False,observed_snapshot=None):
        with self.store.connect() as db:
            self._transaction(db)
            record = self._owned(db, handle)
            if state:
                record['delta']['state'] = state
            if committed:
                removed = set(record['delta']['removed_device_ids'])
                rows = json.loads(json.dumps(record['births'] + record['delta']['added_records']))
                record['births'] = [r for r in rows if r['device']['device_id'] not in removed]
                for row in record['births']:
                    row['nativeBirth']=birth_proof(observed_snapshot or {},row['device']['device_id'])
                if record['delta']['removed_records']:
                    record['tombstones']=list(record['delta']['removed_records'])
                record['revision'] = record['delta']['next_revision']
                record['delta']['state'] = 'committed'
                self._allocation(db, handle.generation, record['births'])
            self._save(db, record)
            return record

class PopulationRuntime:
    """Runs only under UEController._lock, using its existing native driver."""
    def __init__(self, controller):
        self.controller=controller
        self.repo=PopulationRepository(controller.store)

    def observer(self):
        observer=getattr(self.controller.adapter,'device_observer',None)
        if observer is None or not all(callable(getattr(observer,k,None)) for k in ('observe','observe_population','population_inventory','population_launch_evidence')):
            raise ContractError('commissioned_population_observer_required')
        return observer

    def _snapshot(self,delta,ids):
        value=self.observer().observe_population(delta,tuple(ids))
        from .current_sessions import session_rows
        from .ue import seconds
        if (value.get('generation')!=delta.generation or value.get('schemaVersion')!='cityverse.current-state/v2'
                or type(value.get('populationRevision',0)) is not int
                or value.get('populationRevision',0)!=self.repo.current()['revision']
                or not -1<=time.time()-seconds(value.get('observedAt'))<=15
                or {row['deviceId'] for row in value.get('devices',[])}!=set(ids)):
            raise ContractError('fresh_exact_population_snapshot_required')
        session_rows(value)
        return value

    @staticmethod
    def _same_survivors(before,after,ids):
        fields=('deviceId','supi','podUid','containerId','processEpoch','generation','sessionKeys')
        old={r['deviceId']:r for r in before.get('devices',[])}
        new={r['deviceId']:r for r in after.get('devices',[])}
        if any(device not in old or device not in new or any(old[device].get(k)!=new[device].get(k) for k in fields) for device in ids):
            raise ContractError('population_survivor_incarnation_changed')
        session_fields=('sessionKey','smContextRef','establishedEpoch','upfEpoch','nativeUpfId','tun','ueIp','dnn','snssai')
        a={r['sessionKey']:r for r in before.get('perSession',[]) if r.get('deviceId') in ids}
        b={r['sessionKey']:r for r in after.get('perSession',[]) if r.get('deviceId') in ids}
        if set(a)!=set(b) or any(any(a[key].get(k)!=b[key].get(k) for k in session_fields) for key in a):
            raise ContractError('population_survivor_native_binding_changed')

    def readback(self,handle):
        with self.controller.store.connect() as db:
            record=self.repo._owned(db,handle)
        delta=HeldPopulationDelta.from_dict(record['delta'])
        old_ids={r['device']['device_id'] for r in record['births']}
        added={d.device_id for d in delta.added_devices}
        removed=set(delta.removed_device_ids)
        survivor_ids=old_ids-removed if delta.state in {'retiring','committed'} else old_ids
        absent=not removed
        if removed and delta.state in {'retiring','committed'}:
            inventory=self.observer().population_inventory(delta)
            absent=all(inventory['native_absent'].get(r['supi']) is True
                and f"cv-ue-g{delta.generation}-{r['podIndex']}" not in inventory['pods'] for r in delta.removed_records)
        candidate=addition=committed=None
        baseline_ids={r['deviceId'] for r in record['baseline_snapshot']['devices']}
        # One actual complete read serves all projections of the same cohort.
        # Subset views retain native clocks/proofs; they do not invent metrics.
        full_ids=(old_ids|added)-removed if delta.state in {'retiring','committed'} else old_ids|added
        full=None
        if delta.state in {'adding','added_ready','retiring','committed'}:
            try:
                full=self._snapshot(delta,full_ids)
                self._same_survivors(record['baseline_snapshot'],full,(old_ids-removed if delta.state in {'retiring','committed'} else old_ids)&baseline_ids)
            except EvidenceUnavailable:
                pass
        if full is not None:
            committed=json.loads(json.dumps(full))
            committed['devices']=[r for r in full['devices'] if r['deviceId'] in survivor_ids]
            committed['perUe']=committed['devices']
            committed['perSession']=[r for r in full['perSession'] if r['deviceId'] in survivor_ids]
            if added and delta.state in {'adding','added_ready'}:
                addition=full
            if not removed or (absent and delta.state in {'retiring','committed'}):
                candidate=full
        else:
            try:
                committed=self._snapshot(delta,survivor_ids)
                self._same_survivors(record['baseline_snapshot'],committed,survivor_ids&baseline_ids)
            except EvidenceUnavailable:
                pass
        if removed and absent:
            from .ue import seconds
            if not -1<=time.time()-seconds(inventory['observedAt'])<=15:
                absent=False
                candidate=None
        ready=candidate is not None and absent
        return PopulationReadback(delta.generation,record['revision'],delta.state,ready,absent,candidate,committed,
                                  addition,not added or addition is not None or (delta.state=='committed' and ready))

    def reconcile(self):
        record=self.repo.current()
        if not record or not record.get('delta') or record['delta']['state'] in {'reserved','committed','expired','recovery_required','quiesced'}:
            return None
        delta=HeldPopulationDelta.from_dict(record['delta'])
        if time.time()>=delta.valid_until:
            self.repo.update(delta,state='recovery_required')
            raise ContractError('population_expired_no_new_native_writes')
        current=self.controller.store.current()
        if current['generation']!=delta.generation:
            raise ContractError('population_generation_changed')
        if delta.state=='adding':
            # Read actual managed inventory BEFORE any dispatch. A native error
            # after journalled create cannot lead to another create on restart.
            pods=self.controller.driver.read_pods()['items']
            by_name={p['metadata']['name']:p for p in pods}
            known={f"cv-ue-g{delta.generation}-{r['podIndex']}" for r in record['births']+record['delta']['added_records']}
            if any(p.get('metadata',{}).get('labels',{}).get('cityverse.io/managed-by')=='onprem-ue-controller'
                   and p['metadata']['name'] not in known for p in pods):
                raise ContractError('population_has_unowned_managed_pod')
            for row in delta.added_records:
                name=f"cv-ue-g{delta.generation}-{row['podIndex']}"
                if name in by_name:
                    continue  # Sealed spec/incarnation validated by observer.
                if name in record['writes']:
                    return None  # Unknown create result: readback only.
                evidence=self.observer().population_launch_evidence(delta)
                if self.repo.mark_write(delta,name,'create_dispatched'):
                    self.controller.driver.population_create(delta.generation,row,evidence)
                return None
            ids={r['device']['device_id'] for r in record['births']}|{d.device_id for d in delta.added_devices}
            try:
                proof=self._snapshot(delta,ids)
            except EvidenceUnavailable:
                return None
            self._same_survivors(record['baseline_snapshot'],proof,{r['device']['device_id'] for r in record['births']})
            if delta.removed_device_ids:
                self.repo.update(delta,state='added_ready')
            else:
                self.repo.update(delta,committed=True,observed_snapshot=proof)
            return proof
        if delta.state=='retiring':
            inventory=self.observer().population_inventory(delta)
            original={r['deviceId']:r for r in record['baseline_snapshot']['devices']}
            for row in delta.removed_records:
                name=f"cv-ue-g{delta.generation}-{row['podIndex']}"
                pod=inventory['pods'].get(name)
                if pod is not None:
                    expected_uid=original[row['device']['device_id']]['podUid']
                    if pod['metadata'].get('uid')!=expected_uid:
                        raise ContractError('population_retiring_pod_replaced')
                    if inventory['native_absent'].get(row['supi']) is not True:
                        if self.repo.mark_write(delta,name,'deregister_dispatched',pod_uid=expected_uid):
                            self.controller.driver.population_deregister(pod,row['supi'])
                        return None
                    if self.repo.mark_write(delta,name,'delete_dispatched',pod_uid=expected_uid):
                        self.controller.driver.delete_owned_pod(pod)
                    return None
                if inventory['native_absent'].get(row['supi']) is not True:
                    return None
            ids=({r['device']['device_id'] for r in record['births']}|{d.device_id for d in delta.added_devices})-set(delta.removed_device_ids)
            proof=self._snapshot(delta,ids)
            self._same_survivors(record['baseline_snapshot'],proof,{r['device']['device_id'] for r in record['births']}-set(delta.removed_device_ids))
            self.repo.update(delta,committed=True,observed_snapshot=proof)
            return proof
        return None
