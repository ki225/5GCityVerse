"""New-session admission protocol. Policy installation is not session or traffic proof."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import hashlib
import re
import time
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .domain import ContractError, SessionKey, finite, required, timestamp


class AdmissionBlocked(ContractError):
    pass


class AdmissionRequestNotSent(AdmissionBlocked):
    """Trusted transport rejected this exact request before its first send.

    Socket errors are excluded: beginning send may already have written bytes.
    """
    def __init__(self,request):
        super().__init__('Native admission request proven not sent')
        self.request_identity=(request.get_method(),request.full_url,
            hashlib.sha256(request.data or b'').hexdigest())

    def matches(self,request):
        return self.request_identity==(request.get_method(),request.full_url,
            hashlib.sha256(request.data or b'').hexdigest())


@dataclass(frozen=True)
class AdmissionIdentity:
    network_scope: str
    supi: str
    pdu_session_id: int
    nonce: str

    def __post_init__(self):
        required(self.network_scope,'network_scope')
        required(self.supi,'supi')
        if type(self.pdu_session_id) is not int or not 1 <= self.pdu_session_id <= 15:
            raise AdmissionBlocked('PDU session ID must be 1..15')
        if not isinstance(self.nonce,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',self.nonce):
            raise AdmissionBlocked('Invalid admission nonce')

    @property
    def token(self):
        return json.dumps([self.network_scope,self.supi,self.pdu_session_id,self.nonce],separators=(',',':'))


@dataclass(frozen=True)
class PendingAdmission:
    identity: AdmissionIdentity
    owner_id: str
    target_upf_id: str
    valid_from: float
    valid_until: float

    def __post_init__(self):
        required(self.owner_id,'owner_id')
        required(self.target_upf_id,'target_upf_id')
        object.__setattr__(self,'valid_from',timestamp(self.valid_from))
        object.__setattr__(self,'valid_until',timestamp(self.valid_until))
        if self.valid_until <= self.valid_from:
            raise AdmissionBlocked('Invalid admission validity')

    def active(self,now):
        return self.valid_from <= now < self.valid_until

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        from .domain import strict_fields,construct
        strict_fields(value,set(cls.__dataclass_fields__))
        values=dict(value)
        strict_fields(values.get('identity'),set(AdmissionIdentity.__dataclass_fields__))
        values['identity']=construct(AdmissionIdentity,values['identity'])
        return construct(cls,values)

    def to_native_request(self,operation_id,expected_version):
        required(operation_id,'operation_id')
        required(expected_version,'expected_version')
        return {'networkScope':self.identity.network_scope,'supi':self.identity.supi,
                'pduSessionId':self.identity.pdu_session_id,'nonce':self.identity.nonce,
                'ownerId':self.owner_id,'operationId':operation_id,'expectedVersion':expected_version,
                'targetUpfId':self.target_upf_id,'validFrom':self.valid_from,'validUntil':self.valid_until}


@dataclass(frozen=True)
class DeferredPendingAdmission:
    """A logical dependency reference, never a native request or guessed UUID."""
    identity: AdmissionIdentity
    owner_id: str
    target_id: str
    dependency_action_id: str
    reservation_id: str
    reservation_version: str
    valid_from: float
    valid_until: float

    def __post_init__(self):
        for field in ('owner_id','target_id','dependency_action_id','reservation_id','reservation_version'):
            required(getattr(self,field),field)
        object.__setattr__(self,'valid_from',timestamp(self.valid_from))
        object.__setattr__(self,'valid_until',timestamp(self.valid_until))
        if self.valid_until<=self.valid_from:raise AdmissionBlocked('Invalid deferred admission validity')

    def active(self,now):return self.valid_from<=now<self.valid_until
    def to_dict(self):return asdict(self)

    @classmethod
    def from_dict(cls,value):
        from .domain import strict_fields,construct
        strict_fields(value,set(cls.__dataclass_fields__))
        values=dict(value)
        strict_fields(values.get('identity'),set(AdmissionIdentity.__dataclass_fields__))
        values['identity']=construct(AdmissionIdentity,values['identity'])
        return construct(cls,values)


@dataclass(frozen=True)
class DeferredAdmissionAuthority:
    """Trusted reader result: current held reservation plus selected intent scope."""
    reservation: object
    user_owner: str
    intent_id: str
    operation_id: str
    selected_identities: frozenset[str]
    valid_from: float
    valid_until: float

    def __post_init__(self):
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation
        if type(self.reservation) is not HeldUEAllocation:
            raise AdmissionBlocked('Typed current held reservation required')
        held=self.reservation
        object.__setattr__(self,'selected_identities',frozenset(self.selected_identities))
        whole={AdmissionIdentity(held.network_scope,r.supi,r.pdu_session_id,r.nonce).token for r in held.allocation}
        if (held.owner_id!=self.user_owner or held.operation_id!=self.operation_id
                or not self.intent_id or not self.selected_identities or not self.selected_identities<=whole
                or not self.valid_from<self.valid_until):
            raise AdmissionBlocked('Deferred reservation authority mismatch')


class DeferredAdmissionMaterializer:
    """Pure admission construction after fresh server-owned dependency readback.

    reservation_reader(id,user_owner,intent_id,operation_id) must read CURRENT
    durable ownership/classification, not deserialize model-provided authority.
    Persistence/dispatch remain in the original IntentService operation journal.
    """
    def __init__(self,registry,reservation_reader,admission_api,*,clock=time.time):
        self.registry,self.reservation_reader,self.api,self.clock=registry,reservation_reader,admission_api,clock

    def materialize(self,request,action,receipts,*,user_owner,previous_request=None):
        from dataclasses import replace
        if request.action!='assign_sessions' or request.session_keys:
            raise AdmissionBlocked('Deferred materialization requires new-session assignment')
        if request.parameters!=action.parameters:
            raise AdmissionBlocked('Deferred request differs from validated action')
        assignment=SessionAssignment.from_action(action)
        pending=assignment.pending_admissions
        if not pending or any(type(p) is not DeferredPendingAdmission for p in pending):
            raise AdmissionBlocked('Mixed or nondeferred admission batch')
        now=self.clock();snapshot=self.registry.snapshot()
        binding=snapshot.admission_binding(request.target_id,owner_id=request.owner_id,
            operation_id=request.operation_id,now=now)
        prior={r['action_id']:r for r in receipts}
        concrete=[]
        for deferred in pending:
            receipt=prior.get(deferred.dependency_action_id)
            installed={} if receipt is None else receipt.get('installed',{})
            dependency={} if receipt is None else receipt.get('request',{})
            if (deferred.target_id!=request.target_id or deferred.owner_id!=request.owner_id
                    or deferred.dependency_action_id not in action.depends_on
                    or receipt is None or receipt.get('state')!='installation_confirmed'
                    or dependency.get('action') not in {'add_upf','place_nf'}
                    or dependency.get('target_id')!=request.target_id
                    or dependency.get('owner_id')!=request.owner_id
                    or dependency.get('operation_id')!=request.operation_id
                    or installed.get('native_upf_id')!=binding.native_upf_id
                    or installed.get('pod_uid')!=binding.pod_uid
                    or installed.get('kernel_epoch')!=binding.kernel_epoch
                    or installed.get('smf_boot_epoch')!=binding.smf_boot_epoch
                    or installed.get('version')!=binding.version or request.expected_version!=binding.version):
                raise AdmissionBlocked('Exact same-operation UPF dependency proof required')
            authority=self.reservation_reader(deferred.reservation_id,user_owner,request.owner_id,request.operation_id)
            if type(authority) is not DeferredAdmissionAuthority:
                raise AdmissionBlocked('Trusted deferred reservation authority missing')
            held=authority.reservation
            if (authority.user_owner!=user_owner or authority.intent_id!=request.owner_id
                    or authority.operation_id!=request.operation_id or not held.active(now)
                    or held.reservation_id!=deferred.reservation_id or held.version!=deferred.reservation_version
                    or deferred.identity.token not in authority.selected_identities or not deferred.active(now)
                    or deferred.valid_from<max(held.created_at,authority.valid_from)
                    or deferred.valid_until>min(held.valid_until,authority.valid_until)):
                raise AdmissionBlocked('Deferred admission reservation/nonce/validity changed')
            if self.api.inspect(deferred.identity.nonce) is not None:
                raise AdmissionBlocked('Admission already dispatched; native readback required')
            concrete.append(PendingAdmission(deferred.identity,deferred.owner_id,binding.native_upf_id,
                deferred.valid_from,deferred.valid_until))
        parameters={'mode':'new_sessions','pending_admissions':[p.to_dict() for p in concrete]}
        result=replace(request,parameters=parameters)
        if previous_request is not None and json.dumps(asdict(result),sort_keys=True)!=json.dumps(previous_request,sort_keys=True):
            raise AdmissionBlocked('Durable materialized admission changed; readback required')
        return result


@dataclass(frozen=True)
class NativeSessionAdmissionBinding:
    sm_context_ref: str
    established_epoch: str
    session_key: str
    created_at: float
    upf_id: str
    upf_node_ip: str
    installation_version: str

    def __post_init__(self):
        for name in ('sm_context_ref','established_epoch','session_key','upf_id','upf_node_ip','installation_version'):
            required(getattr(self,name),name)
        object.__setattr__(self,'created_at',timestamp(self.created_at))

    @classmethod
    def from_native(cls,value):
        try:
            return cls(value['smContextRef'],value['establishedEpoch'],value['session_key'],value['created_at'],value['upf_id'],value['upf_node_ip'],value['installation_version'])
        except (KeyError,TypeError) as error:
            raise AdmissionBlocked('Native session creation proof incomplete') from error


@dataclass(frozen=True)
class NativeAdmissionState:
    status: str
    version: str
    owner_id: str
    operation_id: str
    identity: AdmissionIdentity
    target_upf_id: str
    valid_from: float
    valid_until: float
    registered_at: float
    boot_epoch: str
    observed_at: float
    source: str
    binding: NativeSessionAdmissionBinding | None = None
    installation_version: str = ''

    def __post_init__(self):
        if self.status not in {'registered','selected','bound','expired','cancelled'}:
            raise AdmissionBlocked('Unknown native admission status')
        for name in ('version','owner_id','operation_id','target_upf_id','boot_epoch'):
            required(getattr(self,name),name)
        if self.source != 'native-smf-session-admission/v1':
            raise AdmissionBlocked('Native admission source unconfirmed')
        for name in ('valid_from','valid_until','registered_at','observed_at'):
            object.__setattr__(self,name,timestamp(getattr(self,name)))
        if not self.valid_from <= self.registered_at < self.valid_until:
            raise AdmissionBlocked('Native registration outside validity')
        if self.registered_at > self.observed_at:
            raise AdmissionBlocked('Native registration after observation')
        if self.status == 'bound' and self.binding is None:
            raise AdmissionBlocked('Bound status requires native creation proof')
        if not self.installation_version:
            original=self.binding.installation_version if self.binding else self.version if self.status=='registered' else ''
            required(original,'native installation version')
            object.__setattr__(self,'installation_version',original)

    @classmethod
    def from_native(cls,value):
        try:
            identity=AdmissionIdentity(value['network_scope'],value['supi'],value['pdu_session_id'],value['nonce'])
            binding=NativeSessionAdmissionBinding.from_native(value['binding']) if value.get('binding') else None
            return cls(value['status'],value['version'],value['owner_id'],value['operation_id'],identity,value['target_upf_id'],value['valid_from'],value['valid_until'],value['registered_at'],value['boot_epoch'],value['observed_at'],value['source'],binding,value.get('installation_version',''))
        except (KeyError,TypeError) as error:
            raise AdmissionBlocked('Native admission readback incomplete') from error


@dataclass(frozen=True)
class AdmissionInstallation:
    admission: PendingAdmission
    operation_id: str
    native_version: str
    native_registered_at: float
    native_boot_epoch: str

    def __post_init__(self):
        for name in ('operation_id','native_version','native_boot_epoch'):
            required(getattr(self,name),name)
        object.__setattr__(self,'native_registered_at',timestamp(self.native_registered_at))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        values=dict(value)
        values['admission']=PendingAdmission.from_dict(values['admission'])
        return cls(**values)


@dataclass(frozen=True)
class SessionAssignment:
    """A future admission is never represented by a fabricated established key."""
    target_id: str
    mode: str
    pending_admissions: tuple[PendingAdmission | DeferredPendingAdmission,...] = ()
    session_keys: tuple[str,...] = ()

    def __post_init__(self):
        required(self.target_id,'assignment target')
        object.__setattr__(self,'pending_admissions',tuple(self.pending_admissions))
        object.__setattr__(self,'session_keys',tuple(self.session_keys))
        if self.mode == 'new_sessions':
            if not self.pending_admissions or self.session_keys:
                raise AdmissionBlocked('New sessions require pending admissions, not established session keys')
            deferred=[type(p) is DeferredPendingAdmission for p in self.pending_admissions]
            if any(deferred) and not all(deferred):raise AdmissionBlocked('Mixed native/deferred admissions')
            targets={p.target_id if type(p) is DeferredPendingAdmission else p.target_upf_id for p in self.pending_admissions}
            identities={(p.identity.network_scope,p.identity.supi,p.identity.pdu_session_id) for p in self.pending_admissions}
            nonces={p.identity.nonce for p in self.pending_admissions}
            if len(targets)!=1 or len(identities)!=len(self.pending_admissions) or len(nonces)!=len(self.pending_admissions):
                raise AdmissionBlocked('Duplicate/ambiguous pending assignment cohort')
        elif self.mode == 'migrate':
            if self.pending_admissions or not self.session_keys or len(set(self.session_keys))!=len(self.session_keys):
                raise AdmissionBlocked('Migration requires exact existing session keys')
            for token in self.session_keys:
                try:
                    values=json.loads(token)
                    if not isinstance(values,list) or len(values)!=4 or SessionKey(*values).token!=token:
                        raise ValueError
                except (ValueError,TypeError):
                    raise AdmissionBlocked('Invalid existing session identity') from None
        else:
            raise AdmissionBlocked('Unsupported assignment mode')

    @classmethod
    def from_action(cls,action):
        if action.kind!='assign_sessions':
            raise AdmissionBlocked('Not a session assignment action')
        if set(action.parameters)-{'mode','pending_admissions'}:
            raise AdmissionBlocked('Unknown assignment parameters')
        pending=tuple((DeferredPendingAdmission if 'dependency_action_id' in v else PendingAdmission).from_dict(v)
            for v in action.parameters.get('pending_admissions',()))
        for p in pending:
            if type(p) is DeferredPendingAdmission and (p.target_id!=action.target_id or p.dependency_action_id not in action.depends_on):
                raise AdmissionBlocked('Deferred admission requires exact target/add dependency')
        return cls(action.target_id,action.parameters.get('mode','new_sessions'),pending,action.session_keys)

    def to_plan_action(self,action_id,expected_version,depends_on=(),timeout_seconds=60):
        from .planning import PlanAction
        parameters={'mode':self.mode}
        if self.pending_admissions:
            parameters['pending_admissions']=[p.to_dict() for p in self.pending_admissions]
        return PlanAction(action_id,'assign_sessions',self.target_id,parameters,self.session_keys,tuple(depends_on),expected_version,timeout_seconds)

    def validate_authority(self,*,owner_id,native_target_upf_id,now,migration_supported=False):
        if self.mode=='migrate':
            if migration_supported is not True:
                raise AdmissionBlocked('Existing session migration is unsupported')
            return
        for admission in self.pending_admissions:
            if type(admission) is DeferredPendingAdmission:
                raise AdmissionBlocked('Deferred admission must be materialized before native preflight')
            if admission.owner_id!=owner_id or admission.target_upf_id!=native_target_upf_id or not admission.active(now):
                raise AdmissionBlocked('Pending assignment owner/target/validity mismatch')


@dataclass(frozen=True)
class AdmissionBindingBatch:
    expected_count: int
    bindings: tuple[NativeSessionAdmissionBinding,...]
    pending_nonces: tuple[str,...]

    @property
    def complete(self):
        return not self.pending_nonces and len(self.bindings)==self.expected_count

    @property
    def session_keys(self):
        return tuple(b.session_key for b in self.bindings)


@dataclass(frozen=True)
class PreparedSessionAdmissions:
    traffic_request_id: str
    reservation_id: str
    reservation_version: str
    installations: tuple[AdmissionInstallation,...]
    selection_required: bool = True
    source: str = 'native-ue-reservation/v1'
    held_ue_allocation: dict | None = None
    pretraffic_plan: dict | None = None
    selected_identities: tuple[str,...] = ()

    def __post_init__(self):
        required(self.traffic_request_id,'traffic_request_id')
        object.__setattr__(self,'installations',tuple(self.installations))
        object.__setattr__(self,'selected_identities',tuple(self.selected_identities))
        if type(self.selection_required) is not bool:
            raise AdmissionBlocked('selection_required must be boolean')
        if self.selection_required:
            required(self.reservation_id,'reservation_id')
            required(self.reservation_version,'reservation_version')
            if self.source!='native-ue-reservation/v1' or not self.installations:
                raise AdmissionBlocked('Native UE reservation and installed admissions required')
            ids=[(h.admission.identity.network_scope,h.admission.identity.supi,h.admission.identity.pdu_session_id) for h in self.installations]
            if len(set(ids))!=len(ids) or len({h.admission.identity.nonce for h in self.installations})!=len(ids):
                raise AdmissionBlocked('Ambiguous reserved session cohort')
        elif self.installations:
            raise AdmissionBlocked('Unexpected admissions for not-required selection')
        if self.held_ue_allocation is not None:
            from backend.onprem.runtime.ue_reservation import HeldUEAllocation
            held=HeldUEAllocation.from_dict(self.held_ue_allocation)
            whole={AdmissionIdentity(held.network_scope,r.supi,r.pdu_session_id,r.nonce).token for r in held.allocation}
            installed={h.admission.identity.token for h in self.installations}
            selected=set(self.selected_identities)
            if (held.reservation_id!=self.reservation_id or held.version!=self.reservation_version
                    or (self.selection_required and (not selected or not selected<=whole or selected!=installed))
                    or (not self.selection_required and (selected or installed))):
                raise AdmissionBlocked('Held reservation differs from installed selections')

    def validate(self,intent,operation_id,traffic_request_id,now):
        if self.traffic_request_id!=traffic_request_id:
            raise AdmissionBlocked('UE reservation traffic request mismatch')
        for handle in self.installations:
            admission=handle.admission
            if (admission.owner_id!=intent.intent_id or handle.operation_id!=operation_id
                    or not admission.active(now) or admission.valid_from<intent.valid_from
                    or admission.valid_until>intent.valid_until):
                raise AdmissionBlocked('UE reservation owner/operation/validity mismatch')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        values=dict(value)
        values['installations']=tuple(AdmissionInstallation.from_dict(h) for h in values.get('installations',()))
        return cls(**values)


class AdmissionApi(Protocol):
    def inspect(self,nonce: str) -> NativeAdmissionState | None: ...
    def register(self,admission: PendingAdmission,operation_id: str,expected_version: str) -> NativeAdmissionState: ...
    def release(self,nonce: str,owner_id: str,operation_id: str,expected_version: str) -> NativeAdmissionState: ...


class PendingAdmissionCoordinator:
    """Native SMF owns CAS and creation hooks; Python verifies immutable lineage."""
    def __init__(self,api: AdmissionApi,*,clock=time.time,maximum_read_age_seconds=15,cleanup_authority=None):
        self.api,self.clock=api,clock
        self.cleanup_authority=cleanup_authority
        self.maximum_read_age_seconds=finite(maximum_read_age_seconds,'maximum read age',.001)

    def _verify_state(self,admission,state):
        if not isinstance(state,NativeAdmissionState):
            raise AdmissionBlocked('Typed native admission proof required')
        if (state.identity != admission.identity or state.owner_id != admission.owner_id
                or state.target_upf_id != admission.target_upf_id
                or state.valid_from != admission.valid_from or state.valid_until != admission.valid_until):
            raise AdmissionBlocked('Native admission identity/owner/policy mismatch')
        if not 0 <= self.clock()-state.observed_at <= self.maximum_read_age_seconds:
            raise AdmissionBlocked('Native admission proof stale')

    def install(self,admission,operation_id,expected_version='absent'):
        if not admission.active(self.clock()):
            raise AdmissionBlocked('Admission not active or expired')
        existing=self.api.inspect(admission.identity.nonce)
        if existing is None:
            if expected_version != 'absent':
                raise AdmissionBlocked('Admission CAS mismatch')
        else:
            self._verify_state(admission,existing)
            if existing.version != expected_version:
                raise AdmissionBlocked('Admission CAS mismatch')
            if existing.status != 'registered':
                raise AdmissionBlocked('Consumed/expired nonce cannot be reinstalled')
            if existing.operation_id != operation_id:
                raise AdmissionBlocked('Admission belongs to another operation')
        state=self.api.register(admission,operation_id,expected_version)
        self._verify_state(admission,state)
        if state.operation_id != operation_id or state.status not in {'registered','selected','bound'}:
            raise AdmissionBlocked('Native registration not confirmed')
        # A creation race may already return bound; preserve original installation revision.
        version=state.installation_version
        installation=AdmissionInstallation(admission,operation_id,version,state.registered_at,state.boot_epoch)
        if state.status == 'bound':
            self._binding(installation,state)
        return installation

    def recover_installation(self,admission,operation_id,expected_boot_epoch=None):
        """Read back an uncertain registration; never resubmit or consume a nonce."""
        state=self.api.inspect(admission.identity.nonce)
        if state is None:
            raise AdmissionBlocked('Native admission registration not observed')
        self._verify_state(admission,state)
        if state.operation_id!=operation_id or state.status not in {'registered','selected','bound'}:
            raise AdmissionBlocked('Native admission recovery lineage/status mismatch')
        if expected_boot_epoch and state.boot_epoch!=expected_boot_epoch:
            raise AdmissionBlocked('Native admission recovery boot mismatch')
        handle=AdmissionInstallation(admission,operation_id,state.installation_version,state.registered_at,state.boot_epoch)
        if state.status=='bound':
            self._binding(handle,state)
        return handle

    def recover_for_release(self,admission,operation_id,expected_boot_epoch=None):
        """Read-only recovery of an owned unknown registration for cleanup.

        Expired/cancelled state grants no new admission or session authority.
        It only supplies immutable lineage needed to close the original nonce.
        """
        state=self.api.inspect(admission.identity.nonce)
        if state is None:raise AdmissionBlocked('Native admission cleanup registration not observed')
        self._verify_state(admission,state)
        if (state.operation_id!=operation_id
                or state.status not in {'registered','selected','bound','expired','cancelled'}
                or expected_boot_epoch and state.boot_epoch!=expected_boot_epoch):
            raise AdmissionBlocked('Native admission cleanup recovery lineage mismatch')
        handle=AdmissionInstallation(admission,operation_id,state.installation_version,state.registered_at,state.boot_epoch)
        if state.status=='bound':self._binding(handle,state)
        return handle

    def _binding(self,installation,state):
        admission=installation.admission
        self._verify_state(admission,state)
        if state.boot_epoch != installation.native_boot_epoch or state.operation_id != installation.operation_id:
            raise AdmissionBlocked('SMF boot/operation lineage changed')
        if state.registered_at != installation.native_registered_at:
            raise AdmissionBlocked('Native registration lineage changed')
        if state.status != 'bound':
            return None
        binding=state.binding
        if binding.established_epoch != binding.sm_context_ref:
            raise AdmissionBlocked('Native session incarnation/ref mismatch')
        if binding.installation_version != installation.native_version or state.installation_version!=installation.native_version:
            raise AdmissionBlocked('Binding installation version mismatch')
        if (binding.upf_id != admission.target_upf_id or
                not max(admission.valid_from,installation.native_registered_at) <= binding.created_at < admission.valid_until
                or binding.created_at > state.observed_at):
            raise AdmissionBlocked('Not a new session created under this admission')
        expected=SessionKey(admission.identity.network_scope,admission.identity.supi,admission.identity.pdu_session_id,binding.established_epoch)
        if binding.session_key != expected.token:
            raise AdmissionBlocked('Native session binding identity mismatch')
        return binding

    def read_binding(self,installation):
        state=self.api.inspect(installation.admission.identity.nonce)
        if state is None:
            raise AdmissionBlocked('Native admission disappeared; recovery required')
        return self._binding(installation,state)

    def read_bindings(self,installations):
        if len({p.admission.identity.token for p in installations})!=len(installations):
            raise AdmissionBlocked('Duplicate admission installation')
        bindings,pending=[],[]
        for installation in installations:
            bound=self.read_binding(installation)
            if bound is None:
                pending.append(installation.admission.identity.nonce)
            else:
                bindings.append(bound)
        return AdmissionBindingBatch(len(installations),tuple(bindings),tuple(pending))

    def release(self,installation,expected_version):
        current=self.api.inspect(installation.admission.identity.nonce)
        if current is None:
            raise AdmissionBlocked('Native admission disappeared; recovery required')
        self._verify_state(installation.admission,current)
        if current.version != expected_version or current.boot_epoch != installation.native_boot_epoch:
            raise AdmissionBlocked('Admission release CAS/boot mismatch')
        if current.operation_id!=installation.operation_id or current.registered_at!=installation.native_registered_at:
            raise AdmissionBlocked('Admission release lineage changed')
        if current.status=='cancelled':
            if self.cleanup_authority is not None:
                self.cleanup_authority.confirm_cancelled(installation,current)
            return current
        permit=(self.cleanup_authority.before_release(installation,current)
                if self.cleanup_authority is not None else None)
        try:
            state=self.api.release(current.identity.nonce,current.owner_id,installation.operation_id,expected_version)
        except AdmissionRequestNotSent:
            if self.cleanup_authority is not None:self.cleanup_authority.mark_not_sent(permit)
            raise
        except BaseException:
            if self.cleanup_authority is not None:self.cleanup_authority.mark_unknown(permit)
            raise
        self._verify_state(installation.admission,state)
        if state.status != 'cancelled':
            if self.cleanup_authority is not None:self.cleanup_authority.mark_unknown(permit)
            raise AdmissionBlocked('Admission cancellation not confirmed')
        if self.cleanup_authority is not None:self.cleanup_authority.confirm_cancelled(installation,state)
        # Cancellation only stops future selection. It never migrates/releases a PDU session.
        return state


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


class FixedOriginSmfAdmissionApi:
    prefix='/nsmf-oam/v1/session-admissions'

    def __init__(self,origin,*,token_reader,timeout=5,opener=None):
        parsed=urlsplit(origin)
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {'','/'} or parsed.query or parsed.fragment or not 0 < timeout <= 10):
            raise AdmissionBlocked('Fixed SMF origin and bounded timeout required')
        self.origin,self.token_reader,self.timeout=origin.rstrip('/'),token_reader,timeout
        self.opener=opener or build_opener(ProxyHandler({}),_NoRedirect())

    def _call(self,method,path,payload=None,*,missing_ok=False):
        token=self.token_reader()
        if not isinstance(token,str) or not token:
            raise AdmissionBlocked('SMF admission credential not configured')
        request=Request(self.origin+path,data=json.dumps(payload,allow_nan=False).encode() if payload is not None else None,
                        method=method,headers={'Content-Type':'application/json','X-SMF-QER-Actuator-Token':token})
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                body=response.read(256_001)
                if response.status not in {200,201} or len(body)>256_000:
                    raise ValueError
            return NativeAdmissionState.from_native(json.loads(body))
        except AdmissionRequestNotSent as error:
            if error.matches(request):raise
            raise AdmissionBlocked('Native admission outcome requires readback') from None
        except HTTPError as error:
            if missing_ok and error.code==404:
                return None
            raise AdmissionBlocked('Native admission outcome requires readback') from None
        except Exception:
            raise AdmissionBlocked('Native admission outcome requires readback') from None

    def inspect(self,nonce):
        AdmissionIdentity('validation','validation',1,nonce)
        return self._call('GET',self.prefix+'/'+quote(nonce,safe=''),missing_ok=True)

    def register(self,admission,operation_id,expected_version):
        return self._call('POST',self.prefix,admission.to_native_request(operation_id,expected_version))

    def release(self,nonce,owner_id,operation_id,expected_version):
        AdmissionIdentity('validation','validation',1,nonce)
        for name,value in (('owner_id',owner_id),('operation_id',operation_id),('expected_version',expected_version)):
            required(value,name)
        return self._call('POST',self.prefix+'/'+quote(nonce,safe='')+'/release',{'ownerId':owner_id,'operationId':operation_id,'expectedVersion':expected_version})
