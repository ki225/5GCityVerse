"""Native UPF lifecycle server. Pinned manifests are code/configuration, never model output.

Pod creation is installation work, not proof of a service effect. The SMF and the
gtp5g collector must independently confirm identity, PFCP and session state.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, asynccontextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import Lock, Thread
import time
import uuid
from urllib.request import Request, build_opener, ProxyHandler

from .actuators import ActuationBlocked
from .domain import timestamp
from .resource_adapters import (KubectlResourceClient, _NoRedirect, parse_cpu_millis,
                                parse_memory_bytes, _token_file_reader)


OWNER = 'cityverse.io/upf-owner'
OPERATION = 'cityverse.io/upf-operation'
NAME = re.compile(r'[a-z0-9][a-z0-9.-]{0,62}')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _fresh(value, now):
    try:
        return 0 <= now - timestamp(value) <= 15
    except (ValueError, TypeError):
        return False


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ActuationBlocked('Exact configured resource name required')
    return value


class NativeSmfUpfClient:
    """Fixed-origin bounded client for the authenticated native SMF extension."""
    def __init__(self, origin, *, token_reader, timeout=5, opener=None):
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {'', '/'} or parsed.query
                or parsed.fragment or not 0 < timeout <= 10):
            raise ValueError('Fixed native SMF origin and bounded timeout required')
        self.origin, self.token_reader, self.timeout = origin.rstrip('/'), token_reader, timeout
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def _call(self, route, value=None):
        request = Request(self.origin + '/nsmf-oam/v1/' + route,
            data=None if value is None else json.dumps(value, allow_nan=False).encode(),
            method='GET' if value is None else 'POST',
            headers={'X-SMF-QER-Actuator-Token':self.token_reader(), 'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_048_577)
                if response.status not in {200,201} or len(raw) > 1_048_576:
                    raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception:
            raise ActuationBlocked('Native SMF readback required') from None

    def capabilities(self):
        return self._call('upf-control-capability')

    def active_inventory(self):
        return self._call('session-active-inventory')

    def read(self, target):
        return self._call('upf-control/read', {'targetId':target})

    def _authority(self, target, authority):
        return dict(targetId=target, ownerId=authority['owner_id'],
                    operationId=authority['operation_id'], expectedVersion=authority['expected_version'])

    def register(self, target, **authority):
        return self._call('upf-control/register', self._authority(target, authority))

    def selection(self, target, *, accepting, **authority):
        return self._call('upf-control/selection', self._authority(target, authority) | {'accepting':accepting})

    def unregister(self, target, **authority):
        return self._call('upf-control/unregister', self._authority(target, authority))

    def admission_api(self):
        from .pending_admission import FixedOriginSmfAdmissionApi
        return FixedOriginSmfAdmissionApi(self.origin,token_reader=self.token_reader,
                                         timeout=self.timeout,opener=self.opener)


class KubectlUpfReadback:
    """Read the existing native gtp5g exporter through an exact-UID Pod proxy.

The optional path reader must supply independent receiver/probe evidence. Kernel
rules and Pod readiness alone cannot stand in for that evidence.
"""
    def __init__(self, client, *, path_reader=None, probe_target=None, clock=time.time,pinned_pods=None):
        self.client, self.path_reader, self.probe_target, self.clock = client, path_reader, probe_target, clock
        self.pinned_pods=deepcopy(pinned_pods or {})

    def _pod_name(self,target,uid=None):
        pin=self.pinned_pods.get(target)
        if pin is None:
            return _name(target)
        if uid is not None and pin['pod_uid']!=uid:
            raise ActuationBlocked('Commissioned existing Pod UID changed')
        return _name(pin['pod_name'])

    def _raw_state(self,route):
        if not isinstance(self.client,KubectlResourceClient):
            return self.client.call('get','--raw',route)
        # kubectl appends --request-timeout as ?timeout=... to raw proxy URLs.
        # The native exporter deliberately accepts exact routes; bound the
        # subprocess itself without changing the upstream request target.
        argv=['kubectl','--context',self.client.context,'--namespace',self.client.namespace,'get','--raw',route]
        try:
            result=self.client.runner(argv,capture_output=True,text=True,timeout=min(self.client.timeout,5),check=False)
            if result.returncode or len(result.stdout)>4*1024*1024 or len(result.stderr)>4096:
                raise ValueError()
            value=json.loads(result.stdout)
            if not isinstance(value,dict):
                raise ValueError()
            return value
        except Exception:
            raise ActuationBlocked('Bounded native UPF raw read unavailable') from None

    def kernel(self, target, uid):
        target = _name(target)
        pod_name=self._pod_name(target,uid)
        before = self.client.call('get', 'pod', pod_name, '-o', 'json')
        if before['metadata']['uid'] != uid:
            raise ActuationBlocked('UPF Pod identity changed')
        route = f'/api/v1/namespaces/{self.client.namespace}/pods/{pod_name}:9081/proxy/v1/upf-state'
        value = self._raw_state(route)
        after = self.client.call('get', 'pod', pod_name, '-o', 'json')
        if (after['metadata']['uid'] != uid or value.get('schemaVersion') != 'cityverse.upf/v1'
                or value.get('source') != 'gtp5g-netlink' or value.get('available') is not True
                or value.get('upf') != target or value.get('errors') != [] or value.get('residuals') != []
                or not _fresh(value.get('observedAt'), self.clock()) or not value.get('epoch')
                or not isinstance(value.get('sessions'), list) or len(value['sessions']) > 4096):
            raise ActuationBlocked('Fresh complete native UPF kernel evidence required')
        for row in value['sessions']:
            if (row.get('evidence') != 'kernel-installed-pdr-far'
                    or row.get('upf') != target or not str(row.get('pfcpSeid', '')).isdigit()
                    or not 0 < int(row['pfcpSeid']) < 2**64):
                raise ActuationBlocked('Native PFCP session evidence incomplete')
        return value

    def capabilities(self):
        if self.path_reader is None or self.probe_target is None:
            return {}
        pod_name=self._pod_name(self.probe_target)
        pod = self.client.call('get', 'pod', pod_name, '-o', 'json')
        kernel = self.kernel(self.probe_target, pod['metadata']['uid'])
        proof = self.path_reader.read(pod_name, pod['metadata']['uid'],kernel['epoch'])
        if (proof.get('verified') is True and proof.get('source') in {'native-receiver-path/v1','native-upf-path-probe/v1'}
                and proof.get('target_id') == pod_name and proof.get('pod_uid') == pod['metadata']['uid']
                and proof.get('upf_epoch') == kernel['epoch']
                and _fresh(proof.get('observed_at'), self.clock())):
            return dict(verified=True, kernel_inventory=True, path_probe=True,
                        receiver_counters=proof.get('receiver_counters') is True,
                        observed_at=self.clock())
        return {}

    def read(self, target, uid):
        try:
            kernel = self.kernel(target, uid)
        except Exception:
            return dict(kernel={},path={},diagnostics=['kernel_readback_missing'])
        pod_name=self._pod_name(target,uid)
        try:
            path = {} if self.path_reader is None else self.path_reader.read(pod_name, uid, kernel['epoch'])
        except Exception as exc:
            reason='path_probe_permission_unavailable' if type(exc) is ActuationBlocked and str(exc)=='Native UPF ICMP permission unavailable' else 'path_probe_failed'
            return dict(kernel=kernel,path={},diagnostics=[reason])
        valid = (path.get('verified') is True and path.get('source') in {'native-receiver-path/v1','native-upf-path-probe/v1'} and path.get('target_id') == pod_name
            and path.get('pod_uid') == uid and path.get('upf_epoch') == kernel['epoch']
            and _fresh(path.get('observed_at'), self.clock()))
        if valid and path.get('verified') is True:
            try:
                closing=self.kernel(target,uid)
                if closing['epoch']!=kernel['epoch']:
                    raise ValueError()
            except Exception:
                return dict(kernel={},path={},diagnostics=['kernel_epoch_changed'])
        return dict(kernel=kernel, path=path if valid else {},diagnostics=[] if valid else ['path_probe_missing'])


class KubectlUpfPathProbe:
    """Actual fixed-argv interface and ICMP checks in the exact new UPF network namespace.

These checks establish path reachability only. They never produce session traffic
counters or service-effect success. All peers/interfaces are operator-pinned.
"""
    def __init__(self, client, targets, *, clock=time.time):
        self.client,self.targets,self.clock = client,deepcopy(targets),clock
        for target,spec in self.targets.items():
            _name(target)
            _name(spec['container'])
            if spec.get('method','ip-ping') not in {'ip-ping','python-icmp'}:
                raise ValueError('Explicit supported native path probe method required')
            if set(spec['interfaces']) != {'n3','n4','n6'} or set(spec['peers']) != {'n3','n6'}:
                raise ValueError('Pin N3/N4/N6 addresses and N3/N6 peer probes')
            for address in (*spec['interfaces'].values(),*spec['peers'].values()):
                ipaddress.IPv4Address(address)

    def _exec(self,target,container,*command):
        argv=['kubectl','--context',self.client.context,'--namespace',self.client.namespace,
            '--request-timeout=5s','exec',target,'-c',container,'--',*command]
        try:
            result=self.client.runner(argv,capture_output=True,text=True,timeout=7,check=False)
            if result.returncode and any(code in result.stderr for code in
                    ('PermissionError: [Errno 1]', 'PermissionError: [Errno 13]')):
                raise ActuationBlocked('Native UPF ICMP permission unavailable')
            if result.returncode or len(result.stdout)>16384 or len(result.stderr)>4096:
                raise ValueError()
            return result.stdout
        except ActuationBlocked:
            raise
        except Exception:
            raise ActuationBlocked('Bounded native UPF path probe unavailable') from None

    def read(self,target,uid,epoch):
        spec=self.targets.get(target)
        if spec is None:
            raise ActuationBlocked('Native UPF path probe target is not configured')
        before=self.client.call('get','pod',target,'-o','json')
        if before['metadata']['uid']!=uid or before.get('spec',{}).get('hostNetwork') is True:
            raise ActuationBlocked('Native path Pod UID changed')
        def identity(pod):
            statuses=[c for c in pod.get('status',{}).get('containerStatuses',()) if c.get('name')==spec['container']]
            if (len(statuses)!=1 or type(statuses[0].get('restartCount')) is not int
                    or statuses[0]['restartCount']<0 or not statuses[0].get('containerID')):
                raise ActuationBlocked('Native selected-container restart identity is unavailable')
            return pod['metadata']['uid'],statuses[0]['restartCount'],statuses[0]['containerID']
        before_identity=identity(before)
        if spec.get('method','ip-ping')=='python-icmp':
            # Existing evidence sidecars have Python but no iputils binaries.
            # This fixed program reads real interface addresses and requires an
            # exact nonce-bearing ICMP reply on each pinned network interface.
            program="""import socket,fcntl,struct,json,sys,os
spec=json.loads(sys.argv[1])
for interface,address in spec['interfaces'].items():
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
  actual=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',interface.encode()))[20:24])
  if actual!=address: raise RuntimeError('interface_address_mismatch')
