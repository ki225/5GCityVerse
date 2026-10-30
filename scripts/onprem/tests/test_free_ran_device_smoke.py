"""No native writes: receipt derivation and exact private smoke boundaries."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.onprem import free_ran_device_smoke as smoke
from scripts.onprem.commission_free_ran_profile import build_profile
from backend.onprem.intents.domain import SessionKey
from test.intents.test_free_ran_device_observer import build


def test_profile_uses_observed_pdu_four_not_cli_default_or_allocation_order():
    key=SessionKey('native','imsi-208930100000001',4,'actual-smf-ref')
    row=dict(session_key=key.token,supi=key.supi,sessionId=4,networkScope='native',smContextRef=key.established_epoch,
        establishedEpoch=key.established_epoch,smf_boot_epoch='boot',upfEpoch='kernel',pfcpSeid='7',sst=1,sd='000001',dnn='internet')
    document=dict(native_census=dict(source='native-smf-active-sessions',complete=True,network_scope='native',
        smf_boot_epoch='boot',observed_at=100,sessions=[row]))
    driver=SimpleNamespace(backend='free-ran-ue',namespace='core',image='fru@sha256:'+'a'*64,reservation_configuration_digest='b'*64,
        device_profile=SimpleNamespace(driver='free-ran-ue',maximum_sessions=1,image_digest='sha256:'+'a'*64,
            gnb_search_endpoint='gnb-embb',commissioned_gnb_endpoints=('gnb-embb',)))
    from backend.onprem.runtime.free_ran_ue import CONFIG_INIT
    member={k:row[k] for k in ('supi','sessionId','smContextRef','dnn','sst','sd','upfEpoch','pfcpSeid')}
    member.update(podUid='actual-ue',registered=True,pduEstablished=True,trafficVerified=True)
    pod=dict(metadata=dict(name='cv-ue-g1-0',uid='actual-ue',namespace='core',labels={
        'cityverse.io/managed-by':'onprem-ue-controller','cityverse.io/generation':'1'},
        annotations={'cityverse.io/supis':json.dumps([key.supi])}),
        spec=dict(containers=[dict(name='ue',image=driver.image,command=['/bin/sh','-c'],
            args=['exec ./free-ran-ue ue -c /run/fru/ue.json -n 1 -p 1'])],initContainers=[dict(name='config',command=['python','-c',CONFIG_INIT],
                env=[dict(name=k,value=v) for k,v in dict(MSIN=key.supi[10:],SST='1',SD='000001',GNB_HOST='gnb-embb').items()])]),
        status=dict(phase='Running',containerStatuses=[dict(name='ue',imageID=driver.image,restartCount=0,ready=True,
            containerID='container-native',state={'running':{'startedAt':'native-start'}})]))
    capture=dict(status=dict(generation=1,failed=0,observedAt='1970-01-01T00:01:40+00:00',perUe=[member]),pods={'cv-ue-g1-0':pod})
    document['bootstrap_ue_evidence']={'before':deepcopy(capture),'after':deepcopy(capture)}
    def seal():document['bootstrap_ue_evidence_sha256']=hashlib.sha256(json.dumps(document['bootstrap_ue_evidence'],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    seal()
    options=dict(supi=key.supi,service_type='video',region='stadium',logical_session_id='video',evidence_reference='sealed-native-receipt',now=110)
    value=build_profile({'session_inventory':{'network_scope':'native'}},driver,document,**options)
    assert dict(value.session_ids)=={'embb':4} and value.device_session_ids==( ('embb','video',4), )
    document['bootstrap_ue_evidence']['before']['pods']['cv-ue-g1-0']['spec']['containers'][0]['image']='foreign@sha256:'+'b'*64
    seal()
    with pytest.raises(ValueError,match='image or exact single-process'):
        build_profile({'session_inventory':{'network_scope':'native'}},driver,document,**options)
    document['bootstrap_ue_evidence']['before']=deepcopy(capture);seal()
    row['session_key']=SessionKey('native',key.supi,1,key.established_epoch).token
    with pytest.raises(ValueError,match='Canonical native'):
        build_profile({'session_inventory':{'network_scope':'native'}},driver,document,**options)


def test_exact_owned_scope_rejects_replaced_uid_before_any_mutation(tmp_path,monkeypatch):
    observer,generation,*rest=build(tmp_path)
    pod=rest[-1]
    monkeypatch.setattr(smoke,'policy_clear',lambda:True)
    scope=smoke.OwnedScope(observer.driver,observer.store);scope.own(generation)
    assert scope.guard() is None
    pod['metadata']['uid']='different-owner-incarnation'
    with pytest.raises(smoke.EvidenceUnavailable,match='changed Pod UID'):scope.guard()


@pytest.mark.parametrize('native_state',['absent','exists','unknown'])
def test_recovery_releases_only_exact_held_after_native_absence(tmp_path,monkeypatch,native_state):
    from scripts.onprem import recover_free_ran_smoke_hold as recovery
    from backend.onprem.runtime.ue import UEController,GenerationStore,timestamp
    from backend.onprem.runtime.ue_reservation import HeldUERepository
    from test.intents.test_device_generation import profile,COUNTS
    store=GenerationStore(tmp_path/'recovery.db')
    held=HeldUERepository(store,clock=lambda:100).reserve(COUNTS,owner_id='owner',operation_id='operation',request_id='request',network_scope='network',valid_until=200,profile=profile(100))
    monkeypatch.setattr(recovery.time,'time',lambda:110)
    driver=SimpleNamespace(read_pods=lambda:dict(items=[]),observe_gnb_contexts=lambda:dict(source='free-ran-ue-gnb-api-ue-count',readouts={name:dict(ueCount=0,supis=[],observedAt=timestamp(110)) for name in smoke.ZERO}))
    controller=UEController(store,adapter=object(),driver=driver)
    def inspect(_):
        if native_state=='unknown':raise smoke.EvidenceUnavailable('HTTP409 is unknown')
        return None if native_state=='absent' else {'status':'registered'}
    api=SimpleNamespace(inspect=inspect)
    census=SimpleNamespace(active_inventory=lambda:dict(source='native-smf-active-sessions',complete=True,network_scope='network',sessions=[],observed_at=110,smf_boot_epoch='actual-boot'))
    expected=json.loads(json.dumps(held.to_dict()))
    if native_state=='absent':
        value=recovery.recover(controller,api,census,expected,owner='owner',operation='operation',execute_release=True)
        assert value['local_ledger_released'] is True
        assert HeldUERepository(store).current().state=='released'
    else:
        with pytest.raises(smoke.EvidenceUnavailable):recovery.recover(controller,api,census,expected,owner='owner',operation='operation',execute_release=True)
        assert HeldUERepository(store).current().state=='held'


def test_primary_and_cleanup_private_tracebacks_are_separate(tmp_path):
    primary=ValueError('primary native inspection failed')
    cleanup=RuntimeError('cleanup outcome remains unknown')
    smoke.private_failure(tmp_path,'primary-failure-traceback',primary)
    smoke.private_failure(tmp_path,'cleanup-failure-traceback',cleanup)
    assert 'primary native inspection failed' in (tmp_path/'primary-failure-traceback.txt').read_text()
    assert 'cleanup outcome remains unknown' in (tmp_path/'cleanup-failure-traceback.txt').read_text()


def consumed_recovery_fixture(tmp_path):
    from backend.onprem.runtime.ue_reservation import HeldUERepository,HeldUEAllocation,reservation_version
    from backend.onprem.intents.pending_admission import AdmissionInstallation,PendingAdmission,AdmissionIdentity
    observer,generation,*_=build(tmp_path)
    persisted=HeldUERepository(observer.store).current()
    data=persisted.to_dict()|dict(state='held',consumed_generation=None)
    data['version']=reservation_version(data)
    original=HeldUEAllocation.from_dict(data)
    member=original.allocation[0]
    pending=PendingAdmission(AdmissionIdentity(original.network_scope,member.supi,member.pdu_session_id,member.nonce),original.owner_id,'native-upf',original.created_at,original.valid_until)
    installation=AdmissionInstallation(pending,original.operation_id,'native-version',original.created_at,'native-boot')
    return observer,generation,original,installation


@pytest.mark.parametrize('fault',[None,'owner','nonce','generation'])
def test_consumed_recovery_requires_exact_immutable_held_and_generation(tmp_path,fault):
    from dataclasses import replace
    observer,generation,original,installation=consumed_recovery_fixture(tmp_path)
    owner=original.owner_id
    if fault=='owner':owner='foreign'
    if fault=='nonce':installation=replace(installation,admission=replace(installation.admission,identity=replace(installation.admission.identity,nonce='f'*32)))
    if fault=='generation':observer.store.current=lambda:dict(generation,generation=generation['generation']+1)
    if fault:
        with pytest.raises(smoke.EvidenceUnavailable):smoke.validate_consumed_recovery(observer.store,original,installation,owner)
    else:assert smoke.validate_consumed_recovery(observer.store,original,installation,owner)['generation']==generation['generation']


def test_consumed_cleanup_only_reads_stabilizes_and_stops_without_creating(tmp_path,monkeypatch):
    observer,generation,original,installation=consumed_recovery_fixture(tmp_path)
    reference=tmp_path/'reserved.json';reference.write_text(json.dumps(original.to_dict()))
    installed=tmp_path/'installed.json';installed.write_text(json.dumps(installation.to_dict()))
    args=SimpleNamespace(recover_consumed_reservation=reference,recover_consumed_reservation_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        recover_installation=installed,recover_installation_sha256=hashlib.sha256(installed.read_bytes()).hexdigest(),owner=original.owner_id,output=tmp_path,timeout=240,cleanup_timeout=360)
    calls=[]
    controller=SimpleNamespace(store=observer.store,driver=observer.driver,acquire=lambda:calls.append('lock'),release=lambda:calls.append('unlock'))
    direct=SimpleNamespace(_observe=lambda generation:calls.append(('native',generation)) or dict(source='native-proof'))
    async def stable(*a):calls.append('existing-stability');return dict(generation=generation['generation'])
    async def stop(controller,scope,handles,coordinator,timeout):
        assert handles==[installation];calls.append('normal-stop');return dict(native_zero=True)
    monkeypatch.setattr(smoke,'wait_established',stable);monkeypatch.setattr(smoke,'stop_owned',stop)
    result=asyncio.run(smoke.recover_consumed(args,controller,smoke.OwnedScope(observer.driver,observer.store),object(),direct,SimpleNamespace(profile_id=original.launch_profile_id)))
    assert result['new_ue_created'] is False and result['new_nonce_registered'] is False
    assert calls==['lock',('native',generation['generation']),'existing-stability','normal-stop','unlock']


def test_only_explicit_consumed_cleanup_job_can_load_expired_profile(tmp_path,monkeypatch):
    from test.intents.test_device_generation import profile
    _,_,original,installation=consumed_recovery_fixture(tmp_path)
    def seal(name,value):
        path=tmp_path/name;path.write_text(json.dumps(value));return str(path),hashlib.sha256(path.read_bytes()).hexdigest()
    p,psha=seal('expired.json',profile(100).to_dict())
    r,rsha=seal('original.json',original.to_dict());i,isha=seal('installation.json',installation.to_dict())
    monkeypatch.setattr(smoke.time,'time',lambda:10000)
    job=dict(schemaVersion='cityverse.free-ran-device-smoke/v1',execute_native=True,recovery_only=True,
        launch_profile=p,launch_profile_sha256=psha,recover_consumed_reservation=r,recover_consumed_reservation_sha256=rsha,
        recover_installation=i,recover_installation_sha256=isha,owner=original.owner_id,operation='cleanup',upf_target='embb',
        spatial_request=str(tmp_path/'spatial.json'),spatial_sha256='a'*64,output=str(tmp_path/'new-output'))
    path,sha=seal('job.json',job)
    assert smoke.validate_job(path,sha)['recovery_only'] is True
    job.pop('recovery_only');path,sha=seal('job.json',job)
    with pytest.raises(ValueError):smoke.validate_job(path,sha)


@pytest.mark.parametrize('foreign_uid',[False,True])
def test_retirement_resume_uses_history_uid_and_never_requests_another_zero(tmp_path,monkeypatch,foreign_uid):
    from backend.onprem.runtime.ue import EvidenceVerifier
    monkeypatch.setattr(smoke,'policy_clear',lambda:True)
    observer,generation,original,installation=consumed_recovery_fixture(tmp_path)
    uid=observer.driver.read_pods()['items'][0]['metadata']['uid']
    stable=EvidenceVerifier(stable_seconds=0).evaluate(generation,observer.observe(generation['generation']),now=110)
    assert stable['status']=='established'
    observer.store.save(stable)
    zero,_=observer.store.request(smoke.ZERO)
    def seal(name,value):
        path=tmp_path/name;path.write_text(json.dumps(value));return path,hashlib.sha256(path.read_bytes()).hexdigest()
    r,rsha=seal('original.json',original.to_dict());i,isha=seal('installation.json',installation.to_dict())
    args=SimpleNamespace(recover_consumed_reservation=r,recover_consumed_reservation_sha256=rsha,
        recover_installation=i,recover_installation_sha256=isha,owner=original.owner_id,output=tmp_path,
        timeout=240,cleanup_timeout=360,recover_retirement_generation=zero['generation'],
        recover_retirement_pod_uid=uid)
    if foreign_uid:
        pod=observer.driver.read_pods()['items'][0];pod['metadata']['uid']='foreign-incarnation'
        observer.driver.read_pods=lambda:dict(items=[pod])
    calls=[]
    controller=SimpleNamespace(store=observer.store,driver=observer.driver,adapter=SimpleNamespace(observe_release=lambda g:calls.append(('native-release-read',g)) or {}),
        acquire=lambda:None,release=lambda:None,request=lambda *_:pytest.fail('Must never redispatch ZERO'))
    async def stop(controller,scope,handles,coordinator,timeout,*,existing_zero_generation):
        assert existing_zero_generation==zero['generation'];calls.append('resume-normal-cleanup');return dict(native_zero=True)
    monkeypatch.setattr(smoke,'stop_owned',stop)
    monkeypatch.setattr(smoke,'wait_established',lambda *_:pytest.fail('Zero generation must not wait for UE establishment'))
    operation=smoke.recover_consumed(args,controller,smoke.OwnedScope(observer.driver,observer.store),object(),None,SimpleNamespace(profile_id=original.launch_profile_id))
    if foreign_uid:
        with pytest.raises(smoke.EvidenceUnavailable,match='changed Pod UID'):asyncio.run(operation)
        assert calls==[]
    else:
        result=asyncio.run(operation)
        assert result['retirement_generation']==zero['generation']
        assert calls==[('native-release-read',zero['generation']),'resume-normal-cleanup']


@pytest.mark.parametrize('fault',[None,'both','missing','expired'])
def test_sealed_job_can_reuse_only_unexpired_exact_launch_profile(tmp_path,monkeypatch,fault):
    from test.intents.test_device_generation import profile
    monkeypatch.setattr(smoke.time,'time',lambda:110)
    value=profile(100).to_dict()
    if fault=='expired':value['valid_until']=109
    source=tmp_path/'profile.json';source.write_text(json.dumps(value))
    job=dict(schemaVersion='cityverse.free-ran-device-smoke/v1',execute_native=True,
        launch_profile=str(source),launch_profile_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        spatial_request=str(tmp_path/'spatial.json'),spatial_sha256='a'*64,owner='owner',operation='operation',
        upf_target='embb',output=str(tmp_path/'output'))
    if fault=='both':job.update(native_census=str(tmp_path/'census'),native_census_sha256='b'*64)
    if fault=='missing':job.pop('launch_profile_sha256')
    sealed=tmp_path/'job.json';sealed.write_text(json.dumps(job));sha=hashlib.sha256(sealed.read_bytes()).hexdigest()
    if fault:
        with pytest.raises(ValueError):smoke.validate_job(sealed,sha)
    else:
        validated=smoke.validate_job(sealed,sha)
        assert validated['launch_profile']==str(source) and 'native_census' not in validated
        parsed=smoke.arguments(['--job-file',str(sealed),'--job-sha256',sha])
        assert parsed.launch_profile==source


def test_receiver_requires_both_directions_and_exact_native_session():
    row=dict(supi='imsi-1',sessionId=4,podUid='native-ue',smContextRef='new-context',upfEpoch='new-upf')
    windows=[dict(direction=d,source='native-iperf3-receiver',receivedBytes=1024,receivedMbps=1,startedAt=10,finishedAt=15,binding=deepcopy(row)) for d in ('ul','dl')]
    smoke.validate_receiver_windows(windows,row)
    windows[1]['binding']['smContextRef']='old-context'
    with pytest.raises(smoke.EvidenceUnavailable,match='exact attached session'):smoke.validate_receiver_windows(windows,row)


def test_sealed_job_has_no_arbitrary_fields_or_implicit_execution(tmp_path):
    job=dict(schemaVersion='cityverse.free-ran-device-smoke/v1',execute_native=True,native_census=str(tmp_path/'census.json'),
        native_census_sha256='a'*64,supi='imsi-1',service_type='video',region='stadium',logical_session_id='video',
        spatial_request=str(tmp_path/'spatial.json'),spatial_sha256='b'*64,owner='owner',operation='native-acceptance',upf_target='embb',output=str(tmp_path/'out'))
    path=tmp_path/'job.json'
    def write():
        raw=json.dumps(job).encode();path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    digest=write();assert smoke.validate_job(path,digest)['cycles']==2
    job['execute_native']=False
    with pytest.raises(ValueError,match='execution intent'):smoke.validate_job(path,write())
    job['execute_native']=True;job['shell_command']='unapproved'
    with pytest.raises(ValueError,match='schema'):smoke.validate_job(path,write())


def test_native_zero_baseline_waits_for_stability_without_changing_verifier(monkeypatch):
    values=[dict(status='verifying_traffic',errors=[]),dict(status='stopped',errors=[],perUe=[])]
    class Controller:
        async def reconcile(self):return values.pop(0)
    async def no_sleep(_):pass
    monkeypatch.setattr(smoke.asyncio,'sleep',no_sleep)
    guards=[]
    result=asyncio.run(smoke.wait_baseline(Controller(),SimpleNamespace(guard=lambda:guards.append(True)),60))
    assert result['status']=='stopped' and len(guards)==2


@pytest.mark.parametrize('fault',[None,'active_pod','busy_census','boot_change','cas_change'])
def test_operator_zero_preserves_failed_history_and_requires_native_absence(tmp_path,monkeypatch,fault):
    from backend.onprem.intents.pending_admission import NativeAdmissionState
    observer,generation,original,installation=consumed_recovery_fixture(tmp_path)
    observer.store.save(dict(generation=generation['generation'],status='failed',errors=['native timeout']))
    current=observer.store.current();out=tmp_path/'operator';out.mkdir()
    monkeypatch.setattr(smoke.time,'time',lambda:110)
    monkeypatch.setattr('backend.onprem.intents.integration.load_configuration',lambda:dict(providers={'qos':dict(origin='http://127.0.0.1:1',token_file='unused')}))
    monkeypatch.setattr('backend.onprem.intents.resource_adapters._token_file_reader',lambda _:None)
    calls=[]
    def census(_):
        calls.append('census')
        if fault=='cas_change' and len(calls)==2:observer.store.save(dict(generation=generation['generation'],status='failed',errors=['changed']))
        return dict(source='native-smf-active-sessions',complete=True,network_scope=original.network_scope,
            observed_at=110,smf_boot_epoch='changed' if fault=='boot_change' and len(calls)==2 else 'native-boot',sessions=[{}] if fault=='busy_census' else [])
    monkeypatch.setattr('backend.onprem.intents.resource_adapters.FixedOriginSessionQerApi.active_inventory',census)
    state=NativeAdmissionState('cancelled','cancelled-version',original.owner_id,original.operation_id,installation.admission.identity,
        'native-upf',original.created_at,original.valid_until,original.created_at,'native-boot',110,'native-smf-session-admission/v1',installation_version='native-version')
    coordinator=SimpleNamespace(api=SimpleNamespace(inspect=lambda _:state),_verify_state=lambda *a:None)
    driver=SimpleNamespace(read_pods=lambda:dict(items=[dict(metadata=dict(labels={'cityverse.io/managed-by':'onprem-ue-controller'}))] if fault=='active_pod' else []),
        observe_gnb_contexts=lambda:dict(zero=True))
    controller=SimpleNamespace(store=observer.store,driver=driver,adapter=SimpleNamespace(observe_release=lambda _:dict(generation=generation['generation'],zero=True)),verifier=SimpleNamespace(reset=lambda:None))
    monkeypatch.setattr(smoke,'assert_native_zero',lambda proof,ran,**_:None if proof.get('zero') and ran.get('zero') else pytest.fail('Native zero required'))
    async def baseline(*_):return dict(status='stopped')
    monkeypatch.setattr(smoke,'wait_baseline',baseline)
    args=SimpleNamespace(output=out,owner=original.owner_id,cleanup_timeout=60)
    operation=smoke.reconcile_failed_native_zero(args,controller,SimpleNamespace(guard=lambda:None,zero=lambda _:None),coordinator,original,installation,current)
    if fault:
        with pytest.raises(smoke.EvidenceUnavailable):asyncio.run(operation)
        assert observer.store.current()['generation']==generation['generation']
        assert not (out/'operator-reconciliation.json').exists()
    else:
        result=asyncio.run(operation)
        assert result['native_zero'] and result['new_ue_created'] is False
        assert len(calls)==2 and observer.store.current()['desired']==smoke.ZERO
        with observer.store.connect() as db:
            old=json.loads(db.execute('SELECT status FROM generations WHERE id=?',(generation['generation'],)).fetchone()[0])
            assert old==current['result']
            assert db.execute('SELECT count(*) FROM ue_smoke_operator_reconciliations').fetchone()[0]==1


@pytest.mark.parametrize('fault',[None,'generation_changed','reservation_committed','persistent'])
def test_reservation_read_retry_stops_on_any_ownership_change(monkeypatch,fault):
    current=dict(generation=2,created=1,desired=smoke.ZERO.copy(),allocation=[],populationRevision=0)
    held=[None];calls=[];expected=object()
    monkeypatch.setattr(smoke,'HeldUERepository',lambda _:SimpleNamespace(current=lambda:held[0]))
    async def no_sleep(_):pass
    monkeypatch.setattr(smoke.asyncio,'sleep',no_sleep)
    async def reserve(counts,**request):
        calls.append(deepcopy(request))
        if len(calls)==1 or fault=='persistent':
            if fault=='generation_changed':current['generation']=3
            if fault=='reservation_committed':held[0]=SimpleNamespace(to_dict=lambda:dict(reservation_id='new-commit'))
            raise smoke.EvidenceUnavailable('Transient native read')
        return expected
    controller=SimpleNamespace(store=SimpleNamespace(current=lambda:deepcopy(current)),reserve=reserve)
    operation=smoke.reserve_after_native_ready(controller,SimpleNamespace(guard=lambda:None),smoke.ZERO,
        valid_until=smoke.time.time()+60,owner_id='owner',operation_id='same-operation')
    if fault:
        with pytest.raises(smoke.EvidenceUnavailable):asyncio.run(operation)
        assert len(calls)==(5 if fault=='persistent' else 1)
    else:
        assert asyncio.run(operation) is expected
        assert len(calls)==2 and calls[0]==calls[1]
