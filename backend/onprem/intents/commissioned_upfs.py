"""Immutable server-owned UPF slots and current incarnation projections.

This is a projection of the lifecycle journal, not a second ownership ledger.
Reserved slots are configuration, never claims that native resources exist.
"""
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
import re
import uuid

from .domain import ContractError


def _name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',value):
        raise ContractError('invalid_commissioned_upf_name')
    return value


def _required(value):
    if not isinstance(value,str) or not value or len(value)>1024:
        raise ContractError('incomplete_commissioned_upf_identity')
    return value


@dataclass(frozen=True)
class UpfEvidenceRoute:
    context: str
    namespace: str
    pod_name: str
    strategy: str = 'kubernetes-pod-proxy'
    port: int = 9081

    def __post_init__(self):
        _required(self.context);_name(self.namespace);_name(self.pod_name)
        if self.strategy!='kubernetes-pod-proxy' or self.port!=9081:
            raise ContractError('uncommissioned_upf_evidence_strategy')

    @property
    def path(self):
        return f'/api/v1/namespaces/{self.namespace}/pods/{self.pod_name}:9081/proxy/v1/upf-state'


@dataclass(frozen=True)
class CommissionedUpfSlot:
    target_id: str
    pod_name: str
    native_node_ip: str
    template_id: str = ''
    evidence_target: str = ''
    slice_instance_id: str = ''
    evidence_route: UpfEvidenceRoute | None = None
    ue_prefixes: tuple[str,...] = ()

    def __post_init__(self):
        _name(self.target_id);_name(self.pod_name)
        ipaddress.IPv4Address(self.native_node_ip)
        if self.template_id:_name(self.template_id)
        if self.evidence_target:_name(self.evidence_target)
        if self.evidence_target and self.evidence_target!=self.target_id:
            raise ContractError('commissioned_upf_exporter_alias_forbidden')
        object.__setattr__(self,'ue_prefixes',tuple(self.ue_prefixes))
        for prefix in self.ue_prefixes:ipaddress.ip_network(prefix,strict=True)
        if self.evidence_route is not None and (type(self.evidence_route) is not UpfEvidenceRoute
                or self.evidence_route.pod_name!=self.pod_name):
            raise ContractError('commissioned_upf_evidence_pod_mismatch')


@dataclass(frozen=True)
class ActiveUpfBinding:
    target_id: str
    pod_uid: str
    native_upf_id: str
    native_node_ip: str
    smf_boot_epoch: str
    kernel_epoch: str
    owner_id: str
    operation_id: str
    journal_revision: int
    version: str
    observed_at: float
    admission_ready: bool = False

    def __post_init__(self):
        _name(self.target_id)
        for field in ('pod_uid','smf_boot_epoch','kernel_epoch','version'):
            _required(getattr(self,field))
        if str(uuid.UUID(self.native_upf_id))!=self.native_upf_id:
            raise ContractError('canonical_native_upf_uuid_required')
        ipaddress.IPv4Address(self.native_node_ip)
        if (type(self.journal_revision) is not int or self.journal_revision<0
                or type(self.observed_at) not in (float,int) or not math.isfinite(self.observed_at)
                or type(self.admission_ready) is not bool):
            raise ContractError('invalid_commissioned_upf_revision_or_clock')
        if bool(self.owner_id)!=bool(self.operation_id):
            raise ContractError('incomplete_commissioned_upf_owner')

    def to_dict(self):return asdict(self)


@dataclass(frozen=True)
class CommissionedUpfObservation:
    slot: CommissionedUpfSlot
    state: str
    binding: ActiveUpfBinding | None = None
    inflight_pod_uid: str = ''
    native_registered: bool = False

    def __post_init__(self):
        if self.state not in {'reserved','deploying','registered','draining','retired','recovery_required'}:
            raise ContractError('invalid_commissioned_upf_state')
        if self.binding and (self.binding.target_id!=self.slot.target_id
                or self.binding.native_node_ip!=self.slot.native_node_ip):
            raise ContractError('commissioned_upf_slot_binding_mismatch')
        if self.state=='registered' and self.binding is None:
            raise ContractError('registered_upf_binding_required')
        if (type(self.native_registered) is not bool or (self.native_registered and self.binding is None)
                or (self.state=='registered' and not self.native_registered)):
            raise ContractError('registered_upf_census_proof_required')
        if self.binding and self.binding.admission_ready and self.state!='registered':
            raise ContractError('nonregistered_upf_admission_forbidden')


@dataclass(frozen=True)
class RegistrySnapshot:
    observations: tuple[CommissionedUpfObservation,...]
    observed_at: float
    maximum_age_seconds: float = 15

    def __post_init__(self):
        object.__setattr__(self,'observations',tuple(self.observations))
        rows=self.observations
        if (not 1<=len(rows)<=64 or len({r.slot.target_id for r in rows})!=len(rows)
                or len({r.slot.native_node_ip for r in rows})!=len(rows)
                or len({r.slot.pod_name for r in rows})!=len(rows)
                or type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at)
                or not 0<self.maximum_age_seconds<=30):
            raise ContractError('ambiguous_or_unbounded_commissioned_upf_registry')
        bindings=self.observation_bindings()
        for field in ('native_upf_id','pod_uid'):
            values=[getattr(b,field) for b in bindings]
            if len(set(values))!=len(values):raise ContractError('commissioned_upf_incarnation_alias')
        prefixes=[ipaddress.ip_network(p,strict=True) for r in rows for p in r.slot.ue_prefixes]
        if any(a.overlaps(b) for i,a in enumerate(prefixes) for b in prefixes[i+1:]):
            raise ContractError('commissioned_upf_prefix_overlap')

    @property
    def revision(self):
        # Sample time is freshness, not identity; do not renew incarnation pins.
        payload=asdict(self)
        payload.pop('observed_at')
        for row in payload['observations']:
            if row['binding']:row['binding'].pop('observed_at')
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def require_fresh(self,now):
        clocks=[self.observed_at,*[r.binding.observed_at for r in self.observations if r.binding]]
        if any(not 0<=now-at<=self.maximum_age_seconds for at in clocks):
            raise ContractError('commissioned_upf_registry_stale')

    def observation_bindings(self):
        return tuple(r.binding for r in self.observations if r.native_registered)

    def cleanup_bindings(self):
        return tuple(r.binding for r in self.observations if r.binding is not None)

    def admission_binding(self,target_id,*,owner_id,operation_id,now):
        self.require_fresh(now)
        rows=[r for r in self.observations if r.slot.target_id==target_id]
        if len(rows)!=1 or rows[0].binding is None:
            raise ContractError('commissioned_upf_admission_binding_missing')
        row=rows[0];binding=row.binding
        if (row.state!='registered' or not binding.admission_ready
                or binding.owner_id!=owner_id or binding.operation_id!=operation_id):
            raise ContractError('commissioned_upf_admission_authority_changed')
        return binding

    def to_dict(self):return dict(schema='cityverse.commissioned-upfs/v1',revision=self.revision,**asdict(self))


class CommissionedUpfRegistry:
    """Read-through port; only the trusted lifecycle producer supplies snapshots."""
    def __init__(self,reader):self.reader=reader
    def snapshot(self):
        value=self.reader()
        if type(value) is not RegistrySnapshot:
            raise ContractError('typed_commissioned_upf_registry_required')
        return value
