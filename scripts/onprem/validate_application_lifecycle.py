"""Isolated native application lifecycle acceptance; no existing NF is changed.

Requires an explicit context/node and an already approved digest image. Only
the exact dedicated slot created by this run may be removed. Output contains
native versions and UID evidence, never bearer credentials or manifests.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.onprem.intents.application_controller import (ApplicationLifecycleController,
    controlled_receiver_template, create_application_controller_app)
from backend.onprem.intents.application_readback import ApplicationReadback
from backend.onprem.intents.resource_adapters import KubectlResourceClient
from backend.onprem.intents.service_routes import ServiceRouteRegistry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', default='free5gc-onprem')
    parser.add_argument('--node', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--network-attachment')
    parser.add_argument('--n6-ip-cidr')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if bool(args.network_attachment) != bool(args.n6_ip_cidr):
        raise SystemExit('Fixed commissioned N6 attachment and address must be supplied together')
    if any(t in args.context.lower() for t in ('eks', 'arn:', 'docker-desktop', 'minikube')):
        raise SystemExit('Explicit approved native lab context required')
    client = KubectlResourceClient(context=args.context, namespace=args.namespace, timeout=5)
    target = 'receiver-live'
    slot = {'template_id': 'receiver', 'node_ids': [args.node], 'pod_name': 'cv-app-live-1',
            'service_name': 'cv-app-live-1', 'endpoint_id': 'cv-app-live-1'}
    for plural, name in [('pods', slot['pod_name']), ('services', slot['service_name'])]:
        if client.call('get', plural, '--field-selector', 'metadata.name=' + name, '-o', 'json')['items']:
            raise SystemExit('Dedicated application slot already exists; do not adopt or delete it')
    report = {'context': args.context, 'namespace': args.namespace, 'node_id': args.node,
              'target_id': target, 'scope': 'dedicated receiver only', 'events': [], 'cleanup_verified': False}
    with tempfile.TemporaryDirectory(prefix='cityverse-app-lifecycle-') as private:
        template = controlled_receiver_template(args.image, network_attachment=args.network_attachment, n6_ip_cidr=args.n6_ip_cidr)
        readback = ApplicationReadback(client)
        def native_reader(instance):
            if instance != target:
                raise ValueError('Dedicated target only')
            return dict(readback.endpoint(slot, template), target_id=instance)
        routes = ServiceRouteRegistry(str(Path(private) / 'routes.sqlite'), native_reader=native_reader)
        controller = ApplicationLifecycleController(client, str(Path(private) / 'applications.sqlite'),
            templates={'receiver': template}, slots={target: slot}, binding_reader=routes, readback=readback)
        token = secrets.token_hex(32)
        import uvicorn
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_application_controller_app(controller, token_reader=lambda: token), log_level='critical'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        def call(route, value):
            request = urllib.request.Request(f'http://127.0.0.1:{port}/cityverse-resource/v1/' + route,
                data=json.dumps(value).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        def poll():
            try:
                return call('applications/read', {'target_id': target})
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                # A native Pod can disappear between the UID-fenced reads.
                # Repeat only this readback; never repeat a native mutation.
                report['events'].append({'phase': 'native_readback_fence_retry', 'http': 409})
                return 409, {'readback_pending': True}
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            _, before = call('applications/read', {'target_id': target})
            authority = dict(owner_id='native-application-acceptance', operation_id=secrets.token_hex(12), expected_version=before['version'])
            report['operation_id'] = authority['operation_id']
            _, acknowledgement = call('applications/deploy', dict(target_id=target, template_id='receiver', node_id=args.node, **authority))
            report['events'].append({'phase': 'deploy_ack', 'accepted': acknowledgement.get('accepted')})
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status, installed = poll()
                report['events'].append({'phase': 'readback', 'http': status, 'exists': installed.get('exists'),
                    'ready': installed.get('ready'), 'pod_uid': installed.get('pod_uid'), 'node_id': installed.get('node_id')})
                if installed.get('ready') and installed.get('active_connections') == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Native receiver readiness/activity not confirmed')
            report['installed'] = {k: installed.get(k) for k in ('pod_uid', 'container_id', 'node_id', 'endpoint_id', 'endpoint_revision', 'endpoint_verified', 'address', 'receiver_address', 'port', 'protocol', 'active_connections', 'endpoint_references')}
            authority['expected_version'] = installed['version']
            call('applications/drain', dict(target_id=target, **authority))
            _, drained = call('applications/read', {'target_id': target})
            authority['expected_version'] = drained['version']
            call('applications/remove', dict(target_id=target, **authority))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status, removed = poll()
                if removed.get('exists') is False and removed.get('readback_pending') is False:
                    break
                time.sleep(1)
            report['cleanup_verified'] = (removed.get('exists') is False and removed.get('pod_uid') is None
                and removed.get('endpoint_references') == 0 and removed.get('active_connections') == 0)
            report['status'] = 'passed' if report['cleanup_verified'] else 'recovery_required'
        except Exception as error:
            report['status'] = 'recovery_required'
            report['error_type'] = type(error).__name__
            # Preserve the journal for explicit UID-fenced recovery, never
            # erase evidence or delete an uncertain native write on timeout.
            recovery = args.output.with_suffix('.recovery')
            recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
            import shutil
            for source in Path(private).glob('*.sqlite'):
                shutil.copy2(source, recovery / source.name)
            report['recovery_journal_directory'] = str(recovery)
        finally:
            server.should_exit = True
            thread.join(5)
            sock.close()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'cleanup_verified': report['cleanup_verified'], 'artifact': str(args.output)}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
