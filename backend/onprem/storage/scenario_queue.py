"""Durable scenario submissions and display lifecycle, separate from native fences."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from backend.onprem.scenarios.contracts import RequestIdConflict, RoundConflict


PREPARING = {'preflight', 'allocating', 'registering', 'establishing_pdu', 'verifying_connectivity'}


class ScenarioScheduleRepository:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scenario_submissions ('
                       'ordinal INTEGER PRIMARY KEY AUTOINCREMENT, queue_id TEXT UNIQUE NOT NULL, '
                       'request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, '
                       'state TEXT NOT NULL, round_id TEXT, submitted_at REAL NOT NULL, error_code TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS scenario_lifecycle ('
                       'round_id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS scenario_submission_state_ordinal '
                       'ON scenario_submissions(state,ordinal)')

    @staticmethod
    def _encoded(body):
        value = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return value, hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _receipt(db, row):
        if row is None:
            return None
        ordinal, queue_id, request_id, _fingerprint, body, state, round_id, submitted_at, error = row
        if state == 'active' and round_id:
            native = db.execute('SELECT snapshot,released FROM rounds WHERE round_id=?', (round_id,)).fetchone()
            if native and native[1] and json.loads(native[0]).get('stage') == 'completed':
                state = 'completed'
        position = db.execute("SELECT count(*) FROM scenario_submissions WHERE state='queued' AND ordinal<=?", (ordinal,)).fetchone()[0] if state == 'queued' else None
        return {'schemaVersion': 'cityverse.scenario-submission/v1', 'queueId': queue_id,
                'requestId': request_id, 'status': state, 'position': position,
                'configuration': json.loads(body), 'roundId': round_id,
                'submittedAt': submitted_at, 'errorCode': error}

    def find(self, *, queue_id=None, request_id=None):
        key, value = ('queue_id', queue_id) if queue_id is not None else ('request_id', request_id)
        with closing(sqlite3.connect(self.ledger.path)) as db:
            return self._receipt(db, db.execute('SELECT * FROM scenario_submissions WHERE '+key+'=?', (value,)).fetchone())

    def replay(self, body):
        receipt = self.find(request_id=body['requestId'])
        if receipt is not None and receipt['configuration'] != body:
            raise RequestIdConflict('Request ID is already bound to a different scenario')
        return receipt

    def enqueue(self, body):
        encoded, fingerprint = self._encoded(body)
        with self.ledger.connection() as db:
            row = db.execute('SELECT * FROM scenario_submissions WHERE request_id=?', (body['requestId'],)).fetchone()
            if row:
                if row[3] != fingerprint:
                    raise RequestIdConflict('Request ID is already bound to a different scenario')
                return self._receipt(db, row)
            queue_id = str(uuid4())
            db.execute('INSERT INTO scenario_submissions(queue_id,request_id,fingerprint,body,state,submitted_at) VALUES(?,?,?,?,?,?)',
                       (queue_id, body['requestId'], fingerprint, encoded, 'queued', time.time()))
            return self._receipt(db, db.execute('SELECT * FROM scenario_submissions WHERE queue_id=?', (queue_id,)).fetchone())

    def entries(self):
        with closing(sqlite3.connect(self.ledger.path)) as db:
            return [self._receipt(db, row) for row in db.execute("SELECT * FROM scenario_submissions WHERE state IN ('queued','dispatching','blocked') ORDER BY ordinal")]

    def transition(self, queue_id, state, *, expected=None, round_id=None, error=None):
        if state not in {'queued', 'dispatching', 'active', 'completed', 'cancelled', 'blocked', 'interrupted'}:
            raise ValueError('Invalid submission state')
        with self.ledger.connection() as db:
            row = db.execute('SELECT * FROM scenario_submissions WHERE queue_id=?', (queue_id,)).fetchone()
            if row is None:
                raise KeyError(queue_id)
            if expected is not None and row[5] not in expected:
                raise RoundConflict('Submission is no longer in the expected state')
            db.execute('UPDATE scenario_submissions SET state=?,round_id=coalesce(?,round_id),error_code=? WHERE queue_id=?',
                       (state, round_id, error, queue_id))
            return self._receipt(db, db.execute('SELECT * FROM scenario_submissions WHERE queue_id=?', (queue_id,)).fetchone())

    def recover_dispatches(self):
        """Called under the process fence; reconcile reservations before redispatch."""
        for entry in self.entries():
            if entry['status'] != 'dispatching':
                continue
            reserved = self.ledger.replay_request(entry['configuration'])
            self.transition(entry['queueId'], 'active' if reserved else 'queued',
                            expected={'dispatching'}, round_id=reserved['roundId'] if reserved else None)

    def patch_lifecycle(self, round_id, **patch):
        allowed = {'evaluationState', 'cyclesAttempted', 'cyclesCompleted', 'stopReason'}
        if set(patch) - allowed:
            raise ValueError('Unknown evaluation lifecycle field')
        if any(type(patch[k]) is not int or not 0 <= patch[k] <= 5 for k in ('cyclesAttempted', 'cyclesCompleted') if k in patch):
            raise ValueError('Evaluation cycles must be within 0..5')
        if 'evaluationState' in patch and patch['evaluationState'] not in {'pending','running','completed','failed','cancelled'}:
            raise ValueError('Invalid evaluation state')
        with self.ledger.connection() as db:
            if not db.execute('SELECT 1 FROM rounds WHERE round_id=?', (round_id,)).fetchone():
                raise KeyError(round_id)
            row = db.execute('SELECT value FROM scenario_lifecycle WHERE round_id=?', (round_id,)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(deepcopy(patch))
            value['revision'] = value.get('revision', 0) + 1
            db.execute('INSERT INTO scenario_lifecycle(round_id,value) VALUES(?,?) ON CONFLICT(round_id) DO UPDATE SET value=excluded.value',
                       (round_id, json.dumps(value, allow_nan=False)))

    def lifecycle(self, snapshot):
        stage, rid = snapshot.get('stage'), snapshot.get('roundId')
        value = {'schemaVersion': 'cityverse.scenario-lifecycle/v1', 'scenarioState': 'idle',
                 'evaluationState': 'pending', 'resourceState': 'released', 'maximumCycles': 5,
                 'cyclesAttempted': 0, 'cyclesCompleted': 0, 'stopReason': None,
                 'canSubmitEvaluation': True, 'submissionMode': 'start', 'revision': 0}
        if not rid:
            return value
        with closing(sqlite3.connect(self.ledger.path)) as db:
            row = db.execute('SELECT value FROM scenario_lifecycle WHERE round_id=?', (rid,)).fetchone()
        if row:
            value.update(json.loads(row[0]))
        if stage == 'completed':
            value.update(scenarioState='stopped', resourceState='released' if snapshot.get('cleanupStatus') == 'verified' else 'unknown')
            if value['resourceState'] != 'released':
                value.update(canSubmitEvaluation=False, submissionMode='blocked')
            if value['evaluationState'] not in {'completed','failed','cancelled'}:
                value['evaluationState'] = 'cancelled' if snapshot.get('executionOutcome') == 'cancelled' else 'failed' if snapshot.get('executionOutcome') == 'failed' else 'completed'
        elif stage == 'cleaning':
            value.update(scenarioState='stopping', resourceState='releasing', canSubmitEvaluation=False, submissionMode='blocked')
        elif stage in PREPARING:
            value.update(scenarioState='preparing', resourceState='reserved', canSubmitEvaluation=False, submissionMode='blocked')
        else:
            value.update(scenarioState='running', resourceState='held', submissionMode='queue')
            if value['evaluationState'] == 'pending':
                value['evaluationState'] = 'running'
        if snapshot.get('cleanupStatus') in {'blocked', 'verification_unavailable'}:
            value.update(resourceState='unknown', canSubmitEvaluation=False, submissionMode='blocked')
        # A baseline can fail before the model loop updates its own lifecycle.
        # The durable execution failure ends pending/running evaluation only;
        # it does not prove resource release or undo a completed evaluation.
        if (snapshot.get('executionOutcome') == 'failed'
                and value['evaluationState'] in {'pending', 'running'}):
            value['evaluationState'] = 'failed'
        return value
