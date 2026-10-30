"""Concrete composition of existing lifecycle/registry/materializer/SMF ports.

No new native writer and no assumed bootstrap telemetry reader. Factory remains
unconfigured unless all server-selected readers and a measured profile exist.
"""
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
import json
import time

from .actuators import ActionRequest
from .bootstrap_capacity import IDENTITY, fresh, need, PhaseAuthorityProof
from .commissioned_upfs import RegistrySnapshot
from .pending_admission import DeferredAdmissionMaterializer, PendingAdmission,SessionAssignment
from .planning import PlanAction


@dataclass(frozen=True)
class CapturedBootstrapProof:
    """Original read-side provenance, pure-validated after the last blocking IO.

    Frozen registry snapshots and canonical JSON own the captured data. Neither
    native metadata nor sample clocks are refreshed by a later validation.
    """
    opening: RegistrySnapshot
    closing: RegistrySnapshot
    binding: object
    path_json: str
    authority_until: float
    certificate_json: str | None = None
    admission_until: float | None = None
    lease_until: float | None = None

    def __iter__(self):
        # Existing proof consumers receive copies, not mutable proof authority.
        yield self.binding
        yield json.loads(self.path_json)

    def with_certificate(self,certificate,*,admission_until=None):
        return replace(self,certificate_json=json.dumps(certificate,sort_keys=True,allow_nan=False),
                       admission_until=admission_until)

    def validate_at(self,now):
        """No IO, no clock capture: caller supplies its FINAL actual decision time."""
        self.opening.require_fresh(now)
        self.closing.require_fresh(now)
        fresh(self.binding.observed_at,now)
        actual=json.loads(self.path_json)
        fresh(actual.get('observed_at'),now)
        need(self.opening.revision==self.closing.revision,
             'bootstrap_registry_changed_during_lifecycle_read')
        for key in ('native_upf_id','pod_uid','kernel_epoch','version','owner_id','operation_id'):
            need(actual.get(key)==getattr(self.binding,key),
                 'bootstrap_registry_lifecycle_identity_mismatch')
        need(all(actual.get(k) is True for k in ('ready','pfcp_associated','path_verified')),
             'bootstrap_native_pfcp_or_path_unverified')
        PhaseAuthorityProof(self.authority_until,
            self.authority_until if self.lease_until is None else self.lease_until).validate_at(now)
        need(self.admission_until is None or now<self.admission_until,
             'bootstrap_original_dispatch_authority_expired')
        if self.certificate_json is not None:
            from .bootstrap_capacity import BootstrapCapacityCertificate
            BootstrapCapacityCertificate(**json.loads(self.certificate_json)).validate(self.binding,now)


