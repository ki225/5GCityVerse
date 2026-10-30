"""Native zero gate: read live state only; write to isolated scratch snapshots."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.onprem.retire_capacity_intent_offline import check_proof  # noqa: E402
from scripts.onprem.start_native_backend import SMF_INVENTORY_IMAGE, read_smf_inventory_token  # noqa: E402

ZERO_FIELDS = ('requested', 'allocated', 'registered', 'pduEstablished', 'trafficVerified', 'failed')
SLICES = {'embb', 'urllc', 'mmtc', 'v2x'}


def validate_zero(status: dict, proof: dict, *, now: datetime) -> int:
    generation = status.get('generation')
    if (type(generation) is not int or generation < 1 or status.get('status') != 'stopped'
            or status.get('errors') != [] or status.get('perUe') != []
            or any(type(status.get(key)) is not int or status[key] != 0 for key in ZERO_FIELDS)):
        raise RuntimeError('UE controller is not at exact stopped zero')
    try:
        check_proof(proof, generation, now=now)
        upf = proof['sources']['upf']
        epochs = json.loads(upf['epoch'])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Native zero proof is missing required fields') from None
    if (upf.get('source') != 'gtp5g-netlink:four-upf' or not isinstance(epochs, dict)
            or set(epochs) != SLICES or any(not isinstance(value, str) or not value for value in epochs.values())):
        raise RuntimeError('Four current UPF epochs are not proven')
    readouts = proof.get('gnbReadouts')
    if proof.get('gnbSource') != 'free-ran-ue-gnb-api-ue-count' or not isinstance(readouts, dict) or set(readouts) != SLICES:
        raise RuntimeError('Four free-ran-ue gNB identities are not proven')
    for row in readouts.values():
        if not isinstance(row, dict) or not isinstance(row.get('podUid'), str) or not row['podUid']:
            raise RuntimeError('gNB Pod identity is unavailable')
        try:
            observed = datetime.fromisoformat(row['observedAt'].replace('Z', '+00:00'))
            age = (now - observed).total_seconds()
        except (KeyError, AttributeError, TypeError, ValueError):
            raise RuntimeError('gNB observation timestamp is unavailable') from None
        if observed.tzinfo is None or not 0 <= age <= 30:
            raise RuntimeError('gNB observation is stale or from the future')
    return generation


def get_status(api_url: str) -> dict:
    with urlopen(api_url.rstrip('/') + '/api/onprem/ue/status', timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('UE status endpoint unavailable')
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError('UE status response is not an object')
    return value


def owned_zero_generation(intent_path: Path) -> int:
    receipt = json.loads(intent_path.read_text(encoding='utf-8'))
    zero = dict.fromkeys(('embb', 'urllc', 'mmtc', 'v2x'), 0)
    accepted = receipt.get('intent', {}).get('generation')
    before = receipt.get('before', {}).get('generation')
    if (receipt.get('httpStatus') != 202 or receipt.get('desired') != zero
            or receipt.get('request') != zero or type(accepted) is not int
            or type(before) is not int or accepted <= before):
        raise RuntimeError('Cleanup intent receipt does not prove the owned zero generation')
    return accepted


def wait_for_zero(api_url: str, generation: int, wait_seconds: int) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        status = get_status(api_url)
        if status.get('generation') != generation or status.get('errors') != []:
            raise RuntimeError('Cleanup generation changed or UE controller reported errors')
        if status.get('status') == 'stopped':
            return status
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned UE zero generation did not reach stopped before deadline')
        time.sleep(2)


def snapshot_database(source: Path, target: Path) -> None:
    """SQLite online backup reads the live DB through a read-only connection."""
    uri = source.resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=10) as live:
        with sqlite3.connect(target) as scratch:
            live.backup(scratch)


def check(args: argparse.Namespace) -> dict:
    for path in (args.kubectl, args.kubeconfig, args.credentials, args.python,
                 args.state, args.state.with_suffix('.oam.sqlite3'), args.intent):
        if not path.is_file():
            raise RuntimeError('A required pinned native input is missing')
    if args.output.exists():
        raise RuntimeError('Native zero report output already exists')
    generation = owned_zero_generation(args.intent)
    status = wait_for_zero(args.api_url, generation, args.wait_seconds)
    scratch = ROOT / '.tmp' / f'fru-native-zero-{uuid4().hex}'
    scratch.mkdir(parents=True, exist_ok=False)
    state_snapshot = scratch / 'ue-state.sqlite3'
    snapshot_database(args.state, state_snapshot)
    snapshot_database(args.state.with_suffix('.oam.sqlite3'), state_snapshot.with_suffix('.oam.sqlite3'))
    observation = scratch / 'observation.json'
    env = dict(os.environ)
    env['PATH'] = str(args.kubectl.resolve().parent) + os.pathsep + env.get('PATH', '')
    env['KUBECONFIG'] = str(args.kubeconfig.resolve())
    command = [str(args.kubectl.resolve()), '--kubeconfig', env['KUBECONFIG'],
               '--context', args.context, '-n', args.namespace]
    token = read_smf_inventory_token(command, env)
    env.update({
        'CITYVERSE_ONPREM_UE_STATE_FILE': str(state_snapshot),
        'CITYVERSE_ONPREM_KUBE_CONTEXT': args.context,
        'CITYVERSE_ONPREM_NAMESPACE': args.namespace,
        'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
        'CITYVERSE_ONPREM_CREDENTIALS_FILE': str(args.credentials.resolve()),
        'CITYVERSE_ONPREM_MONGO_URI': 'mongodb://127.0.0.1:27018',
        'CITYVERSE_ONPREM_MONGO_DATABASE': 'free5gc',
        'CITYVERSE_ONPREM_AMF_OAM_URL': 'http://127.0.0.1:18080',
        'CITYVERSE_ONPREM_SMF_OAM_URL': 'http://127.0.0.1:18081',
        'CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED': 'true',
        'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE': SMF_INVENTORY_IMAGE,
        'CITYVERSE_ONPREM_SMF_INVENTORY_URL': 'http://127.0.0.1:18081/nsmf-oam/v1/sm-contexts',
        'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN': token,
        'CITYVERSE_ONPREM_UPF_EVIDENCE_URLS': json.dumps({name: f'http://127.0.0.1:{19081+i}'
                                                         for i, name in enumerate(('embb','urllc','mmtc','v2x'))}),
    })
    run = subprocess.run([str(args.python.resolve()), str(ROOT / 'scripts/onprem/observe_ue_native.py'),
                          '--generation', str(generation), '--output', str(observation)],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    if run.returncode != 0 or not observation.is_file():
        (scratch / 'observer-failure.log').write_text(run.stderr, encoding='utf-8')
        raise RuntimeError('Fresh native observation failed')
    proof = json.loads(observation.read_text(encoding='utf-8'))
    validate_zero(status, proof, now=datetime.now(timezone.utc))
    after = get_status(args.api_url)
    validate_zero(after, proof, now=datetime.now(timezone.utc))
    report = {'status': 'passed', 'generation': generation, 'observedAt': proof['observedAt'],
              'proof': str(observation), 'scope': 'ue-amf-smf-four-upf-four-gnb-native-zero'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    lab = ROOT / '.cache/kubeadm-lab'
    parser.add_argument('--kubectl', type=Path, default=lab / 'bin/kubectl.exe')
    parser.add_argument('--kubeconfig', type=Path, default=lab / 'kubeconfig')
    parser.add_argument('--credentials', type=Path, default=lab / 'groups.json')
    parser.add_argument('--python', type=Path, default=lab / 'backend-venv/Scripts/python.exe')
    parser.add_argument('--state', type=Path, default=lab / 'ue-state.sqlite3')
    parser.add_argument('--intent', type=Path, required=True,
                        help='Exact cleanup-ui/intent.json receipt for this owned zero generation')
    parser.add_argument('--wait-seconds', type=int, default=1500)
    parser.add_argument('--context', default='cityverse-kubeadm-lab')
    parser.add_argument('--namespace', default='free5gc-onprem')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    parsed = urlsplit(args.api_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        parser.error('Only loopback HTTP API is allowed')
    if not 0 <= args.wait_seconds <= 1500:
        parser.error('wait-seconds must be 0..1500')
    print(json.dumps(check(args), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
