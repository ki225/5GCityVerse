"""Held UE identities, atomically consumed before any generation is runnable.

This module never launches UEs. PDU IDs must be commissioned from the exact
native launch profile; there is intentionally no default session ID.
"""
from dataclasses import asdict,dataclass
import hashlib
import json
import time
from uuid import uuid4

from backend.onprem.intents.domain import ContractError,finite,required,timestamp


@dataclass(frozen=True)
class UEAdmissionLaunchProfile:
    profile_id:str
    image_digest:str
    configuration_digest:str
    evidence_reference:str
    session_ids:tuple[tuple[str,int],...]
    valid_until:float
    source:str='native-ue-launch-profile/v1'
    device_sessions:tuple=()
    device_session_ids:tuple=()

    def __post_init__(self):
        for field in ('profile_id','image_digest','configuration_digest','evidence_reference'):
            required(getattr(self,field),field)
        if self.source not in {'native-ue-launch-profile/v1','native-ue-device-launch-profile/v2'} or not self.image_digest.startswith('sha256:'):
            raise ContractError('commissioned_ue_launch_profile_required')
        pairs=tuple((str(k),v) for k,v in self.session_ids)
        if not pairs or len(dict(pairs))!=len(pairs) or any(type(v) is not int or not 1<=v<=15 for _,v in pairs):
            raise ContractError('explicit_ue_profile_pdu_ids_required')
        object.__setattr__(self,'session_ids',pairs)
        object.__setattr__(self,'valid_until',timestamp(self.valid_until))
        from .device_sessions import SessionLaunchSpec
        catalog=tuple((name,tuple(SessionLaunchSpec(**s) if isinstance(s,dict) else s for s in rows)) for name,rows in self.device_sessions)
        ids=tuple(tuple(row) for row in self.device_session_ids)
        if catalog or ids or self.source=='native-ue-device-launch-profile/v2':
            if self.source!='native-ue-device-launch-profile/v2' or not catalog or len(dict(catalog))!=len(catalog):
                raise ContractError('explicit_device_launch_catalog_required')
            keys={(group,s.session_id) for group,rows in catalog for s in rows}
            if any(not rows or any(type(s) is not SessionLaunchSpec for s in rows) for _,rows in catalog):
                raise ContractError('typed_device_launch_sessions_required')
            if len(ids)!=len(keys) or {(g,s) for g,s,_ in ids}!=keys:
                raise ContractError('commissioned_device_pdu_ids_incomplete')
            for group,rows in catalog:
                values=[pdu for g,_,pdu in ids if g==group]
                if len(set(values))!=len(values) or any(type(pdu) is not int or not 1<=pdu<=15 for pdu in values):
                    raise ContractError('commissioned_device_pdu_ids_invalid')
                from .device_sessions import DeviceAllocation
                DeviceAllocation('validation','imsi-208930000000001',0,group,'validation',rows)
        object.__setattr__(self,'device_sessions',catalog)
        object.__setattr__(self,'device_session_ids',ids)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        return cls(**value)


@dataclass(frozen=True)
class ReservedUE:
    supi:str
    pdu_session_id:int
    slice:str
    credential_group:int
    nonce:str
    session_id:str=''

    def __post_init__(self):
        from backend.onprem.intents.pending_admission import AdmissionIdentity
        AdmissionIdentity('validation',self.supi,self.pdu_session_id,self.nonce)
        required(self.slice,'slice')
        if not isinstance(self.session_id,str):
            raise ContractError('invalid_reserved_logical_session_id')
        if type(self.credential_group) is not int or not 0<=self.credential_group<=3:
            raise ContractError('invalid_ue_credential_group')


