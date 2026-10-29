"""Intent admission against reserved identities and actual current NF capacity.

There are no established UE/session metrics at this phase. The real model may
choose pending selection on commissioned ready UPFs; service effect is verified
only after native creation and traffic. Infrastructure creation is not silently
claimed by this admission planner.
"""
from dataclasses import replace,dataclass
import hashlib
import json
import time

from backend.onprem.runtime.ue_reservation import HeldUERepository,UEAdmissionLaunchProfile
from .domain import ContractError,finite
from .service_classification import configured_profile, configured_session_profile, matches_service_types
from .membership import ServiceMembership
from .observations import ObservationSnapshot
from .planning import ConstrainedPlanner,PlanningError
from .pending_admission import AdmissionIdentity,SessionAssignment,PreparedSessionAdmissions


def reserved_identities(reservation):
    return frozenset(AdmissionIdentity(reservation.network_scope,r.supi,r.pdu_session_id,r.nonce).token
                     for r in reservation.allocation)


@dataclass(frozen=True)
class ReservedAdmissionCohort:
    whole:object
    allocation:tuple

    @property
    def network_scope(self): return self.whole.network_scope
    @property
    def created_at(self): return self.whole.created_at
    @property
    def valid_until(self): return self.whole.valid_until
    def to_dict(self):
        from dataclasses import asdict
        value=self.whole.to_dict()
        value['whole_launch_counts']=value.pop('counts')
        value['allocation']=[asdict(r) for r in self.allocation]
        return value


class ReservedServiceCatalog:
    """Pinned future-launch classification; scenario names are not service proof."""
    def __init__(self,scenarios):
        self.scenarios=scenarios

    def __call__(self,body,reservation):
        if reservation.devices:
            # A physical-device count cannot classify two applications by SUPI.
            # These records came from the pinned server launch profile, not body.
            devices={d.supi:d for d in reservation.devices}
            owners={}
            if reservation.launch_profile_id.startswith('current-free-ran-'):
                from backend.onprem.scenarios.contracts import bind_scenarios
                owners={r['supi']:r for r in bind_scenarios(body,
                    [{'supi':r.supi,'slice':r.slice} for r in reservation.allocation])}
            result={}
            for reserved in reservation.allocation:
                specs=[s for s in devices[reserved.supi].sessions if s.session_id==reserved.session_id]
                if len(specs)!=1:
                    raise PlanningError('needs_definition:reserved_logical_session_classification_missing')
                spec=specs[0]
                profile_id=configured_session_profile(spec)
                if owners:
                    owner=owners[reserved.supi]
                    configured=self.scenarios.get(owner['scenario'])
                    if not isinstance(configured,dict) or not configured.get('configuration_id'):
                        raise PlanningError('needs_definition:future_launch_classification_missing')
                    profile_id=configured_profile(owner['scenario'],spec.service_type,reserved.slice)
                token=AdmissionIdentity(reservation.network_scope,reserved.supi,reserved.pdu_session_id,reserved.nonce).token
                result[token]={'service_type':spec.service_type,'region':spec.region,
                    'service_profile_id':profile_id,
                    'application_ids':list(spec.applications),
                    'configuration_id':'device-profile:'+reservation.launch_profile_id+':'+reservation.version,
                    'source':'configured-city-launch-classification/v1'}
            return result
        from backend.onprem.scenarios.contracts import bind_scenarios
        owners=bind_scenarios(body,[{'supi':r.supi,'slice':r.slice} for r in reservation.allocation])
        by_supi={r.supi:r for r in reservation.allocation}
        result={}
        for owner in owners:
            row=self.scenarios.get(owner['scenario'])
            if not isinstance(row,dict) or not row.get('configuration_id'):
                raise PlanningError('needs_definition:future_launch_classification_missing')
            reserved=by_supi[owner['supi']]
            token=AdmissionIdentity(reservation.network_scope,reserved.supi,reserved.pdu_session_id,reserved.nonce).token
            result[token]=dict(row,source='configured-city-launch-classification/v1',
                service_profile_id=configured_profile(owner['scenario'],row['service_type'],reserved.slice))
        return result


