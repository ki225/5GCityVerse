"""Read-only, explicitly commissioned same-node CRI process incarnation proof.

No image tools, shell, CRI exec, model-selected path or runtime mutation is used.
Raw CRI inspect output (which can contain environment secrets) never leaves the
bounded parser; errors deliberately contain no command output.
"""
from dataclasses import dataclass
from http.client import HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
from urllib.parse import quote
from uuid import UUID

from .owned_native_teardown import TeardownBlocked


@dataclass(frozen=True)
class HostCriBinding:
    node_name:str
    node_uid:str
    runtime_version:str
    runtime_endpoint:str='unix:///run/containerd/containerd.sock'
    crictl_path:str='/usr/bin/crictl'
    executable:str='/free5gc/smf'

    def __post_init__(self):
        if (any(not isinstance(v,str) for v in (self.node_name,self.node_uid,self.runtime_version,self.runtime_endpoint,self.crictl_path,self.executable))
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',self.node_name)
                or str(UUID(self.node_uid))!=self.node_uid
                or not re.fullmatch(r'containerd://[0-9]+\.[0-9]+\.[0-9]+',self.runtime_version)
                or self.runtime_endpoint!='unix:///run/containerd/containerd.sock'
                or self.crictl_path!='/usr/bin/crictl' or self.executable!='/free5gc/smf'):
            raise ValueError('Explicit allowlisted same-node SMF CRI binding required')


@dataclass(frozen=True)
class HostCriProcessProof:
    identity:str
    opening_observed_at:float
    closing_observed_at:float
    source:str='native-host-cri-process/v1'

    def __post_init__(self):
        if (self.source!='native-host-cri-process/v1' or not isinstance(self.identity,str)
                or any(type(t) not in {int,float} or not math.isfinite(t) for t in (self.opening_observed_at,self.closing_observed_at))
                or self.closing_observed_at<self.opening_observed_at):
            raise TeardownBlocked('Original native process opening/closing clock proof required')


