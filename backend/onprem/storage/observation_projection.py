"""Bounded historical display derivation; every invocation rechecks raw proof."""
from copy import deepcopy
from dataclasses import dataclass
import json
from threading import Lock

from backend.onprem.evidence import batch_receipts
from backend.onprem.evidence.control_decode import parser_policy
from backend.onprem.evidence.report_receipts import ReportReceiptRepository
from backend.onprem.storage.summary_projection import _reference_source, _retained_size


@dataclass(frozen=True)
class _PreparedObservation:
    raw: str
    policy: tuple
    rtt_present: bool
    rtt_rows: tuple
    entry: tuple | None
    snapshot: dict | None
    cacheable: bool
    round_id: str
    reference: dict
    report_texts: tuple


class ObservationProjection:
    """One exact-source pure entry. No native/DB validation result is cached."""

    MAX_RETAINED_BYTES = 64 * 1024 * 1024

    def __init__(self):
        self._lock = Lock()
        self._entry = None
        self._counters = dict(hit=0, miss=0, oversize=0, retainedBytes=0)

    def statistics(self):
        with self._lock:
            return dict(self._counters)

    @staticmethod
    def _validate_policy(raw, rows, policy, report_texts=()):
        if parser_policy() != policy:
            # Revalidate all text against the new CPython parser policy. Never
            # publish old-policy derived data under a new key or retry a read.
            json.loads(raw)
            for _, encoded in rows:
                json.loads(encoded)
            for encoded in report_texts:
                json.loads(encoded)

    def capture(self, db, raw, round_id):
        """Fresh proof and RTT reads in the caller's original transaction."""
        policy = parser_policy()
        with self._lock:
            entry = self._entry
        source_match = (entry is not None and type(raw) is str
                        and entry[0] == raw and entry[1] == policy and entry[6] == round_id)
        snapshot = None if source_match else json.loads(raw)
        reference = entry[5] if source_match else snapshot
        # These are the original validators, not memoized acceptance flags.
        batch_receipts.control_projection(db, reference)
        for action in reference.get('actions', []):
            diagnostics = action.get('workerDiagnostics')
            if isinstance(diagnostics, dict) and 'rawRecordsRef' in diagnostics:
                batch_receipts.receipt(db, reference, action)
        reports = ReportReceiptRepository(db)
        reports.validate(reference)
        present = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='rtt_observations'").fetchone())
        rows = tuple(db.execute(
            'SELECT traffic_action_id,observation FROM rtt_observations WHERE round_id=? AND generation=?',
            (round_id, reference.get('generation'))).fetchall()) if present else ()
        # Never deduplicate the key. A legacy table with duplicate action IDs
        # retains the original row-order/last-wins behavior without caching.
        unique = len({row[0] for row in rows}) == len(rows)
        cacheable_rows = unique and all(type(key) is str and type(value) is str for key, value in rows)
        exact_rows = tuple(sorted(rows)) if cacheable_rows else rows
        hit = source_match and cacheable_rows and entry[2] is present and entry[3] == exact_rows
        if not hit and snapshot is None:
            snapshot = json.loads(raw)
        proof_source = entry[5] if source_match else _reference_source(snapshot)
        if snapshot is not None:
            snapshot = reports.hydrate(snapshot, copy_snapshot=False)
        report_texts = reports.policy_inputs()
        self._validate_policy(raw, rows, policy, report_texts)
        return _PreparedObservation(raw, policy, present, exact_rows,
                                    entry if hit else None, snapshot,
                                    type(raw) is str and cacheable_rows, round_id, proof_source, report_texts)

    def project(self, prepared):
        """Pure derivation/copy after the read transaction has finished."""
        if prepared.entry is not None:
            entry = prepared.entry
            with self._lock:
                self._counters['hit'] = min((1 << 63) - 1, self._counters['hit'] + 1)
        else:
            snapshot = prepared.snapshot
            if prepared.rtt_present:
                snapshot['rttObservations'] = {key: json.loads(value) for key, value in prepared.rtt_rows}
            from backend.onprem.scenarios.observability import _derive_observation
            derived = _derive_observation(snapshot)
            entry = (prepared.raw, prepared.policy, prepared.rtt_present,
                     prepared.rtt_rows, derived, prepared.reference, prepared.round_id)
            retained = _retained_size(entry, self.MAX_RETAINED_BYTES)
            with self._lock:
                self._counters['miss'] = min((1 << 63) - 1, self._counters['miss'] + 1)
                if retained > self.MAX_RETAINED_BYTES:
                    self._counters['oversize'] = min((1 << 63) - 1, self._counters['oversize'] + 1)
                if (prepared.cacheable and retained <= self.MAX_RETAINED_BYTES
                        and parser_policy() == prepared.policy):
                    self._entry = entry
                    self._counters['retainedBytes'] = retained
                else:
                    self._entry = None
                    self._counters['retainedBytes'] = 0
        # All nested dictionaries/lists returned to a service are private.
        from backend.onprem.scenarios.observability import _ObservationProjection
        result = _ObservationProjection(*deepcopy(entry[4]))
        self._validate_policy(prepared.raw, prepared.rtt_rows, prepared.policy, prepared.report_texts)
        return result
