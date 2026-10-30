"""Native application lifecycle server for fixed, operator approved receivers.

No requests supply manifests, addresses or commands. An application is one
UID-fenced Pod and one Service; relocation creates another commissioned slot.
"""
from copy import deepcopy
from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from .actuators import ActuationBlocked
from .application_journal import ApplicationJournal, command_key, digest
from .application_readback import ApplicationReadback
from .resource_adapters import KubectlResourceClient, _token_file_reader


class ApplicationDeletionPending(ActuationBlocked):
    pass


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', value):
        raise ValueError('Fixed application identity required')
    return value


def controlled_receiver_template(image=None, *, port=5201, network_attachment=None, n6_ip_cidr=None):
    """An actual idle iperf3 receiver; its native socket census is observable.

    Use a digest already approved and cached by the infrastructure. This does
    not select an image from an AI proposal or create any Kubernetes resource.
    """
    from backend.onprem.runtime.traffic_manager import traffic_image
    image = traffic_image() if image is None else image
    result = {'container': 'receiver', 'port': port, 'protocol': 'UDP',
        'pod': {'apiVersion': 'v1', 'kind': 'Pod', 'spec': {'containers': [{
            'name': 'receiver', 'image': image, 'command': ['iperf3', '-s', '-p', str(port)],
            'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                'capabilities': {'drop': ['ALL']}},
            'readinessProbe': {'tcpSocket': {'port': port}, 'periodSeconds': 2, 'timeoutSeconds': 1},
            'resources': {'requests': {'cpu': '50m', 'memory': '32Mi'}, 'limits': {'cpu': '1', 'memory': '128Mi'}}}]}},
        'service': {'apiVersion': 'v1', 'kind': 'Service', 'spec': {'ports': [
            {'name': 'control', 'port': port, 'targetPort': port, 'protocol': 'TCP'},
            {'name': 'data', 'port': port, 'targetPort': port, 'protocol': 'UDP'}]}}}
    if network_attachment is not None:
        _name(network_attachment)
        import ipaddress
        address = str(ipaddress.IPv4Interface(n6_ip_cidr))
        result.update(endpoint_interface='n6', network_attachment=network_attachment)
        result['pod']['metadata'] = {'annotations': {'k8s.v1.cni.cncf.io/networks': json.dumps([
            {'name': network_attachment, 'interface': 'n6', 'ips': [address]}])}}
    return result