class NativeBootstrapPorts:
    def __init__(self,lifecycle,registry,admission_coordinator,reservation_reader,
                 *,load_reader=None,node_reader=None,clock=time.time):
        self.lifecycle,self.registry,self.coordinator=lifecycle,registry,admission_coordinator
        self.load_reader,self.node_reader,self.clock=load_reader,node_reader,clock
        self.materializer=DeferredAdmissionMaterializer(registry,reservation_reader,
                                                        admission_coordinator.api,clock=clock)
        self.journal_guard=None

    def _guard(self,record,*,cleanup=False):
        need(callable(self.journal_guard),'bootstrap_native_mutation_guard_missing')
        self.journal_guard(record,cleanup=cleanup)

    def _guarded_coordinator(self,record,*,cleanup=False):
        """The coordinator GET is not the final mutation authority boundary."""
        from .pending_admission import PendingAdmissionCoordinator
        outer=self;source=self.coordinator.api
        class GuardedApi:
            def inspect(self,nonce):return source.inspect(nonce)
            def register(self,*args):
                from .pretraffic_infrastructure import BootstrapCommand
                proof=outer.binding(BootstrapCommand(**record['command']),record).with_certificate(
                    record['materialization_certificate'],admission_until=args[0].valid_until)
                outer._guard(record)
                proof.validate_at(outer.clock())
                return source.register(*args)
            def release(self,*args):
                outer._guard(record,cleanup=True)
                return source.release(*args)
        return PendingAdmissionCoordinator(GuardedApi(),clock=self.clock,
            maximum_read_age_seconds=self.coordinator.maximum_read_age_seconds)

    @property
    def configured(self):
        return callable(self.load_reader) and callable(self.node_reader)

    def inspect(self,target):
        value=self.lifecycle.inspect(target)
        need(value.get('source')=='native-upf-controller' and value.get('target_id')==target
             and value.get('version'),'bootstrap_current_lifecycle_readback_missing')
        fresh(value.get('observed_at'),self.clock())
        return value

    def path_ready(self,actual,command,record):
        if actual.get('exists') is False:
            return False
        need(actual.get('owner_id')==record['intent_id'] and actual.get('operation_id')==record['operation_id']
             and actual.get('template_id')==command.template_id and actual.get('node_id')==command.node_id,
             'bootstrap_lifecycle_owner_or_placement_changed')
        return all(actual.get(k) is True for k in ('ready','pfcp_associated','path_verified'))

    def binding(self,command,record):
        snapshot=self.registry.snapshot()
        need(type(snapshot) is RegistrySnapshot,'typed_bootstrap_registry_required')
        snapshot.require_fresh(self.clock())
        observations=[r for r in snapshot.observations if r.slot.target_id==command.target_id]
        need(len(observations)==1 and observations[0].native_registered
             and observations[0].binding is not None,'bootstrap_native_incarnation_not_registered')
        binding=observations[0].binding
        need(binding.owner_id==record['intent_id'] and binding.operation_id==record['operation_id']
             and observations[0].state=='registered','bootstrap_registry_owner_or_state_changed')
        actual=self.inspect(command.target_id)
        need(self.path_ready(actual,command,record),'bootstrap_native_pfcp_or_path_unverified')
        for key in ('native_upf_id','pod_uid','kernel_epoch','version'):
            need(actual.get(key)==getattr(binding,key),'bootstrap_registry_lifecycle_identity_mismatch')
        # The lifecycle GET can return a captured projection while native state
        # changes before that response reaches us. Close with the authoritative
        # registry AFTER that GET; do not treat two old projections as current.
        closing=self.registry.snapshot()
        need(type(closing) is RegistrySnapshot,'typed_bootstrap_registry_required')
        proof=CapturedBootstrapProof(snapshot,closing,binding,
            json.dumps(actual,sort_keys=True,allow_nan=False),record['valid_until'],
            lease_until=record.get('lease_valid_until',record['valid_until']))
        proof.validate_at(self.clock())
        # Revision includes exact state, owner/op, incarnation and current
        # version but excludes sample time. Preserve the opening clocks below.
        return proof

    def deploy(self,command,record):
        self.lifecycle.deploy(command.target_id,template_id=command.template_id,node_id=command.node_id,
            owner_id=record['intent_id'],operation_id=record['operation_id'],expected_version=record['before']['version'])

    def select(self,command,record):
        proof=self.binding(command,record).with_certificate(record['certificate'])
        binding,actual=proof
        self._guard(record)
        decision_at=self.clock()
        proof.validate_at(decision_at)
        need(binding.version==record['selection_version']
             and decision_at<record['certificate']['valid_until'],'bootstrap_selection_certificate_changed')
        self.lifecycle.configure_smf(command.target_id,accepting=True,owner_id=record['intent_id'],
            operation_id=record['operation_id'],expected_version=binding.version)

    def materialize(self,command,record,binding,actual,certificate):
        action=PlanAction(**command.assignment_action)
        need(action.kind=='assign_sessions' and action.target_id==command.target_id
             and command.dependency_action_id in action.depends_on,
             'bootstrap_deferred_assignment_dependency_changed')
        parameters=deepcopy(action.parameters)
        for pending in parameters['pending_admissions']:
            # A pending policy may authorize creation only while the measured
            # bootstrap admission certificate is current, not the whole intent.
            pending['valid_until']=min(pending['valid_until'],record['valid_until'],
                                       certificate.valid_until)
        action=replace(action,parameters=parameters)
        request=ActionRequest('assign_sessions',command.target_id,record['intent_id'],record['operation_id'],
                              binding.version,action.parameters,action.session_keys)
        deployment=ActionRequest('add_upf',command.target_id,record['intent_id'],record['operation_id'],
            record['before']['version'],{'template_id':command.template_id,'node_id':command.node_id})
        installed={**actual,**{k:getattr(binding,k) for k in IDENTITY}}
        receipt={'action_id':command.dependency_action_id,'state':'installation_confirmed',
                 'request':deployment.to_dict(),'installed':installed}
        return self.materializer.materialize(request,action,[receipt],user_owner=record['user_owner'])

    def _pending(self,record):
        value=record.get('materialized')
        if not value:
            return ()
        request=ActionRequest.from_dict(value)
        need(request.action=='assign_sessions' and request.owner_id==record['intent_id']
             and request.operation_id==record['operation_id'] and not request.session_keys
             and request.target_id==record['command']['target_id']
             and request.expected_version==record['materialization_certificate']['native_identity']['version']
             and set(request.parameters)=={'mode','pending_admissions'}
             and request.parameters['mode']=='new_sessions',
             'bootstrap_materialized_original_lineage_changed')
        rows=tuple(PendingAdmission.from_dict(v) for v in request.parameters['pending_admissions'])
        need(len(rows)==1,'not_configured:bootstrap_multi_admission_dispatch')
        original=SessionAssignment.from_action(PlanAction(**record['command']['assignment_action'])).pending_admissions
        expected={p.identity.token:p for p in original}
        need({p.identity.token for p in rows}==set(expected),'bootstrap_materialized_nonce_changed')
        for pending in rows:
            need(pending.owner_id==record['intent_id']
                 and pending.target_upf_id==record['materialization_certificate']['native_identity']['native_upf_id']
                 and pending.valid_from==expected[pending.identity.token].valid_from
                 and pending.valid_until==min(expected[pending.identity.token].valid_until,
                     record['valid_until'],record['materialization_certificate']['valid_until']),
                 'bootstrap_materialized_native_target_or_validity_changed')
        return rows

    def install_admissions(self,record):
        need(self.clock()<record['materialization_certificate']['valid_until'],
             'bootstrap_admission_certificate_expired')
        for pending in self._pending(record):
            self._guarded_coordinator(record).install(pending,record['operation_id'])

    def read_admissions(self,record):
        return [asdict(self.coordinator.recover_installation(p,record['operation_id'],
            record['certificate']['native_identity']['smf_boot_epoch'])) for p in self._pending(record)]

    def recover_cleanup_admissions(self,record):
        if record.get('admission_dispatch_started') is True:
            for p in self._pending(record):
                self.coordinator.recover_for_release(p,record['operation_id'],
                    record['certificate']['native_identity']['smf_boot_epoch'])
        else:
            need(record.get('admission_dispatch_started') is False,
                 'unknown_bootstrap_admission_dispatch_history')

    def cancel_admissions(self,record):
        if record.get('admission_dispatch_started') is not True:
            return
        for p in self._pending(record):
            handle=self.coordinator.recover_for_release(p,record['operation_id'],
                record['certificate']['native_identity']['smf_boot_epoch'])
            state=self.coordinator.api.inspect(p.identity.nonce)
            if state.status=='cancelled':
                continue
            self._guarded_coordinator(record,cleanup=True).release(handle,state.version)

    def admissions_closed(self,record):
        if record.get('admission_dispatch_started') is not True:
            return record.get('admission_dispatch_started') is False
        for p in self._pending(record):
            handle=self.coordinator.recover_for_release(p,record['operation_id'],
                record['certificate']['native_identity']['smf_boot_epoch'])
            state=self.coordinator.api.inspect(p.identity.nonce)
            need(state.operation_id==handle.operation_id,'bootstrap_admission_cleanup_owner_changed')
            if state.status!='cancelled':
                return False
        return True

    def restore(self,record):
        target=record['command']['target_id'];actual=self.inspect(target)
        need(actual.get('owner_id')==record['intent_id'] and actual.get('operation_id')==record['operation_id'],
             'bootstrap_compensation_owner_changed')
        self._guard(record,cleanup=True)
        self.lifecycle.restore(target,record['before'],owner_id=record['intent_id'],
            operation_id=record['operation_id'],expected_version=actual['version'])

    def close_selection(self,record):
        actual=self.inspect(record['command']['target_id'])
        need((actual.get('owner_id'),actual.get('operation_id'))==
             (record['intent_id'],record['operation_id']),'bootstrap_quiescence_owner_changed')
        self._guard(record,cleanup=True)
        self.lifecycle.configure_smf(record['command']['target_id'],accepting=False,
            owner_id=record['intent_id'],operation_id=record['operation_id'],expected_version=actual['version'])

    def selection_closed(self,record):
        actual=self.inspect(record['command']['target_id'])
        need((actual.get('owner_id'),actual.get('operation_id'))==
             (record['intent_id'],record['operation_id']),'bootstrap_quiescence_owner_changed')
        return actual.get('smf_selected') is False

    def restored(self,record):
        actual=self.inspect(record['command']['target_id'])
        return (actual.get('exists') is False and actual.get('restored') is True
            and actual.get('cleanup_pending') is False and actual.get('active_session_keys')==[]
            and actual.get('restoration_owner_id')==record['intent_id']
            and actual.get('restoration_operation_id')==record['operation_id']
            and all(actual.get(k)==record['before'].get(k) for k in ('owner_id','operation_id','exists')))