for interface,peer in spec['peers'].items():
 payload=b'cityverse-path-v1'+os.urandom(16)
 packet=struct.pack('!BBHHH',8,0,0,0x4356,1)+payload
 checksum_packet=packet+(b'\\0' if len(packet)%2 else b'')
 words=struct.unpack('!%dH'%(len(checksum_packet)//2),checksum_packet)
 total=sum(words); total=(total&65535)+(total>>16); total=(total&65535)+(total>>16)
 packet=struct.pack('!BBHHH',8,0,(~total)&65535,0x4356,1)+payload
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_ICMP) as sock:
  sock.settimeout(1)
  sock.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,(interface+'\\0').encode())
  sock.bind((spec['interfaces'][interface],0))
  sock.sendto(packet,(peer,0))
  identifier=sock.getsockname()[1]
  reply,address=sock.recvfrom(256)
  if address[0]!=peer or len(reply)!=len(packet) or reply[0:2]!=b'\\0\\0' or reply[8:]!=payload:
   raise RuntimeError('icmp_reply_mismatch')
  if struct.unpack('!HH',reply[4:8])!=(identifier,1): raise RuntimeError('icmp_identifier_mismatch')
print('native-interface-icmp-verified')
"""
            output=self._exec(target,spec['container'],'python','-c',program,json.dumps({k:spec[k] for k in ('interfaces','peers')}))
            if output.strip()!='native-interface-icmp-verified':
                raise ActuationBlocked('Native UPF ICMP path proof missing')
        else:
            for interface,address in spec['interfaces'].items():
                rows=json.loads(self._exec(target,spec['container'],'ip','-j','-4','addr','show','dev',interface))
                actual={r.get('local') for row in rows for r in row.get('addr_info',()) if r.get('family')=='inet'}
                if address not in actual:
                    raise ActuationBlocked('Native UPF attachment address differs from pinned template')
            for interface,address in spec['peers'].items():
                self._exec(target,spec['container'],'ping','-n','-I',interface,'-c','1','-W','1',address)
        after=self.client.call('get','pod',target,'-o','json')
        if before_identity!=identity(after):
            raise ActuationBlocked('Native UPF restarted during path probe')
        return dict(source='native-upf-path-probe/v1',target_id=target,pod_uid=uid,upf_epoch=epoch,
            observed_at=self.clock(),verified=True,probe_method=spec.get('method','ip-ping'),
            receiver_counters=False,receiver_bytes_by_session={})


class UpfLifecycleController:
    """CAS journal plus real kubectl creation/deletion for fixed UPF Pod templates."""
    def __init__(self, client: KubectlResourceClient, journal, *, templates, smf=None,
                 readback=None, clock=time.time, removal_timeout=30, admission_coordinator=None,existing_targets=None):
        self.client, self.path, self.smf, self.readback, self.clock = client, Path(journal), smf, readback, clock
        self.templates = deepcopy(templates)
        self.existing_targets=deepcopy(existing_targets or {})
        self.admissions=admission_coordinator
        if self.admissions is None and isinstance(smf,NativeSmfUpfClient):
            from .pending_admission import PendingAdmissionCoordinator
            self.admissions=PendingAdmissionCoordinator(smf.admission_api(),clock=clock)
        if not 0 < removal_timeout <= 120:
            raise ValueError('Bounded removal deadline required')
        self.removal_timeout,self._workers,self._workers_lock=removal_timeout,{},Lock()
        self._read_workers,self._read_cache,self._read_lock={},{},Lock()
        if not 1 <= len(self.templates)+len(self.existing_targets) <= 64:
            raise ValueError('Configure 1..64 fixed UPF templates or commissioned existing targets')
        self.by_target = {}
        object_names, prefixes, n3_n4_addresses = set(), [], set()
        for key, template in self.templates.items():
            _name(key)
            target = _name(template['pod']['metadata']['name'])
            if target in self.by_target or template['pod'].get('kind') != 'Pod':
                raise ValueError('Each template needs a distinct exact Pod')
            if not 1 <= len(template['node_ids']) <= 4:
                raise ValueError('Configure exact allowed nodes')
            for node in template['node_ids']:
                _name(node)
            objects = [*template.get('configmaps', ()), template['pod']]
            for obj in objects:
                if obj['kind'] not in {'Pod','ConfigMap'} or obj.get('apiVersion') != 'v1':
                    raise ValueError('Only pinned v1 Pod and ConfigMap objects are supported')
                _name(obj['metadata']['name'])
                identity = obj['kind'],obj['metadata']['name']
                if identity in object_names:
                    raise ValueError('Template object names must be globally unique')
                object_names.add(identity)
                if obj['metadata'].get('namespace', client.namespace) != client.namespace:
                    raise ValueError('Template namespace mismatch')
                if any(k in obj['metadata'] for k in ('uid','resourceVersion','generateName','ownerReferences')):
                    raise ValueError('Template must be an unowned new object')
            for container in (*template['pod']['spec']['containers'],*template['pod']['spec'].get('initContainers',())):
                if not re.search(r'@sha256:[0-9a-f]{64}$', container.get('image','')):
                    raise ValueError('UPF template images must be immutable digests')
            if target not in {'upf-embb','upf-urllc','upf-mmtc','upf-v2x'}:
                def commissioned_collector(container):
                    command=[*container.get('command',()),*container.get('args',())]
                    env={row.get('name'):row for row in container.get('env',())}
                    return (any(command[i:i+2]==['--upf',target] for i in range(len(command)-1))
                        and any(command[i:i+2]==['--commissioned-upf',target] for i in range(len(command)-1))
                        and env.get('POD_UID',{}).get('valueFrom',{}).get('fieldRef',{}).get('fieldPath')=='metadata.uid')
                if not any(commissioned_collector(c) for c in template['pod']['spec']['containers']):
                    raise ValueError('Dynamic UPF collector requires exact opt-in and Downward API Pod UID')
            networks = template['pod']['metadata'].get('annotations',{}).get('k8s.v1.cni.cncf.io/networks')
            parsed = json.loads(networks or '[]')
            if not parsed or not all(isinstance(n,dict) and n.get('name') and n.get('ips') for n in parsed):
                raise ValueError('Pin exact Multus attachments and IP addresses')
            for network in parsed:
                _name(network['name'])
                for address in network['ips']:
                    ipaddress.ip_interface(address)
            interfaces = {n.get('interface'):str(ipaddress.ip_interface(n['ips'][0]).ip) for n in parsed}
            if set(interfaces) != {'n3','n4','n6'}:
                raise ValueError('Pin one N3/N4/N6 attachment per UPF')
            for interface in ('n3','n4'):
                if interfaces[interface] in n3_n4_addresses:
                    raise ValueError('UPF N3/N4 addresses must be unique')
                n3_n4_addresses.add(interfaces[interface])
            expected_prefixes = tuple(ipaddress.ip_network(p,strict=True) for p in template['ue_prefixes'])
            if (not expected_prefixes or any(a.overlaps(b) for a in expected_prefixes for b in prefixes)
                    or any(a.overlaps(b) for i,a in enumerate(expected_prefixes) for b in expected_prefixes[i+1:])):
                raise ValueError('UPF UE address prefixes must be distinct')
            prefixes.extend(expected_prefixes)
            native = self._upf_config(template.get('configmaps',()))
            if (str(native['pfcp']['addr']) != interfaces['n4']
                    or {str(r['addr']) for r in native['gtpu']['ifList'] if r.get('type')=='N3'} != {interfaces['n3']}
                    or {ipaddress.ip_network(r['cidr'],strict=True) for r in native['dnnList']} != set(expected_prefixes)):
                raise ValueError('Pinned native UPF config, attachments and UE prefixes differ')
            self.by_target[target] = (key, template)
        for target,pin in self.existing_targets.items():
            _name(target)
            if target in self.by_target or set(pin)!={'pod_name','pod_uid','smf_target_id','native_upf_id','node_ip'}:
                raise ValueError('Exact distinct existing UPF pins required')
            _name(pin['pod_name'])
            if not isinstance(pin['smf_target_id'],str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}',pin['smf_target_id']):
                raise ValueError('Pinned native SMF topology name required')
            for field in ('pod_uid','native_upf_id'):
                if str(uuid.UUID(pin[field]))!=pin[field]:
                    raise ValueError('Canonical existing UPF UUIDs required')
            ipaddress.IPv4Address(pin['node_ip'])
            self.by_target[target]=(None,None)
        self.path.parent.mkdir(parents=True, exist_ok=True,mode=0o700)
        try:
            descriptor=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            os.close(descriptor)
        except FileExistsError:
            if self.path.is_symlink() or (os.name!='nt' and stat.S_IMODE(self.path.stat().st_mode)&0o077):
                raise ValueError('UPF journal must be a private regular file')
        with self._db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS targets (id TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,state TEXT NOT NULL,value TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS deploy_queue (id TEXT PRIMARY KEY,target TEXT NOT NULL,fingerprint TEXT NOT NULL,'
                'state TEXT NOT NULL,request TEXT NOT NULL,value TEXT NOT NULL);')
    def recover_pending(self):
        """Read back unknown writes; advance only untouched owned removal steps."""
        for target in self.by_target:
            record=self._record(target)
            if record.get('busy') and record.get('deletion_phase'):
                self._start_removal_finalizer(target,record['busy'])
            elif record.get('busy') and record.get('removal_native_phase'):
                try:
                    self._continue_native_removal(target,record['busy'],record)
                except Exception:
                    self._mark_removal_recovery(target,record['busy'])
        with self._db() as db:
            # A crash after dispatch can conceal successful native creation.
            # Never replay it; existing UID-fenced restore is the recovery port.
            db.execute("UPDATE deploy_queue SET state='recovery_required' WHERE state='running'")
            queued=db.execute("SELECT id,target FROM deploy_queue WHERE state='queued'").fetchall()
        for identifier,target in queued:
            self._start_deploy_worker(target,identifier)

    def enqueue_deploy(self,target,*,template_id,node_id,**authority):
        if (template_id not in self.templates or self.by_target.get(target,(None,))[0]!=template_id
                or node_id not in self.templates[template_id]['node_ids']):
            raise ActuationBlocked('Fixed target/template/node mapping required')
        if set(authority)!={'owner_id','operation_id','expected_version'} or any(
                not isinstance(v,str) or not 1<=len(v)<=1024 for v in authority.values()):
            raise ActuationBlocked('Exact owner, operation and expected version required')
        identifier=_hash([target,authority['operation_id'],'deploy'])
        request=dict(target=target,template_id=template_id,node_id=node_id,authority=authority)
        fingerprint=_hash(request)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT fingerprint,state,value FROM deploy_queue WHERE id=?',(identifier,)).fetchone()
            if previous:
                if previous[0]!=fingerprint:
                    raise ActuationBlocked('Deployment idempotency conflict')
                return dict(accepted=True,target_id=target,operation_id=authority['operation_id'],
                    deployment_state=previous[1],installation_pending=previous[1] in {'queued','running'},service_effect_verified=False)
            record=self._record(target,db)
            if (record['busy'] or record['owner_id'] not in (None,authority['owner_id']) or db.execute(
                    "SELECT 1 FROM deploy_queue WHERE target=? AND state IN ('queued','running','recovery_required')",(target,)).fetchone()):
                raise ActuationBlocked('Target has an unresolved owned deployment')
            db.execute('INSERT INTO deploy_queue VALUES (?,?,?,?,?,?)',
                (identifier,target,fingerprint,'queued',json.dumps(request), '{}'))
        self._start_deploy_worker(target,identifier)
        return dict(accepted=True,target_id=target,operation_id=authority['operation_id'],
            deployment_state='queued',installation_pending=True,service_effect_verified=False)

    def _start_deploy_worker(self,target,identifier):
        with self._workers_lock:
            previous=self._workers.get(target)
            if previous is not None and previous.is_alive():
                return
            thread=Thread(target=self._run_deploy,args=(identifier,),daemon=True,name='upf-deploy-'+target)
            self._workers[target]=thread
            thread.start()

    def _run_deploy(self,identifier):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT request FROM deploy_queue WHERE id=? AND state='queued'",(identifier,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE deploy_queue SET state='running' WHERE id=?",(identifier,))
        request=json.loads(row[0])
        try:
            result=self.deploy(request['target'],template_id=request['template_id'],node_id=request['node_id'],**request['authority'])
            state='complete'
        except Exception:
            record=self._record(request['target'])
            uncertain=bool(record.get('busy') or record.get('resources'))
            state='recovery_required' if uncertain else 'rejected'
            result={'error':'native_deployment_requires_owner_safe_recovery' if uncertain else 'native_precondition_rejected'}
        with self._db() as db:
            db.execute('UPDATE deploy_queue SET state=?,value=? WHERE id=?',(state,json.dumps(result),identifier))

    def poll_readback(self,target):
        """Bounded HTTP read port; background work is current native reads only.

        A cached proof is usable only within the native 15-second age budget,
        measured from the start of collection, and the same journal revision.
        No result is stamped fresh when it merely finishes a slow collection.
        """
        revision=self._record(target)['revision']
        now=self.clock()
        with self._read_lock:
            cached=self._read_cache.get(target)
            usable=(cached is not None and 0<=now-cached['observed_at']<=15
                and cached['journal_revision']==revision)
            previous=self._read_workers.get(target)
            if (previous is None or not previous.is_alive()) and (not usable or time.monotonic()-cached['_completed']>=.5):
                thread=Thread(target=self._refresh_readback,args=(target,),daemon=True,name='upf-read-'+target)
                self._read_workers[target]=thread
                thread.start()
            if usable:
                return {k:deepcopy(v) for k,v in cached.items() if not k.startswith('_')}|{
                    'readback_pending':False,'readback_age_seconds':now-cached['observed_at']}
        return dict(target_id=target,readback_pending=True,service_effect_verified=False)

    def _refresh_readback(self,target):
        started=self.clock()
        revision=self._record(target)['revision']
        try:
            observed=self.inspect(target)
            if (observed['journal_revision']!=revision or self._record(target)['revision']!=revision
                    or not 0<=self.clock()-started<=15):
                return
            observed.update(observed_at=started,_completed=time.monotonic())
            with self._read_lock:
                self._read_cache[target]=observed
        except Exception:
            # Do not publish partial inventory, errors, or a synthetic success.
            return

    @staticmethod
    def _upf_config(configmaps):
        import yaml
        values = [yaml.safe_load(obj['data']['upfcfg.yaml']) for obj in configmaps if 'upfcfg.yaml' in obj.get('data',{})]
        if len(values)!=1 or not isinstance(values[0],dict):
            raise ValueError('Pin exactly one native upfcfg.yaml ConfigMap')
        return values[0]

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA busy_timeout=5000')
            with db:
                yield db
        finally:
            db.close()

    def _record(self, target, db=None):
        if target not in self.by_target:
            raise ActuationBlocked('Target is not configured')
        if db is None:
            with self._db() as connection:
                return self._record(target, connection)
        row = db.execute('SELECT value FROM targets WHERE id=?',(target,)).fetchone()
        return json.loads(row[0]) if row else dict(revision=0, owner_id=None, operation_id=None, resources=[], busy=None)

    def _save(self, target, value, db=None):
        if db is None:
            with self._db() as connection:
                self._save(target, value, connection)
            return
        db.execute('INSERT OR REPLACE INTO targets VALUES (?,?)',(target,json.dumps(value)))

    def capabilities(self):
        enabled = False
        assignment=False
        try:
            smf, readback = self.smf.capabilities(), self.readback.capabilities()
            enabled = (bool(self.templates) and smf.get('verified') is True and smf.get('compare_and_swap') is True
                and _fresh(smf.get('observed_at'), self.clock())
                and all(smf.get(k) is True for k in ('register','selection','drain'))
                and readback.get('verified') is True and readback.get('kernel_inventory') is True
                and readback.get('path_probe') is True and _fresh(readback.get('observed_at'),self.clock()))
            assignment=(smf.get('verified') is True and smf.get('compare_and_swap') is True
                and smf.get('session_admissions') is True and _fresh(smf.get('observed_at'),self.clock())
                and self.admissions is not None and readback.get('verified') is True
                and readback.get('kernel_inventory') is True and readback.get('path_probe') is True
                and _fresh(readback.get('observed_at'),self.clock()))
        except Exception:
            pass
        return dict(verified=enabled or assignment, compare_and_swap=True, add_upf=enabled, place_nf=enabled,
            assign_sessions=assignment,
            drain_and_remove=enabled, session_migration=False, observed_at=self.clock(),
            source='native-upf-controller', service_effect_verified=False)

    def _pod(self, target):
        pin=self.existing_targets.get(target)
        name=_name(target) if pin is None else pin['pod_name']
        rows = self.client.call('get','pods','--field-selector','metadata.name='+name,'-o','json')['items']
        if rows == []:
            return None
        if len(rows) != 1:
            raise ActuationBlocked('Exact native Pod identity required')
        value = rows[0]
        if (value.get('metadata',{}).get('name') != name or not value['metadata'].get('uid')
                or (pin is not None and value['metadata']['uid']!=pin['pod_uid'])):
            raise ActuationBlocked('Exact native Pod identity required')
        native_pin=self._record(target).get('native_pin')
        if native_pin and value['metadata']['uid']!=native_pin['pod_uid']:
            raise ActuationBlocked('Pinned UPF Pod incarnation changed')
        return value

    def _smf(self, target):
        pin=self.existing_targets.get(target)
        native_target=target if pin is None else pin['smf_target_id']
        value = self.smf.read(native_target)
        census=value.get('census_revision')
        if (value.get('source') != 'native-smf-upf/v1' or value.get('target_id') != native_target
                or value.get('complete') is not True or not ((type(census) is int and census>=0) or (isinstance(census,str) and census))
                or not value.get('version') or not _fresh(value.get('observed_at'),self.clock())
                or not isinstance(value.get('active_session_keys'),list)
                or not isinstance(value.get('active_pfcp_seids'),list)
                or not isinstance(value.get('inflight_session_keys'),list)):
            raise ActuationBlocked('Fresh exact native SMF topology and session evidence required')
        if value.get('registered') is True:
            address=pin['node_ip'] if pin is not None else self._upf_config(self.by_target[target][1].get('configmaps',()))['pfcp']['addr']
            if (value.get('node_ip') != address or (pin is not None and value.get('target_upf_id')!=pin['native_upf_id'])):
                raise ActuationBlocked('SMF topology N4 identity differs from pinned UPF')
        elif pin is not None:
            raise ActuationBlocked('Commissioned existing UPF absent from native SMF topology')
        incarnation=self._record(target).get('native_pin')
        if incarnation and value.get('smf_boot_epoch',value.get('boot_epoch'))!=incarnation['smf_boot_epoch']:
            raise ActuationBlocked('Pinned native SMF incarnation changed')
        if incarnation and value.get('registered') is True:
            if (value.get('target_upf_id')!=incarnation['native_upf_id']
                    or value.get('node_ip')!=incarnation['native_node_ip']
                    or value.get('smf_boot_epoch',value.get('boot_epoch'))!=incarnation['smf_boot_epoch']
                    or value.get('owner_id')!=incarnation['owner_id']):
                raise ActuationBlocked('Pinned native UPF incarnation changed')
            record=self._record(target)
            if value.get('operation_id') not in {incarnation.get('last_operation_id',incarnation['registration_operation_id']),record['operation_id']}:
                raise ActuationBlocked('Pinned native UPF operation changed')
        return value

    def _pin_registered(self,target,record,smf,pod,kernel=None):
        """Pin real register readback in the existing target ownership record."""
        if target in self.existing_targets:return
        resources=[r for r in record['resources'] if r['kind']=='Pod']
        meta=pod.get('metadata',{}) if pod else {}
        identifier=smf.get('target_upf_id')
        try:canonical=str(uuid.UUID(identifier))==identifier
        except (ValueError,TypeError,AttributeError):canonical=False
        boot=smf.get('smf_boot_epoch',smf.get('boot_epoch'))
        if (not canonical or not boot or smf.get('registered') is not True
                or len(resources)!=1 or meta.get('uid')!=resources[0]['uid']
                or meta.get('annotations',{}).get(OWNER)!=record['owner_id']
                or smf.get('owner_id')!=record['owner_id']
                or smf.get('operation_id') not in {record['operation_id'],(record.get('native_pin') or {}).get('last_operation_id')}
                or not _fresh(smf.get('observed_at'),self.clock())):
            raise ActuationBlocked('Owned native UPF registration identity incomplete')
        value=dict(pod_uid=meta['uid'],native_upf_id=identifier,native_node_ip=smf['node_ip'],
            smf_boot_epoch=boot,owner_id=record['owner_id'],
            registration_operation_id=(record.get('native_pin') or {}).get('registration_operation_id',smf['operation_id']),
            last_operation_id=(record.get('native_pin') or {}).get('last_operation_id',smf['operation_id']),
            kernel_epoch=None)
        previous=record.get('native_pin')
        if previous:
            if any(value[k]!=previous[k] for k in value if k!='kernel_epoch'):
                raise ActuationBlocked('Pinned native UPF incarnation changed')
            value['kernel_epoch']=previous.get('kernel_epoch')
        if kernel:
            if (kernel.get('source')!='gtp5g-netlink' or kernel.get('upf')!=target
                    or kernel.get('available') is not True or not kernel.get('epoch')
                    or kernel.get('errors')!=[] or kernel.get('residuals')!=[]
                    or not _fresh(kernel.get('observedAt'),self.clock())):
                raise ActuationBlocked('Pinned native UPF kernel evidence incomplete')
            if value['kernel_epoch'] not in (None,kernel['epoch']):
                raise ActuationBlocked('Pinned native UPF kernel incarnation changed')
            value['kernel_epoch']=kernel['epoch']
        if value!=previous:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self._record(target,db)
                if any(current.get(k)!=record.get(k) for k in ('revision','owner_id','operation_id','resources','busy','native_pin')):
                    raise ActuationBlocked('UPF journal changed while pinning native incarnation')
                current['native_pin']=value
                self._save(target,current,db)
            record['native_pin']=value

    def _confirm_retired_scope(self,target,record):
        # Exact current command, not a state name or a historical receipt scan.
        restored=record.get('restoration_complete') is True
        authority=record.get('restored_by',{}) if restored else record
        owner,operation=authority.get('owner_id'),authority.get('operation_id')
        if (not owner or not operation or record.get('resources') or record.get('busy')
                or record.get('recovery_required') or record.get('deletion_phase')
                or record.get('deleting_uid') or record.get('restore_baseline')):
            raise ActuationBlocked('Unresolved UPF lifecycle cannot be omitted from physical scope')
        identifier=_hash([target,operation,'remove'])
        def command():
            with self._db() as db:
                return db.execute('SELECT fingerprint,state,value FROM commands WHERE id=?',(identifier,)).fetchone()
        terminal=command()
        if terminal is None or terminal[1]!='complete':
            raise ActuationBlocked('UPF absence requires its exact completed removal command')
        value=json.loads(terminal[2])
        if (value.get('accepted') is not True or value.get('target_id')!=target
                or value.get('operation_id')!=operation or value.get('cleanup_pending') is not False):
            raise ActuationBlocked('UPF terminal removal proof differs from current ownership')
        pod=self._pod(target);smf=self._smf(target);pin=record.get('native_pin')
        if (pod is not None or smf.get('registered') is not False or smf.get('accepting') is not False
                or any(smf[k] for k in ('active_session_keys','active_pfcp_seids','inflight_session_keys'))
                or smf.get('owner_id')!=owner or smf.get('operation_id')!=operation
                or (pin and (pin.get('owner_id')!=owner or
                    (smf.get('target_upf_id'),smf.get('node_ip'),smf.get('smf_boot_epoch',smf.get('boot_epoch')))!=
                    (pin['native_upf_id'],pin['native_node_ip'],pin['smf_boot_epoch'])))):
            raise ActuationBlocked('Retired UPF current native ownership or absence changed')
        closing=self._smf(target)
        fields=('version','registered','accepting','owner_id','operation_id','target_upf_id','node_ip',
                'smf_boot_epoch','boot_epoch','active_session_keys','active_pfcp_seids','inflight_session_keys')
        if (self._pod(target) is not None or self._record(target)!=record or command()!=terminal
                or any(closing.get(k)!=smf.get(k) for k in fields)
                or not _fresh(smf.get('observed_at'),self.clock())):
            raise ActuationBlocked('Retired UPF current absence proof changed during readback')

    def current_registry(self):
        """Authenticated producer port. No native writes, no second registry DB."""
        from .commissioned_upfs import (CommissionedUpfSlot,ActiveUpfBinding,UpfEvidenceRoute,
            CommissionedUpfObservation,RegistrySnapshot)
        started=self.clock();rows=[]
        inventory_reader=getattr(self.smf,'active_inventory',None)
        if not callable(inventory_reader):raise ActuationBlocked('Complete native UPF census not configured')
        def census():
            value=inventory_reader()
            if (value.get('source')!='native-smf-active-sessions' or value.get('complete') is not True
                    or not value.get('smf_boot_epoch') or type(value.get('census_revision')) is not int
                    or not _fresh(value.get('observed_at'),self.clock()) or not isinstance(value.get('upfs'),list)
                    or len(value['upfs'])>64):
                raise ActuationBlocked('Complete fresh native UPF census required')
            identities=[]
            for item in value['upfs']:
                if not _fresh(item.get('observed_at'),self.clock()):raise ActuationBlocked('Native UPF census row stale')
                identities.append((item['node_ip'],item['upf_id'],item['upf_epoch']))
            if len({r[0] for r in identities})!=len(identities) or len({r[1] for r in identities})!=len(identities):
                raise ActuationBlocked('Ambiguous native UPF census')
            return value,sorted(identities)
        opening,opening_ids=census()
        for target,(template_id,template) in sorted(self.by_target.items()):
            record=self._record(target);existing=self.existing_targets.get(target)
            address=existing['node_ip'] if existing else self._upf_config(template.get('configmaps',()))['pfcp']['addr']
            pod_name=existing['pod_name'] if existing else target
            route=UpfEvidenceRoute(self.client.context,self.client.namespace,pod_name)
            slot=CommissionedUpfSlot(target,pod_name,address,template_id or '',target,
                evidence_route=route,ue_prefixes=tuple(template['ue_prefixes']) if template else ())
            pristine=dict(revision=0,owner_id=None,operation_id=None,resources=[],busy=None)
            if existing is None and record==pristine:
                rows.append(CommissionedUpfObservation(slot,'reserved'));continue
            if existing is None and not record['resources'] and not record['busy']:
                self._confirm_retired_scope(target,record)
                rows.append(CommissionedUpfObservation(slot,'retired'));continue
            pod=self._pod(target);smf=self._smf(target)
            if smf.get('registered') is not True:
                retiring=None
                if record.get('native_pin') and pod is not None:
                    # Unregistered but still-owned kernel resources must stay in
                    # cleanup scope; no registered/admission binding is invented.
                    kernel=self.readback.kernel(target,pod['metadata']['uid'])
                    pin=record['native_pin']
                    if (smf.get('registered') is not False or smf.get('owner_id')!=record['owner_id'] or smf.get('owner_id')!=pin['owner_id']
                            or smf.get('operation_id') not in {record['operation_id'],pin.get('last_operation_id')}
                            or (smf.get('target_upf_id'),smf.get('node_ip'),smf.get('smf_boot_epoch',smf.get('boot_epoch')))!=(
                                pin['native_upf_id'],pin['native_node_ip'],pin['smf_boot_epoch'])
                            or pod['metadata']['uid']!=pin['pod_uid']
                            or kernel.get('source')!='gtp5g-netlink' or kernel.get('upf')!=target
                            or kernel.get('available') is not True or kernel.get('errors')!=[]
                            or not isinstance(kernel.get('sessions'),list) or not isinstance(kernel.get('residuals'),list)
                            or kernel.get('epoch')!=pin.get('kernel_epoch') or not _fresh(kernel.get('observedAt'),self.clock())):
                        raise ActuationBlocked('Retiring UPF current native ownership or incarnation changed')
                    closing=self._smf(target);closing_pod=self._pod(target);latest=self._record(target)
                    if (closing_pod is None or closing_pod['metadata']!=pod['metadata']
                            or any(closing.get(k)!=smf.get(k) for k in ('version','target_upf_id','node_ip','registered',
                                'accepting','owner_id','operation_id','smf_boot_epoch','boot_epoch','active_session_keys',
                                'active_pfcp_seids','inflight_session_keys'))
                            or latest!=record
                            or not _fresh(smf.get('observed_at'),self.clock()) or not _fresh(kernel.get('observedAt'),self.clock())):
                        raise ActuationBlocked('Retiring UPF current native proof changed during readback')
                    retiring=ActiveUpfBinding(target,pin['pod_uid'],pin['native_upf_id'],address,
                        pin['smf_boot_epoch'],pin['kernel_epoch'],record['owner_id'],record['operation_id'],
                        record['revision'],_hash([record['revision'],pod['metadata']['uid'],pod['metadata'].get('resourceVersion'),smf['version'],pin['kernel_epoch']]),
                        min(started,smf['observed_at'],closing['observed_at'],timestamp(kernel['observedAt'])),False)
                rows.append(CommissionedUpfObservation(slot,'deploying' if not record.get('native_pin') else 'draining',
                    retiring,pod['metadata']['uid'] if pod else ''))
                continue
            if pod is None:raise ActuationBlocked('Registered native UPF Pod missing')
            proof=self.readback.read(target,pod['metadata']['uid']);kernel=proof.get('kernel',{})
            if sorted(str(r['pfcpSeid']) for r in kernel.get('sessions',()))!=sorted(str(s) for s in smf['active_pfcp_seids']):
                raise ActuationBlocked('UPF registry SMF/kernel session census differs')
            self._pin_registered(target,record,smf,pod,kernel)
            pin=record.get('native_pin')
            epoch=kernel.get('epoch')
            if not epoch:raise ActuationBlocked('Registered UPF kernel identity missing')
            if existing:
                if smf.get('target_upf_id')!=existing['native_upf_id']:
                    raise ActuationBlocked('Existing native UPF incarnation changed')
                pin=dict(pod_uid=existing['pod_uid'],native_upf_id=existing['native_upf_id'],native_node_ip=address,
                    smf_boot_epoch=smf.get('smf_boot_epoch',smf.get('boot_epoch')),kernel_epoch=epoch)
            closing=self._smf(target);closing_pod=self._pod(target)
            if (closing_pod is None or closing_pod['metadata']['uid']!=pod['metadata']['uid']
                    or any(closing.get(k)!=smf.get(k) for k in ('version','target_upf_id','registered','accepting','owner_id','operation_id'))):
                raise ActuationBlocked('UPF registry changed during readback')
            latest=self._record(target)
            if latest['revision']!=record['revision']:raise ActuationBlocked('UPF registry owner revision changed')
            recovery=bool(record.get('recovery_required') or record['busy'])
            draining=bool(record.get('quiesced') or record.get('deletion_phase') or not smf.get('accepting'))
            ready=bool(not recovery and not draining and smf.get('pfcp_associated') is True
                and proof.get('path',{}).get('verified') is True
                and any(c.get('type')=='Ready' and c.get('status')=='True' for c in pod.get('status',{}).get('conditions',())))
            binding=ActiveUpfBinding(target,pin['pod_uid'],pin['native_upf_id'],address,pin['smf_boot_epoch'],epoch,
                record['owner_id'] or '',record['operation_id'] or '',record['revision'],
                _hash([record['revision'],pod['metadata']['uid'],pod['metadata'].get('resourceVersion'),smf['version'],epoch]),
                min(started,smf['observed_at'],timestamp(kernel['observedAt'])),ready)
            rows.append(CommissionedUpfObservation(slot,'recovery_required' if recovery else 'draining' if draining else 'registered',binding,
                native_registered=True))
        closing,closing_ids=census()
        actual_ids=sorted((r.binding.native_node_ip,r.binding.native_upf_id,r.binding.kernel_epoch)
            for r in rows if r.native_registered)
        if (actual_ids!=opening_ids or opening_ids!=closing_ids
                or opening['smf_boot_epoch']!=closing['smf_boot_epoch']
                or any(r.binding.smf_boot_epoch!=opening['smf_boot_epoch'] for r in rows if r.native_registered)
                or opening['census_revision']!=closing['census_revision']):
            raise ActuationBlocked('Complete native UPF registry membership changed')
        snapshot=RegistrySnapshot(tuple(rows),min(started,opening['observed_at']))
        snapshot.require_fresh(self.clock())
        return snapshot

    def inspect(self, target):
        record, pod = self._record(target), self._pod(target)
        diagnostics=[]
        try:
            smf = self._smf(target)
        except Exception:
            smf = {}
            diagnostics.append('smf_readback_missing')
        native = {}
        if pod is not None and self.readback is not None:
            try:
                native = self.readback.read(target,pod['metadata']['uid'])
            except Exception:
                diagnostics.append('native_readback_missing')
        diagnostics.extend(code for code in native.get('diagnostics',()) if code in
            {'kernel_readback_missing','path_probe_failed','path_probe_missing','path_probe_permission_unavailable','kernel_epoch_changed'})
        kernel, path = native.get('kernel',{}), native.get('path',{})
        pinned=record.get('native_pin',{})
        if pinned and kernel and pinned.get('kernel_epoch') not in (None,kernel.get('epoch')):
            raise ActuationBlocked('Pinned native UPF kernel incarnation changed')
        actual_seids = sorted(str(s['pfcpSeid']) for s in kernel.get('sessions',()))
        matched = bool(kernel) and bool(smf) and actual_seids == sorted(str(s) for s in smf['active_pfcp_seids'])
        absent_and_drained = (pod is None and bool(smf) and smf.get('registered') is False
            and smf['active_session_keys'] == [] and smf['active_pfcp_seids'] == [] and smf['inflight_session_keys'] == [])
        ready = pod is not None and any(c.get('type') == 'Ready' and c.get('status') == 'True'
            for c in pod.get('status',{}).get('conditions',()))
        meta = {} if pod is None else pod['metadata']
        admission_handles=record.get('admission_installations',[])
        admissions_closed=not admission_handles
        admissions_installed,bindings_complete,bound_session_keys=False,False,[]
        if admission_handles and self.admissions is not None:
            try:
                from .pending_admission import AdmissionInstallation
                handles=tuple(AdmissionInstallation.from_dict(v) for v in admission_handles)
                states=tuple(self.admissions.api.inspect(h.admission.identity.nonce) for h in handles)
                if any(s is None or s.status not in {'registered','selected','bound'} for s in states):
                    raise ActuationBlocked('Admission is no longer installed')
                batch=self.admissions.read_bindings(handles)
                admissions_installed=True
                bindings_complete=batch.complete
                bound_session_keys=list(batch.session_keys)
            except Exception:
                pass
            try:
                admissions_closed=True
                for handle in handles:
                    state=self.admissions.api.inspect(handle.admission.identity.nonce)
                    self.admissions._verify_state(handle.admission,state)
                    if (state.status!='cancelled' or state.boot_epoch!=handle.native_boot_epoch
                            or state.operation_id!=handle.operation_id or state.registered_at!=handle.native_registered_at):
                        admissions_closed=False
                        break
            except Exception:
                admissions_closed=False
        cleanup_pending=bool(record.get('busy') and record.get('deletion_phase'))
        with self._db() as db:
            deployment=db.execute('SELECT state FROM deploy_queue WHERE target=? ORDER BY rowid DESC LIMIT 1',(target,)).fetchone()
        restored=bool(record.get('restoration_complete') and not record['busy']
            and not record['resources'] and absent_and_drained and record['owner_id'] is None)
        return dict(source='native-upf-controller',target_id=target,
            diagnostics=sorted(set(diagnostics)),
            version=_hash([record['revision'],meta.get('uid'),meta.get('resourceVersion'),smf.get('version'),kernel.get('epoch')]),
            exists=pod is not None or cleanup_pending,pod_exists=pod is not None,cleanup_pending=cleanup_pending,
            owner_id=record['owner_id'],operation_id=record['operation_id'],
            deployment_state=None if deployment is None else deployment[0],
            installation_pending=deployment is not None and deployment[0] in {'queued','running'},
            deployment_phase=record.get('deployment_phase'),recovery_required=record.get('recovery_required',False),
            template_id=record.get('template_id'),node_id=None if pod is None else pod['spec'].get('nodeName'),
            ready=ready,pfcp_associated=smf.get('pfcp_associated') is True and bool(kernel),
            path_verified=path.get('verified') is True, smf_selected=smf.get('registered') is True and smf.get('accepting') is True,
            accepting=smf.get('accepting'),active_session_keys=smf.get('active_session_keys') if matched or absent_and_drained else None,
            receiver_bytes_by_session=path.get('receiver_bytes_by_session',{}),
            placement_capacity_verified=self._placement_available(target),
            pod_uid=meta.get('uid'),kernel_epoch=kernel.get('epoch'),journal_revision=record['revision'],
            native_upf_id=smf.get('target_upf_id'),admission_handles=deepcopy(admission_handles),
            smf_boot_epoch=smf.get('smf_boot_epoch',smf.get('boot_epoch')),
            admissions_installed=admissions_installed,bindings_complete=bindings_complete,bound_session_keys=bound_session_keys,
            admissions_closed=admissions_closed,selection_closed=(admissions_closed if target in self.existing_targets else smf.get('accepting') is False),
            ownership_scope='pending-admissions' if target in self.existing_targets else 'managed-nf',
            nf_owner_id=smf.get('owner_id'),
            restored=restored,restoration_pending=bool(record.get('restore_baseline')) and not restored,
            restoration_owner_id=record.get('restoration_owner_id'),
            restoration_operation_id=record.get('restoration_operation_id'),
            existing_baseline_restored=record.get('existing_baseline_restored'),
            service_effect_verified=False)

    def _placement_available(self, target):
        if target in self.existing_targets:
            return False
        _, template = self.by_target[target]
        return any(self._capacity(node,template,raise_error=False) for node in template['node_ids'])

    def _capacity(self, node_id, template, *, raise_error=True):
        try:
            if node_id not in template['node_ids']:
                raise ValueError()
            node = self.client.call('get','node',node_id,'-o','json')
            conditions = {c['type']:c['status'] for c in node['status']['conditions']}
            if (conditions.get('Ready') != 'True' or node.get('spec',{}).get('unschedulable') is True
                    or any(conditions.get(k) == 'True' for k in ('MemoryPressure','DiskPressure','PIDPressure'))
                    or node['metadata'].get('labels',{}).get('cityverse.io/gtp5g-ready') != 'true'
                    or any(t.get('effect') in {'NoSchedule','NoExecute'} for t in node.get('spec',{}).get('taints',()))):
                raise ValueError()
            if any(node['metadata'].get('labels',{}).get(k)!=v
                    for k,v in template['pod']['spec'].get('nodeSelector',{}).items()):
                raise ValueError()
            rows = self.client.call('get','pods','--all-namespaces','--field-selector','spec.nodeName='+node_id,'-o','json')['items']
            specs = [p['spec'] for p in rows if p.get('status',{}).get('phase') not in {'Succeeded','Failed'}]
            specs.append(template['pod']['spec'])
            cpu = memory = 0
            for spec in specs:
                requests = [c.get('resources',{}).get('requests',{}) for c in (*spec.get('containers',()),*spec.get('initContainers',()))]
                requests.append(spec.get('overhead',{}))
                cpu += sum(parse_cpu_millis(r.get('cpu','0')) for r in requests)
                memory += sum(parse_memory_bytes(r.get('memory','0')) for r in requests)
            total = node['status']['allocatable']
            if cpu > parse_cpu_millis(total['cpu']) or memory > parse_memory_bytes(total['memory']):
                raise ValueError()
            return True
        except Exception:
            if raise_error:
                raise ActuationBlocked('Exact target node lacks verified UPF capacity') from None
            return False

    def _reserve(self, action, target, params, authority):
        owner, operation, expected = (authority[k] for k in ('owner_id','operation_id','expected_version'))
        if any(not isinstance(v,str) or not 1 <= len(v) <= 1024 for v in (owner,operation,expected)):
            raise ActuationBlocked('Owner, operation and expected version required')
        identifier = _hash([target,operation,action])
        fingerprint = _hash([params,authority])
        with self._db() as db:
            queued=db.execute("SELECT request FROM deploy_queue WHERE target=? AND state IN ('queued','running')",(target,)).fetchone()
            if queued and (action!='deploy' or json.loads(queued[0])['authority']['operation_id']!=operation):
                raise ActuationBlocked('Queued deployment owns this target')
            previous = db.execute('SELECT fingerprint,state,value FROM commands WHERE id=?',(identifier,)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise ActuationBlocked('Operation requires readback or has an idempotency conflict')
            if previous[1] != 'complete':
                record=self._record(target)
                if action=='remove' and record.get('deletion_phase') and record.get('busy')==identifier:
                    return identifier,None,dict(accepted=True,target_id=target,operation_id=operation,
                        cleanup_pending=True,service_effect_verified=False)
                raise ActuationBlocked('Operation requires readback or has an idempotency conflict')
            return identifier,None,json.loads(previous[2])
        current = self.inspect(target)
        if current['version'] != expected:
            raise ActuationBlocked('Native UPF version changed')
        required='assign_sessions' if target in self.existing_targets and action in {'assign','quiesce','restore-existing-admissions'} else 'add_upf'
        if target in self.existing_targets and action not in {'assign','quiesce','restore-existing-admissions'}:
            raise ActuationBlocked('Existing NF ownership and lifecycle cannot be changed')
        if not self.capabilities().get(required):
            raise ActuationBlocked('Required native UPF primitives are not commissioned')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self._record(target,db)
            if (record['busy'] or record['owner_id'] not in (None,owner)
                    or record['revision'] != current['journal_revision']):
                raise ActuationBlocked('UPF is busy or belongs to another owner')
            record.update(busy=identifier,owner_id=owner,operation_id=operation,revision=record['revision']+1)
            if action!='remove':
                record.update(restoration_complete=False,restore_baseline=None)
            self._save(target,record,db)
            db.execute('INSERT INTO commands VALUES (?,?,?,?)',(identifier,fingerprint,'pending','{}'))
        return identifier,record,None

    def _execute(self, action, target, params, authority, function):
        identifier,record,cached = self._reserve(action,target,params,authority)
        if record is None:
            return cached
        try:
            function(record)
            record.update(busy=None,revision=record['revision']+1)
            with self._db() as db:
                self._save(target,record,db)
                value = dict(accepted=True,target_id=target,operation_id=authority['operation_id'],service_effect_verified=False)
                db.execute("UPDATE commands SET state='complete',value=? WHERE id=?",(json.dumps(value),identifier))
            return value
        except Exception:
            # Preserve uncertain ownership. There is no automatic replay of native writes.
            record['recovery_required'] = True
            self._save(target,record)
            raise ActuationBlocked('Native UPF operation requires readback/recovery') from None

    def _owned(self, resource, record):
        obj = self.client.call('get',resource['kind'],resource['name'],'-o','json')
        meta = obj['metadata']
        if (meta.get('uid') != resource['uid'] or meta.get('annotations',{}).get(OWNER) != record['owner_id']
                or meta.get('annotations',{}).get(OPERATION) != resource['operation_id']):
            raise ActuationBlocked('Exact created-resource UID and ownership required')
        return obj

    def _delete(self, resource, record):
        actual = self._owned(resource,record)
        meta = actual['metadata']
        # Kubernetes DeleteOptions preconditions protect against name reuse after this read.
        plural = {'Pod':'pods','ConfigMap':'configmaps'}[resource['kind']]
        route = f'/api/v1/namespaces/{self.client.namespace}/{plural}/{resource["name"]}'
        self.client.call('delete','--raw',route,'-f','-',body={'apiVersion':'v1','kind':'DeleteOptions',
            'preconditions':{'uid':meta['uid'],'resourceVersion':meta['resourceVersion']},'propagationPolicy':'Foreground'})

    def deploy(self, target, *, template_id, node_id, **authority):
        if template_id not in self.templates or self.by_target.get(target,(None,))[0] != template_id:
            raise ActuationBlocked('Fixed target/template mapping required')
        template = self.templates[template_id]
        def install(record):
            record['deployment_phase']='preflight'
            self._save(target,record)
            self._capacity(node_id,template)
            if self._pod(target) is not None:
                raise ActuationBlocked('Existing Pod cannot be replaced')
            networks = json.loads(template['pod']['metadata']['annotations']['k8s.v1.cni.cncf.io/networks'])
            desired_ips = {str(ipaddress.ip_interface(ip).ip) for net in networks for ip in net['ips']}
            for network in networks:
                self.client.call('get','network-attachment-definition',network['name'],'-o','json')
            pods = self.client.call('get','pods','--all-namespaces','-o','json')['items']
            for pod in pods:
                annotations = pod.get('metadata',{}).get('annotations',{})
                observed = json.loads(annotations.get('k8s.v1.cni.cncf.io/network-status','[]'))
                observed += json.loads(annotations.get('k8s.v1.cni.cncf.io/networks','[]'))
                if desired_ips & {str(ipaddress.ip_interface(ip).ip) for net in observed for ip in net.get('ips',())}:
                    raise ActuationBlocked('Pinned Multus address is already in use')
            configmaps = self.client.call('get','configmaps','-o','json')['items']
            import yaml
            desired_prefixes = tuple(ipaddress.ip_network(p) for p in template['ue_prefixes'])
            for obj in configmaps:
                if 'upfcfg.yaml' not in obj.get('data',{}):
                    continue
                native = yaml.safe_load(obj['data']['upfcfg.yaml'])
                if not isinstance(native,dict) or not isinstance(native.get('dnnList'),list):
                    raise ActuationBlocked('Existing native UPF prefix configuration is unreadable')
                for row in native['dnnList']:
                    prefix = ipaddress.ip_network(row['cidr'],strict=True)
                    if any(prefix.overlaps(p) for p in desired_prefixes):
                        raise ActuationBlocked('Pinned UE prefix overlaps an existing native UPF')
            record.update(template_id=template_id,node_id=node_id)
            if record.get('native_pin'):
                absent=self._smf(target)
                if (record['resources'] or absent.get('registered') is not False
                        or absent.get('active_session_keys')!=[] or absent.get('active_pfcp_seids')!=[]
                        or absent.get('inflight_session_keys')!=[]):
                    raise ActuationBlocked('Previous UPF incarnation has not retired')
                record['native_pin']=None
                record['quiesced']=False
            self._save(target,record)
            for manifest in (*template.get('configmaps',()),template['pod']):
                obj = deepcopy(manifest)
                obj['metadata'].update(namespace=self.client.namespace)
                obj['metadata'].setdefault('annotations',{}).update({OWNER:authority['owner_id'],OPERATION:authority['operation_id']})
                if obj['kind'] == 'Pod':
                    obj['spec']['nodeName'] = node_id
                record['deployment_phase']='create_started:'+obj['kind']+':'+obj['metadata']['name']
                self._save(target,record)
                created = self.client.call('create','-f','-','-o','json',body=obj)
                meta = created['metadata']
                if meta.get('name') != obj['metadata']['name'] or not meta.get('uid'):
                    raise ActuationBlocked('Native create identity missing')
                record['resources'].append(dict(kind=obj['kind'],name=meta['name'],uid=meta['uid'],operation_id=authority['operation_id']))
                record['deployment_phase']='created:'+obj['kind']+':'+meta['name']
                self._save(target,record)
            smf = self._smf(target)
            record['deployment_phase']='register_started'
            self._save(target,record)
            self.smf.register(target,**(authority | {'expected_version':smf['version']}))
            registered=self._smf(target)
            if (registered.get('registered') is not True or registered.get('owner_id')!=authority['owner_id']
                    or registered.get('operation_id')!=authority['operation_id']):
                raise ActuationBlocked('Native SMF registration readback did not confirm ownership')
            pod=self._pod(target)
            try:kernel=self.readback.read(target,pod['metadata']['uid']).get('kernel',{})
            except Exception:kernel={}
            self._pin_registered(target,record,registered,pod,kernel or None)
            record['deployment_phase']='registered'
            self._save(target,record)
        return self._execute('deploy',target,dict(template_id=template_id,node_id=node_id),authority,install)

    def configure_smf(self, target, *, accepting, **authority):
        if type(accepting) is not bool:
            raise ActuationBlocked('Accepting must be a boolean')
        def select(record):
            actual = self.inspect(target)
            if accepting and not all(actual[k] is True for k in ('ready','pfcp_associated','path_verified')):
                raise ActuationBlocked('Native PFCP and independent path proof required before selection')
            smf = self._smf(target)
            if target not in self.existing_targets:
                pod=self._pod(target)
                proof=self.readback.read(target,pod['metadata']['uid'])
                self._pin_registered(target,record,smf,pod,proof.get('kernel'))
            self.smf.selection(target,accepting=accepting,**(authority | {'expected_version':smf['version']}))
            observed = self._smf(target)
            if (observed.get('accepting') is not accepting or observed.get('owner_id')!=authority['owner_id']
                    or observed.get('operation_id')!=authority['operation_id']):
                raise ActuationBlocked('SMF selection readback did not confirm')
            if record.get('native_pin'):
                record['native_pin']['last_operation_id']=observed['operation_id']
            record['quiesced']=not accepting
        return self._execute('smf',target,{'accepting':accepting},authority,select)

    def prepare_drain(self,target,**authority):
        """Close only owned future admissions; active PDU sessions remain intact."""
        def close(record):
            if record.get('admission_installations'):
                if self.admissions is None:
                    raise ActuationBlocked('Native admission cancellation unavailable')
                from .pending_admission import AdmissionInstallation
                for value in record['admission_installations']:
                    handle=AdmissionInstallation.from_dict(value)
                    state=self.admissions.api.inspect(handle.admission.identity.nonce)
                    if state is None:
                        raise ActuationBlocked('Native owned admission disappeared')
                    self.admissions.release(handle,state.version)
            if target not in self.existing_targets:
                smf=self._smf(target)
                self.smf.selection(target,accepting=False,**(authority|{'expected_version':smf['version']}))
                observed=self._smf(target)
                if (observed.get('accepting') is not False or observed.get('owner_id')!=authority['owner_id']
                        or observed.get('operation_id')!=authority['operation_id']):
                    raise ActuationBlocked('Native selection closure not confirmed')
                if record.get('native_pin'):
                    record['native_pin']['last_operation_id']=observed['operation_id']
            record['quiesced']=True
        result=self._execute('quiesce',target,{},authority,close)
        actual=self.inspect(target)
        return result|dict(admissions_closed=actual['admissions_closed'],selection_closed=actual['selection_closed'],
            native_ues_gone=False,ownership_scope=actual['ownership_scope'])

    def assign(self, target, session_keys=(), *, mode, pending_admissions=(), **authority):
        from .pending_admission import PendingAdmission,SessionAssignment
        pending=tuple(PendingAdmission.from_dict(p) for p in pending_admissions)
        assignment=SessionAssignment(target,mode,pending,tuple(session_keys))
        if mode != 'new_sessions' or not self.capabilities()['assign_sessions']:
            raise ActuationBlocked('Exact new-session admission required; migration is unsupported')
        def admit(record):
            smf = self._smf(target)
            if not smf.get('target_upf_id') or not smf.get('accepting'):
                raise ActuationBlocked('Fresh native selected UPF identity required')
            assignment.validate_authority(owner_id=authority['owner_id'],
                native_target_upf_id=smf['target_upf_id'],now=self.clock())
            record.setdefault('admission_installations',[])
            for admission in pending:
                handle=self.admissions.install(admission,authority['operation_id'])
                record['admission_installations'].append(handle.to_dict())
                self._save(target,record)
        return self._execute('assign',target,dict(session_keys=session_keys,mode=mode,
            pending_admissions=pending_admissions),authority,admit)

    def _drained(self, target):
        smf = self._smf(target)
        pod = self._pod(target)
        if (smf.get('accepting') is not False or smf['active_session_keys'] != []
                or smf['active_pfcp_seids'] != [] or smf['inflight_session_keys'] != []):
            raise ActuationBlocked('UPF still accepts or carries active/inflight sessions')
        if pod is not None:
            proof = self.readback.read(target,pod['metadata']['uid'])
            if proof['kernel'].get('sessions') != []:
                raise ActuationBlocked('Native UPF kernel still carries sessions')
        return smf

    def _start_removal_finalizer(self,target,identifier):
        with self._workers_lock:
            previous=self._workers.get(target)
            if previous is not None and previous.is_alive():
                return
            worker=Thread(target=self._finish_removal,args=(target,identifier),daemon=True,
                name='upf-removal-'+target)
            self._workers[target]=worker
            worker.start()

    def _finish_removal(self,target,identifier):
        deadline=time.monotonic()+self.removal_timeout
        try:
            record=self._record(target)
            pod_resource=next((r for r in record['resources'] if r['kind']=='Pod'),None)
            while True:
                pod=self._pod(target)
                if pod is None:
                    break
                if pod_resource is None or pod['metadata']['uid']!=pod_resource['uid']:
                    raise ActuationBlocked('Pod identity changed during deletion')
                if time.monotonic()>=deadline:
                    raise ActuationBlocked('Pod deletion did not finish within its deadline')
                time.sleep(.05)
            self._drained(target)
            record=self._record(target)
            if record['busy']!=identifier:
                raise ActuationBlocked('Removal owner changed')
            record['resources']=[r for r in record['resources'] if r['kind']!='Pod']
            self._save(target,record)
            for resource in reversed(record['resources'][:]):
                replay=(record.get('deletion_phase')=='configmap_delete_started'
                    and record.get('deleting_uid')==resource['uid'])
                if not replay:
                    record.update(deletion_phase='configmap_delete_started',deleting_uid=resource['uid'])
                    self._save(target,record)
                    self._delete(resource,record)
                # A restarted worker reads the prior outcome instead of replaying DELETE.
                while True:
                    rows=self.client.call('get','configmaps','--field-selector','metadata.name='+resource['name'],'-o','json')['items']
                    if not rows:
                        break
                    if len(rows)!=1 or rows[0]['metadata']['uid']!=resource['uid']:
                        raise ActuationBlocked('ConfigMap identity changed during deletion')
                    if time.monotonic()>=deadline:
                        raise ActuationBlocked('ConfigMap deletion outcome requires recovery')
                    time.sleep(.05)
                record['resources']=[r for r in record['resources'] if r['uid']!=resource['uid']]
                self._save(target,record)
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self._record(target,db)
                if current['busy']!=identifier:
                    raise ActuationBlocked('Removal owner changed')
                current.update(busy=None,deletion_phase=None,deleting_uid=None,
                    recovery_required=False,revision=current['revision']+1)
                operation_id=current['operation_id']
                if current.get('restore_baseline'):
                    baseline=current['restore_baseline']
                    current.update(owner_id=baseline['owner_id'],operation_id=baseline['operation_id'],
                        node_id=None,template_id=None,restoration_complete=True,restore_baseline=None,
                        restored_by={'owner_id':current['restoration_owner_id'],
                                     'operation_id':current['restoration_operation_id']})
                    db.execute("UPDATE deploy_queue SET state='restored' WHERE target=? AND state='recovery_required' "
                        "AND json_extract(request,'$.authority.owner_id')=? AND json_extract(request,'$.authority.operation_id')=?",
                        (target,current['restoration_owner_id'],current['restoration_operation_id']))
                self._save(target,current,db)
                result=dict(accepted=True,target_id=target,operation_id=operation_id,
                    cleanup_pending=False,service_effect_verified=False)
                db.execute("UPDATE commands SET state='complete',value=? WHERE id=?",(json.dumps(result),identifier))
        except Exception:
            with self._db() as db:
                current=self._record(target,db)
                if current.get('busy')==identifier:
                    current['recovery_required']=True
                    self._save(target,current,db)

    def _mark_removal_recovery(self,target,identifier):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self._record(target,db)
            if current.get('busy')==identifier:
                current['recovery_required']=True
                self._save(target,current,db)

    def _removal_phase(self,target,record,phase,**changes):
        """CAS a bounded phase before native I/O; never hold its transaction over I/O."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self._record(target,db)
            if current!=record:
                raise ActuationBlocked('Removal journal changed')
            current.update(removal_native_phase=phase,**changes)
            self._save(target,current,db)
        record.clear();record.update(current)

    @staticmethod
    def _removal_lineage(smf):
        return {key:smf.get(key) for key in ('target_upf_id','node_ip','owner_id','incarnation')}

    def _continue_native_removal(self,target,identifier,record):
        """Advance untouched phases only after readback proves any unknown prior write."""
        if record.get('busy')!=identifier:
            raise ActuationBlocked('Removal ownership changed')
        authority=dict(owner_id=record['owner_id'],operation_id=record['operation_id'])
        smf=self._drained(target)
        phase=record.get('removal_native_phase')
        if phase is None:
            before=dict(version=smf['version'],operation_id=smf.get('operation_id'),
                smf_boot_epoch=smf.get('smf_boot_epoch',smf.get('boot_epoch')),
                lineage=self._removal_lineage(smf))
            if (smf.get('registered') is not True or smf.get('owner_id')!=authority['owner_id']):
                raise ActuationBlocked('Owned registered UPF required before removal transfer')
            self._removal_phase(target,record,'transfer_started',removal_precondition=before)
            # Even a nonaccepting selection needs an explicit CAS transfer to the
            # NEW remove operation: native unregister requires SAME current op.
            self.smf.selection(target,accepting=False,**(authority|{'expected_version':smf['version']}))
            smf=self._drained(target)
            phase='transfer_started'
        before=record['removal_precondition']
        if phase=='transfer_started':
            if (smf.get('registered') is not True or smf.get('operation_id')!=authority['operation_id']
                    or self._removal_lineage(smf)!=before['lineage']
                    or smf.get('smf_boot_epoch',smf.get('boot_epoch'))!=before['smf_boot_epoch']
                    or smf['version']==before['version']):
                raise ActuationBlocked('Uncertain selection transfer requires readback; no replay')
            pin=deepcopy(record.get('native_pin'))
            if pin is None:raise ActuationBlocked('Removal native registration pin missing')
            pin['last_operation_id']=authority['operation_id']
            self._removal_phase(target,record,'transfer_confirmed',native_pin=pin,
                removal_confirmed_version=smf['version'],recovery_required=False)
            phase='transfer_confirmed'
        if phase=='transfer_confirmed':
            smf=self._drained(target)
            if (smf.get('registered') is not True or smf.get('operation_id')!=authority['operation_id']
                    or self._removal_lineage(smf)!=before['lineage']
                    or smf['version']!=record['removal_confirmed_version']):
                raise ActuationBlocked('Selection transfer changed before unregister')
            self._removal_phase(target,record,'unregister_started')
            self.smf.unregister(target,**(authority|{'expected_version':smf['version']}))
            smf=self._drained(target)
            phase='unregister_started'
        if phase=='unregister_started':
            if (smf.get('registered') is not False or smf.get('operation_id')!=authority['operation_id']
                    or self._removal_lineage(smf)!=before['lineage']
                    or smf.get('smf_boot_epoch',smf.get('boot_epoch'))!=before['smf_boot_epoch']
                    or smf['version']==record['removal_confirmed_version']):
                raise ActuationBlocked('Uncertain unregister requires tombstone readback; no replay')
            self._removal_phase(target,record,'unregistered_confirmed',removal_tombstone_version=smf['version'],recovery_required=False)
            phase='unregistered_confirmed'
        if phase!='unregistered_confirmed':
            raise ActuationBlocked('Unknown removal phase')
        smf=self._drained(target)
        if (smf.get('registered') is not False or smf.get('operation_id')!=authority['operation_id']
                or self._removal_lineage(smf)!=before['lineage']
                or smf.get('smf_boot_epoch',smf.get('boot_epoch'))!=before['smf_boot_epoch']
                or smf['version']!=record['removal_tombstone_version']):
            raise ActuationBlocked('Native removal tombstone changed before Pod deletion')
        # Persist before sending DELETE so process death cannot cause a blind replay.
        pod_resource=next((r for r in record['resources'] if r['kind']=='Pod'),None)
        if pod_resource is None:raise ActuationBlocked('Owned Pod creation identity is missing')
        self._removal_phase(target,record,phase,deletion_phase='pod_delete_started')
        self._delete(pod_resource,record)
        self._removal_phase(target,record,phase,deletion_phase='pod_delete_requested')
        self._start_removal_finalizer(target,identifier)
        return dict(accepted=True,target_id=target,operation_id=authority['operation_id'],
            cleanup_pending=True,service_effect_verified=False)

    def remove(self, target, **authority):
        identifier,record,cached=self._reserve('remove',target,{},authority)
        if record is None:
            return cached
        try:
            if record.get('admission_installations'):
                if self.admissions is None:
                    raise ActuationBlocked('Native admission cancellation is unavailable')
                from .pending_admission import AdmissionInstallation
                for value in record['admission_installations']:
                    handle=AdmissionInstallation.from_dict(value)
                    state=self.admissions.api.inspect(handle.admission.identity.nonce)
                    if state is None:
                        raise ActuationBlocked('Native admission disappeared during drain')
                    self.admissions.release(handle,state.version)
            return self._continue_native_removal(target,identifier,record)
        except Exception:
            self._mark_removal_recovery(target,identifier)
            raise ActuationBlocked('Native UPF removal requires readback/recovery') from None

    def restore(self, target, before, **authority):
        # Restore targets are read from this controller's journal, never trusted from HTTP.
        record = self._record(target)
        if target in self.existing_targets:
            return self._restore_commissioned_admissions(target,before,record,authority)
        with self._db() as db:
            if db.execute("SELECT 1 FROM deploy_queue WHERE target=? AND state IN ('queued','running')",(target,)).fetchone():
                raise ActuationBlocked('Deployment must settle before owner-safe compensation')
        restored_by={'owner_id':authority.get('owner_id'),'operation_id':authority.get('operation_id')}
        if (record['owner_id'] is None and record.get('restored_by')==restored_by
                and before.get('exists') is False and not record['resources'] and self._pod(target) is None):
            self._drained(target)
            return dict(restored=True,accepted=True,target_id=target,service_effect_verified=False)
        if record['owner_id'] != authority.get('owner_id'):
            raise ActuationBlocked('Restore owner changed')
        if before.get('exists') is True:
            return self._restore_existing(target,before,record,authority)
        if (before.get('exists') is not False or before.get('owner_id') is not None
                or before.get('node_id') is not None or before.get('active_session_keys') != []
                or before.get('accepting') is not False):
            raise ActuationBlocked('Existing UPF restoration requires explicit native topology compensation')
        if any(r['operation_id'] != authority.get('operation_id') for r in record['resources']):
            raise ActuationBlocked('Compensation cannot remove resources created by another operation')
        if self.inspect(target)['version'] != authority.get('expected_version'):
            raise ActuationBlocked('Compensation native version changed')
        if record.get('restore_baseline'):
            if (record.get('restoration_owner_id')!=authority['owner_id']
                    or record.get('restoration_operation_id')!=authority['operation_id']):
                raise ActuationBlocked('Another restoration owns this UPF')
            if record['busy'] and record.get('deletion_phase'):
                self._start_removal_finalizer(target,record['busy'])
                return dict(accepted=True,pending=True,restored=False,target_id=target,service_effect_verified=False)
        if not record['resources'] and not record['busy'] and self._pod(target) is None:
            self._drained(target)
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self._record(target,db)
                if current!=record:
                    raise ActuationBlocked('Compensation owner changed')
                current.update(owner_id=None,operation_id=None,template_id=None,node_id=None,
                    revision=current['revision']+1,restored_by=restored_by,restoration_complete=True,
                    restoration_owner_id=authority['owner_id'],restoration_operation_id=authority['operation_id'])
                self._save(target,current,db)
            return dict(restored=True,accepted=True,target_id=target,service_effect_verified=False)
        if record['busy']:
            if record.get('deletion_phase'):
                raise ActuationBlocked('Another removal already owns this UPF')
            # Resume compensation only after independently re-reading every created UID.
            if not record['resources']:
                raise ActuationBlocked('Uncertain creation has no journaled UID; operator recovery required')
            for resource in record['resources']:
                self._owned(resource,record)
            self._drained(target)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self._record(target,db)
            if current!=record:
                raise ActuationBlocked('Restoration owner changed')
            current.update(busy=None,revision=current['revision']+1,
                restore_baseline={'exists':False,'owner_id':None,'operation_id':None},
                restoration_owner_id=authority['owner_id'],restoration_operation_id=authority['operation_id'],
                restoration_complete=False)
            self._save(target,current,db)
        authority=authority|{'expected_version':self.inspect(target)['version']}
        result = self.remove(target,**authority)
        actual = self.inspect(target)
        return result|{'restored':actual['restored'],'pending':not actual['restored']}

    def _restore_commissioned_admissions(self,target,before,record,authority):
        pin=self.existing_targets[target]
        if self._pod(target) is None:
            raise ActuationBlocked('Commissioned existing Pod is absent')
        self._smf(target)
        if (before.get('exists') is not True or before.get('ownership_scope')!='pending-admissions'
                or before.get('pod_uid')!=pin['pod_uid'] or before.get('native_upf_id')!=pin['native_upf_id']
                or before.get('owner_id') is not None or before.get('admission_handles')):
            raise ActuationBlocked('Exact unowned commissioned admission baseline required')
        if (record.get('owner_id') is None and record.get('existing_baseline_restored')==_hash(before)
                and record.get('restoration_owner_id')==authority.get('owner_id')
                and record.get('restoration_operation_id')==authority.get('operation_id')):
            return dict(restored=True,accepted=True,target_id=target,service_effect_verified=False)
        if record.get('owner_id')!=authority.get('owner_id') or record.get('operation_id')!=authority.get('operation_id'):
            raise ActuationBlocked('Commissioned admission lease owner changed')
        if self.inspect(target)['version']!=authority.get('expected_version'):
            raise ActuationBlocked('Commissioned admission version changed')
        from .pending_admission import AdmissionInstallation
        handles=tuple(AdmissionInstallation.from_dict(v) for v in record.get('admission_installations',()))
        owned={(h.admission.identity.supi,str(h.admission.identity.pdu_session_id)) for h in handles}
        native=self._smf(target)
        for key in (*native['active_session_keys'],*native['inflight_session_keys']):
            try:
                identity=json.loads(key)
                if len(identity)!=4 or not isinstance(identity,list):
                    raise ValueError()
                if (identity[1],str(identity[2])) in owned:
                    raise ActuationBlocked('Owned session must disappear before final admission restoration')
            except (ValueError,TypeError,IndexError):
                raise ActuationBlocked('Canonical native session census required') from None
        for handle in handles:
            state=self.admissions.api.inspect(handle.admission.identity.nonce)
            if state is None:
                raise ActuationBlocked('Native owned admission disappeared')
            self.admissions.release(handle,state.version)
        # This journal lease owns nonces only. Native Pod/topology/selection is
        # never patched, claimed, unregistered or deleted for existing targets.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._record(target,db)!=record:
                raise ActuationBlocked('Admission restoration owner changed')
            record.update(owner_id=None,operation_id=None,admission_installations=[],busy=None,
                revision=record['revision']+1,existing_baseline_restored=_hash(before),
                restoration_owner_id=authority['owner_id'],restoration_operation_id=authority['operation_id'])
            self._save(target,record,db)
        return dict(restored=True,accepted=True,target_id=target,service_effect_verified=False)

    def _restore_existing(self,target,before,record,authority):
        """Undo assignment/selection on this exact managed Pod, never remove it."""
        pod=next((r for r in record['resources'] if r['kind']=='Pod'),None)
        if (before.get('source')!='native-upf-controller' or pod is None
                or before.get('pod_uid')!=pod['uid'] or before.get('owner_id')!=authority['owner_id']
                or before.get('operation_id')!=authority['operation_id']
                or before.get('template_id')!=record.get('template_id') or before.get('node_id')!=record.get('node_id')
                or type(before.get('accepting')) is not bool or before.get('admission_handles')):
            raise ActuationBlocked('Exact owned managed baseline required; consumed admissions cannot be recreated')
        if self.inspect(target)['version']!=authority.get('expected_version'):
            raise ActuationBlocked('Existing baseline compensation version changed')
        self._owned(pod,record)
        def restore_policy(current):
            if current.get('admission_installations'):
                if self.admissions is None:
                    raise ActuationBlocked('Owned admission restoration unavailable')
                from .pending_admission import AdmissionInstallation
                for value in current['admission_installations']:
                    handle=AdmissionInstallation.from_dict(value)
                    state=self.admissions.api.inspect(handle.admission.identity.nonce)
                    if state is None:
                        raise ActuationBlocked('Owned native admission disappeared')
                    self.admissions.release(handle,state.version)
                current['admission_installations']=[]
            smf=self._smf(target)
            if smf.get('accepting')!=before['accepting']:
                self.smf.selection(target,accepting=before['accepting'],**(authority|{'expected_version':smf['version']}))
            native=self._smf(target)
            if native.get('accepting')!=before['accepting'] or native.get('owner_id')!=authority['owner_id']:
                raise ActuationBlocked('Native selection baseline restoration unconfirmed')
            current.update(existing_baseline_restored=_hash(before),restoration_owner_id=authority['owner_id'],
                restoration_operation_id=authority['operation_id'])
        if record.get('existing_baseline_restored')!=_hash(before):
            self._execute('restore-existing-'+_hash(before),target,{},authority,restore_policy)
        actual=self.inspect(target)
        fields=('exists','owner_id','operation_id','accepting','node_id','template_id','pod_uid')
        restored=all(actual.get(k)==before.get(k) for k in fields) and not actual.get('admission_handles')
        return dict(restored=restored,pending=False,accepted=restored,target_id=target,service_effect_verified=False)


