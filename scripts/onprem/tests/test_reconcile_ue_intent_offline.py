import json
from datetime import datetime, timedelta, timezone
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.onprem.runtime.ue import GenerationStore
from scripts.onprem import reconcile_ue_intent_offline as operator


def zero_proof(generation):
    now = datetime.now(timezone.utc).isoformat()
    return {'generation': generation, 'observedAt': now, 'ownedPods': [], 'rows': [],
            'amfRegistered': 0, 'smfSessions': 0, 'upfPfcpRows': 0,
            'unattributedUpfRows': 0, 'gnbUeCounts': operator.ZERO,
            'gnbSource': 'free-ran-ue-gnb-api-ue-count',
            'gnbReadouts': {name: {'podUid': f'{name}-uid', 'observedAt': now}
                            for name in operator.ZERO},
            'subscriberVerifiedCount': 1000, 'subscriberCredentialsMatched': True,
            'sources': {name: {'available': True, 'observedAt': now,
                               'inventoryComplete': name == 'smf',
                               'epoch': json.dumps({slice_name: f'{slice_name}-epoch'
                                                    for slice_name in operator.ZERO}) if name == 'upf' else None}
                        for name in ('amf', 'smf', 'upf')}}


class OperatorReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=operator.ROOT / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state.sqlite3'
        self.store = GenerationStore(self.state)
        self.lock_path = Path(str(self.state) + '.lock')
        self.lock_path.write_bytes(b'0')
        current, _ = self.store.request({'embb': 1, 'urllc': 0, 'mmtc': 0, 'v2x': 0})
        self.generation = current['generation']
        status = current['result']
        status.update(status='degraded', errors=['Reconciliation timed out'], requested=1)
        self.store.save(status)
        self.proof_path = self.base / 'proof.json'
        self.backup = self.base / 'before.sqlite3'
        self.receipt = self.base / 'receipt.json'

    def tearDown(self):
        gc.collect()

    def run_operator(self, *, dry_run=False):
        with patch.object(operator.subprocess, 'check_output', return_value=json.dumps({'items': []})):
            return operator.reconcile(self.state, self.proof_path, self.generation,
                                      self.backup, self.receipt, Path('kubectl.exe'),
                                      Path('kubeconfig'), dry_run=dry_run)

    def test_dry_run_does_not_mutate_or_create_artifacts(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        before = operator._digest(self.state.read_bytes())
        result = self.run_operator(dry_run=True)
        self.assertTrue(result['dryRun'])
        self.assertEqual(operator._digest(self.state.read_bytes()), before)
        self.assertEqual(self.store.current()['generation'], self.generation)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.lock_path.read_bytes(), b'0')

    def test_dry_run_rejects_missing_lock_without_creating_it(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        self.lock_path.unlink()
        before = operator._digest(self.state.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'ownership lock is missing'):
            self.run_operator(dry_run=True)
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(operator._digest(self.state.read_bytes()), before)

    def test_rejects_nonterminal_intent_with_errors(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        status = self.store.current()['result']
        status['status'] = 'registering'
        self.store.save(status)
        with self.assertRaisesRegex(RuntimeError, 'not a quarantined failed intent'):
            self.run_operator(dry_run=True)
        self.assertEqual(self.store.current()['generation'], self.generation)

    def test_rejects_missing_upf_epoch_and_stale_gnb(self):
        proof = zero_proof(self.generation)
        proof['sources']['upf']['epoch'] = None
        self.proof_path.write_text(json.dumps(proof), encoding='utf-8')
        with self.assertRaisesRegex((RuntimeError, TypeError), 'UPF'):
            self.run_operator()
        proof = zero_proof(self.generation)
        proof['gnbReadouts']['embb']['observedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.proof_path.write_text(json.dumps(proof), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'gNB readout is stale'):
            self.run_operator()
        self.assertEqual(self.store.current()['generation'], self.generation)

    def test_rejects_pod_reappearance(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        with patch.object(operator.subprocess, 'check_output', return_value=json.dumps({'items': [{}]})):
            with self.assertRaisesRegex(RuntimeError, 'Pods appeared'):
                operator.reconcile(self.state, self.proof_path, self.generation,
                                   self.backup, self.receipt, Path('kubectl.exe'), Path('kubeconfig'))
        self.assertEqual(self.store.current()['generation'], self.generation)

    def test_rejects_malformed_pod_inventory(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        with patch.object(operator.subprocess, 'check_output', return_value=json.dumps({'items': None})):
            with self.assertRaisesRegex(RuntimeError, 'Pod inventory is malformed'):
                operator.reconcile(self.state, self.proof_path, self.generation,
                                   self.backup, self.receipt, Path('kubectl.exe'), Path('kubeconfig'))
        self.assertEqual(self.store.current()['generation'], self.generation)

    def test_rechecks_pods_before_transaction_commit(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        responses = [json.dumps({'items': []}), json.dumps({'items': [{}]})]
        with patch.object(operator.subprocess, 'check_output', side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, 'Pods appeared'):
                operator.reconcile(self.state, self.proof_path, self.generation,
                                   self.backup, self.receipt, Path('kubectl.exe'), Path('kubeconfig'))
        self.assertEqual(self.store.current()['generation'], self.generation)
        self.assertFalse(self.receipt.exists())

    def test_transaction_preserves_failed_row_and_receipt(self):
        self.proof_path.write_text(json.dumps(zero_proof(self.generation)), encoding='utf-8')
        result = self.run_operator()
        self.assertEqual(result['previousGeneration'], self.generation)
        self.assertEqual(self.store.current()['desired'], operator.ZERO)
        self.assertEqual(GenerationStore(self.backup).current()['generation'], self.generation)
        with sqlite3.connect(self.state) as db:
            durable = json.loads(db.execute('SELECT receipt FROM ue_operator_reconciliations').fetchone()[0])
        self.assertEqual(durable, json.loads(self.receipt.read_text(encoding='utf-8')))
        self.assertEqual(durable['zeroGeneration'], self.store.current()['generation'])
        self.assertEqual(durable['backupSha256'], operator._digest(self.backup.read_bytes()))


if __name__ == '__main__':
    unittest.main()
