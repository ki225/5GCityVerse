"""Read exact SMF/gtp5g sessions and actual N6 NAT state for service routes."""
import ipaddress
import json
import math
import re
import shlex
import time

from .actuators import ActuationBlocked
from backend.onprem.runtime.ue import seconds


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return seconds(value)


class NativeServiceSessionReader:
    def __init__(self, api, readback, client, targets, *, clock=time.time, maximum_age=15):
        self.api, self.readback, self.client, self.targets = api, readback, client, dict(targets)
        self.clock, self.maximum_age = clock, maximum_age

    def _run(self, pod, container, *command):
        argv = ['kubectl', '--context', self.client.context, '--namespace', self.client.namespace,
            '--request-timeout=5s', 'exec', pod, '-c', container, '--', *command]
        result = self.client.runner(argv, capture_output=True, text=True, timeout=7, check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ActuationBlocked('Bounded native N6 route read unavailable')
        return result.stdout

    def __call__(self, session_key):
        return self._read(session_key)

    def for_endpoint(self, session_key, address):
        return self._read(session_key, str(ipaddress.IPv4Address(address)))

    def _read(self, session_key, destination=None):
        census = self.api.active_inventory()
        if (census.get('source') != 'native-smf-active-sessions' or census.get('complete') is not True
                or not census.get('smf_boot_epoch') or not 0 <= self.clock() - _stamp(census.get('observed_at')) <= self.maximum_age):
            raise ActuationBlocked('Fresh complete SMF census required')
        matches = [r for r in census.get('sessions', ()) if r.get('session_key') == session_key]
        if len(matches) != 1:
            raise ActuationBlocked('Exact native route session required')
        native = matches[0]
        live_upfs = {r.get('target_id'): r.get('upf_id') for r in census.get('upfs', ())}
        selected = [(target, spec) for target, spec in self.targets.items()
            if spec.get('native_upf_id', live_upfs.get(target)) == native.get('upf_id')]
        if len(selected) != 1:
            raise ActuationBlocked('Native route UPF is not uniquely commissioned')
        target, spec = selected[0]
        pod_name = self.readback._pod_name(target)
        pod = self.client.call('get', 'pod', pod_name, '-o', 'json')
        uid = pod['metadata']['uid']
        def incarnation(value):
            statuses=[r for r in value.get('status',{}).get('containerStatuses',()) if r.get('name')==spec['container']]
            if (len(statuses)!=1 or not statuses[0].get('containerID') or type(statuses[0].get('restartCount')) is not int
                    or not statuses[0].get('state',{}).get('running',{}).get('startedAt')):
                raise ActuationBlocked('Native UPF container incarnation evidence unavailable')
            row=statuses[0]
            return row['containerID'],row['restartCount'],row['state']['running']['startedAt']
        before_incarnation=incarnation(pod)
        kernel = self.readback.kernel(target, uid)
        if kernel.get('epoch') != native.get('upfEpoch'):
            raise ActuationBlocked('UPF kernel incarnation differs from SMF census')
        sessions = [r for r in kernel.get('sessions', ()) if str(r.get('pfcpSeid')) == str(native.get('pfcpSeid'))
            and r.get('evidence') == 'kernel-installed-pdr-far']
        if len(sessions) != 1:
            raise ActuationBlocked('Exact installed PDR/FAR path required')
        pdrs = [r for r in kernel.get('pdrs', ()) if str(r.get('seid', r.get('pfcpSeid'))) == str(native['pfcpSeid'])]
        if (len(pdrs)<2 or any(type(r.get('id')) is not int or type(r.get('farId')) is not int for r in pdrs)
                or len({r['id'] for r in pdrs})!=len(pdrs)):
            raise ActuationBlocked('Exact installed native PDR/FAR identifiers required')
        pdr_ids=tuple(sorted(r['id'] for r in pdrs))
        far_ids=tuple(sorted({r['farId'] for r in pdrs}))
        interface = spec.get('n6_interface', 'n6')
        container = spec['container']
        if any(re.fullmatch(r'[a-zA-Z0-9_.-]{1,63}', value) is None for value in (interface, container)):
            raise ActuationBlocked('Pinned native N6 interface/container required')
        addresses = json.loads(self._run(pod_name, container, 'ip', '-j', '-4', 'address', 'show', 'dev', interface))
        actual = [r['local'] for row in addresses for r in row.get('addr_info', ()) if r.get('family') == 'inet']
        if len(actual) != 1:
            raise ActuationBlocked('Unique native N6 IPv4 address required')
        n6_ip = str(ipaddress.IPv4Address(actual[0]))
        if destination is not None:
            route = json.loads(self._run(pod_name, container, 'ip', '-j', '-4', 'route', 'get', destination))
            if len(route) != 1 or route[0].get('dev') != interface or route[0].get('prefsrc', route[0].get('src')) != n6_ip:
                raise ActuationBlocked('Application endpoint is not on the commissioned native N6 path')
        rules = self._run(pod_name, container, 'iptables-save', '-t', 'nat')
        ue_ip = str(ipaddress.IPv4Address(sessions[0].get('ip')))
        peer = None
        # Accept only a plain complete MASQUERADE rule for this UE source and
        # exact N6 interface. Unknown NAT chains/options never become evidence.
        for line in rules.splitlines():
            fields = shlex.split(line)
            if fields[:2] != ['-A', 'POSTROUTING'] or '-s' not in fields or '-o' not in fields or '-j' not in fields:
                continue
            if len(fields) != 8:
                continue
            if (ipaddress.IPv4Address(ue_ip) in ipaddress.IPv4Network(fields[fields.index('-s') + 1], strict=False)
                    and fields[fields.index('-o') + 1] == interface and fields[fields.index('-j') + 1] == 'MASQUERADE'):
                if peer is not None:
                    raise ActuationBlocked('Ambiguous native N6 NAT route')
                peer = n6_ip
        if peer is None:
            raise ActuationBlocked('Exact native N6 MASQUERADE rule unavailable')
        closing = self.client.call('get', 'pod', pod_name, '-o', 'json')
        final = self.api.active_inventory()
        if (closing['metadata']['uid'] != uid or incarnation(closing)!=before_incarnation or final.get('source') != 'native-smf-active-sessions'
                or final.get('complete') is not True or final.get('smf_boot_epoch') != census['smf_boot_epoch']
                or final.get('census_revision') != census.get('census_revision')
                or not 0 <= self.clock() - _stamp(final.get('observed_at')) <= self.maximum_age
                or [r for r in final.get('sessions', ()) if r.get('session_key') == session_key] != [native]
                or not 0 <= self.clock() - _stamp(kernel.get('observedAt')) <= self.maximum_age):
            raise ActuationBlocked('Native route evidence changed during read')
        final_kernel=self.readback.kernel(target,uid)
        if final_kernel.get('epoch')!=kernel['epoch']:
            raise ActuationBlocked('Native UPF kernel changed during route read')
        return {'source': 'native-smf+gtp5g-pdr-far+n6-nat/v1', 'session_key': session_key,
            'upf_target_id': target, 'upf_epoch': kernel['epoch'], 'pfcp_seid': str(native['pfcpSeid']),
            'path_revision': ':'.join((census['smf_boot_epoch'], native['upf_id'], kernel['epoch'], str(native['pfcpSeid']))),
            'pdr_far_verified': True, 'pdr_ids': pdr_ids, 'far_ids': far_ids, 'receiver_peer': peer, 'pod_uid': uid,
            'observed_at': min(_stamp(census['observed_at']), _stamp(kernel['observedAt']))}
