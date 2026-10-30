"""Exact, separate web100 admission. No general capacity or policy authority."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time

from backend.onprem.scenarios.contracts import validate_round, EVENT_SLICES
from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
from backend.onprem.capacity.performance import criteria_metadata

PROTOCOL='cityverse.typed-web100-capacity-protocol/v1'
CONFIG='cityverse.typed-web100-capacity-config/v1'
MANIFEST='cityverse.typed-web100-capacity-manifest/v1'
MAX_BYTES=16*1024*1024

class ScopedCapacityBlocked(ValueError):pass

def need(value, code):
    if not value:raise ScopedCapacityBlocked(code)

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()

def read_pinned(filename, sha):
    path=Path(filename)
    need(path.is_absolute() and not path.is_symlink() and path.is_file(),'scoped_capacity_path')
    raw=path.read_bytes()
    need(0<len(raw)<=MAX_BYTES and hashlib.sha256(raw).hexdigest()==sha,'scoped_capacity_digest')
    def unique(pairs):
        result={}
        for key,value in pairs:
            need(key not in result,'scoped_capacity_duplicate_key');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:need(False,'scoped_capacity_nonfinite'))

def request_scope(body):
    body=validate_round(body)
    need(body['strategy']=='none' and body.get('trafficMode')=='qos'
        and body['baselineUes']==100 and set(body['events'])==set(EVENT_SLICES)
        and not any(body['events'].values()),'scoped_capacity_request_profile')
    resolved=ServiceRequirementCompiler.compile(body.get('serviceRequirements'),body,traffic_mode='qos')
    need(set(resolved['services'])=={'web_browsing'},'scoped_capacity_services')
    service=resolved['services']['web_browsing']
    need(service['offeredUplinkKbpsPerUe']==service['offeredDownlinkKbpsPerUe']==100,'scoped_capacity_rates')
    need(bool(body.get('demandGroups')) and all(g['profileId']=='web_browsing' for g in body['demandGroups']),
         'scoped_capacity_geography')
    return dict(profileId='web_browsing',devices=100,pdusPerDevice=1,slice='embb',trafficMode='qos',
        offeredUplinkKbpsPerDevice=100,offeredDownlinkKbpsPerDevice=100,
        requirementsSchema=body['serviceRequirements']['schemaVersion'],
        topologyRevision=body['topologyRevision'],demandGroups=deepcopy(body['demandGroups']))

def runtime_scope(driver):
    """Native GETs only, called by the owning backend/operator, never a write."""
    from backend.onprem.agents.startup import _runtime
    from backend.onprem.runtime.spatial_ran import configured_registry
    from backend.onprem.runtime.kubeadm import TRAFFIC_IMAGE
    registry=configured_registry()
    pods=driver._read_kubectl('get','pods','-o','json').get('items',[])
    apps={g['app'] for g in registry['gnbs']}|{'smf','amf'}|{'upf-'+s for s in ('embb','urllc','mmtc','v2x')}
    incarnations=[]
    for app in sorted(apps):
        found=[p for p in pods if p.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/name')==app]
        need(len(found)==1,'scoped_capacity_native_population')
        pod=found[0];meta=pod['metadata'];states=pod.get('status',{}).get('containerStatuses',[])
        need(meta.get('uid') and not meta.get('deletionTimestamp') and pod.get('status',{}).get('phase')=='Running'
            and states and all(s.get('ready') is True and s.get('containerID') and s.get('imageID') for s in states),
            'scoped_capacity_native_incarnation')
        incarnations.append(dict(app=app,uid=meta['uid'],containers=[{k:s.get(k) for k in
            ('name','containerID','imageID','restartCount')} for s in sorted(states,key=lambda s:s['name'])],
            resources=[{'name':c['name'],'resources':c.get('resources')} for c in pod['spec']['containers']]))
    catalog=Path(__file__).resolve().parents[3]/'shared/service-profiles.json'
    from backend.onprem.intents.integration import load_configuration
    config=load_configuration()
    profile=config.get('ue_launch_profile',{})
    return dict(runtime=_runtime(driver,TRAFFIC_IMAGE),nativeIncarnations=incarnations,
        serviceCatalog=deepcopy(config.get('service_catalog')),
        launchPduSpecs={key:deepcopy(profile.get(key)) for key in ('device_sessions','device_session_ids','session_ids')},
        writeAuthorizationSha256=digest({key:config.get(key) for key in ('targets','capacity_profiles','budgets','authority')}),
        topologyRegistrySha256=digest(registry),sharedServiceCatalogSha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
        initializerConfigurationSha256=driver.reservation_configuration_digest,
        runtimePolicySha256=driver.runtime_policy_digest,resourcePolicySha256=driver.resource_policy_digest)

def make_protocol(body, runtime, *, now=None, trial_seconds=3600, grant_seconds=3600):
    now=time.time() if now is None else now
    need(type(now) in (int,float) and math.isfinite(now) and type(trial_seconds) is int and 60<=trial_seconds<=3600
        and type(grant_seconds) is int and 60<=grant_seconds<=3600,'scoped_capacity_protocol_time')
    value=dict(schemaVersion=PROTOCOL,declaredAt=now,trialValidUntil=now+trial_seconds,
        grantMaximumAgeSeconds=grant_seconds,requestScope=request_scope(body),runtime=deepcopy(runtime),
        runtimeFingerprint=digest(runtime),criteria=criteria_metadata(),requiredStages=['measuring_result'],
        requiredWindowCount=1,windowSelection='first-measuring-result/v1',expectedDevices=100,expectedSessions=100,expectedDirectionalJobs=200,
        minimumCommonReceiverSeconds=8)
    value['requireRenderedEvidence']=True
    validate_protocol(value,now=now)
    return value

def validate_protocol(value, *, now=None):
    expected={'schemaVersion','declaredAt','trialValidUntil','grantMaximumAgeSeconds','requestScope','runtime',
        'runtimeFingerprint','criteria','requiredStages','requiredWindowCount','expectedDevices','expectedSessions',
        'expectedDirectionalJobs','minimumCommonReceiverSeconds','requireRenderedEvidence','windowSelection'}
    need(type(value) is dict and set(value)==expected and value['schemaVersion']==PROTOCOL,'scoped_capacity_protocol_schema')
    need(value['criteria']==criteria_metadata() and value['runtimeFingerprint']==digest(value['runtime'])
        and value['requiredStages']==['measuring_result'] and value['requiredWindowCount']==1
        and value['windowSelection']=='first-measuring-result/v1'
        and value['expectedDevices']==value['expectedSessions']==100 and value['expectedDirectionalJobs']==200
        and value['minimumCommonReceiverSeconds']==8 and value['requireRenderedEvidence'] is True,
        'scoped_capacity_protocol_contract')
    scope=value['requestScope']
    need(type(scope) is dict and scope.get('profileId')=='web_browsing' and scope.get('devices')==100
        and scope.get('pdusPerDevice')==1 and scope.get('slice')=='embb' and scope.get('trafficMode')=='qos'
        and scope.get('offeredUplinkKbpsPerDevice')==scope.get('offeredDownlinkKbpsPerDevice')==100,
        'scoped_capacity_protocol_request')
    need(type(value['grantMaximumAgeSeconds']) is int and 60<=value['grantMaximumAgeSeconds']<=3600,
         'scoped_capacity_grant_time')
    for key in ('declaredAt','trialValidUntil'):
        need(type(value[key]) in (int,float) and math.isfinite(value[key]),'scoped_capacity_protocol_clock')
    need(60<=value['trialValidUntil']-value['declaredAt']<=3600,'scoped_capacity_protocol_clock')
    if now is not None:need(value['declaredAt']<=now<=value['trialValidUntil'],'scoped_capacity_protocol_expired')
    return value

@dataclass(frozen=True)
class ScopedVerifiedTrafficCapacityGrant:
    protocol_sha256:str
    manifest_sha256:str
    runtime_fingerprint:str
    request_scope_sha256:str
    valid_until:float
    verified_devices:int=100

    def matches(self,body,runtime,now):
        try:return (now<self.valid_until and digest(request_scope(body))==self.request_scope_sha256
            and digest(runtime)==self.runtime_fingerprint)
        except (ValueError,TypeError,KeyError):return False