def selected_reserved_cohort(intent,reservation,classification):
    selected=[]
    selector=intent.selector
    for row in reservation.allocation:
        token=AdmissionIdentity(reservation.network_scope,row.supi,row.pdu_session_id,row.nonce).token
        if selector.supis and row.supi not in selector.supis:
            continue
        record=classification.get(token,{})
        if record.get('source')!='configured-city-launch-classification/v1' or not record.get('configuration_id'):
            raise PlanningError('needs_definition:future_launch_classification_missing')
        if not matches_service_types(selector.service_types,record.get('service_type'),record.get('service_profile_id','')):
            continue
        checks=((selector.regions,'region'),(selector.slice_ids,'slice_instance_id'))
        if any(values and record.get(field) is not None and record[field] not in values for values,field in checks):
            continue
        if any(values and not record.get(field) for values,field in checks):
            raise PlanningError('needs_definition:future_selector_classification_missing')
        if selector.applications:
            apps=set(record.get('application_ids',()))
            if not apps:
                raise PlanningError('needs_definition:future_application_classification_missing')
            if not apps & set(selector.applications):
                continue
            if not apps<=set(selector.applications):
                raise PlanningError('needs_definition:unsupported_reserved_application_granularity')
        selected.append(row)
    if not selected:
        raise PlanningError('needs_definition:no_matching_reserved_service_sessions')
    return ReservedAdmissionCohort(reservation,tuple(selected))


def requested_city_workload(body,reservation,*,session_compiler=None):
    """Compile the same future traffic contract as the native round, not telemetry."""
    if reservation.devices:
        return requested_session_workload(body,reservation,session_compiler)
    from backend.onprem.scenarios.contracts import bind_scenarios
    from backend.onprem.traffic.workload import workload_metadata_for_snapshot,applied_rates
    from backend.onprem.agents.native_observation import direction_rate
    owners=bind_scenarios(body,[{'supi':r.supi,'slice':r.slice} for r in reservation.allocation])
    snapshot={'allocation':body,'perUe':owners,'trafficMode':body.get('trafficMode','functional'),'actions':[]}
    if 'serviceRequirements' in body:
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        snapshot['serviceRequirements']=body['serviceRequirements']
        snapshot['resolvedRequirements']=ServiceRequirementCompiler.compile(body['serviceRequirements'],body,
            traffic_mode=body.get('trafficMode'))
    metadata=workload_metadata_for_snapshot(snapshot)
    rates=applied_rates(metadata)
    class RequestedRates(dict):
        traffic_profile=metadata['profileId']
        source='validated-city-workload/v1'
        direction_bps={AdmissionIdentity(reservation.network_scope,r.supi,r.pdu_session_id,r.nonce).token:
            {d:direction_rate(rates[r.supi],d)*1000 for d in ('ul','dl')} for r in reservation.allocation}
    return RequestedRates({AdmissionIdentity(reservation.network_scope,r.supi,r.pdu_session_id,r.nonce).token:
            sum(direction_rate(rates[r.supi],d) for d in ('ul','dl'))*1000 for r in reservation.allocation})