class ApplicationLifecycleController:
    def __init__(self, client, journal, *, templates, slots, binding_reader, readback=None, clock=time.time):
        self.client, self.clock, self.bindings = client, clock, binding_reader
        self.journal = ApplicationJournal(journal)
        self.readback = readback or ApplicationReadback(client, clock=clock)
        self.templates, self.slots = deepcopy(templates), deepcopy(slots)
        if not self.templates or not self.slots:
            raise ValueError('Fixed application templates and slots required')
        for key, template in self.templates.items():
            _name(key)
            optional_network = {'endpoint_interface', 'network_attachment'} if 'endpoint_interface' in template else set()
            if (set(template) != {'pod', 'service', 'container', 'port', 'protocol'} | optional_network
                    or template['protocol'] not in ('TCP', 'UDP') or type(template['port']) is not int or not 1 <= template['port'] <= 65535):
                raise ValueError('Approved controlled receiver template required')
            if optional_network:
                _name(template['network_attachment'])
                if template['endpoint_interface'] != 'n6':
                    raise ValueError('Only a fixed commissioned N6 interface is supported')
            _name(template['container'])
            pod = template['pod']
            if (pod.get('kind') != 'Pod' or template['service'].get('kind') != 'Service'
                    or pod['spec'].get('hostNetwork') or pod['spec'].get('hostPID')
                    or pod['spec'].get('initContainers') or pod['spec'].get('volumes')
                    or len(pod['spec'].get('containers', [])) != 1):
                raise ValueError('Only isolated single process controlled applications are commissioned')
            container = pod['spec']['containers'][0]
            security = container.get('securityContext', {})
            if (container.get('name') != template['container'] or not re.fullmatch(r'[^@]+@sha256:[a-f0-9]{64}', container.get('image', ''))
                    or security.get('privileged') or security.get('allowPrivilegeEscalation') is not False
                    or security.get('capabilities', {}).get('drop') != ['ALL']
                    or not container.get('readinessProbe') or not container.get('resources', {}).get('requests')):
                raise ValueError('Pinned bounded application with readiness probe and least privilege required')
        for target, slot in self.slots.items():
            _name(target)
            extra = {'n6_ip_cidr'} if 'n6_ip_cidr' in slot else set()
            if (set(slot) != {'template_id', 'node_ids', 'pod_name', 'service_name', 'endpoint_id'} | extra
                    or slot['template_id'] not in self.templates or not slot['node_ids']):
                raise ValueError('Fixed application slot required')
            if extra:
                import ipaddress
                ipaddress.IPv4Interface(slot['n6_ip_cidr'])
                if not self.templates[slot['template_id']].get('endpoint_interface'):
                    raise ValueError('Slot N6 address requires commissioned interface template')
            for field in ('pod_name', 'service_name', 'endpoint_id'):
                _name(slot[field])
            for node in slot['node_ids']:
                _name(node)
        for field in ('pod_name', 'service_name', 'endpoint_id'):
            if len({s[field] for s in self.slots.values()}) != len(self.slots):
                raise ValueError('Application slot native identities must be unique')
        native_addresses = set()
        for slot in self.slots.values():
            template = self.templates[slot['template_id']]
            if template.get('endpoint_interface'):
                import ipaddress
                networks = json.loads(template['pod'].get('metadata', {}).get('annotations', {}).get('k8s.v1.cni.cncf.io/networks', '[]'))
                matches = [n for n in networks if n.get('name') == template['network_attachment'] and n.get('interface') == 'n6']
                if len(matches) != 1 or len(matches[0].get('ips', [])) != 1:
                    raise ValueError('Exact N6 template attachment address required')
                address = str(ipaddress.IPv4Interface(slot.get('n6_ip_cidr', matches[0]['ips'][0])).ip)
                key = (template['network_attachment'], address)
                if key in native_addresses:
                    raise ValueError('Application slots cannot share a static N6 address')
                native_addresses.add(key)

    def _slot(self, target):
        _name(target)
        if target not in self.slots:
            raise ActuationBlocked('Application target is not commissioned')
        slot = self.slots[target]
        return slot, self.templates[slot['template_id']]

    def _refs(self, target):
        if self.bindings is None:
            raise ActuationBlocked('Native endpoint reference registry unavailable')
        proof = self.bindings.inspect_application(target)
        if (not isinstance(proof, dict) or not proof.get('version')
                or type(proof.get('references')) is not int or proof['references'] < 0
                or type(proof.get('accepting')) is not bool
                or type(proof.get('observed_at')) not in (int, float)
                or not -1 <= self.clock() - proof['observed_at'] <= 15):
            raise ActuationBlocked('Fresh native endpoint references unavailable')
        return proof

    def endpoint(self, target):
        slot, template = self._slot(target)
        state = self.readback.endpoint(slot, template)
        record = self.journal.target(target)
        if state.get('exists'):
            self._owned_pod(target, record, expected_uid=state['pod_uid'])
        return dict(state, target_id=target, owner_id=record['owner_id'], operation_id=record['operation_id'])

    def inspect(self, target):
        slot, template = self._slot(target)
        record = self.journal.target(target)
        native = self.readback.endpoint(slot, template)
        refs = self._refs(target)
        activity = None
        if native.get('exists'):
            self._owned_pod(target, record, expected_uid=native['pod_uid'])
            if refs.get('pod_uid') not in (None, native['pod_uid']):
                raise ActuationBlocked('Application endpoint references belong to another Pod')
            try:
                activity = self.readback.activity(slot, template, native['pod_uid'])
            except ActuationBlocked:
                pass  # Unknown is null; it can never authorize remove/restore.
        value = dict(native, source='native-application-controller', target_id=target,
            journal_revision=record['revision'],
            owner_id=record['owner_id'], operation_id=record['operation_id'], template_id=slot['template_id'],
            active_connections=activity['active_connections'] if activity else (0 if native.get('exists') is False else None),
            endpoint_references=refs['references'], accepting=refs['accepting'],
            activity_ref=digest(activity) if activity else None, binding_version=refs['version'],
            readback_pending=bool(record['busy']) or native.get('exists') is True and native.get('ready') is not True, cleanup_pending=False)
        if native.get('exists') is False:
            value['node_id'] = record.get('node_id')
        value['placement_capacity_verified'] = any(self.readback.capacity(node, template) for node in slot['node_ids']) if not native.get('exists') else True
        value['version'] = digest([record, {k: v for k, v in native.items() if k != 'observed_at'}, refs['version'], value['active_connections']])
        return value

    def capabilities(self):
        configured = (self.bindings is not None and callable(getattr(self.bindings, 'begin_drain_application', None))
                      and callable(getattr(self.bindings, 'set_accepting', None)))
        if configured:
            try:
                for target, slot in self.slots.items():
                    self._refs(target)
                    if not any(self.readback.capacity(node, self.templates[slot['template_id']]) for node in slot['node_ids']):
                        configured = False
                        break
            except Exception:
                configured = False
        return dict(verified=configured, compare_and_swap=True, place_application=configured, drain_application=configured,
                    remove_application=configured, observed_at=self.clock(), source='native-application-controller')

    def _authority(self, authority):
        if set(authority) != {'owner_id', 'operation_id', 'expected_version'} or any(not isinstance(v, str) or not v or len(v) > 256 for v in authority.values()):
            raise ValueError('Exact application authority required')

    def _replay(self, target, action, authority, parameters):
        self._authority(authority)
        key = command_key(target, action, authority, parameters)
        for previous_key, command in self.journal.commands():
            if previous_key == key:
                if command['authority'] != authority or command['parameters'] != parameters:
                    raise ActuationBlocked('Application command replay differs')
                return dict(accepted=True, target_id=target, readback_pending=command['phase'] not in ('deployed', 'removed', 'drained', 'restored'), replay=True)
        return None

    def _before(self, target, authority):
        self._authority(authority)
        before = self.inspect(target)
        if before['version'] != authority['expected_version'] or before['owner_id'] not in (None, authority['owner_id']):
            raise ActuationBlocked('Application native version or owner changed')
        return before

    def _owned_pod(self, target, record, *, expected_uid=None):
        slot, _ = self._slot(target)
        pod = self.readback._pod(slot['pod_name'])
        if pod is None:
            raise ActuationBlocked('Owned application Pod disappeared')
        labels = pod['metadata'].get('labels', {})
        pins = [r for r in record['resources'] if r['kind'] == 'Pod']
        creator = pins[0] if pins else record
        if (labels.get('cityverse.io/application-target') != target
                or labels.get('cityverse.io/application-owner') != digest(creator['owner_id'])[:32]
                or labels.get('cityverse.io/application-operation') != digest(creator['operation_id'])[:32]
                or expected_uid is not None and pod['metadata']['uid'] != expected_uid):
            raise ActuationBlocked('Exact application Pod ownership unavailable')
        if pins and (len(pins) != 1 or pins[0]['uid'] != pod['metadata']['uid']):
            raise ActuationBlocked('Application Pod UID replaced')
        return pod

    def deploy(self, target, *, template_id, node_id, **authority):
        slot, template = self._slot(target)
        parameters = dict(template_id=template_id, node_id=node_id)
        replay = self._replay(target, 'deploy', authority, parameters)
        if replay:
            return replay
        before = self._before(target, authority)
        if (before['exists'] is not False or template_id != slot['template_id'] or node_id not in slot['node_ids']
                or not self.readback.capacity(node_id, template)):
            raise ActuationBlocked('Absent application and approved native capacity required')
        # An existing Service at the commissioned name is never adopted.
        if self.client.call('get', 'services', '--field-selector', 'metadata.name=' + slot['service_name'], '-o', 'json')['items']:
            raise ActuationBlocked('Commissioned application Service already exists')
        key, command, fresh = self.journal.claim(target, 'deploy', authority, parameters, before)
        labels = {'cityverse.io/application-target': target, 'cityverse.io/application-owner': digest(authority['owner_id'])[:32],
                  'cityverse.io/application-operation': digest(authority['operation_id'])[:32]}
        resources = []
        try:
            for kind, name, body in [('Pod', slot['pod_name'], template['pod']), ('Service', slot['service_name'], template['service'])]:
                manifest = deepcopy(body)
                annotations = deepcopy(manifest.get('metadata', {}).get('annotations', {}))
                if kind == 'Pod' and slot.get('n6_ip_cidr'):
                    networks = json.loads(annotations['k8s.v1.cni.cncf.io/networks'])
                    for network in networks:
                        if network['name'] == template['network_attachment'] and network['interface'] == 'n6':
                            network['ips'] = [slot['n6_ip_cidr']]
                    annotations['k8s.v1.cni.cncf.io/networks'] = json.dumps(networks)
                manifest['metadata'] = {'name': name, 'namespace': self.client.namespace, 'labels': labels, 'annotations': annotations}
                if kind == 'Pod':
                    # Preserve scheduler capacity admission while pinning the
                    # exact commissioned node; nodeName would bypass it.
                    manifest['spec'].pop('nodeName', None)
                    manifest['spec'].setdefault('affinity', {})['nodeAffinity'] = {
                        'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
                            {'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [node_id]}]}]}}
                    manifest['spec']['automountServiceAccountToken'] = False
                    manifest['spec']['restartPolicy'] = 'Never'
                else:
                    manifest['spec']['selector'] = labels
                self.journal.advance(key, 'dispatching_' + kind.lower(), resources=resources)
                actual = self.client.call('create', '-f', '-', '-o', 'json', body=manifest)
                if actual.get('metadata', {}).get('name') != name or not actual['metadata'].get('uid'):
                    raise ActuationBlocked('Application create acknowledgement unavailable')
                resources.append({'kind': kind, 'name': name, 'uid': actual['metadata']['uid'], 'owner_id': authority['owner_id'], 'operation_id': authority['operation_id']})
                self.journal.advance(key, 'created_' + kind.lower(), resources=resources)
            with self.journal.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                record = self.journal.target(target, db)
                record['node_id'] = node_id
                self.journal.save(target, record, db)
            self.journal.advance(key, 'deployed', resources=resources, finish=True)
        except Exception:
            self.journal.advance(key, 'recovery_required', resources=resources)
            raise ActuationBlocked('Application deployment requires independent readback') from None
        return dict(accepted=True, target_id=target, service_effect_verified=False)

    def drain(self, target, **authority):
        replay = self._replay(target, 'drain', authority, {})
        if replay:
            return replay
        before = self._before(target, authority)
        if before['owner_id'] != authority['owner_id']:
            raise ActuationBlocked('Drain requires owned application')
        key, _, _ = self.journal.claim(target, 'drain', authority, {}, before)
        try:
            self.bindings.begin_drain_application(target, owner_id=authority['owner_id'], operation_id=authority['operation_id'], expected_version=before['binding_version'])
            proof = self._refs(target)
            if proof['accepting']:
                raise ActuationBlocked('Application admission drain unconfirmed')
            self.journal.advance(key, 'drained', finish=True)
        except Exception:
            self.journal.advance(key, 'recovery_required')
            raise
        return dict(accepted=True, target_id=target, draining=True)

    def _delete_resources(self, target, record, key):
        # Delete the Service before its Pod, always with native UID and RV CAS.
        for pin in sorted(record['resources'], key=lambda r: r['kind'] != 'Service'):
            plural = 'services' if pin['kind'] == 'Service' else 'pods'
            rows = self.client.call('get', plural, '--field-selector', 'metadata.name=' + pin['name'], '-o', 'json')['items']
            if not rows:
                continue
            if len(rows) != 1 or rows[0]['metadata'].get('uid') != pin['uid']:
                raise ActuationBlocked('Application native resource UID changed')
            native = rows[0]
            expected = {'cityverse.io/application-target': target,
                'cityverse.io/application-owner': digest(pin['owner_id'])[:32],
                'cityverse.io/application-operation': digest(pin['operation_id'])[:32]}
            if not all(native['metadata'].get('labels', {}).get(k) == v for k, v in expected.items()):
                raise ActuationBlocked('Application native resource ownership changed')
            body = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions':
                {'uid': pin['uid'], 'resourceVersion': native['metadata']['resourceVersion']}}
            route = '/api/v1/namespaces/' + self.client.namespace + '/' + plural + '/' + pin['name']
            self.journal.advance(key, 'deleting_' + pin['kind'].lower())
            self.client.call('delete', '--raw', route, '-f', '-', body=body)
            if self.client.call('get', plural, '--field-selector', 'metadata.name=' + pin['name'], '-o', 'json')['items']:
                raise ApplicationDeletionPending('Application native deletion pending')
        slot, _ = self._slot(target)
        for plural, name in [('pods', slot['pod_name']), ('services', slot['service_name'])]:
            if self.client.call('get', plural, '--field-selector', 'metadata.name=' + name, '-o', 'json')['items']:
                raise ActuationBlocked('Application removal native census is not empty')

    def remove(self, target, **authority):
        replay = self._replay(target, 'remove', authority, {})
        if replay:
            return replay
        before = self._before(target, authority)
        if (before['owner_id'] != authority['owner_id'] or before['accepting'] is not False
                or before['active_connections'] != 0 or before['endpoint_references'] != 0):
            raise ActuationBlocked('Application must drain native activity and endpoint references')
        key, _, _ = self.journal.claim(target, 'remove', authority, {}, before)
        try:
            # Re-read native activity and durable refs after the deletion claim.
            latest = self.inspect(target)
            if latest['active_connections'] != 0 or latest['endpoint_references'] != 0 or latest['accepting'] is not False:
                raise ActuationBlocked('Application acquired activity during removal')
            self._delete_resources(target, self.journal.target(target), key)
            self.journal.advance(key, 'removed', resources=[], finish=True)
        except ApplicationDeletionPending:
            self.journal.advance(key, 'cleanup_pending')
            return dict(accepted=True, target_id=target, cleanup_pending=True, readback_pending=True)
        except Exception:
            self.journal.advance(key, 'recovery_required')
            raise
        return dict(accepted=True, target_id=target, service_effect_verified=False)

    def restore(self, target, before, **authority):
        parameters = {'before_digest': digest(before), 'before_version': before.get('version')}
        replay = self._replay(target, 'restore', authority, parameters)
        if replay:
            return replay
        actual = self._before(target, authority)
        if actual['readback_pending'] and self.journal.target(target)['busy']:
            record = self.journal.target(target)
            slot, _ = self._slot(target)
            pins = {(r['kind'], r['name']): r['uid'] for r in record['resources']}
            for kind, plural, name in [('Pod', 'pods', slot['pod_name']), ('Service', 'services', slot['service_name'])]:
                rows = self.client.call('get', plural, '--field-selector', 'metadata.name=' + name, '-o', 'json')['items']
                if rows and (len(rows) != 1 or pins.get((kind, name)) != rows[0]['metadata'].get('uid')):
                    raise ActuationBlocked('Read back uncertain application resources before restoration')
        def baseline_matches(saved):
            fields = ('source', 'target_id', 'version', 'exists', 'pod_uid', 'owner_id', 'operation_id', 'node_id', 'template_id', 'accepting')
            return all(before.get(k) == saved.get(k) for k in fields)
        if before.get('exists') is True:
            drains = [c for _, c in self.journal.commands() if c['target_id'] == target and c['action'] == 'drain'
                and c['authority']['owner_id'] == authority['owner_id'] and c['authority']['operation_id'] == authority['operation_id']
                and c['phase'] == 'drained' and baseline_matches(c['before'])]
            if (len(drains) != 1 or actual.get('pod_uid') != before.get('pod_uid')
                    or actual.get('endpoint_revision') != before.get('endpoint_revision')):
                raise ActuationBlocked('Exact unchanged drain baseline required')
            key, _, _ = self.journal.claim(target, 'restore', authority, parameters,
                                          dict(before, journal_revision=actual['journal_revision']))
            try:
                self.bindings.set_accepting(target, before['accepting'], authority['owner_id'],
                                             authority['operation_id'], actual['binding_version'])
                self.journal.advance(key, 'restored', finish=True)
            except Exception:
                self.journal.advance(key, 'recovery_required')
                raise
            return dict(accepted=True, restored=True, target_id=target, service_effect_verified=False)
        # Only undo this operation's newly created idle application. Removed
        # existing processes cannot be resurrected with their original UID.
        originals = [c for _, c in self.journal.commands() if c['target_id'] == target and c['action'] == 'deploy'
                     and c['authority']['owner_id'] == authority['owner_id'] and c['authority']['operation_id'] == authority['operation_id']]
        if len(originals) != 1 or not baseline_matches(originals[0]['before']) or before.get('exists') is not False:
            raise ActuationBlocked('Exact original application deployment baseline required')
        if actual['exists']:
            if actual['active_connections'] != 0 or actual['endpoint_references'] != 0:
                raise ActuationBlocked('Application restoration must wait for native drain')
        claim_baseline = dict(before, journal_revision=actual['journal_revision'])
        if actual['readback_pending']:
            claim_baseline['journal_revision'] = self.journal.release_for_compensation(target, authority, actual['journal_revision'])
        key, _, _ = self.journal.claim(target, 'restore', authority, parameters, claim_baseline)
        try:
            if actual['exists'] and actual['accepting']:
                self.journal.advance(key, 'restoration_draining')
                self.bindings.begin_drain_application(target, owner_id=authority['owner_id'], operation_id=authority['operation_id'], expected_version=actual['binding_version'])
            latest = self.inspect(target)
            if (latest['active_connections'] != 0 or latest['endpoint_references'] != 0
                    or latest['exists'] and latest['accepting'] is not False):
                raise ActuationBlocked('Application restoration drain unconfirmed')
            self._delete_resources(target, self.journal.target(target), key)
            self.journal.advance(key, 'restored', resources=[], finish=True)
        except Exception:
            self.journal.advance(key, 'recovery_required')
            raise
        return dict(accepted=True, restored=True, target_id=target, service_effect_verified=False)

    def recover_pending(self):
        # Readback only. A process crash cannot authorize another create/delete.
        for key, command in self.journal.commands():
            if command['phase'] in ('deployed', 'removed', 'restored', 'drained'):
                continue
            target = command['target_id']
            record = self.journal.target(target)
            if record['busy'] != key:
                continue
            slot, template = self._slot(target)
            if command['action'] == 'restore' and command['before'].get('exists') is True:
                before = command['before']
                native = self.readback.endpoint(slot, template)
                references = self._refs(target)
                if (native.get('pod_uid') == before.get('pod_uid')
                        and native.get('endpoint_revision') == before.get('endpoint_revision')
                        and native.get('ready') == before.get('ready')
                        and references['accepting'] == before.get('accepting')):
                    self._owned_pod(target, record, expected_uid=native['pod_uid'])
                    self.journal.advance(key, 'restored', finish=True)
                continue
            if command['action'] in ('remove', 'restore'):
                absent = True
                for pin in record['resources']:
                    plural = 'services' if pin['kind'] == 'Service' else 'pods'
                    rows = self.client.call('get', plural, '--field-selector', 'metadata.name=' + pin['name'], '-o', 'json')['items']
                    if rows:
                        if len(rows) != 1 or rows[0]['metadata'].get('uid') != pin['uid']:
                            raise ActuationBlocked('Application removal readback UID changed')
                        absent = False
                if absent and self._refs(target)['references'] == 0:
                    self.journal.advance(key, 'removed' if command['action'] == 'remove' else 'restored', resources=[], finish=True)
                continue
            if command['action'] == 'deploy':
                pod = self.readback._pod(slot['pod_name'])
                if pod:
                    self._owned_pod(target, record, expected_uid=pod['metadata']['uid'])
                recovered = []
                for kind, plural, name in [('Pod', 'pods', slot['pod_name']), ('Service', 'services', slot['service_name'])]:
                    rows = self.client.call('get', plural, '--field-selector', 'metadata.name=' + name, '-o', 'json')['items']
                    if not rows:
                        continue
                    labels = rows[0]['metadata'].get('labels', {})
                    if (len(rows) != 1 or labels.get('cityverse.io/application-target') != target
                            or labels.get('cityverse.io/application-owner') != digest(command['authority']['owner_id'])[:32]
                            or labels.get('cityverse.io/application-operation') != digest(command['authority']['operation_id'])[:32]):
                        raise ActuationBlocked('Application recovery resource belongs to another command')
                    recovered.append(dict(kind=kind, name=name, uid=rows[0]['metadata']['uid'],
                        owner_id=command['authority']['owner_id'], operation_id=command['authority']['operation_id']))
                endpoint = self.readback.endpoint(slot, template)
                if endpoint.get('ready') and len(recovered) == 2:
                    self.journal.advance(key, 'deployed', resources=recovered, finish=True)
                else:
                    self.journal.advance(key, 'recovery_required', resources=recovered)

    def poll_readback(self, target):
        self.recover_pending()
        return self.inspect(target)


def create_application_controller_app(controller, *, token_reader):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    @asynccontextmanager
    async def lifespan(_):
        await asyncio.to_thread(controller.recover_pending)
        yield

    async def dispatch(request):
        try:
            token = token_reader()
        except Exception:
            return JSONResponse({'error': 'authority_not_configured'}, status_code=503)
        if not token or not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'error': 'forbidden'}, status_code=403)
        try:
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 65536:
                    raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            route = request.path_params['route']
            if route == 'capabilities':
                if value != {'subsystem': 'applications'}:
                    raise ValueError()
                result = await asyncio.to_thread(controller.capabilities)
            else:
                names = {'applications/read': 'poll_readback', 'applications/endpoint-read': 'endpoint', 'applications/deploy': 'deploy',
                         'applications/drain': 'drain', 'applications/remove': 'remove', 'applications/restore': 'restore'}
                if route not in names:
                    return JSONResponse({'error': 'not_found'}, status_code=404)
                fields = {'target_id'}
                if names[route] not in ('poll_readback', 'endpoint'):
                    fields |= {'owner_id', 'operation_id', 'expected_version'}
                if names[route] == 'deploy':
                    fields |= {'template_id', 'node_id'}
                if names[route] == 'restore':
                    fields |= {'before'}
                if set(value) != fields:
                    raise ValueError()
                target = _name(value.pop('target_id'))
                result = await asyncio.to_thread(getattr(controller, names[route]), target, **value)
            pending = route == 'applications/read' and result.get('readback_pending') is True
            return JSONResponse(result, status_code=202 if pending else 200, headers={'Cache-Control': 'no-store'})
        except ActuationBlocked:
            return JSONResponse({'error': 'native_state_or_precondition_unavailable'}, status_code=409)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, RecursionError):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
    return Starlette(routes=[Route('/cityverse-resource/v1/{route:path}', dispatch, methods=['POST'])], lifespan=lifespan)


