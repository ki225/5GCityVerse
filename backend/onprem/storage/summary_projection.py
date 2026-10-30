"""One bounded pure display projection; every reference check remains fresh."""
from copy import deepcopy
import json
import logging
import sys
from threading import Lock

from backend.onprem.evidence import batch_receipts
from backend.onprem.evidence.control_decode import parser_policy
from backend.onprem.evidence.report_receipts import ReportReceiptRepository


def _reference_source(snapshot):
    source = {key: snapshot[key] for key in ('roundId', 'generation') if key in snapshot}
    fields = ('id', 'kind', 'mode', 'token', 'jobs', 'shardIndex', 'shardCount',
              'executionMode', 'detachedReservation', 'reportsRef')
    source['actions'] = []
    for action in snapshot.get('actions', []):
        row = {key: action[key] for key in fields if key in action}
        if 'reportsRef' in action and 'reports' in action:
            row['reports'] = None  # Presence detects ambiguous persisted formats.
        if 'workerDiagnostics' in action:
            diagnostics = action['workerDiagnostics']
            row['workerDiagnostics'] = (
                {key: None if key == 'rawRecords' else diagnostics[key]
                 for key in ('rawRecords', 'rawRecordsRef') if key in diagnostics}
                if isinstance(diagnostics, dict) else diagnostics)
        source['actions'].append(row)
    return source


def _retained_size(value, limit):
    """Account owned Python objects, including Unicode keys, without recursion."""
    pending, seen, total = [value], set(), 0
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(current)
        if total > limit:
            return total
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return total


class SummaryProjection:
    """Memoize parsing/derivation, never DB authority or runtime observations."""

    MAX_RETAINED_BYTES = 64 * 1024 * 1024
    _COUNTER_MAX = (1 << 63) - 1

    def __init__(self):
        self._lock = Lock()
        self._entry = None
        self._counters = dict(hit=0, miss=0, oversize=0, retainedBytes=0)
        self._last_sample = 0

    def _increment(self, key):
        self._counters[key] = min(self._COUNTER_MAX, self._counters[key] + 1)

    def statistics(self):
        """Private bounded numeric diagnostics; not an admission/evidence port."""
        with self._lock:
            return dict(self._counters)

    def read(self, db, raw, maintenance):
        policy = parser_policy()
        reports = ReportReceiptRepository(db)
        with self._lock:
            hit = (self._entry is not None and self._entry[0] == raw
                   and self._entry[1] == policy)
            if hit:
                entry = self._entry
                self._increment('hit')
            else:
                self._increment('miss')
                snapshot = json.loads(raw)
                # Retain complete raw input until all original verdicts derive.
                batch_receipts.control_projection(db, snapshot)
                reference = _reference_source(snapshot)
                snapshot = reports.hydrate(snapshot, copy_snapshot=False)
                marker = maintenance(snapshot)
                from backend.onprem.scenarios.presentation import project_round_summary, with_traffic_counts
                projected = with_traffic_counts(project_round_summary(snapshot))
                scope = tuple(snapshot.get(key) for key in ('roundId', 'generation', 'sequence'))
                entry = (raw, policy, projected, reference, marker, scope)
                retained = _retained_size(entry, self.MAX_RETAINED_BYTES)
                if retained <= self.MAX_RETAINED_BYTES and parser_policy() == policy:
                    self._entry = entry
                    self._counters['retainedBytes'] = retained
                else:
                    self._entry = None
                    self._counters['retainedBytes'] = 0
                    if retained > self.MAX_RETAINED_BYTES:
                        self._increment('oversize')
            attempts = self._counters['hit'] + self._counters['miss']
            sample = None
            if attempts > self._last_sample and attempts & (attempts - 1) == 0:
                self._last_sample = attempts
                sample = dict(self._counters)
        if sample is not None:
            try:
                logging.getLogger('uvicorn.error').info(
                    'round.summary_projection_cache hit=%d miss=%d oversize=%d retainedBytes=%d',
                    *(sample[key] for key in ('hit', 'miss', 'oversize', 'retainedBytes')))
            except Exception:
                pass  # Numeric diagnostics cannot replace display validation.
        # Use one invocation's private entry even if another source replaces it.
        # No cache lock spans a warm read's fresh reference validation.
        if hit:
            batch_receipts.control_projection(db, entry[3])
            reports.validate(entry[3])
        result = deepcopy(entry[2]), entry[4], deepcopy(entry[5])
        if parser_policy() != policy:
            # Do not promote old-policy parse data to a new key or retry a read.
            json.loads(raw)
        reports.revalidate_policy()
        return result