def bounded_cri_inspect(binding,container_id,*,deadline):
    """One bounded fixed-argv inspect; kill/reap only the child we just created."""
    if os.name!='posix' or not re.fullmatch('[0-9a-f]{64}',container_id):
        raise TeardownBlocked('Same-node CRI process reader is unavailable')
    tool=Path(binding.crictl_path)
    if (tool.is_symlink() or not tool.is_file() or tool.stat().st_uid!=0
            or tool.stat().st_mode&0o022 or not os.access(tool,os.X_OK)):
        raise TeardownBlocked('Commissioned CRI executable identity is unsafe')
    runtime=Path('/run/containerd/containerd.sock')
    if (runtime.is_symlink() or not runtime.exists() or not stat.S_ISSOCK(runtime.stat().st_mode)
            or runtime.stat().st_uid!=0 or runtime.stat().st_mode&0o002):
        raise TeardownBlocked('Commissioned native runtime socket identity is unsafe')
    child=None
    try:
        child=subprocess.Popen([binding.crictl_path,'--runtime-endpoint',binding.runtime_endpoint,
            'inspect','--output','json',container_id],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,close_fds=True,env={'PATH':os.defpath})
        raw=bytearray()
        with selectors.DefaultSelector() as poll:
            os.set_blocking(child.stdout.fileno(),False)
            poll.register(child.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError()
                ready=poll.select(remaining)
                if not ready:raise TimeoutError()
                block=os.read(child.stdout.fileno(),min(16384,262145-len(raw)))
                if not block:break
                raw.extend(block)
                if len(raw)>262144:raise ValueError()
        remaining=deadline-time.monotonic()
        if remaining<=0 or child.wait(timeout=remaining)!=0:raise ValueError()
        result=json.loads(raw)
        if not isinstance(result,dict):raise ValueError()
        return result
    except Exception:
        raise TeardownBlocked('Bounded exact-container CRI inspect unavailable') from None
    finally:
        if child is not None:
            if child.poll() is None:child.kill()
            try:child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise TeardownBlocked('Owned read-only CRI child termination is unconfirmed') from None
            finally:
                if child.stdout is not None:child.stdout.close()


def host_process(pid):
    """Read only fixed /proc fields; no commandline, environment or auth files."""
    if os.name!='posix' or type(pid) is not int or not 1<pid<=4194304:
        raise TeardownBlocked('Exact host process PID required')
    def read(path,maximum):
        with Path(path).open('rb') as stream:raw=stream.read(maximum+1)
        if len(raw)>maximum:raise ValueError()
        return raw.decode('utf-8')
    try:
        base=f'/proc/{pid}'
        stat=read(base+'/stat',8192)
        if int(stat.split(' ',1)[0])!=pid:raise ValueError()
        tail=stat.rsplit(')',1)[1].split()
        if tail[0]=='Z' or not tail[19].isdigit():raise ValueError()
        status=read(base+'/status',65536)
        matches=[line.split()[1:] for line in status.splitlines() if line.startswith('NSpid:')]
        if len(matches)!=1:raise ValueError()
        nspid=tuple(int(v) for v in matches[0])
        boot=read('/proc/sys/kernel/random/boot_id',128).strip()
        if str(UUID(boot))!=boot:raise ValueError()
        return dict(host_pid=pid,namespace_pids=nspid,kernel_boot_id=boot,start_ticks=tail[19],
            executable=os.readlink(base+'/exe'),pid_namespace_inode=os.stat(base+'/ns/pid').st_ino)
    except Exception:
        raise TeardownBlocked('Exact host process incarnation unavailable') from None


class FixedNodeMetadataRead:
    """One fixed authenticated Node GET using the existing kubeconfig TLS identity."""
    def __init__(self,metadata_reader,binding):
        if metadata_reader is None:
            raise ValueError('Existing explicit-context authenticated Kubernetes reader required')
        self.reader,self.binding=metadata_reader,binding

    def __call__(self,*,deadline):
        connection=None
        try:
            remaining=deadline-time.monotonic()
            if not 0<remaining<=10:raise TimeoutError()
            reader=self.reader
            connection=HTTPSConnection(reader.host,reader.port,context=reader.context,timeout=remaining)
            connection.request('GET','/api/v1/nodes/'+quote(self.binding.node_name,safe=''),headers={'Accept':'application/json'})
            transport=connection.sock
            if transport is not None:transport.settimeout(max(.001,deadline-time.monotonic()))
            response=connection.getresponse()
            if response.status!=200:raise ValueError()
            raw=bytearray()
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError()
                if transport is not None:transport.settimeout(remaining)
                block=response.read1(min(16384,262145-len(raw)))
                if not block:break
                raw.extend(block)
                if len(raw)>262144:raise ValueError()
            if time.monotonic()>deadline:raise TimeoutError()
            return json.loads(raw)
        except Exception:
            raise TeardownBlocked('Commissioned native Node identity unavailable') from None
        finally:
            if connection is not None:connection.close()


class HostCriProcessIdentityReader:
    """Exact Pod/runtime/Node/process opening and closing fences, not a fallback."""
    def __init__(self,binding,node_reader,*,inspect=bounded_cri_inspect,process=host_process):
        if type(binding) is not HostCriBinding:raise ValueError('Typed same-node CRI binding required')
        self.binding,self.node_reader,self.inspect,self.process=binding,node_reader,inspect,process

    def _node(self,*,deadline):
        node=self.node_reader(deadline=deadline)
        metadata=node.get('metadata',{});info=node.get('status',{}).get('nodeInfo',{})
        b=self.binding
        boot=info.get('bootID')
        if (metadata.get('name')!=b.node_name or metadata.get('uid')!=b.node_uid
                or info.get('containerRuntimeVersion')!=b.runtime_version or not isinstance(boot,str)
                or str(UUID(boot))!=boot):
            raise TeardownBlocked('Native Node UID/runtime/boot differs from commissioned binding')
        return boot

    def _container(self,raw,pod,selector,container_id):
        status=raw.get('status',{});info=raw.get('info',{});labels=status.get('labels',{})
        if isinstance(info,str):
            if len(info)>262144:raise TeardownBlocked('Native CRI metadata exceeds bound')
            info=json.loads(info)
        meta=pod['metadata'];pid=info.get('pid')
        if (status.get('id')!=container_id or status.get('state')!='CONTAINER_RUNNING'
                or status.get('metadata',{}).get('name')!=selector.container_name
                or labels.get('io.kubernetes.pod.uid')!=meta['uid']
                or labels.get('io.kubernetes.pod.namespace')!=meta['namespace']
                or labels.get('io.kubernetes.pod.name')!=meta['name']
                or labels.get('io.kubernetes.container.name')!=selector.container_name
                or type(pid) is not int or not 1<pid<=4194304):
            raise TeardownBlocked('CRI container/Pod/PID mapping differs from exact native owner')
        return pid

    def read(self,pod,selector,*,deadline):
        opening_at=time.time()
        if (type(deadline) not in {int,float} or not math.isfinite(deadline) or not 0<deadline-time.monotonic()<=8
                or selector.role!='smf' or selector.container_name!='smf' or selector.process_pid!=1
                or pod.get('spec',{}).get('nodeName')!=self.binding.node_name):
            raise TeardownBlocked('Host CRI reader is commissioned only for the fixed-node SMF PID1')
        statuses=[s for s in pod.get('status',{}).get('containerStatuses',[]) if s.get('name')==selector.container_name]
        if len(statuses)!=1:raise TeardownBlocked('Exact SMF runtime container unavailable')
        status=statuses[0];identity=status.get('containerID','')
        if (not re.fullmatch('containerd://[0-9a-f]{64}',identity) or status.get('ready') is not True
                or not status.get('state',{}).get('running') or type(status.get('restartCount')) is not int
                or status['restartCount']<0 or pod.get('metadata',{}).get('deletionTimestamp')):
            raise TeardownBlocked('Exact running SMF container incarnation unavailable')
        container_id=identity.split('://',1)[1]
        try:
            boot=self._node(deadline=deadline)
            pid=self._container(self.inspect(self.binding,container_id,deadline=deadline),pod,selector,container_id)
            first=self.process(pid)
            if (first.get('host_pid')!=pid or tuple(first.get('namespace_pids',()))!=(pid,1)
                    or first.get('kernel_boot_id')!=boot or first.get('executable')!=self.binding.executable
                    or not isinstance(first.get('start_ticks'),str) or not first['start_ticks'].isdigit()
                    or type(first.get('pid_namespace_inode')) is not int or first['pid_namespace_inode']<=0):
                raise TeardownBlocked('SMF host process does not match its container PID namespace/incarnation')
            closing=self._container(self.inspect(self.binding,container_id,deadline=deadline),pod,selector,container_id)
            second=self.process(closing)
            if closing!=pid or second!=first or self._node(deadline=deadline)!=boot or time.monotonic()>deadline:
                raise TeardownBlocked('Native Node/runtime/process incarnation changed during observation')
            # No raw inspect, environment, commandline or credentials escape.
            identity=json.dumps(['native-host-cri-process/v1',self.binding.node_uid,identity,status['restartCount'],
                boot,pid,first['start_ticks'],first['pid_namespace_inode'],first['executable']],separators=(',',':'))
            return HostCriProcessProof(identity,opening_at,time.time())
        except TeardownBlocked:raise
        except Exception:
            raise TeardownBlocked('Exact commissioned native process proof unavailable') from None