def create_upf_controller_app(controller, *, token_reader):
    """HTTP server for FixedOriginUpfControlApi. No production server starts on import."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    import asyncio

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(controller.recover_pending)
        yield

    async def dispatch(request):
        try:
            expected = token_reader()
        except Exception:
            return JSONResponse({'error':'authority_not_configured'},status_code=503)
        if not expected or not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+expected):
            return JSONResponse({'error':'forbidden'},status_code=403)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError()
            value = json.loads(raw)
            if not isinstance(value,dict):
                raise ValueError()
            route = request.path_params['route']
            if route == 'capabilities':
                if value != {'subsystem':'upf'}:
                    raise ValueError()
                result = await asyncio.to_thread(controller.capabilities)
            elif route=='upf/registry':
                if value!={}:raise ValueError()
                snapshot=await asyncio.to_thread(controller.current_registry)
                result=snapshot.to_dict()
            else:
                action = {'upf/read':'poll_readback','upf/deploy':'enqueue_deploy','upf/smf':'configure_smf','upf/prepare-drain':'prepare_drain',
                    'upf/assign':'assign','upf/remove':'remove','upf/restore':'restore'}.get(route)
                if action is None:
                    return JSONResponse({'error':'not_found'},status_code=404)
                fields={'upf/read':{'target_id'},
                    'upf/deploy':{'target_id','template_id','node_id','owner_id','operation_id','expected_version'},
                    'upf/smf':{'target_id','accepting','owner_id','operation_id','expected_version'},
                    'upf/prepare-drain':{'target_id','owner_id','operation_id','expected_version'},
                    'upf/assign':{'target_id','session_keys','mode','owner_id','operation_id','expected_version'},
                    'upf/remove':{'target_id','owner_id','operation_id','expected_version'},
                    'upf/restore':{'target_id','before','owner_id','operation_id','expected_version'}}[route]
                if route=='upf/assign':
                    fields=fields|({'pending_admissions'} if 'pending_admissions' in value else set())
                if set(value)!=fields:
                    raise ValueError()
                if route=='upf/restore' and not isinstance(value.get('before'),dict):
                    raise ValueError()
                target = _name(value.pop('target_id'))
                result = await asyncio.to_thread(getattr(controller,action),target,**value)
            return JSONResponse(result,status_code=202 if result.get('readback_pending') is True else 200,
                headers={'Cache-Control':'no-store'})
        except ActuationBlocked:
            return JSONResponse({'error':'native_state_or_precondition_unavailable'},status_code=409)
        except (ValueError,TypeError,KeyError,UnicodeDecodeError,RecursionError):
            return JSONResponse({'error':'invalid_request'},status_code=400)
    return Starlette(routes=[Route('/cityverse-resource/v1/{route:path}',dispatch,methods=['POST'])],lifespan=lifespan)


def configured_resource_controller():
    """Standalone uvicorn factory, with no cluster writes during construction.

