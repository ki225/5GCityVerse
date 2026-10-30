"""Original-operation, current-only bootstrap execution; native ACK loss never replays.

No public endpoint or automatic factory enables this executor. Its authority,
load and node readers are mandatory trusted server ports. The existing operation
row and leases are the only journal; every external call is outside SQLite TX.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
import inspect
import json
import time

from .actuators import ActionRequest
from .bootstrap_capacity import BootstrapBlocked,BootstrapCapacityCertificate,certify,digest,need,number,validate_node,PhaseAuthorityProof


async def invoke(function,*args,**kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args,**kwargs)
    value=await asyncio.to_thread(function,*args,**kwargs)
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class BootstrapAuthority:
    intent_id: str
    user_owner: str
    operation_id: str
    revision: int
    budget_id: str
    valid_until: float
    allowed_targets: frozenset
    allowed_templates: frozenset
    allowed_nodes: frozenset
    authorized_plan_ids: frozenset
    remaining_cpu: float
    remaining_memory: int
    remaining_cost: float
    template_digests: dict
    template_costs: dict
    allowed_profile_ids: frozenset


@dataclass(frozen=True)
class BootstrapCommand:
    plan_id: str
    dependency_action_id: str
    target_id: str
    template_id: str
    node_id: str
    profile_id: str
    cost: float
    demand_bps: dict
    assignment_action: dict

    def to_dict(self):
        return asdict(self)


class BootstrapPreparationPending(BootstrapBlocked):
    """Durable same-operation work exists; do not release its held UE."""


class BootstrapPreparationRecovery(BootstrapBlocked):
    """An original dispatch is uncertain; keep all ownership for readback."""


class OriginalBootstrapJournal:
    KEY='pretraffic_infrastructure'

    def __init__(self,repository,*,clock=time.time):
        self.repository,self.clock=repository,clock

    def _row(self,db,operation_id,user_owner,*,cleanup=False):
        row=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,user_owner)).fetchone()
        need(row is not None and row['kind']=='reconcile','bootstrap_original_operation_unavailable')
        current=db.execute('SELECT * FROM intents WHERE id=? AND owner=?',(row['intent_id'],user_owner)).fetchone()
        need(current is not None,'bootstrap_original_intent_unavailable')
        if not cleanup:
            intent=json.loads(current['value'])
            need(row['status'] in {'queued','running'} and (current['state'] in {'active','satisfied','degraded'}
                 or (current['state']=='recovery_required' and json.loads(row['value']).get(self.KEY)))
                 and row['revision']==current['revision'] and intent['valid_from']<=self.clock()<intent['valid_until'],
                 'bootstrap_current_operation_authority_changed')
            self.repository._assert_fleet_open(db,row['intent_id'],operation_id)
        else:
            need(row['status'] in {'queued','running','completed','degraded','recovery_required','needs_definition','failed'},
                 'bootstrap_cleanup_operation_unavailable')
        return row,current,json.loads(row['value'])

    def read(self,operation_id,user_owner):
        with self.repository.connect() as db:
            row=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',(operation_id,user_owner)).fetchone()
            need(row is not None,'bootstrap_original_operation_unavailable')
            return json.loads(row[0]).get(self.KEY)

    def _save(self,db,row,value,record):
        value[self.KEY]=record
        db.execute('UPDATE operations SET value=?,updated=? WHERE id=? AND owner=?',
                   (json.dumps(value,allow_nan=False),self.clock(),row['id'],row['owner']))

    @staticmethod
    def scopes(command,authority):
        return sorted({'resource:'+command.target_id,'budget:'+authority.budget_id,
                       'resource:node:'+command.node_id})

    def begin(self,command,authority,envelope):
        need(type(command) is BootstrapCommand and type(authority) is BootstrapAuthority,'typed_bootstrap_authority_required')
        need(command.plan_id in authority.authorized_plan_ids and command.target_id in authority.allowed_targets
             and command.template_id in authority.allowed_templates and command.node_id in authority.allowed_nodes
             and command.profile_id in authority.allowed_profile_ids
             and command.template_id==envelope['template_id']
             and authority.template_digests.get(command.template_id)==envelope['template_sha256']
             and authority.template_costs.get(command.template_id)==command.cost,
             'bootstrap_plan_outside_trusted_authority')
        need(number(command.cost)<=number(authority.remaining_cost)
             and envelope['cpu_cores']<=number(authority.remaining_cpu)
             and envelope['memory_bytes']<=number(authority.remaining_memory),'bootstrap_budget_exceeded')
        body=command.to_dict();fingerprint=digest(body)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row,current,value=self._row(db,authority.operation_id,authority.user_owner)
            intent=json.loads(current['value'])
            need((row['intent_id'],row['revision'],intent['resource_budget_id'])==
                 (authority.intent_id,authority.revision,authority.budget_id)
                 and self.clock()<authority.valid_until<=intent['valid_until'],
                 'bootstrap_authority_intent_budget_or_validity_changed')
            existing=value.get(self.KEY)
            if existing:
                need(existing['fingerprint']==fingerprint,'bootstrap_original_request_changed')
                self._leases(db,row,existing)
                return existing
            scopes=self.scopes(command,authority)
            acquired=[]
            for scope in scopes:
                holder=db.execute('SELECT operation_id,expires FROM leases WHERE scope=?',(scope,)).fetchone()
                need(holder is None or holder[0]==row['id'],'bootstrap_resource_or_budget_reserved')
                need(holder is None or self.clock()<number(holder[1]),'bootstrap_resource_lease_expired')
                if holder is None:
                    acquired.append(scope)
            for scope in acquired:
                db.execute('INSERT INTO leases VALUES(?,?,?)',(scope,row['id'],authority.valid_until))
            record={'schema':'cityverse.pretraffic-bootstrap/v1','fingerprint':fingerprint,
                    'command':body,'phase':'reserved','revision':0,'scopes':scopes,'acquired_scopes':acquired,
                    'intent_id':authority.intent_id,'operation_id':row['id'],
                    'user_owner':row['owner'],'intent_revision':row['revision'],
                    'valid_until':authority.valid_until,'lease_valid_until':min(
                        db.execute('SELECT expires FROM leases WHERE scope=?',(scope,)).fetchone()[0]
                        for scope in scopes),'profile_id':command.profile_id,
                    'admission_dispatch_started':False}
            self._save(db,row,value,record)
            return record

    def _leases(self,db,row,record,*,cleanup=False):
        for scope in record['scopes']:
            holder=db.execute('SELECT operation_id,expires FROM leases WHERE scope=?',(scope,)).fetchone()
            need(holder is not None and holder[0]==row['id'],'bootstrap_resource_lease_lost')
            if not cleanup:
                need(self.clock()<number(holder[1]) and holder[1]>=record.get('lease_valid_until',record['valid_until']),
                     'bootstrap_resource_lease_expired_or_shortened')

    def cas(self,operation_id,user_owner,expected,phase,*,cleanup=False,**updates):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row,current,value=self._row(db,operation_id,user_owner,cleanup=cleanup)
            record=value.get(self.KEY)
            need(record is not None and record['revision']==expected['revision']
                 and record['fingerprint']==expected['fingerprint']
                 and record['phase']==expected['phase'],'bootstrap_journal_cas_conflict')
            need(record['intent_id']==row['intent_id'] and record['intent_revision']==row['revision'],
                 'bootstrap_original_lineage_changed')
            self._leases(db,row,record,cleanup=cleanup)
            result={**record,**updates,'phase':phase,'revision':record['revision']+1}
            self._save(db,row,value,result)
            return result

    def guard(self,operation_id,user_owner,record,*,cleanup=False):
        with self.repository.connect() as db:
            row,current,value=self._row(db,operation_id,user_owner,cleanup=cleanup)
            need(value.get(self.KEY)==record,'bootstrap_current_journal_changed')
            self._leases(db,row,record,cleanup=cleanup)

    def finish_cleanup(self,operation_id,user_owner,record):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row,current,value=self._row(db,operation_id,user_owner,cleanup=True)
            need(value.get(self.KEY)==record,'bootstrap_current_journal_changed')
            self._leases(db,row,record,cleanup=True)
            result={**record,'phase':'compensated','revision':record['revision']+1}
            for scope in record['acquired_scopes']:
                db.execute('DELETE FROM leases WHERE scope=? AND operation_id=?',(scope,operation_id))
            self._save(db,row,value,result)
            return result

    def release_transient_leases(self,operation_id,user_owner):
        """Normal post-effect completion retains original bootstrap ownership."""
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',
                           (operation_id,user_owner)).fetchone()
            need(row is not None,'bootstrap_original_operation_unavailable')
            record=json.loads(row[0]).get(self.KEY)
            keep=record['scopes'] if record and record['phase']!='compensated' else []
            if keep:
                db.execute('DELETE FROM leases WHERE operation_id=? AND scope NOT IN ('+
                           ','.join('?' for _ in keep)+')',(operation_id,*keep))
            else:
                db.execute('DELETE FROM leases WHERE operation_id=?',(operation_id,))

    def resume_operation(self,operation_id,user_owner):
        """Exact idempotency replay resumes only this journal, never an apply."""
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',
                           (operation_id,user_owner)).fetchone()
            need(row is not None and row['status']=='recovery_required' and row['kind']=='reconcile',
                 'bootstrap_resume_original_operation_unavailable')
            value=json.loads(row['value']);record=value.get(self.KEY)
            need(record is not None and record.get('preparation') and record['phase']!='compensated'
                 and value.get('pending_session_preparation') is True
                 and not any(value.get(k) for k in ('receipts','pending_action','pending_traffic_start','traffic_started')),
                 'bootstrap_resume_other_dispatch_requires_recovery')
            self._leases(db,row,record)
            db.execute("UPDATE operations SET status='queued',stage='bootstrap_readback_recovery' WHERE id=?",
                       (operation_id,))


@dataclass(frozen=True)
class CapturedCapacityPhase:
    certificate: object
    binding: object
    actual_json: str
    proof: PhaseAuthorityProof

    def __iter__(self):
        yield self.certificate
        yield self.binding
        yield json.loads(self.actual_json)

    def validate_at(self,now):
        self.proof.validate_at(now)


class PreTrafficInfrastructureExecutor:
    """Advance one bounded stage per call; polling/restart never redispatches unknown."""
    def __init__(self,journal,profiles,ports,authority_reader,*,clock=time.time,enabled=False):
        self.journal,self.profiles,self.ports,self.authority_reader=journal,profiles,ports,authority_reader
        self.clock=clock
        need(type(enabled) is bool,'invalid_bootstrap_enable_flag')
        self.enabled=enabled

    def capabilities(self):
        return {'pretraffic_add_upf':bool(self.enabled and self.profiles.entries and self.ports.configured),
                'service_effect_verified':False,'commissioned':False}

    async def _authority(self,op,owner,command):
        need(self.enabled,'not_configured:pretraffic_bootstrap_disabled')
        need(self.ports.configured,'not_configured:bootstrap_current_native_ports_missing')
        authority=await invoke(self.authority_reader,op,owner)
        need(type(authority) is BootstrapAuthority and authority.operation_id==op
             and authority.user_owner==owner,'trusted_original_bootstrap_authority_required')
        profile=self.profiles.get(command.profile_id,self.clock())
        envelope,_,_=profile.load(self.clock())
        return authority,profile,envelope

    async def _proof(self,record,profile):
        command=BootstrapCommand(**record['command'])
        opening=await invoke(self.ports.binding,command,record)
        binding,actual=opening
        load=await invoke(self.ports.load_reader,command.target_id)
        node=await invoke(self.ports.node_reader,command.node_id)
        certificate=certify(profile,binding,load,node,command.demand_bps,
                            len(command.assignment_action['parameters']['pending_admissions']),self.clock())
        need(load.get('node_id')==command.node_id,'bootstrap_placement_node_changed')
        closing=await invoke(self.ports.binding,command,record)
        again,_=closing
        certificate.validate(again,self.clock())
        self.journal.guard(record['operation_id'],record['user_owner'],record)
        proof=PhaseAuthorityProof(record['valid_until'],record.get('lease_valid_until',record['valid_until']),
            profile_until=profile.valid_until,native_proofs=(opening,closing),
            certificate_checks=((json.dumps(certificate.to_dict(),sort_keys=True,allow_nan=False),again),))
        proof.validate_at(self.clock())
        return CapturedCapacityPhase(certificate,again,json.dumps(actual,sort_keys=True,allow_nan=False),proof)

    async def advance(self,operation_id,user_owner,command):
        authority,profile,envelope=await self._authority(operation_id,user_owner,command)
        record=self.journal.read(operation_id,user_owner)
        if record is None:
            node=await invoke(self.ports.node_reader,command.node_id)
            validate_node(node,command.node_id,envelope,self.clock(),deployed=False)
        record=self.journal.begin(command,authority,envelope)
        phase=record['phase']
        if phase=='reserved':
            node=await invoke(self.ports.node_reader,command.node_id)
            validate_node(node,command.node_id,envelope,self.clock(),deployed=False)
            before=await invoke(self.ports.inspect,command.target_id)
            fresh_now=self.clock()
            profile.load(fresh_now)
            validate_node(node,command.node_id,envelope,fresh_now,deployed=False)
            need(before.get('exists') is False and before.get('owner_id') in (None,authority.intent_id),
                 'bootstrap_target_is_not_original_absence')
            proof=PhaseAuthorityProof(record['valid_until'],record.get('lease_valid_until',record['valid_until']),
                profile_until=profile.valid_until,node_json=json.dumps(node,sort_keys=True,allow_nan=False),
                envelope_json=json.dumps(envelope,sort_keys=True,allow_nan=False),
                absence_json=json.dumps(before,sort_keys=True,allow_nan=False),node_id=command.node_id)
            record=self.journal.cas(operation_id,user_owner,record,'deploy_dispatching',before=before)
            self.journal.guard(operation_id,user_owner,record)
            proof.validate_at(self.clock())
            await invoke(self.ports.deploy,command,record)  # Never called again in this phase.
        elif phase=='deploy_dispatching':
            actual=await invoke(self.ports.inspect,command.target_id)
            if not self.ports.path_ready(actual,command,record):
                return self.result(record)
            record=self.journal.cas(operation_id,user_owner,record,'path_ready')
        elif phase=='path_ready':
            certificate,binding,actual=await self._proof(record,profile)
            record=self.journal.cas(operation_id,user_owner,record,'selection_dispatching',
                certificate=certificate.to_dict(),selection_version=binding.version)
            self.journal.guard(operation_id,user_owner,record)
            await invoke(self.ports.select,command,record)
        elif phase=='selection_dispatching':
            actual=await invoke(self.ports.inspect,command.target_id)
            if not self.ports.path_ready(actual,command,record) or actual.get('smf_selected') is not True:
                return self.result(record)
            record=self.journal.cas(operation_id,user_owner,record,'selected')
        elif phase=='selected':
            certificate,binding,actual=await self._proof(record,profile)
            request=await invoke(self.ports.materialize,command,record,binding,actual,certificate)
            need(type(request) is ActionRequest,'typed_bootstrap_materialized_request_required')
            record=self.journal.cas(operation_id,user_owner,record,'admission_ready',
                certificate=certificate.to_dict(),materialization_certificate=certificate.to_dict(),
                materialized=request.to_dict())
        elif phase=='admission_ready':
            _,binding,_=await self._proof(record,profile)
            BootstrapCapacityCertificate(**record['materialization_certificate']).validate(binding,self.clock())
            record=self.journal.cas(operation_id,user_owner,record,'admission_dispatching',
                                    admission_dispatch_started=True)
            self.journal.guard(operation_id,user_owner,record)
            await invoke(self.ports.install_admissions,record)
        elif phase=='admission_dispatching':
            installations=await invoke(self.ports.read_admissions,record)
            certificate,_,_=await self._proof(record,profile)
            record=self.journal.cas(operation_id,user_owner,record,'ready',
                certificate=certificate.to_dict(),admission_installations=installations)
        elif phase=='ready':
            proof=await self._proof(record,profile)
            await invoke(self.ports.read_admissions,record)
            self.journal.guard(operation_id,user_owner,record)
            # Admission GETs and SQLite waits cannot rejuvenate the old proof.
            proof.validate_at(self.clock())
            BootstrapCapacityCertificate(**record['materialization_certificate']).validate(proof.binding,self.clock())
        else:
            raise BootstrapBlocked('bootstrap_phase_requires_owned_recovery')
        return self.result(record)

    @staticmethod
    def result(record):
        return {'operation_id':record['operation_id'],'phase':record['phase'],
                'pending':record['phase']!='ready','service_effect_verified':False,
                'certificate':deepcopy(record.get('certificate')),
                'admission_installations':deepcopy(record.get('admission_installations',[]))}

    async def compensate(self,operation_id,user_owner):
        record=self.journal.read(operation_id,user_owner)
        need(record is not None,'bootstrap_owned_journal_missing')
        if record['phase']=='compensated':
            return {'restored':True}
        self.journal.guard(operation_id,user_owner,record,cleanup=True)
        if record['phase']=='reserved':
            return {'restored':self.journal.finish_cleanup(operation_id,user_owner,record)['phase']=='compensated'}
        if record['phase'] not in {'restore_dispatching','cancel_dispatching','cleanup_ready'}:
            # Recover every potentially dispatched admission positively before
            # cleanup; no404/unchanged-target claim proves in-flight absence.
            await invoke(self.ports.recover_cleanup_admissions,record)
            record=self.journal.cas(operation_id,user_owner,record,'cancel_dispatching',cleanup=True)
            self.journal.guard(operation_id,user_owner,record,cleanup=True)
            await invoke(self.ports.cancel_admissions,record)
        elif record['phase']=='cancel_dispatching':
            if not await invoke(self.ports.admissions_closed,record):
                return {'restored':False,'pending':True}
            record=self.journal.cas(operation_id,user_owner,record,'cleanup_ready',cleanup=True)
        elif record['phase']=='cleanup_ready':
            record=self.journal.cas(operation_id,user_owner,record,'restore_dispatching',cleanup=True)
            self.journal.guard(operation_id,user_owner,record,cleanup=True)
            await invoke(self.ports.restore,record)
        elif record['phase']=='restore_dispatching':
            if await invoke(self.ports.restored,record):
                self.journal.finish_cleanup(operation_id,user_owner,record)
                return {'restored':True}
        return {'restored':False,'pending':True}

    async def quiesce(self,operation_id,user_owner):
        """Close future selection before owned UE drain; no NF deletion here."""
        record=self.journal.read(operation_id,user_owner)
        need(record is not None,'bootstrap_owned_journal_missing')
        self.journal.guard(operation_id,user_owner,record,cleanup=True)
        stage=record.get('quiescence')
        if stage is None:
            await invoke(self.ports.recover_cleanup_admissions,record)
            record=self.journal.cas(operation_id,user_owner,record,record['phase'],cleanup=True,
                                    quiescence='cancel_dispatching')
            self.journal.guard(operation_id,user_owner,record,cleanup=True)
            await invoke(self.ports.cancel_admissions,record)
        elif stage=='cancel_dispatching':
            if not await invoke(self.ports.admissions_closed,record):
                return {'admissions_closed':False}
            record=self.journal.cas(operation_id,user_owner,record,record['phase'],cleanup=True,
                                    quiescence='selection_dispatching')
            self.journal.guard(operation_id,user_owner,record,cleanup=True)
            await invoke(self.ports.close_selection,record)
        elif stage=='selection_dispatching':
            if not await invoke(self.ports.selection_closed,record):
                return {'admissions_closed':False}
            record=self.journal.cas(operation_id,user_owner,record,record['phase'],cleanup=True,
                                    quiescence='closed')
        elif stage=='closed':
            need(await invoke(self.ports.selection_closed,record),'bootstrap_selection_reopened')
            need(await invoke(self.ports.admissions_closed,record),'bootstrap_admission_reopened')
            return {'admissions_closed':True,'source':'owned-bootstrap-quiescence/v1'}
        else:
            raise BootstrapBlocked('bootstrap_unknown_quiescence_phase')
        return {'admissions_closed':record.get('quiescence')=='closed',
                'source':'owned-bootstrap-quiescence/v1'}
