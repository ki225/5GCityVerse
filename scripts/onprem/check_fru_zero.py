"""Read-only guard for free-ran benchmark Kubernetes objects.

This does not prove native AMF, SMF, UPF, gNB, or UE controller zero state.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

LABEL = 'fru-benchmark'
PREFIX = 'cv-fru-'


def residuals(items: list[dict]) -> list[str]:
    """Include unlabeled benchmark objects so a missing label cannot hide them."""
    remaining = []
    for item in items:
        metadata = item.get('metadata', {})
        name = metadata.get('name', '')
        labels = metadata.get('labels') or {}
        if labels.get('cityverse.io/managed-by') == LABEL or name.startswith(PREFIX):
            remaining.append(f"{item.get('kind', 'Unknown')}/{name}")
    return sorted(remaining)


def check(kubectl: Path, kubeconfig: Path) -> dict:
    if not kubectl.is_file() or not kubeconfig.is_file():
        raise RuntimeError('Explicit kubeadm kubectl and kubeconfig required')
    command = [str(kubectl.resolve()), '--kubeconfig', str(kubeconfig.resolve()),
               '--context', 'cityverse-kubeadm-lab', '-n', 'free5gc-onprem',
               'get', 'pods,jobs,deployments,services,configmaps', '-o', 'json']
    items = json.loads(subprocess.check_output(command, text=True, timeout=30))['items']
    found = residuals(items)
    if found:
        raise RuntimeError('FRU resources remain: ' + ', '.join(found))
    return {'context': 'cityverse-kubeadm-lab', 'namespace': 'free5gc-onprem',
            'checkedKinds': ['Pod', 'Job', 'Deployment', 'Service', 'ConfigMap'],
            'fruResiduals': 0, 'scope': 'fru-kubernetes-objects-only'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubectl', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.kubectl, args.kubeconfig), sort_keys=True))


if __name__ == '__main__':
    main()