Run ``uvicorn backend.onprem.intents.upf_controller:configured_resource_controller
--factory --host 127.0.0.1 --port <operator-selected-port>``. Set both
CITYVERSE_UPF_CONTROLLER_CONFIG_FILE (absolute path) and
CITYVERSE_UPF_CONTROLLER_CONFIG_SHA256. The <=1 MiB JSON schema is
``cityverse.upf-controller/v1``. Fields: context, namespace, journal (absolute
SQLite path), templates (fixed Pod/ConfigMap definitions plus node_ids and
ue_prefixes), controller_token_file, optional smf_origin + smf_token_file,
probe_target (an existing commissioned UPF), path_probes, kubernetes_timeout.

existing_targets maps logical UPF names to exact {pod_name,pod_uid,
smf_target_id,native_upf_id,node_ip}. Both UUIDs and N4 address are pinned;
templates may be empty for admission-only commissioning. These targets permit
only owned pending-session admissions, never NF ownership, global selection
changes, unregister or deletion. probe_target names the logical pinned target;
path_probes continues to use its actual exact Pod name.

path_probes map exact Pod names to {container, interfaces:{n3,n4,n6},
peers:{n3,n6}} with literal IPv4 addresses. Missing SMF or independent
kernel/path evidence leaves capabilities false. No images, peers, manifests,
token values or origins come from an HTTP request. No deployment occurs here.
"""
    path = Path(os.environ.get('CITYVERSE_UPF_CONTROLLER_CONFIG_FILE',''))
    digest = os.environ.get('CITYVERSE_UPF_CONTROLLER_CONFIG_SHA256','')
    if not path.is_absolute() or path.is_symlink() or not re.fullmatch('[a-f0-9]{64}',digest):
        raise ValueError('Pinned absolute UPF controller configuration and digest required')
    with path.open('rb') as stream:
        raw = stream.read(1_048_577)
    if len(raw)>1_048_576 or hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError('UPF controller configuration digest mismatch')
    config=json.loads(raw)
    allowed={'schema','context','namespace','journal','templates','controller_token_file',
        'smf_origin','smf_token_file','probe_target','path_probes','kubernetes_timeout','existing_targets'}
    if (not isinstance(config,dict) or set(config)-allowed
            or config.get('schema')!='cityverse.upf-controller/v1' or not Path(config['journal']).is_absolute()):
        raise ValueError('Invalid UPF controller configuration schema')
    client=KubectlResourceClient(context=config['context'],namespace=config['namespace'],
        timeout=config.get('kubernetes_timeout',5))
    if bool(config.get('smf_origin'))!=bool(config.get('smf_token_file')):
        raise ValueError('Configure SMF origin and credential file together')
    smf = NativeSmfUpfClient(config['smf_origin'],token_reader=_token_file_reader(config['smf_token_file'])) if config.get('smf_origin') else None
    path_reader=KubectlUpfPathProbe(client,config['path_probes']) if config.get('path_probes') else None
    pins=config.get('existing_targets',{})
    readback=KubectlUpfReadback(client,path_reader=path_reader,probe_target=config.get('probe_target'),
        pinned_pods={k:{f:v[f] for f in ('pod_name','pod_uid')} for k,v in pins.items()})
    controller=UpfLifecycleController(client,config['journal'],templates=config.get('templates',{}),
        smf=smf,readback=readback,existing_targets=pins)
    return create_upf_controller_app(controller,token_reader=_token_file_reader(config['controller_token_file']))
