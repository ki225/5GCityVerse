"""Private, journalled standard-SBI fallback after an owned UE is quiescent.

This port deliberately requires a fleet drain: complete SMF and all four native
UPF kernel views must be empty before orphan AM-policy removal can occur.
An explicitly owned live AMF context is removed only after its children vanish.
Its result covers core contexts only. The caller must still retire the real Pod
and prove RAN context disappearance through the existing full release proof.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
from typing import Callable, ContextManager, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, HTTPHandler, HTTPSHandler
import http.client
import io
import ipaddress
from uuid import UUID


class TeardownBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedDrainBaseline:
    """Immutable accepted-V2 pre-stop authority, never captured after NAS."""
    round_id:str
    lease:dict
    permit:dict
    original_generation:int
    allocation_sha256:str
    snapshot_sha256:str
    targets_sha256:str
    configuration_seal:str
    source:str='owned-native-drain-baseline/v1'
    upf_topology:object=None

    def __post_init__(self):
        if self.upf_topology is not None:
            from .owned_upf_topology import OwnedUpfTopology
            if isinstance(self.upf_topology,dict):object.__setattr__(self,'upf_topology',OwnedUpfTopology.from_dict(self.upf_topology))
            if type(self.upf_topology) is not OwnedUpfTopology:raise ValueError('Typed immutable topology pin required')
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficDrainPermit
        lease=ServiceIntentTrafficLease.from_dict(self.lease)
        permit=IntentTrafficDrainPermit(**self.permit)
        permit.validate(lease,float(permit.observed_at))
        if (self.source!='owned-native-drain-baseline/v1' or type(self.original_generation) is not int
                or self.original_generation<1):
            raise ValueError('Exact original owned drain baseline required')
        object.__setattr__(self,'lease',lease.to_dict())
        object.__setattr__(self,'permit',permit.to_dict())
        for value in (self.round_id,self.configuration_seal):_identifier(value)
        for value in (self.allocation_sha256,self.snapshot_sha256,self.targets_sha256):
            if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
                raise ValueError('Exact owned drain baseline digest required')

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(),sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def to_dict(self):
        value=asdict(self)
        if self.upf_topology is None:value.pop('upf_topology')
        return value


@dataclass(frozen=True)
class OwnedZeroGenerationStopReceipt:
    """Issued atomically with a normal owned zero write, not a gen+1 guess."""
    baseline:OwnedDrainBaseline
    zero_generation:int
    source:str='owned-zero-generation-stop/v1'

    def __post_init__(self):
        if (type(self.baseline) is not OwnedDrainBaseline or self.source!='owned-zero-generation-stop/v1'
                or type(self.zero_generation) is not int or self.zero_generation<=self.baseline.original_generation):
            raise ValueError('Typed owned zero generation receipt required')

    def to_dict(self):return dict(baseline=self.baseline.to_dict(),zero_generation=self.zero_generation,source=self.source)

    @classmethod
    def from_dict(cls,value):
        values=dict(value);values['baseline']=OwnedDrainBaseline(**values['baseline'])
        return cls(**values)


def _identifier(value):
    if not isinstance(value,str) or not value or len(value)>2048 or any(ord(c)<32 for c in value):
        raise ValueError('Invalid native teardown identity')


def _native_uuid(value):
    if not isinstance(value,str) or str(UUID(value))!=value:
        raise ValueError('Exact canonical native UPF UUID is required')


@dataclass(frozen=True)
class CommissionedUpfBinding:
    logical_target: str
    native_upf_id: str
    node_ip: str
    upf_epoch: str

    def __post_init__(self):
        if self.logical_target not in {'embb','urllc','mmtc','v2x'} and type(self) is not PinnedCommissionedUpfBinding:
            raise ValueError('Unknown commissioned UPF target')
        for name in ('native_upf_id','upf_epoch'):
            _identifier(getattr(self,name))
        _native_uuid(self.native_upf_id)
        ipaddress.ip_address(self.node_ip)


class PinnedCommissionedUpfBinding(CommissionedUpfBinding):
    """Accepted only with the separately validated immutable target topology."""
    pass


@dataclass(frozen=True)
class OwnedSession:
    session_key: str
    pdu_session_id: int
    sm_context_ref: str
    smf_epoch: str
    upf_id: str
    upf_epoch: str
    native_upf_id: str
    upf_node_ip: str
    native_owner_id: str | None
    native_operation_id: str | None
    version: str
    pfcp_seid: str

    def __post_init__(self):
        for name in ('session_key','sm_context_ref','smf_epoch','upf_id','upf_epoch','native_upf_id','upf_node_ip'):
            _identifier(getattr(self,name))
        if self.upf_id not in {'embb','urllc','mmtc','v2x'} and type(self) is not PinnedOwnedSession:
            raise ValueError('Session requires an explicit commissioned logical UPF target')
        _native_uuid(self.native_upf_id)
        ipaddress.ip_address(self.upf_node_ip)
        for name in ('native_owner_id','native_operation_id'):
            value=getattr(self,name)
            if value in (None,''):
                object.__setattr__(self,name,None)
            else:
                _identifier(value)
        if not isinstance(self.version,str) or re.fullmatch('[0-9a-f]{64}',self.version) is None:
            raise ValueError('Exact native session version digest is required')
        if (not isinstance(self.pfcp_seid,str) or not self.pfcp_seid.isdecimal()
                or str(int(self.pfcp_seid))!=self.pfcp_seid or not 0<int(self.pfcp_seid)<2**64):
            raise ValueError('Exact native PFCP SEID is required')
        if type(self.pdu_session_id) is not int or not 1<=self.pdu_session_id<=15:
            raise ValueError('Invalid PDU session ID')
        try:
            key=json.loads(self.session_key)
            if (not isinstance(key,list) or len(key)!=4 or type(key[2]) is not int
                    or key[2]!=self.pdu_session_id or self.session_key!=json.dumps(key,separators=(',',':'))):
                raise ValueError()
            for index in (0,1,3):
                _identifier(key[index])
        except (TypeError,ValueError):
            raise ValueError('Canonical session identity must bind scope, SUPI, PDU and establishment epoch') from None

    @classmethod
    def from_native_row(cls,row,*,smf_epoch,upf_bindings):
        """Bind the actual complete native census fields, not a caller-supplied opaque key."""
        try:
            expected=[row['networkScope'],row['supi'],row['sessionId'],row['establishedEpoch']]
            if row['smf_boot_epoch']!=smf_epoch or row['session_key']!=json.dumps(expected,separators=(',',':')):
                raise ValueError()
            bindings=tuple(upf_bindings)
            if len({b.logical_target for b in bindings})!=len(bindings) or len({b.native_upf_id for b in bindings})!=len(bindings) or len({b.node_ip for b in bindings})!=len(bindings):
                raise ValueError()
            matches=[b for b in bindings if (b.native_upf_id,b.node_ip,b.upf_epoch)==(row['upf_id'],row['upf_node_ip'],row['upfEpoch'])]
            if len(matches)!=1:
                raise ValueError()
            return cls(row['session_key'],row['sessionId'],row['smContextRef'],smf_epoch,
                matches[0].logical_target,row['upfEpoch'],row['upf_id'],row['upf_node_ip'],
                row['owner_id'],row['operation_id'],row['version'],row['pfcpSeid'])
        except (KeyError,TypeError,ValueError):
            raise ValueError('Native session census identity or epoch is inconsistent') from None


class PinnedOwnedSession(OwnedSession):
    """Dynamic UPF identity still requires exact membership in target topology."""
    pass


@dataclass(frozen=True)
class OwnedAmfContext:
    supi: str
    amf_epoch: str
    amf_ue_ngap_id: str
    ran_ue_ngap_id: str
    access_type: str
    sm_context_refs: tuple[str,...]
    provenance: str

    def __post_init__(self):
        for name in ('supi','amf_epoch','amf_ue_ngap_id','ran_ue_ngap_id'):
            _identifier(getattr(self,name))
        if not self.amf_ue_ngap_id.isdecimal() or not self.ran_ue_ngap_id.isdecimal():
            raise ValueError('Exact native NGAP identifiers are required')
        if self.access_type!='3GPP_ACCESS' or self.provenance!='amf-active-ue-context':
            raise ValueError('Supported native AMF access and provenance are required')
        object.__setattr__(self,'sm_context_refs',tuple(self.sm_context_refs))
        if len(set(self.sm_context_refs))!=len(self.sm_context_refs) or len(self.sm_context_refs)>30:
            raise ValueError('Ambiguous AMF child context references')
        for ref in self.sm_context_refs:
            _identifier(ref)

    @classmethod
    def from_native_row(cls,row,*,amf_epoch):
        """amf_epoch comes from the protected Pod/container/process, never nfInstanceId."""
        try:
            observation=row['ueAmbrObservation']
            if row['Supi']!=observation['supi'] or row['AccessType']!=observation['accessType']:
                raise ValueError()
            return cls(row['Supi'],amf_epoch,observation['amfUeNgapId'],observation['ranUeNgapId'],
                row['AccessType'],tuple(s['SmContextRef'] for s in row['PduSessions']),observation['source'])
        except (KeyError,TypeError,ValueError):
            raise ValueError('Native AMF ownership identity is inconsistent') from None


@dataclass(frozen=True)
class OwnedAssociation:
    association_id: str
    pcf_epoch: str

    def __post_init__(self):
        _identifier(self.association_id)
        _identifier(self.pcf_epoch)

    @classmethod
    def from_native_oam_row(cls,row,*,pcf_epoch,queried_supi,expected_supi):
        """Ownership comes from exact SUPI-filtered OAM; resource GET need not contain SUPI."""
        if queried_supi!=expected_supi or row.get('AccessType')!='3GPP_ACCESS':
            raise ValueError('PCF OAM scope or access type does not match owned UE')
        return cls(row['PolicyAssociationID'],pcf_epoch)


@dataclass(frozen=True)
class OwnedTeardownTarget:
    operation_id: str
    lease_id: str
    owner_id: str
    generation: int
    supi: str
    pod_uid: str
    namespace: str
    container_id: str
    process_epoch: str
    configuration_seal: str
    protected_nf_identities: tuple[tuple[str,str],...]
    sessions: tuple[OwnedSession,...]
    associations: tuple[OwnedAssociation,...]
    amf_context: OwnedAmfContext | None = None
    upf_topology:object=None

    def __post_init__(self):
        if self.upf_topology is not None:
            from .owned_upf_topology import OwnedUpfTopology
            if isinstance(self.upf_topology,dict):object.__setattr__(self,'upf_topology',OwnedUpfTopology.from_dict(self.upf_topology))
            if type(self.upf_topology) is not OwnedUpfTopology:raise ValueError('Typed immutable topology pin required')
            allowed={r.logical_target:r for r in self.upf_topology.registered}
            if any(s.upf_id not in allowed or (s.native_upf_id,s.upf_node_ip,s.upf_epoch,s.smf_epoch)!=(
                    allowed[s.upf_id].native_upf_id,allowed[s.upf_id].node_ip,allowed[s.upf_id].kernel_epoch,allowed[s.upf_id].smf_epoch)
                    for s in self.sessions):raise ValueError('Session is outside immutable registered topology')
        elif any(s.upf_id not in {'embb','urllc','mmtc','v2x'} for s in self.sessions):
            raise ValueError('Dynamic target requires immutable topology pin')
        for name in ('operation_id','lease_id','owner_id','supi','pod_uid','namespace','container_id','process_epoch','configuration_seal'):
            _identifier(getattr(self,name))
        if type(self.generation) is not int or self.generation<0:
            raise ValueError('Invalid generation')
        object.__setattr__(self,'sessions',tuple(self.sessions))
        object.__setattr__(self,'associations',tuple(self.associations))
        object.__setattr__(self,'protected_nf_identities',tuple(tuple(v) for v in self.protected_nf_identities))
        names={k for k,v in self.protected_nf_identities}
        if not {'amf','smf','pcf','embb','urllc','mmtc','v2x'}<=names or len(names)!=len(self.protected_nf_identities):
            raise ValueError('Complete protected native NF identity fence is required')
        for key,value in self.protected_nf_identities:
            _identifier(key)
            _identifier(value)
        if not 1<=len(self.sessions)<=30 or len({s.session_key for s in self.sessions})!=len(self.sessions):
            raise ValueError('Owned session census must be nonempty and unique')
        if len({s.sm_context_ref for s in self.sessions})!=len(self.sessions):
            raise ValueError('Duplicate SM context')
        if any(json.loads(s.session_key)[1]!=self.supi for s in self.sessions):
            raise ValueError('Native session SUPI does not match owned target')
        if any(s.native_owner_id is not None or s.native_operation_id is not None for s in self.sessions):
            raise ValueError('Native policy ownership must be restored before teardown baseline is pinned')
        if len({a.association_id for a in self.associations})!=len(self.associations):
            raise ValueError('Duplicate policy association')
        if self.amf_context is not None:
            if self.amf_context.supi!=self.supi or not set(self.amf_context.sm_context_refs)<={s.sm_context_ref for s in self.sessions}:
                raise ValueError('AMF context does not belong to the owned UE session census')

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(),sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def to_dict(self):
        value=asdict(self)
        if self.upf_topology is None:value.pop('upf_topology')
        return value

    @property
    def identity(self):
        value=[self.lease_id,self.generation,self.supi,self.pod_uid]
        return hashlib.sha256(json.dumps(value,separators=(',',':')).encode()).hexdigest()

    @classmethod
    def from_dict(cls,value):
        values=dict(value)
        session_type=PinnedOwnedSession if values.get('upf_topology') is not None else OwnedSession
        values['sessions']=tuple(session_type(**s) for s in values['sessions'])
        values['associations']=tuple(OwnedAssociation(**a) for a in values['associations'])
        if values.get('amf_context') is not None:
            values['amf_context']=OwnedAmfContext(**values['amf_context'])
        return cls(**values)


@dataclass(frozen=True)
class KernelView:
    upf_id: str
    epoch: str
    observed_at: float
    session_count: int
    residual_count: int
    available: bool = True
    source: str = 'gtp5g-netlink'
    errors: tuple[str,...] = ()
    native_upf_id: str = ''
    node_ip: str = ''


@dataclass(frozen=True)
class CurrentTeardownEvidence:
    observed_at: float
    operation_id: str
    lease_id: str
    owner_id: str
    generation: int
    supi: str
    pod_uid: str
    namespace: str
    container_id: str
    process_epoch: str
    configuration_seal: str
    protected_nf_identities: tuple[tuple[str,str],...]
    admissions_closed: bool
    policy_restoration_complete: bool
    amf_absent: bool
    amf_observed_at: float
    ue_terminal: bool
    quiescence_observed_at: float
    smf_complete: bool
    smf_epoch: str
    sessions: tuple[OwnedSession,...]
    pcf_epoch: str
    associations: tuple[str,...]
    kernels: tuple[KernelView,...]
    authority_valid: bool = True
    pcf_complete: bool = True
    smf_observed_at: float = 0
    pcf_observed_at: float = 0
    authority_observed_at: float = 0
    protected_nf_observed_at: float = 0
    upf_bindings: tuple[CommissionedUpfBinding,...] = ()
    amf_context: OwnedAmfContext | None = None
    amf_epoch: str = ''
    amf_complete: bool = True
    amf_context_count: int = 0
    pcf_queried_supi: str = ''
    pcf_access_types: tuple[tuple[str,str],...] = ()
    upf_topology:object=None


class CurrentTeardownReader(Protocol):
    def snapshot(self,target:OwnedTeardownTarget)->CurrentTeardownEvidence: ...


class TeardownJournal:
    def __init__(self,path):
        self.path=Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path,timeout=5) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_operations_v2 (operation_id TEXT PRIMARY KEY,authority TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_targets_v2 (operation_id TEXT NOT NULL,target_id TEXT NOT NULL,digest TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(operation_id,target_id))')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_commands_v2 (operation_id TEXT NOT NULL,target_id TEXT NOT NULL,kind TEXT NOT NULL,resource TEXT NOT NULL,status TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(operation_id,target_id,kind,resource))')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_drain_bindings_v1 (operation_id TEXT PRIMARY KEY,baseline TEXT NOT NULL,stop_receipt TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_terminal_v1 (target_digest TEXT PRIMARY KEY,observed_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_nas_v1 (target_digest TEXT PRIMARY KEY,status TEXT NOT NULL)')
            yield db

    def reserve_nas(self,target):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT status FROM owned_teardown_nas_v1 WHERE target_digest=?',(target.digest,)).fetchone()
            if prior:
                if prior!=('blocked_not_sent',):return False
                db.execute("UPDATE owned_teardown_nas_v1 SET status='dispatch_reserved' WHERE target_digest=? AND status='blocked_not_sent'",(target.digest,))
                return True
            db.execute('INSERT INTO owned_teardown_nas_v1 VALUES(?,?)',(target.digest,'dispatch_reserved'))
            return True

    def block_nas_not_sent(self,target):
        """Only called before entering dispatch; unknown or sent rows never reset."""
        with self.connect() as db:
            changed=db.execute("UPDATE owned_teardown_nas_v1 SET status='blocked_not_sent' WHERE target_digest=? AND status='dispatch_reserved'",(target.digest,)).rowcount
            if changed!=1:raise TeardownBlocked('Pre-dispatch NAS reservation changed')

    def acknowledge_nas(self,target):
        with self.connect() as db:
            db.execute("UPDATE owned_teardown_nas_v1 SET status='acknowledged' WHERE target_digest=?",(target.digest,))

    def terminal(self,target,observed_at=None):
        with self.connect() as db:
            if observed_at is not None:
                db.execute('INSERT OR IGNORE INTO owned_teardown_terminal_v1 VALUES(?,?)',(target.digest,observed_at))
                db.execute("UPDATE owned_teardown_nas_v1 SET status='terminal_observed' WHERE target_digest=?",(target.digest,))
            row=db.execute('SELECT observed_at FROM owned_teardown_terminal_v1 WHERE target_digest=?',(target.digest,)).fetchone()
        return row[0] if row else None

    def bind(self,target):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            authority=json.dumps([target.owner_id,target.lease_id,target.generation],separators=(',',':'))
            operation=db.execute('SELECT authority FROM owned_teardown_operations_v2 WHERE operation_id=?',(target.operation_id,)).fetchone()
            if operation and operation[0]!=authority:
                raise TeardownBlocked('Release operation lease or owner changed')
            same_ue=db.execute("SELECT target_id FROM owned_teardown_targets_v2 WHERE operation_id=? AND json_extract(value,'$.supi')=?",
                (target.operation_id,target.supi)).fetchone()
            if same_ue and same_ue[0]!=target.identity:
                raise TeardownBlocked('Owned UE target identity changed within release')
            # Refuse to discard pre-upgrade dispatch uncertainty under a new table name.
            legacy=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owned_teardown_targets'").fetchone()
            if legacy and db.execute('SELECT 1 FROM owned_teardown_targets WHERE operation_id=?',(target.operation_id,)).fetchone():
                raise TeardownBlocked('Legacy teardown operation requires read-only recovery')
            row=db.execute('SELECT digest FROM owned_teardown_targets_v2 WHERE operation_id=? AND target_id=?',(target.operation_id,target.identity)).fetchone()
            if row and row[0]!=target.digest:
                raise TeardownBlocked('Operation target authority changed')
            db.execute('INSERT OR IGNORE INTO owned_teardown_operations_v2 VALUES (?,?)',(target.operation_id,authority))
            db.execute('INSERT OR IGNORE INTO owned_teardown_targets_v2 VALUES (?,?,?,?)',
                (target.operation_id,target.identity,target.digest,json.dumps(target.to_dict(),sort_keys=True)))

    def reserve(self,target,kind,resource,evidence):
        """Commit dispatch intent before network IO. Existing entries never authorize replay."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT 1 FROM owned_teardown_commands_v2 WHERE operation_id=? AND target_id=? AND kind=? AND resource=?',
                (target.operation_id,target.identity,kind,resource)).fetchone()
            if existing:
                return False
            value={'target_digest':target.digest,'before':asdict(evidence),'dispatched_at':time.time()}
            db.execute('INSERT INTO owned_teardown_commands_v2 VALUES (?,?,?,?,?,?)',
                (target.operation_id,target.identity,kind,resource,'dispatch_reserved',json.dumps(value,allow_nan=False)))
            return True

    def finish(self,target,kind,resource,status,http_status=None):
        with self.connect() as db:
            row=db.execute('SELECT value FROM owned_teardown_commands_v2 WHERE operation_id=? AND target_id=? AND kind=? AND resource=?',
                (target.operation_id,target.identity,kind,resource)).fetchone()
            value=json.loads(row[0])
            value.update(observed_at=time.time(),http_status=http_status)
            db.execute('UPDATE owned_teardown_commands_v2 SET status=?,value=? WHERE operation_id=? AND target_id=? AND kind=? AND resource=?',
                (status,json.dumps(value),target.operation_id,target.identity,kind,resource))

    def commands(self,target):
        with self.connect() as db:
            rows=db.execute("SELECT kind,resource,status,value FROM owned_teardown_commands_v2 WHERE operation_id=? AND target_id=? AND json_extract(value,'$.target_digest')=? ORDER BY rowid",
                (target.operation_id,target.identity,target.digest)).fetchall()
        return [dict(kind=r[0],resource=r[1],status=r[2],**json.loads(r[3])) for r in rows]

    def targets(self,operation_id):
        with self.connect() as db:
            rows=db.execute('SELECT value FROM owned_teardown_targets_v2 WHERE operation_id=? ORDER BY target_id',
                (operation_id,)).fetchall()
        return tuple(OwnedTeardownTarget.from_dict(json.loads(r[0])) for r in rows)

    def drain_binding(self,operation_id):
        with self.connect() as db:
            row=db.execute('SELECT baseline,stop_receipt FROM owned_teardown_drain_bindings_v1 WHERE operation_id=?',(operation_id,)).fetchone()
        if row is None:return None
        return (OwnedDrainBaseline(**json.loads(row[0])),
            OwnedZeroGenerationStopReceipt.from_dict(json.loads(row[1])) if row[1] else None)

    def bind_drain(self,baseline,receipt=None):
        if type(baseline) is not OwnedDrainBaseline or (receipt is not None and
                (type(receipt) is not OwnedZeroGenerationStopReceipt or receipt.baseline!=baseline)):
            raise TeardownBlocked('Typed immutable drain binding required')
        operation=baseline.permit['release_operation_id']
        raw=json.dumps(baseline.to_dict(),sort_keys=True,separators=(',',':'))
        stop=json.dumps(receipt.to_dict(),sort_keys=True,separators=(',',':')) if receipt else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT baseline,stop_receipt FROM owned_teardown_drain_bindings_v1 WHERE operation_id=?',(operation,)).fetchone()
            if row and (row[0]!=raw or row[1] is not None and stop is not None and row[1]!=stop):
                raise TeardownBlocked('Pre-stop baseline or zero receipt changed')
            db.execute('INSERT OR IGNORE INTO owned_teardown_drain_bindings_v1 VALUES (?,?,?)',(operation,raw,stop))
            if stop:db.execute('UPDATE owned_teardown_drain_bindings_v1 SET stop_receipt=? WHERE operation_id=? AND stop_receipt IS NULL',(stop,operation))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


