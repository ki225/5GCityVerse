"""Native single-device/multi-PDU observation with sealed Pod and census fences."""
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
import time

import yaml

from backend.onprem.intents.domain import SessionBinding, SessionKey
from backend.onprem.runtime.device_sessions import (DeviceAllocation, NativeSessionInterface, join_ueransim_sessions,
    DEVICE_SPATIAL_FIELDS, validate_device_spatial_context)
from backend.onprem.runtime.device_verification import SOURCE
from backend.onprem.runtime.ue import EvidenceUnavailable, SLICES, shards, seconds, timestamp
from backend.onprem.runtime.kubeadm import KubeadmReadKind
from .probe_batch import read_probe_batch


class NativeDeviceSessionObserver:
    def __init__(self, driver, store, native_api, *, network_scope, smf_id,
                 upf_bindings, native_oam, subscribers, gnb_reader, upf_reader,
                 endpoints, clock=time.time, maximum_age_seconds=15,current_upfs=None):
        self.driver,self.store,self.api=driver,store,native_api
        self.scope,self.smf_id=network_scope,smf_id
        self.upfs=dict(upf_bindings)
        self.oam,self.subscribers=native_oam,subscribers
        self.gnb_reader,self.upf_reader=gnb_reader,upf_reader
        self.endpoints=dict(endpoints)
        self.clock,self.maximum_age=clock,maximum_age_seconds
        self.current_upfs=current_upfs
        if not network_scope or not smf_id or not self.upfs or getattr(driver,'device_profile',None) is None:
            raise ValueError('Explicit commissioned device driver and native scope required')
        for address in self.endpoints.values():
            ipaddress.IPv4Address(address)

    def _fresh(self, value):
        value=value if type(value) in (int,float) else seconds(value)
        if not -1<=self.clock()-value<=self.maximum_age:
            raise EvidenceUnavailable('Native device source stale')

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _census_fence(self, census):
        """Compare native state, while validating read timestamps separately.

        Native SMF refreshes each row's observed_at on every inventory read.
        That timestamp is freshness evidence, not a session/configuration
        version. All other native fields (including future fields) stay fenced.
        Row order is not part of a complete inventory's identity.
        """
        self._fresh(census['observed_at'])
        rows=[]
        for row in census['sessions']:
            if 'observed_at' in row:
                self._fresh(row['observed_at'])
            rows.append({key:value for key,value in row.items() if key!='observed_at'})
        return (census['smf_boot_epoch'],census['census_revision'],
                sorted(rows,key=lambda row:row['session_key']))

    def _exec(self,pod,*args):
        result=self.driver.read_runner(KubeadmReadKind.NATIVE_PROBE)(['kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,
            'exec',pod,'-c','ue','--',*args],text=True,capture_output=True,timeout=12,check=False)
        if result.returncode or len(result.stdout.encode())>1024*1024:
            raise EvidenceUnavailable('Native device read or connectivity probe failed')
        return result.stdout

    def _pod(self, actual, expected, created):
        meta,status=actual.get('metadata',{}),actual.get('status',{})
        emeta=expected['metadata']
        if (not meta.get('uid') or meta.get('name')!=emeta['name'] or meta.get('namespace')!=self.driver.namespace
            or meta.get('deletionTimestamp') or status.get('phase')!='Running'
            or any(meta.get('labels',{}).get(k)!=v for k,v in emeta['labels'].items())
            or any(meta.get('annotations',{}).get(k)!=v for k,v in emeta['annotations'].items())):
            raise EvidenceUnavailable('Native device Pod ownership mismatch')
        born=seconds(meta.get('creationTimestamp'))
        if not created<=born<=self.clock():
            raise EvidenceUnavailable('Native device Pod predates allocation')
        spec,reference=actual.get('spec',{}),expected['spec']
        for key in ('restartPolicy','automountServiceAccountToken','nodeSelector','volumes'):
            if spec.get(key)!=reference.get(key):
                raise EvidenceUnavailable('Native device Pod security or volume specification changed')
        if any(spec.get(key) for key in ('hostNetwork','hostPID','hostIPC','shareProcessNamespace','ephemeralContainers','hostAliases')):
            raise EvidenceUnavailable('Native device Pod namespace overlay forbidden')
        for category,status_key in (('containers','containerStatuses'),('initContainers','initContainerStatuses')):
            actual_rows={c['name']:c for c in spec.get(category,[])}
            wanted={c['name']:c for c in reference.get(category,[])}
            if set(actual_rows)!=set(wanted):
                raise EvidenceUnavailable('Native device container overlay forbidden')
            statuses={c['name']:c for c in status.get(status_key,[])}
            for name,row in wanted.items():
                observed=actual_rows[name]
                from backend.onprem.runtime.sealed_container import sealed_container_fields
                if not sealed_container_fields(observed,row,('image','command','args','env','envFrom','volumeMounts','securityContext','resources')):
                    raise EvidenceUnavailable('Native device sealed container specification changed')
                state=statuses.get(name,{})
                digest=row['image'].split('@')[-1]
                if not digest.startswith('sha256:') or not str(state.get('imageID','')).endswith(digest) or state.get('restartCount')!=0:
                    raise EvidenceUnavailable('Native device pinned image or container incarnation mismatch')
                if category=='initContainers':
                    terminated=state.get('state',{}).get('terminated',{})
                    if terminated.get('exitCode')!=0 or not born<=seconds(terminated.get('finishedAt'))<=self.clock():
                        raise EvidenceUnavailable('Native device credential/config init incomplete')
                elif state.get('ready') is not True or not state.get('containerID') or not state.get('state',{}).get('running'):
                    raise EvidenceUnavailable('Native device container not running')
        ue={c['name']:c for c in status['containerStatuses']}['ue']
        return {'podUid':meta['uid'],'containerId':ue['containerID'],'created':born}

    def _census(self,upf_scope=None):
        value=self.api.active_inventory()
        if (value.get('source')!='native-smf-active-sessions' or value.get('complete') is not True
            or value.get('network_scope')!=self.scope or not value.get('smf_boot_epoch')
            or type(value.get('census_revision')) is not int or not isinstance(value.get('sessions'),list)
            or len(value['sessions'])>4096 or not isinstance(value.get('upfs'),list)):
            raise EvidenceUnavailable('Complete native device SMF census unavailable')
        self._fresh(value['observed_at'])
        if upf_scope is not None:upf_scope.validate_census(value)
        seen=set()
        for native in value['sessions']:
            if 'observed_at' in native:
                self._fresh(native['observed_at'])
            key=SessionKey(self.scope,native['supi'],native['sessionId'],native['smContextRef'])
            if (native.get('session_key')!=key.token or key.token in seen or native.get('networkScope')!=self.scope
                or native.get('establishedEpoch')!=key.established_epoch
                or native.get('smf_boot_epoch')!=value['smf_boot_epoch']):
                raise EvidenceUnavailable('Native device census identity malformed')
            seen.add(key.token)
        return value

    def _registered_scope(self):
        scope=self.current_upfs.proof() if self.current_upfs is not None else None
        return scope,scope.bindings() if scope is not None else self.upfs

    def _dumps(self,upfs,scope,census):
        dumps={target:(self.upf_reader(target,upf_scope=scope) if scope is not None else self.upf_reader(target))
               for target in upfs}
        if scope is not None:scope.validate_kernels(census,dumps)
        return dumps

    def observe(self,generation):
        try:
            return self._observe(generation)
        except EvidenceUnavailable:
            raise
        except Exception:
            raise EvidenceUnavailable('Native device session observation unavailable') from None

    def observe_population(self,delta,device_ids):
        """Fresh native proof for an exact subset authorized by current journal.

        Known pending devices remain owned, not adopted into the published
        cohort. Arbitrary caller-selected identities cannot use this port.
        """
        from backend.onprem.runtime.population import PopulationRepository,HeldPopulationDelta
        with self.store.connect() as db:
            record=PopulationRepository(self.store)._owned(db,delta)
        if (type(delta) is not HeldPopulationDelta or not record or not record.get('delta')
                or record['delta']['delta_id']!=delta.delta_id or record['generation']!=delta.generation):
            raise EvidenceUnavailable('Population observation lacks current journal authority')
        known={r['device']['device_id'] for r in record['births']+record['delta']['added_records']}
        if not set(device_ids)<=known:
            raise EvidenceUnavailable('Population observation contains foreign devices')
        empty_barrier=None
        if not device_ids and not record['births'] and record['delta']['state']=='committed':
            empty_barrier=self.population_inventory(delta)
            if any(empty_barrier['native_absent'].get(r['supi']) is not True
                   or f"cv-ue-g{delta.generation}-{r['podIndex']}" in empty_barrier['pods'] for r in record['tombstones']):
                raise EvidenceUnavailable('Owned empty population still has native residuals')
        value=self._observe(delta.generation,set(device_ids))
        if empty_barrier is not None:
            self._fresh(empty_barrier['observedAt'])
            value['nativeEmptyPopulation']={'source':'native-owned-empty-population/v1',
                'generation':delta.generation,'populationRevision':record['revision'],
                'observedAt':empty_barrier['observedAt'],
                'tombstones':[{'deviceId':r['device']['device_id'],'supi':r['supi'],'podIndex':r['podIndex'],
                    'nativeAbsent':True,'podAbsent':True} for r in record['tombstones']]}
        value['populationRevision']=record['revision']
        return value

    def population_inventory(self,delta):
        """Read-only departure barrier: actual AMF + complete SMF/kernel + Pod.

        A missing Pod alone is never a released PDU. Kernel rows without an IP
        cannot discharge a removed member's identity and fail closed.
        """
        from backend.onprem.runtime.population import PopulationRepository
        with self.store.connect() as db:
            record=PopulationRepository(self.store)._owned(db,delta)
        if not record or not record.get('delta') or record['delta']['delta_id']!=delta.delta_id:
            raise EvidenceUnavailable('Population inventory authority changed')
        scope,upfs=self._registered_scope()
        opening=self._census(scope)
        amf_reader=getattr(self.oam,'collect_registered_devices',None)
        native=amf_reader() if callable(amf_reader) else self.oam.collect()
        required_sources=('amf',) if callable(amf_reader) else ('amf','smf')
        for name in required_sources:
            source=native.get('sources',{}).get(name,{})
            if source.get('available') is not True:
                raise EvidenceUnavailable('Population native absence source unavailable')
            self._fresh(source['observedAt'])
        dumps=self._dumps(upfs,scope,opening)
        for dump in dumps.values():
            if (dump.get('source')!='gtp5g-netlink' or dump.get('available') is not True
                    or dump.get('errors')!=[] or not dump.get('epoch') or not isinstance(dump.get('sessions'),list)
                    or any(not row.get('ip') for row in dump['sessions'])):
                raise EvidenceUnavailable('Population complete attributed kernel inventory unavailable')
            self._fresh(dump['observedAt'])
        pods=self.driver.read_pods().get('items',[])
        by_name={p['metadata']['name']:p for p in pods}
        baseline=record.get('baseline_snapshot',{})
        removed={r['supi']:r for r in record['tombstones']+list(delta.removed_records)}
        absent={}
        for supi,row in removed.items():
            owned_ips={r['ueIp'] for r in row.get('nativeBirth',{}).get('sessions',[])}
            if not owned_ips:
                owned_ips={r['ueIp'] for r in baseline.get('perSession',[]) if r.get('supi')==supi}
            if len(owned_ips)!=len(row['device']['sessions']):
                raise EvidenceUnavailable('Population departing member lacks original native binding proof')
            in_amf=any(r.get('supi')==supi and r.get('amf',{}).get('registered') is True for r in native['ues'])
            in_smf=any(r['supi']==supi for r in opening['sessions'])
            in_kernel=any(r.get('ip') in owned_ips for dump in dumps.values() for r in dump['sessions'])
            absent[supi]=not (in_amf or in_smf or in_kernel)
        closing=self._census(scope)
        if self._census_fence(opening)!=self._census_fence(closing):
            raise EvidenceUnavailable('Population native census changed during departure observation')
        if self.store.current()['generation']!=delta.generation:
            raise EvidenceUnavailable('Population generation changed during departure observation')
        clocks=[opening['observed_at'],closing['observed_at']]
        clocks.extend(native['sources'][name]['observedAt'] for name in required_sources)
        clocks.extend(dump['observedAt'] for dump in dumps.values())
        for value in clocks:self._fresh(value)
        oldest=min(value if type(value) in (int,float) else seconds(value) for value in clocks)
        if scope is not None:scope.finish()
        return {'observedAt':timestamp(oldest),'pods':by_name,'native_absent':absent}

    def population_launch_evidence(self,delta):
        """Actual prelaunch guards, independent of pending PDU readiness."""
        from backend.onprem.runtime.population import PopulationRepository
        with self.store.connect() as db:
            record=PopulationRepository(self.store)._owned(db,delta)
        if not record or not record.get('delta') or record['delta']['delta_id']!=delta.delta_id:
            raise EvidenceUnavailable('Population launch authority changed')
        scope,upfs=self._registered_scope()
        census=self._census(scope)
        amf_reader=getattr(self.oam,'collect_registered_devices',None)
        native=amf_reader() if callable(amf_reader) else self.oam.collect()
        required_sources=('amf',) if callable(amf_reader) else ('amf','smf')
        sources=deepcopy(native.get('sources',{}))
        for name in required_sources:
            if sources.get(name,{}).get('available') is not True:
                raise EvidenceUnavailable('Population native prelaunch source unavailable')
            self._fresh(sources[name]['observedAt'])
        pool=self.subscribers.collect()
        self._fresh(pool['observedAt'])
        if pool.get('verifiedCount')!=1000 or pool.get('credentialsMatched') is not True:
            raise EvidenceUnavailable('Population subscriber pool not commissioned')
        pods=self.driver.read_pods().get('items',[])
        gnb=self.gnb_reader(pods)
        dumps=list(self._dumps(upfs,scope,census).values())
        for dump in dumps:
            if (dump.get('source')!='gtp5g-netlink' or dump.get('available') is not True
                    or dump.get('errors')!=[] or not dump.get('epoch')):
                raise EvidenceUnavailable('Population native kernel prelaunch source unavailable')
            self._fresh(dump['observedAt'])
        closing=self._census(scope)
        if self._census_fence(census)!=self._census_fence(closing):
            raise EvidenceUnavailable('Population native prelaunch census changed')
        observed=timestamp(self.clock())
        if callable(amf_reader):
            sources['smf']={'available':True,'source':census['source'],'observedAt':timestamp(census['observed_at']),
                'inventoryComplete':True,'bootEpoch':census['smf_boot_epoch'],'contextCount':len(census['sessions'])}
        sources.update(ue={'available':True,'source':'native-kubernetes-pod-list','observedAt':observed},
                       upf={'available':True,'source':'gtp5g-netlink','observedAt':min(d['observedAt'] for d in dumps)})
        if scope is not None:scope.finish()
        return {'generation':delta.generation,'observedAt':observed,'sources':sources,
                'subscriberPool':pool,'gnb':gnb,'ues':[]}

    def _observe(self,generation,device_ids=None):
        started=self.clock()
        current=self.store.current()
        if not current or current['generation']!=generation or (not current['allocation'] and device_ids is None) or any('device' not in r for r in current['allocation']):
            raise EvidenceUnavailable('Native device generation missing')
        allocation_digest=self._digest(current['allocation'])
        selected=current['allocation']
        if device_ids is not None:
            selected=[row for row in selected if row['device']['device_id'] in device_ids]
            if {row['device']['device_id'] for row in selected}!=set(device_ids):
                raise EvidenceUnavailable('Population subset contains an unowned device')
        scope,upfs=self._registered_scope()
        opening=self._census(scope)
        amf_reader=getattr(self.oam,'collect_registered_devices',None)
        native=amf_reader() if callable(amf_reader) else self.oam.collect()
        required_sources=('amf',) if callable(amf_reader) else ('amf','smf')
        if any(native.get('sources',{}).get(name,{}).get('available') is not True for name in required_sources):
            raise EvidenceUnavailable('Native AMF/SMF source unavailable')
        for name in required_sources:
            self._fresh(native['sources'][name]['observedAt'])
        amf={}
        for row in native['ues']:
            if row['supi'] in amf:
                raise EvidenceUnavailable('Native AMF registration ambiguous')
            amf[row['supi']]=row['amf']
        subscribers=self.subscribers.collect()
        self._fresh(subscribers['observedAt'])
        if subscribers.get('verifiedCount')!=1000 or subscribers.get('credentialsMatched') is not True:
            raise EvidenceUnavailable('Native subscriber commissioning unavailable')
        dumps=self._dumps(upfs,scope,opening)
        for target,dump in dumps.items():
            if (dump.get('source')!='gtp5g-netlink' or dump.get('available') is not True
                or dump.get('errors')!=[] or not dump.get('epoch') or not isinstance(dump.get('sessions'),list)):
                raise EvidenceUnavailable('Native complete UPF sources unavailable')
            self._fresh(dump['observedAt'])
        all_pods=self.driver.read_pods().get('items',[])
        gnb=self.gnb_reader(all_pods)
        device_rows,session_rows,pod_fences=[],[],[]
        spatial_bindings, spatial_ran_fences = [], {}
        for index,unit in enumerate(shards(selected,self.driver.batch_size)):
            index=unit.get('podIndex',index)
            device=DeviceAllocation.from_dict(unit['device'])
            ran=gnb.get(device.access_slice,{})
            if ran.get('ngapAssociated') is not True:
                raise EvidenceUnavailable('Native device access gNB association missing')
            self._fresh(ran['observedAt'])
            endpoint=self.driver.device_profile.gnb_search_endpoint
            if (ran.get('gnbSearchEndpoint')!=endpoint or not ran.get('gnbPodIp')
                    or not ran.get('podUid')):
                raise EvidenceUnavailable('Native device commissioned gNB endpoint proof mismatch')
            spatial = validate_device_spatial_context({k:unit[k] for k in DEVICE_SPATIAL_FIELDS if k in unit}, self.driver.device_profile)
            if spatial:
                if (spatial['spatialAccess']['gnbPodUid'] != ran['podUid']
                        or ran.get('source') != 'native-ngap-sctp/v2' or ran.get('available') is not True
                        or ran.get('sourceRevision') != self.driver.device_profile.source_revision
                        or ran.get('imageDigest') != self.driver.device_profile.image_digest
                        or not ran.get('processEpoch') or not ran.get('n3NetworkPolicy')):
                    raise EvidenceUnavailable('Native spatial gNB incarnation or profile differs from frozen context')
                spatial_ran_fences[device.access_slice] = deepcopy(ran)
            expected=self.driver.pod_manifest(generation,index,unit)
            name=expected['metadata']['name']
            actual=self.driver.read_pod(name)
            identity=self._pod(actual,expected,unit.get('deviceBornAt',current['created']))
            probes=read_probe_batch(lambda *args:self._exec(name,*args),[
                ('addresses',['python','-c',"import json,socket,sys;print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],4997,socket.AF_INET,socket.SOCK_DGRAM)})))",endpoint]),
                ('configured',['python','-c',"import json;print(json.dumps(json.load(open('/run/ue/config.yaml')).get('gnbSearchList'))) "]),
                ('process',['sh','-c',"readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id"]),
                ('node',['/ueransim/nr-cli','--dump']),
                ('status',['/ueransim/nr-cli',device.supi,'--exec','status']),
                ('ps',['/ueransim/nr-cli',device.supi,'--exec','ps-list']),
                ('interfaces',['ip','-j','-4','address','show'])],timeout=10,outer_timeout=12)
            addresses=json.loads(probes['addresses'])
            configured=json.loads(probes['configured'])
            if addresses!=[ran['gnbPodIp']] or configured!=addresses:
                raise EvidenceUnavailable('Native UE search destination differs from commissioned isolated gNB')
            process=probes['process'].splitlines()
            if len(process)!=3 or process[0]!='/ueransim/nr-ue' or not process[1].isdigit() or not re.fullmatch(r'[0-9a-f-]{36}',process[2]):
                raise EvidenceUnavailable('Native device process incarnation unavailable')
            process_epoch=process[2]+':'+process[1]
            node=probes['node'].strip()
            if node!=device.supi:
                raise EvidenceUnavailable('Multiple or foreign UE processes in device Pod')
            status=yaml.safe_load(probes['status'])
            if status.get('mm-state')!='MM-REGISTERED/NORMAL-SERVICE':
                raise EvidenceUnavailable('Native UE MM registration not established')
            if amf.get(device.supi,{}).get('registered') is not True:
                raise EvidenceUnavailable('Native AMF registration not established')
            ps=yaml.safe_load(probes['ps'])
            interfaces=json.loads(probes['interfaces'])
            paths=[]
            for label,row in ps.items():
                match=re.fullmatch(r'PDU Session([1-9]|1[0-5])',label)
                if not match:
                    raise EvidenceUnavailable('Native PDU list malformed')
                ip=str(ipaddress.IPv4Address(row['address']))
                matching=[r['ifname'] for r in interfaces if re.fullmatch(r'uesimtun[0-9]{1,3}',r.get('ifname',''))
                    and any(a.get('family')=='inet' and a.get('local')==ip for a in r.get('addr_info',[]))]
                if len(matching)!=1:
                    raise EvidenceUnavailable('Native PDU TUN/IP ambiguous')
                paths.append(NativeSessionInterface(int(match[1]),matching[0],ip))
            native_rows=[r for r in opening['sessions'] if r['supi']==device.supi]
            bindings=[]
            ping_probes=[]
            for row in native_rows:
                target_rows=[(target,cfg) for target,cfg in upfs.items() if cfg['native_node_ip']==row['upf_node_ip']]
                if len(target_rows)!=1:
                    raise EvidenceUnavailable('Native UPF target not commissioned')
                target,configuration=target_rows[0]
                if configuration.get('native_upf_id')!=row['upf_id']:
                    raise EvidenceUnavailable('Native UPF UUID changed')
                dump=dumps[target]
                self._fresh(dump['observedAt'])
                path=[p for p in paths if p.pdu_session_id==row['sessionId']]
                kernel=[s for s in dump['sessions'] if str(s.get('pfcpSeid'))==str(row['pfcpSeid'])]
                if (len(path)!=1 or len(kernel)!=1 or kernel[0].get('ip')!=path[0].address
                    or kernel[0].get('evidence')!='kernel-installed-pdr-far' or dump.get('epoch')!=row['upfEpoch']
                    or dump.get('source')!='gtp5g-netlink' or dump.get('available') is not True
                    or dump.get('errors')!=[] or not identity['created']<=row['created_at']<=self.clock()):
                    raise EvidenceUnavailable('Native SMF/PFCP/kernel/TUN path proof mismatch')
                if spatial:
                    native_n3 = {str(ipaddress.IPv4Address(p['address'])) for p in ran['n3NetworkPolicy'].values()}
                    if kernel[0].get('gnbAddress') not in native_n3:
                        raise EvidenceUnavailable('Installed PDU downlink FAR does not reach commissioned spatial gNB')
                snssai=f"{row['sst']}-{row['sd']}"
                specs=[s for s in device.sessions if (s.dnn,s.snssai)==(row['dnn'],snssai)]
                if len(specs)!=1:
                    raise EvidenceUnavailable('Native device session not in sealed configuration')
                spec=specs[0]
                ping_probes.append((str(row['sessionId']),['ping','-n','-I',path[0].interface,'-I',path[0].address,'-c','1','-W','2',self.endpoints[target]]))
                bindings.append(SessionBinding(SessionKey(self.scope,device.supi,row['sessionId'],row['smContextRef']),
                    snssai,row['dnn'],self.smf_id,target,configuration['slice_instance_id'],spec.region,spec.service_type,
                    f"{opening['smf_boot_epoch']}:{row['upfEpoch']}:{row['pfcpSeid']}",()))
            readback=join_ueransim_sessions(device,ps,bindings,paths,pod_uid=identity['podUid'],
                container_id=identity['containerId'],process_epoch=process_epoch)
            read_probe_batch(lambda *args:self._exec(name,*args),ping_probes,timeout=10,outer_timeout=12)
            summary=readback.per_device()|{'generation':generation,
                'gnbSearchEndpoint':endpoint,'gnbPodUid':ran['podUid'],'gnbPodIp':ran['gnbPodIp']}
            if spatial:
                summary.update({k:deepcopy(spatial[k]) for k in ('demandGroupId','sourceLocation','servingCellId')})
            device_rows.append(summary)
            for row in readback.per_session():
                native_row=next(r for r in native_rows if r['sessionId']==row['pduSessionId'])
                slice_names=[key for key,s in SLICES.items() if (s['sst'],s['sd'])==(row['sst'],f"{row['sd']:06x}")]
                if len(slice_names)!=1:
                    raise EvidenceUnavailable('Native slice traffic binding uncommissioned')
                row.update(generation=generation,slice=slice_names[0],upfEpoch=native_row['upfEpoch'],
                    nativeUpfId=native_row['upf_id'],containerId=identity['containerId'],processEpoch=process_epoch)
                row['proof']={'source':SOURCE,'observedAt':timestamp(self.clock()),'sessionKey':row['sessionKey'],
                    'amf_registered':True,'smf_active':True,'pfcp_kernel_verified':True,'tun_present':True,
                    'connectivity_verified':True,'smfBootEpoch':opening['smf_boot_epoch'],
                    'upfEpoch':row['upfEpoch'],'podUid':identity['podUid'],
                    'gnbSearchEndpoint':endpoint,'gnbPodUid':ran['podUid'],'gnbPodIp':ran['gnbPodIp'],
                    'containerId':identity['containerId'],'processEpoch':process_epoch}
                if spatial:
                    kernel_session = next(s for s in dumps[next(t for t,c in upfs.items() if c['native_upf_id']==native_row['upf_id'])]['sessions']
                                          if str(s.get('pfcpSeid'))==str(native_row['pfcpSeid']))
                    proof = {'source':'native-ueransim-single-commissioned-gnb/v1', 'supi':device.supi,
                        'demandGroupId':spatial['demandGroupId'],'gnbId':spatial['spatialAccess']['gnbId'],
                        'servingCellId':spatial['servingCellId'],'gnbPodUid':ran['podUid'],
                        'gnbSearchEndpoint':endpoint,'gnbN3Address':kernel_session['gnbAddress'],
                        'pdr_far_gnb_verified':True,'observedAt':timestamp(self.clock())}
                    row.update({k:deepcopy(spatial[k]) for k in ('demandGroupId','sourceLocation','servingCellId')})
                    row['proof']['spatialRan'] = proof
                    if not any(p['supi']==device.supi for p in spatial_bindings):
                        spatial_bindings.append(deepcopy(proof))
                session_rows.append(row)
            pod_fences.append((name,expected,identity,process,unit.get('deviceBornAt',current['created'])))
        closing=self._census(scope)
        if self._census_fence(closing)!=self._census_fence(opening):
            raise EvidenceUnavailable('Native SMF census changed during device observation')
        upf_epochs=lambda census:sorted((u['upf_id'],u['node_ip'],u['upf_epoch']) for u in census['upfs'])
        if upf_epochs(closing)!=upf_epochs(opening):
            raise EvidenceUnavailable('Native UPF incarnation changed during device observation')
        if spatial_ran_fences:
            native_closing = self.gnb_reader(self.driver.read_pods().get('items', []))
            fields = ('podUid','gnbPodIp','gnbSearchEndpoint','n3NetworkPolicy','processEpoch','sourceRevision','imageDigest','ngapAssociated','available','source')
            for group, opening_ran in spatial_ran_fences.items():
                current_ran = native_closing.get(group, {})
                self._fresh(current_ran.get('observedAt'))
                if any(current_ran.get(k) != opening_ran.get(k) for k in fields):
                    raise EvidenceUnavailable('Native spatial gNB changed during device observation')
        for name,expected,identity,process,born in pod_fences:
            if self._pod(self.driver.read_pod(name),expected,born)!=identity or read_probe_batch(
                    lambda *args:self._exec(name,*args),[('process',['sh','-c',"readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id"])],
                    timeout=10,outer_timeout=12)['process'].splitlines()!=process:
                raise EvidenceUnavailable('Native device Pod/process changed during observation')
        latest=self.store.current()
        if not latest or latest['generation']!=generation or self._digest(latest['allocation'])!=allocation_digest:
            raise EvidenceUnavailable('Native device generation changed during observation')
        if self.clock()-started>self.maximum_age:
            raise EvidenceUnavailable('Native device observation exceeded freshness deadline')
        for name in required_sources:
            self._fresh(native['sources'][name]['observedAt'])
        self._fresh(subscribers['observedAt'])
        for dump in dumps.values():
            self._fresh(dump['observedAt'])
        sources=deepcopy(native['sources'])
        if scope is not None:scope.finish()
        if callable(amf_reader):
            sources['smf']={'available':True,'source':opening['source'],'observedAt':timestamp(opening['observed_at']),
                'inventoryComplete':True,'bootEpoch':opening['smf_boot_epoch'],'contextCount':len(opening['sessions'])}
        observed=timestamp(self.clock())
        sources.update(ue={'available':True,'source':SOURCE,'observedAt':observed},
            upf={'available':True,'source':'gtp5g-netlink','observedAt':min(d['observedAt'] for d in dumps.values())},
            traffic={'available':True,'source':'native-pdu-source-ip-bound-icmp','observedAt':observed})
        if scope is not None:
            sources['upfRegistry']={'available':True,'source':'cityverse.commissioned-upfs/v1',
                'observedAt':timestamp(min(scope.snapshot.observed_at,*[b.observed_at for b in scope.snapshot.observation_bindings()])),
                'revision':scope.revision,'registeredTargets':sorted(scope.rows)}
        result = {'schemaVersion':'cityverse.current-state/v2','source':SOURCE,'generation':generation,
            'populationRevision':current.get('populationRevision',0),
            'observedAt':observed,'devices':device_rows,'perUe':device_rows,'perSession':session_rows,
            'sources':sources,'subscriberPool':subscribers,'gnb':gnb,'ues':[]}
        if spatial_bindings:
            result['spatialRan'] = {'source':'native-ueransim-single-commissioned-gnb/v1',
                'generation':generation,'observedAt':observed,'bindings':spatial_bindings}
        return result


