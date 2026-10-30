"""Whole-UPF Linux netdev RX load; never a service-payload throughput estimate."""
from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
import json
import math
import re
import time

from .actuators import ActuationBlocked
from .observations import Metric


def _name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',value):
        raise ActuationBlocked('Invalid commissioned native resource name')
    return value


class NativeUpfKernelEpochReader:
    """Bind fixed evidence Service -> exactly one Pod UID -> native netns/epoch."""
    def __init__(self,client,targets,*,clock=time.time,maximum_age_seconds=15):
        self.client,self.targets,self.clock=client,dict(targets),clock
        self.maximum_age=maximum_age_seconds

    def __call__(self,target,pod):
        spec=self.targets[target];service=_name(spec['evidence_service'])
        actual=self.client.call('get','service',service,'-o','json')
        selector=actual.get('spec',{}).get('selector',{})
        if (actual.get('metadata',{}).get('namespace')!=self.client.namespace or not selector
            or not all(pod['metadata'].get('labels',{}).get(k)==v for k,v in selector.items())
            or not any(p.get('port')==9081 for p in actual['spec'].get('ports',()))):
            raise ActuationBlocked('Native evidence service does not identify target Pod')
        endpoints=self.client.call('get','endpoints',service,'-o','json')
        addresses=[]
        for subset in endpoints.get('subsets',()):
            if subset.get('notReadyAddresses'):
                raise ActuationBlocked('Native evidence endpoint is not ready')
            addresses.extend(subset.get('addresses',()))
        if (len(addresses)!=1 or addresses[0].get('targetRef',{}).get('uid')!=pod['metadata']['uid']
            or addresses[0].get('ip')!=pod.get('status',{}).get('podIP')):
            raise ActuationBlocked('Native evidence endpoint UID differs from counter Pod')
        # kubectl's request-timeout adds a query that the fixed legacy evidence
        # handler does not accept. The process timeout still bounds this IO.
        path=f'/api/v1/namespaces/{self.client.namespace}/services/http:{service}:9081/proxy/v1/upf-state'
        result=self.client.runner(['kubectl','--context',self.client.context,'--namespace',self.client.namespace,
            'get','--raw',path],capture_output=True,text=True,timeout=self.client.timeout+2,check=False)
        if result.returncode or len(result.stdout.encode())>8*1024*1024:
            raise ActuationBlocked('Bounded native kernel response unavailable')
        try:dump=json.loads(result.stdout)
        except ValueError:raise ActuationBlocked('Native kernel response invalid') from None
        try:
            at=datetime.fromisoformat(dump['observedAt'].replace('Z','+00:00'))
            if at.tzinfo is None:raise ValueError()
            at=at.timestamp()
        except (KeyError,ValueError,TypeError):
            raise ActuationBlocked('Native kernel observation clock is unavailable') from None
        if (dump.get('source')!='gtp5g-netlink' or dump.get('schemaVersion')!='cityverse.upf/v1'
            or dump.get('available') is not True or dump.get('upf')!=spec['native_upf_name']
            or not dump.get('epoch') or not re.fullmatch(r'net:\[[0-9]+\]',dump.get('netns',''))
            or not 0<=self.clock()-at<=self.maximum_age):
            raise ActuationBlocked('Fresh native UPF kernel identity is unavailable')
        return {'source':'gtp5g-netlink','pod_uid':pod['metadata']['uid'],'upf_epoch':dump['epoch'],
            'netns':dump['netns'],'observed_at':at,'evidence_service_uid':actual['metadata']['uid']}


