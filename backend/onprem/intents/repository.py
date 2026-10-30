"""SQLite intent journal. Transactions never span external work."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class IntentConflict(ValueError):
    pass


class IntentNotFound(LookupError):
    pass


class IntentRepository:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=5000')
            yield db

    def initialize(self):
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL,
                    value TEXT NOT NULL, state TEXT NOT NULL, issues TEXT NOT NULL,
                    membership TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, owner TEXT NOT NULL,
                    kind TEXT NOT NULL, revision INTEGER NOT NULL, key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                    value TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(owner,key));
                CREATE TABLE IF NOT EXISTS leases (
                    scope TEXT PRIMARY KEY, operation_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS operation_pending ON operations(status,created);
                CREATE INDEX IF NOT EXISTS operation_latest ON operations(intent_id,owner,created DESC);
                CREATE INDEX IF NOT EXISTS intent_owner ON intents(owner,updated);
                CREATE INDEX IF NOT EXISTS intent_state_id ON intents(state,id);
                CREATE TABLE IF NOT EXISTS policy_claims (
                    scope TEXT PRIMARY KEY, intent_id TEXT NOT NULL, operation_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qos_claims (
                    session_key TEXT NOT NULL, intent_id TEXT NOT NULL,
                    target_id TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(session_key,intent_id));
                CREATE TABLE IF NOT EXISTS qos_effective (
                    session_key TEXT PRIMARY KEY, target_id TEXT NOT NULL,
                    baseline TEXT NOT NULL, receipt TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qos_policy_definitions (
                    intent_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_admission_resources (
                    intent_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    operation_id TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intent_traffic_resources (
                    intent_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    operation_id TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleet_closing (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),intent_id TEXT NOT NULL,
                    owner TEXT NOT NULL,operation_id TEXT NOT NULL,request_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dependency_epoch (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO dependency_epoch VALUES (1,0);
            ''')

    @staticmethod
    def _intent(row):
        return dict(**json.loads(row['value']), status=row['state'], issues=json.loads(row['issues']),
                    membership=json.loads(row['membership']))

    @staticmethod
    def _operation(row):
        return dict(operation_id=row['id'], intent_id=row['intent_id'], revision=row['revision'],
                    kind=row['kind'], status=row['status'], stage=row['stage'],
                    created_at=row['created'], updated_at=row['updated'], **json.loads(row['value']))

    def get(self, intent_id, owner):
        with self.connect() as db:
            row = db.execute('SELECT * FROM intents WHERE id=? AND owner=?', (intent_id,owner)).fetchone()
        if row is None:
            raise IntentNotFound('Intent not found')
        return self._intent(row)

    def list(self, owner, offset=0, limit=50):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM intents WHERE owner=? ORDER BY updated DESC,id LIMIT ? OFFSET ?',
                              (owner,limit,offset)).fetchall()
        return [self._intent(r) for r in rows]

    def replace_draft(self, payload, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM fleet_closing').fetchone():
                raise IntentConflict('Native city fleet is closing; draft dependencies are not admitted')
            db.execute('UPDATE intents SET value=?,updated=? WHERE id=? AND owner=? AND revision=1',
                       (json.dumps(payload,allow_nan=False),time.time(),payload['intent_id'],owner))
            self._advance_dependency_epoch(db)

    def submit(self, payload, owner, key, fingerprint, *, kind='reconcile', expected_revision=None, metadata=None):
        now, op_id = time.time(), str(uuid4())
        intent_id = payload['intent_id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM operations WHERE owner=? AND key=?', (owner,key)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise IntentConflict('Idempotency key reused with different request')
                return self._operation(previous)
            if kind!='release' and db.execute('SELECT 1 FROM fleet_closing').fetchone():
                raise IntentConflict('Native city fleet is closing; new dependencies are not admitted')
            current = db.execute('SELECT * FROM intents WHERE id=?', (intent_id,)).fetchone()
            if current and current['owner'] != owner:
                raise IntentNotFound('Intent not found')
            if current and not metadata:
                latest = db.execute('SELECT value FROM operations WHERE intent_id=? AND owner=? ORDER BY created DESC LIMIT 1',(intent_id,owner)).fetchone()
                inherited = json.loads(latest[0]).get('expected_traffic_request_id') if latest else None
                prior = json.loads(latest[0]) if latest else {}
                metadata = {'expected_traffic_request_id':inherited,
                            'traffic_admission_state':prior.get('traffic_admission_state','pending')} if inherited else {}
            if expected_revision is None and current:
                if current['state'] == 'expired' and kind != 'release':
                    raise IntentConflict('Released intent requires an explicit new revision')
                if current['revision'] != payload['revision']:
                    raise IntentConflict('Intent revision changed')
            elif expected_revision is not None:
                if not current or current['revision'] != expected_revision:
                    raise IntentConflict('Intent revision changed')
            if current is None:
                db.execute('INSERT INTO intents(id,owner,revision,value,state,issues,updated) VALUES (?,?,?,?,?,?,?)',
                           (intent_id,owner,payload['revision'],json.dumps(payload,allow_nan=False),'active','[]',now))
            elif expected_revision is not None:
                db.execute('UPDATE intents SET revision=?,value=?,state=?,issues=?,updated=? WHERE id=?',
                           (payload['revision'],json.dumps(payload,allow_nan=False),'active','[]',now,intent_id))
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (op_id,intent_id,owner,kind,payload['revision'],key,fingerprint,'queued','queued',json.dumps(metadata or {},allow_nan=False),now,now))
            row = db.execute('SELECT * FROM operations WHERE id=?',(op_id,)).fetchone()
            self._advance_dependency_epoch(db)
        return self._operation(row)

    @staticmethod
    def _advance_dependency_epoch(db):
        db.execute('UPDATE dependency_epoch SET revision=revision+1 WHERE singleton=1')

    @staticmethod
    def _assert_fleet_open(db,intent_id,operation_id):
        closing=db.execute('SELECT * FROM fleet_closing WHERE singleton=1').fetchone()
        if closing:
            operation=db.execute('SELECT kind,intent_id,value FROM operations WHERE id=?',(operation_id,)).fetchone()
            value=json.loads(operation['value']) if operation else {}
            failure_owner=(operation_id==closing['operation_id'] and operation is not None
                and operation['intent_id']==closing['intent_id']==intent_id
                and value.get('failure_cleanup') is True and value.get('release_purpose')=='full_release')
            if (not operation or operation['intent_id']!=intent_id
                    or value.get('release_purpose')=='membership_withdrawal'
                    or (operation['kind']!='release' and not failure_owner)):
                raise IntentConflict('Native city fleet is closing; new dependencies are not admitted')

    def assert_fleet_open(self,intent_id,operation_id):
        with self.connect() as db:self._assert_fleet_open(db,intent_id,operation_id)

    def dependency_version(self):
        with self.connect() as db:
            return db.execute('SELECT revision FROM dependency_epoch WHERE singleton=1').fetchone()[0]

    def fleet_closing_state(self):
        with self.connect() as db:
            row=db.execute('SELECT * FROM fleet_closing WHERE singleton=1').fetchone()
            return dict(row) if row else None

    def begin_fleet_closing(self,intent_id,owner,operation_id,expected_epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM fleet_closing WHERE singleton=1').fetchone()
            op=db.execute('SELECT * FROM operations WHERE id=? AND owner=? AND intent_id=?',
                (operation_id,owner,intent_id)).fetchone()
            value=json.loads(op['value']) if op else {}
            failure_cleanup=value.get('failure_cleanup') is True and value.get('release_purpose')=='full_release'
            if (not op or (op['kind']!='release' and not failure_cleanup)
                    or value.get('release_purpose')=='membership_withdrawal'
                    or op['status'] not in ('queued','running')):
                raise IntentConflict('Owned release operation required for fleet closing')
            if prior:
                if (prior['intent_id'],prior['owner'],prior['operation_id'])!=(intent_id,owner,operation_id):
                    raise IntentConflict('Native city fleet closing owner changed')
                return dict(prior)
            if db.execute('SELECT revision FROM dependency_epoch WHERE singleton=1').fetchone()[0]!=expected_epoch:
                raise IntentConflict('Fleet dependency epoch changed before closing')
            busy=db.execute("SELECT 1 FROM operations o JOIN intents i ON i.id=o.intent_id "
                "WHERE o.id!=? AND o.kind!='release' AND i.state!='expired' "
                "AND o.status IN ('queued','running','recovery_required') LIMIT 1",(operation_id,)).fetchone()
            if busy:raise IntentConflict('Fleet dependencies have outstanding operations')
            traffic=db.execute('SELECT value FROM intent_traffic_resources WHERE intent_id=? AND owner=?',(intent_id,owner)).fetchone()
            if not traffic:return None
            request=json.loads(traffic[0])['receipt']['requestId']
            db.execute('INSERT INTO fleet_closing VALUES (1,?,?,?,?)',(intent_id,owner,operation_id,request))
            return dict(intent_id=intent_id,owner=owner,operation_id=operation_id,request_id=request)

    def finish_fleet_closing(self,intent_id,owner,operation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM fleet_closing WHERE intent_id=? AND owner=? AND operation_id=?',
                    (intent_id,owner,operation_id)).fetchone():return
            op=db.execute('SELECT * FROM operations WHERE id=? AND owner=? AND intent_id=?',(operation_id,owner,intent_id)).fetchone()
            value=json.loads(op['value']) if op else {}
            cleanup=op is not None and value.get('failure_cleanup') is True and value.get('release_purpose')=='full_release'
            if (not op or (op['kind']!='release' and not cleanup)
                    or value.get('release_purpose')=='membership_withdrawal'
                    or op['status'] not in (('completed','degraded') if cleanup else ('completed',))
                    or value.get('native_ues_gone') is not True or value.get('traffic_drain_protocol')!='owned-native-ue-gone/v1'):
                raise IntentConflict('Verified completed native fleet release required')
            db.execute('DELETE FROM fleet_closing WHERE singleton=1 AND intent_id=? AND owner=? AND operation_id=?',
                (intent_id,owner,operation_id))

    def operation(self, operation_id, owner):
        with self.connect() as db:
            row = db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
        if row is None:
            raise IntentNotFound('Operation not found')
        return self._operation(row)

    def recover_failed_preparation(self,operation_id,owner,expected_fingerprint,proof_summary,*,expected_traffic_fingerprint):
        """CAS one no-dispatch operation after a trusted typed native readback.

        This private port cannot clear leases, infer external absence, recover an
        unknown native write, or change the intent/current successful traffic.
        """
        import math,re
        from .preparation_recovery import preparation_recovery_fingerprint
        expected_fields={'source','observed_at','external_effects_absent','dispatch_started','proof_sha256'}
        if (not isinstance(proof_summary,dict) or set(proof_summary)!=expected_fields
                or proof_summary.get('source')!='fixed-reservation-only-no-dispatch/v1'
                or proof_summary.get('external_effects_absent') is not True
                or proof_summary.get('dispatch_started') is not False
                or type(proof_summary.get('observed_at')) not in (int,float)
                or not math.isfinite(proof_summary['observed_at']) or proof_summary['observed_at']<=0
                or not isinstance(proof_summary.get('proof_sha256'),str)
                or re.fullmatch(r'[0-9a-f]{64}',proof_summary['proof_sha256']) is None
                or not isinstance(expected_fingerprint,str)
                or re.fullmatch(r'[0-9a-f]{64}',expected_fingerprint) is None
                or not isinstance(expected_traffic_fingerprint,str)
                or re.fullmatch(r'[0-9a-f]{64}',expected_traffic_fingerprint) is None):
            raise IntentConflict('Bounded validated preparation recovery summary required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
            if row is None: raise IntentNotFound('Operation not found')
            actual=self._operation(row);value=json.loads(row['value'])
            traffic=db.execute('SELECT value FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
                (row['intent_id'],owner)).fetchone()
            if not traffic or preparation_recovery_fingerprint(json.loads(traffic[0]))!=expected_traffic_fingerprint:
                raise IntentConflict('Current successful traffic changed before preparation recovery')
            if (preparation_recovery_fingerprint(actual)!=expected_fingerprint
                    or row['kind']!='reconcile' or row['status']!='recovery_required'
                    or value.get('pending_session_preparation') is not True
                    or any(value.get(k) for k in ('receipts','pending_action','pending_traffic_start','traffic_started',
                        'traffic_admission_receipt','prepared_session_admissions','pending_restoration',
                        'pending_release','pending_traffic_release','native_restoration_complete'))):
                raise IntentConflict('Original preparation changed or has external effects')
            if (db.execute('SELECT 1 FROM leases WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone()
                    or db.execute('SELECT 1 FROM session_admission_resources WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone()
                    or db.execute('SELECT 1 FROM intent_traffic_resources WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone()):
                raise IntentConflict('Preparation still owns native resources or leases')
            value.update(pending_session_preparation=None,traffic_admission_failed=True,traffic_admission_state='failed',
                preparation_recovery_proof=dict(proof_summary))
            db.execute('UPDATE operations SET status=?,stage=?,value=?,updated=? WHERE id=? AND owner=?',
                ('traffic_admission_failed','preparation_no_dispatch_verified',json.dumps(value,allow_nan=False),time.time(),operation_id,owner))
            return self._operation(db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone())

    def recover_baseline_no_hold(self,operation_id,owner,expected_fingerprint,proof_summary):
        """CAS only the original no-dispatch pending operation; no foreign state changes."""
        from .baseline_preparation_recovery import SOURCE,validate_operation
        from .preparation_recovery import preparation_recovery_fingerprint as fingerprint
        import math,re
        fields={'source','observed_at','generation','reservation_fingerprint','native_evidence_sha256',
            'proof_sha256','dispatch_started','external_effects_absent'}
        if (type(proof_summary) is not dict or set(proof_summary)!=fields
                or proof_summary.get('source')!=SOURCE or proof_summary.get('dispatch_started') is not False
                or proof_summary.get('external_effects_absent') is not True
                or type(proof_summary.get('observed_at')) not in (int,float)
                or not math.isfinite(proof_summary['observed_at'])
                or not -0.000001<=time.time()-proof_summary['observed_at']<=15
                or type(proof_summary.get('generation')) is not int or proof_summary['generation']<0
                or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in
                    (expected_fingerprint,proof_summary.get('reservation_fingerprint'),
                     proof_summary.get('native_evidence_sha256'),proof_summary.get('proof_sha256')))):
            raise IntentConflict('Fresh bounded baseline recovery proof required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
            if row is None:raise IntentNotFound('Operation not found')
            actual=self._operation(row)
            if fingerprint(actual)!=expected_fingerprint:
                raise IntentConflict('Original baseline preparation changed')
            validate_operation(actual)
            current=db.execute('SELECT revision FROM intents WHERE id=? AND owner=?',(row['intent_id'],owner)).fetchone()
            if current is None or current[0]!=row['revision']:
                raise IntentConflict('Intent revision changed before baseline recovery')
            if (db.execute('SELECT 1 FROM intent_traffic_resources WHERE intent_id=? AND owner=? LIMIT 1',
                    (row['intent_id'],owner)).fetchone()
                    or db.execute('SELECT 1 FROM leases WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone()
                    or db.execute('SELECT 1 FROM session_admission_resources WHERE operation_id=? LIMIT 1',(operation_id,)).fetchone()):
                raise IntentConflict('Baseline preparation still owns resources or traffic')
            value=json.loads(row['value'])
            value.update(pending_session_preparation=None,traffic_admission_failed=True,
                traffic_admission_state='failed',baseline_preparation_recovery_proof=dict(proof_summary))
            db.execute('UPDATE operations SET status=?,stage=?,value=?,updated=? WHERE id=? AND owner=?',
                ('traffic_admission_failed','baseline_preparation_no_dispatch_verified',
                 json.dumps(value,allow_nan=False),time.time(),operation_id,owner))
            return self._operation(db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone())

    def blocking_release(self,intent_id,owner,revision):
        """Resolve the exact owned intent fence, never scan or steal old leases."""
        with self.connect() as db:
            row=db.execute('SELECT o.* FROM leases l JOIN operations o ON o.id=l.operation_id '
                'WHERE l.scope=? AND o.intent_id=? AND o.owner=? AND o.revision=? AND o.kind=?',
                ('intent:'+intent_id,intent_id,owner,revision,'release')).fetchone()
        if row is None:
            return None
        operation=self._operation(row)
        if (operation['status'] not in {'needs_definition','recovery_required','degraded','stale','blocked'}
                or operation.get('policy_restoration_complete') is not True
                or operation.get('release_phase') not in {'policy_restored','draining_traffic','restoring_infrastructure'}):
            return None
        return operation

    def other_current_policy_sessions(self,intent_id):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT session_key FROM qos_claims WHERE intent_id!=?',(intent_id,))]

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM operations WHERE status IN ('queued','running') ORDER BY created").fetchall()
        return [(r['owner'],self._operation(r)) for r in rows]

    def current_intent_traffic(self,intent_id,owner):
        with self.connect() as db:
            row=db.execute('SELECT * FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
                           (intent_id,owner)).fetchone()
        return json.loads(row['value']) if row else None

    def mark_traffic_restored(self,intent_id,owner,release_operation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
                           (intent_id,owner)).fetchone()
            if row:
                value=json.loads(row['value'])
                value.update(native_restoration_complete=True,restoration_operation_id=release_operation_id)
                db.execute('UPDATE intent_traffic_resources SET value=? WHERE intent_id=? AND owner=?',
                           (json.dumps(value,allow_nan=False),intent_id,owner))

    def renew_intent_traffic(self, lease, now):
        """Refresh an owned lease from the current contract, never from report history.

        The round ledger applies its own CAS afterwards. A crash between journals
        is safe: this method is replayable and preserves the immutable lease ID.
        """
        from dataclasses import replace
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        if type(lease) is not ServiceIntentTrafficLease:
            raise IntentConflict('Typed traffic lease required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            intent=db.execute('SELECT * FROM intents WHERE id=? AND owner=?',
                (lease.intent_id,lease.owner_id)).fetchone()
            row=db.execute('SELECT * FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
                (lease.intent_id,lease.owner_id)).fetchone()
            releasing=db.execute("SELECT 1 FROM operations WHERE intent_id=? AND owner=? "
                "AND kind='release' AND status IN ('queued','running') LIMIT 1",
                (lease.intent_id,lease.owner_id)).fetchone()
            if not intent or not row or intent['state']=='expired' or releasing:
                return None
            current=json.loads(row['value'])
            recorded=ServiceIntentTrafficLease.from_dict(current['receipt']['service_intent_traffic_lease'])
            identity=('lease_id','intent_id','owner_id','operation_id','request_id','valid_from')
            if any(getattr(recorded,k)!=getattr(lease,k) for k in identity):
                raise IntentConflict('Traffic renewal ownership changed')
            if current.get('native_restoration_complete') is True:
                return None
            contract=json.loads(intent['value'])
            from .domain import timestamp
            valid_from=timestamp(contract['valid_from'])
            valid_until=timestamp(contract['valid_until'])
            if valid_from!=lease.valid_from or not valid_from<=now<valid_until:
                return None
            if intent['revision']<max(recorded.intent_revision,lease.intent_revision):
                raise IntentConflict('Traffic renewal revision regressed')
            renewed=replace(recorded,intent_revision=intent['revision'],valid_until=valid_until)
            current['receipt']['service_intent_traffic_lease']=renewed.to_dict()
            db.execute('UPDATE intent_traffic_resources SET value=? WHERE intent_id=? AND owner=?',
                (json.dumps(current,allow_nan=False),lease.intent_id,lease.owner_id))
            return renewed

    def clear_intent_traffic(self,intent_id,owner,start_operation_id):
        with self.connect() as db:
            db.execute('DELETE FROM intent_traffic_resources WHERE intent_id=? AND owner=? AND operation_id=?',
                       (intent_id,owner,start_operation_id))

    def update_operation(self, operation_id, *, status=None, stage=None, **data):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            value = json.loads(row['value'])
            value.update(data)
            db.execute('UPDATE operations SET status=?,stage=?,value=?,updated=? WHERE id=?',
                       (status or row['status'],stage or row['stage'],json.dumps(value,allow_nan=False),time.time(),operation_id))

    def abort_traffic(self, operation_id, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
            if row is None:
                raise IntentNotFound('Operation not found')
            value = json.loads(row['value'])
            if value.get('pending_action') or value.get('receipts') or value.get('qos_claims_committed'):
                raise IntentConflict('Native execution may have started; readback recovery is required')
            value['traffic_admission_failed'] = True
            value['error'] = {'code':'traffic_admission_failed'}
            db.execute("UPDATE operations SET status='traffic_admission_failed',stage='traffic_admission_failed',value=?,updated=? WHERE id=?",(json.dumps(value),time.time(),operation_id))

    def confirm_traffic(self, operation_id, owner, receipt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
            if row is None:
                raise IntentNotFound('Operation not found')
            value = json.loads(row['value'])
            if value.get('traffic_admission_failed'):
                raise IntentConflict('Failed traffic admission cannot be confirmed')
            if (not isinstance(receipt,dict) or receipt.get('accepted') is not True
                    or not value.get('expected_traffic_request_id')
                    or receipt.get('requestId') != value['expected_traffic_request_id']):
                raise IntentConflict('Traffic admission receipt does not match operation')
            value.update(traffic_admission_state='accepted',traffic_admission_receipt=receipt)
            lease=receipt.get('service_intent_traffic_lease')
            if lease is not None:
                if (not isinstance(lease,dict) or lease.get('intent_id')!=row['intent_id']
                        or lease.get('intent_revision')!=row['revision'] or lease.get('owner_id')!=owner
                        or lease.get('operation_id')!=operation_id or lease.get('request_id')!=receipt['requestId']
                        or not lease.get('lease_id')):
                    raise IntentConflict('Traffic lease identity does not match operation')
                existing=db.execute('SELECT operation_id FROM intent_traffic_resources WHERE intent_id=?',(row['intent_id'],)).fetchone()
                if existing and existing[0]!=operation_id:
                    raise IntentConflict('Current intent traffic must be released before replacement')
                current={'start_operation_id':operation_id,'receipt':receipt,'native_restoration_complete':False}
                db.execute('INSERT OR REPLACE INTO intent_traffic_resources VALUES (?,?,?,?)',
                           (row['intent_id'],owner,operation_id,json.dumps(current,allow_nan=False)))
            db.execute('UPDATE operations SET value=?,updated=? WHERE id=?',(json.dumps(value,allow_nan=False),time.time(),operation_id))

    def set_session_admissions(self,intent_id,owner,operation_id,revision,value):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT owner,revision,state FROM intents WHERE id=?',(intent_id,)).fetchone()
            if current is None or current['owner']!=owner or current['revision']!=revision or current['state']=='expired':
                raise IntentConflict('Intent changed before admission resource commit')
            previous=db.execute('SELECT operation_id FROM session_admission_resources WHERE intent_id=?',(intent_id,)).fetchone()
            if previous and previous[0]!=operation_id:
                raise IntentConflict('Existing selection resources require release before replacement')
            db.execute('INSERT OR REPLACE INTO session_admission_resources VALUES (?,?,?,?)',
                       (intent_id,owner,operation_id,json.dumps(value,allow_nan=False)))

    def session_admissions(self,intent_id,owner):
        with self.connect() as db:
            row=db.execute('SELECT value FROM session_admission_resources WHERE intent_id=? AND owner=?',(intent_id,owner)).fetchone()
        return json.loads(row[0]) if row else None

    def clear_session_admissions(self,intent_id,operation_id):
        with self.connect() as db:
            db.execute('DELETE FROM session_admission_resources WHERE intent_id=? AND operation_id=?',(intent_id,operation_id))

    def update_intent(self, intent_id, revision, state, *, issues=(), membership=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            closing=db.execute('SELECT intent_id FROM fleet_closing WHERE singleton=1').fetchone()
            if closing and closing['intent_id']!=intent_id and state in {'active','satisfied','degraded'}:
                raise IntentConflict('Native city fleet is closing; dependent publication is fenced')
            current=db.execute('SELECT membership FROM intents WHERE id=? AND revision=?',(intent_id,revision)).fetchone()
            if current:
                from .service_policy_state import preserve_pending_membership
                state,membership,issues=preserve_pending_membership(json.loads(current[0]),state,membership,issues)
            if membership is None:
                db.execute("UPDATE intents SET state=?,issues=?,updated=? WHERE id=? AND revision=? AND (state!='expired' OR ?='expired')",
                           (state,json.dumps(issues),time.time(),intent_id,revision,state))
            else:
                db.execute("UPDATE intents SET state=?,issues=?,membership=?,updated=? WHERE id=? AND revision=? AND (state!='expired' OR ?='expired')",
                           (state,json.dumps(issues),json.dumps(membership),time.time(),intent_id,revision,state))
            self._advance_dependency_epoch(db)

    def acquire(self, operation_id, scopes, ttl=300, *, intent_id=None, shared_scopes=()):
        now = time.time()
        scopes = sorted(set(scopes))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if intent_id is not None:
                self._assert_pending_member_coverage(db,intent_id,operation_id,
                    {scope[8:] for scope in scopes if scope.startswith('session:')})
            # Expired leases remain exclusive until recovery checks external state.
            for scope in scopes:
                claim = db.execute('SELECT intent_id FROM policy_claims WHERE scope=?',(scope,)).fetchone()
                if claim and claim[0] != intent_id and not (claim[0] == 'shared:qos' and scope in shared_scopes):
                    raise IntentConflict('Overlapping effective policies require an explicit merged owner')
                row = db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
                if row and row[0] != operation_id:
                    raise IntentConflict('Resource is reserved by another operation')
            for scope in scopes:
                db.execute('INSERT OR REPLACE INTO leases VALUES (?,?,?)',(scope,operation_id,now+ttl))

    def claim_policy(self, intent_id, operation_id, scopes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for scope in sorted(set(scopes)):
                row = db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
                if row is None or row[0]!=operation_id:
                    raise IntentConflict('Resource lease lost before policy commit')
                db.execute('INSERT OR REPLACE INTO policy_claims VALUES (?,?,?)',(scope,intent_id,operation_id))

    def release_policy(self, intent_id):
        with self.connect() as db:
            db.execute('DELETE FROM policy_claims WHERE intent_id=?',(intent_id,))

    def release_leases(self, operation_id):
        with self.connect() as db:
            db.execute('DELETE FROM leases WHERE operation_id=?',(operation_id,))

    def owned_receipts(self, intent_id, owner):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM operations WHERE intent_id=? AND owner=? AND kind!='release' AND status IN ('completed','degraded','recovery_required') AND created > COALESCE((SELECT MAX(created) FROM operations WHERE intent_id=? AND owner=? AND kind='release' AND status='completed'),0) ORDER BY created",
                              (intent_id,owner,intent_id,owner)).fetchall()
        def identity(receipt):
            request=receipt['request']
            return tuple(request[k] for k in ('action','target_id','owner_id','operation_id'))+(tuple(request.get('session_keys',())),)
        result=[]
        for row in rows:
            value=json.loads(row['value'])
            finished={identity(r) for r in list(value.get('compensation',()))+list(value.get('released',()))
                      if r.get('state') in {'compensated','released'}}
            for receipt in list(value.get('compensation',()))+list(value.get('released',())):
                if receipt.get('state') in {'compensated','released'}:
                    finished.update(identity(r) for r in receipt.get('evidence',{}).get('coalesced_stages',()))
            result.extend(r for r in value.get('receipts',()) if identity(r) not in finished)
            pending=value.get('pending_action')
            if pending:
                uncertain={'request':pending,'state':'recovery_required','before':value.get('pending_before',{}),
                           'installed':{},'evidence':{'uncertain_native_write':True},'started_at':row['created']}
                if identity(uncertain) not in finished and not any(identity(r)==identity(uncertain) for r in result):
                    result.append(uncertain)
        return result

    def active(self, limit=100, after_id=''):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intents WHERE state IN ('active','satisfied','degraded') AND id>? ORDER BY id LIMIT ?",
                              (after_id,limit)).fetchall()
        return [(r['owner'],self._intent(r)) for r in rows]

    def iter_active(self, page_size=100):
        cursor = ''
        while True:
            page = self.active(page_size, cursor)
            if not page:
                return
            yield from page
            cursor = page[-1][1]['intent_id']

    def qos_state(self, session_keys=(), *, intent_id=None):
        with self.connect() as db:
            keys = set(session_keys)
            if intent_id is not None:
                keys.update(r[0] for r in db.execute('SELECT session_key FROM qos_claims WHERE intent_id=?',(intent_id,)))
            claims, effective = [], {}
            for key in sorted(keys):
                claims.extend(dict(target_id=r['target_id'],claim=json.loads(r['value'])) for r in db.execute('SELECT * FROM qos_claims WHERE session_key=?',(key,)))
                row = db.execute('SELECT * FROM qos_effective WHERE session_key=?',(key,)).fetchone()
                if row:
                    effective[key] = {name:json.loads(row[name]) for name in ('baseline','receipt','value')} | {'target_id':row['target_id']}
        return claims,effective

    def current_qos_definition(self,intent_id):
        with self.connect() as db:
            row=db.execute('SELECT value FROM qos_policy_definitions WHERE intent_id=?',(intent_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def persist_observe_definition(self,intent_id,operation_id,definition):
        from .service_policy_state import encoded,persist,observe_native,validate_observe_native
        import hashlib
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_fleet_open(db,intent_id,operation_id)
            row=db.execute('SELECT * FROM operations WHERE id=? AND intent_id=?',(operation_id,intent_id)).fetchone()
            value=json.loads(row['value']) if row else {}
            effect=value.get('effect',{})
            if (not row or row['kind']=='release' or row['status'] not in {'running','completed'}
                    or value.get('plan',{}).get('actions')!=[] or effect.get('met') is not True
                    or effect.get('protected_services_met') is not True
                    or definition.get('effect_evidence_sha256')!=hashlib.sha256(encoded(effect).encode()).hexdigest()
                    or definition.get('verified_observation_id')!=value.get('after',{}).get('observation_id')):
                raise IntentConflict('Verified unchanged whole-cohort hold required for native baseline definition')
            current=db.execute('SELECT value FROM intents WHERE id=?',(intent_id,)).fetchone()
            from .domain import ServiceIntent
            contract=ServiceIntent.from_dict(json.loads(current[0]))
            validate_observe_native(contract,definition)
            self._assert_pending_member_coverage(db,intent_id,operation_id,effect.get('session_keys',()))
            existing=db.execute('SELECT value FROM qos_policy_definitions WHERE intent_id=?',(intent_id,)).fetchone()
            if existing and not observe_native(json.loads(existing[0])):
                return False  # A hold cannot replace an existing native QoS policy.
            if db.execute('SELECT 1 FROM qos_claims WHERE intent_id=? LIMIT 1',(intent_id,)).fetchone():
                return False
            persist(db,intent_id,operation_id,definition)
            self._advance_dependency_epoch(db)
            return True

    @staticmethod
    def _assert_pending_member_coverage(db,intent_id,operation_id,session_keys):
        operation=db.execute('SELECT kind,value FROM operations WHERE id=?',(operation_id,)).fetchone()
        if operation and operation[0]=='release': return
        if operation:
            value=json.loads(operation['value'])
            if (value.get('failure_cleanup') is True and value.get('release_purpose')=='full_release'
                    and db.execute('SELECT 1 FROM fleet_closing WHERE intent_id=? AND operation_id=?',
                        (intent_id,operation_id)).fetchone()):
                return
        row=db.execute('SELECT membership FROM intents WHERE id=?',(intent_id,)).fetchone()
        membership=json.loads(row[0]) if row else {}
        required={m['session_token'] for m in membership.get('pending_members',())}
        observed=(json.loads(operation['value']).get('membership',{}) if operation else {})
        observed_keys={m['session_token'] for m in observed.get('members',())}
        # SharedQoSRuntime scopes also contain members being RETIRED. Their
        # presence in a lock scope does not prove the observation included them.
        if membership.get('effect_invalidated') and (not required<=set(session_keys)
                or (required and not required<=observed_keys)):
            from .planning_trace import ReobserveBeforeWrite
            raise ReobserveBeforeWrite('population_pending_members_require_complete_observation')

    def retire_qos_definition(self,intent_id,owner,operation_id):
        """Called only after full completed release has expired this intent."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            intent=db.execute('SELECT state FROM intents WHERE id=? AND owner=?',(intent_id,owner)).fetchone()
            operation=db.execute('SELECT kind,status,value FROM operations WHERE id=? AND intent_id=? AND owner=?',
                (operation_id,intent_id,owner)).fetchone()
            if (intent is None or intent['state']!='expired' or operation is None
                    or operation['kind']!='release' or operation['status']!='completed'
                    or json.loads(operation['value']).get('release_purpose')=='membership_withdrawal'):
                raise IntentConflict('Only completed full expiry retires current service policy')
            db.execute('DELETE FROM qos_policy_definitions WHERE intent_id=?',(intent_id,))

    def commit_qos(self, intent_id, operation_id, claims, effective, scopes,*,desired_definition=None):
        """Commit desired claims only after native installation/effect; leases fence writers."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_fleet_open(db,intent_id,operation_id)
            self._assert_pending_member_coverage(db,intent_id,operation_id,
                {r['claim']['member']['session_token'] for r in claims if r['claim']['intent_id']==intent_id})
            for scope in sorted(set(scopes)):
                row = db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
                if row is None or row[0] != operation_id:
                    raise IntentConflict('Shared policy lease lost')
            db.execute('DELETE FROM qos_claims WHERE intent_id=?',(intent_id,))
            for item in claims:
                claim = item['claim']
                db.execute('INSERT OR REPLACE INTO qos_claims VALUES (?,?,?,?)',
                           (claim['member']['session_token'],claim['intent_id'],item['target_id'],json.dumps(claim,allow_nan=False)))
            for key,item in effective.items():
                if item is None:
                    db.execute('DELETE FROM qos_effective WHERE session_key=?',(key,))
                else:
                    db.execute('INSERT OR REPLACE INTO qos_effective VALUES (?,?,?,?,?)',
                               (key,item['target_id'],json.dumps(item['baseline']),json.dumps(item['receipt']),json.dumps(item['value'])))
            for scope in sorted(set(scopes)):
                if scope.startswith(('session:','resource:')):
                    db.execute('INSERT OR REPLACE INTO policy_claims VALUES (?,?,?)',(scope,'shared:qos',operation_id))
            # A shared marker is kept only while at least one matching active claim exists.
            for row in db.execute("SELECT scope FROM policy_claims WHERE intent_id='shared:qos'").fetchall():
                scope = row[0]
                if scope.startswith('session:'):
                    present = db.execute('SELECT 1 FROM qos_claims WHERE session_key=? LIMIT 1',(scope[8:],)).fetchone()
                else:
                    present = db.execute('SELECT 1 FROM qos_claims WHERE target_id=? LIMIT 1',(scope[9:],)).fetchone()
                if not present:
                    db.execute('DELETE FROM policy_claims WHERE scope=?',(scope,))
            from .service_policy_state import persist
            persist(db,intent_id,operation_id,desired_definition)
            self._advance_dependency_epoch(db)

    def latest_operation(self, intent_id, owner):
        with self.connect() as db:
            row = db.execute('SELECT * FROM operations WHERE intent_id=? AND owner=? ORDER BY created DESC LIMIT 1',
                              (intent_id,owner)).fetchone()
        return self._operation(row) if row else None
