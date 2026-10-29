#!/usr/bin/env python3
"""Normalize Helm's null map deletions before Kubernetes decodes string selectors.

Only null nodeSelector values are removed. Physical site selectors, hostname
selectors, ConfigMap data and all other workload fields retain their semantics.
"""
import sys
import json
import yaml


def normalize(value):
    if isinstance(value, dict):
        selector = value.get('nodeSelector')
        if isinstance(selector, dict):
            value['nodeSelector'] = {key: item for key, item in selector.items() if item is not None}
        for child in value.values():
            normalize(child)
    elif isinstance(value, list):
        for child in value:
            normalize(child)
    return value


def prepare(documents):
    """Merge declarations for the upstream AMF's missing sidecar extension."""
    documents = [normalize(doc) for doc in documents if doc is not None]
    configs = [doc for doc in documents if doc.get('kind') == 'ConfigMap'
        and doc.get('metadata', {}).get('name') == 'cityverse-ran-evidence-config']
    if not configs:
        return documents
    if len(configs) != 1 or configs[0].get('data', {}).get('schema') != 'cityverse.ran-evidence-config/v1':
        raise ValueError('Exact native RAN evidence configuration required')
    config = configs[0]
    amfs = [doc for doc in documents if doc.get('kind') == 'Deployment'
        and doc.get('metadata', {}).get('namespace') == config['metadata']['namespace']
        and doc.get('metadata', {}).get('labels', {}).get('app.kubernetes.io/name') == 'free5gc-amf']
    if len(amfs) != 1:
        raise ValueError('Exact upstream AMF Deployment required for evidence hook')
    pod = amfs[0]['spec']['template']
    for field, key in (('containers', 'amf-container.json'), ('volumes', 'source-volume.json')):
        item = json.loads(config['data'][key])
        rows = pod['spec'].setdefault(field, [])
        previous = [row for row in rows if row.get('name') == item['name']]
        if previous and previous != [item]:
            raise ValueError('Native AMF evidence declaration conflicts')
        if not previous:
            rows.append(item)
    pod.setdefault('metadata', {}).setdefault('annotations', {})['checksum/ran-evidence'] = config['data']['source-revision']
    return documents


if __name__ == '__main__':
    yaml.safe_dump_all(prepare(yaml.safe_load_all(sys.stdin)),
                       sys.stdout, sort_keys=False)
