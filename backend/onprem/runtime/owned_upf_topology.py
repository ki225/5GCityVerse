"""Immutable pre-stop physical topology, distinct from permanent protected NFs."""
from dataclasses import asdict,dataclass
import hashlib
import ipaddress
import json
import math
import re
from uuid import UUID

from backend.onprem.intents.commissioned_upfs import UpfEvidenceRoute


@dataclass(frozen=True)
class OwnedUpfMember:
    logical_target:str
    registry_target_id:str
    pod_name:str
    pod_uid:str
    process_identity:str
    native_upf_id:str
    node_ip:str
    smf_epoch:str
    kernel_epoch:str
    evidence_route:UpfEvidenceRoute
    owner_id:str
    operation_id:str
    journal_revision:int
    version:str
    state:str
    registered:bool
    admission_ready:bool

    def __post_init__(self):
        for name in ('logical_target','registry_target_id','pod_name'):
            if not isinstance(getattr(self,name),str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,62}',getattr(self,name)):
                raise ValueError('Exact native topology target required')
        for name in ('pod_uid','process_identity','smf_epoch','kernel_epoch','version','state'):
            value=getattr(self,name)
            if not isinstance(value,str) or not value or len(value)>4096 or any(ord(c)<32 for c in value):
                raise ValueError('Complete physical UPF incarnation required')
        if str(UUID(self.native_upf_id))!=self.native_upf_id:raise ValueError('Canonical UPF UUID required')
        ipaddress.ip_address(self.node_ip)
        route=self.evidence_route if type(self.evidence_route) is UpfEvidenceRoute else UpfEvidenceRoute(**self.evidence_route)
        if route.pod_name!=self.pod_name:raise ValueError('Exact topology Pod proxy route required')
        object.__setattr__(self,'evidence_route',route)
        identity=json.loads(self.process_identity)
        if (not isinstance(identity,list) or len(identity)!=3 or identity[0]!=self.pod_uid
                or not all(isinstance(x,str) and x for x in identity)):
            raise ValueError('Exact Pod/container/process UPF identity required')
        if (type(self.registered) is not bool or type(self.admission_ready) is not bool
                or type(self.journal_revision) is not int or self.journal_revision<0
                or not isinstance(self.owner_id,str) or not isinstance(self.operation_id,str)
                or bool(self.owner_id)!=bool(self.operation_id)):
            raise ValueError('Complete lifecycle authority required')
        if not self.registered and not self.owner_id:raise ValueError('Inactive cleanup UPF must have exact owner')


@dataclass(frozen=True)
class OwnedUpfTopology:
    members:tuple[OwnedUpfMember,...]
    registry_revision:str
    observed_at:float
    source:str='owned-native-upf-topology/v1'

    def __post_init__(self):
        object.__setattr__(self,'members',tuple(self.members))
        if (self.source!='owned-native-upf-topology/v1' or not 1<=len(self.members)<=64
                or not all(type(r) is OwnedUpfMember for r in self.members)
                or not any(r.registered for r in self.members)
                or not isinstance(self.registry_revision,str) or not re.fullmatch('[0-9a-f]{64}',self.registry_revision)
                or type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at)):
            raise ValueError('Complete bounded attested teardown topology required')
        for name in ('logical_target','registry_target_id','native_upf_id','node_ip','pod_uid','pod_name'):
            values=[getattr(r,name) for r in self.members]
            if len(set(values))!=len(values):raise ValueError('Topology physical alias is forbidden')

    @property
    def identity(self):
        value=self.to_dict();value.pop('observed_at')
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    @property
    def registered(self):return tuple(r for r in self.members if r.registered)

    @property
    def cleanup(self):return tuple(r for r in self.members if not r.registered)

    def to_dict(self):return asdict(self)

    @classmethod
    def from_dict(cls,value):
        raw=dict(value);raw['members']=tuple(OwnedUpfMember(**r) for r in raw['members'])
        return cls(**raw)


