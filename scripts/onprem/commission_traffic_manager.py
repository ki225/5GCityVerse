"""Switch only idle N6 traffic containers to a pinned persistent manager image.

Default is read-only; --execute applies exact UID/resourceVersion CAS patches.
No UE is allocated. Full backups contain generated credentials and stay private.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.onprem.runtime.traffic_manager import LEGACY_TRAFFIC_IMAGE, manager_container
from backend.onprem.traffic.transports.manager import TrafficManagerTransport
from backend.onprem.evidence.kubernetes_pod_read import KubernetesPodRead

NAMES = ('n6-embb', 'n6-urllc', 'n6-mmtc', 'n6-v2x')
REAPER = ['bash', '-c', "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"]


def patch_for(deployment, image, previous_image=LEGACY_TRAFFIC_IMAGE):
    if deployment['metadata']['name'] not in NAMES or deployment['spec'].get('replicas') != 1:
        raise RuntimeError('Expected one owned N6 receiver replica')
    if deployment['spec'].get('strategy', {}).get('type') != 'Recreate':
        raise RuntimeError('Static N6 addresses require Recreate rollout')
    containers = deployment['spec']['template']['spec']['containers']
    indexes = [i for i, c in enumerate(containers) if c.get('name') == 'traffic']
    if len(indexes) != 1:
        raise RuntimeError('Expected one traffic container')
    index = indexes[0]
    old = containers[index]
    if old['image'] == image and old.get('command') == ['/usr/local/bin/cityverse-traffic-manager']:
        tokens = [v.get('value') for v in old.get('env', []) if v.get('name') == 'CITYVERSE_TRAFFIC_MANAGER_TOKEN']
        if len(tokens) == 1 and re.fullmatch(r'[a-f0-9]{64}', str(tokens[0])):
            return []  # Resume from observed state; do not rotate credentials or restart.
    previous_command = REAPER if previous_image == LEGACY_TRAFFIC_IMAGE else ['/usr/local/bin/cityverse-traffic-manager']
    if old['image'] != previous_image or old.get('command') != previous_command:
        raise RuntimeError('N6 traffic container is not the recorded legacy runtime')
    replacement = manager_container(deepcopy(old), image=image)
    return [{'op': 'test', 'path': '/metadata/uid', 'value': deployment['metadata']['uid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': deployment['metadata']['resourceVersion']},
            {'op': 'test', 'path': f'/spec/template/spec/containers/{index}', 'value': old},
            {'op': 'replace', 'path': f'/spec/template/spec/containers/{index}', 'value': replacement}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--from-image', default=LEGACY_TRAFFIC_IMAGE)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix' or args.api_url != 'http://127.0.0.1:8000':
        raise RuntimeError('Commission from the local Linux backend host')
    manager_container({'name': 'traffic'}, image=args.image)  # Validate pin before I/O.
    if args.from_image != LEGACY_TRAFFIC_IMAGE:
        manager_container({'name': 'traffic'}, image=args.from_image)
    args.output.mkdir(parents=True, exist_ok=False)
    args.output.chmod(0o700)
    reader = KubernetesPodRead(args.context, args.namespace, kubeconfig=args.kubeconfig)

    def save(name, value):
        path = args.output / name
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
        path.chmod(0o600)

    def kubectl(*argv, body=None, timeout=15):
        if argv == ('get', 'pods', '-o', 'json'):
            return json.dumps(reader.pods(deadline=time.monotonic()+10))
        if len(argv) == 5 and argv[:2] == ('get', 'deployment') and argv[3:] == ('-o', 'json'):
            values = reader.workloads('deployments', deadline=time.monotonic()+10)['items']
            matches = [v for v in values if v['metadata']['name'] == argv[2]]
            if len(matches) != 1:
                raise RuntimeError('Unique receiver deployment unavailable')
            return json.dumps(matches[0])
        try:
            result = subprocess.run(['kubectl', '--kubeconfig', args.kubeconfig, '--context', args.context,
                '-n', args.namespace, '--request-timeout=10s', *argv],
                input=json.dumps(body) if body is not None else None, text=True,
                capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError('N6 commissioning command timed out; inspect before resuming') from None
        if result.returncode:
            raise RuntimeError('N6 commissioning command failed')
        return result.stdout

    def read(route):
        with urlopen(args.api_url + route, timeout=5) as response:
            return json.load(response)

    def idle():
        ue, current = read('/api/onprem/ue'), read('/api/onprem/scenario-rounds/current')
        if (ue.get('requested') != 0 or ue.get('allocated') != 0 or ue.get('perUe')
                or current.get('stage') != 'completed' or current.get('cleanupStatus') != 'verified'):
            raise RuntimeError('A round or UE allocation still requires cleanup')
        pods = json.loads(kubectl('get', 'pods', '-o', 'json'))['items']
        if any(p['metadata']['name'].startswith('cv-ue-') or
               p['metadata'].get('labels', {}).get('cityverse.io/managed-by') == 'onprem-ue-controller'
               for p in pods):
            raise RuntimeError('UE Pods remain')
        return ue, current, pods

    ue, current, pods = idle()
    result = {'image': args.image, 'executed': args.execute, 'generation': ue['generation'],
              'roundId': current['roundId'], 'ueAllocated': 0, 'receivers': [], 'passed': False}
    save('idle.json', {'generation': ue['generation'], 'roundId': current['roundId'],
                       'cleanupStatus': current['cleanupStatus'], 'uePodsAbsent': True})
    adapter = TrafficManagerTransport()
    try:
        for name in NAMES:
            idle()
            deployment = json.loads(kubectl('get', 'deployment', name, '-o', 'json'))
            patch = patch_for(deployment, args.image, args.from_image)
            save(name + '.before.json', deployment)
            if not args.execute:
                result['receivers'].append({'deployment': name, 'planned': True, 'alreadyConfigured': not patch})
                continue
            # No unrelated iperf workload may be interrupted by the rollout.
            live = [p for p in json.loads(kubectl('get', 'pods', '-o', 'json'))['items']
                    if p['metadata'].get('labels', {}).get('app.kubernetes.io/name') == name
                    and not p['metadata'].get('deletionTimestamp')]
            if len(live) != 1:
                raise RuntimeError('Receiver ownership is ambiguous')
            if patch:
                probe = 'for f in /proc/[0-9]*/comm; do read -r n < "$f" || continue; [ "$n" != iperf3 ] || exit 1; done; printf absent'
                observed = kubectl('exec', live[0]['metadata']['name'], '-c', 'traffic', '--', 'sh', '-c', probe)
                if observed != 'absent':
                    raise RuntimeError('Receiver traffic is still active')
            # Recheck the idle owner immediately before the CAS mutation.
            now, owner, _ = idle()
            if now['generation'] != ue['generation'] or owner['roundId'] != current['roundId']:
                raise RuntimeError('Idle owner changed')
            if patch:
                kubectl('patch', 'deployment', name, '--type=json', '-p', json.dumps(patch))
                kubectl('rollout', 'status', 'deployment/'+name, '--timeout=60s', timeout=65)
            _, _, observed = idle()
            live = [p for p in observed if p['metadata'].get('labels', {}).get('app.kubernetes.io/name') == name
                    and not p['metadata'].get('deletionTimestamp')]
            if len(live) != 1:
                raise RuntimeError('Manager receiver ownership is ambiguous')
            identity = adapter.execute('identity', live[0], timeout=2, maximum=128)
            if not identity.startswith('IDENTITYv1 '):
                raise RuntimeError('Manager identity unavailable')
            save(name + '.after.json', json.loads(kubectl('get', 'deployment', name, '-o', 'json')))
            result['receivers'].append({'deployment': name, 'managerReady': True,
                                       'podUid': live[0]['metadata']['uid']})
            save(name + '.receipt.json', result['receivers'][-1])
        result['passed'] = True
    finally:
        adapter.close()
        save('summary.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