def configured_application_controller():
    path = Path(os.environ.get('CITYVERSE_APPLICATION_CONTROLLER_CONFIG_FILE', ''))
    expected = os.environ.get('CITYVERSE_APPLICATION_CONTROLLER_CONFIG_SHA256', '')
    if not path.is_absolute() or path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Pinned application controller config required')
    raw = path.read_bytes()
    if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Application controller config digest mismatch')
    config = json.loads(raw)
    if set(config) != {'schema', 'context', 'namespace', 'journal', 'templates', 'slots', 'controller_token_file', 'binding_registry'} or config['schema'] != 'cityverse.application-controller/v1':
        raise ValueError('Application controller configuration schema invalid')
    client = KubectlResourceClient(context=config['context'], namespace=config['namespace'])
    from .service_routes import ServiceRouteRegistry
    # endpoint-native reader is independent of route references, avoiding a
    # controller -> registry -> controller inspection recursion.
    readback = ApplicationReadback(client)
    def native_reader(target):
        slot = config['slots'][target]
        return dict(readback.endpoint(slot, config['templates'][slot['template_id']]), target_id=target)
    bindings = ServiceRouteRegistry(config['binding_registry'], native_reader=native_reader)
    controller = ApplicationLifecycleController(client, config['journal'], templates=config['templates'], slots=config['slots'], binding_reader=bindings, readback=readback)
    return create_application_controller_app(controller, token_reader=_token_file_reader(config['controller_token_file']))
