"""Explicit immutable ownership transfer for closed formal sampler receipts.

Raw/canonical bytes remain in their original tables. Runtime only consumes the
current ownership index; old payloads are checked once at transfer or explicit
offline repair, never hydrated on each measurement/recovery poll.
"""
import json

TABLE = '''CREATE TABLE IF NOT EXISTS batch_archived_receipts (
round_id TEXT NOT NULL, action_id TEXT NOT NULL, binding_sha TEXT NOT NULL,
raw_sha TEXT NOT NULL, reports_sha TEXT, PRIMARY KEY(round_id,action_id))'''


def installed(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_archived_receipts'").fetchone())


def install(db):
    db.execute(TABLE)
    db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_window_archive '
        '(round_id TEXT,action_id TEXT,receipt TEXT,PRIMARY KEY(round_id,action_id))')
    # Once its bytes have passed binding/hash checks, an archived action cannot
    # be rewritten to manufacture cleanup authority. The index is immutable too.
    for name, sql in {
        'batch_archive_immutable_update': "BEFORE UPDATE ON intent_traffic_window_archive WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)",
        'batch_archive_immutable_delete': "BEFORE DELETE ON intent_traffic_window_archive WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)",
        'batch_archive_index_immutable_update': 'BEFORE UPDATE ON batch_archived_receipts',
        'batch_archive_index_immutable_delete': 'BEFORE DELETE ON batch_archived_receipts',
        'batch_archive_immutable_insert': "BEFORE INSERT ON intent_traffic_window_archive WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=NEW.round_id AND action_id=NEW.action_id) AND NEW.receipt IS NOT (SELECT receipt FROM intent_traffic_window_archive WHERE round_id=NEW.round_id AND action_id=NEW.action_id)",
        'batch_archive_raw_immutable_update': 'BEFORE UPDATE ON batch_raw_receipts WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)',
        'batch_archive_raw_immutable_delete': 'BEFORE DELETE ON batch_raw_receipts WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)',
        'batch_archive_reports_immutable_update': 'BEFORE UPDATE ON canonical_batch_reports WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)',
        'batch_archive_reports_immutable_delete': 'BEFORE DELETE ON canonical_batch_reports WHEN EXISTS(SELECT 1 FROM batch_archived_receipts WHERE round_id=OLD.round_id AND action_id=OLD.action_id)',
    }.items():
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {sql} BEGIN SELECT RAISE(ABORT,'Immutable archived batch ownership'); END")


def current_clause(db, table):
    if not installed(db):
        return '1'
    if table not in {'batch_raw_receipts', 'canonical_batch_reports'}:
        raise ValueError('Unknown immutable receipt table')
    return f'NOT EXISTS(SELECT 1 FROM batch_archived_receipts archived WHERE archived.round_id={table}.round_id AND archived.action_id={table}.action_id)'


def validate_index(db, snapshot):
    if not installed(db):
        return
    if not db.execute('SELECT 1 FROM batch_archived_receipts WHERE round_id=? LIMIT 1', (snapshot['roundId'],)).fetchone():
        return  # Legacy ledgers with no transferred owner need no new schema.
    # Historical integrity is validated at transfer/explicit offline repair.
    # Indexed archival owners and their original action bytes are immutable.
    # Never hydrate or enumerate the history on a current-state read.
    triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'batch_archive_%'")}
    if not {'batch_archive_immutable_update','batch_archive_immutable_delete','batch_archive_immutable_insert',
            'batch_archive_index_immutable_update','batch_archive_index_immutable_delete',
            'batch_archive_raw_immutable_update','batch_archive_raw_immutable_delete',
            'batch_archive_reports_immutable_update','batch_archive_reports_immutable_delete'} <= triggers:
        raise ValueError('Immutable batch archive schema incomplete')
    ids = [a.get('id') for a in snapshot.get('actions', ())]
    if ids and db.execute('SELECT 1 FROM batch_archived_receipts WHERE round_id=? AND action_id IN ('
            + ','.join('?' for _ in ids) + ') LIMIT 1', (snapshot['roundId'], *ids)).fetchone():
        raise ValueError('Archived immutable batch action duplicated in current ownership')