class NativeNfLoadReader:
    """Two actual counter samples, fenced by Pod/container/config/kernel/interface.

    RX counts include native netdev framing and encapsulation as Linux reports
    them, plus unrelated traffic. They exclude eth0 and do not count TX again.
    Capacity calibration must use this same metric/source/interface definition.
    """
    metric_name='nf_ingress_wire_bps'
    source='native-upf-interface-rx'

    def __init__(self,client,targets,*,epoch_reader=None,clock=time.time,
                 minimum_window_seconds=.1,maximum_window_seconds=30,maximum_sample_seconds=10,
                 maximum_parallel_targets=4):
        self.client,self.targets,self.clock=client,dict(targets),clock
        self.minimum_window,self.maximum_window=minimum_window_seconds,maximum_window_seconds
        self.maximum_sample=maximum_sample_seconds
        if type(maximum_parallel_targets) is not int or not 1<=maximum_parallel_targets<=4:
            raise ActuationBlocked('Native read concurrency must be bounded to 1..4 targets')
        self.parallel_targets=maximum_parallel_targets
        self.previous={};self.lock=RLock()
        if not self.targets or len(self.targets)>64 or not 0<self.minimum_window<self.maximum_window:
            raise ActuationBlocked('Bounded native load scope required')
        physical_targets=[(spec['pod'],spec['container']) for spec in self.targets.values()]
        if len(set(physical_targets))!=len(physical_targets):
            raise ActuationBlocked('A native NF cannot be counted under multiple load targets')
        for target,spec in self.targets.items():
            _name(target);_name(spec['pod']);_name(spec['container'])
            interfaces=(spec['n3_interface'],spec['n6_interface'])
            if (len(set(interfaces))!=2 or any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}',i) or i in {'eth0','lo'} for i in interfaces)
                or not spec.get('n3_network') or not spec.get('n6_network')):
                raise ActuationBlocked('Commissioned distinct N3/N6 data interfaces required')
        self.epoch_reader=epoch_reader or NativeUpfKernelEpochReader(client,self.targets,clock=clock)

    def _pod(self,spec):
        pod=self.client.call('get','pod',spec['pod'],'-o','json')
        meta=pod.get('metadata',{})
        states=[s for s in pod.get('status',{}).get('containerStatuses',()) if s.get('name')==spec['container']]
        containers=[s for s in pod.get('spec',{}).get('containers',()) if s.get('name')==spec['container']]
        if (meta.get('name')!=spec['pod'] or meta.get('namespace')!=self.client.namespace or not meta.get('uid') or not meta.get('resourceVersion') or meta.get('deletionTimestamp')
            or len(states)!=1 or len(containers)!=1 or states[0].get('ready') is not True
            or 'running' not in states[0].get('state',{}) or not states[0].get('containerID')
            or type(states[0].get('restartCount')) is not int or states[0]['restartCount']<0):
            raise ActuationBlocked('Exact stable running NF container required')
        networks=json.loads(meta.get('annotations',{}).get('k8s.v1.cni.cncf.io/network-status','[]'))
        for role in ('n3','n6'):
            matches=[n for n in networks if n.get('interface')==spec[role+'_interface'] and n.get('name')==spec[role+'_network']]
            if len(matches)!=1 or not matches[0].get('ips'):
                raise ActuationBlocked('Commissioned data-interface role not proven by native Pod network metadata')
        identity={'pod_uid':meta['uid'],'resource_version':meta['resourceVersion'],'container_id':states[0]['containerID'],
            'restart_count':states[0]['restartCount'],'resources':containers[0].get('resources',{}),
            'image':containers[0].get('image'),'image_id':states[0].get('imageID')}
        return pod,identity

    def _exec(self,spec,*args):
        command=['kubectl','--context',self.client.context,'--namespace',self.client.namespace,
            f'--request-timeout={self.client.timeout}s','exec',spec['pod'],'-c',spec['container'],'--',*args]
        result=self.client.runner(command,capture_output=True,text=True,timeout=self.client.timeout+2,check=False)
        if result.returncode or len(result.stdout.encode())>4096:
            raise ActuationBlocked('Bounded native NF counter read failed')
        return result.stdout.strip()

    def sample(self,target):
        spec=self.targets[target];started=self.clock()
        pod,identity=self._pod(spec)
        epoch=self.epoch_reader(target,pod)
        netns=self._exec(spec,'readlink','/proc/self/ns/net')
        paths=[]
        for role in ('n3','n6'):
            base='/sys/class/net/'+spec[role+'_interface']
            paths.extend((base+'/ifindex',base+'/address',base+'/statistics/rx_bytes'))
        read_started=self.clock();raw=self._exec(spec,'cat',*paths);observed=self.clock()
        values=raw.splitlines()
        if len(values)!=6:raise ActuationBlocked('Native counter fields missing')
        interfaces={}
        for offset,role in ((0,'n3'),(3,'n6')):
            index,mac,counter=values[offset:offset+3]
            if not index.isascii() or not index.isdecimal() or not counter.isascii() or not counter.isdecimal():
                raise ActuationBlocked('Native interface counter is invalid')
            if not 0<int(index)<2**31 or not 0<=int(counter)<2**64 or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',mac):
                raise ActuationBlocked('Native interface identity is invalid')
            interfaces[role]={'name':spec[role+'_interface'],'ifindex':int(index),'mac':mac.lower(),'rx_bytes':int(counter)}
        after,current=self._pod(spec);last_epoch=self.epoch_reader(target,after)
        for proof in (epoch,last_epoch):
            at=proof.get('observed_at')
            if (proof.get('source')!='gtp5g-netlink' or proof.get('pod_uid')!=identity['pod_uid']
                or proof.get('netns')!=netns or not proof.get('upf_epoch') or not proof.get('evidence_service_uid')
                or type(at) not in (int,float) or not math.isfinite(at) or not 0<=self.clock()-at<=15):
                raise ActuationBlocked('Native kernel epoch is not bound to counter namespace')
        if (identity!=current or any(epoch[k]!=last_epoch[k] for k in ('upf_epoch','netns','evidence_service_uid'))
            or not 0<=self.clock()-started<=self.maximum_sample):
            raise ActuationBlocked('NF generation or state changed during counter read')
        return {'target_id':target,**identity,'upf_epoch':epoch['upf_epoch'],'netns':netns,
            'evidence_service_uid':epoch['evidence_service_uid'],'interfaces':interfaces,'observed_at':observed,
            'read_started_at':read_started,'sampling_started_at':started,'sampling_finished_at':self.clock(),
            'source':self.source,'definition':'sum Linux N3+N6 rx_bytes deltas; all netdev traffic; not service payload'}

    def collect(self):
        with self.lock:
            metrics=[];measurements=[];observations=[];missing=[]
            def read(target):
                try:
                    return self.sample(target)
                except Exception as error:
                    return error
            if len(self.targets)>1 and self.parallel_targets>1:
                # Independent NFs retain their own real read windows and fences.
                # Do not serialize four Kubernetes/exec pipelines before planning.
                with ThreadPoolExecutor(max_workers=min(self.parallel_targets,len(self.targets)),
                                        thread_name_prefix='native-nf-read') as pool:
                    samples=dict(zip(self.targets,pool.map(read,self.targets)))
            else:
                samples={target:read(target) for target in self.targets}
            for target in self.targets:
                try:
                    current=samples[target]
                    if isinstance(current,Exception):
                        raise current
                    before=self.previous.get(target);self.previous[target]=current
                    def state(row):
                        fields={k:v for k,v in row.items() if k in ('pod_uid','resource_version','container_id','restart_count','resources','image','image_id','upf_epoch','netns','evidence_service_uid')}
                        fields['interfaces']={role:{k:v for k,v in value.items() if k!='rx_bytes'} for role,value in row['interfaces'].items()}
                        return fields
                    if before is None or state(before)!=state(current):
                        current['status']='awaiting_second_sample';observations.append(current);missing.append(target+':native_nf_load:awaiting_second_sample');continue
                    window=current['observed_at']-before['observed_at']
                    deltas={role:current['interfaces'][role]['rx_bytes']-before['interfaces'][role]['rx_bytes'] for role in ('n3','n6')}
                    if not self.minimum_window<=window<=self.maximum_window or any(d<0 for d in deltas.values()):
                        current['status']='counter_reset_or_window_invalid';observations.append(current);missing.append(target+':native_nf_load:counter_reset_or_window_invalid');continue
                    value=sum(deltas.values())*8/window
                    if not math.isfinite(value):raise ActuationBlocked('Native counter rate is invalid')
                    resource=target+':nf-ingress-wire'
                    metrics.append(Metric(self.metric_name,value,'bps',self.source,current['observed_at'],window,resource,'valid',scope='resource'))
                    measurements.append((target,(resource,)))
                    current.update(status='measured',window_started_at=before['observed_at'],window_seconds=window,
                        rx_byte_deltas=deltas,ingress_wire_bps=value,read_uncertainty_seconds=(current['observed_at']-current['read_started_at'])+(before['observed_at']-before['read_started_at']))
                    observations.append(current)
                except Exception:
                    self.previous.pop(target,None);missing.append(target+':native_nf_load:unavailable')
            return {'metrics':tuple(metrics),'target_measurement_ids':tuple(measurements),'observations':observations,'missing':tuple(missing)}