def configured_native_device_observer(collector,config):
    """Composition at the real NativeCollector constructor, never browser authority."""
    from backend.onprem.intents.resource_adapters import FixedOriginSessionQerApi, _token_file_reader
    values=config.get('session_inventory',{})
    qos=config.get('providers',{}).get('qos',{})
    endpoints=config.get('ue_device_endpoints')
    if (not values.get('network_scope') or not values.get('smf_id') or not values.get('upf_bindings')
        or not qos.get('origin') or not qos.get('token_file') or not endpoints):
        raise ValueError('not_configured: native device census, credentials and DN endpoints required')
    api=FixedOriginSessionQerApi(qos['origin'],token_reader=_token_file_reader(qos['token_file']),
        timeout=min(qos.get('timeout',5),10))
    profile=getattr(collector.driver,'device_profile',None)
    if profile is None or getattr(collector.driver,'backend','ueransim')!=profile.driver:
        raise ValueError('not_configured: native device driver/profile vendor mismatch')
    observer_class=NativeDeviceSessionObserver
    if profile.driver=='free-ran-ue':
        from .free_ran_device_sessions import FreeRanDeviceSessionObserver,FreeRanDeviceRanReader
        observer_class=FreeRanDeviceSessionObserver
        gnb_reader=FreeRanDeviceRanReader(collector.driver)
    else:
        ran=config.get('ue_device_ran')
        if not ran:
            raise ValueError('not_configured: matching isolated UERANSIM RAN proof required')
        from .device_ran import NativeDeviceRanReader
        gnb_reader=NativeDeviceRanReader(collector.driver,**ran)
    from backend.onprem.intents.current_upf_scope import configured_current_upfs
    current_upfs=configured_current_upfs(config)
    def upf_reader(target,*,upf_scope=None):
        if upf_scope is not None:return collector._upf(target,upf_scope=upf_scope,finalize_scope=False)
        name=values['upf_bindings'][target].get('collector_target',target)
        if name not in SLICES:
            raise EvidenceUnavailable('Explicit device UPF collector target mapping required')
        return collector._upf(name)
    observer=observer_class(collector.driver,collector.store,api,
        network_scope=values['network_scope'],smf_id=values['smf_id'],upf_bindings=values['upf_bindings'],
        native_oam=collector.native,subscribers=collector.subscribers,
        gnb_reader=gnb_reader,upf_reader=upf_reader,
        endpoints=endpoints,current_upfs=current_upfs)
    collector.driver.device_ran_reader=gnb_reader
    return observer
