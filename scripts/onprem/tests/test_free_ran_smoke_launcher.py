"""Native worker launch contract, with no real subprocess or UE writes."""
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_native_backend_launcher import launcher, setup


def test_smoke_dependency_check_imports_only_its_worker_not_api(monkeypatch):
    calls=[]
    monkeypatch.setattr(launcher.subprocess,'check_output',lambda command,**kwargs:calls.append((command,kwargs)))
    env={'CITYVERSE_INTENT_CONFIG_FILE':'sealed-config'}
    launcher.verify_wsl_python('/venv/python',env,free_ran_smoke=True)
    assert calls[0][0]==['/venv/python','-c','import scripts.onprem.free_ran_device_smoke']
    assert calls[0][1]['env'] is env
    assert calls[0][1]['timeout']==60
    launcher.verify_wsl_python('/venv/python',env)
    assert calls[1][0][-1]=='import backend.onprem.api.server'


def sealed(tmp_path, **changes):
    path = tmp_path / 'sealed job.json'
    path.write_text(json.dumps({'schemaVersion':'cityverse.free-ran-device-smoke/v1','execute_native':True,
        'native_census':str((tmp_path/'census.json').resolve()),'native_census_sha256':'0'*64,
        'supi':'imsi-208930100000001','service_type':'video','region':'ne','logical_session_id':'video',
        'spatial_request':str((tmp_path/'spatial.json').resolve()),'spatial_sha256':'1'*64,
        'owner':'test','operation':'test-op','upf_target':'upf-embb','output':str((tmp_path/'output').resolve())}))
    values = dict(free_ran_device_smoke=path,
        free_ran_device_smoke_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        capacity_gates='', capacity_cleanup_from=None, capacity_observe_output=None,
        capacity_prior_100=None, enable_mutations=True, enable_traffic=True, enable_smf_inventory=True)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('damage', ['hash','missing_hash','missing_job','capacity','traffic','mutations','inventory','json'])
def test_invalid_smoke_job_and_missing_native_gates_fail_closed(tmp_path, damage):
    args = sealed(tmp_path)
    if damage=='hash':args.free_ran_device_smoke_sha256='0'*64
    if damage=='missing_hash':args.free_ran_device_smoke_sha256=None
    if damage=='missing_job':args.free_ran_device_smoke=None
    if damage=='capacity':args.capacity_observe_output=tmp_path/'native.json'
    if damage=='traffic':args.enable_traffic=False
    if damage=='mutations':args.enable_mutations=False
    if damage=='inventory':args.enable_smf_inventory=False
    if damage=='json':
        args.free_ran_device_smoke.write_text('[]')
        args.free_ran_device_smoke_sha256=hashlib.sha256(b'[]').hexdigest()
    with pytest.raises(SystemExit):launcher.verified_free_ran_smoke_command(args,'python')


def test_sealed_data_cannot_select_an_executable(tmp_path):
    args=sealed(tmp_path)
    command=launcher.verified_free_ran_smoke_command(args,'fixed-python')
    assert command==['fixed-python',str(launcher.ROOT/'scripts/onprem/free_ran_device_smoke.py'),
        '--job-file',str(args.free_ran_device_smoke.resolve()),'--job-sha256',args.free_ran_device_smoke_sha256]
    assert 'arbitrary-executable' not in command
    data=json.loads(args.free_ran_device_smoke.read_text())
    data['command']=['arbitrary-executable','--bad']
    args.free_ran_device_smoke.write_text(json.dumps(data))
    args.free_ran_device_smoke_sha256=hashlib.sha256(args.free_ran_device_smoke.read_bytes()).hexdigest()
    with pytest.raises(SystemExit):launcher.verified_free_ran_smoke_command(args,'fixed-python')


@pytest.mark.parametrize('check_only', [False,True])
def test_only_fixed_worker_gets_verified_environment_and_no_backend(monkeypatch,tmp_path,check_only):
    args,_,services=setup(monkeypatch,tmp_path)
    job=sealed(tmp_path)
    def get(command,**kwargs):
        if 'get-contexts' in command:return 'cityverse-kubeadm-lab\n'
        if 'namespace' in command:return json.dumps({'metadata':{'name':'free5gc-onprem'}})
        if 'deployment' in command:return json.dumps({'spec':{'template':{'spec':{'containers':[{'image':launcher.SMF_INVENTORY_IMAGE}]}}}})
        if any(str(p).endswith('pfcp_preflight.py') for p in command):return json.dumps({'associations':{n:{'uid':n} for n in ('upf-embb','upf-urllc','upf-mmtc','upf-v2x')}})
        if 'secret' in command:return json.dumps({'data':{'token':base64.b64encode(b'a'*64).decode()}})
        return json.dumps(services)
    monkeypatch.setattr(launcher.subprocess,'check_output',get)
    class Process:
        def __init__(self,worker):self.returncode=0 if worker else None
        def poll(self):return self.returncode
        def terminate(self):self.returncode=0
        def wait(self,timeout=None):return self.returncode
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
    launched=[]
    def popen(command,**kwargs):
        launched.append((command,kwargs['env'].copy()))
        return Process(any(str(p).endswith('free_ran_device_smoke.py') for p in command))
    monkeypatch.setattr(launcher.subprocess,'Popen',popen)
    monkeypatch.setattr(launcher.socket,'create_connection',lambda *a,**k:Connection())
    argv=(args if check_only else [a for a in args if a!='--check-only'])+[
        '--enable-mutations','--enable-traffic','--enable-smf-inventory',
        '--free-ran-device-smoke',str(job.free_ran_device_smoke),
        '--free-ran-device-smoke-sha256',job.free_ran_device_smoke_sha256]
    launcher.main(argv)
    if check_only:
        assert launched==[]
        return
    assert len(launched)==9  # Existing eight verified forwards plus one worker.
    command,env=launched[-1]
    assert command[-4:]==['--job-file',str(job.free_ran_device_smoke.resolve()),'--job-sha256',job.free_ran_device_smoke_sha256]
    assert not any('uvicorn' in str(cmd) for cmd,_ in launched)
    assert env['CITYVERSE_ONPREM_RAN_BACKEND']=='free-ran-ue'
    assert env['CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED']=='true'
    assert env['CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN']=='a'*64
    assert env['CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED']=='true'
    assert env['CITYVERSE_ONPREM_TRAFFIC_ENABLED']=='true'
    assert env['CITYVERSE_ONPREM_UE_STATE_FILE'].endswith('ue-state.sqlite3')
