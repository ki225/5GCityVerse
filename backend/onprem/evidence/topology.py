"""Independent Pod readiness observations; never authorize traffic or policy writes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

NODE_IDS = ('gnb', 'amf', 'smf', 'pcf', 'nssf', 'udm-ausf', 'nrf', 'upf', 'dn')
SLICES = ('embb', 'urllc', 'mmtc', 'v2x')


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def unknown_nodes(reason):
    return [dict(mapNodeId=node, status='unknown', reason=reason, observedAt=None,
                 evidenceRef=None, scenario=None, slice=None, shared=True, callId=None, cycle=None)
            for node in NODE_IDS]


class PodTopologyCollector:
    """One explicit-context, bounded namespace read per sample, independent of LLMs."""

    def __init__(self, read_pods):
        self.read_pods = read_pods

    @classmethod
    def from_context(cls, context):
        driver = context.ue_controller.driver
        if driver is None:
            return None
        from os import environ
        def read():
            command = ['kubectl']
            if environ.get('KUBECONFIG'):
                command += ['--kubeconfig', environ['KUBECONFIG']]
            command += ['--context', driver.context, '--namespace', driver.namespace,
                        'get', 'pods', '--request-timeout=8s', '-o', 'json']
            result = driver.runner(command, text=True, capture_output=True, timeout=10, check=False)
            if result.returncode or len(result.stdout.encode('utf-8')) > 4 * 1024 * 1024:
                raise ValueError('Topology Pod inventory unavailable')
            return json.loads(result.stdout)
        return cls(read)

    def collect(self, *, observed_at):
        inventory = self.read_pods()
        if not isinstance(inventory, dict) or not isinstance(inventory.get('items'), list):
            raise ValueError('Invalid topology Pod inventory')
        grouped = {}
        for pod in inventory['items']:
            meta, state = pod.get('metadata', {}), pod.get('status', {})
            if meta.get('deletionTimestamp'):
                continue
            labels = meta.get('labels', {})
            app = labels.get('app.kubernetes.io/name', '')
            nf = labels.get('nf') or (app.removeprefix('free5gc-') if app.startswith('free5gc-') else app)
            key = None
            if nf in ('amf', 'smf', 'pcf', 'nssf', 'nrf', 'udm', 'ausf'):
                key = (nf, None)
            for prefix, node in (('gnb-', 'gnb'), ('upf-', 'upf'), ('n6-', 'dn')):
                if app.startswith(prefix) and app[len(prefix):] in SLICES:
                    key = (node, app[len(prefix):])
            if key is None or not isinstance(meta.get('uid'), str) or not meta['uid']:
                continue
            ready = any(c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in state.get('conditions', []))
            grouped.setdefault(key, []).append(dict(podUid=meta['uid'], pod=meta.get('name'),
                phase=state.get('phase'), ready=ready))
        nodes = []
        for node in NODE_IDS:
            for slice_id in SLICES if node in ('gnb', 'upf', 'dn') else (None,):
                required = ('udm', 'ausf') if node == 'udm-ausf' else (node,)
                groups = [grouped.get((name, slice_id), []) for name in required]
                pods = [p for group in groups for p in group]
                if any(not group for group in groups):
                    status, reason = 'unknown', 'pod_inventory_incomplete'
                elif all(p['phase'] == 'Running' and p['ready'] for p in pods):
                    status, reason = 'running', 'pod_ready'
                else:
                    status, reason = 'error', 'pod_not_ready'
                material = dict(node=node, slice=slice_id, observedAt=timestamp(observed_at), pods=pods)
                digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
                nodes.append(dict(mapNodeId=node, status=status, reason=reason,
                    observedAt=timestamp(observed_at), evidenceRef='topology-pods:'+digest,
                    scenario=None, slice=slice_id, shared=True, callId=None, cycle=None,
                    source='kubernetes-pod-readiness', pods=pods))
        return nodes
