import json
from datetime import datetime, timedelta, timezone
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.onprem.runtime.ue import GenerationStore
from scripts.onprem import retire_capacity_intent_offline as offline


def proof(generation):
    now = datetime.now(timezone.utc).isoformat()
    return {'generation': generation, 'observedAt': now, 'ownedPods': [], 'rows': [],
            'amfRegistered': 0, 'smfSessions': 0, 'upfPfcpRows': 0,
            'unattributedUpfRows': 0, 'gnbUeCounts': offline.ZERO,
            'subscriberVerifiedCount': 1000, 'subscriberCredentialsMatched': True,
            'sources': {name: {'available': True, 'observedAt': now,
                               'inventoryComplete': True if name == 'smf' else None}
                        for name in ('amf', 'smf', 'upf')}}


class OfflineRetirementTests(unittest.TestCase):
    def test_rejects_stale_or_nonzero_native_proof(self):
        current = proof(199)
        current['observedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            offline.check_proof(current, 199, now=datetime.now(timezone.utc))
        current = proof(199)
        current['smfSessions'] = 1
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            offline.check_proof(current, 199, now=datetime.now(timezone.utc))

    def test_only_upf_allows_bounded_guest_clock_ahead(self):
        now = datetime.now(timezone.utc)
        current = proof(199)
        current['sources']['upf']['observedAt'] = (now + timedelta(seconds=6)).isoformat()
        offline.check_proof(current, 199, now=now)
        current['sources']['upf']['observedAt'] = (now + timedelta(seconds=11)).isoformat()
        with self.assertRaisesRegex(RuntimeError, 'upf native evidence'):
            offline.check_proof(current, 199, now=now)
        current['sources']['upf']['observedAt'] = now.isoformat()
        current['sources']['amf']['observedAt'] = (now + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(RuntimeError, 'amf native evidence'):
            offline.check_proof(current, 199, now=now)

    def test_atomic_zero_intent_under_exclusive_lock_without_controller(self):
        with tempfile.TemporaryDirectory(dir=offline.ROOT / '.tmp') as directory:
            state = Path(directory) / 'ue-state.sqlite3'
            store = GenerationStore(state)
            current, changed = store.request(offline.FAILED)
            self.assertTrue(changed)
            generation = current['generation']
            failure = {'generation': generation, 'requested': 100, 'status': 'registering',
                       'perUe': [{'supi': row['supi']} for row in current['allocation']]}
            backup = Path(directory) / 'before.sqlite3'
            with patch.object(offline.subprocess, 'check_output', return_value=json.dumps({'items': []})) as get_pods:
                result = offline.retire(state, proof(generation), failure, generation, backup,
                                        Path('kubectl.exe'), Path('kubeconfig'))
            self.assertTrue(get_pods.called)
            self.assertEqual(result['desired'], offline.ZERO)
            self.assertEqual(GenerationStore(state).current()['desired'], offline.ZERO)
            self.assertEqual(GenerationStore(backup).current()['desired'], offline.FAILED)
            self.assertEqual(GenerationStore(state).current()['allocation'], [])
            gc.collect()

    def test_refuses_failed_run_mismatch_without_db_transition(self):
        with tempfile.TemporaryDirectory(dir=offline.ROOT / '.tmp') as directory:
            state = Path(directory) / 'ue-state.sqlite3'
            store = GenerationStore(state)
            current, _ = store.request(offline.FAILED)
            with self.assertRaisesRegex(RuntimeError, 'Failed-run marker'):
                offline.retire(state, proof(current['generation']), {'generation': -1},
                               current['generation'], Path(directory) / 'backup.sqlite3',
                               Path('kubectl.exe'), Path('kubeconfig'))
            self.assertEqual(store.current()['generation'], current['generation'])
            gc.collect()


if __name__ == '__main__':
    unittest.main()
