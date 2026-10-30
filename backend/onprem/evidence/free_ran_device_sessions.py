"""FreeRAN single-PDU native joins; no invented UE NAS/status HTTP endpoint."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ipaddress
import json
import re
import secrets
import shlex
import time

from .device_sessions import NativeDeviceSessionObserver
from backend.onprem.intents.domain import SessionBinding,SessionKey
from backend.onprem.runtime.device_sessions import (DeviceAllocation,DeviceSessionReadback,
    NativeSessionInterface,DEVICE_SPATIAL_FIELDS,validate_device_spatial_context)
from backend.onprem.runtime.ue import EvidenceUnavailable,SLICES,shards,seconds,timestamp
from backend.onprem.runtime.kubeadm import KubeadmReadKind

SOURCE='native-free-ran-ue-single-session/v2'
SHELL_BATCH_SCRIPT='''set -eu
token=$1
shift
while [ "$#" -gt 0 ]; do
    name=$1
    command=$2
    shift 2
    printf '%s\\n' "$token:begin:$name"
    sh -c "$command"
    printf '\\n%s\\n' "$token:end:$name"
done
'''


class FreeRanDeviceRanReader:
    """Native gNB HTTP/reciprocal SCTP plus current gNB kernel addresses."""
    def __init__(self,driver):self.driver=driver
    def __call__(self,pods):
        return self._read(pods)

    def read_selected(self,pods,gnb_ids):
        if not isinstance(gnb_ids,set) or not gnb_ids or any(not isinstance(v,str) or not v for v in gnb_ids):
            raise EvidenceUnavailable('Exact native serving gNB identities required')
        return self._read(pods,gnb_ids)

    def _read(self,pods,gnb_ids=None):
        from backend.onprem.runtime.spatial_ran import configured_registry,native_readouts,_incarnation
        profile=self.driver.device_profile
        if self.driver.backend!='free-ran-ue' or profile.driver!='free-ran-ue':
            raise EvidenceUnavailable('FreeRAN device RAN/profile vendor mismatch')
        registry=configured_registry()
        if gnb_ids is not None:
            if not gnb_ids<={entry['gnbId'] for entry in registry['gnbs']}:
                raise EvidenceUnavailable('Serving gNB is not in the commissioned city registry')
            registry=dict(registry,gnbs=[entry for entry in registry['gnbs'] if entry['gnbId'] in gnb_ids])
        native=native_readouts(self.driver,registry,pods=pods)
        allowed=set(getattr(profile,'commissioned_gnb_endpoints',()))|{profile.gnb_search_endpoint}
        entries=[entry for entry in registry['gnbs'] if entry['endpoint'] in allowed]
        def read_entry(entry):
            matches=[p for p in pods if p.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/name')==entry['app']]
            if len(matches)!=1:raise EvidenceUnavailable('Unique commissioned FreeRAN device gNB missing')
            pod=matches[0];uid=_incarnation(pod,entry['container'],self.driver.namespace)
            raw=self.driver.read_runner(KubeadmReadKind.NATIVE_PROBE)(['kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,
                'exec',pod['metadata']['name'],'-c',entry['container'],'--','ip','-j','-4','address','show'],
                text=True,capture_output=True,timeout=12,check=False)
            if raw.returncode or len(raw.stdout.encode())>1024*1024:raise EvidenceUnavailable('Native FreeRAN gNB interface read failed')
            addresses=sorted({str(ipaddress.IPv4Address(a['local'])) for r in json.loads(raw.stdout)
                for a in r.get('addr_info',()) if a.get('family')=='inet' and not ipaddress.IPv4Address(a['local']).is_loopback})
            if not addresses:
                raise EvidenceUnavailable('FreeRAN native gNB interface incarnation changed')
            proof=dict(native[entry['gnbId']],nativeAddresses=addresses,driverVendor='free-ran-ue',
                sourceRevision=profile.source_revision,imageDigest=profile.image_digest)
            return entry,proof,pod['metadata']['name'],uid
        # Independent native interface captures remain fenced by their exact
        # Pod/container incarnations. Bound fan-out keeps the complete scan
        # inside the existing freshness window on the seven-gNB city.
        with ThreadPoolExecutor(max_workers=min(4,max(1,len(entries)))) as pool:
            captures=list(pool.map(read_entry,entries))
        closing={p['metadata']['name']:p for p in self.driver.read_pods().get('items',())}
        result={}
        for entry,proof,name,uid in captures:
            if _incarnation(closing.get(name,{}),entry['container'],self.driver.namespace)!=uid:
                raise EvidenceUnavailable('FreeRAN native gNB interface incarnation changed')
            result[entry['gnbId']]=proof
        by_slice={}
        for name in SLICES:
            proofs=[result[e['gnbId']] for e in registry['gnbs'] if name in e['slices'] and e['gnbId'] in result]
            if proofs:by_slice[name]=dict(proofs[0],spatialReadouts=proofs)
        return by_slice


class FreeRanDeviceSessionObserver(NativeDeviceSessionObserver):
    def _census_fence(self, census):
        """Stable complete control-plane identity; source clocks stay separate."""
        fields = ('session_key', 'networkScope', 'supi', 'sessionId', 'smContextRef',
                  'establishedEpoch', 'smf_boot_epoch', 'upf_id', 'upf_node_ip',
                  'pfcpSeid', 'upfEpoch', 'dnn', 'sst', 'sd', 'created_at')
        if (census.get('source') != 'native-smf-active-sessions' or census.get('complete') is not True
                or census.get('network_scope') != self.scope or not census.get('smf_boot_epoch')):
            raise EvidenceUnavailable('FreeRAN complete native census scope unavailable')
        self._fresh(census['observed_at'])
        rows = []
        for row in census.get('sessions', ()):
            if any(k not in row for k in fields):
                raise EvidenceUnavailable('FreeRAN native census identity is incomplete')
            if 'observed_at' in row:self._fresh(row['observed_at'])
            rows.append({k:row[k] for k in fields})
        if len({r['session_key'] for r in rows}) != len(rows):
            raise EvidenceUnavailable('FreeRAN native census session identity is duplicated')
        return (census['network_scope'], census['smf_boot_epoch'], census.get('census_revision'),
                sorted(rows, key=lambda r:r['session_key']))

    """Current AMF registration + exact gNB SUPI + SMF/PFCP/TUN, one native UE."""
    def _read_probes(self,pod,probes,*,timeout=10):
        """Pinned FRU has shell/ip/ping, with one host-owned capture deadline."""
        deadline=time.monotonic()+timeout
        names=[name for name,_ in probes]
        if (not 0<timeout<=10 or not names or len(names)>32 or len(set(names))!=len(names)
                or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',n) for n in names)):
            raise EvidenceUnavailable('Invalid FreeRAN native probe batch')
        token='cityverse-fru-'+secrets.token_hex(16)
        arguments=[]
        for name,argv in probes:
            if not argv or any(not isinstance(a,str) or '\x00' in a for a in argv):raise EvidenceUnavailable('Invalid FreeRAN native probe argv')
            arguments.extend((name,shlex.join(argv)))
        remaining=deadline-time.monotonic()
        if remaining<=0:raise EvidenceUnavailable('FreeRAN native probe deadline')
        result=self.driver.read_runner(KubeadmReadKind.NATIVE_PROBE)([
            'kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,
            'exec',pod,'-c','ue','--','sh','-c',SHELL_BATCH_SCRIPT,'cityverse-fru-probe-batch',token,*arguments],
            text=True,capture_output=True,timeout=remaining,check=False)
        if (result.returncode or not isinstance(result.stdout,str) or len(result.stdout.encode())>1048576
                or time.monotonic()>deadline):raise EvidenceUnavailable('FreeRAN native probe failed, oversized or late')
        rest=result.stdout;outputs={}
        for name in names:
            begin=token+':begin:'+name+'\n';end='\n'+token+':end:'+name+'\n'
            if not rest.startswith(begin):raise EvidenceUnavailable('FreeRAN native probe frame missing or reordered')
            value,marker,rest=rest[len(begin):].partition(end)
            if not marker or len(value.encode())>65536:raise EvidenceUnavailable('FreeRAN native probe frame incomplete or oversized')
            outputs[name]=value
        if rest:raise EvidenceUnavailable('Unexpected FreeRAN native probe frame suffix')
        return outputs

    def _ran_fence(self,ran):
        return self._digest({k:v for k,v in ran.items() if k not in ('observedAt','spatialReadouts')})

    def _observe(self,generation,device_ids=None):
        began = time.monotonic()
        profile=self.driver.device_profile
        if self.driver.backend!='free-ran-ue' or profile.driver!='free-ran-ue' or profile.maximum_sessions!=1:
            raise EvidenceUnavailable('FreeRAN exact single-PDU commissioned profile required')
        current=self.store.current()
        if not current or current['generation']!=generation or any('device' not in r for r in current['allocation']):
            raise EvidenceUnavailable('FreeRAN device generation unavailable')
        allocation_digest=self._digest(current['allocation'])
        selected=current['allocation'] if device_ids is None else [r for r in current['allocation'] if r['device']['device_id'] in device_ids]
        if device_ids is not None and {r['device']['device_id'] for r in selected}!=set(device_ids):
            raise EvidenceUnavailable('FreeRAN device subset is not owned')
        if device_ids is None and not selected:raise EvidenceUnavailable('FreeRAN native device population missing')
        if len(selected) > 100:
            raise EvidenceUnavailable('FreeRAN native device observation exceeds 100-device scope')
        scope,upfs=self._registered_scope();opening=self._census(scope)
        registered=getattr(self.oam,'collect_registered_devices',None)
        native=registered() if callable(registered) else self.oam.collect()
        required=('amf',) if callable(registered) else ('amf','smf')
        for name in required:
            if native.get('sources',{}).get(name,{}).get('available') is not True:raise EvidenceUnavailable('FreeRAN AMF registration source unavailable')
            self._fresh(native['sources'][name]['observedAt'])
        pool=self.subscribers.collect();self._fresh(pool['observedAt'])
        if pool.get('verifiedCount')!=1000 or pool.get('credentialsMatched') is not True:raise EvidenceUnavailable('FreeRAN subscriber commissioning unavailable')
        dumps=self._dumps(upfs,scope,opening)
        for dump in dumps.values():
            if dump.get('source')!='gtp5g-netlink' or dump.get('available') is not True or dump.get('errors')!=[] or not dump.get('epoch'):
                raise EvidenceUnavailable('FreeRAN complete native kernel evidence unavailable')
            self._fresh(dump['observedAt'])
        serving_ids=set()
        for unit in selected:
            spatial=validate_device_spatial_context({k:unit[k] for k in DEVICE_SPATIAL_FIELDS if k in unit},profile)
            if not spatial:raise EvidenceUnavailable('FreeRAN device requires commissioned geographic binding')
            serving_ids.add(spatial['spatialAccess']['gnbId'])
        def read_ran():
            pods=self.driver.read_pods().get('items',[])
            selected_reader=getattr(self.gnb_reader,'read_selected',None)
            return selected_reader(pods,serving_ids) if serving_ids and callable(selected_reader) else self.gnb_reader(pods)
        ran_open=read_ran()
        devices,rows,attachments,fences,ran_fences=[],[],[],[],{}
        def read_device(item):
            fallback,unit = item
            device=DeviceAllocation.from_dict(unit['device'])
            if len(device.sessions)!=1:raise EvidenceUnavailable('FreeRAN multiple PDU device unsupported')
            spatial=validate_device_spatial_context({k:unit[k] for k in DEVICE_SPATIAL_FIELDS if k in unit},profile)
            if not spatial:raise EvidenceUnavailable('FreeRAN device requires commissioned geographic binding')
            access=spatial['spatialAccess']
            candidates=ran_open.get(device.access_slice,{}).get('spatialReadouts',())
            matches=[r for r in candidates if r.get('gnbId')==access['gnbId']]
            if len(matches)!=1:raise EvidenceUnavailable('FreeRAN exact commissioned serving gNB unavailable')
            ran=matches[0];self._fresh(ran['observedAt'])
            if (ran.get('source')!='free-ran-ue-api+native-ngap-sctp/v1' or ran.get('ngapAssociated') is not True
                    or ran.get('endpoint')!=access['endpoint'] or ran.get('podUid')!=access['gnbPodUid']
                    or ran.get('cellId')!=access['cellId'] or device.supi not in ran.get('supis',())
                    or ran.get('driverVendor')!='free-ran-ue' or ran.get('imageDigest')!=profile.image_digest
                    or ran.get('sourceRevision')!=profile.source_revision or not ran.get('gnbIncarnation') or not ran.get('amfIncarnation')
                    or ran.get('peer',{}).get('ngSetupState')!='CONNECTED'):
                raise EvidenceUnavailable('FreeRAN native attachment, vendor, UID or profile differs')
            registrations=[r for r in native['ues'] if r.get('supi')==device.supi]
            if len(registrations)!=1 or registrations[0].get('amf',{}).get('registered') is not True:
                raise EvidenceUnavailable('FreeRAN current AMF registration missing or ambiguous')
            expected=self.driver.pod_manifest(generation,unit.get('podIndex',fallback),unit)
            name=expected['metadata']['name'];identity=self._pod(self.driver.read_pod(name),expected,unit.get('deviceBornAt',current['created']))
            probes=self._read_probes(name,[
                ('process',['sh','-c',"readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id"]),
                ('argv',['sh','-c',"tr '\\000' '\\n' < /proc/1/cmdline"]),
                ('interfaces',['ip','-j','-4','address','show'])],timeout=10)
            process=probes['process'].splitlines();argv=probes['argv'].splitlines()
            if (len(process)!=3 or process[0].rsplit('/',1)[-1]!='free-ran-ue' or not process[1].isdigit()
                    or not re.fullmatch(r'[0-9a-f-]{36}',process[2])
                    or len(argv)!=8 or argv[0].rsplit('/',1)[-1]!='free-ran-ue'
                    or argv[1:]!=['ue','-c','/run/fru/ue.json','-n','1','-p','1']):
                raise EvidenceUnavailable('FreeRAN native single-UE process incarnation differs')
            epoch=process[2]+':'+process[1]
            sessions=[r for r in opening['sessions'] if r['supi']==device.supi]
            if len(sessions)!=1:raise EvidenceUnavailable('FreeRAN exact native single-PDU census required')
            session=sessions[0];spec=device.sessions[0]
            if (session['dnn']!=spec.dnn or f"{session['sst']}-{session['sd']}"!=spec.snssai):
                raise EvidenceUnavailable('FreeRAN native PDU differs from commissioned launch demand')
            targets=[(target,cfg) for target,cfg in upfs.items() if cfg['native_node_ip']==session['upf_node_ip'] and cfg['native_upf_id']==session['upf_id']]
            if len(targets)!=1:raise EvidenceUnavailable('FreeRAN selected native UPF is not commissioned')
            target,cfg=targets[0];dump=dumps[target]
            kernel=[r for r in dump.get('sessions',()) if str(r.get('pfcpSeid'))==str(session['pfcpSeid'])]
            if (len(kernel)!=1 or kernel[0].get('evidence')!='kernel-installed-pdr-far' or dump['epoch']!=session['upfEpoch']
                    or kernel[0].get('gnbAddress') not in ran.get('nativeAddresses',())
                    or not identity['created']<=session['created_at']<=self.clock()):
                raise EvidenceUnavailable('FreeRAN installed PFCP/FAR/serving-gNB evidence mismatch')
            address=str(ipaddress.IPv4Address(kernel[0]['ip']))
            interfaces=[r['ifname'] for r in json.loads(probes['interfaces']) if re.fullmatch(r'ueTun[0-9]+',r.get('ifname',''))
                and any(a.get('family')=='inet' and a.get('local')==address for a in r.get('addr_info',()))]
            if len(interfaces)!=1:raise EvidenceUnavailable('FreeRAN exact native TUN/IP binding unavailable')
            interface=NativeSessionInterface(session['sessionId'],interfaces[0],address)
            key=SessionKey(self.scope,device.supi,session['sessionId'],session['smContextRef'])
            binding=SessionBinding(key,spec.snssai,spec.dnn,self.smf_id,target,cfg['slice_instance_id'],spec.region,spec.service_type,
                f"{opening['smf_boot_epoch']}:{dump['epoch']}:{session['pfcpSeid']}",())
            readback=DeviceSessionReadback(device,identity['podUid'],identity['containerId'],epoch,((spec.session_id,binding,interface),))
            self._read_probes(name,[('ping',['ping','-n','-I',interface.interface,'-I',address,'-c','1','-W','2',self.endpoints[target]])],timeout=10)
            attachment=dict(source=SOURCE,supi=device.supi,demandGroupId=spatial['demandGroupId'],gnbId=access['gnbId'],
                servingCellId=access['cellId'],gnbPodUid=ran['podUid'],gnbSearchEndpoint=ran['endpoint'],gnbN3Address=kernel[0]['gnbAddress'],
                pdr_far_gnb_verified=True,observedAt=timestamp(self.clock()),vendor='free-ran-ue',
                gnbIncarnation=deepcopy(ran['gnbIncarnation']),amfIncarnation=deepcopy(ran['amfIncarnation']),peer=deepcopy(ran['peer']))
            common={k:deepcopy(spatial[k]) for k in ('sourceLocation','demandGroupId','servingCellId')}
            summary=readback.per_device()|dict(generation=generation,gnbSearchEndpoint=ran['endpoint'],gnbPodUid=ran['podUid'])|common
            row=readback.per_session()[0]
            row.update(generation=generation,slice=device.access_slice,upfEpoch=session['upfEpoch'],nativeUpfId=session['upf_id'],
                pfcpSeid=session['pfcpSeid'],
                containerId=identity['containerId'],processEpoch=epoch,**common)
            row['proof']=dict(source=SOURCE,observedAt=timestamp(self.clock()),sessionKey=key.token,amf_registered=True,
                mm_registration_source='native-amf-current-registration+free-ran-gnb-api',smf_active=True,pfcp_kernel_verified=True,
                tun_present=True,connectivity_verified=True,gnbSearchEndpoint=ran['endpoint'],gnbPodUid=ran['podUid'],spatialRan=attachment,
                smfBootEpoch=opening['smf_boot_epoch'],upfEpoch=session['upfEpoch'],podUid=identity['podUid'],
                containerId=identity['containerId'],processEpoch=epoch)
            return (summary, row, attachment,
                    (name,expected,identity,process,unit.get('deviceBornAt',current['created'])),
                    access['gnbId'], self._ran_fence(ran))
        units = tuple(enumerate(shards(selected,self.driver.batch_size)))
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(units)))) as executor:
            results = list(executor.map(read_device, units))
        for summary, row, attachment, fence, gnb_id, ran_fence in results:
            if gnb_id in ran_fences and ran_fences[gnb_id] != ran_fence:
                raise EvidenceUnavailable('FreeRAN gNB opening fence differs between devices')
            devices.append(summary);rows.append(row);attachments.append(attachment);fences.append(fence)
            ran_fences[gnb_id] = ran_fence
        if (len(devices) != len(selected) or len(rows) != len(selected)
                or len({r['deviceId'] for r in devices}) != len(selected)
                or len({r['sessionKey'] for r in rows}) != len(selected)):
            raise EvidenceUnavailable('FreeRAN complete unique native device/PDU coverage required')
        closing=self._census(scope)
        if self._census_fence(opening)!=self._census_fence(closing):raise EvidenceUnavailable('FreeRAN SMF census changed during observation')
        ran_close=read_ran()
        closing_by_id={r['gnbId']:r for v in ran_close.values() for r in v.get('spatialReadouts',())}
        for key in ran_fences:
            if key in closing_by_id:self._fresh(closing_by_id[key].get('observedAt'))
        if any(key not in closing_by_id or self._ran_fence(closing_by_id[key])!=value for key,value in ran_fences.items()):
            raise EvidenceUnavailable('FreeRAN gNB incarnation or contexts changed during observation')
        def check_device(fence):
            name,expected,identity,process,born = fence
            if (self._pod(self.driver.read_pod(name),expected,born)!=identity or self._exec(name,'sh','-c',"readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id").splitlines()!=process):
                raise EvidenceUnavailable('FreeRAN native UE process changed during observation')
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(fences)))) as executor:
            list(executor.map(check_device, fences))
        latest=self.store.current()
        if latest['generation']!=generation or self._digest(latest['allocation'])!=allocation_digest:
            raise EvidenceUnavailable('FreeRAN allocation changed during native observation')
        if scope is not None:scope.finish()
        if time.monotonic() - began > self.maximum_age:
            raise EvidenceUnavailable('FreeRAN native device observation exceeded freshness deadline')
        for name in required:
            self._fresh(native['sources'][name]['observedAt'])
        self._fresh(pool['observedAt'])
        self._fresh(opening['observed_at']);self._fresh(closing['observed_at'])
        for dump in dumps.values():self._fresh(dump['observedAt'])
        observed=timestamp(self.clock())
        sources=deepcopy(native['sources']);sources.update(ue=dict(available=True,source=SOURCE,observedAt=observed),
            smf=dict(available=True,source=closing['source'],observedAt=timestamp(closing['observed_at'])),
            upf=dict(available=True,source='gtp5g-netlink',observedAt=min(d['observedAt'] for d in dumps.values())),
            traffic=dict(available=True,source='native-pdu-source-ip-bound-icmp',observedAt=observed))
        return dict(schemaVersion='cityverse.current-state/v2',source=SOURCE,generation=generation,populationRevision=current.get('populationRevision',0),
            observationFence=dict(source='native-free-ran-ue-cohort-fence/v1',
                generation=generation,allocationDigest=allocation_digest,deviceCount=len(devices),sessionCount=len(rows),
                openedAt=opening['observed_at'],closedAt=closing['observed_at'],
                openingCensusDigest=self._digest(self._census_fence(opening)),
                closingCensusDigest=self._digest(self._census_fence(closing))),
            observedAt=observed,devices=devices,perUe=devices,perSession=rows,sources=sources,subscriberPool=pool,gnb=ran_open,ues=[],
            spatialRan=dict(source=SOURCE,generation=generation,observedAt=observed,bindings=attachments))
