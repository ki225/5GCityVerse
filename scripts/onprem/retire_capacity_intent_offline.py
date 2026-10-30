"""Retire one quarantined capacity intent after independent native-zero proof.

This intentionally never constructs or starts UEController: no reconcile can
run while the failed generation still requests UEs.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import msvcrt
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.onprem.runtime.ue import GenerationStore  # noqa: E402

ZERO = dict.fromkeys(('embb', 'urllc', 'mmtc', 'v2x'), 0)
FAILED = dict.fromkeys(ZERO, 25)


def check_proof(proof: dict, generation: int, *, now: datetime) -> None:
    observed = datetime.fromisoformat(proof['observedAt'])
    age = (now - observed).total_seconds()
    if proof.get('generation') != generation or not 0 <= age <= 30:
        raise RuntimeError('Native-zero proof has wrong generation or is stale')
    if (proof.get('ownedPods') != [] or proof.get('rows') != []
            or proof.get('amfRegistered') != 0 or proof.get('smfSessions') != 0
            or proof.get('upfPfcpRows') != 0 or proof.get('unattributedUpfRows') != 0
            or proof.get('gnbUeCounts') != ZERO
            or proof.get('subscriberVerifiedCount') != 1000
            or proof.get('subscriberCredentialsMatched') is not True):
        raise RuntimeError('Native-zero or subscriber proof is incomplete')
    for name in ('amf', 'smf', 'upf'):
        source = proof.get('sources', {}).get(name, {})
        timestamp = datetime.fromisoformat(source['observedAt'])
        # AMF/SMF receive timestamps use the host clock. UPF evidence is
        # sampled on kubeadm guests, whose clock may lead this host by ~5.5s.
        # Retain a strict 30s age and cap only that producer's future skew.
        lower_bound = -10 if name == 'upf' else 0
        if source.get('available') is not True or not lower_bound <= (now - timestamp).total_seconds() <= 30:
            raise RuntimeError(f'{name} native evidence is unavailable or stale')
    if proof['sources']['smf'].get('inventoryComplete') is not True:
        raise RuntimeError('SMF inventory is not authoritative')


def retire(state: Path, proof: dict, failure: dict, generation: int, backup: Path,
           kubectl: Path, kubeconfig: Path) -> dict:
    backup = backup.resolve()
    if ROOT / '.tmp' not in backup.parents or backup.exists():
        raise RuntimeError('Backup must be a new file beneath workspace .tmp')
    lock_path = Path(str(state) + '.lock')
    with lock_path.open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            now = datetime.now(timezone.utc)
            check_proof(proof, generation, now=now)
            store = GenerationStore(state)
            current = store.current()
            if (not current or current['generation'] != generation
                    or current['desired'] != FAILED or len(current['allocation']) != 100):
                raise RuntimeError('Quarantined generation identity changed')
            if (failure.get('generation') != generation or failure.get('requested') != 100
                    or failure.get('status') in ('established', 'stopped')
                    or {row.get('supi') for row in failure.get('perUe', [])}
                       != {row['supi'] for row in current['allocation']}):
                raise RuntimeError('Failed-run marker does not match quarantined allocation')
            command = [str(kubectl.resolve()), '--kubeconfig', str(kubeconfig.resolve()),
                       '--context', 'cityverse-kubeadm-lab', '-n', 'free5gc-onprem',
                       'get', 'pods', '-l', 'cityverse.io/managed-by=onprem-ue-controller', '-o', 'json']
            pods = json.loads(subprocess.check_output(command, text=True, timeout=20))['items']
            if pods:
                raise RuntimeError('Owned UE Pods appeared after native-zero observation')
            with closing(sqlite3.connect(store.path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            result, changed = store.request(ZERO)
            if not changed or result['generation'] <= generation or result['desired'] != ZERO:
                raise RuntimeError('Offline ZERO transition did not persist')
            return {'previousGeneration': generation, 'zeroGeneration': result['generation'],
                    'desired': result['desired'], 'backup': str(backup)}
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--failed-run', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--kubectl', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    args = parser.parse_args()
    if args.generation != 199 or args.state.resolve() != (ROOT / '.cache/kubeadm-lab/ue-state.sqlite3').resolve():
        raise SystemExit('Only exact generation199 dedicated-lab state may be retired')
    proof_path = args.proof.resolve()
    failure_path = args.failed_run.resolve()
    if (ROOT / '.tmp' not in proof_path.parents
            or failure_path != (ROOT / '.tmp/ue-capacity/20261001T192422Z/100-failed.json').resolve()):
        raise SystemExit('Proof and failed-run marker must match exact lab scope')
    if not args.kubectl.is_file() or not args.kubeconfig.is_file():
        raise SystemExit('Explicit kubeadm client and kubeconfig required')
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    failure = json.loads(failure_path.read_text(encoding='utf-8'))
    print(json.dumps(retire(args.state, proof, failure, args.generation,
                            args.backup, args.kubectl, args.kubeconfig), sort_keys=True))


if __name__ == '__main__':
    main()
