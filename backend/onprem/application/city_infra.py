"""Read-only city infra projection. Never returns Pod env, volumes or Secrets."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import yaml

VALUES = Path(__file__).resolve().parents[3] / 'infrastructure/onprem/values.yaml'


def configured_infra(path=VALUES):
    values = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    city = values['city']
    sites = {name: dict(id=name, tier=site['tier'], position=site['position'],
                        relativeCapacity=site['relativeCapacity']) for name, site in city['sites'].items()}
    workloads = []
    def add(name, function, site, resources, category='nf'):
        workloads.append(dict(workload=name, function=function, siteId=site,
                              resources=deepcopy(resources), category=category))
    for nf in ('amf', 'ausf', 'nrf', 'pcf', 'udm', 'udr', 'webui'):
        config = values['free5gc']['free5gc-'+nf][nf]
        add('free5gc-'+nf, nf.upper() if nf != 'webui' else 'WebUI',
            config['nodeSelector']['cityverse.io/site'], config['resources'], 'service' if nf == 'webui' else 'nf')
    mongo = values['free5gc']['mongodb']
    add('mongodb', 'MongoDB', mongo['nodeSelector']['cityverse.io/site'], mongo['resources'], 'service')
    for nf, config in city['core'].items():
        add(nf, nf.upper(), config['site'], config['resources'])
    for name, config in city['slices'].items():
        add('upf-'+name, 'UPF / '+name, config['site'], config['resources'])
    for gnb in city['gnbs']:
        sites[gnb['id']] = dict(id=gnb['id'], tier='ran', position=gnb['position'], relativeCapacity=None)
        add(gnb['workload'], 'gNB', gnb['id'], gnb.get('resources', city['gnbResources']), 'ran')
    return dict(schemaVersion='cityverse.infra-config/v1', source='infrastructure/onprem/values.yaml',
                sites=list(sites.values()), workloads=workloads)


def project_pods(inventory, config, namespace):
    if not isinstance(inventory, dict) or not isinstance(inventory.get('items'), list) or len(inventory['items']) > 4096 or inventory.get('metadata', {}).get('continue'):
        raise ValueError('Incomplete Pod inventory')
    sites = {s['id'] for s in config['sites']}
    rows = []
    for pod in inventory['items']:
        meta, spec, status = (pod.get(k, {}) for k in ('metadata', 'spec', 'status'))
        if meta.get('namespace') != namespace:
            raise ValueError('Unexpected Pod namespace')
        labels = meta.get('labels', {})
        name, app, nf = meta.get('name', ''), labels.get('app.kubernetes.io/name', ''), labels.get('nf', '')
        matched = next((w for w in config['workloads'] if app == w['workload']
                        or (w['workload'].startswith('free5gc-') and nf == w['workload'][8:])
                        or name.startswith(w['workload']+'-')), None)
        if not matched:
            continue
        site = labels.get('cityverse.io/site') or meta.get('annotations', {}).get('cityverse.io/site')
        binding = 'pod-metadata' if site in sites else 'configured-workload'
        site = site if site in sites else matched['siteId']
        states = {c['name']: c for c in status.get('containerStatuses', [])}
        containers = []
        for c in spec.get('containers', []):
            state = states.get(c['name'], {})
            containers.append(dict(name=c['name'], image=c.get('image'),
                requests={k: c.get('resources', {}).get('requests', {}).get(k) for k in ('cpu', 'memory')},
                limits={k: c.get('resources', {}).get('limits', {}).get(k) for k in ('cpu', 'memory')},
                ready=state.get('ready', False), restarts=state.get('restartCount', 0)))
        rows.append(dict(name=name, uid=meta.get('uid'), namespace=namespace, siteId=site,
            siteBinding=binding, workload=matched['workload'], function=matched['function'],
            category=matched['category'], phase='Terminating' if meta.get('deletionTimestamp') else status.get('phase', 'Unknown'),
            ready=any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])),
            node=spec.get('nodeName'), podIp=status.get('podIP'), containers=containers))
    return sorted(rows, key=lambda row: (row['siteId'], row['function'], row['name']))


class CityInfraReader:
    """Coalesces clicks into one bounded namespace read; no background polling."""
    def __init__(self, read_pods, namespace, config=None):
        self.read_pods, self.namespace = read_pods, namespace
        self.config = config or configured_infra()
        self.lock, self.cached, self.expires = threading.Lock(), None, 0

    def snapshot(self):
        with self.lock:
            if self.cached is not None and time.monotonic() < self.expires:
                return deepcopy(self.cached)
            observed = datetime.now(timezone.utc).isoformat()
            pods = project_pods(self.read_pods(), self.config, self.namespace)
            self.cached = dict(schemaVersion='cityverse.city-infra/v1', source='kubernetes-pod-spec',
                observedAt=observed, namespace=self.namespace, pods=pods, usageAvailable=False)
            self.expires = time.monotonic() + 5
            return deepcopy(self.cached)


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Export sanitized city infra configuration for the standalone map')
    parser.add_argument('--export-config', type=Path, required=True)
    args = parser.parse_args()
    args.export_config.parent.mkdir(parents=True, exist_ok=True)
    args.export_config.write_text(json.dumps(configured_infra(), indent=2)+'\n', encoding='utf-8')
