"""Independent native application endpoint and bounded TCP activity readback."""
import ipaddress
import json
import re
import subprocess
import time

from .actuators import ActuationBlocked
from .application_journal import digest
from .resource_adapters import _cpu_millis, _memory_bytes

# Controlled iperf3 receivers create UDP sockets for active tests; the idle
# listener is TCP LISTEN. A permanent UDP listener remains conservatively busy.
ACTIVITY_SCRIPT = '''set -eu
port=$(printf '%04X' "$1")
awk -v port="$port" 'FNR>1 {split($2,a,":"); if (toupper(a[2])==port) {if (FILENAME ~ /udp/ || $4=="01") active++; if ($4=="0A") listening=1}} END {printf "%d %d\\n",active,listening}' /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6
'''
INTERFACE_SCRIPT = "import socket,fcntl,struct,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);print(socket.inet_ntoa(fcntl.ioctl(s.fileno(),0x8915,struct.pack('256s',sys.argv[1].encode()))[20:24]))"
INTERFACE_PERL = 'use Socket; socket(my $s,AF_INET,SOCK_DGRAM,0) or die; my $r=pack("a16a24",$ARGV[0],""); ioctl($s,0x8915,$r) or die; print inet_ntoa(substr($r,20,4));'


class ApplicationReadback:
    def __init__(self, client, *, clock=time.time):
        self.client, self.clock = client, clock

    def _pod(self, name):
        rows = self.client.call('get', 'pods', '--field-selector', 'metadata.name=' + name, '-o', 'json')['items']
        if len(rows) > 1:
            raise ActuationBlocked('Ambiguous native application Pod')
        return rows[0] if rows else None

    def _identity(self, pod, container):
        meta, status = pod.get('metadata', {}), pod.get('status', {})
        rows = [r for r in status.get('containerStatuses', []) if r.get('name') == container]
        if (not meta.get('uid') or meta.get('deletionTimestamp') or status.get('phase') != 'Running'
                or len(rows) != 1 or not rows[0].get('containerID') or type(rows[0].get('restartCount')) is not int):
            raise ActuationBlocked('Native application process incarnation unavailable')
        return {'pod_uid': meta['uid'], 'pod_resource_version': meta.get('resourceVersion'), 'container_id': rows[0]['containerID'], 'restart_count': rows[0]['restartCount'],
                'ready': rows[0].get('ready') is True, 'node_id': pod.get('spec', {}).get('nodeName')}

    def endpoint(self, slot, template):
        pod = self._pod(slot['pod_name'])
        if pod is None:
            return {'exists': False, 'ready': False, 'endpoint_verified': False, 'pod_uid': None,
                    'endpoint_revision': None, 'observed_at': self.clock()}
        expected = next(c for c in template['pod']['spec']['containers'] if c['name'] == template['container'])
        actual = [c for c in pod.get('spec', {}).get('containers', []) if c.get('name') == template['container']]
        if (len(actual) != 1 or any(actual[0].get(k) != expected.get(k) for k in ('image', 'command', 'args'))):
            raise ActuationBlocked('Native application image or process differs from approved template')
        if (pod.get('metadata', {}).get('deletionTimestamp') or pod.get('status', {}).get('phase') != 'Running'
                or not pod.get('status', {}).get('containerStatuses')):
            if not pod.get('metadata', {}).get('uid'):
                raise ActuationBlocked('Native pending application UID unavailable')
            return {'exists': True, 'ready': False, 'endpoint_verified': False,
                'pod_uid': pod['metadata']['uid'], 'pod_resource_version': pod['metadata'].get('resourceVersion'),
                'node_id': pod.get('spec', {}).get('nodeName'), 'container_id': None,
                'endpoint_revision': None, 'observed_at': self.clock()}
        identity = self._identity(pod, template['container'])
        service_rows = self.client.call('get', 'services', '--field-selector', 'metadata.name=' + slot['service_name'], '-o', 'json')['items']
        if len(service_rows) != 1:
            return dict(identity, exists=True, ready=False, endpoint_verified=False, endpoint_revision=None, observed_at=self.clock())
        service = service_rows[0]
        selectors = service.get('spec', {}).get('selector', {})
        labels = pod['metadata'].get('labels', {})
        protocol = template['protocol'].upper()
        ports = [r for r in service['spec'].get('ports', []) if r.get('port') == template['port'] and r.get('protocol', 'TCP') == protocol]
        slices = self.client.call('get', 'endpointslices', '-l', 'kubernetes.io/service-name=' + slot['service_name'], '-o', 'json')['items']
        endpoints = [(ep, sl) for sl in slices for ep in sl.get('endpoints', []) if ep.get('conditions', {}).get('ready') is True]
        exact = [(ep, sl) for ep, sl in endpoints if ep.get('targetRef', {}).get('uid') == identity['pod_uid']]
        verified = (bool(selectors) and all(labels.get(k) == v for k, v in selectors.items()) and len(ports) == 1
                    and len(exact) == 1 and len(endpoints) == 1 and identity['ready'])
        address = None
        receiver_address = None
        if verified:
            address = str(ipaddress.ip_address(service['spec']['clusterIP']))
            addresses = exact[0][0].get('addresses', [])
            if len(addresses) != 1 or addresses[0] != pod.get('status', {}).get('podIP'):
                verified = False
            else:
                receiver_address = str(ipaddress.ip_address(addresses[0]))
            endpoint_ports = exact[0][1].get('ports', [])
            verified = verified and any(r.get('port') == template['port'] and r.get('protocol', 'TCP') == protocol for r in endpoint_ports)
            if verified and template.get('endpoint_interface'):
                interface = template['endpoint_interface']
                configured = json.loads(pod['metadata'].get('annotations', {}).get('k8s.v1.cni.cncf.io/network-status', '[]'))
                networks = [n for n in configured if n.get('interface') == interface and n.get('name') in
                    (template['network_attachment'], self.client.namespace + '/' + template['network_attachment'])]
                if len(networks) != 1 or len(networks[0].get('ips', [])) != 1:
                    raise ActuationBlocked('Application commissioned N6 attachment is not installed')
                native_ip = str(ipaddress.IPv4Address(networks[0]['ips'][0]))
                expected_networks = json.loads(template['pod']['metadata']['annotations']['k8s.v1.cni.cncf.io/networks'])
                pinned = next(n for n in expected_networks if n.get('name') == template['network_attachment'] and n.get('interface') == interface)
                expected_ip = str(ipaddress.IPv4Interface(slot.get('n6_ip_cidr', pinned['ips'][0])).ip)
                if native_ip != expected_ip:
                    raise ActuationBlocked('Application N6 address differs from commissioned slot')
                argv = ['kubectl', '--context', self.client.context, '--namespace', self.client.namespace,
                    'exec', slot['pod_name'], '-c', template['container'], '--', 'ip', '-4', '-o', 'address', 'show', 'dev', interface]
                observed = self.client.runner(argv, capture_output=True, text=True, timeout=self.client.timeout, check=False)
                ips = re.findall(r'\binet ([0-9.]+)/[0-9]+\b', observed.stdout) if not observed.returncode else []
                if not ips:
                    fallback = argv[:argv.index('--') + 1] + ['python3', '-c', INTERFACE_SCRIPT, interface]
                    observed = self.client.runner(fallback, capture_output=True, text=True, timeout=self.client.timeout, check=False)
                    ips = [observed.stdout.strip()] if not observed.returncode else []
                if not ips:
                    fallback = argv[:argv.index('--') + 1] + ['perl', '-e', INTERFACE_PERL, interface]
                    observed = self.client.runner(fallback, capture_output=True, text=True, timeout=self.client.timeout, check=False)
                    ips = [observed.stdout.strip()] if not observed.returncode else []
                if ips != [native_ip]:
                    raise ActuationBlocked('Application N6 kernel interface address differs')
                address = receiver_address = native_ip
        closing = self._pod(slot['pod_name'])
        if closing is None or self._identity(closing, template['container']) != identity:
            raise ActuationBlocked('Application incarnation changed during endpoint read')
        return dict(identity, exists=True, ready=bool(verified), endpoint_verified=bool(verified),
            endpoint_id=slot['endpoint_id'], address=address, receiver_address=receiver_address, port=template['port'], protocol=protocol.lower(), receiver_pod=slot['pod_name'],
            endpoint_revision=digest([service['metadata'].get('uid'), service['metadata'].get('resourceVersion'),
                [(s['metadata'].get('uid'), s['metadata'].get('resourceVersion')) for s in slices], receiver_address]), observed_at=self.clock())

    def activity(self, slot, template, expected_uid):
        pod = self._pod(slot['pod_name'])
        if pod is None or pod['metadata'].get('uid') != expected_uid:
            raise ActuationBlocked('Application activity Pod changed')
        before = self._identity(pod, template['container'])
        argv = ['kubectl', '--context', self.client.context, '--namespace', self.client.namespace,
                'exec', slot['pod_name'], '-c', template['container'], '--',
                '/bin/sh', '-c', ACTIVITY_SCRIPT, 'activity-read', str(template['port'])]
        try:
            result = self.client.runner(argv, capture_output=True, text=True, timeout=self.client.timeout, check=False)
            if result.returncode or len(result.stdout) > 4096:
                raise ValueError()
            fields = result.stdout.strip().split()
            if len(fields) != 2 or not fields[0].isdigit() or fields[1] not in ('0', '1'):
                raise ValueError()
            value = {'active_connections': int(fields[0]), 'listening': fields[1] == '1'}
            if self._identity(self._pod(slot['pod_name']), template['container']) != before:
                raise ValueError()
            return dict(value, pod_uid=expected_uid, observed_at=self.clock(), source='native-proc-net-tcp')
        except Exception:
            raise ActuationBlocked('Independent application activity unavailable') from None

    def capacity(self, node_id, template):
        nodes = self.client.call('get', 'nodes', '--field-selector', 'metadata.name=' + node_id, '-o', 'json')['items']
        if len(nodes) != 1 or nodes[0].get('spec', {}).get('unschedulable'):
            return False
        node = nodes[0]
        if not any(r.get('type') == 'Ready' and r.get('status') == 'True' for r in node.get('status', {}).get('conditions', [])):
            return False
        cpu, memory = 0, 0
        pods = self.client.call('get', 'pods', '--all-namespaces', '--field-selector', 'spec.nodeName=' + node_id, '-o', 'json')['items']
        for pod in pods:
            if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
                continue
            pod_cpu, pod_memory = 0, 0
            for c in pod['spec'].get('containers', []):
                req = c.get('resources', {}).get('requests', {})
                pod_cpu += _cpu_millis(req.get('cpu', '0'))
                pod_memory += _memory_bytes(req.get('memory', '0'))
            # Conservatively include every init request; restartable sidecars
            # and admission-injected work must not vanish from capacity checks.
            for c in pod['spec'].get('initContainers', []):
                req = c.get('resources', {}).get('requests', {})
                pod_cpu += _cpu_millis(req.get('cpu', '0'))
                pod_memory += _memory_bytes(req.get('memory', '0'))
            overhead = pod['spec'].get('overhead', {})
            cpu += pod_cpu + _cpu_millis(overhead.get('cpu', '0'))
            memory += pod_memory + _memory_bytes(overhead.get('memory', '0'))
        for c in template['pod']['spec']['containers']:
            req = c.get('resources', {}).get('requests', {})
            if not req.get('cpu') or not req.get('memory'):
                return False
            cpu += _cpu_millis(req['cpu'])
            memory += _memory_bytes(req['memory'])
        alloc = node['status']['allocatable']
        return cpu <= _cpu_millis(alloc['cpu']) and memory <= _memory_bytes(alloc['memory'])
