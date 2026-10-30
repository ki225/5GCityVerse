"""Isolated UERANSIM gNB and reciprocal AMF kernel SCTP evidence."""
import json
import ipaddress
import re
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from backend.onprem.runtime.ue import EvidenceUnavailable,timestamp,seconds,SLICES
from .probe_batch import read_probe_batch


class NativeDeviceRanReader:
    def __init__(self,driver,*,namespace,pod_name,container,image,amf_namespace,amf_selector,groups,n3_addresses,clock=time.time):
        self.driver=driver
        self.namespace,self.pod_name,self.container=namespace,pod_name,container
        self.image,self.amf_namespace,self.amf_selector=image,amf_namespace,amf_selector
        self.groups,self.clock=tuple(groups),clock
        if (not isinstance(n3_addresses,dict) or set(n3_addresses)!=set(self.groups)
                or len(set(n3_addresses.values()))!=len(n3_addresses)):
            raise ValueError('Explicit isolated N3 interface addresses required')
        self.n3_addresses={k:str(ipaddress.IPv4Address(v)) for k,v in sorted(n3_addresses.items())}
        profile=getattr(driver,'device_profile',None)
        if (not self.groups or len(set(self.groups))!=len(self.groups) or not set(self.groups)<=set(SLICES)
                or not image or '@sha256:' not in image or not re.fullmatch(r'cityverse-device-[a-z0-9-]{1,40}',namespace)
                or pod_name!='gnb-device'
                or profile is None or getattr(driver,'image',None)!=image
                or image.split('@')[-1]!=getattr(profile,'image_digest',None)
                or not getattr(profile,'source_revision',None)):
            raise ValueError('Commissioned isolated RAN configuration required')

    def _network_policy(self,pod,config,observed):
        from backend.onprem.runtime.ueransim_device_canary import INIT
        spec=pod['spec'];expected=[dict(interface='n3'+str(i),address=ip) for i,(_,ip) in enumerate(self.n3_addresses.items())]
        if (any(spec.get(k,False) is not False for k in ('hostNetwork','hostPID','hostIPC','shareProcessNamespace'))
                or spec.get('ephemeralContainers',[])!=[]
                or spec.get('automountServiceAccountToken') is not False
                or spec.get('volumes')!=[{'name':'config','emptyDir':{'medium':'Memory'}}]
                or len(spec.get('initContainers',[]))!=1 or len(spec.get('containers',[]))!=1):
            raise EvidenceUnavailable('Isolated RAN namespace boundary changed')
        init=spec['initContainers'][0];runtime=spec['containers'][0]
        defaults={'terminationMessagePath','terminationMessagePolicy','imagePullPolicy','resources'}
        if (set(init)-({'name','image','command','env','securityContext','volumeMounts'}|defaults)
                or set(runtime)-({'name','image','command','securityContext','volumeMounts'}|defaults)
                or init.get('name')!='config' or init.get('image')!=self.image or init.get('command')!=['python','-c',INIT]
                or init.get('securityContext')!={'privileged':True,'allowPrivilegeEscalation':True,'readOnlyRootFilesystem':True}
                or init.get('volumeMounts')!=[{'name':'config','mountPath':'/run/gnb'}]
                or runtime.get('name')!=self.container or runtime.get('image')!=self.image
                or runtime.get('command')!=['/ueransim/nr-gnb','-c','/run/gnb/gnb.yaml']
                or runtime.get('securityContext')!={'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}
                or runtime.get('volumeMounts')!=[{'name':'config','mountPath':'/run/gnb','readOnly':True}]):
            raise EvidenceUnavailable('Isolated RAN sealed init or runtime specification changed')
        env=init.get('env',[])
        if len(env)!=3 or {x.get('name') for x in env}!={'RAN_CONFIG','POD_IP','ARP_INTERFACES'}:
            raise EvidenceUnavailable('Isolated RAN setup environment changed')
        values={x['name']:x for x in env}
        if any(set(values[k])!={'name','value'} for k in ('RAN_CONFIG','ARP_INTERFACES')):
            raise EvidenceUnavailable('Isolated RAN setup values changed')
        pod_ip=values['POD_IP'];reference=pod_ip.get('valueFrom',{}).get('fieldRef',{})
        if (set(pod_ip)!={'name','valueFrom'} or set(pod_ip['valueFrom'])!={'fieldRef'}
                or reference.get('fieldPath')!='status.podIP' or reference.get('apiVersion','v1')!='v1'
                or set(reference)-{'fieldPath','apiVersion'}):
            raise EvidenceUnavailable('Isolated RAN Pod address source changed')
        declared=json.loads(values['RAN_CONFIG']['value'])
        declared.update(linkIp=pod['status']['podIP'],gtpIp=pod['status']['podIP'],gtpAdvertiseIp=pod['status']['podIP'])
        if declared!=config or json.loads(values['ARP_INTERFACES']['value'])!=expected:
            raise EvidenceUnavailable('Isolated RAN setup declaration changed')
        states=pod['status'].get('initContainerStatuses',[])
        if (len(states)!=1 or states[0].get('name')!='config' or states[0].get('restartCount')!=0
                or states[0].get('state',{}).get('terminated',{}).get('exitCode')!=0
                or not states[0].get('containerID') or not states[0].get('imageID','').endswith(self.image.split('@')[-1])):
            raise EvidenceUnavailable('Isolated RAN setup did not complete in pinned incarnation')
        wanted={p['interface']:{'address':p['address'],'arpAnnounce':2} for p in expected}
        if observed!=wanted:
            raise EvidenceUnavailable('Isolated RAN native N3 ARP source policy unavailable')
        return observed

    def _probes(self, opening):
        expected=[dict(interface='n3'+str(i),address=ip) for i,(_,ip) in enumerate(self.n3_addresses.items())]
        policy="import json,socket,fcntl,struct,sys;policies=json.loads(sys.argv[1]);out={};\nfor p in policies:\n name=p['interface'];sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);address=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',name.encode()))[20:24]);sock.close();out[name]={'address':address,'arpAnnounce':int(open('/proc/sys/net/ipv4/conf/'+name+'/arp_announce').read())}\nprint(json.dumps(out))"
        probes=[('process',['sh','-ec',"readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id"]),
            ('config',['cat','/run/gnb/gnb.yaml']),
            ('policy',['python','-c',policy,json.dumps(expected)])]
        if opening:
            probes += [('node',['/ueransim/nr-cli','--dump']),
                *[(name,['/ueransim/nr-cli','@probe-node','--exec',name]) for name in ('status','amf-list','ue-list')],
                ('sctp',['cat','/proc/net/sctp/assocs'])]
        return probes

    def _run(self,namespace,args):
        result=self.driver.runner(['kubectl','--context',self.driver.context,'--namespace',namespace,*args],
            text=True,capture_output=True,timeout=8,check=False)
        if result.returncode or len(result.stdout.encode())>1024*1024:
            raise EvidenceUnavailable('Native isolated RAN read failed')
        return result.stdout

    @staticmethod
    def _pair(first,second):
        # A failed sibling must finish before this phase returns or raises.
        # Existing runner wrappers clip both reads to their common deadline.
        with ThreadPoolExecutor(max_workers=2) as pool:
            left=pool.submit(first);right=pool.submit(second)
            return left.result(),right.result()

    @staticmethod
    def _identity(pod,container,image=None):
        meta=pod.get('metadata',{})
        spec=[c for c in pod.get('spec',{}).get('containers',[]) if c.get('name')==container]
        status=[c for c in pod.get('status',{}).get('containerStatuses',[]) if c.get('name')==container]
        if (not meta.get('uid') or meta.get('deletionTimestamp') or pod.get('status',{}).get('phase')!='Running'
            or len(spec)!=1 or len(status)!=1 or status[0].get('ready') is not True):
            raise EvidenceUnavailable('Native isolated RAN incarnation unavailable')
        current=status[0]
        if (not isinstance(current.get('containerID'),str) or not current['containerID']
                or type(current.get('restartCount')) is not int or current['restartCount']<0):
            raise EvidenceUnavailable('Native isolated RAN container incarnation unavailable')
        if image and (spec[0].get('image')!=image or not current.get('imageID','').endswith(image.split('@')[-1])):
            raise EvidenceUnavailable('Native isolated RAN image changed')
        return {'containerId':current['containerID'],'restartCount':current['restartCount'],
                'imageId':current['imageID'],'podUid':meta['uid'],'podIp':pod['status'].get('podIP'),
                'profile':meta.get('labels',{}).get('cityverse.io/profile'),
                'managedBy':meta.get('labels',{}).get('cityverse.io/managed-by')}

    def __call__(self,unused_pods):
        from .collector import current_ngap_peer
        started=timestamp(self.clock())
        pod_raw,amfs_raw=self._pair(
            lambda:self._run(self.namespace,['get','pod',self.pod_name,'-o','json']),
            lambda:self._run(self.amf_namespace,['get','pods','-l',self.amf_selector,'-o','json']))
        pod=json.loads(pod_raw)
        if (pod.get('metadata',{}).get('name')!=self.pod_name or pod['metadata'].get('namespace')!=self.namespace
                or pod['metadata'].get('labels',{}).get('cityverse.io/managed-by')!='isolated-device-ran'
                or pod['metadata'].get('labels',{}).get('cityverse.io/profile')!=self.driver.device_profile.source_revision):
            raise EvidenceUnavailable('Native isolated RAN profile ownership changed')
        gnb_identity=self._identity(pod,self.container,self.image)
        amfs=json.loads(amfs_raw)['items']
        if len(amfs)!=1:
            raise EvidenceUnavailable('Unique native AMF Pod required')
        amf=amfs[0]
        amf_identity=self._identity(amf,'amf')
        def exec_gnb(*args):
            return self._run(self.namespace,['exec',self.pod_name,'-c',self.container,'--',*args])
        def process(raw):
            rows=raw.splitlines()
            if (len(rows)!=3 or rows[0]!='/ueransim/nr-gnb' or not rows[1].isdigit() or int(rows[1])<=0
                    or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',rows[2])):
                raise EvidenceUnavailable('Native isolated gNB process incarnation unavailable')
            return rows[2]+':'+rows[1]
        opening=read_probe_batch(exec_gnb,self._probes(True))
        process_epoch=process(opening['process'])
        node=opening['node'].strip()
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',node):
            raise EvidenceUnavailable('Unique isolated gNB CLI node required')
        config=yaml.safe_load(opening['config'])
        network_policy=self._network_policy(pod,config,json.loads(opening['policy']))
        wanted={(SLICES[g]['sst'],int(SLICES[g]['sd'],16)) for g in self.groups}
        declared=config.get('slices',[]) if isinstance(config,dict) else []
        if (not isinstance(declared,list) or len(declared)!=len(wanted)
                or any(not isinstance(s,dict) or type(s.get('sst')) is not int or type(s.get('sd')) is not int for s in declared)
                or {(s['sst'],s['sd']) for s in declared}!=wanted
                or any(config.get(k)!=gnb_identity['podIp'] for k in ('linkIp','gtpIp','gtpAdvertiseIp'))):
            raise EvidenceUnavailable('Native isolated RAN sealed slice or endpoint configuration changed')
        status=yaml.safe_load(opening['status'])
        peers=yaml.safe_load(opening['amf-list'])
        if not isinstance(peers,list) or len(peers)!=1 or type(peers[0].get('id')) is not int:
            raise EvidenceUnavailable('Unique native gNB AMF context required')
        info_raw,amf_sctp=self._pair(
            lambda:exec_gnb('/ueransim/nr-cli',node,'--exec','amf-info '+str(peers[0]['id'])),
            lambda:self._run(self.amf_namespace,['exec',amf['metadata']['name'],'-c','amf','--','cat','/proc/net/sctp/assocs']))
        info=yaml.safe_load(info_raw)
        contexts=yaml.safe_load(opening['ue-list'])
        if contexts is None: contexts=[]
        if (not isinstance(contexts,list) or len(contexts)>1000
                or any(not isinstance(r,dict) or type(r.get('ue-id')) is not int or r['ue-id']<=0
                       or any(type(r.get(k)) is not int or r[k]<0 for k in ('ran-ngap-id','amf-ngap-id')) for r in contexts)
                or len({r['ue-id'] for r in contexts})!=len(contexts)):
            raise EvidenceUnavailable('Native gNB UE context list malformed')
        gnb_sctp=opening['sctp']
        peer=current_ngap_peer(config,status,peers,info,gnb_sctp,amf_sctp)
        closing_probes=read_probe_batch(exec_gnb,self._probes(False))
        closing_raw,closing_amf_raw=self._pair(
            lambda:self._run(self.namespace,['get','pod',self.pod_name,'-o','json']),
            lambda:self._run(self.amf_namespace,['get','pod',amf['metadata']['name'],'-o','json']))
        closing=json.loads(closing_raw);closing_amf=json.loads(closing_amf_raw)
        if (self._identity(closing,self.container,self.image)!=gnb_identity or self._identity(closing_amf,'amf')!=amf_identity
                or process(closing_probes['process'])!=process_epoch or yaml.safe_load(closing_probes['config'])!=config
                or self._network_policy(closing,config,json.loads(closing_probes['policy']))!=network_policy):
            raise EvidenceUnavailable('Native isolated RAN incarnation changed')
        proof={'source':'native-ngap-sctp/v2','ngapAssociated':True,'available':True,'observedAt':started,
            'podUid':gnb_identity['podUid'],'gnbIncarnation':gnb_identity,'amfIncarnation':amf_identity,'peer':peer,
            'gnbPodIp':pod['status']['podIP'],'gnbSearchEndpoint':self.driver.device_profile.gnb_search_endpoint,
            'contextScope':'shared-isolated-gnb-all-nssai','sharedGnBId':gnb_identity['podUid'],
            'processEpoch':process_epoch,'sourceRevision':self.driver.device_profile.source_revision,
            'imageDigest':self.driver.device_profile.image_digest,
            'n3NetworkPolicy':network_policy,
            'nativeContextCount':len(contexts),'nativeContexts':contexts}
        # These aliases carry the SAME shared gNB proof. They are not per-slice
        # counts and callers must deduplicate sharedGnBId when displaying totals.
        return {group:dict(proof) for group in self.groups}

    def context_proof(self):
        """One actual shared gNB read, with aliases explicitly deduplicated."""
        rows=self([])
        return {'source':'native-isolated-gnb-contexts/v1','scope':'commissioned-isolated-device-ran',
                'groups':list(self.groups),'imageDigest':self.driver.device_profile.image_digest,
                'sourceRevision':self.driver.device_profile.source_revision,
                'endpoint':self.driver.device_profile.gnb_search_endpoint,'readouts':rows,
                'observedAt':min(r['observedAt'] for r in rows.values())}


def verified_isolated_contexts(proof,profile,groups,*,now=None):
    """Validate the shared native proof without converting it to four gNBs."""
    now=time.time() if now is None else now
    if (not isinstance(proof,dict) or proof.get('source')!='native-isolated-gnb-contexts/v1'
            or proof.get('scope')!='commissioned-isolated-device-ran'
            or proof.get('imageDigest')!=profile.image_digest or proof.get('sourceRevision')!=profile.source_revision
            or proof.get('endpoint')!=profile.gnb_search_endpoint or proof.get('groups')!=list(groups)
            or not isinstance(proof.get('readouts'),dict) or set(proof['readouts'])!=set(groups)):
        raise EvidenceUnavailable('Native isolated context proof profile mismatch')
    shared={};clocks=[]
    for group,row in proof['readouts'].items():
        identity=row.get('gnbIncarnation',{})
        amf=row.get('amfIncarnation',{})
        contexts=row.get('nativeContexts')
        if (row.get('source')!='native-ngap-sctp/v2' or row.get('available') is not True or row.get('ngapAssociated') is not True
                or row.get('contextScope')!='shared-isolated-gnb-all-nssai' or not row.get('sharedGnBId')
                or row['sharedGnBId']!=row.get('podUid') or row['sharedGnBId']!=identity.get('podUid')
                or row.get('gnbSearchEndpoint')!=profile.gnb_search_endpoint
                or row.get('sourceRevision')!=profile.source_revision or row.get('imageDigest')!=profile.image_digest
                or not identity.get('containerId') or identity.get('restartCount')!=0
                or not str(identity.get('imageId','')).endswith(profile.image_digest)
                or not amf.get('podUid') or not amf.get('containerId')
                or row.get('peer',{}).get('ngSetupState')!='CONNECTED'
                or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[1-9][0-9]*',str(row.get('processEpoch','')))
                or not isinstance(contexts,list) or type(row.get('nativeContextCount')) is not int
                or row['nativeContextCount']!=len(contexts)
                or not -1<=now-seconds(row.get('observedAt'))<=15):
            raise EvidenceUnavailable('Native isolated context identity or freshness unavailable')
        try:
            ipaddress.IPv4Address(row['gnbPodIp'])
        except (ValueError,KeyError):
            raise EvidenceUnavailable('Native isolated context endpoint unavailable') from None
        if row['gnbPodIp']!=identity.get('podIp'):
            raise EvidenceUnavailable('Native isolated context endpoint changed')
        canonical=json.dumps(row,sort_keys=True,separators=(',',':'))
        if row['sharedGnBId'] in shared and shared[row['sharedGnBId']]['canonical']!=canonical:
            raise EvidenceUnavailable('Native shared gNB aliases disagree')
        shared[row['sharedGnBId']]={'canonical':canonical,'ueCount':len(contexts),'processEpoch':row['processEpoch']}
        clocks.append(seconds(row['observedAt']))
    if len(shared)!=1:
        raise EvidenceUnavailable('Exactly one commissioned isolated gNB required')
    return {'uniqueGnBCount':len(shared),'ueCount':sum(r['ueCount'] for r in shared.values()),
            'observed':clocks,'sharedGnBs':{key:{k:v for k,v in row.items() if k!='canonical'} for key,row in shared.items()}}