@dataclass(frozen=True)
class HeldUEAllocation:
    reservation_id:str
    version:str
    owner_id:str
    operation_id:str
    request_id:str
    network_scope:str
    counts:tuple[tuple[str,int],...]
    allocation:tuple[ReservedUE,...]
    baseline_generation:int
    valid_until:float
    state:str
    created_at:float
    consumed_generation:int|None
    launch_profile_id:str
    devices:tuple=()
    runtime_policy_digest:str=''

    def __post_init__(self):
        for field in ('reservation_id','version','owner_id','operation_id','request_id','network_scope','launch_profile_id'):
            required(getattr(self,field),field)
        from .ue import validate_counts
        counts=tuple(validate_counts(dict(self.counts)).items())
        allocation=tuple(self.allocation)
        from .device_sessions import DeviceAllocation,validate_devices
        devices=validate_devices(DeviceAllocation.from_dict(d) if isinstance(d,dict) else d for d in self.devices)
        identities={(r.supi,r.pdu_session_id) for r in allocation}
        if len(identities)!=len(allocation) or len({r.nonce for r in allocation})!=len(allocation):
            raise ContractError('duplicate_reserved_session_identity')
        if devices:
            if any(sum(d.access_slice==name for d in devices)!=n for name,n in counts):
                raise ContractError('reserved_device_allocation_differs_from_counts')
            if {(r.supi,r.pdu_session_id) for r in allocation} and {r.supi for r in allocation}!={d.supi for d in devices}:
                raise ContractError('reserved_device_sessions_incomplete')
            if len(allocation)!=sum(len(d.sessions) for d in devices):
                raise ContractError('reserved_device_sessions_incomplete')
        elif any(sum(r.slice==name for r in allocation)!=n for name,n in counts):
            raise ContractError('reserved_allocation_differs_from_counts')
        if self.state not in {'held','consumed','released','expired'}:
            raise ContractError('invalid_held_ue_state')
        if type(self.baseline_generation) is not int or self.baseline_generation<0:
            raise ContractError('invalid_reservation_baseline')
        if self.state=='consumed' and (type(self.consumed_generation) is not int or self.consumed_generation<=0):
            raise ContractError('consumed_reservation_requires_generation')
        object.__setattr__(self,'counts',counts)
        object.__setattr__(self,'allocation',allocation)
        object.__setattr__(self,'devices',devices)
        object.__setattr__(self,'created_at',timestamp(self.created_at))
        object.__setattr__(self,'valid_until',timestamp(self.valid_until))
        if self.valid_until<=self.created_at:
            raise ContractError('invalid_held_ue_validity')
        if reservation_version(self.to_dict())!=self.version:
            raise ContractError('held_reservation_content_version_mismatch')
        if (not isinstance(self.runtime_policy_digest, str) or self.runtime_policy_digest
                and (len(self.runtime_policy_digest) != 64 or any(c not in '0123456789abcdef' for c in self.runtime_policy_digest))):
            raise ContractError('invalid_reserved_runtime_policy_digest')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        fields=dict(value)
        fields['counts']=tuple(tuple(x) for x in fields['counts'])
        fields['allocation']=tuple(ReservedUE(**x) for x in fields['allocation'])
        return cls(**fields)

    def active(self,now):
        return self.state=='held' and self.created_at<=now<self.valid_until


