"""Read-only PDU-profile builder from an operator-sealed real SMF bootstrap census.

Creates private artifacts only; never starts a UE or installs a native policy.
The receipt proves the observed PDU identity and current compiler configuration,
not full formal service commissioning or SLA attainment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from scripts.onprem.free_ran_device_smoke import sealed_json
from backend.onprem.intents.domain import SessionKey
from backend.onprem.intents.integration import load_configuration
from backend.onprem.runtime.device_sessions import SessionLaunchSpec
from backend.onprem.runtime.ue_reservation import UEAdmissionLaunchProfile
from backend.onprem.runtime.kubeadm import configured_driver
from backend.onprem.runtime.ue import SLICES,seconds


def typed_bootstrap_member(document, status, driver, native):
    """One sealed typed canary, never a population or capacity grant."""
    from backend.onprem.runtime.current_sessions import V2, session_rows
    from backend.onprem.runtime.device_sessions import DeviceAllocation
    from backend.onprem.runtime.ue_reservation import HeldUEAllocation, ReservedUE
    receipt = document.get('bootstrap_device_admission')
    if (not isinstance(receipt, dict) or set(receipt) != {'generation', 'held', 'driver_configuration_digest'}
            or hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                != document.get('bootstrap_device_admission_sha256')):
        raise ValueError('Sealed typed canary generation and held nonce evidence required')
    if receipt['driver_configuration_digest'] != driver.reservation_configuration_digest:
        raise ValueError('Typed bootstrap captured driver configuration differs; new canary commissioning required')
    generation, held = receipt['generation'], HeldUEAllocation.from_dict(receipt['held'])
    rows = session_rows(status)
    if (status.get('schemaVersion') != V2 or len(rows) != 1 or len(status.get('devices', ())) != 1
            or len(held.devices) != 1 or len(held.allocation) != 1
            or status.get('generation') != generation.get('generation')
            or generation.get('desired') != dict(held.counts) or len(generation.get('allocation', ())) != 1
            or held.baseline_generation >= generation['generation']
            or held.consumed_generation not in (None, generation['generation'])):
        raise ValueError('Exact single-device typed bootstrap ownership required')
    allocation = generation['allocation'][0]
    device = DeviceAllocation.from_dict(allocation['device'])
    reserved = tuple(ReservedUE(**r) for r in allocation.get('reservedSessions', ()))
    member, physical = rows[0], status['devices'][0]
    if (device != held.devices[0] or reserved != held.allocation or len(device.sessions) != 1
            or member['supi'] != native['supi'] or member['supi'] != device.supi
            or member['pduSessionId'] != reserved[0].pdu_session_id
            or member.get('logicalSessionId') != reserved[0].session_id
            or any(member.get(k) != physical.get(k) for k in ('deviceId', 'podUid', 'containerId', 'processEpoch'))
            or any(physical.get(k) is not True for k in ('registered', 'pduEstablished', 'trafficVerified'))
            or member.get('proof', {}).get('source') != 'native-free-ran-ue-single-session/v2'
            or any(member.get('proof', {}).get(k) is not True for k in
                   ('amf_registered', 'smf_active', 'pfcp_kernel_verified', 'tun_present', 'connectivity_verified'))):
        raise ValueError('Typed bootstrap device nonce or complete native PDU proof differs')
    proof = member['proof']
    if (any(proof.get(k) != member.get(k) for k in ('sessionKey', 'podUid', 'containerId', 'processEpoch', 'upfEpoch'))
            or proof.get('smfBootEpoch') != native.get('smf_boot_epoch')
            or not held.created_at <= generation['created'] <= seconds(proof['observedAt']) <= seconds(status['observedAt']) + 1
            or seconds(status['observedAt']) - seconds(proof['observedAt']) > 15):
        raise ValueError('Typed bootstrap original native proof or held generation clocks differ')
    access = allocation.get('spatialAccess') or {}
    radio = proof.get('spatialRan') or {}
    if (not access or any(radio.get(k) != access.get(v) for k,v in (
            ('gnbId', 'gnbId'), ('servingCellId', 'cellId'), ('gnbPodUid', 'gnbPodUid'), ('gnbSearchEndpoint', 'endpoint')))
            or radio.get('source') != 'native-free-ran-ue-single-session/v2' or radio.get('vendor') != 'free-ran-ue'
            or radio.get('pdr_far_gnb_verified') is not True or radio.get('peer', {}).get('ngSetupState') != 'CONNECTED'
            or not radio.get('gnbIncarnation') or not radio.get('amfIncarnation')
            or proof.get('mm_registration_source') != 'native-amf-current-registration+free-ran-gnb-api'
            or member.get('sourceLocation') != allocation.get('sourceLocation')
            or member.get('demandGroupId') != allocation.get('demandGroupId')):
        raise ValueError('Typed bootstrap reciprocal native geographic RAN binding differs')
    driver.device_profile.validate(device)
    return member, allocation


def verify_bootstrap_ue(document,driver,row,*,now):
    evidence=document.get('bootstrap_ue_evidence')
    sha=document.get('bootstrap_ue_evidence_sha256')
    if (not isinstance(evidence,dict) or set(evidence)!={'before','after'}
            or hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()!=sha):
        raise ValueError('Sealed before/after bootstrap UE evidence required')
    identities=[]
    for capture in (evidence['before'],evidence['after']):
        status=capture['status']
        typed = status.get('schemaVersion') == 'cityverse.current-state/v2'
        allocation = None
        if typed:
            member, allocation = typed_bootstrap_member(document, status, driver, row)
            members = [member]
        else:
            members=[r for r in status.get('perUe',()) if r.get('supi')==row['supi']]
        if (len(members)!=1 or len(status.get('perUe',()))!=1 or status.get('failed')!=0
                or not -1<=now-seconds(status['observedAt'])<=1800):raise ValueError('Exact recent bootstrap UE status required')
        member=members[0]
        if typed:
            def sd_number(value):
                import re
                if type(value) is int and 0 <= value <= 0xffffff:return value
                if type(value) is str and re.fullmatch(r'[0-9a-f]{6}', value):return int(value, 16)
                raise ValueError('Canonical typed bootstrap SD required')
            matching_sd = sd_number(member.get('sd')) == sd_number(row.get('sd'))
        else:
            matching_sd = member.get('sd') == row.get('sd')
        if (str(member.get('sessionId'))!=str(row['sessionId']) or member.get('smContextRef')!=row['smContextRef']
                or any(member.get(k)!=row.get(k) for k in ('dnn','sst','upfEpoch')) or not matching_sd
                or str(member.get('pfcpSeid'))!=str(row['pfcpSeid'])
                or not typed and any(member.get(k) is not True for k in ('registered','pduEstablished','trafficVerified'))):
            raise ValueError('Bootstrap UE status differs from exact native SMF incarnation')
        pods=capture.get('pods',{})
        values=list(pods.values()) if isinstance(pods,dict) else pods
        matches=[p for p in values if p.get('metadata',{}).get('uid')==member.get('podUid')]
        if len(matches)!=1:raise ValueError('Bootstrap native Pod UID is not bound to observed session')
        pod=matches[0];meta=pod['metadata'];labels=meta.get('labels',{})
        containers=[c for c in pod['spec']['containers'] if c['name']=='ue']
        states=[c for c in pod['status']['containerStatuses'] if c['name']=='ue']
        if (meta.get('namespace')!=driver.namespace or meta.get('deletionTimestamp') or pod['status'].get('phase')!='Running'
                or labels.get('cityverse.io/managed-by')!='onprem-ue-controller'
                or labels.get('cityverse.io/generation')!=str(status['generation'])
                or json.loads(meta.get('annotations',{}).get('cityverse.io/supis','[]'))!=[row['supi']]
                or len(containers)!=1 or len(states)!=1):raise ValueError('Exact owned bootstrap single-UE Pod required')
        container,state=containers[0],states[0]
        if (container.get('image')!=driver.image or not str(state.get('imageID','')).endswith('@'+driver.device_profile.image_digest)
                or state.get('restartCount')!=0 or state.get('ready') is not True or not state.get('containerID')
                or not state.get('state',{}).get('running')
                or container.get('command')!=['/bin/sh','-c']
                or container.get('args')!=['exec ./free-ran-ue ue -c /run/fru/ue.json -n 1 -p 1']):
            raise ValueError('Bootstrap UE image or exact single-process launch differs')
        configs=[c for c in pod['spec'].get('initContainers',()) if c['name']=='config']
        if len(configs)!=1:raise ValueError('Bootstrap native configuration initializer missing')
        env={v['name']:v.get('value') for v in configs[0].get('env',())}
        if typed:
            from backend.onprem.runtime.ue import shards
            expected = driver.pod_manifest(status['generation'], allocation.get('podIndex', 0), shards([allocation])[0])
            initializer = next(c for c in expected['spec']['initContainers'] if c['name'] == 'config')
            if configs[0].get('command') != initializer.get('command') or configs[0].get('env') != initializer.get('env'):
                raise ValueError('Typed bootstrap exact commissioned initializer differs')
        if (env.get('MSIN')!=row['supi'][10:] or env.get('SST')!=str(row['sst']) or env.get('SD')!=str(row['sd'])
                or not typed and "'dnn': 'internet'" not in configs[0].get('command',[''])[-1] or row['dnn']!='internet'):
            raise ValueError('Bootstrap PDU request differs from observed commissioned session')
        allowed=set(getattr(driver.device_profile,'commissioned_gnb_endpoints',()))|{driver.device_profile.gnb_search_endpoint}
        if env.get('GNB_HOST') not in allowed|{v.split('.')[0] for v in allowed if v}:
            raise ValueError('Bootstrap gNB endpoint is not in the native compiler catalog')
        identities.append((meta['uid'],state['containerID'],state['state']['running'],container,
                           (member.get('processEpoch'), member.get('proof', {}).get('smfBootEpoch')) if typed else None))
    if identities[0]!=identities[1]:raise ValueError('Bootstrap native UE process/container changed between captures')
    return sha


def build_profile(config,driver,document,*,supi,service_type,region,logical_session_id,evidence_reference,now=None,valid_seconds=1800):
    now=time.time() if now is None else now
    census=document.get('native_census',document)
    if (driver is None or driver.backend!='free-ran-ue' or getattr(driver,'device_profile',None) is None
            or driver.device_profile.driver!='free-ran-ue' or driver.device_profile.maximum_sessions!=1):
        raise ValueError('Matching commissioned single-PDU FreeRAN driver required')
    if (census.get('source')!='native-smf-active-sessions' or census.get('complete') is not True
            or census.get('network_scope')!=config['session_inventory']['network_scope']
            or type(census.get('observed_at')) not in (int,float) or not 0<=now-census['observed_at']<=1800
            or not census.get('smf_boot_epoch') or not 60<=valid_seconds<=3600):
        raise ValueError('Recent complete native SMF bootstrap census required')
    rows=[row for row in census.get('sessions',()) if row.get('supi')==supi]
    if len(rows)!=1:raise ValueError('Exactly one observed PDU for the commissioned bootstrap SUPI required')
    row=rows[0];key=SessionKey(census['network_scope'],supi,row['sessionId'],row['smContextRef'])
    if (row.get('session_key')!=key.token or row.get('establishedEpoch')!=key.established_epoch
            or row.get('networkScope')!=key.network_scope or row.get('smf_boot_epoch')!=census['smf_boot_epoch']
            or not row.get('upfEpoch') or not row.get('pfcpSeid')):
        raise ValueError('Canonical native bootstrap session identity required')
    ue_sha=verify_bootstrap_ue(document,driver,row,now=now)
    evidence_reference=evidence_reference+';bootstrap-ue-sha256='+ue_sha
    if document['bootstrap_ue_evidence']['before']['status'].get('schemaVersion') == 'cityverse.current-state/v2':
        evidence_reference += ';bootstrap-device-sha256=' + document['bootstrap_device_admission_sha256']
    sd=int(row['sd'],16) if isinstance(row['sd'],str) else row['sd']
    slices=[name for name,spec in SLICES.items() if spec['sst']==row['sst'] and int(spec['sd'],16)==sd]
    if len(slices)!=1:raise ValueError('Native bootstrap S-NSSAI is not a known commissioned slice')
    name=slices[0]
    spec=SessionLaunchSpec(logical_session_id,row['dnn'],row['sst'],sd,service_type,region)
    if driver.image.split('@')[-1]!=driver.device_profile.image_digest:raise ValueError('Current native compiler/profile image differs')
    digest=driver.reservation_configuration_digest
    identity=hashlib.sha256((evidence_reference+digest+key.token).encode()).hexdigest()[:24]
    return UEAdmissionLaunchProfile('fru-native-'+identity,driver.device_profile.image_digest,digest,evidence_reference,
        ((name,key.pdu_session_id),),now+valid_seconds,'native-ue-device-launch-profile/v2',
        ((name,(spec,)),),((name,logical_session_id,key.pdu_session_id),))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-census',type=Path,required=True);parser.add_argument('--native-census-sha256',required=True)
    for key in ('supi','service-type','region','logical-session-id'):parser.add_argument('--'+key,required=True)
    parser.add_argument('--valid-seconds',type=int,default=1800);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if not args.output.is_absolute() or args.output.exists():parser.error('Output must be a new absolute private profile file')
    config=load_configuration();driver=configured_driver()
    document=sealed_json(args.native_census,args.native_census_sha256)
    profile=build_profile(config,driver,document,supi=args.supi,service_type=args.service_type,region=args.region,
        logical_session_id=args.logical_session_id,evidence_reference=str(args.native_census)+'#sha256='+args.native_census_sha256,
        valid_seconds=args.valid_seconds)
    raw=json.dumps(profile.to_dict(),indent=2,allow_nan=False).encode()
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with args.output.open('xb') as out:out.write(raw)
    print(json.dumps(dict(status='native_pdu_profile_written',file=str(args.output),sha256=hashlib.sha256(raw).hexdigest(),
        profile_id=profile.profile_id,observed_pdu_ids=list(profile.session_ids),formal_service_commissioned=False)))
    return 0


if __name__=='__main__':raise SystemExit(main())