def transfer(db, snapshot, action):
    """Called in the same writer transaction that prunes this CLOSED action."""
    from . import batch_receipts as b
    from .report_receipts import ReportReceiptRepository
    install(db)
    raw = db.execute('SELECT binding_sha,raw_sha,raw_json FROM batch_raw_receipts WHERE round_id=? AND action_id=?',
        (snapshot['roundId'], action['id'])).fetchone()
    if raw is None:
        if (action.get('workerDiagnostics') or {}).get('rawRecordsRef'):
            raise ValueError('Archived batch raw receipt missing')
        return False  # A non-batch sampler has no immutable raw ownership.
    if (action.get('kind') != 'traffic_window' or action.get('mode') != 'batch'
            or action.get('accepted') is not True or action.get('cleanupVerified') is not True
            or raw[0] != b.binding(snapshot, action) or b.digest(raw[2]) != raw[1]
            or action.get('workerDiagnostics', {}).get('rawRecordsRef') != {'schema':'cityverse.batch-raw/v1','sha256':raw[1]}
            or 'rawRecords' in action.get('workerDiagnostics', {})):
        raise ValueError('Unverified immutable batch archive transfer')
    canonical = db.execute('SELECT binding_sha,reports_sha,reports_json FROM canonical_batch_reports WHERE round_id=? AND action_id=?',
        (snapshot['roundId'], action['id'])).fetchone()
    sha = None
    if canonical:
        repo = ReportReceiptRepository(db)
        if canonical[0] != raw[0] or action.get('reportsRef') != repo._reference(canonical[1]) or 'reports' in action:
            raise ValueError('Archived canonical report binding/reference mismatch')
        repo._payload(action, canonical)  # Original full payload hash and shape.
        sha = canonical[1]
    elif action.get('reportsRef') is not None:
        raise ValueError('Archived canonical report missing')
    archived = db.execute('SELECT receipt FROM intent_traffic_window_archive WHERE round_id=? AND action_id=?',
        (snapshot['roundId'], action['id'])).fetchone()
    if archived is None or not b.same_json(json.loads(archived[0]), action):
        raise ValueError('Archived batch action bytes differ from owner transfer')
    values = (snapshot['roundId'], action['id'], raw[0], raw[1], sha)
    previous = db.execute('SELECT round_id,action_id,binding_sha,raw_sha,reports_sha FROM batch_archived_receipts WHERE round_id=? AND action_id=?', values[:2]).fetchone()
    if previous is not None and previous != values:
        raise ValueError('Archived batch index cannot change')
    db.execute('INSERT OR IGNORE INTO batch_archived_receipts VALUES(?,?,?,?,?)', values)
    return True


def repair_current(ledger, expected_round_id, expected_action_ids):
    """Explicit offline repair of exact preserved archived owners; no inference."""
    with ledger.connection() as db:
        row = db.execute('SELECT round_id,snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
        if row is None or row[0] != expected_round_id:
            raise ValueError('Current archived repair round changed')
        snapshot = json.loads(row[1])
        from backend.onprem.scenarios.intent_traffic import read_lease
        formal = read_lease(db, row[0])
        if formal is None or formal['lease']['request_id'] != snapshot.get('requestId'):
            raise ValueError('Exact formal traffic ownership required for archived repair')
        current = {a['id'] for a in snapshot.get('actions', ())}
        missing = {a[0] for a in db.execute('SELECT action_id FROM batch_raw_receipts WHERE round_id=?', (row[0],))} - current
        if missing != set(expected_action_ids) or len(expected_action_ids) != len(set(expected_action_ids)):
            raise ValueError('Archived repair expected exact missing immutable owners')
        for identifier in sorted(missing):
            archived = db.execute('SELECT receipt FROM intent_traffic_window_archive WHERE round_id=? AND action_id=?', (row[0], identifier)).fetchone()
            if archived is None:
                raise ValueError('Archived repair cannot manufacture a missing action')
            transfer(db, snapshot, json.loads(archived[0]))
        from .batch_receipts import control_projection
        control_projection(db, snapshot)
        from .report_receipts import ReportReceiptRepository
        ReportReceiptRepository(db, consumed_ids=()).validate(snapshot)
        return dict(round_id=row[0], indexed=len(missing), current_actions=len(current))
