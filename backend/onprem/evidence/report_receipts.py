"""Immutable complete canonical reports, resolved within one ledger transaction.

This is storage representation, not independent measurement validation. Native
raw receipts, reducers, clocks and publication gates retain their own contracts.
No decoded payload or validation result survives the owning transaction.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from backend.onprem.evidence.control_decode import parser_policy
from backend.onprem.evidence.full_decode import decode_full
from backend.onprem.evidence.report_decode import decode_checked_report


TABLE = '''CREATE TABLE IF NOT EXISTS canonical_batch_reports (
round_id TEXT NOT NULL, action_id TEXT NOT NULL, binding_sha TEXT NOT NULL,
reports_sha TEXT NOT NULL, reports_json TEXT NOT NULL, PRIMARY KEY(round_id,action_id))'''
SCHEMA = 'cityverse.batch-reports/v1'


class ReportReceiptRepository:
    """One transaction's fresh rows and decoded lists; never authority cache."""

    def __init__(self, db, *, consumed_ids=None, action_ids=None):
        self.db = db
        self.action_ids = None if action_ids is None else tuple(sorted(set(action_ids)))
        self.consumed_ids = None if consumed_ids is None else frozenset(consumed_ids)
        self._round = None
        self._rows = None
        self._decoded = {}
        self._nonfinite = {}
        self._policy = parser_policy()
        self._trusted = {}
        self._strict_json = False

    @staticmethod
    def _reference(sha):
        return {'schema': SCHEMA, 'sha256': sha}

    def _load(self, snapshot):
        from . import batch_archive
        batch_archive.validate_index(self.db, snapshot)
        current = batch_archive.current_clause(self.db, 'canonical_batch_reports')
        identifiers = tuple(a.get('id') for a in snapshot.get('actions', ()))
        marks = ','.join('?' for _ in identifiers)
        owner = snapshot['roundId']
        policy = parser_policy()
        if self._rows is None or owner != self._round or policy != self._policy:
            self._round, self._policy = owner, policy
            self._decoded = {}
            self._nonfinite = {}
            present = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='canonical_batch_reports' AND type='table'").fetchone()
            if not present:
                rows = []
            elif self.action_ids is not None:
                wanted = self.action_ids
                rows = list(self.db.execute(
                    'SELECT action_id,binding_sha,reports_sha,reports_json FROM canonical_batch_reports '
                    'WHERE round_id=? AND ' + current + ' AND action_id IN (' + ','.join('?' for _ in wanted) + ')',
                    (owner, *wanted))) if wanted else []
            elif self.consumed_ids is None:
                if self.db.execute('SELECT 1 FROM canonical_batch_reports WHERE round_id=? AND ' + current
                        + (' AND action_id NOT IN (' + marks + ')' if identifiers else '') + ' LIMIT 1',
                        (owner, *identifiers)).fetchone():
                    raise ValueError('Immutable canonical report action missing or duplicated')
                rows = list(self.db.execute(
                    'SELECT action_id,binding_sha,reports_sha,reports_json FROM canonical_batch_reports WHERE round_id=? AND ' + current
                    + ' AND action_id IN (' + marks + ')', (owner,*identifiers))) if identifiers else []
            else:
                # Metadata is always fresh; full payloads only for evidence this
                # transition actually consumes. No result cache grants authority.
                metadata = list(self.db.execute(
                    'SELECT action_id,binding_sha,reports_sha FROM canonical_batch_reports WHERE round_id=? AND ' + current
                    + ' AND action_id IN (' + marks + ')', (owner,*identifiers))) if identifiers else []
                if self.db.execute('SELECT 1 FROM canonical_batch_reports WHERE round_id=? AND ' + current
                        + (' AND action_id NOT IN (' + marks + ')' if identifiers else '') + ' LIMIT 1',
                        (owner,*identifiers)).fetchone():
                    raise ValueError('Immutable canonical report action missing or duplicated')
                wanted = sorted(self.consumed_ids)
                payloads = dict(self.db.execute(
                    'SELECT action_id,reports_json FROM canonical_batch_reports WHERE round_id=? AND ' + current + ' AND action_id IN ('
                    + ','.join('?' for _ in wanted) + ')', (owner, *wanted))) if wanted else {}
                rows = [(identifier, binding, sha, payloads.get(identifier))
                        for identifier, binding, sha in metadata]
            if len({r[0] for r in rows}) != len(rows):
                raise ValueError('Duplicate immutable canonical reports')
            self._rows = {r[0]: r[1:] for r in rows}
        return self._rows

    def _payload(self, action, row):
        binding, sha, text = row
        key = action['id']
        if key not in self._decoded:
            if (type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None
                    or type(text) is not str or hashlib.sha256(text.encode()).hexdigest() != sha):
                raise ValueError('Corrupt immutable canonical reports')
            reports, nonfinite = (decode_checked_report(text) if self._strict_json else (decode_full(text), True))
            if (not isinstance(reports, list) or not isinstance(action.get('jobs'), list)
                    or len(reports) != len(action['jobs'])
                    or any(not isinstance(report, dict) for report in reports)):
                raise ValueError('Invalid immutable canonical report list')
            self._decoded[key] = reports
            # Reads use the complete fast decoder. Strict writes track overflow
            # during parsing, avoiding a second walk of ordinary finite reports.
            # Cached read payloads conservatively retain the final-tree check.
            self._nonfinite[key] = nonfinite
        # No normalization/coercion: type-strict comparisons are also used on
        # hydrated write-back, not an encoding that conflates bool and int.
        return self._decoded[key]

    def _checked(self, snapshot, *, hydrated=False):
        from backend.onprem.evidence.batch_receipts import binding, same_json
        rows = self._load(snapshot)
        actions = snapshot.get('actions', [])
        relevant = [a for a in actions if a.get('id') in rows or 'reportsRef' in a]
        if (len({a.get('id') for a in relevant}) != len(relevant)
                or set(rows) - {a.get('id') for a in relevant}):
            raise ValueError('Immutable canonical report action missing or duplicated')
        result = {}
        for action in relevant:
            row = rows.get(action.get('id'))
            if (row is None or action.get('kind') != 'traffic_window' or action.get('mode') != 'batch'
                    or row[0] != binding(snapshot, action)
                    or type(row[1]) is not str or re.fullmatch('[0-9a-f]{64}', row[1]) is None):
                raise ValueError('Immutable canonical report binding changed')
            reference = self._reference(row[1])
            consumed = self.consumed_ids is None or action['id'] in self.consumed_ids
            payload = self._payload(action, row) if consumed else None
            if 'reportsRef' in action:
                if 'reports' in action or not same_json(action['reportsRef'], reference):
                    raise ValueError('Invalid or ambiguous immutable canonical report reference')
            elif (not hydrated or self._trusted.get(action['id']) != (row[0], reference)
                    or not same_json(action.get('reports'), payload)):
                raise ValueError('Immutable canonical reports changed or reference missing')
            result[action['id']] = (action, payload, reference)
        return result

    def validate(self, snapshot):
        """Validate stored representation before any caller patch is applied."""
        checked = self._checked(snapshot)
        self._trusted = {key: (self._rows[key][0], deepcopy(ref))
                         for key, (_, _, ref) in checked.items()}
        return checked

    def policy_inputs(self):
        """Already-read text for a display's original post-derivation guard."""
        return tuple(row[2] for row in (self._rows or {}).values())

    def revalidate_policy(self):
        if parser_policy() != self._policy:
            for text in self.policy_inputs():
                json.loads(text)

    def hydrate(self, snapshot, *, copy_snapshot=True, consume_payloads=False):
        if self.consumed_ids is not None:
            raise ValueError('Scoped report writer cannot hydrate a complete snapshot')
        checked = self._checked(snapshot)
        result = deepcopy(snapshot) if copy_snapshot else snapshot
        for action in result.get('actions', []):
            if action.get('id') in checked:
                _, payload, _ = checked[action['id']]
                action.pop('reportsRef')
                # Only a fresh, privately-owned repository may transfer its
                # trees. Reusable repositories retain independent return values.
                action['reports'] = payload if consume_payloads else deepcopy(payload)
                if consume_payloads:
                    self._decoded.pop(action['id'], None)
                    self._nonfinite.pop(action['id'], None)
        return result

    def normalize(self, snapshot, *, accept_hydrated=False, originate_ids=(), strict_json=False):
        """Keep existing refs; only typed publication may originate new rows."""
        previous_strict = self._strict_json
        self._strict_json = strict_json
        try:
            checked = self._checked(snapshot, hydrated=accept_hydrated)
        finally:
            self._strict_json = previous_strict
        if strict_json:
            for key, (_, payload, _) in checked.items():
                if payload is not None and self._nonfinite[key]:
                    # A duplicate JSON key can overwrite an earlier NaN/Inf.
                    # Inspect the final tree only for this rare token flag;
                    # ordinary finite payloads never take a second traversal.
                    pending, actual = [payload], False
                    while pending:
                        value = pending.pop()
                        if isinstance(value, dict):
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)
                        elif type(value) is float and not math.isfinite(value):
                            actual = True
                            break
                    self._nonfinite[key] = actual
                    if actual:
                        raise ValueError('Out of range float values are not JSON compliant')
        for action, _, reference in checked.values():
            action.pop('reports', None)
            action['reportsRef'] = deepcopy(reference)
        if not originate_ids:
            return snapshot
        from backend.onprem.evidence.batch_receipts import binding, encoded, receipt
        wanted = set(originate_ids)
        matches = [a for a in snapshot.get('actions', []) if a.get('id') in wanted]
        if len(wanted) != len(originate_ids) or len(matches) != len(wanted):
            raise ValueError('Canonical report publication action missing or duplicated')
        for action in matches:
            if action['id'] in checked:
                continue
            if (action.get('kind') != 'traffic_window' or action.get('mode') != 'batch'
                    or action.get('accepted') is not True or 'reportsRef' in action
                    or not isinstance(action.get('reports'), list)
                    or not isinstance(action.get('jobs'), list)
                    or len(action['reports']) != len(action['jobs'])
                    or any(not isinstance(report, dict) for report in action['reports'])):
                raise ValueError('Invalid canonical report publication')
            # Keep original raw hash/binding checks. Store the exact reports;
            # never rebuild a mismatching client/server pair from correct raw.
            receipt(self.db, snapshot, action)
            text = encoded(action['reports'])
            sha = hashlib.sha256(text.encode()).hexdigest()
            bind = binding(snapshot, action)
            self.db.execute('INSERT INTO canonical_batch_reports VALUES(?,?,?,?,?)',
                (snapshot['roundId'], action['id'], bind, sha, text))
            self._rows[action['id']] = (bind, sha, text)
            self._decoded[action['id']] = json.loads(text)
            self._nonfinite[action['id']] = False  # encoded(...allow_nan=False) just succeeded.
            action.pop('reports')
            action['reportsRef'] = self._reference(sha)
        return snapshot