def reservation_version(value):
    content=dict(value)
    content.pop('version',None)
    # Preserve the deployed v1 empty-device handle hash across additive migration.
    if not content.get('devices'):
        content.pop('devices',None)
    if not content.get('runtime_policy_digest'):
        content.pop('runtime_policy_digest', None)
    content['allocation']=[dict(row) for row in content['allocation']]
    for row in content['allocation']:
        if not row.get('session_id'):
            row.pop('session_id',None)
    for field in ('created_at','valid_until'):
        content[field]=timestamp(content[field])
    return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class HeldUERepository:
    """Uses the generation DB and its recovery fence, never a second lock domain."""
    def __init__(self,generation_store,clock=time.time):
        self.store,self.clock=generation_store,clock

    @staticmethod
    def initialize(db):
        db.execute('CREATE TABLE IF NOT EXISTS ue_held_reservation (slot INTEGER PRIMARY KEY CHECK(slot=1),value TEXT NOT NULL)')

    @staticmethod
    def _read(db):
        row=db.execute('SELECT value FROM ue_held_reservation WHERE slot=1').fetchone()
        return HeldUEAllocation.from_dict(json.loads(row[0])) if row else None

    @staticmethod
    def _save(db,value):
        data=value.to_dict() if isinstance(value,HeldUEAllocation) else dict(value)
        data['version']=reservation_version(data)
        db.execute('INSERT OR REPLACE INTO ue_held_reservation VALUES (1,?)',(json.dumps(data,allow_nan=False),))
        return HeldUEAllocation.from_dict(data)

    def current(self):
        with self.store.connect() as db:
            self.initialize(db)
            return self._read(db)

    @staticmethod
    def assert_no_held(db,now=None):
        HeldUERepository.initialize(db)
        current=HeldUERepository._read(db)
        if current and current.active(time.time() if now is None else now):
            raise ContractError('held_ue_reservation_owns_allocation')

    def reserve(self,counts,*,owner_id,operation_id,request_id,network_scope,valid_until,profile,runtime_policy_digest=''):
        from .ue import validate_counts,allocate
        counts=validate_counts(counts)
        now=self.clock()
        if type(profile) is not UEAdmissionLaunchProfile or not now<profile.valid_until:
            raise ContractError('needs_measurement:commissioned_ue_launch_profile_missing')
        ids=dict(profile.session_ids)
        if any(n and name not in ids for name,n in counts.items()):
            raise ContractError('needs_measurement:ue_profile_session_id_missing')
        expiry=min(timestamp(valid_until),profile.valid_until)
        if expiry<=now or not any(counts.values()):
            raise ContractError('invalid_reservation_demand_or_validity')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.initialize(db)
            self.store._assert_recovery_clear(db,require_fresh=True)
            from .population import PopulationRepository
            PopulationRepository.assert_no_pending(db)
            previous=self._read(db)
            same=previous and (previous.owner_id,previous.operation_id,previous.request_id)==(owner_id,operation_id,request_id)
            if same and previous.state in {'held','consumed'}:
                if (dict(previous.counts)!=counts or previous.network_scope!=network_scope
                        or previous.launch_profile_id!=profile.profile_id or previous.runtime_policy_digest != runtime_policy_digest):
                    raise ContractError('reservation_idempotency_conflict')
                if previous.state=='consumed' or previous.active(now):
                    return previous
            if previous and previous.active(now):
                raise ContractError('foreign_held_ue_reservation')
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            if current and (current['allocation'] or any(current['desired'].values()) or current['result'].get('status')!='stopped'):
                raise ContractError('empty_native_generation_required_for_pretraffic_reservation')
            devices=()
            if profile.device_sessions:
                from .device_sessions import DeviceAllocation
                catalog=dict(profile.device_sessions)
                pdu_ids={(group,name):pdu for group,name,pdu in profile.device_session_ids}
                if any(n and group not in catalog for group,n in counts.items()):
                    raise ContractError('commissioned_device_catalog_group_missing')
                devices=tuple(DeviceAllocation('device-'+r['supi'],r['supi'],r['credentialGroup'],r['slice'],network_scope,catalog[r['slice']]) for r in allocate(counts))
                rows=tuple(ReservedUE(d.supi,pdu_ids[(d.access_slice,s.session_id)],d.access_slice,d.credential_group,uuid4().hex,s.session_id) for d in devices for s in d.sessions)
            else:
                rows=tuple(ReservedUE(r['supi'],ids[r['slice']],r['slice'],r['credentialGroup'],uuid4().hex)
                           for r in allocate(counts))
            data=dict(reservation_id=str(uuid4()),version='new',owner_id=owner_id,operation_id=operation_id,
                request_id=request_id,network_scope=network_scope,counts=tuple(counts.items()),allocation=[asdict(r) for r in rows],
                baseline_generation=current['generation'] if current else 0,valid_until=expiry,state='held',
                created_at=now,consumed_generation=None,launch_profile_id=profile.profile_id,devices=[d.to_dict() for d in devices],
                runtime_policy_digest=runtime_policy_digest)
            return self._save(db,data)

    def consume(self,handle,counts,*,allocation_builder=None):
        from .ue import validate_counts,blank_status
        if type(handle) is not HeldUEAllocation:
            raise ContractError('typed_held_ue_reservation_required')
        counts=validate_counts(counts)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.initialize(db)
            self.store._assert_recovery_clear(db,require_fresh=True)
            from .population import PopulationRepository
            PopulationRepository.assert_no_pending(db)
            current=self._read(db)
            if current is None or (current.reservation_id,current.owner_id,current.operation_id,current.request_id)!=(handle.reservation_id,handle.owner_id,handle.operation_id,handle.request_id):
                raise ContractError('foreign_or_missing_held_reservation')
            if (dict(current.counts)!=counts or current.allocation!=handle.allocation or current.devices!=handle.devices
                    or current.runtime_policy_digest != handle.runtime_policy_digest):
                raise ContractError('reserved_allocation_changed')
            if current.state=='consumed':
                # A replay observes the same native launch generation, never a new one.
                row=db.execute('SELECT * FROM generations WHERE id=?',(current.consumed_generation,)).fetchone()
                if row is None or db.execute('SELECT MAX(id) FROM generations').fetchone()[0]!=current.consumed_generation:
                    raise ContractError('consumed_reservation_generation_changed')
                decoded = self.store.decode(row)
                if allocation_builder is not None and allocation_builder(decoded['allocation']) != decoded['allocation']:
                    raise ContractError('consumed_spatial_allocation_changed')
                return decoded,False
            if current.version!=handle.version or not current.active(self.clock()):
                raise ContractError('stale_or_expired_held_reservation')
            last=db.execute('SELECT MAX(id) FROM generations').fetchone()[0] or 0
            if last!=current.baseline_generation:
                raise ContractError('reservation_baseline_generation_changed')
            if current.devices:
                allocation=[{'supi':d.supi,'slice':d.access_slice,'credentialGroup':d.credential_group,
                             'device':d.to_dict(),'reservedSessions':[asdict(r) for r in current.allocation if r.supi==d.supi]} for d in current.devices]
                if current.runtime_policy_digest:
                    for row in allocation:row['runtimePolicyDigest'] = current.runtime_policy_digest
            else:
                allocation=[{'supi':r.supi,'slice':r.slice,'credentialGroup':r.credential_group,
                             'pduSessionId':r.pdu_session_id,'reservationNonce':r.nonce} for r in current.allocation]
            if allocation_builder is not None:
                allocation = allocation_builder(allocation)
            cursor=db.execute('INSERT INTO generations(desired,allocation,created,status) VALUES (?,?,?,?)',
                (json.dumps(counts),json.dumps(allocation),self.clock(),'{}'))
            result=blank_status(cursor.lastrowid,counts)
            db.execute('UPDATE generations SET status=? WHERE id=?',(json.dumps(result),cursor.lastrowid))
            data=current.to_dict()|{'state':'consumed','consumed_generation':cursor.lastrowid}
            self._save(db,data)
            return self.store.decode(db.execute('SELECT * FROM generations WHERE id=?',(cursor.lastrowid,)).fetchone()),True

    def release(self,handle):
        if type(handle) is not HeldUEAllocation:
            raise ContractError('typed_held_ue_reservation_required')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.initialize(db)
            current=self._read(db)
            if current is None or (current.reservation_id,current.owner_id,current.operation_id,current.request_id)!=(handle.reservation_id,handle.owner_id,handle.operation_id,handle.request_id):
                raise ContractError('foreign_or_missing_held_reservation')
            if current.state in {'released','expired'}:
                return current
            if current.state=='consumed':
                raise ContractError('consumed_reservation_requires_native_traffic_cleanup')
            if current.version!=handle.version:
                raise ContractError('stale_held_reservation_release')
            return self._save(db,current.to_dict()|{'state':'released'})
