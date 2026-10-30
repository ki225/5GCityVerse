"""One compensation for an uncertain fixture restore still observed at its cap."""
from contextlib import closing
from copy import deepcopy
import sqlite3

from backend.onprem.validation import constrained as state
from backend.onprem.validation import normal as common
from backend.onprem.validation.native import seal_native_receipt, same_rule_state


class FixtureRestoreRecovery:
    def __init__(self, provider, journal, reader, *, config_sha256, registry):
        self.provider, self.journal, self.reader = provider, journal, reader
        self.config_sha256, self.registry = config_sha256, registry

    def repair(self, supi):
        snapshot = self.reader()
        if (snapshot.get('stage') != 'cleaning' or snapshot.get('cleanupStatus') != 'blocked'
                or snapshot.get('executionOutcome') not in {'failed', 'cancelled'}):
            raise state.ConstrainedFixtureBlocked('Fixture recovery lacks a blocked cleanup owner')
        record = state.read_constrained_receipt(self.journal, snapshot['requestId'])
        claim = state.constrained_claim_for_snapshot(self.journal, snapshot,
                    expected_quarantine_registry=self.registry)
        if record['state'] != 'inner_restored' or claim['configSha256'] != self.config_sha256:
            raise state.ConstrainedFixtureBlocked('Fixture recovery owner differs')
        with closing(sqlite3.connect(state.fixture_path(self.journal).as_uri()+'?mode=ro', uri=True)) as db:
            attempts = [common._read_json(x[0]) for x in db.execute(
                'SELECT record FROM fixture_attempts WHERE request_id=?', (snapshot['requestId'],))]
        uncertain = [a for a in attempts if a['state'] != 'accepted']
        if (len(uncertain) != 1 or uncertain[0]['phase'] != 'fixture_restore'
                or uncertain[0]['state'] != 'unknown' or uncertain[0]['supi'] != supi
                or any(a['phase'] == 'fixture_restore_repair' for a in attempts)):
            raise state.ConstrainedFixtureBlocked('Fixture compensation is not a unique unattempted repair')
        restored = {a['supi'] for a in attempts if a['phase'] == 'fixture_restore' and a['state'] == 'accepted'}

        def observe(repaired=False):
            if self.reader() != snapshot:
                raise state.ConstrainedFixtureBlocked('Fixture recovery owner changed')
            dumps = {}
            proof = self.provider.native(snapshot, _fixture_raw_sink=dumps)
            native = seal_native_receipt(snapshot, claim, proof, dumps, 'original_restored')
            actual = {r['supi']: r for r in native['native']['perUe']}
            originals = record['originalNative']['native']['perUe']
            if set(actual) != {r['supi'] for r in originals}:
                raise state.ConstrainedFixtureBlocked('Fixture recovery cohort changed')
            for original in originals:
                row = actual[original['supi']]
                fields = ('supi','sessionId','smContextRef','pfcpSeid','upfEpoch','slice','upf','sst','sd')
                expected = deepcopy(original['qers'])
                at_original = original['supi'] in restored or (repaired and original['supi'] == supi)
                selected = next(q for q in expected if q['id'] == original['originalAmbrQerId'])
                if not at_original:
                    selected['mbrKbps'] = dict.fromkeys(('ul','dl'), claim['fixtureCapKbps'])
                if (any(row.get(k) != original.get(k) for k in fields)
                        or not same_rule_state(row['qers'], expected)
                        or not same_rule_state(row['pdrs'], original['pdrs'])):
                    raise state.ConstrainedFixtureBlocked('Fixture recovery has foreign QER or native identity')
            if self.reader() != snapshot:
                raise state.ConstrainedFixtureBlocked('Fixture recovery owner changed after observation')
            return native

        authority = self.provider.begin_fixture_batch(self.journal, snapshot,
            expected_config_sha256=self.config_sha256, expected_quarantine_registry=self.registry,
            phase='fixture_restore')
        try:
            before = observe()
            intent = self.provider.begin_fixture_restore_intent(self.journal, snapshot, supi,
                batch_authority=authority, expected_quarantine_registry=self.registry, _repair=True)
            observe()  # Fresh state and owner immediately before consuming dispatch.
            response = self.provider.request_fixture_restore(self.journal, snapshot, intent['intentId'],
                batch_authority=authority, expected_config_sha256=self.config_sha256,
                expected_quarantine_registry=self.registry, _repair=True)
            after = observe(repaired=True)
        finally:
            self.provider.end_fixture_batch(authority)
        return {'schema':'cityverse.fixture-restore-compensation/v1','roundId':snapshot['roundId'],
            'generation':snapshot['generation'],'originalAttempt':uncertain[0], 'repairResponse':response,
            'beforeNative':before,'afterNative':after}