class CurrentOwnedTopology:
    """One authoritative registry pair, original process clocks, no stored history."""
    def __init__(self,source,expected=None):
        from .owned_native_teardown import TeardownBlocked
        self.source=source;self.scope=source.current_upfs.proof();self.expected=expected
        self.rows=dict(self.scope.rows)
        for row in self.scope.snapshot.observations:
            if row.native_registered:continue
            if row.binding is not None:self.rows[row.slot.target_id]=row
            elif row.inflight_pod_uid or row.state not in {'reserved','retired'}:
                # Pod loss is not proof that journal-owned resources/kernel
                # state disappeared. Only the producer's verified no-resource
                # states may leave the immutable physical cleanup scope.
                raise TeardownBlocked('Unresolved owned lifecycle has no complete native incarnation pin')
        self.logical={}
        for target,row in self.rows.items():
            matches=[b for b in source.upfs if b.census_target_id==target]
            if matches:
                configured=matches[0]
                if (configured.native_upf_id,configured.node_ip)!=(row.binding.native_upf_id,row.binding.native_node_ip):
                    raise TeardownBlocked('Original commissioned native UPF changed')
                self.logical[target]=configured.logical_target
            else:self.logical[target]=target
            route=row.slot.evidence_route
            if (route is None or (route.context,route.namespace)!=(source.driver.context,source.driver.namespace)):
                raise TeardownBlocked('Current teardown topology route scope changed')
        if not {b.census_target_id for b in source.upfs}<={r.slot.target_id for r in self.scope.rows.values()}:
            raise TeardownBlocked('Original commissioned UPF membership is incomplete')
        self.pin=None

    def capture_processes(self,protected,protected_at):
        from .owned_native_teardown_adapter import ProtectedNf,_fresh
        from .owned_native_teardown import TeardownBlocked
        identities=dict(protected);extra=[]
        for target,row in self.rows.items():
            logical=self.logical[target]
            if logical not in identities:extra.append(ProtectedNf(logical,row.slot.pod_name,'upf',1))
        extra_at=protected_at
        if extra:
            extra_identities,_,extra_at=self.source.nf_identities(selectors=tuple(extra))
            identities.update(extra_identities)
        self.extra_selectors=tuple(extra)
        self.extra_identities=tuple(extra_identities) if extra else ()
        members=[]
        for target,row in sorted(self.rows.items()):
            b=row.binding;identity=identities[self.logical[target]]
            if json.loads(identity)[0]!=b.pod_uid:raise TeardownBlocked('Topology process Pod UID differs from registry')
            members.append(OwnedUpfMember(self.logical[target],target,row.slot.pod_name,b.pod_uid,identity,
                b.native_upf_id,b.native_node_ip,b.smf_boot_epoch,b.kernel_epoch,asdict(row.slot.evidence_route),
                b.owner_id or '',b.operation_id or '',b.journal_revision,b.version,row.state,row.native_registered,b.admission_ready))
        self.pin=OwnedUpfTopology(tuple(members),self.scope.revision,
            min(protected_at,extra_at,self.scope.snapshot.observed_at,*[r.binding.observed_at for r in self.rows.values()]))
        if self.expected is not None and self.pin.identity!=self.expected.identity:
            raise TeardownBlocked('Immutable pre-stop UPF topology or lifecycle authority changed')
        _fresh(self.pin.observed_at,self.source.clock())
        return self.pin

    def finish(self):
        from .owned_native_teardown_adapter import _fresh
        from .owned_native_teardown import TeardownBlocked
        if self.pin is None:raise TeardownBlocked('Complete topology process proof is missing')
        if self.extra_selectors:
            current,_,at=self.source.nf_identities(selectors=self.extra_selectors)
            if current!=self.extra_identities:raise TeardownBlocked('Added UPF process incarnation changed during proof')
            _fresh(at,self.source.clock())
        self.scope.finish(maximum_age_seconds=10)
        _fresh(self.pin.observed_at,self.source.clock())
        return self.pin

    def current_target(self,logical):
        return next(target for target,name in self.logical.items() if name==logical)