def requested_session_workload(body,reservation,compiler):
    """Forecast exact reserved PDUs using their pinned declared service specs.

    The access slice is a physical UE launch parameter and cannot classify all
    that UE's sessions. Each reserved PDU's own S-NSSAI selects its slice here.
    """
    from backend.onprem.runtime.topology import SLICES
    from backend.onprem.traffic.session_workload import SessionWorkloadCompiler,SessionWorkloadError,session_rates
    if not isinstance(compiler,SessionWorkloadCompiler):
        raise PlanningError('needs_definition:per_session_future_workload_compiler_required')
    devices={d.supi:d for d in reservation.devices}
    rows=[]
    for reserved in reservation.allocation:
        specs=[s for s in devices[reserved.supi].sessions if s.session_id==reserved.session_id]
        if len(specs)!=1:
            raise PlanningError('needs_definition:reserved_logical_session_classification_missing')
        spec=specs[0]
        slices=[name for name,value in SLICES.items()
                if value['sst']==spec.sst and int(value['sd'],16)==spec.sd]
        if len(slices)!=1:
            raise PlanningError('needs_definition:reserved_session_slice_not_commissioned')
        rows.append({'identity':AdmissionIdentity(reservation.network_scope,reserved.supi,
                     reserved.pdu_session_id,reserved.nonce).token,'slice':slices[0],
                      'service_type':spec.service_type,'region':spec.region,'ue_id':reserved.supi})
    try:
        metadata=compiler.compile(rows,traffic_mode=body.get('trafficMode','functional'),
                                  service_requirements=body.get('serviceRequirements'))
    except SessionWorkloadError as error:
        raise PlanningError('needs_definition:'+str(error)) from None
    class RequestedRates(dict):
        traffic_profile=metadata['profileId']
        source='validated-city-workload/v1'
        direction_bps={identity:{d:rate[d]*1000 for d in ('ul','dl')}
                       for identity,rate in session_rates(metadata).items()}
    return RequestedRates({identity:sum(rate.values())*1000
                           for identity,rate in session_rates(metadata).items()})


class NativePreTrafficObservationSource:
    """Real resource and whole-NF readers; no UE throughput or zero-load fallback."""
    def __init__(self,resource_telemetry,nf_load_reader,*,clock=time.time,maximum_age_seconds=30):
        self.resource_telemetry,self.nf_load_reader=resource_telemetry,nf_load_reader
        self.clock,self.maximum_age=clock,maximum_age_seconds

    async def snapshot_reserved(self,intent,reservation):
        from .service import invoke
        if self.nf_load_reader is None or self.resource_telemetry is None:
            raise PlanningError('needs_measurement:native_whole_nf_load_reader_missing')
        load=await invoke(self.nf_load_reader.collect)
        resources=await invoke(self.resource_telemetry.collect,(),())
        metrics=tuple(load.get('metrics',()))+tuple(resources.get('metrics',()))
        if not metrics:
            raise PlanningError('needs_measurement:native_nf_counters_not_ready')
        memberships=load.get('target_measurement_ids',())
        memberships=tuple(memberships.items()) if isinstance(memberships,dict) else tuple(memberships)
        versions=tuple(sorted(resources.get('target_versions',{}).items()))
        observed=min(m.observed_at for m in metrics)
        content=json.dumps([versions,[m.__dict__ for m in metrics]],sort_keys=True,allow_nan=False)
        identity=hashlib.sha256(content.encode()).hexdigest()
        return ObservationSnapshot(identity,observed,hashlib.sha256(json.dumps(versions).encode()).hexdigest(),
            (),metrics,tuple(sorted(resources.get('available_resources',{}).items())),
            maximum_age_seconds=self.maximum_age,target_versions=versions,target_measurement_ids=memberships)