def build_bootstrap_executor(repository,lifecycle,registry,coordinator,reservation_reader,
                             authority_reader,profiles,*,load_reader=None,node_reader=None,clock=time.time,enabled=False):
    """Explicit private factory port. Production integration is intentionally pending."""
    from .pretraffic_infrastructure import OriginalBootstrapJournal,PreTrafficInfrastructureExecutor
    journal=OriginalBootstrapJournal(repository,clock=clock)
    ports=NativeBootstrapPorts(lifecycle,registry,coordinator,reservation_reader,
                             load_reader=load_reader,node_reader=node_reader,clock=clock)
    ports.journal_guard=lambda record,cleanup=False:journal.guard(
        record['operation_id'],record['user_owner'],record,cleanup=cleanup)
    return PreTrafficInfrastructureExecutor(journal,profiles,ports,authority_reader,clock=clock,enabled=enabled)


class BootstrapPreTrafficBridge:
    """Actual planner/preparer port; only configured measured infrastructure paths.

    A model proposes two actions. This server validator replaces its cost and
    attainment claims, checks pinned target policy, exact held nonces and the
    measured profile, then persists that choice in the ORIGINAL operation.
    """
    def __init__(self,executor,choices,*,clock=time.time):
        self.executor,self.choices,self.clock=executor,tuple(choices),clock

    def check_configured(self):
        need(not getattr(self.executor,'native_final_write_authority_missing',False),
             'not_configured:native_final_write_authority_missing')
        need(self.executor.enabled and self.executor.ports.configured
             and callable(self.executor.authority_reader),
             "not_configured:bootstrap_trusted_sources_missing")
        need(bool(self.choices),"not_configured:bootstrap_profiles_missing")
        for choice in self.choices:
            self.executor.profiles.get(choice["profile_id"],self.clock()).load(self.clock())

    def current(self,operation_id,owner):
        return self.executor.journal.read(operation_id,owner)

    def resume_held(self,operation_id,owner,body):
        from .bootstrap_capacity import digest
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation
        record=self.current(operation_id,owner)
        if not record:
            return None
        preparation=record.get('preparation')
        need(preparation is not None and preparation['body_sha256']==digest(body),
             'bootstrap_preparation_original_body_changed')
        held=HeldUEAllocation.from_dict(preparation['held'])
        need((held.owner_id,held.operation_id,held.request_id)==(owner,operation_id,body['requestId']),
             'bootstrap_preparation_held_lineage_changed')
        return held

    async def choose(self,planner,intent,body,held,cohort,operation_id,owner):
        from .pretraffic_infrastructure import BootstrapAuthority,BootstrapCommand,invoke
        from .pretraffic_planning import reserved_identities
        from .membership import ServiceMembership
        from .bootstrap_capacity import digest
        from dataclasses import replace
        from .pending_admission import DeferredPendingAdmission
        # Before model/native deployment, require actual pinned profiles and all
        # trusted sources. Missing commission is a clear not_configured result.
        need(self.executor.enabled and self.executor.ports.configured,
             'not_configured:pretraffic_bootstrap_disabled_or_native_sources_missing')
        authority=await invoke(self.executor.authority_reader,operation_id,owner)
        need(type(authority) is BootstrapAuthority and authority.intent_id==intent.intent_id
             and authority.revision==intent.revision and authority.operation_id==operation_id
             and authority.user_owner==owner,'bootstrap_original_authority_changed')
        commissioned=[]
        for choice in self.choices:
            profile=self.executor.profiles.get(choice['profile_id'],self.clock())
            envelope,_,_=profile.load(self.clock())
            need(choice['template_id']==envelope['template_id'],'bootstrap_choice_template_changed')
            commissioned.append(dict(choice,envelope=envelope))
        need(commissioned,'not_configured:bootstrap_profile_choices_missing')
        snapshot=await invoke(planner.observation_source.snapshot_reserved,intent,held)
        need(0<=self.clock()-snapshot.observed_at<=snapshot.maximum_age_seconds,
             'bootstrap_planning_native_source_expired')
        rates=await invoke(planner.demand_reader,body,held)
        directions=getattr(rates,'direction_bps',None)
        identities=reserved_identities(cohort)
        need(isinstance(directions,dict) and set(directions)==reserved_identities(held)
             and identities==reserved_identities(held),
             'not_configured:bootstrap_complete_duplex_held_cohort_required')
        demand={d:sum(v[d] for v in directions.values()) for d in ('ul','dl')}
        membership=ServiceMembership(intent.intent_id,intent.revision,())
        from .model_planner import ResourceProposalProvider
        source=planner.proposal_provider
        need(isinstance(source,ResourceProposalProvider),'not_configured:bootstrap_model_transport_missing')
        # Reuse the real strict proposal transport. Bootstrap is a distinct
        # server-authorized context, not the legacy assign-only pretraffic view.
        provider=ResourceProposalProvider(source.model,policy_view={
            'base_options':source.policy_view,'bootstrap_targets':commissioned,
            'authorized_plan_ids':sorted(authority.authorized_plan_ids),
            'reserved_admission':cohort.to_dict(),
            'future_duplex_workload':directions,'measured_sender_load':False,
            'bootstrap_contract':{'actions':['add_upf','assign_sessions'],
                'one_reserved_pdu_only':True,'assignment_target':'logical commissioned target',
                'native_uuid':'deferred; never invent UUID',
                'pending_admissions_fields':['identity','owner_id','target_id','dependency_action_id',
                    'reservation_id','reservation_version','valid_from','valid_until']}},
            maximum_payload_bytes=source.maximum_payload_bytes,maximum_candidates=source.maximum_candidates,
            timeout_seconds=source.timeout_seconds)
        candidates=await provider.propose(intent,snapshot,membership)
        accepted=[]
        for plan in candidates:
            try:
                need((plan.intent_id,plan.intent_revision,plan.observation_id)==
                     (intent.intent_id,intent.revision,snapshot.observation_id)
                     and self.clock()<plan.expires_at<=intent.valid_until
                     and snapshot.observation_id in plan.evidence,'bootstrap_model_plan_stale')
                need(len(plan.actions)==2,'not_configured:bootstrap_requires_one_add_one_assignment')
                add,assign=plan.actions
                need(add.kind=='add_upf' and assign.kind=='assign_sessions'
                     and add.target_id==assign.target_id and not add.session_keys and not assign.session_keys
                     and not add.depends_on and tuple(assign.depends_on)==(add.action_id,)
                     and assign.expected_version=='dependency:'+add.action_id,
                     'bootstrap_plan_dependency_changed')
                matching=[c for c in commissioned if c['target_id']==add.target_id
                    and add.parameters=={'template_id':c['template_id'],'node_id':c['node_id']}]
                need(len(matching)==1,'bootstrap_target_template_not_commissioned')
                choice=matching[0]
                policies=[planner.validator.targets.get((a.kind,a.target_id)) for a in (add,assign)]
                need(all(p is not None and p.preserves_guarantees is True and not p.required_diagnosis
                         and not p.numeric_bounds for p in policies),
                     'bootstrap_target_guarantees_not_authorized')
                need(dict(policies[0].fixed_parameters)==add.parameters
                     and dict(policies[1].fixed_parameters)=={'mode':'new_sessions'},
                     'bootstrap_target_parameter_authority_changed')
                disruption=sum(p.disruption_seconds for p in policies)
                need(disruption<=intent.continuity_policy.maximum_interruption_seconds,
                     'bootstrap_continuity_not_authorized')
                assignment=SessionAssignment.from_action(assign)
                need(assignment.mode=='new_sessions' and len(assignment.pending_admissions)==1
                     and {p.identity.token for p in assignment.pending_admissions}==identities,
                     'not_configured:bootstrap_requires_one_measured_reserved_pdu')
                for pending in assignment.pending_admissions:
                    need(type(pending) is DeferredPendingAdmission and pending.owner_id==intent.intent_id
                         and pending.target_id==add.target_id and pending.dependency_action_id==add.action_id
                         and pending.reservation_id==held.reservation_id and pending.reservation_version==held.version
                         and pending.active(self.clock()) and pending.valid_until<=min(held.valid_until,intent.valid_until),
                         'bootstrap_deferred_reservation_changed')
                command=BootstrapCommand(plan.plan_id,add.action_id,add.target_id,choice['template_id'],
                    choice['node_id'],choice['profile_id'],authority.template_costs[choice['template_id']],
                    demand,asdict(assign))
                profile=self.executor.profiles.get(command.profile_id,self.clock())
                envelope,_,_=profile.load(self.clock())
                # Begin performs authoritative budget/template/plan/node CAS. No
                # native dispatch occurs until after preparation is persisted.
                accepted.append((replace(plan,cost=command.cost,disruption=disruption,
                                         expected_attainment=0,guarantees_preserved=True),command,envelope))
            except (ValueError,KeyError,TypeError):
                continue
        need(accepted,'not_configured:no_authorized_bootstrap_dependency_plan')
        plan,command,envelope=min(accepted,key=lambda v:(v[0].cost,v[0].disruption,v[0].plan_id))
        record=self.executor.journal.begin(command,authority,envelope)
        preparation={'held':held.to_dict(),'body_sha256':digest(body),'plan':plan.to_dict(),
                     'selected_identities':sorted(identities)}
        if record.get('preparation'):
            need(record['preparation']==preparation,'bootstrap_original_preparation_changed')
        else:
            record=self.executor.journal.cas(operation_id,owner,record,record['phase'],preparation=preparation)
        return record

    async def prepare(self,planner,intent,body,held,cohort,operation_id,owner):
        from .pretraffic_infrastructure import (BootstrapCommand,BootstrapPreparationPending,
            BootstrapPreparationRecovery)
        from .pending_admission import PreparedSessionAdmissions,AdmissionInstallation
        record=self.current(operation_id,owner)
        try:
            if not record or not record.get('preparation'):
                record=await self.choose(planner,intent,body,held,cohort,operation_id,owner)
            need(self.resume_held(operation_id,owner,body).to_dict()==held.to_dict(),
                 'bootstrap_original_held_changed')
            result=await self.executor.advance(operation_id,owner,BootstrapCommand(**record['command']))
            if result['pending']:
                raise BootstrapPreparationPending('bootstrap_preparation_pending')
            record=self.current(operation_id,owner)
            handles=tuple(AdmissionInstallation.from_dict(v) for v in result['admission_installations'])
            return PreparedSessionAdmissions(body['requestId'],held.reservation_id,held.version,handles,
                held_ue_allocation=held.to_dict(),pretraffic_plan=record['preparation']['plan'],
                selected_identities=tuple(record['preparation']['selected_identities']))
        except BootstrapPreparationPending:
            raise
        except Exception as error:
            if self.current(operation_id,owner):
                raise BootstrapPreparationRecovery('bootstrap_original_preparation_requires_recovery') from error
            raise

    async def validate_before_start(self,operation_id,owner):
        from .pretraffic_infrastructure import BootstrapCommand
        record=self.current(operation_id,owner)
        need(record is not None and record["phase"]=="ready","bootstrap_admissions_not_ready")
        profile=self.executor.profiles.get(record["profile_id"],self.clock())
        _,binding,_=await self.executor._proof(record,profile)
        from .bootstrap_capacity import BootstrapCapacityCertificate
        BootstrapCapacityCertificate(**record["materialization_certificate"]).validate(binding,self.clock())
        await self.executor.advance(operation_id,owner,BootstrapCommand(**record["command"]))

    async def compensate(self,operation_id,owner,controller,*,native_gone=False):
        from .pretraffic_infrastructure import invoke
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation,HeldUERepository
        result=await self.executor.compensate(operation_id,owner)
        if result.get('restored') is True:
            record=self.current(operation_id,owner)
            if record.get('preparation'):
                held=HeldUEAllocation.from_dict(record['preparation']['held'])
                current=await invoke(HeldUERepository(controller.store).current)
                if current is not None and current.reservation_id==held.reservation_id and current.state=="consumed":
                    need(native_gone is True,"bootstrap_consumed_reservation_requires_native_drain")
                else:
                    await invoke(HeldUERepository(controller.store).release,held)
        return result