class _DeadlineRaw(io.RawIOBase):
    def __init__(self,sock,deadline):
        self.sock,self.deadline=sock,deadline
        self.raw=sock.makefile('rb',buffering=0)

    def readable(self):
        return True

    def readinto(self,buffer):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError('Standard SBI total response deadline exceeded')
        self.sock.settimeout(remaining)
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self,sock,deadline):
        self.sock,self.deadline=sock,deadline

    def __getattr__(self,name):
        return getattr(self.sock,name)

    def makefile(self,mode,*args,**kwargs):
        if mode!='rb':
            raise ValueError('Unexpected standard SBI socket mode')
        return io.BufferedReader(_DeadlineRaw(self.sock,self.deadline))


class _DeadlineConnection:
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.response_deadline=time.monotonic()+self.timeout

    def send(self,data):
        # OS DNS resolution is synchronous; an expired resolution never permits a late write.
        if self.sock is None:
            self.connect()
        remaining=self.response_deadline-time.monotonic()
        if remaining<=0:
            self.close()
            raise TimeoutError('Standard SBI total request deadline exceeded')
        self.sock.settimeout(remaining)
        return super().send(data)

    def getresponse(self):
        if self.sock is not None:
            self.sock=_DeadlineSocket(self.sock,self.response_deadline)
        return super().getresponse()