class PreTrafficAdmissionPlanner:
    def __init__(self,proposal_provider,validator,*,observation_source,demand_reader,clock=time.time,bootstrap=None):
        self.proposal_provider,self.validator=proposal_provider,validator
        self.observation_source,self.demand_reader=observation_source,demand_reader
        self.clock=clock
        self.bootstrap=bootstrap

    async def plan(self,intent,body,reservation,*,cohort=None,target_constraint=None):
        from .service import invoke
        if intent.definition_issues(self.validator.authority):
            raise PlanningError('needs_definition:pretraffic_policy_authority')
        if self.observation_source is None or self.demand_reader is None:
            raise PlanningError('needs_measurement:pretraffic_native_capacity_or_requested_workload_missing')
        snapshot=await invoke(self.observation_source.snapshot_reserved,intent,reservation)
        if not isinstance(snapshot,ObservationSnapshot) or not 0<=self.clock()-snapshot.observed_at<=snapshot.maximum_age_seconds:
            raise PlanningError('needs_measurement:pretraffic_current_native_capacity_missing')
        # Requested workload is a forecast, never an actual sender measurement.
        requested=await invoke(self.demand_reader,body,reservation)
        whole_identities=reserved_identities(reservation)
        if cohort is None:
            raise PlanningError('needs_definition:reserved_target_classification_required')
        identities=reserved_identities(cohort)
        if not isinstance(requested,dict) or set(requested)!=whole_identities:
            raise PlanningError('needs_measurement:complete_reserved_workload_definition_missing')
        forecast_profile=getattr(requested,'traffic_profile','')
        forecast_source=getattr(requested,'source','')
        requested={k:finite(v,'requested reserved workload bps',0) for k,v in requested.items()}
        unselected_load=sum(value for key,value in requested.items() if key not in identities)
        membership=ServiceMembership(intent.intent_id,intent.revision,())
        from .actuators import ActionRequest
        native_targets=[]
        if target_constraint is not None:
            from .session_constraints import NewSessionRouteConstraint
            if (type(target_constraint) is not NewSessionRouteConstraint or target_constraint.intent_id!=intent.intent_id
                    or target_constraint.intent_revision!=intent.revision):
                raise PlanningError('needs_definition:server_route_constraint_authority_changed')
        for kind,target in self.validator.targets:
            if kind!='assign_sessions' or target_constraint is not None and target!=target_constraint.target_id:
                continue
            try:
                observed=await invoke(self.validator.registry.get(kind).provider.inspect,
                    ActionRequest(kind,target,intent.intent_id,'pretraffic-read-only','read-only'))
                if (observed.get('source')!='native-upf-controller' or not observed.get('native_upf_id')
                        or not all(observed.get(k) is True for k in ('ready','pfcp_associated','path_verified','smf_selected'))
                        or not 0<=self.clock()-observed.get('observed_at',float('-inf'))<=snapshot.maximum_age_seconds):
                    continue
                native_targets.append({k:observed[k] for k in ('target_id','native_upf_id','version','observed_at')})
            except Exception:
                continue
        if not native_targets:
            raise PlanningError('needs_measurement:ready_native_upf_selection_targets_missing')
        candidates=await invoke(self.proposal_provider.propose,intent,snapshot,membership,
            reserved_admission=cohort,native_admission_targets=native_targets,
            requested_workload={'source':forecast_source,'traffic_profile':forecast_profile,
                'target_payload_bps':{key:value for key,value in requested.items() if key in identities},
                'other_launch_payload_bps_worst_case':unselected_load,'measured_sender_load':False})
        admitted=[]
        for candidate in candidates:
            try:
                if target_constraint is not None and any(a.target_id!=target_constraint.target_id for a in candidate.actions):
                    raise PlanningError('needs_definition:proposal_violates_planned_new_session_upf')
                self._capacity(candidate,snapshot,requested,forecast_profile,forecast_source,unselected_load)
                admitted.append(await invoke(self.validator.validate,candidate,intent,snapshot,membership,
                                               reserved_identities=identities))
            except ContractError:
                continue
        if not admitted:
            raise PlanningError('needs_measurement:no_authorized_measured_pretraffic_plan')
        context=replace(self.validator.context(intent,snapshot,membership,
            frozenset(p.plan_id for p in admitted)),reserved_identities=identities)
        return ConstrainedPlanner().select(tuple(admitted),context)

    def _capacity(self,plan,snapshot,requested,forecast_profile='',forecast_source='',unselected_load=0):
        loads={}
        for action in plan.actions:
            if action.kind!='assign_sessions':
                raise PlanningError('needs_measurement:pretraffic_infrastructure_action_not_commissioned')
            assignment=SessionAssignment.from_action(action)
            loads[action.target_id]=loads.get(action.target_id,0)+sum(requested.get(p.identity.token,float('inf'))
                                                                    for p in assignment.pending_admissions)
        if not loads:
            raise PlanningError('No pending selections proposed')
        for target,future_load in loads.items():
            # Other groups keep default routing. Without a future exact native
            # destination, reserve their worst-case load here without steering them.
            future_load+=unselected_load
            policy=self.validator.targets.get(('assign_sessions',target))
            if policy is None:
                raise PlanningError('Unauthorized admission target')
            profile=self.validator.profiles.get(policy.calibration_profile_id)
            if profile is None or (profile.load_metric_name,profile.load_metric_source,profile.load_metric_scope)!=(policy.load_metric_name,policy.load_metric_source,policy.load_metric_scope):
                raise PlanningError('needs_measurement:pretraffic_load_metric_semantics_mismatch')
            if policy.load_metric_name!='offered_bps':
                if (policy.forecast_load_multiplier is None or policy.forecast_load_multiplier<1
                        or forecast_source!='validated-city-workload/v1' or not forecast_profile
                        or not forecast_profile==policy.forecast_packet_profile==profile.traffic_profile):
                    raise PlanningError('needs_measurement:wire_forecast_overhead_or_packet_profile_missing')
                future_load*=policy.forecast_load_multiplier
            allocation_target=policy.allocation_resource_id or target
            allocation={m.name:m for m in snapshot.fresh_metrics(self.clock()) if m.resource_id==allocation_target
                        and m.name in {'cpu_limits','memory_limits'}}
            if (profile is None or set(allocation)!={'cpu_limits','memory_limits'}
                    or allocation['cpu_limits'].unit!='cores' or allocation['memory_limits'].unit!='bytes'
                    or allocation['cpu_limits'].value!=profile.cpu_cores
                    or allocation['memory_limits'].value!=profile.memory_bytes):
                raise PlanningError('needs_measurement:current_nf_allocation_not_calibrated')
            ids=dict(snapshot.target_measurement_ids).get(target,())
            if not ids or set(ids)!=set(policy.measurement_resource_ids):
                raise PlanningError('needs_measurement:complete_current_nf_load_missing')
            current=[m for m in snapshot.fresh_metrics(self.clock()) if m.resource_id in ids
                     and m.name==policy.load_metric_name and m.unit=='bps' and m.statistic=='mean'
                     and (not policy.load_metric_source or m.source==policy.load_metric_source)
                     and (not policy.load_metric_scope or m.scope==policy.load_metric_scope)]
            if len(current)!=len(ids) or len({(m.observed_at,m.window_seconds) for m in current})!=1:
                raise PlanningError('needs_measurement:complete_current_nf_load_missing')
            scope=dict(policy.calibration_scope)
            scope['load_bps']=sum(m.value for m in current)+future_load
            if profile is None or not profile.supports(**scope,now=self.clock()):
                raise PlanningError('needs_measurement:reserved_load_outside_measured_capacity')


