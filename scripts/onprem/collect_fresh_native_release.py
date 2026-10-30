"""Capture a fresh, read-only native-zero proof for a quarantined UE generation.

Each run snapshots the durable databases into a unique workspace .tmp directory.
The observer can update only the snapshot reference DB, never the live ledger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.onprem.check_fru_native_zero import snapshot_database  # noqa: E402
from scripts.onprem.retire_capacity_intent_offline import check_proof  # noqa: E402
from scripts.onprem.start_native_backend import SMF_INVENTORY_IMAGE, read_smf_inventory_token  # noqa: E402

SLICES = {'embb', 'urllc', 'mmtc', 'v2x'}


def new_output_dir() -> Path:
    output = ROOT / '.tmp' / f'fru-native-release-{uuid4().hex}'
    output.mkdir(parents=True, exist_ok=False)
    return output


def validate_proof(proof: dict, generation: int, *, now: datetime) -> None:
    check_proof(proof, generation, now=now)
    if proof.get('gnbSource') != 'free-ran-ue-gnb-api-ue-count':
        raise RuntimeError('Free-RAN gNB source is unavailable')
    readouts = proof.get('gnbReadouts')
    if not isinstance(readouts, dict) or set(readouts) != SLICES:
        raise RuntimeError('Four fresh gNB readouts are unavailable')
    for row in readouts.values():
        if not isinstance(row, dict) or not isinstance(row.get('podUid'), str) or not row['podUid']:
            raise RuntimeError('gNB Pod identity is unavailable')
        observed = datetime.fromisoformat(row['observedAt'].replace('Z', '+00:00'))
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 30:
            raise RuntimeError('gNB readout is stale or future-dated')
    upf = proof['sources']['upf']
    if upf.get('source') != 'gtp5g-netlink:four-upf':
        raise RuntimeError('Four native UPF sources are unavailable')
    epochs = json.loads(upf['epoch'])
    if not isinstance(epochs, dict) or set(epochs) != SLICES or any(not value for value in epochs.values()):
        raise RuntimeError('Four UPF epochs are unavailable')


def collect(generation: int) -> dict:
    if generation < 1:
        raise RuntimeError('A positive generation is required')
    lab = ROOT / '.cache/kubeadm-lab'
    state = lab / 'ue-state.sqlite3'
    refs = state.with_suffix('.oam.sqlite3')
    kubectl = lab / 'bin/kubectl.exe'
    kubeconfig = lab / 'kubeconfig'
    credentials = lab / 'groups.json'
    python = lab / 'backend-venv/Scripts/python.exe'
    for path in (state, refs, kubectl, kubeconfig, credentials, python):
        if not path.is_file():
            raise RuntimeError('A pinned native input is missing')
    output = new_output_dir()
    snapshot = output / 'ue-state.snapshot.sqlite3'
    snapshot_database(state, snapshot)
    snapshot_database(refs, snapshot.with_suffix('.oam.sqlite3'))
    env = dict(os.environ)
    env['PATH'] = str(kubectl.parent) + os.pathsep + env.get('PATH', '')
    env['KUBECONFIG'] = str(kubeconfig)
    base = [str(kubectl), '--kubeconfig', str(kubeconfig), '--context', 'cityverse-kubeadm-lab', '-n', 'free5gc-onprem']
    token = read_smf_inventory_token(base, env)
    env.update({
        'CITYVERSE_ONPREM_UE_STATE_FILE': str(snapshot),
        'CITYVERSE_ONPREM_KUBE_CONTEXT': 'cityverse-kubeadm-lab',
        'CITYVERSE_ONPREM_NAMESPACE': 'free5gc-onprem',
        'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
        'CITYVERSE_ONPREM_CREDENTIALS_FILE': str(credentials),
        'CITYVERSE_ONPREM_MONGO_URI': 'mongodb://127.0.0.1:27018',
        'CITYVERSE_ONPREM_MONGO_DATABASE': 'free5gc',
        'CITYVERSE_ONPREM_AMF_OAM_URL': 'http://127.0.0.1:18080',
        'CITYVERSE_ONPREM_SMF_OAM_URL': 'http://127.0.0.1:18081',
        'CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED': 'true',
        'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE': SMF_INVENTORY_IMAGE,
        'CITYVERSE_ONPREM_SMF_INVENTORY_URL': 'http://127.0.0.1:18081/nsmf-oam/v1/sm-contexts',
        'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN': token,
        'CITYVERSE_ONPREM_UPF_EVIDENCE_URLS': json.dumps({name: f'http://127.0.0.1:{19081 + index}'
                                                         for index, name in enumerate(('embb', 'urllc', 'mmtc', 'v2x'))}),
    })
    proof_path = output / 'native-proof.json'
    command = [str(python), str(ROOT / 'scripts/onprem/observe_ue_native.py'),
               '--generation', str(generation), '--output', str(proof_path)]
    run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    if run.returncode or not proof_path.is_file():
        (output / 'observer-error.log').write_text(run.stderr, encoding='utf-8')
        raise RuntimeError(f'Native observation failed; private log: {output / "observer-error.log"}')
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    validate_proof(proof, generation, now=datetime.now(timezone.utc))
    return {'generation': generation, 'proof': str(proof_path),
            'stateSnapshot': str(snapshot), 'referenceSnapshot': str(snapshot.with_suffix('.oam.sqlite3')),
            'observedAt': proof['observedAt']}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=int, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(collect(args.generation), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
