"""Compensate a failed partial fixture, retaining every uncertain original write."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.request import Request
from urllib.error import HTTPError, URLError
from uuid import uuid4

from backend.onprem.validation import constrained as state
from backend.onprem.validation import normal as common
from backend.onprem.validation.native import seal_native_receipt, same_rule_state
from backend.onprem.validation.partial import TABLE, compensation_rows, validate_compensations
from backend.onprem.policy.group_qer import GroupQerAdapter
from backend.onprem.policy.group_qer_provider import QER_PATH


class PartialFixtureRecovery:
    """One original-rate POST per owner; a lost compensation is never resent."""

    def __init__(self, provider, journal, reader, *, config_sha256, registry):
        self.provider, self.journal, self.reader = provider, journal, reader
        self.config_sha256, self.registry = config_sha256, registry

    def restore(self):
        snapshot = self.reader()
        record = state.read_constrained_receipt(self.journal, snapshot.get('requestId'))
        claim = state.constrained_claim_for_snapshot(self.journal, snapshot, expected_quarantine_registry=self.registry)
        cleanup = [a for a in snapshot.get('actions', []) if a.get('kind') == 'adaptive_qer_cleanup']
        if (snapshot.get('stage') != 'cleaning' or snapshot.get('executionOutcome') not in {'failed','cancelled'}
                or record['state'] != 'active' or claim['configSha256'] != self.config_sha256
                or len(cleanup) != 1 or cleanup[0].get('cleanupVerified') is not True
                or cleanup[0].get('reconciliationRequired') is not False or cleanup[0].get('policyActionIds') != []
                or any(a.get('kind') in {'traffic_window','traffic_aggregate'} for a in snapshot.get('actions', []))):
            raise state.ConstrainedFixtureBlocked('Partial recovery lacks an exact failed pre-model owner')
        path = state.fixture_path(self.journal)
        with closing(state._database(path)) as db:
            attempts = [common._read_json(r[0]) for r in db.execute(
                'SELECT record FROM fixture_attempts WHERE request_id=?', (snapshot['requestId'],))]
            db.execute(TABLE); db.commit()
            existing = compensation_rows(db, snapshot['requestId'])
        if (not attempts or any(a['phase'] != 'fixture_install' or a['state'] not in {'accepted','unknown'} for a in attempts)
                or len({a['supi'] for a in attempts}) != len(attempts)):
            raise state.ConstrainedFixtureBlocked('Partial install still has an in-flight or foreign intent')
        # An independent model journal must also be empty for this owner.
        with closing(state.sqlite3.connect(self.journal.as_uri()+'?mode=ro', uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_qer_actions'").fetchone():
                raise state.ConstrainedFixtureBlocked('Partial recovery model journal is unavailable')
            if db.execute('SELECT 1 FROM group_qer_actions WHERE round_id=? LIMIT 1', (snapshot['roundId'],)).fetchone():
                raise state.ConstrainedFixtureBlocked('Partial fixture has a model policy action')
        accepted = {}
        for _, _, supi, raw in existing:
            item = common._read_json(raw)
            if (item.get('state') != 'accepted' or item.get('rates') != state._original_rates(record, supi)
                    or any(item.get(k) != snapshot.get(k) for k in ('roundId','generation','requestId'))
                    or item.get('manifestDigest') != record['manifestDigest']
                    or item.get('httpStatus') != 200 or item.get('pfcpAccepted') is not True
                    or item.get('uncertain') is not False or item.get('errorCategory') is not None):
                raise state.ConstrainedFixtureBlocked('Partial compensation is unresolved; no resend')
            accepted[supi] = item
        originals = record['originalNative']['native']['perUe']
        attempted = {a['supi'] for a in attempts}

        def owned():
            if self.reader() != snapshot or state.read_constrained_receipt(self.journal, snapshot['requestId']) != record:
                raise state.ConstrainedFixtureBlocked('Partial recovery owner or fixture archive changed')

        def observe(final=False):
            owned(); dumps = {}
            proof = self.provider.native(snapshot, _fixture_raw_sink=dumps)
            native = seal_native_receipt(snapshot, claim, proof, dumps, 'original_restored')
            actual = {r['supi']: r for r in native['native']['perUe']}
            if set(actual) != {r['supi'] for r in originals}:
                raise state.ConstrainedFixtureBlocked('Partial recovery cohort changed')
            for original in originals:
                row = actual[original['supi']]
                cap = deepcopy(original['qers'])
                next(q for q in cap if q['id'] == original['originalAmbrQerId'])['mbrKbps'] = dict.fromkeys(('ul','dl'), claim['fixtureCapKbps'])
                original_match = same_rule_state(row['qers'], original['qers'])
                cap_allowed = not final and original['supi'] in attempted and original['supi'] not in accepted
                if (any(row.get(k) != original.get(k) for k in ('supi','sessionId','smContextRef','pfcpSeid','upfEpoch','slice','upf','sst','sd'))
                        or not same_rule_state(row['pdrs'], original['pdrs'])
                        or not (original_match or cap_allowed and same_rule_state(row['qers'], cap))):
                    raise state.ConstrainedFixtureBlocked('Partial recovery has a foreign native rule or identity')
            before_qos, after_qos = record['originalNative']['native']['qosBySlice'], native['native']['qosBySlice']
            if (set(before_qos) != set(after_qos) or any(any(before_qos[s].get(k) != after_qos[s].get(k)
                    for k in ('epoch','enabled','readback')) for s in before_qos)):
                raise state.ConstrainedFixtureBlocked('Partial recovery QoS epoch changed')
            owned(); return native

        before = observe()
        profiles = getattr(self.provider, '_profiles', {})
        if not profiles or self.provider.capability(snapshot, tuple(profiles)) is None:
            raise state.ConstrainedFixtureBlocked('Partial recovery SMF commissioning is unavailable')
        token = self.provider._token()
        for original in sorted(originals, key=lambda r:r['supi']):
            supi = original['supi']
            if supi in accepted: continue
            owned()
            intent = dict(intentId=uuid4().hex, requestId=snapshot['requestId'], roundId=snapshot['roundId'],
                generation=snapshot['generation'], manifestDigest=record['manifestDigest'], supi=supi,
                rates=deepcopy(state._original_rates(record, supi)), state='dispatched', httpStatus=None,
                pfcpAccepted=False, observedAt=None, uncertain=True, errorCategory=None)
            with closing(state._database(path)) as db:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT record FROM constrained_arms WHERE request_id=?', (snapshot['requestId'],)).fetchone()
                    if current is None or common._read_json(current[0]) != record:
                        raise state.ConstrainedFixtureBlocked('Partial restore intent lost its source')
                    try: db.execute('INSERT INTO fixture_compensations VALUES(?,?,?,?)',
                        (intent['intentId'],snapshot['requestId'],supi,common._canonical(intent).decode()))
                    except state.sqlite3.IntegrityError as exc:
                        raise state.ConstrainedFixtureBlocked('Partial restore was already attempted; no resend') from exc
            owned()
            request = Request(self.provider.smf_url+QER_PATH, method='POST',
                data=json.dumps(dict(supi=supi,uplinkMbrKbps=intent['rates']['ul'],downlinkMbrKbps=intent['rates']['dl'])).encode(),
                headers={'Content-Type':'application/json','X-SMF-QER-Actuator-Token':token})
            status, body, error = None, None, 'invalid_response'
            try:
                with self.provider.opener.open(request, timeout=self.provider.timeout) as response:
                    status, raw = response.status, response.read(65537)
                if len(raw) <= 65536: body = common._read_json(raw)
            except HTTPError as exc: status, error = exc.code, 'http'
            except (TimeoutError, URLError): error = 'connection_or_timeout'
            except Exception: error = 'exception'
            owner = next(r for r in snapshot['perUe'] if r['supi'] == supi)
            success = GroupQerAdapter._response(status, body, owner, intent['rates'])
            settled = {**intent, 'state':'accepted' if success else 'unknown', 'httpStatus':status,
                'pfcpAccepted':success, 'uncertain':not success, 'errorCategory':None if success else error,
                'observedAt':datetime.now(timezone.utc).isoformat() if success else None}
            with closing(state._database(path)) as db:
                with db:
                    changed = db.execute('UPDATE fixture_compensations SET record=? WHERE id=? AND record=?',
                        (common._canonical(settled).decode(),intent['intentId'],common._canonical(intent).decode())).rowcount
                    if changed != 1: raise state.ConstrainedFixtureBlocked('Partial compensation receipt changed')
            if not success: raise state.ConstrainedFixtureBlocked('Partial compensation outcome is unknown; no resend')
            accepted[supi] = settled
        after = observe(final=True)
        reconciled = {**record,'state':'reconciled','writePossible':False,'originalRestoredNative':after}
        with closing(state._database(path)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                validate_compensations(reconciled, compensation_rows(db, snapshot['requestId']))
                old = db.execute('SELECT record FROM constrained_arms WHERE request_id=?',(snapshot['requestId'],)).fetchone()
                if old is None or common._read_json(old[0]) != record:
                    raise state.ConstrainedFixtureBlocked('Partial recovery seal source changed')
                changed = db.execute('UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?',
                    (common._canonical(reconciled).decode(),snapshot['requestId'],old[0])).rowcount
                if changed != 1: raise state.ConstrainedFixtureBlocked('Partial recovery seal lost source CAS')
        final = state.read_constrained_receipt(self.journal, snapshot['requestId'])
        if final['state'] != 'reconciled': raise state.ConstrainedFixtureBlocked('Partial recovery archive replay failed')
        return {'claim':claim,'reconciled':True,'compensatedCount':len(accepted),
                'beforeNativeSha256':common._digest(before),'originalRestoredNativeSha256':common._digest(after)}