class _HttpConnection(_DeadlineConnection,http.client.HTTPConnection):
    pass


class _HttpsConnection(_DeadlineConnection,http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        remaining=self.response_deadline-time.monotonic()
        if remaining<=0:
            self.close()
            raise TimeoutError('Standard SBI TLS deadline exceeded')
        self.sock.settimeout(remaining)
        self.sock=self._context.wrap_socket(self.sock,server_hostname=self.host)


class _DeadlineHTTPHandler(HTTPHandler):
    def http_open(self,request):
        return self.do_open(_HttpConnection,request)


class _DeadlineHTTPSHandler(HTTPSHandler):
    def https_open(self,request):
        return self.do_open(_HttpsConnection,request,context=self._context,check_hostname=self._check_hostname)


@dataclass(frozen=True)
class SbiEndpoint:
    origin: str
    auth_mode: str
    token_reader: Callable[[],str] | None = field(default=None,repr=False,compare=False)
    timeout_seconds: float = 10

    def __post_init__(self):
        parsed=urlsplit(self.origin)
        if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
            raise ValueError('SBI endpoint must be a fixed origin')
        try:
            ipaddress.ip_address(parsed.hostname)
            if '%' in parsed.hostname:
                raise ValueError()
        except ValueError:
            raise ValueError('Private SBI writes require a pinned numeric IP origin') from None
        if self.auth_mode not in {'none','bearer'} or (self.auth_mode=='bearer' and self.token_reader is None):
            raise ValueError('Explicit standard SBI authentication configuration required')
        if type(self.timeout_seconds) not in {int,float} or not math.isfinite(self.timeout_seconds) or not 0<self.timeout_seconds<=60:
            raise ValueError('Invalid SBI timeout')


class StandardSbiTeardownHttp:
    """Separate service auth configuration; no QER credentials or redirect forwarding."""
    def __init__(self,smf:SbiEndpoint,pcf:SbiEndpoint,amf:SbiEndpoint | None=None):
        self.smf,self.pcf,self.amf=smf,pcf,amf
        self.opener=build_opener(ProxyHandler({}),_NoRedirect(),_DeadlineHTTPHandler(),_DeadlineHTTPSHandler())

    def _request(self,endpoint,path,method,data=None):
        headers={'Content-Type':'application/json'}
        if endpoint.auth_mode=='bearer':
            token=endpoint.token_reader()
            if not isinstance(token,str) or not token.strip() or any(c in token for c in '\r\n'):
                raise TeardownBlocked('Standard SBI authentication unavailable')
            headers['Authorization']='Bearer '+token.strip()
        request=Request(endpoint.origin.rstrip('/')+path,data=data,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=endpoint.timeout_seconds) as response:
                # Only status is needed. A trickled/invalid response body cannot hold ownership.
                return response.status
        except HTTPError as error:
            status=error.code
            error.close()
            return status

    def release_session(self,session):
        return self._request(self.smf,'/nsmf-pdusession/v1/sm-contexts/'+quote(session.sm_context_ref,safe='')+'/release','POST',b'{}')

    def delete_association(self,association):
        return self._request(self.pcf,'/npcf-am-policy-control/v1/policies/'+quote(association.association_id,safe=''),'DELETE')

    def release_amf_context(self,context):
        if self.amf is None:
            raise TeardownBlocked('Standard AMF SBI capability is not configured')
        return self._request(self.amf,'/namf-comm/v1/ue-contexts/'+quote(context.supi,safe='')+'/release',
            'POST',b'{"ngapCause":{"group":0,"value":0}}')


class OwnedNativeTeardown:
    def __init__(self,reader:CurrentTeardownReader,http:StandardSbiTeardownHttp,journal:TeardownJournal,
                 *,authority_guard:Callable[[OwnedTeardownTarget],ContextManager],clock=time.time,maximum_age_seconds=10):
        if not callable(authority_guard):
            raise ValueError('A current-owner admission guard is required')
        self.reader,self.http,self.journal=reader,http,journal
        self.authority_guard,self.clock,self.maximum_age_seconds=authority_guard,clock,maximum_age_seconds

    def _current(self,target,allowed_sessions=None):
        evidence=self.reader.snapshot(target)
        return self._validate_current(target,evidence,allowed_sessions)

    def _validate_current(self,target,evidence,allowed_sessions=None,*,now=None,_pre_nas=False):
        """Validate original proof fields without IO, caching or replacing clocks."""
        clock=self.clock if now is None else lambda:now
        for name in ('operation_id','lease_id','owner_id','generation','supi','pod_uid','namespace','container_id','process_epoch','configuration_seal','protected_nf_identities'):
            if getattr(evidence,name)!=getattr(target,name):
                raise TeardownBlocked('Current native owner or identity changed')
        stamps=(evidence.observed_at,evidence.amf_observed_at,evidence.quiescence_observed_at,
            evidence.smf_observed_at,evidence.pcf_observed_at,evidence.authority_observed_at,evidence.protected_nf_observed_at)
        if any(type(s) not in {int,float} or not math.isfinite(s) or not -0.000001<=clock()-s<=self.maximum_age_seconds for s in stamps):
            raise TeardownBlocked('Current native teardown evidence is stale')
        if not all(v is True for v in (evidence.authority_valid,evidence.admissions_closed,evidence.policy_restoration_complete,
                evidence.smf_complete,evidence.pcf_complete,evidence.amf_complete)) or evidence.ue_terminal is not (not _pre_nas):
            raise TeardownBlocked('Native teardown prerequisites are not proven')
        if type(evidence.amf_context_count) is not int or evidence.amf_context_count<0:
            raise TeardownBlocked('Complete native AMF census count is required')
        actual=evidence.amf_context
        if type(evidence.amf_absent) is not bool or evidence.amf_absent!=(actual is None):
            raise TeardownBlocked('AMF absence contradicts the current native context')
        if target.amf_context is not None:
            if evidence.amf_epoch!=target.amf_context.amf_epoch:
                raise TeardownBlocked('AMF process incarnation changed')
        if actual is not None:
            expected=target.amf_context
            if expected is None:
                raise TeardownBlocked('Registered AMF context is not explicitly owned')
            for name in ('supi','amf_epoch','amf_ue_ngap_id','ran_ue_ngap_id','access_type','provenance'):
                if getattr(actual,name)!=getattr(expected,name):
                    raise TeardownBlocked('Native AMF context identity changed')
            if not set(actual.sm_context_refs)<=set(expected.sm_context_refs):
                raise TeardownBlocked('AMF context acquired unowned child sessions')
        if any(s.smf_epoch!=evidence.smf_epoch for s in target.sessions):
            raise TeardownBlocked('SMF epoch changed')
        if any(a.pcf_epoch!=evidence.pcf_epoch for a in target.associations):
            raise TeardownBlocked('PCF epoch changed')
        allowed={s.session_key:s for s in (allowed_sessions if allowed_sessions is not None else target.sessions)}
        if len({s.session_key for s in evidence.sessions})!=len(evidence.sessions) or any(allowed.get(s.session_key)!=s for s in evidence.sessions):
            raise TeardownBlocked('Unowned or replaced native session is present')
        if len(set(evidence.associations))!=len(evidence.associations) or not set(evidence.associations)<={a.association_id for a in target.associations}:
            raise TeardownBlocked('Unowned policy association is present')
        if evidence.pcf_queried_supi!=target.supi or len(evidence.pcf_access_types)!=len(evidence.associations) or dict(evidence.pcf_access_types)!={a:'3GPP_ACCESS' for a in evidence.associations}:
            raise TeardownBlocked('Exact SUPI-filtered native PCF access evidence is required')
        kernels={k.upf_id:k for k in evidence.kernels}
        expected={'embb','urllc','mmtc','v2x'}
        if target.upf_topology is not None:
            from .owned_upf_topology import OwnedUpfTopology
            topology=evidence.upf_topology
            if (type(topology) is not OwnedUpfTopology or topology.identity!=target.upf_topology.identity
                    or not -0.000001<=clock()-topology.observed_at<=self.maximum_age_seconds):
                raise TeardownBlocked('Current full physical topology differs from immutable pre-stop pin')
            expected={r.logical_target for r in target.upf_topology.members}
        elif evidence.upf_topology is not None:
            raise TeardownBlocked('Unpinned dynamic topology cannot replace fixed four authority')
        if len(evidence.kernels)!=len(expected) or set(kernels)!=expected:
            raise TeardownBlocked('Complete pinned independent UPF kernel views are required')
        bindings={b.logical_target:b for b in evidence.upf_bindings}
        if (len(evidence.upf_bindings)!=len(expected) or set(bindings)!=set(kernels)
                or len({b.native_upf_id for b in evidence.upf_bindings})!=len(expected)
                or len({b.node_ip for b in evidence.upf_bindings})!=len(expected)):
            raise TeardownBlocked('Complete commissioned native UPF crosswalk is required')
        if target.upf_topology is not None:
            for member in target.upf_topology.members:
                binding=bindings[member.logical_target]
                if (binding.native_upf_id,binding.node_ip,binding.upf_epoch)!=(member.native_upf_id,member.node_ip,member.kernel_epoch):
                    raise TeardownBlocked('Kernel crosswalk differs from immutable physical topology')
        for kernel in evidence.kernels:
            if kernel.available is not True or kernel.source!='gtp5g-netlink' or kernel.errors or not -0.000001<=clock()-kernel.observed_at<=self.maximum_age_seconds:
                raise TeardownBlocked('Native UPF kernel evidence unavailable')
            if type(kernel.session_count) is not int or type(kernel.residual_count) is not int or min(kernel.session_count,kernel.residual_count)<0:
                raise TeardownBlocked('Invalid native kernel counters')
            binding=bindings[kernel.upf_id]
            if (kernel.native_upf_id,kernel.node_ip,kernel.epoch)!=(binding.native_upf_id,binding.node_ip,binding.upf_epoch):
                raise TeardownBlocked('Kernel UUID, node or epoch does not match commissioned UPF')
        for session in target.sessions:
            binding=bindings.get(session.upf_id)
            if binding is None or (binding.native_upf_id,binding.node_ip,binding.upf_epoch)!=(session.native_upf_id,session.upf_node_ip,session.upf_epoch):
                raise TeardownBlocked('UPF epoch changed')
        return evidence

    def validate_before_nas(self,target,evidence,allowed_sessions):
        """Validate a fresh RUNNING proof; it never authorizes terminal cleanup."""
        self._validate_current(target,evidence,allowed_sessions,_pre_nas=True)
        bindings={b.logical_target:b for b in evidence.upf_bindings}
        for session in allowed_sessions:
            binding=bindings.get(session.upf_id)
            if binding is None or (binding.native_upf_id,binding.node_ip,binding.upf_epoch)!=(
                    session.native_upf_id,session.upf_node_ip,session.upf_epoch):
                raise TeardownBlocked('Pre-NAS owned cohort UPF incarnation changed')
        if not self._known_kernel_accounting(evidence):
            raise TeardownBlocked('Pre-NAS native kernel and complete SMF census differ')
        return evidence

    @staticmethod
    def _zero(evidence):
        return not evidence.sessions and all(k.session_count==0 and k.residual_count==0 for k in evidence.kernels)

    @staticmethod
    def _known_kernel_accounting(evidence):
        """No orphan/projection-only remainder while advancing other owned PDUs."""
        return all(k.residual_count==0 and k.session_count==sum(
            s.native_upf_id==k.native_upf_id and s.upf_node_ip==k.node_ip for s in evidence.sessions)
            for k in evidence.kernels)

    def _dispatch(self,target,kind,resource,evidence,call):
        if not self.journal.reserve(target,kind,resource,evidence):
            return False
        try:
            status=call()
        except Exception:
            self.journal.finish(target,kind,resource,'outcome_unknown')
            return False
        self.journal.finish(target,kind,resource,'acknowledged' if status==204 else 'outcome_unknown',status)
        return status==204

    def retire(self,target:OwnedTeardownTarget):
        return self.retire_many((target,))[0]

    def retire_many(self,targets):
        """One lease-wide authority guard; release every owned UE before PCF cleanup."""
        targets=tuple(targets)
        if not targets or len({t.identity for t in targets})!=len(targets):
            raise TeardownBlocked('Distinct owned targets are required')
        first=targets[0]
        if any((t.operation_id,t.owner_id,t.lease_id,t.generation)!=(first.operation_id,first.owner_id,first.lease_id,first.generation) for t in targets):
            raise TeardownBlocked('Native retirement batch must share the release lease')
        if any(t.upf_topology!=first.upf_topology for t in targets):
            raise TeardownBlocked('Native retirement cohort must share one immutable pre-stop topology')
        sessions=tuple(s for target in targets for s in target.sessions)
        if len({s.session_key for s in sessions})!=len(sessions) or len({s.sm_context_ref for s in sessions})!=len(sessions):
            raise TeardownBlocked('Session or SMF reference is duplicated across owned UEs')
        for target in targets:
            self.journal.bind(target)
        if any(t.amf_context is not None for t in targets) and self.http.amf is None:
            raise TeardownBlocked('Standard AMF SBI capability is not configured')
        with self.authority_guard(first):
            def evidence(target):
                return self._current(target,sessions)
            def results(state):
                return tuple(self._result(target,evidence(target),state) for target in targets)
            def current_batch():
                current=tuple(evidence(target) for target in targets)
                counts={e.amf_context_count for e in current}
                if len(counts)!=1 or next(iter(counts))!=sum(e.amf_context is not None for e in current):
                    raise TeardownBlocked('Global AMF census contains unowned or inconsistent contexts')
                return current
            initial=current_batch()
            if all(self._zero(e) and not e.associations and e.amf_absent and e.amf_context_count==0 for e in initial):
                # No native mutation or await follows this complete fresh batch.
                # Resolve only its exact pinned uncertain resources, then format
                # those same original observations without rereading all NFs.
                settle=[]
                for target,current in zip(targets,initial):
                    allowed={'smf_release':{s.sm_context_ref for s in target.sessions},
                        'pcf_delete':{a.association_id for a in target.associations},
                        'amf_release':{target.supi} if target.amf_context is not None else set()}
                    for command in self.journal.commands(target):
                        if command['status'] not in {'dispatch_reserved','outcome_unknown'}:continue
                        if command['resource'] not in allowed.get(command['kind'],set()):
                            raise TeardownBlocked('Uncertain cleanup command is outside the pinned native authority')
                        settle.append((target,command))
                # A later target/SQLite read can age an earlier original proof.
                # Revalidate every source timestamp at one decision clock before
                # changing any journal marker. This performs no native read.
                decision_at=self.clock()
                for target,current in zip(targets,initial):
                    self._validate_current(target,current,sessions,now=decision_at)
                for target,command in settle:
                    self.journal.finish(target,command['kind'],command['resource'],'absence_observed')
                return tuple(self._result(target,current,'native_gone') for target,current in zip(targets,initial))
            for target in targets:
                current=evidence(target)
                uncertain=[c for c in self.journal.commands(target) if c['status'] in {'dispatch_reserved','outcome_unknown'}]
                if uncertain:
                    # Missing projections never resolve an unknown native outcome.
                    for command in uncertain:
                        if command['kind']=='smf_release' and not self._zero(current):
                            # An uncertain PDU does not own the other independently
                            # pinned contexts. Leave its command unresolved (and
                            # never replay it), while retiring those other PDUs.
                            if not self._known_kernel_accounting(current):return results('recovery_required')
                            continue
                        if not self._zero(current):
                            return results('recovery_required')
                        if command['kind']=='pcf_delete' and command['resource'] in current.associations:
                            return results('recovery_required')
                        if command['kind']=='amf_release' and current.amf_context is not None:
                            return results('recovery_required')
                        self.journal.finish(target,command['kind'],command['resource'],'absence_observed')
            for target in targets:
                for session in target.sessions:
                    # Every target fence remains valid before each native write.
                    current_batch()
                    current=evidence(target)
                    if session not in current.sessions:
                        continue
                    if any(c['kind']=='smf_release' and c['resource']==session.sm_context_ref
                           for c in self.journal.commands(target)):
                        # The dispatch may still be in flight or have an unknown
                        # ACK. Its exact current presence never authorizes replay.
                        continue
                    acknowledged=self._dispatch(target,'smf_release',session.sm_context_ref,current,
                        lambda:self.http.release_session(session))
                    current=evidence(target)
                    if not acknowledged or session in current.sessions:
                        return results('recovery_required')
            if any(not self._zero(evidence(target)) for target in targets):
                return results('recovery_required')
            # Only fresh complete SMF and independently zero kernel views settle
            # uncertain earlier releases; partial projection loss cannot do so.
            for target in targets:
                current=evidence(target)
                if not self._zero(current):return results('recovery_required')
                for command in self.journal.commands(target):
                    if (command['kind']=='smf_release' and command['status'] in {'dispatch_reserved','outcome_unknown'}
                            and all(s.sm_context_ref!=command['resource'] for s in current.sessions)):
                        self.journal.finish(target,command['kind'],command['resource'],'absence_observed')
            for target in targets:
                for association in target.associations:
                    for participant in current_batch():
                        if not self._zero(participant):
                            raise TeardownBlocked('Native sessions reappeared before PCF deletion')
                    current=evidence(target)
                    if association.association_id not in current.associations:
                        continue
                    acknowledged=self._dispatch(target,'pcf_delete',association.association_id,current,
                        lambda:self.http.delete_association(association))
                    current=evidence(target)
                    if not acknowledged or association.association_id in current.associations:
                        return results('recovery_required')
            for target in targets:
                current=current_batch()
                if any(not self._zero(e) or e.associations for e in current):
                    raise TeardownBlocked('AMF removal requires independently absent SMF, kernel and PCF children')
                actual=evidence(target).amf_context
                if actual is None:
                    continue
                acknowledged=self._dispatch(target,'amf_release',target.supi,evidence(target),
                    lambda:self.http.release_amf_context(actual))
                current=evidence(target)
                if not acknowledged or current.amf_context is not None:
                    return results('recovery_required')
            current=current_batch()
            state='native_gone' if all(self._zero(e) and not e.associations and e.amf_absent and e.amf_context_count==0 for e in current) else 'recovery_required'
            return tuple(self._result(target,e,state) for target,e in zip(targets,current))

    def _result(self,target,evidence,state):
        return {'source':'owned-native-teardown/v1','state':state,'target_digest':target.digest,
                'scope':'owned-core-contexts-only','core_contexts_gone':state=='native_gone',
                'native_gone':state=='native_gone','current':asdict(evidence),'commands':self.journal.commands(target)}


def configured_owned_native_teardown(config,*,reader,authority_guard):
    """Compose from trusted private runtime configuration, never an HTTP payload.

    `smf_sbi` and `pcf_sbi` each require origin/auth_mode and, for bearer,
    a standard-SBI token_file. No QER token or environment fallback is used.
    """
    if not isinstance(config,dict) or not {'journal_file','smf_sbi','pcf_sbi'}<=set(config) or set(config)-{'journal_file','smf_sbi','pcf_sbi','amf_sbi'}:
        raise ValueError('Complete private owned-native teardown configuration required')
    journal=Path(config['journal_file'])
    if not journal.is_absolute() or journal.is_symlink():
        raise ValueError('Private absolute teardown journal path required')
    def endpoint(value):
        if not isinstance(value,dict) or not {'origin','auth_mode'}<=set(value) or set(value)-{'origin','auth_mode','token_file','timeout_seconds'}:
            raise ValueError('Invalid standard SBI configuration')
        token_reader=None
        if value['auth_mode']=='bearer':
            path=Path(value.get('token_file',''))
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError('Private standard SBI token file required')
            def token_reader():
                if path.is_symlink():
                    raise TeardownBlocked('Standard SBI token source changed')
                with path.open(encoding='utf-8') as stream:
                    return stream.read(16385)
        return SbiEndpoint(value['origin'],value['auth_mode'],token_reader,value.get('timeout_seconds',10))
    return OwnedNativeTeardown(reader,StandardSbiTeardownHttp(endpoint(config['smf_sbi']),endpoint(config['pcf_sbi']),
        endpoint(config['amf_sbi']) if config.get('amf_sbi') else None),
        TeardownJournal(journal),authority_guard=authority_guard)