def configured_bootstrap_bridge(context,settings,repository,providers,coordinator,*,clock=time.time):
    """Pinned config plus native ports only; no profile or authority from a request.

    Native load/node/envelope commissioning remains explicit. Missing ports
    create a disabled, readable contract and block before a held reservation.
    """
    from .bootstrap_capacity import BootstrapProfiles
    from .commissioned_upfs import CommissionedUpfRegistry
    from .pending_admission import DeferredAdmissionAuthority,AdmissionIdentity
    from backend.onprem.runtime.ue_reservation import HeldUERepository
    need(isinstance(settings,dict) and set(settings)<={'enabled','profiles','choices'},
         'invalid_bootstrap_factory_configuration')
    need(type(settings.get('enabled',False)) is bool,'invalid_bootstrap_enable_flag')
    entries=settings.get('profiles',[])
    profiles=BootstrapProfiles.pinned([(r['profile_id'],r['path'],r['sha256']) for r in entries])
    lifecycle=getattr(providers.get('upf'),'api',None)
    reader=getattr(lifecycle,'current_registry',None)
    registry=CommissionedUpfRegistry(reader) if callable(reader) else None
    controller=context.ue_controller
    def reservation_reader(reservation_id,owner,intent_id,operation_id):
        # Materializer supplies logical intent, original operation and user.
        operation=repository.operation(operation_id,owner)
        held=HeldUERepository(controller.store,clock=clock).current()
        need(operation['intent_id']==intent_id and held is not None and held.reservation_id==reservation_id
             and (held.owner_id,held.operation_id)==(owner,operation_id),
             'bootstrap_current_reserved_authority_changed')
        ids=frozenset(AdmissionIdentity(held.network_scope,r.supi,r.pdu_session_id,r.nonce).token
                      for r in held.allocation)
        return DeferredAdmissionAuthority(held,owner,intent_id,operation_id,ids,held.created_at,held.valid_until)
    authority=getattr(context,'intent_bootstrap_authority_reader',None)
    load=getattr(context,'intent_bootstrap_load_reader',None)
    node=getattr(context,'intent_bootstrap_node_reader',None)
    # No lower worker/held-launch final-write authority hook exists in this batch.
    # A config flag and fresh caller GETs cannot authorize asynchronous writes.
    native_final_write_authority_implemented=False
    enabled=(native_final_write_authority_implemented and settings.get('enabled') is True and lifecycle is not None and registry is not None
             and coordinator is not None and callable(authority) and callable(load) and callable(node))
    # Even disabled composition never uses a default unverified reader.
    executor=build_bootstrap_executor(repository,lifecycle,registry,coordinator,reservation_reader,
        authority,profiles,load_reader=load,node_reader=node,clock=clock,enabled=enabled)
    executor.native_final_write_authority_missing=(settings.get('enabled') is True
        and not native_final_write_authority_implemented)
    return BootstrapPreTrafficBridge(executor,settings.get('choices',()),clock=clock)
