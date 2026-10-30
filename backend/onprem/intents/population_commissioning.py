"""Finite existing-four commissioning and durable post-topology service proof.

No model/API input can supply a receipt, native reader or publication port.
Topology publication is never receiver verification. Unknown measurement dispatch
is read back from its exact private receipt, never silently sampled again.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import asdict,dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import UUID

from .domain import ContractError,ServiceIntent,Selector,Objective,SessionBinding,SessionKey,FlowBinding
from .service import invoke
from .population_service import assert_population_open,record_population,PopulationForwardStopped
from .membership import MembershipResolver
from .observations import evaluate_objective
from backend.onprem.scenarios.population import ScenarioPopulationAdapter
from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
from backend.onprem.runtime.current_sessions import session_rows,IDENTITY_FIELDS

SOURCE_ROOT=Path(__file__).resolve().parents[3]
COMMISSION_SOURCE_PATHS=(
    'backend/onprem/intents/population_bridge.py',
    'backend/onprem/intents/population_service.py',
    'backend/onprem/intents/population_commissioning.py',
    'backend/onprem/intents/native_path_calibration.py',
    'backend/onprem/intents/native_path_capacity.py',
    'backend/onprem/intents/pending_admission.py',
    'backend/onprem/scenarios/population.py',
    'backend/onprem/runtime/current_sessions.py',
    'backend/onprem/runtime/owned_native_teardown_adapter.py',
    'backend/onprem/runtime/round_resources.py',
)


def source_tuple(rows):
    require(isinstance(rows,list) and len(rows)==len(COMMISSION_SOURCE_PATHS)
        and all(isinstance(r,dict) and set(r)=={'path','sha256'} for r in rows),'source_tuple_missing')
    paths=[]
    for row in rows:
        path=Path(row['path'])
        require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents))
            and isinstance(row['sha256'],str) and len(row['sha256'])==64
            and all(c in '0123456789abcdef' for c in row['sha256']),'source_tuple_unsafe')
        paths.append(path.resolve())
    require(len(set(paths))==len(paths) and set(paths)=={SOURCE_ROOT/p for p in COMMISSION_SOURCE_PATHS},'source_tuple_scope')


def require(value,reason):
    if not value:raise ContractError('population_commission:'+reason)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def cohort_fence(raw):
    rows=session_rows(raw)
    return digest({'roundId':raw.get('roundId'),'requestId':raw.get('requestId'),
        'generation':raw.get('generation'),'revision':raw.get('populationRevision',0),
        'sessions':[{k:r.get(k) for k in IDENTITY_FIELDS+('sst','sd','service_type','region')}
            for r in sorted(rows,key=lambda r:r['sessionKey'])]})
def fresh(at,now,age=10):
    require(type(at) in (int,float) and math.isfinite(at) and 0<=now-at<=age,'source_clock_stale')
def sealed(path,sha):
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not path.is_symlink(),'receipt_path')
    with path.open('rb') as stream:data=stream.read(1024*1024+1)
    require(len(data)<=1024*1024 and hashlib.sha256(data).hexdigest()==sha,'receipt_digest')
    return json.loads(data)


@dataclass(frozen=True)
class PopulationCommissionReceipt:
    encoded:bytes

    @property
    def data(self):return json.loads(self.encoded)
    def __post_init__(self):
        d=self.data
        require(set(d)=={'schema','receipt_id','owner','intent_id','intent_revision','lease_id','start_operation_id','request_id',
            'generation','observed_at','valid_until','network_scope','context','namespace','boot_id','smf_boot_epoch','native_config_sha256',
            'driver_profile','nf_identities_sha256','targets','receiver_pins','source_files','admission_trial',
            'max_devices','max_sessions','combined_budget_kbps','rates_kbps','realtime_control','publication_directory'},'receipt_schema')
        require(d['schema']=='cityverse.existing-four-population-commission/v1','receipt_schema')
        for key in ('receipt_id','boot_id'):require(str(UUID(d[key]))==d[key],'receipt_identity')
        require(type(d['generation']) is int and d['generation']>0 and type(d['intent_revision']) is int
            and d['intent_revision']>0 and d['context']=='kubernetes-admin@cityverse-wsl'
            and d['namespace']=='free5gc-onprem','receipt_scope')
        require(d['max_devices']==2 and d['max_sessions']==4 and d['combined_budget_kbps']==8000,'finite_commission_scope')
        require(type(d['rates_kbps']) is dict and set(d['rates_kbps'])=={'ul','dl'}
            and all(type(v) is int and 0<v<=2000 for v in d['rates_kbps'].values()),'finite_offered_envelope')
        require(type(d['observed_at']) in (int,float) and type(d['valid_until']) in (int,float)
            and 0<d['valid_until']-d['observed_at']<=1800,'original_receipt_expiry')
        require(len(d['targets'])==4 and {t['logical_target'] for t in d['targets']}=={'embb','urllc','mmtc','v2x'}
            and len({t['native_upf_id'] for t in d['targets']})==4,'existing_four_only')
        for target in d['targets']:
            require(set(target)=={'logical_target','target_id','native_upf_id','node_ip','upf_epoch'},'target_shape')
            require(str(UUID(target['native_upf_id']))==target['native_upf_id']
                and all(isinstance(target[k],str) and target[k] for k in target),'native_target_identity')
        source_tuple(d['source_files'])
        require(isinstance(d['receiver_pins'],list) and len(d['receiver_pins'])==4
            and all(set(r)=={'pod_uid','image','endpoint'} and all(isinstance(v,str) and v for v in r.values()) for r in d['receiver_pins']), 'receiver_identity_missing')
        realtime_control=d['realtime_control'];require(set(realtime_control)=={'selector','objectives'},'medical_scope')
        selector=Selector(**realtime_control['selector'])
        require(selector.service_types==('realtime_control',) and not selector.supis and realtime_control['objectives'],'medical_scope')
        for value in realtime_control['objectives']:require(Objective(**value).scope=='per_session','medical_scope')
        # A separate native two-device admission trial is mandatory. An old
        # path profile or a future-count assertion cannot replace this evidence.
        trial=d['admission_trial']
        require(set(trial)=={'source','configuration_sha256','observed_at','devices','sessions','profile'}
            and trial['source']=='native-population-admission-trial/v1'
            and trial['configuration_sha256']==d['native_config_sha256'],'admission_trial_missing')
        require(len(trial['devices'])==2 and len({r['pod_uid'] for r in trial['devices']})==2
            and len({r['supi'] for r in trial['devices']})==2
            and all(all(r.get(k) for k in ('pod_uid','container_id','process_epoch','supi')) for r in trial['devices']), 'actual_two_device_trial_required')
        require(len(trial['sessions'])==4 and len({r['session_key'] for r in trial['sessions']})==4
            and {r['service_type'] for r in trial['sessions']}=={'video','realtime_control'}
            and all(all(r.get(k) for k in ('session_key','sm_context_ref','pfcp_seid','upf_epoch')) for r in trial['sessions']), 'actual_four_session_trial_required')
        from .native_path_capacity import PathCalibration
        profile=PathCalibration(**trial['profile'])
        proof,_=profile.load(d['observed_at'])
        require(proof['schema']=='cityverse.path-calibration/v2' and trial['observed_at']<=d['observed_at']
            and d['valid_until']<=profile.valid_until and len(proof['all_job_bindings'])==4
            and set(proof['all_job_bindings'])=={r['session_key'] for r in trial['sessions']}
            and {r['podUid'] for r in proof['all_job_bindings'].values()}=={r['pod_uid'] for r in trial['devices']},'admission_trial_lineage')
        devices={r['supi']:r for r in trial['devices']}
        actual=proof['native_census_before']
        from .native_path_capacity import calibration_cohort
        native_rows=calibration_cohort(actual,proof['all_job_bindings'])
        require(actual['smf_boot_epoch']==d['smf_boot_epoch'],'admission_trial_SMF_epoch')
        pins={(r['node_ip'],r['upf_id'],r['upf_epoch']) for r in actual['upfs']}
        require(pins=={(r['node_ip'],r['native_upf_id'],r['upf_epoch']) for r in d['targets']},'admission_trial_target_lineage')
        kinds={supi:[] for supi in devices}
        for claimed in trial['sessions']:
            row=native_rows[claimed['session_key']]
            binding=proof['all_job_bindings'][claimed['session_key']]
            device=devices.get(row['supi'])
            require(device is not None and (binding.get('supi'),binding.get('podUid'),binding.get('containerId'),binding.get('processEpoch'))
                ==(device['supi'],device['pod_uid'],device['container_id'],device['process_epoch']),'admission_trial_device_lineage')
            require((claimed['sm_context_ref'],claimed['pfcp_seid'],claimed['upf_epoch'])
                ==(row['smContextRef'],row['pfcpSeid'],row['upfEpoch']),'admission_trial_session_lineage')
            kinds[row['supi']].append(claimed['service_type'])
        require(all(sorted(v)==['realtime_control','video'] for v in kinds.values()),'admission_trial_per_device_services')
        require(Path(d['publication_directory']).is_absolute(),'publication_directory')


class ExistingFourPopulationAdapter(ScenarioPopulationAdapter):
    """The public capability is evaluated, not a commissioned=True assignment."""
    @property
    def commissioned(self):return self.commissioning is not None and self.commissioning.available()
    @commissioned.setter
    def commissioned(self,value):self.commissioning=None
    @property
    def capabilities(self):
        valid=self.configured and self.commissioned
        return dict(configured=self.configured,fleet_join=valid,fleet_leave=valid,
            reason=None if valid else 'native_v2_population_not_commissioned')
    async def validate_population(self,intent,owner,request,operation_id=None):
        require(self.commissioning is not None,'not_configured')
        return await invoke(self.commissioning.check,intent,owner,request,operation_id)
    async def verify_population(self,service,operation,owner):
        require(self.commissioning is not None,'post_topology_not_configured')
        return await self.commissioning.advance(service,operation,owner)


class NativePopulationCommissioning:
    def __init__(self,context,service,receipt_file,receipt_sha256,*,clock=time.time,guest_reader=None):
        from .native_path_capacity import NativeCalibratedPathCapacityReader
        from backend.onprem.runtime.owned_native_teardown_adapter import NativeTeardownSource
        self.context,self.service,self.clock=context,service,clock
        self.path,self.sha=Path(receipt_file),receipt_sha256
        self.source=getattr(context,'intent_population_native_source',None)
        self.publisher=getattr(context,'intent_path_capacity_reader',None)
        self.resources=context.round_coordinator.resources
        self.source_factory=getattr(self.resources,'owned_native_teardown_factory',None)
        require((type(self.source) is NativeTeardownSource or self.source is None and callable(self.source_factory))
            and type(self.publisher) is NativeCalibratedPathCapacityReader
            and callable(self.publisher.hardware_reader) and self.publisher.publication_store is not None,'actual_ports_not_configured')
        self.guest_reader=guest_reader or (lambda:Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    def current_source(self):
        """Reuse the server-pinned normal cleanup factory as a read-only source.

        Its native writers cannot be entered through this object: the supplied
        mutation guard always denies. No second URL, credentials or census DI.
        """
        from backend.onprem.runtime.owned_native_teardown_adapter import NativeTeardownSource
        if self.source is not None:return self.source
        snapshot=self.context.round_coordinator.ledger.current_service_scope()
        def deny_mutation(*args,**kwargs):
            raise ContractError('population_commission:read_only_native_source')
        adapter=self.source_factory(snapshot,deny_mutation)
        require(type(adapter.source) is NativeTeardownSource,'actual_native_source_not_configured')
        return adapter.source
    def receipt(self):
        return PopulationCommissionReceipt(json.dumps(sealed(self.path,self.sha),sort_keys=True,allow_nan=False).encode())
    def available(self):
        try:
            receipt=self.receipt().data
            current=self.service.repository.get(receipt['intent_id'],receipt['owner'])
            intent=ServiceIntent.from_dict({k:v for k,v in current.items() if k in ServiceIntent.__dataclass_fields__})
            self.check(intent,receipt['owner'],{'expected_population_revision':0,'add_counts':{'embb':0},'remove_device_ids':[]})
            return True
        except Exception:return False
    def check(self,intent,owner,request,operation_id=None):
        d=self.receipt().data
        assert_population_open(self.service.repository,intent.intent_id,operation_id,owner)
        require(type(getattr(self.context.round_coordinator,'_population_window_lock',None)) is asyncio.Lock,
            'normal_sender_window_not_configured')
        source=self.current_source()
        cleanup=bool(request.get('remove_device_ids')) and not any(request.get('add_counts',{}).values())
        require((intent.intent_id,owner,intent.revision)==(d['intent_id'],d['owner'],d['intent_revision']),'owner_or_revision_changed')
        current=self.service.repository.get(intent.intent_id,owner)
        require(current['revision']==intent.revision,'current_revision_changed')
        entry=self.context.round_coordinator.ledger.intent_traffic_lease()
        lease=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
        require(lease and (lease.lease_id,lease.operation_id,lease.request_id,lease.owner_id,lease.intent_id)==
            (d['lease_id'],d['start_operation_id'],d['request_id'],owner,intent.intent_id),'current_lease_changed')
        store=source.driver.store if getattr(source.driver,'store',None) else self.context.ue_controller.store
        fleet=store.current()
        require(fleet and fleet['generation']==d['generation'],'generation_changed')
        devices=[row['device'] for row in fleet['allocation']]
        require(len({v['device_id'] for v in devices})==len(devices) and all(v['network_scope']==d['network_scope'] for v in devices),'current_device_scope_changed')
        added=sum(request.get('add_counts',{}).values());removed=set(request.get('remove_device_ids',()))
        owned_ids={v['device_id'] for v in devices};delta=None
        if operation_id:
            from backend.onprem.scenarios.population import read
            operation=self.service.repository.operation(operation_id,owner)
            require(operation['intent_id']==intent.intent_id and operation['revision']==intent.revision
                and operation['kind']=='population_change' and operation.get('population_request')==request
                and digest(operation.get('population_intent'))==digest(intent.to_dict()),'original_population_operation_changed')
            record=read(self.context.round_coordinator.ledger,operation_id,owner)
            if operation.get('population_topology_complete'):
                require(record is not None and record.get('phase')=='completed' and record.get('pending') is False,
                    'original_topology_commit_unverified')
            if record:
                require(record['request']==request and record['owner_id']==owner and record['generation']==d['generation'],
                    'original_delta_request_changed');delta=record.get('delta')
        require(removed<=owned_ids or delta and removed==set(delta['removed_device_ids']),'foreign_departure')
        future=(len((owned_ids|{v['device_id'] for v in delta['added_devices']})-removed)
            if delta else len(devices)+added-len(removed))
        require(1<=future<=2,'finite_device_scope_exceeded_use_full_release_for_empty')
        require(all(len(v['sessions'])==2 and {s['service_type'] for s in v['sessions']}=={'video','realtime_control'} for v in devices),'two_session_device_scope')
        require(asdict(source.driver.device_profile)==d['driver_profile'],'driver_profile_changed')
        for row in d['source_files']:
            path=Path(row['path']);require(path.is_absolute() and not path.is_symlink()
                and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],'source_drift')
        native,epochs,at=source.nf_identities()
        census,sessions,bindings,census_at=source.census()
        require(digest(native)==d['nf_identities_sha256'] and source.configuration_seal_reader()==d['native_config_sha256'],'native_process_or_config_changed')
        require(census['smf_boot_epoch']==d['smf_boot_epoch'],'SMF_process_epoch_changed')
        expected={(v['logical_target'],v['native_upf_id'],v['node_ip'],v['upf_epoch']) for v in d['targets']}
        require({(v.logical_target,v.native_upf_id,v.node_ip,v.upf_epoch) for v in bindings}==expected,'target_incarnation_changed')
        require(all(json.loads(v.session_key)[0]==d['network_scope'] for v in sessions),'foreign_native_network')
        declared={v['supi']:v for v in devices}
        require(len(declared)==len(devices) and all(json.loads(v.session_key)[1] in declared for v in sessions),'foreign_native_device')
        for row in census['sessions']:
            device=declared.get(row['supi'])
            require(device is not None and len([s for s in device['sessions']
                if (s['dnn'],s['sst'],s['sd'])==(row['dnn'],row['sst'],row['sd'])])==1,'native_service_scope_changed')
        require(self.guest_reader()==d['boot_id'],'host_boot_changed')
        from backend.onprem.runtime.round_resources import native_receiver_pods,TRAFFIC_IMAGE,ENDPOINTS
        receivers=native_receiver_pods(source.pods(),d['namespace'])
        actual_receivers=[dict(pod_uid=receivers[k]['metadata']['uid'],image=TRAFFIC_IMAGE,endpoint=ENDPOINTS[k]) for k in sorted(receivers)]
        require(sorted(actual_receivers,key=lambda r:r['pod_uid'])==sorted(d['receiver_pins'],key=lambda r:r['pod_uid']),'receiver_incarnation_changed')
        # The receipt cannot substitute an unrelated receiver trial on another
        # Pod incarnation that happened to use the same logical UPF alias.
        from .native_path_capacity import PathCalibration
        trial,_=PathCalibration(**d['admission_trial']['profile']).load(d['observed_at'])
        nf=json.loads(dict(native)[trial['target_id']])
        require((trial['nf_samples'][0]['pod_uid'],trial['nf_samples'][0]['container_id'])==(nf[0],nf[1]),
            'admission_trial_NF_incarnation_changed')
        for row in d['source_files']:
            require(hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256'],'closing_source_drift')
        # Final original clocks after every blocking file/native/SQL read.
        closing_fleet=store.current()
        require(closing_fleet is not None and all(closing_fleet.get(k)==fleet.get(k)
            for k in ('generation','population_revision','populationRevision','allocation')),'closing_generation_or_allocation_changed')
        closing_entry=self.context.round_coordinator.ledger.intent_traffic_lease()
        require(closing_entry and closing_entry['lease']==entry['lease']
            and self.service.repository.get(intent.intent_id,owner)['revision']==intent.revision,'closing_owner_or_revision_changed')
        assert_population_open(self.service.repository,intent.intent_id,operation_id,owner)
        now=self.clock();fresh(at,now);fresh(census_at,now)
        require(d['observed_at']<=now and (cleanup or now<min(d['valid_until'],intent.valid_until,lease.valid_until)), 'commission_expired')
        return {'receipt_sha256':self.sha,'cleanup':cleanup,'generation':fleet['generation']}

    async def advance(self,service,operation,owner):
        """One durable forward phase. All receipts remain in operations.value."""
        from .native_path_calibration import NativePathCalibrationRunner
        from .native_path_capacity import PathCalibration
        from .observations import ObservationSnapshot
        from dataclasses import replace
        opid=operation['operation_id'];request=operation['population_request']
        intent=ServiceIntent.from_dict(operation['population_intent']);d=self.receipt().data
        await invoke(self.check,intent,owner,request,opid)
        async def persist(state,stage):
            if not await invoke(record_population,service,opid,owner,status='running',stage=stage,population_commission=state,
                    population={'pending':True,'phase':stage}):
                raise PopulationForwardStopped('population_forward_authority_closed')
        state=deepcopy(operation.get('population_commission'))
        raw=await invoke(self.context.round_coordinator.ledger.current_service_scope)
        require(raw.get('schemaVersion')=='cityverse.current-state/v2','native_v2_current_cohort_required')
        rows=session_rows(raw);keys=sorted(r['sessionKey'] for r in rows)
        require(1<=len(keys)<=4 and len(keys)==len(set(keys)),'exact_remaining_cohort_required')
        fence=cohort_fence(raw)
        if state is None:
            state={'phase':'measure','cohort':fence,'receipt_sha256':self.sha,'profiles':{},'target':None,'child_id':None}
            await persist(state,'population_needs_measurement')
        require(state['cohort']==fence and state['receipt_sha256']==self.sha,'post_topology_cohort_changed')
        # Exact current classification, including pending additions, is read from
        # the native observer rather than synthesized from requested metadata.
        batch=await invoke(service.observer.session_binding_reader.current_bindings,raw)
        sessions=[]
        for proof in batch.values():
            value=dict(proof['binding']);value['key']=SessionKey(**value['key'])
            value['flows']=tuple(FlowBinding(**f) for f in value.get('flows',()))
            sessions.append(SessionBinding(**value))
        sessions=tuple(sessions)
        require({s.key.token for s in sessions}==set(keys),'native_session_cohort_changed')
        video=MembershipResolver().resolve(intent,sessions)
        medical_intent=replace(intent,selector=Selector(**d['realtime_control']['selector']))
        realtime_control=MembershipResolver().resolve(medical_intent,sessions)
        require(video.members and realtime_control.members and not video.rejected and not realtime_control.rejected
            and {m.session_token for m in video.members}|{m.session_token for m in realtime_control.members}==set(keys),'full_service_classification_required')
        targets={r['upf']:sorted(r2['sessionKey'] for r2 in rows if r2['upf']==r['upf']) for r in rows}
        jobs=await invoke(self.resources.traffic_jobs,raw)
        offered={}
        for job in jobs:
            key=job['binding']['sessionKey'];direction=job['direction'];rate=job['offered_mbps']*1000
            require(key in keys and direction in ('ul','dl') and type(rate) in (int,float)
                and rate>0 and int(rate)==rate and direction not in offered.setdefault(key,{}),'current_full_job_envelope_missing')
            offered[key][direction]=int(rate)
        require(set(offered)==set(keys) and all(set(v)=={'ul','dl'} for v in offered.values())
            and sum(sum(v.values()) for v in offered.values())<=8000,'current_combined_budget_exceeded')
        runner=NativePathCalibrationRunner(self.resources,self.publisher.api,self.publisher.nf_load_reader,
            hardware_reader=self.publisher.hardware_reader,clock=self.clock)
        directory=Path(d['publication_directory']);require(directory.is_dir() and not directory.is_symlink(),'publication_directory_missing')
        if state['phase'] in {'measure','measuring','publishing'}:
            for target,selected in sorted(targets.items()):
                if target in state['profiles']:continue
                name='population-'+opid+'-'+hashlib.sha256(target.encode()).hexdigest()[:12]
                path=directory/(name+'.json')
                if state['phase']=='measuring':
                    # A process restart cannot infer a lost sampler ACK from a
                    # missing file. Wait for exact original evidence readback.
                    if not path.is_file():return False
                    evidence=json.loads(path.read_bytes())
                    require(evidence['profile_id']==name,'measurement_readback_identity_changed')
                    profile=PathCalibration(name,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),
                        min(d['valid_until'],evidence['calibrated_at']+900),.9)
                    profile.load(self.clock())
                elif state['phase']=='publishing':
                    profile=PathCalibration(**state['target']['profile'])
                    require(state['target']['target_id']==target,'publication_checkpoint_changed')
                else:
                    rates=offered[selected[0]]
                    require(all(offered[k]==rates for k in selected) and all(rates[k]<=d['rates_kbps'][k] for k in rates),'target_offered_envelope_unsupported')
                    # Use the SAME lock as ordinary coordinator sender windows.
                    # A private/new lock would not prevent two independent 8M
                    # windows from overlapping on the actual fleet.
                    lock=getattr(self.context.round_coordinator,'_population_window_lock',None)
                    require(type(lock) is asyncio.Lock,'normal_sender_window_not_configured')
                    remaining=min(d['valid_until'],intent.valid_until)-self.clock()
                    require(remaining>0,'commission_expired_waiting_sender_window')
                    try:
                        await asyncio.wait_for(lock.acquire(),timeout=remaining)
                    except TimeoutError:
                        raise ContractError('population_commission:sender_window_deadline_exceeded') from None
                    try:
                        # Queueing behind a normal sender grants no new time,
                        # ownership or current-cohort authority. Persisting the
                        # measuring marker happens ONLY after this fresh check.
                        await invoke(self.check,intent,owner,request,opid)
                        current=await invoke(self.context.round_coordinator.ledger.current_service_scope)
                        require(cohort_fence(current)==fence,'cohort_changed_waiting_sender_window')
                        state.update(phase='measuring',target={'target_id':target,'file':str(path),
                            'registry_revision':await invoke(self.publisher.registry_revision)})
                        await persist(state,'population_needs_measurement')
                        require(getattr(self.context.round_coordinator,'_population_window_lock',None) is lock,
                            'normal_sender_window_changed')
                        await invoke(assert_population_open,service.repository,intent.intent_id,opid,owner)
                        require(self.clock()<min(d['valid_until'],intent.valid_until),'commission_expired_before_measurement')
                        evidence=await runner.run(raw,profile_id=name,target_id=target,target_session_keys=selected,
                            rates_kbps=rates,functional_combined_budget_kbps=8000,duration_seconds=10)
                    finally:
                        lock.release()
                    # File publication and normal reconciliation do not hold
                    # the sender lock. In particular reconcile may request a
                    # normal measurement window itself.
                    remaining=int(d['valid_until']-evidence['calibrated_at'])
                    require(remaining>=1,'commission_expired_during_measurement')
                    profile=await invoke(runner.pin_receipt,evidence,path,valid_seconds=min(900,remaining))
                state['target']['profile']=asdict(profile);state['phase']='publishing'
                await persist(state,'population_publishing_profile')
                await invoke(self.check,intent,owner,request,opid)
                await invoke(self.publisher.publish_calibration,profile,sessions,
                    expected_registry_revision=state['target']['registry_revision'])
                state['profiles'][target]=asdict(profile);state.update(phase='measure',target=None)
                await persist(state,'population_needs_measurement')
            current=await invoke(self.publisher.current_capacities,sessions)
            require(set(keys)<=set(current),'new_cohort_profiles_not_current')
            state['phase']='reconcile_pending';state['child_key']='population-effect:'+opid
            await persist(state,'population_verifying')
            await invoke(assert_population_open,service.repository,intent.intent_id,opid,owner)
            child=await service.reconcile(intent.intent_id,owner,state['child_key'])
            state['child_id']=child['operation_id'];state['phase']='reconciling'
            await persist(state,'population_verifying')
            return False
        if state['phase']=='reconcile_pending':
            with service.repository.connect() as db:
                child=db.execute('SELECT id FROM operations WHERE owner=? AND key=?',(owner,state['child_key'])).fetchone()
            if child is None:return False  # unknown create ACK; never new key/dispatch
            state['child_id']=child[0];state['phase']='reconciling';await persist(state,'population_verifying')
        child=await invoke(service.repository.operation,state['child_id'],owner)
        require(child['intent_id']==intent.intent_id and child['revision']==intent.revision
            and child['kind']=='reconcile','foreign_effect_child')
        if child['status'] in {'queued','running'}:return False
        require(child['status']=='completed' and child.get('effect',{}).get('met') is True
            and child.get('effect',{}).get('protected_services_met') is True,'normal_reconcile_effect_unverified')
        after=ObservationSnapshot.from_dict(child['after'])
        require({s.key.token for s in after.sessions}==set(keys) and after.observed_at>operation['created_at'],'effect_cohort_or_window_changed')
        now=self.clock();fresh(after.observed_at,now,15)
        for contract,members,objectives in ((intent,video,intent.objectives),(medical_intent,realtime_control,tuple(Objective(**v) for v in d['realtime_control']['objectives']))):
            tokens=tuple(m.session_token for m in members.members)
            require(all(evaluate_objective(o,after,tokens,now).status=='passed' for o in objectives),'fresh_video_or_medical_receiver_unverified')
        state['phase']='verified';state['observation_id']=after.observation_id
        await invoke(self.check,intent,owner,request,opid)
        await persist(state,'population_verified')
        return True
