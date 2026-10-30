"""Internal immutable batch raw storage; public snapshots remain fully hydrated."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from threading import Lock

from backend.onprem.evidence.control_decode import decode_control, parser_policy
from backend.onprem.evidence import batch_archive

TABLE = '''CREATE TABLE IF NOT EXISTS batch_raw_receipts (
round_id TEXT NOT NULL, action_id TEXT NOT NULL, binding_sha TEXT NOT NULL,
raw_sha TEXT NOT NULL, raw_json TEXT NOT NULL, PRIMARY KEY(round_id,action_id))'''
DETACHED_RESERVATION_JSON_MAX = 49152  # Encodes within the worker's 64 KiB base64 argv cap.
CONTROL_FIELDS = ('roundId', 'generation', 'stage', 'executionOutcome', 'fleetLease', 'trafficMode', 'allocation',
                  'serviceRequirements', 'resolvedRequirements', 'populationRevision', 'populationPending')
CONTROL_ACTION_FIELDS = ('id', 'kind', 'mode', 'token', 'jobs', 'stage', 'cleanupVerified',
                         'shardIndex', 'shardCount', 'workload', 'offeredBySupi',
                         'aggregateKbps', 'bindings', 'executionMode', 'detachedReservation')


@dataclass(frozen=True)
class _ExpectedControlBinding:
    action_id: object
    diagnostics_dict: bool
    has_reference: bool
    inline: bool
    reference: object
    binding: str | None
    reservation_valid: bool
    binding_error: tuple | None
    reservation_error: tuple | None


class BatchControlProjection:
    """One parsed source keyed by exact fresh bytes and parser policy.

    No authority or native observations are cached. Reference bindings are read
    again in the caller's transaction; returned objects never alias this source.
    """

    def __init__(self):
        self._lock = Lock()
        self._raw = self._entry = None
        self._parser_policy = None

    def read(self, db, raw, *, ownership_only=False, current_only=False):
        with self._lock:
            policy = parser_policy()
            if raw != self._raw or policy != self._parser_policy:
                snapshot = decode_control(raw, root_fields=CONTROL_FIELDS, action_fields=CONTROL_ACTION_FIELDS)
                if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('actions', []), list)
                        or not isinstance(snapshot.get('perUe', []), list)
                        or any(not isinstance(a, dict) for a in snapshot.get('actions', []))
                        or any(not isinstance(r, dict) for r in snapshot.get('perUe', []))):
                    raise ValueError('Malformed batch control source')
                source = {k: snapshot[k] for k in CONTROL_FIELDS if k in snapshot}
                source['perUe'] = [{k: r[k] for k in ('supi', 'scenario', 'slice', 'podUid') if k in r}
                                   for r in snapshot.get('perUe', [])]
                source['actions'] = [{k: a[k] for k in CONTROL_ACTION_FIELDS if k in a}
                                     for a in snapshot.get('actions', [])]
                expected = []
                for action in snapshot.get('actions', []):
                    diagnostics = action.get('workerDiagnostics')
                    is_dict = isinstance(diagnostics, dict)
                    expected_binding = binding_error = reservation_error = None
                    if is_dict and 'rawRecords' not in diagnostics:
                        try:
                            expected_binding = binding(snapshot, action)
                        except (KeyError, TypeError, ValueError) as error:
                            binding_error = (type(error), error.args)
                    mode, reservation = action.get('executionMode'), action.get('detachedReservation')
                    valid = mode is None and reservation is None
                    if not valid and mode == 'detached-shard-v1' and isinstance(reservation, dict):
                        try:
                            valid = len(encoded(reservation).encode()) <= DETACHED_RESERVATION_JSON_MAX
                        except (KeyError, TypeError, ValueError) as error:
                            reservation_error = (type(error), error.args)
                    expected.append(_ExpectedControlBinding(action.get('id'), is_dict,
                        is_dict and 'rawRecordsRef' in diagnostics,
                        is_dict and 'rawRecords' in diagnostics,
                        deepcopy(diagnostics.get('rawRecordsRef')) if is_dict else None,
                        expected_binding, valid, binding_error, reservation_error))
                # This private entry contains only pure derivations. Publishing
                # it never grants authority; every read validates fresh DB rows.
                entry = (source, tuple(expected))
                if parser_policy() == policy:
                    self._raw, self._entry, self._parser_policy = raw, entry, policy
            else:
                entry = self._entry
            source, expected = entry
        require_coverage(db, source, projected=current_only)
        if parser_policy() != policy:
            json.loads(raw)  # Validate changed policy without publishing a wrong cache key.
        # Keep this entry's immutable lifetime while another thread replaces
        # the one cache slot. No returned object ever aliases its nested tree.
        identifiers = [a['id'] for a in source['actions'] if a.get('kind') == 'traffic_window']
        sql = 'SELECT action_id,binding_sha,raw_sha FROM batch_raw_receipts WHERE round_id=? AND ' + batch_archive.current_clause(db, 'batch_raw_receipts')
        parameters = (source['roundId'],)
        sql += ' AND action_id IN (' + ','.join('?' for _ in identifiers) + ')'
        parameters += tuple(identifiers)
        rows = {a: (b, h) for a, b, h in db.execute(sql, parameters).fetchall()}
        owned = set(rows)
        matches = [item for item in expected if item.action_id in owned]
        if len(matches) != len(owned) or {item.action_id for item in matches} != owned:
            raise ValueError('Immutable batch receipt action missing or duplicated')
        if any(not item.diagnostics_dict for item in matches):
            raise ValueError('Immutable batch diagnostics missing')
        for item in expected:
            row = rows.get(item.action_id)
            if row is None and not item.has_reference:
                continue
            if (row is None or not item.diagnostics_dict or item.inline
                    or not isinstance(row[1], str) or re.fullmatch(r'[0-9a-f]{64}', row[1]) is None):
                raise ValueError('Invalid immutable batch control reference/binding')
            if item.binding_error:
                error_type, args = item.binding_error
                raise error_type(*args)
            if (item.binding is None or row[0] != item.binding
                    or item.reference != {'schema': 'cityverse.batch-raw/v1', 'sha256': row[1]}):
                raise ValueError('Invalid immutable batch control reference/binding')
            if item.reservation_error:
                error_type, args = item.reservation_error
                raise error_type(*args)
            if not item.reservation_valid:
                raise ValueError('Invalid detached batch control reservation')
        result = {'controlOnly': True, **source}
        if ownership_only:
            result = {k: result[k] for k in ('controlOnly', *CONTROL_FIELDS, 'perUe') if k in result}
        if parser_policy() != policy:
            json.loads(raw)
        return deepcopy(result)


class BatchCheckpoint:
    """One diagnostic schema shared by the producer and durable ledger."""

    PHASES = frozenset(('native_preflight', 'receiver_prepare', 'client_measurement',
                       'native_postflight', 'evidence_reduce', 'cleanup', 'final_freshness'))
    REQUIRED = frozenset({'observerNow'} | {name + suffix for name in
        ('startedAt', 'finishedAt', 'receiverStartedAt', 'receiverFinishedAt') for suffix in ('Min', 'Max')})
    OPTIONAL = frozenset(('observerBoottime', 'phaseSpans'))

    @classmethod
    def valid(cls, phase, diagnostics):
        if (phase not in cls.PHASES or not isinstance(diagnostics, dict)
                or not cls.REQUIRED <= diagnostics.keys()
                or diagnostics.keys() - cls.REQUIRED - cls.OPTIONAL):
            return False
        if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value))
               for key, value in diagnostics.items() if key != 'phaseSpans'):
            return False
        if 'observerBoottime' in diagnostics:
            value = diagnostics['observerBoottime']
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return False
        if 'phaseSpans' in diagnostics:
            from backend.onprem.traffic.protocol import validate_batch_spans
            if not validate_batch_spans(diagnostics['phaseSpans']):
                return False
        return True

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def same_json(left, right):
    """Type-strict comparison without repeatedly serializing immutable reports."""
    if type(left) is not type(right): return False
    if isinstance(left,dict):
        return left.keys()==right.keys() and all(same_json(left[k],right[k]) for k in left)
    if isinstance(left,list):
        return len(left)==len(right) and all(same_json(a,b) for a,b in zip(left,right))
    return left==right

def binding(snapshot, action):
    return digest(encoded({'roundId':snapshot['roundId'],'generation':snapshot['generation'],
                           'actionId':action['id'],'token':action['token'],'jobs':action['jobs'],
                           **({key: action[key] for key in ('shardIndex', 'shardCount') if key in action})}))

def receipt(db, snapshot, action):
    row=db.execute('SELECT binding_sha,raw_sha,raw_json FROM batch_raw_receipts WHERE round_id=? AND action_id=?',
                   (snapshot['roundId'],action['id'])).fetchone()
    if not row or row[0]!=binding(snapshot,action) or digest(row[2])!=row[1]:
        raise ValueError('Missing/corrupt or mismatched immutable batch receipt')
    return row

def require_coverage(db, snapshot, *, projected=False):
    """Fresh ownership checks, including the parsed-source cache-hit path."""
    batch_archive.validate_index(db, snapshot)
    ids = [a.get('id') for a in snapshot.get('actions', ())]
    authority = ids
    if projected and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rounds'").fetchone():
        # live_state omits earlier CLOSED windows that can still be owned in
        # the compact main journal until the next archive boundary. Inspect
        # compact IDs only, inside this same caller-owned read transaction.
        row = db.execute("SELECT round_id,json_extract(snapshot,'$.generation') FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
        if row is None:
            # First startup has no owner; an idle projection must not hide any
            # unarchived immutable owner in this same coherent read transaction.
            if (snapshot['roundId'] is not None or snapshot.get('generation') is not None
                    or snapshot.get('stage') != 'idle' or ids or snapshot.get('perUe')
                    or snapshot.get('devices') or snapshot.get('perSession')
                    or snapshot.get('fleetLease') is not None):
                raise ValueError('Empty ledger projection claims immutable batch ownership')
            for table in ('batch_raw_receipts', 'canonical_batch_reports'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    current = batch_archive.current_clause(db, table)
                    if db.execute(f'SELECT 1 FROM {table} WHERE {current} LIMIT 1').fetchone():
                        raise ValueError('Empty ledger contains unclaimed current immutable receipts')
            return set()
        if row != (snapshot['roundId'],snapshot.get('generation')):
            raise ValueError('Current immutable batch ownership generation changed')
        authority = [r[0] for r in db.execute("SELECT json_extract(value,'$.id') FROM json_each("
            "(SELECT snapshot FROM rounds WHERE round_id=?),'$.actions')", (snapshot['roundId'],))]
        if not set(ids) <= set(authority):
            raise ValueError('Current immutable batch projection contains foreign action')
        batch_archive.validate_index(db, {'roundId':snapshot['roundId'],'actions':[{'id':a} for a in authority]})
    current = batch_archive.current_clause(db, 'batch_raw_receipts')
    marks = ','.join('?' for _ in authority)
    # SQL coverage proves that no unarchived immutable owner is hidden, while
    # Python only receives the bounded current-action subset.
    missing = db.execute('SELECT 1 FROM batch_raw_receipts WHERE round_id=? AND ' + current
        + (' AND action_id NOT IN (' + marks + ')' if authority else '') + ' LIMIT 1', (snapshot['roundId'], *authority)).fetchone()
    if missing:
        raise ValueError('Immutable batch receipt action missing or duplicated')
    owned = {row[0] for row in db.execute('SELECT action_id FROM batch_raw_receipts WHERE round_id=? AND ' + current
        + ' AND action_id IN (' + marks + ')', (snapshot['roundId'], *authority))} if authority else set()
    if len([a for a in authority if a in owned]) != len(owned):
        raise ValueError('Immutable batch receipt action missing or duplicated')
    return owned


def require_actions(db, snapshot):
    owned = require_coverage(db, snapshot)
    actions = [a for a in snapshot.get('actions',[]) if a.get('id') in owned]
    if len(actions)!=len(owned) or {a['id'] for a in actions}!=owned:
        raise ValueError('Immutable batch receipt action missing or duplicated')
    if any(not isinstance(a.get('workerDiagnostics'),dict) for a in actions):
        raise ValueError('Immutable batch diagnostics missing')


def control_projection(db, snapshot):
    """Ownership/workload control only; NOT hydrated evidence or policy input.

    Validate references and binding metadata without reading raw_json. Actual
    raw bytes are still hash-verified by every public hydration/acceptance path.
    """
    require_actions(db, snapshot)
    ids = [a.get('id') for a in snapshot.get('actions', ())]
    rows = {a: (b, h) for a, b, h in db.execute(
        'SELECT action_id,binding_sha,raw_sha FROM batch_raw_receipts WHERE round_id=? AND ' + batch_archive.current_clause(db, 'batch_raw_receipts')
        + ' AND action_id IN (' + ','.join('?' for _ in ids) + ')', (snapshot['roundId'], *ids))} if ids else {}
    for action in snapshot.get('actions', []):
        diagnostics = action.get('workerDiagnostics')
        reference = diagnostics.get('rawRecordsRef') if isinstance(diagnostics, dict) else None
        row = rows.get(action.get('id'))
        if row is None and not (isinstance(diagnostics, dict) and 'rawRecordsRef' in diagnostics):
            continue  # Original legacy inline evidence is not upgraded/accepted.
        if (row is None or not isinstance(diagnostics, dict)
                or 'rawRecords' in diagnostics
                or not isinstance(row[1], str) or re.fullmatch(r'[0-9a-f]{64}', row[1]) is None
                or row[0] != binding(snapshot, action)
                or reference != {'schema': 'cityverse.batch-raw/v1', 'sha256': row[1]}):
            raise ValueError('Invalid immutable batch control reference/binding')
        mode, reservation = action.get('executionMode'), action.get('detachedReservation')
        if mode is not None or reservation is not None:
            if (mode != 'detached-shard-v1' or not isinstance(reservation, dict)
                    or len(encoded(reservation).encode()) > DETACHED_RESERVATION_JSON_MAX):
                raise ValueError('Invalid detached batch control reservation')
    return {'controlOnly': True, **{k: snapshot[k] for k in CONTROL_FIELDS if k in snapshot},
            'perUe': [{k: r[k] for k in ('supi', 'scenario', 'slice', 'podUid') if k in r}
                      for r in snapshot.get('perUe', [])],
            'actions': [{k: a[k] for k in CONTROL_ACTION_FIELDS if k in a}
                        for a in snapshot.get('actions', [])]}

def hydrate(db, snapshot, *, copy_snapshot=True, report_repository=None):
    """Verify raw receipts; consume an explicitly owned tree when requested.

    The default keeps caller-owned snapshots independent. A repository read
    directly decoded from SQLite may hydrate that new local tree in place.
    Reference, binding and raw hash checks are identical in either mode.
    """
    require_actions(db,snapshot)
    result=deepcopy(snapshot) if copy_snapshot else snapshot
    for action in result.get('actions',[]):
        diagnostics=action.get('workerDiagnostics')
        if not isinstance(diagnostics,dict) or 'rawRecordsRef' not in diagnostics:
            if isinstance(diagnostics,dict) and db.execute('SELECT 1 FROM batch_raw_receipts WHERE round_id=? AND action_id=?',
                                                          (result['roundId'],action['id'])).fetchone():
                raise ValueError('Immutable batch reference missing')
            continue
        if 'rawRecords' in diagnostics:
            raise ValueError('Ambiguous inline/referenced batch receipt')
        row=receipt(db,result,action)
        if diagnostics.pop('rawRecordsRef') != {'schema':'cityverse.batch-raw/v1','sha256':row[1]}:
            raise ValueError('Invalid immutable batch receipt reference')
        diagnostics['rawRecords']=json.loads(row[2])
    from backend.onprem.evidence.report_receipts import ReportReceiptRepository
    if report_repository is None:
        # This repository never escapes this call. Transfer its fresh reports
        # instead of copying them; rawRecords were independently decoded above.
        return ReportReceiptRepository(db).hydrate(result, copy_snapshot=False, consume_payloads=True)
    return report_repository.hydrate(result, copy_snapshot=False)

def compact(db, snapshot, *, copy_snapshot=True, report_repository=None, accept_hydrated_reports=False, raw_ids=None):
    """Validate raw receipts, optionally consuming a private, newly decoded tree.

    The default keeps caller-owned snapshots independent. Opting out of the
    copy is only safe for a disposable local tree: a later validation failure
    may leave earlier actions compacted. The caller retains its transaction
    rollback boundary; receipt, binding and raw hash checks remain identical.
    """
    require_actions(db,snapshot)
    result=deepcopy(snapshot) if copy_snapshot else snapshot
    for action in result.get('actions',[]):
        if raw_ids is not None and action.get('id') not in raw_ids:
            continue
        diagnostics=action.get('workerDiagnostics')
        if not isinstance(diagnostics,dict): continue
        if 'rawRecordsRef' in diagnostics:
            # Internal references are validated even on metadata-only updates.
            row=receipt(db,result,action)
            if diagnostics['rawRecordsRef']!={'schema':'cityverse.batch-raw/v1','sha256':row[1]} or 'rawRecords' in diagnostics:
                raise ValueError('Invalid immutable batch reference')
            continue
        existing=db.execute('SELECT 1 FROM batch_raw_receipts WHERE round_id=? AND action_id=?',
                            (result['roundId'],action['id'])).fetchone()
        if existing:
            row=receipt(db,result,action)
            if digest(encoded(diagnostics.get('rawRecords')))!=row[1]:
                raise ValueError('Immutable batch raw evidence changed')
            diagnostics.pop('rawRecords')
            diagnostics['rawRecordsRef']={'schema':'cityverse.batch-raw/v1','sha256':row[1]}
    from backend.onprem.evidence.report_receipts import ReportReceiptRepository
    return (report_repository or ReportReceiptRepository(db)).normalize(
        result, accept_hydrated=accept_hydrated_reports)
