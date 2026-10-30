"""Private, exact-owned FreeRAN V2 attachment/receiver/stop/reconnect acceptance.

Requires a real commissioned PDU launch profile and the normal native launcher
environment. Default mode only checks sealed inputs. --execute-native is for the
authorized operator. This is native commissioning, not formal AI/SLA acceptance
or a transparent migration. No guessed PDU IDs or policy-safety bypasses.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from backend.onprem.evidence.collector import configured_native_collector
from backend.onprem.intents.integration import load_configuration
from backend.onprem.intents.pending_admission import AdmissionIdentity,AdmissionInstallation,PendingAdmission,PendingAdmissionCoordinator,FixedOriginSmfAdmissionApi
from backend.onprem.intents.resource_adapters import _token_file_reader
from backend.onprem.runtime.current_sessions import session_rows,V2
from backend.onprem.runtime.kubeadm import configured_driver
from backend.onprem.runtime.ue import EvidenceUnavailable,GenerationStore,UEController,shards,seconds
from backend.onprem.runtime.ue_reservation import UEAdmissionLaunchProfile,HeldUEAllocation,HeldUERepository
from backend.onprem.policy.safety import policy_inventory_clear,policy_quarantine_clear

ZERO=dict.fromkeys(('embb','urllc','mmtc','v2x'),0)


def private_failure(output,name,error):
    """Keep primary and cleanup diagnostics separate, without stdout disclosure."""
    path=Path(output)/(name+'.txt')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        traceback.print_exception(type(error),error,error.__traceback__,file=stream)


def sealed_json(path,digest):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():raise ValueError('Absolute sealed input required')
    raw=path.read_bytes()
    if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Sealed input digest differs')
    return json.loads(raw)


def validate_job(path,digest):
    """Read-only launcher preflight; validates schema, bounds and native intent."""
    job=sealed_json(path,digest)
    required={'schemaVersion','execute_native','spatial_request','spatial_sha256','owner','operation','upf_target','output'}
    optional={'cycles','timeout','cleanup_timeout','replacement_upf_target','profile_valid_seconds',
        'native_census','native_census_sha256','launch_profile','launch_profile_sha256',
        'supi','service_type','region','logical_session_id','recover_reservation','recover_reservation_sha256',
        'recovery_only','recover_consumed_reservation','recover_consumed_reservation_sha256','recover_installation','recover_installation_sha256',
        'recover_retirement_generation','recover_retirement_pod_uid','recover_failed_native_zero'}
    if (not isinstance(job,dict) or not required<=set(job) or set(job)-required-optional
            or job['schemaVersion']!='cityverse.free-ran-device-smoke/v1' or job['execute_native'] is not True):
        raise ValueError('Invalid private FreeRAN native job schema or execution intent')
    census_fields={'native_census','native_census_sha256'}
    profile_fields={'launch_profile','launch_profile_sha256'}
    if not ((census_fields<=set(job) and not profile_fields&set(job))
            or (profile_fields<=set(job) and not census_fields&set(job))):
        raise ValueError('Exactly one complete sealed native census or launch profile required')
    if 'native_census' in job and not {'supi','service_type','region','logical_session_id'}<=set(job):
        raise ValueError('Native census derivation requires exact service scope')
    consumed_fields={'recover_consumed_reservation','recover_consumed_reservation_sha256','recover_installation','recover_installation_sha256'}
    if 'recovery_only' in job and type(job['recovery_only']) is not bool:raise ValueError('Explicit recovery-only boolean required')
    if 'recover_failed_native_zero' in job and (job.get('recovery_only') is not True or job['recover_failed_native_zero'] is not True or 'recover_retirement_generation' in job):
        raise ValueError('Explicit failed native-zero operator reconciliation requires recovery-only mode')
    if job.get('recovery_only') is True:
        if not consumed_fields<=set(job) or 'launch_profile' not in job or 'recover_reservation' in job:
            raise ValueError('Recovery-only requires exact consumed reservation and installation')
        original=HeldUEAllocation.from_dict(sealed_json(job['recover_consumed_reservation'],job['recover_consumed_reservation_sha256']))
        installation=AdmissionInstallation.from_dict(sealed_json(job['recover_installation'],job['recover_installation_sha256']))
        if original.owner_id!=job['owner'] or installation.admission.owner_id!=job['owner']:
            raise ValueError('Recovery-only owner differs')
    elif consumed_fields&set(job):raise ValueError('Consumed recovery references require recovery-only mode')
    if {'recover_retirement_generation','recover_retirement_pod_uid'}&set(job):
        if (job.get('recovery_only') is not True or type(job.get('recover_retirement_generation')) is not int
                or job['recover_retirement_generation']<=1 or not isinstance(job.get('recover_retirement_pod_uid'),str)
                or not 1<=len(job['recover_retirement_pod_uid'])<=128):
            raise ValueError('Explicit retirement generation and original Pod UID required for recovery-only')
    recovery_fields={'recover_reservation','recover_reservation_sha256'}
    if recovery_fields&set(job) and not recovery_fields<=set(job):
        raise ValueError('Complete sealed held recovery reference required')
    if 'launch_profile' in job:
        profile=UEAdmissionLaunchProfile.from_dict(sealed_json(job['launch_profile'],job['launch_profile_sha256']))
        if profile.source!='native-ue-device-launch-profile/v2' or (profile.valid_until<=time.time() and job.get('recovery_only') is not True):
            raise ValueError('Sealed native launch profile expired or wrong source')
    if 'recover_reservation' in job:
        recovery=sealed_json(job['recover_reservation'],job['recover_reservation_sha256'])
        if recovery.get('owner_id')!=job['owner'] or recovery.get('state')!='held' or recovery.get('consumed_generation') is not None:
            raise ValueError('Exact unconsumed owned recovery reference required')
    values=dict(cycles=2,timeout=240,cleanup_timeout=360,profile_valid_seconds=1800,**{k:v for k,v in job.items() if k not in optional})
    values.update({k:job[k] for k in optional if k in job})
    if (type(values['cycles']) is not int or values['cycles'] not in (1,2)
            or any(type(values[k]) is not int or not 60<=values[k]<=900 for k in ('timeout','cleanup_timeout'))
            or type(values['profile_valid_seconds']) is not int or not 60<=values['profile_valid_seconds']<=3600):
        raise ValueError('Invalid bounded native job cycles/timeouts')
    for key in ('spatial_request','output',*([k for k in ('native_census','launch_profile','recover_reservation') if k in values])):
        if not isinstance(values[key],str) or not Path(values[key]).is_absolute():raise ValueError('Absolute private native job paths required')
    for key in ('owner','operation','upf_target',*([k for k in ('supi','service_type','region','logical_session_id') if k in values])):
        if not isinstance(values[key],str) or not 1<=len(values[key])<=128:raise ValueError('Bounded exact native scope required')
    if values.get('replacement_upf_target') is not None and (not isinstance(values['replacement_upf_target'],str) or not 1<=len(values['replacement_upf_target'])<=128):
        raise ValueError('Invalid replacement UPF target')
    return values


def policy_clear():
    for env,check in (('CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY',policy_inventory_clear),
                      ('CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY',policy_quarantine_clear)):
        value=os.environ.get(env)
        if not value or not Path(value).is_absolute() or not check(Path(value)):
            raise EvidenceUnavailable('Existing policy ownership fences must be clear')
    return True


class OwnedScope:
    """One controller OS lock plus durable generation/SUPI and native UID fences."""
    def __init__(self,driver,store):
        self.driver,self.store=driver,store
        self.units={};self.uids={};self.generations=set();self.baseline=None
    def baseline_zero(self):
        current=self.store.current()
        if current and (current['allocation'] or any(current['desired'].values())):
            raise EvidenceUnavailable('A dedicated stopped-zero state DB is required')
        self.baseline=current['generation'] if current else None
        self.guard()
    def own(self,generation):
        if len(generation['allocation'])!=1 or 'device' not in generation['allocation'][0]:
            raise EvidenceUnavailable('Exact single-device held allocation required')
        self.generations.add(generation['generation'])
        for i,unit in enumerate(shards(generation['allocation'],self.driver.batch_size)):
            name=f"cv-ue-g{generation['generation']}-{unit.get('podIndex',i)}"
            self.units[name]=(str(generation['generation']),tuple(unit['supis']))
    def zero(self,current):
        if current['allocation'] or current['desired']!=ZERO:raise EvidenceUnavailable('Exact native zero generation required')
        self.generations.add(current['generation'])
    def guard(self,_mutation=None):
        policy_clear()
        current=self.store.current()
        if current and current['generation'] not in self.generations and current['generation']!=self.baseline:
            # A zero request advances its own journal before its first cleanup
            # write. Only a zero successor of the currently held OS-lock owner
            # is accepted; no foreign allocation can cross this fence.
            if current['desired']==ZERO and not current['allocation'] and self.generations:
                self.generations.add(current['generation'])
            else:raise EvidenceUnavailable('Owned native generation changed')
        for pod in self.driver.read_pods().get('items',()):
            meta=pod.get('metadata',{});labels=meta.get('labels',{})
            if labels.get('cityverse.io/managed-by')!='onprem-ue-controller':continue
            name,uid=meta.get('name'),meta.get('uid')
            expected=self.units.get(name)
            if (expected is None or expected[0]!=labels.get('cityverse.io/generation') or not uid
                    or tuple(json.loads(meta.get('annotations',{}).get('cityverse.io/supis','[]')))!=expected[1]
                    or name in self.uids and self.uids[name]!=uid):
                raise EvidenceUnavailable('Foreign native UE scope or changed Pod UID')
            self.uids[name]=uid
        return None


def assert_native_zero(proof,ran,*,now):
    if proof.get('ues')!=[]:raise EvidenceUnavailable('Native UE contexts remain')
    for name in ('amf','smf','upf'):
        source=proof.get('sources',{}).get(name,{})
        if source.get('available') is not True or not -1<=now-seconds(source.get('observedAt'))<=15:
            raise EvidenceUnavailable('Fresh complete native zero sources required')
    if proof['sources']['smf'].get('inventoryComplete') is not True:
        raise EvidenceUnavailable('Native SMF zero inventory incomplete')
    if ran.get('source')!='free-ran-ue-gnb-api-ue-count':raise EvidenceUnavailable('Native FreeRAN gNB zero contexts required')
    readouts=ran.get('readouts',ran.get('slices',{}))
    if set(readouts)!=set(ZERO):raise EvidenceUnavailable('All native slice gNB readouts required')
    for row in readouts.values():
        if row.get('ueCount')!=0 or row.get('supis',[]) or not -1<=now-seconds(row.get('observedAt'))<=15:
            raise EvidenceUnavailable('Native gNB contexts remain or stale')


async def wait_established(controller,scope,timeout):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        scope.guard()
        value=await controller.reconcile()
        if (value.get('status')=='established' and value.get('errors')==[] and value.get('source')=='native-free-ran-ue-single-session/v2'
                and value.get('schemaVersion')==V2 and len(session_rows(value))==1):return value
        # A full native capture already performs bounded I/O. Avoid adding a
        # two-second gap that can break the unchanged 15-second continuity
        # requirement when the seven-gNB cluster is busy.
        await asyncio.sleep(.1)
    raise EvidenceUnavailable('Native V2 attachment timeout')


async def reserve_after_native_ready(controller,scope,counts,**request):
    """Retry transient pre-reservation reads, never an uncertain ledger write."""
    repository=HeldUERepository(controller.store)
    def ownership():
        current=controller.store.current();held=repository.current()
        if current and (current['allocation'] or any(current['desired'].values())):
            raise EvidenceUnavailable('Native reservation requires the same empty generation')
        identity=None if current is None else {k:current.get(k) for k in ('generation','created','desired','allocation','populationRevision')}
        return json.dumps(dict(generation=identity,held=held.to_dict() if held else None),sort_keys=True,separators=(',',':'),allow_nan=False)
    before=ownership()
    for attempt in range(5):
        scope.guard()
        if time.time()>=request['valid_until']:raise EvidenceUnavailable('Native reservation authority expired during readiness checks')
        try:
            return await controller.reserve(counts,**request)
        except EvidenceUnavailable:
            # A local reservation commit or another owner/generation is an
            # unknown result, even if the caller received a read failure.
            # Preserve it for explicit recovery; never mint a new nonce here.
            if ownership()!=before or attempt==4:raise
            await asyncio.sleep(.5)


async def stop_owned(controller,scope,handles,coordinator,timeout,*,existing_zero_generation=None):
    # Close only this harness's pending registrations. A cancel ACK is never
    # release evidence; AMF/SMF/kernel/gNB absence is independently read below.
    for handle in handles:
        state=await asyncio.to_thread(coordinator.api.inspect,handle.admission.identity.nonce)
        if state is None:raise EvidenceUnavailable('Owned native admission outcome unknown')
        await asyncio.to_thread(coordinator.release,handle,state.version)
    scope.guard()
    if existing_zero_generation is None:
        await controller.request(ZERO)
    elif controller.store.current()['generation']!=existing_zero_generation:
        raise EvidenceUnavailable('Explicit retirement generation changed')
    scope.zero(controller.store.current())
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        scope.guard()
        status=await controller.reconcile()
        if status.get('status')=='stopped' and status.get('errors')==[]:
            if any(p.get('metadata',{}).get('labels',{}).get('cityverse.io/managed-by')=='onprem-ue-controller' for p in controller.driver.read_pods().get('items',())):
                await asyncio.sleep(2);continue
            proof=await asyncio.to_thread(controller.adapter.observe_release,status['generation'])
            ran=await asyncio.to_thread(controller.driver.observe_gnb_contexts)
            assert_native_zero(proof,ran,now=time.time())
            census=await asyncio.to_thread(coordinator.api.active_inventory) if hasattr(coordinator.api,'active_inventory') else None
            return dict(status=status,proof=proof,gnb=ran,census=census)
        await asyncio.sleep(2)
    raise EvidenceUnavailable('Owned native stop/absence timeout; retain state for recovery')


def validate_receiver_windows(windows,row):
    if len(windows)!=2 or {w.get('direction') for w in windows}!={'ul','dl'}:
        raise EvidenceUnavailable('Exact UL and DL native receiver windows required')
    for window in windows:
        binding=window.get('binding',{})
        if (window.get('source')!='native-iperf3-receiver' or window.get('receivedBytes',0)<=0
                or window.get('receivedMbps',0)<=0 or window.get('startedAt',0)>=window.get('finishedAt',0)
                or any(binding.get(k)!=row[k] for k in ('supi','sessionId','podUid','smContextRef','upfEpoch'))):
            raise EvidenceUnavailable('Native receiver result differs from exact attached session')


def configured_workload_compiler(config):
    """Use the same pinned catalog and budget contract as formal integration."""
    from backend.onprem.traffic.session_workload import SessionWorkloadCompiler
    workload=config.get('session_workload',{})
    if not isinstance(workload,dict):
        raise EvidenceUnavailable('Invalid server session workload configuration')
    catalog=config.get('service_catalog',config.get('ue_classification',{}).get('service_mapping',{}))
    return SessionWorkloadCompiler(catalog,
        functional_combined_budget_kbps=workload.get('functional_combined_budget_kbps'))


async def receiver_windows(controller,scope,config,snapshot,operation,*,timeout=120):
    from backend.onprem.traffic.capability import IperfCapabilityTraffic
    from backend.onprem.runtime.round_resources import NativeRoundResources
    compiler=configured_workload_compiler(config)
    kubeconfig=Path(os.environ['KUBECONFIG'])
    traffic=IperfCapabilityTraffic('kubectl',kubeconfig,controller.driver.context,controller.driver.namespace)
    raw=dict(deepcopy(snapshot),roundId=operation,sequence=1,stage='measuring_result',trafficMode='functional')
    resources=NativeRoundResources(controller,traffic,policy_quiet=policy_clear,session_workload_compiler=compiler)
    resources.round_reader=lambda:raw
    windows=[]
    try:
        jobs=await resources.traffic_jobs(raw)
        if len(jobs)!=2:raise EvidenceUnavailable('Exactly one native UE UL/DL workload required')
        for job in jobs:
            scope.guard()
            await wait_established(controller,scope,timeout)
            windows.append(await resources.sample(job))
        validate_receiver_windows(windows,session_rows(snapshot)[0])
        return windows
    finally:
        traffic.close()


def write_artifact(root,name,value):
    target=root/(name+'.json')
    with target.open('x',encoding='utf-8') as out:json.dump(value,out,indent=2,allow_nan=False)


def progress(args,phase,**facts):
    args.phase=phase
    with (args.output/'progress.jsonl').open('a',encoding='utf-8') as out:
        out.write(json.dumps(dict(phase=phase,observed_at=time.time(),**facts),allow_nan=False)+'\n')


async def wait_baseline(controller,scope,timeout):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        scope.guard()
        value=await controller.reconcile()
        if value.get('status')=='stopped' and value.get('errors')==[] and value.get('perUe')==[]:
            return value
        await asyncio.sleep(2)
    raise EvidenceUnavailable('Stable native zero baseline deadline exceeded')


def validate_consumed_recovery(store,original,installation,owner,*,retirement_generation=None):
    persisted=HeldUERepository(store).current();current=store.current()
    if retirement_generation is not None:
        if (persisted is None or current is None or type(retirement_generation) is not int
                or retirement_generation!=persisted.consumed_generation+1 or current['generation']!=retirement_generation
                or current['desired']!=ZERO or current['allocation']):
            raise EvidenceUnavailable('Exact zero successor retirement generation required')
        with store.connect() as db:
            historical=db.execute('SELECT * FROM generations WHERE id=?',(persisted.consumed_generation,)).fetchone()
            if historical is None:raise EvidenceUnavailable('Consumed historical generation missing')
            current=store.decode(historical)
    canonical=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
    immutable=lambda v:{k:val for k,val in v.to_dict().items() if k not in {'state','version','consumed_generation'}}
    if (original.state!='held' or original.consumed_generation is not None or persisted is None
            or persisted.state!='consumed' or persisted.owner_id!=owner or canonical(immutable(persisted))!=canonical(immutable(original))
            or current is None or current['generation']!=persisted.consumed_generation
            or current['desired']!=dict(persisted.counts) or len(current['allocation'])!=1 or len(persisted.devices)!=1
            or len(persisted.allocation)!=1):
        raise EvidenceUnavailable('Consumed recovery reservation, owner or generation differs')
    row=current['allocation'][0];device=persisted.devices[0];member=persisted.allocation[0]
    expected_identity=AdmissionIdentity(persisted.network_scope,member.supi,member.pdu_session_id,member.nonce)
    if (row.get('supi')!=device.supi or canonical(row.get('device'))!=canonical(device.to_dict())
            or canonical(row.get('reservedSessions'))!=canonical([asdict(member)])
            or installation.admission.identity!=expected_identity or installation.admission.owner_id!=owner
            or installation.operation_id!=persisted.operation_id or persisted.launch_profile_id!=original.launch_profile_id):
        raise EvidenceUnavailable('Consumed recovery native identity or device allocation differs')
    return current


async def reconcile_failed_native_zero(args,controller,scope,coordinator,original,installation,current):
    """Explicit operator retirement after native timeout has already removed UE.

    Preserve the failed generation. No admission, Pod or network mutation is
    permitted here; two independent native absence captures precede a CAS audit.
    """
    import hashlib
    import sqlite3
    from contextlib import closing
    from backend.onprem.runtime.ue import blank_status
    from backend.onprem.intents.resource_adapters import FixedOriginSessionQerApi,_token_file_reader
    from backend.onprem.intents.integration import load_configuration
    restart_pending=(current['result'].get('status')=='verification_unavailable' and
        current['result'].get('errors')==['Runtime reconciliation pending after backend restart'])
    if not restart_pending and (current['result'].get('status') not in {'failed','degraded'} or not current['result'].get('errors')):
        raise EvidenceUnavailable('Failed native intent required for operator reconciliation')
    if any(k in current['result'] for k in ('recoveryMode','exceptionalRecovery','hostStateLoss')):
        raise EvidenceUnavailable('Exceptional recovery cannot use smoke native-zero reconciliation')
    if current.get('populationRevision',0)!=0:raise EvidenceUnavailable('Population runtime cannot use smoke operator reconciliation')
    controller.store.assert_no_active_exceptional()
    config=load_configuration();qos=config['providers']['qos']
    census_api=FixedOriginSessionQerApi(qos['origin'],token_reader=_token_file_reader(qos['token_file']))
    canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    captures=[]
    for _ in range(2):
        scope.guard()
        if any(p.get('metadata',{}).get('labels',{}).get('cityverse.io/managed-by')=='onprem-ue-controller'
               for p in driver_pods(controller)):
            raise EvidenceUnavailable('Operator reconciliation cannot retire an existing UE Pod')
        proof=await asyncio.to_thread(controller.adapter.observe_release,current['generation'])
        if proof.get('generation')!=current['generation']:raise EvidenceUnavailable('Native release generation differs')
        ran=await asyncio.to_thread(controller.driver.observe_gnb_contexts)
        assert_native_zero(proof,ran,now=time.time())
        census=await asyncio.to_thread(census_api.active_inventory)
        if (census.get('source')!='native-smf-active-sessions' or census.get('complete') is not True
                or census.get('network_scope')!=original.network_scope or census.get('sessions')!=[]
                or not census.get('smf_boot_epoch') or not -1<=time.time()-census.get('observed_at',0)<=15):
            raise EvidenceUnavailable('Fresh complete native zero SMF census required')
        state=await asyncio.to_thread(coordinator.api.inspect,installation.admission.identity.nonce)
        coordinator._verify_state(installation.admission,state)
        if (state.status!='cancelled' or state.operation_id!=installation.operation_id
                or state.boot_epoch!=installation.native_boot_epoch or census['smf_boot_epoch']!=state.boot_epoch
                or state.installation_version!=installation.native_version):
            raise EvidenceUnavailable('Exact owned admission must already be cancelled in the same SMF boot')
        captures.append(dict(proof=proof,gnb=ran,census=census,admission=asdict(state)))
    if captures[0]['census']['smf_boot_epoch']!=captures[1]['census']['smf_boot_epoch']:
        raise EvidenceUnavailable('Native SMF boot changed during operator reconciliation')
    validate_consumed_recovery(controller.store,original,installation,args.owner)
    scope.guard()
    if any(p.get('metadata',{}).get('labels',{}).get('cityverse.io/managed-by')=='onprem-ue-controller' for p in driver_pods(controller)):
        raise EvidenceUnavailable('Native UE appeared before operator reconciliation')
    write_artifact(args.output,'operator-native-zero-captures',captures)
    backup=args.output/'operator-state-backup.sqlite3'
    with closing(sqlite3.connect(controller.store.path)) as source,closing(sqlite3.connect(backup)) as target:source.backup(target)
    os.chmod(backup,0o600)
    with controller.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        controller.store._assert_recovery_clear(db,require_fresh=True)
        from backend.onprem.runtime.population import PopulationRepository
        HeldUERepository.assert_no_held(db)
        PopulationRepository.assert_no_pending(db,allow_stop=True)
        assert_native_zero(captures[-1]['proof'],captures[-1]['gnb'],now=time.time())
        if not -1<=time.time()-captures[-1]['census']['observed_at']<=15:raise EvidenceUnavailable('Native zero census expired before CAS')
        latest=controller.store.decode(db.execute('SELECT g.*,w.last_established FROM generations g LEFT JOIN ue_reconciliation_watchdogs w ON w.generation_id=g.id ORDER BY g.id DESC LIMIT 1').fetchone())
        latest['populationRevision']=0
        if canonical(latest)!=canonical(current):raise EvidenceUnavailable('Failed generation changed before operator CAS')
        cursor=db.execute('INSERT INTO generations(desired,allocation,created,status) VALUES (?,?,?,?)',(json.dumps(ZERO),'[]',time.time(),'{}'))
        generation=cursor.lastrowid
        db.execute('UPDATE generations SET status=? WHERE id=?',(json.dumps(blank_status(generation,ZERO)),generation))
        receipt=dict(schemaVersion='cityverse.free-ran-smoke-operator-zero/v1',previous_generation=current['generation'],zero_generation=generation,
            owner=args.owner,reservation_id=original.reservation_id,previous_intent_sha256=hashlib.sha256(canonical(current).encode()).hexdigest(),
            native_proof_sha256=hashlib.sha256(canonical(captures).encode()).hexdigest(),committed_at=time.time(),native_writes=False)
        db.execute('CREATE TABLE IF NOT EXISTS ue_smoke_operator_reconciliations (zero_generation INTEGER PRIMARY KEY,receipt TEXT NOT NULL)')
        db.execute('INSERT INTO ue_smoke_operator_reconciliations VALUES (?,?)',(generation,json.dumps(receipt,sort_keys=True)))
    write_artifact(args.output,'operator-reconciliation',receipt)
    scope.zero(controller.store.current())
    controller.verifier.reset()
    await wait_baseline(controller,scope,args.cleanup_timeout)
    return dict(status='failed_owned_intent_reconciled_to_native_zero',native_zero=True,native_execution=True,
        recovery_only=True,new_ue_created=False,new_nonce_registered=False,previous_generation=current['generation'],zero_generation=generation)


def driver_pods(controller):
    return controller.driver.read_pods().get('items',())


async def recover_consumed(args,controller,scope,coordinator,observer,profile):
    original=HeldUEAllocation.from_dict(sealed_json(args.recover_consumed_reservation,args.recover_consumed_reservation_sha256))
    installation=AdmissionInstallation.from_dict(sealed_json(args.recover_installation,args.recover_installation_sha256))
    if original.launch_profile_id!=profile.profile_id:raise EvidenceUnavailable('Consumed recovery launch profile differs')
    controller.acquire()
    try:
        retirement=getattr(args,'recover_retirement_generation',None)
        current=validate_consumed_recovery(controller.store,original,installation,args.owner,retirement_generation=retirement)
        scope.own(current);driver=controller.driver;driver.bind_final_mutation_guard(scope.guard)
        if getattr(args,'recover_failed_native_zero',False):
            return await reconcile_failed_native_zero(args,controller,scope,coordinator,original,installation,current)
        if retirement is not None:
            previous=[r for r in session_rows(current['result']) if r.get('supi')==original.devices[0].supi]
            if (current['result'].get('status')!='established' or current['result'].get('errors')!=[]
                    or len(previous)!=1 or previous[0].get('podUid')!=args.recover_retirement_pod_uid):
                raise EvidenceUnavailable('Retirement original stable Pod UID differs from sealed recovery pin')
            scope.zero(controller.store.current())
            scope.uids[next(iter(scope.units))]=args.recover_retirement_pod_uid
            scope.guard()
            progress(args,'recovery_resume_existing_zero')
            raw=await asyncio.to_thread(controller.adapter.observe_release,retirement)
            write_artifact(args.output,'recovery-retirement-readback',raw)
            validate_consumed_recovery(controller.store,original,installation,args.owner,retirement_generation=retirement)
            cleanup=await stop_owned(controller,scope,[installation],coordinator,args.cleanup_timeout,existing_zero_generation=retirement)
            write_artifact(args.output,'recovery-native-zero',cleanup)
            return dict(status='owned_retirement_resumed_and_stopped',native_execution=True,recovery_only=True,
                        native_zero=True,new_ue_created=False,new_nonce_registered=False,retirement_generation=retirement)
        progress(args,'recovery_native_readback')
        # Surface a concrete native read failure immediately. Do not wait four
        # minutes behind the public unavailable-observation envelope.
        raw=await asyncio.to_thread(observer._observe,current['generation'])
        write_artifact(args.output,'recovery-direct-native-readback',raw)
        progress(args,'recovery_existing_stability_barrier')
        attached=await wait_established(controller,scope,args.timeout)
        validate_consumed_recovery(controller.store,original,installation,args.owner)
        write_artifact(args.output,'recovery-established',dict(snapshot=attached,pods=driver.read_pods()))
        progress(args,'recovery_normal_stop')
        cleanup=await stop_owned(controller,scope,[installation],coordinator,args.cleanup_timeout)
        write_artifact(args.output,'recovery-native-zero',cleanup)
        return dict(status='owned_generation_reconciled_and_stopped',native_execution=True,recovery_only=True,
                    native_zero=True,new_ue_created=False,new_nonce_registered=False)
    except BaseException as error:
        private_failure(args.output,'primary-failure-traceback',error)
        raise
    finally:controller.release()


async def run(args):
    progress(args,'sealed_inputs')
    config=load_configuration()
    driver=configured_driver()
    if getattr(args,'native_census',None) is not None:
        from scripts.onprem.commission_free_ran_profile import build_profile
        document=sealed_json(args.native_census,args.native_census_sha256)
        profile=build_profile(config,driver,document,supi=args.supi,service_type=args.service_type,region=args.region,
            logical_session_id=args.logical_session_id,evidence_reference=str(args.native_census)+'#sha256='+args.native_census_sha256,
            valid_seconds=args.profile_valid_seconds)
        write_artifact(args.output,'derived-native-launch-profile',profile.to_dict())
    else:
        profile=UEAdmissionLaunchProfile.from_dict(sealed_json(args.launch_profile,args.launch_profile_sha256))
    spatial=sealed_json(args.spatial_request,args.spatial_sha256)
    from backend.onprem.scenarios.contracts import validate_round,slice_counts
    spatial=validate_round(spatial)
    counts=slice_counts(spatial)
    if sum(counts.values())!=1 or not spatial.get('demandGroups') or profile.source!='native-ue-device-launch-profile/v2':
        raise EvidenceUnavailable('One geographic V2 device and native commissioned PDU profile required')
    if any(len(rows)!=1 for _,rows in profile.device_sessions):raise EvidenceUnavailable('Single PDU per device required')
    if driver is None or driver.backend!='free-ran-ue' or getattr(driver,'device_profile',None) is None or driver.device_profile.driver!='free-ran-ue':
        raise EvidenceUnavailable('Commissioned FreeRAN device runtime required')
    if (profile.configuration_digest!=driver.reservation_configuration_digest or profile.image_digest!=driver.image.split('@')[-1]
            or (profile.valid_until<=time.time() and not getattr(args,'recovery_only',False))):raise EvidenceUnavailable('Native launch profile differs from current compiler or expired')
    if not args.execute_native:return dict(status='inputs_checked',native_execution=False,profile_id=profile.profile_id)
    policy_clear()
    state=Path(os.environ['CITYVERSE_ONPREM_UE_STATE_FILE'])
    store=GenerationStore(state);adapter=configured_native_collector(store,driver)
    controller=UEController(store,adapter,driver)
    scope=OwnedScope(driver,store)
    qos=config['providers']['qos']
    coordinator=PendingAdmissionCoordinator(FixedOriginSmfAdmissionApi(qos['origin'],token_reader=_token_file_reader(qos['token_file']),timeout=min(10,qos.get('timeout',5))))
    observer=adapter.device_observer
    if observer is None:raise EvidenceUnavailable('Native FreeRAN V2 observer unavailable')
    if getattr(args,'recovery_only',False):
        return await recover_consumed(args,controller,scope,coordinator,observer,profile)
    if getattr(args,'recover_reservation',None) is not None:
        from scripts.onprem.recover_free_ran_smoke_hold import recover
        from backend.onprem.intents.resource_adapters import FixedOriginSessionQerApi
        expected=sealed_json(args.recover_reservation,args.recover_reservation_sha256)
        progress(args,'explicit_held_recovery')
        recovery=recover(controller,coordinator.api,FixedOriginSessionQerApi(qos['origin'],token_reader=_token_file_reader(qos['token_file'])),
            expected,owner=args.owner,operation=expected['operation_id'],execute_release=True)
        write_artifact(args.output,'held-recovery',recovery)
    controller.acquire()
    outcomes=[];handles=[];held=None;consumed=False;pending_attempt=None;operation=None;cleanup_confirmed=False;primary_failure=None
    try:
        scope.baseline_zero();driver.bind_final_mutation_guard(scope.guard)
        progress(args,'native_zero_baseline')
        await wait_baseline(controller,scope,args.timeout)
        for cycle in range(args.cycles):
            target=args.upf_target if cycle==0 else (args.replacement_upf_target or args.upf_target)
            prefix=f'cycle-{cycle+1}'
            native_scope,bindings=await asyncio.to_thread(observer._registered_scope)
            if target not in bindings:raise EvidenceUnavailable('Requested native UPF is not commissioned')
            inventory=await asyncio.to_thread(observer._census,native_scope)
            if native_scope is not None:await asyncio.to_thread(native_scope.finish)
            operation=args.operation+':'+str(cycle+1)
            progress(args,'reserve',cycle=cycle+1,target=target)
            held=await reserve_after_native_ready(controller,scope,counts,owner_id=args.owner,operation_id=operation,request_id=spatial['requestId']+':'+str(cycle+1),
                network_scope=config['session_inventory']['network_scope'],valid_until=min(time.time()+args.timeout+120,profile.valid_until),profile=profile)
            handles=[];consumed=False;cleanup_confirmed=False
            # Persist each attempted nonce before native registration, and never
            # retry an unknown registration result with a new nonce.
            write_artifact(args.output,prefix+'-reserved',held.to_dict())
            for member in held.allocation:
                pending=PendingAdmission(AdmissionIdentity(held.network_scope,member.supi,member.pdu_session_id,member.nonce),
                    args.owner,bindings[target]['native_upf_id'],held.created_at,held.valid_until)
                pending_attempt=pending
                progress(args,'native_pending_registration',cycle=cycle+1)
                handle=await asyncio.to_thread(coordinator.install,pending,operation)
                handles.append(handle)
                pending_attempt=None
                write_artifact(args.output,prefix+'-admission-'+str(len(handles)),handle.to_dict())
            progress(args,'consume_and_attach',cycle=cycle+1)
            await controller.request(counts,reservation=held,spatial_request=spatial)
            consumed=True;scope.own(store.current())
            attached=await wait_established(controller,scope,args.timeout)
            row=session_rows(attached)[0]
            if row['upf']!=target:raise EvidenceUnavailable('Native session selected a different UPF')
            bound=await asyncio.to_thread(coordinator.read_bindings,handles)
            if not bound.complete or set(bound.session_keys)!={row['sessionKey']}:
                raise EvidenceUnavailable('New native session did not bind this exact reserved nonce')
            write_artifact(args.output,prefix+'-attached',dict(snapshot=attached,bindings=asdict(bound)))
            progress(args,'receiver_ul_dl',cycle=cycle+1)
            windows=await receiver_windows(controller,scope,config,attached,operation,timeout=args.timeout)
            write_artifact(args.output,prefix+'-receiver-windows',windows)
            progress(args,'normal_stop_and_native_zero',cycle=cycle+1)
            cleanup=await stop_owned(controller,scope,handles,coordinator,args.cleanup_timeout)
            cleanup_confirmed=True
            write_artifact(args.output,prefix+'-native-zero',cleanup)
            outcome=dict(generation=attached['generation'],session_key=row['sessionKey'],nonce=handles[0].admission.identity.nonce,
                supi=row['supi'],pod_uid=row['podUid'],upf=target,gnb=row['proof']['spatialRan'],
                demand_group_id=row['demandGroupId'],source_location=row['sourceLocation'],native_zero=True)
            if outcomes and (outcome['supi']!=outcomes[-1]['supi'] or outcome['nonce']==outcomes[-1]['nonce'] or outcome['session_key']==outcomes[-1]['session_key']
                    or outcome['pod_uid']==outcomes[-1]['pod_uid'] or outcome['gnb']['gnbPodUid']!=outcomes[-1]['gnb']['gnbPodUid']
                    or outcome['demand_group_id']!=outcomes[-1]['demand_group_id'] or outcome['source_location']!=outcomes[-1]['source_location']):
                raise EvidenceUnavailable('Interrupted reconnect lineage/geography differs or native identity reused')
            outcomes.append(outcome);handles=[];held=None;consumed=False
        return dict(status='native_v2_receiver_and_stop_verified',native_execution=True,cycles=outcomes,
            interrupted_reconnect_verified=len(outcomes)==2,formal_parent_rebuild_verified=False,service_sla_verified=False)
    except BaseException as error:
        primary_failure=error
        private_failure(args.output,'primary-failure-traceback',error)
        raise
    finally:
        try:
            if held is not None and not consumed and not cleanup_confirmed:
                persisted=HeldUERepository(store).current()
                current=store.current()
                if persisted is not None and persisted.reservation_id==held.reservation_id and persisted.state=='consumed':
                    if (current is None or current['generation']!=persisted.consumed_generation
                            or {r['supi'] for r in current['allocation']}!={d.supi for d in persisted.devices}):
                        raise EvidenceUnavailable('Consumed native reservation scope changed; retain state for recovery')
                    scope.own(current);consumed=True
            if consumed and not cleanup_confirmed:
                cleanup=await stop_owned(controller,scope,handles,coordinator,args.cleanup_timeout)
                write_artifact(args.output,'failure-native-zero',cleanup)
            elif held is not None and not cleanup_confirmed:
                if pending_attempt is not None:
                    # Recover the exact attempted nonce read-only. Unknown
                    # registration keeps the held reservation for recovery.
                    recovered=await asyncio.to_thread(coordinator.recover_for_release,pending_attempt,operation)
                    handles.append(recovered)
                for handle in handles:
                    actual=await asyncio.to_thread(coordinator.api.inspect,handle.admission.identity.nonce)
                    if actual is not None:await asyncio.to_thread(coordinator.release,handle,actual.version)
                HeldUERepository(store).release(held)
        except BaseException as error:
            private_failure(args.output,'cleanup-failure-traceback',error)
            if primary_failure is None:raise
        finally:controller.release()


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-file',type=Path);parser.add_argument('--job-sha256')
    for name in ('launch-profile','spatial-request'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--launch-profile-sha256');parser.add_argument('--spatial-sha256')
    parser.add_argument('--owner');parser.add_argument('--operation')
    parser.add_argument('--upf-target');parser.add_argument('--replacement-upf-target')
    parser.add_argument('--cycles',type=int,choices=(1,2),default=2)
    def bounded(value):
        parsed=int(value)
        if not 60<=parsed<=900:raise argparse.ArgumentTypeError('Timeout must be 60..900 seconds')
        return parsed
    parser.add_argument('--timeout',type=bounded,default=240)
    parser.add_argument('--cleanup-timeout',type=bounded,default=360)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--execute-native',action='store_true')
    args=parser.parse_args(argv)
    if args.job_file is not None:
        if not args.job_sha256:parser.error('Sealed job SHA-256 required')
        job=validate_job(args.job_file,args.job_sha256)
        for key,value in job.items():
            if key!='schemaVersion':setattr(args,key,value)
        for key in ('native_census','launch_profile','spatial_request','output','recover_reservation'):
            if getattr(args,key,None) is not None:setattr(args,key,Path(getattr(args,key)))
        args.profile_valid_seconds=job.get('profile_valid_seconds',1800)
        if (type(args.cycles) is not int or args.cycles not in (1,2) or any(type(v) is not int or not 60<=v<=900 for v in (args.timeout,args.cleanup_timeout))
                or type(args.profile_valid_seconds) is not int or not 60<=args.profile_valid_seconds<=3600):
            parser.error('Invalid bounded native job cycles/timeouts')
    elif any(getattr(args,key) is None for key in ('launch_profile','launch_profile_sha256','spatial_request','spatial_sha256','owner','operation','upf_target','output')):
        parser.error('Provide a sealed job or all direct profile/spatial/scope arguments')
    for key in ('owner','operation','upf_target'):
        if not isinstance(getattr(args,key),str) or not 1<=len(getattr(args,key))<=128:parser.error('Bounded exact owner/operation/target required')
    if not args.output.is_absolute() or args.output.exists():parser.error('Output must be a new absolute private artifact directory')
    return args


def main(argv=None):
    args=arguments(argv)
    args.output.mkdir(parents=True,mode=0o700)
    try:
        result=asyncio.run(run(args));code=0
    except Exception as exc:
        with (args.output/'failure-traceback.txt').open('x',encoding='utf-8') as out:out.write(traceback.format_exc())
        result=dict(status='blocked_or_recovery_required',phase=getattr(args,'phase','preflight'),reason_type=type(exc).__name__,
            private_traceback='failure-traceback.txt',native_success_claimed=False,retain_state_for_recovery=True);code=2
    write_artifact(args.output,'result',result)
    print(json.dumps({k:v for k,v in result.items() if k!='cycles'}))
    return code


if __name__=='__main__':raise SystemExit(main())