class PreTrafficAdmissionPreparer:
    """Concrete reserve → validated model choice → native SMF install workflow."""
    def __init__(self,controller,planner,admission_coordinator,*,profile_reader,network_scope,classification_reader=None,clock=time.time):
        self.controller,self.planner,self.coordinator=controller,planner,admission_coordinator
        self.profile_reader,self.network_scope,self.clock=profile_reader,network_scope,clock
        self.classification_reader=classification_reader

    def has_bootstrap(self,operation_id,owner):
        bridge=getattr(self.planner,'bootstrap',None)
        return bridge is not None and bridge.current(operation_id,owner) is not None

    async def compensate_bootstrap(self,operation_id,owner,*,native_gone=False):
        bridge=getattr(self.planner,'bootstrap',None)
        if bridge is None or not self.has_bootstrap(operation_id,owner):
            return {'restored':True}
        return await bridge.compensate(operation_id,owner,self.controller,native_gone=native_gone)

    async def validate_before_start(self,operation_id,owner):
        bridge=getattr(self.planner,'bootstrap',None)
        if bridge:
            await bridge.validate_before_start(operation_id,owner)

    async def __call__(self,intent,body,operation_id,*,owner):
        from .service import invoke
        from backend.onprem.scenarios.contracts import slice_counts,validate_scenario_request
        if intent.definition_issues(self.planner.validator.authority):
            raise PlanningError('needs_definition:pretraffic_policy_authority')
        valid=validate_scenario_request(body,allow_none=True)
        launch_args={}
        if getattr(getattr(self.controller,'driver',None),'backend',None)=='free-ran-ue':
            from backend.onprem.runtime.current_launch import current_device_launch_contract
            launch_args['launch_contract']=current_device_launch_contract(self.controller.driver)
        else:
            profile=await invoke(self.profile_reader,valid) if self.profile_reader else None
            if type(profile) is not UEAdmissionLaunchProfile:
                raise PlanningError('needs_measurement:commissioned_ue_launch_profile_missing')
            launch_args['profile']=profile
        bridge=getattr(self.planner,'bootstrap',None)
        if bridge:
            bridge.check_configured()
        try:
            reservation=bridge.resume_held(operation_id,owner,valid) if bridge else None
            if reservation is None:
                reservation=await self.controller.reserve(slice_counts(valid),owner_id=owner,operation_id=operation_id,
                    request_id=valid['requestId'],network_scope=self.network_scope,valid_until=intent.valid_until,**launch_args)
        except Exception:
            if bridge and bridge.current(operation_id,owner):
                from .pretraffic_infrastructure import BootstrapPreparationRecovery
                raise BootstrapPreparationRecovery('bootstrap_held_reservation_requires_recovery') from None
            raise PlanningError('needs_measurement:held_ue_reservation_unavailable') from None
        installed=[]
        try:
            if self.classification_reader is None:
                raise PlanningError('needs_definition:future_launch_classification_missing')
            classification=await invoke(self.classification_reader,valid,reservation)
            cohort=selected_reserved_cohort(intent,reservation,classification)
            if bridge:
                # This branch owns its compensation. Pending/unknown native
                # dispatch must never enter legacy installed[] cleanup below.
                return await bridge.prepare(self.planner,intent,valid,reservation,cohort,operation_id,owner)
            plan=await self.planner.plan(intent,valid,reservation,cohort=cohort)
            for action in plan.actions:
                for pending in SessionAssignment.from_action(action).pending_admissions:
                    installed.append(await invoke(self.coordinator.install,pending,operation_id))
            return PreparedSessionAdmissions(valid['requestId'],reservation.reservation_id,reservation.version,
                tuple(installed),held_ue_allocation=reservation.to_dict(),pretraffic_plan=plan.to_dict(),
                selected_identities=tuple(sorted(reserved_identities(cohort))))
        except Exception as error:
            if bridge and bridge.current(operation_id,owner):
                raise
            # Release installed selection policies before releasing the held UE;
            # a failed native cancellation retains the reservation for recovery.
            for handle in reversed(installed):
                current=await invoke(self.coordinator.api.inspect,handle.admission.identity.nonce)
                if current is None:
                    raise PlanningError('native_admission_compensation_requires_recovery')
                await invoke(self.coordinator.release,handle,current.version)
            await invoke(HeldUERepository(self.controller.store).release,reservation)
            if isinstance(error,ContractError) and str(error).startswith(('needs_measurement:','needs_definition:','not_configured:')):
                raise
            raise PlanningError('needs_measurement:pretraffic_preparation_unavailable') from None
