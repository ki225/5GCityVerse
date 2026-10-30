"""Offline, evidence-gated retirement of one quarantined kubeadm UE intent.

The backend must be stopped so its lifetime state lock is free. The original
generation remains in the ledger; a zero generation and its audit row commit
in one SQLite transaction. This tool never starts a UE controller.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.onprem.runtime.ue import GenerationStore, SLICES, blank_status  # noqa: E402
from scripts.onprem.retire_capacity_intent_offline import check_proof  # noqa: E402

ZERO = {name: 0 for name in SLICES}
LABEL = 'cityverse.io/managed-by=onprem-ue-controller'


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_evidence(proof: dict, generation: int, now: datetime) -> None:
    check_proof(proof, generation, now=now)
    if proof.get('gnbSource') != 'free-ran-ue-gnb-api-ue-count':
        raise RuntimeError('Four-slice native gNB source is missing')
    readouts = proof.get('gnbReadouts')
    if not isinstance(readouts, dict) or set(readouts) != set(ZERO):
        raise RuntimeError('Four-slice native gNB readouts are missing')
    for name, row in readouts.items():
        if not isinstance(row, dict) or not isinstance(row.get('podUid'), str) or not row['podUid']:
            raise RuntimeError(f'{name} gNB Pod identity is missing')
        stamp = datetime.fromisoformat(row['observedAt'])
        if not 0 <= (now - stamp).total_seconds() <= 30:
            raise RuntimeError(f'{name} gNB readout is stale')
    upf = proof['sources']['upf']
    try:
        epochs = json.loads(upf.get('epoch', 'null'))
    except (TypeError, ValueError):
        raise RuntimeError('Four-slice native UPF epochs are missing') from None
    if not isinstance(epochs, dict) or set(epochs) != set(ZERO) or not all(epochs.values()):
        raise RuntimeError('Four-slice native UPF epochs are missing')


def _check_intent(current: dict | None, generation: int) -> None:
    if not current or current['generation'] != generation:
        raise RuntimeError('Quarantined generation identity changed')
    desired = current['desired']
    result = current['result']
    if (not isinstance(desired, dict) or set(desired) != set(ZERO)
            or not all(type(value) is int and value >= 0 for value in desired.values())
            or sum(desired.values()) <= 0 or len(current['allocation']) != sum(desired.values())
            or result.get('generation') != generation
            or result.get('status') not in ('degraded', 'failed')
            or type(result.get('requested')) is not int
            or result['requested'] != sum(desired.values())
            or not isinstance(result.get('errors'), list) or not result['errors']):
        raise RuntimeError('Current generation is not a quarantined failed intent')
    supis = [row.get('supi') for row in current['allocation']]
    if len(set(supis)) != len(supis) or any(not isinstance(supi, str) for supi in supis):
        raise RuntimeError('Quarantined allocation is malformed')


def _check_pods(kubectl: Path, kubeconfig: Path) -> None:
    command = [str(kubectl.resolve()), '--kubeconfig', str(kubeconfig.resolve()),
               '--context', 'cityverse-kubeadm-lab', '-n', 'free5gc-onprem',
               'get', 'pods', '-l', LABEL, '-o', 'json']
    pods = json.loads(subprocess.check_output(command, text=True, timeout=20))['items']
    if not isinstance(pods, list):
        raise RuntimeError('Owned UE Pod inventory is malformed')
    if pods:
        raise RuntimeError('Owned UE Pods appeared after native-zero observation')


def reconcile(state: Path, proof_path: Path, generation: int, backup: Path,
              receipt: Path, kubectl: Path, kubeconfig: Path, *, dry_run: bool = False) -> dict:
    """Fail closed if proof, identity, Pod absence, or exclusive ownership changes."""
    proof_bytes = proof_path.read_bytes()
    proof = json.loads(proof_bytes)
    for path in (backup, receipt):
        if ROOT / '.tmp' not in path.resolve().parents or path.exists():
            raise RuntimeError('Backup and receipt must be new files beneath workspace .tmp')
    if backup.resolve() == receipt.resolve():
        raise RuntimeError('Backup and receipt paths must differ')
    lock_path = Path(str(state) + '.lock')
    if not lock_path.is_file():
        raise RuntimeError('Backend ownership lock is missing')
    with lock_path.open('r+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            raise RuntimeError('Backend ownership lock is empty')
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            now = datetime.now(timezone.utc)
            _check_evidence(proof, generation, now)
            # Do not run GenerationStore.__init__: it performs schema migration
            # and would make even --dry-run write to an existing ledger.
            store = object.__new__(GenerationStore)
            store.path = state
            current = store.current()
            _check_intent(current, generation)
            _check_pods(kubectl, kubeconfig)
            if dry_run:
                return {'dryRun': True, 'generation': generation,
                        'proofSha256': _digest(proof_bytes), 'requested': sum(current['desired'].values())}
            backup.parent.mkdir(parents=True, exist_ok=True)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(store.path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            backup_hash = _digest(backup.read_bytes())
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                latest = store.decode(db.execute(
                    'SELECT g.*, w.last_established FROM generations g '
                    'LEFT JOIN ue_reconciliation_watchdogs w ON w.generation_id=g.id '
                    'ORDER BY g.id DESC LIMIT 1').fetchone())
                _check_intent(latest, generation)
                if latest != current:
                    raise RuntimeError('Quarantined generation changed during backup')
                _check_evidence(proof, generation, datetime.now(timezone.utc))
                _check_pods(kubectl, kubeconfig)
                created = time.time()
                cursor = db.execute('INSERT INTO generations(desired,allocation,created,status) VALUES (?,?,?,?)',
                                    (json.dumps(ZERO), '[]', created, '{}'))
                zero_generation = cursor.lastrowid
                status = blank_status(zero_generation, ZERO)
                db.execute('UPDATE generations SET status=? WHERE id=?', (json.dumps(status), zero_generation))
                audit = {'schemaVersion': 'cityverse.ue-operator-reconcile/v1',
                         'previousGeneration': generation, 'zeroGeneration': zero_generation,
                         'previousIntentSha256': _digest(json.dumps(current, sort_keys=True).encode()),
                         'proofSha256': _digest(proof_bytes), 'backupSha256': backup_hash,
                         'backup': str(backup.resolve()), 'observedAt': proof['observedAt'],
                         'committedAt': datetime.now(timezone.utc).isoformat()}
                db.execute('CREATE TABLE IF NOT EXISTS ue_operator_reconciliations '
                           '(zero_generation INTEGER PRIMARY KEY, receipt TEXT NOT NULL)')
                db.execute('INSERT INTO ue_operator_reconciliations VALUES (?,?)',
                           (zero_generation, json.dumps(audit, sort_keys=True)))
            # The receipt is also durable in SQLite if a filesystem write fails.
            with receipt.open('x', encoding='utf-8') as output:
                json.dump(audit, output, indent=2, sort_keys=True)
                output.write('\n')
            return {**audit, 'receipt': str(receipt.resolve())}
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--kubectl', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.state.resolve() != (ROOT / '.cache/kubeadm-lab/ue-state.sqlite3').resolve():
        raise SystemExit('Only the dedicated kubeadm lab state may be reconciled')
    if (ROOT / '.tmp' not in args.proof.resolve().parents or not args.kubectl.is_file()
            or not args.kubeconfig.is_file()):
        raise SystemExit('Explicit lab proof, kubeadm client, and kubeconfig required')
    print(json.dumps(reconcile(args.state, args.proof, args.generation, args.backup,
                               args.receipt, args.kubectl, args.kubeconfig,
                               dry_run=args.dry_run), sort_keys=True))


if __name__ == '__main__':
    main()
