"""Transactional scenario round repository and durable publication/maintenance state."""
from __future__ import annotations
from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from backend.onprem.evidence import batch_receipts as batch_receipts
from backend.onprem.evidence import batch_archive
from backend.onprem.evidence.report_receipts import ReportReceiptRepository, TABLE as REPORTS_TABLE
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4
from backend.onprem.scenarios.contracts import RoundConflict, RequestIdConflict, validate_round, slice_counts, idle_snapshot, STAGES
from backend.onprem.storage.maintenance_decode import decode_reservation_snapshot
from backend.onprem.storage import reservation_index, live_state
from backend.onprem.storage.writer_gate import writer_slot

def maintenance_pending(snapshot):
    """Unknown/malformed maintenance is quarantined, never automatically expired."""
    marker = snapshot.get("gen17PartialRelease", ...)
    gen17_pending = (marker is not ...
                     and (not isinstance(marker, dict) or marker.get("state") != "acknowledged"))
    gen34_marker = snapshot.get("gen34PartialRecovery", ...)
    gen34_pending = (gen34_marker is not ...
                     and (not isinstance(gen34_marker, dict)
                          or gen34_marker.get("state") != "acknowledged"))
    return (gen17_pending or gen34_pending
            or any("maintenance" in action and
               (not isinstance(action["maintenance"], dict) or
                action["maintenance"].get("state") != "acknowledged")
               for action in snapshot.get("actions", [])))


@dataclass(frozen=True)
class RoundDisplayRead:
    """One request's display data and maintenance result, never admission proof."""

    snapshot: dict
    maintenance_pending: bool
    scope: tuple

    def checked_snapshot(self):
        if (not isinstance(self.snapshot, dict) or type(self.maintenance_pending) is not bool
                or not isinstance(self.scope, tuple) or len(self.scope) != 3
                or not batch_receipts.same_json(list(self.scope),
                    [self.snapshot.get(k) for k in ('roundId', 'generation', 'sequence')])):
            raise RoundConflict('Round display scope changed')
        return self.snapshot

    def with_snapshot(self, snapshot):
        self.checked_snapshot()
        result = RoundDisplayRead(snapshot, self.maintenance_pending, self.scope)
        result.checked_snapshot()
        return result

def maintenance_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()

def _maintenance_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RoundConflict("Invalid maintenance timestamp")
    return value

def _maintenance_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False

class RoundLedger:
    def __init__(self, path: Path, *, qos_objectives=None, canonical_reports=False):
        if type(canonical_reports) is not bool:
            raise ValueError('Canonical report writer flag must be boolean')
        self.canonical_reports = canonical_reports
        from backend.onprem.scenarios.objectives import configured_objectives, validate_objectives
        self.qos_objectives = validate_objectives(qos_objectives) if qos_objectives is not None else configured_objectives()
        self.path = Path(path)
        self._exclusive_connections = set()
        self._exclusive_precommit_guards = {}
        self._batch_control_projection = batch_receipts.BatchControlProjection()
        from backend.onprem.storage.summary_projection import SummaryProjection
        self._summary_projection = SummaryProjection()
        from backend.onprem.storage.observation_projection import ObservationProjection
        self._observation_projection = ObservationProjection()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS rounds (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "round_id TEXT UNIQUE NOT NULL, request_id TEXT UNIQUE NOT NULL, "
                       "fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0)")
            db.execute(batch_receipts.TABLE)
            db.execute(REPORTS_TABLE)
            from backend.onprem.evidence import batch_archive
            batch_archive.install(db)
            db.execute("CREATE TABLE IF NOT EXISTS rtt_observations (round_id TEXT NOT NULL, generation INTEGER NOT NULL, "
                       "traffic_action_id TEXT NOT NULL, observation TEXT NOT NULL, PRIMARY KEY(round_id,generation,traffic_action_id))")
            reservation_index.install(db)
            live_state.install(db)

    @classmethod
    def open_existing(cls, path: Path):
        """Bind an existing ledger for offline recovery without schema writes."""
        source = Path(path)
        if source.is_symlink():
            raise RoundConflict("Existing RoundLedger authority is unavailable")
        path = source.resolve(strict=True)
        if not path.is_file():
            raise RoundConflict("Existing RoundLedger authority is unavailable")
        ledger = cls.__new__(cls)
        ledger.path = path
        ledger.canonical_reports = False
        ledger._exclusive_connections = set()
        ledger._exclusive_precommit_guards = {}
        ledger._batch_control_projection = batch_receipts.BatchControlProjection()
        from backend.onprem.storage.summary_projection import SummaryProjection
        ledger._summary_projection = SummaryProjection()
        from backend.onprem.storage.observation_projection import ObservationProjection
        ledger._observation_projection = ObservationProjection()
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"rounds", "batch_raw_receipts"} <= tables:
            raise RoundConflict("Existing RoundLedger schema is incomplete")
        return ledger

    @contextmanager
    def connection(self):
        with writer_slot(self.path) as remaining:
            db = sqlite3.connect(self.path, timeout=remaining)
            phase, phase_started = 'configure', time.perf_counter()
            try:
                db.execute("PRAGMA synchronous=FULL")
                phase, phase_started = 'begin', time.perf_counter()
                db.execute("BEGIN IMMEDIATE")
                phase, phase_started = 'body', time.perf_counter()
                yield db
                phase, phase_started = 'commit', time.perf_counter()
                db.commit()
            except BaseException as error:
                if isinstance(error, sqlite3.Error):
                    try:
                        error.onprem_transaction_phase = phase
                        error.onprem_transaction_elapsed_ms = (time.perf_counter() - phase_started) * 1000
                    except Exception:
                        pass  # Diagnostics must not replace the original failure.
                db.rollback()
                raise
            finally:
                db.close()

    @contextmanager
    def _read_connection(self):
        """Read DELETE snapshots; preserve the writer fence in other modes."""
        # as_uri quotes spaces, '?' and '#'; mode=ro must never create a ledger.
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            # BEGIN alone is deferred. Take a real read lock before inspecting
            # the mode, so it cannot change between this choice and projection.
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            if db.execute("PRAGMA journal_mode").fetchone()[0] == "delete":
                yield db
                db.commit()
                return
            db.rollback()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        # In WAL even EXCLUSIVE permits readers. Retain the original writer
        # reservation for every non-DELETE mode; never change the journal mode.
        with self.connection() as db:
            db.execute("PRAGMA query_only=ON")
            yield db

    @contextmanager
    def exclusive_connection(self):
        """Hold the ledger writer fence across an operator physical mutation.

        Exceptional offline recovery transitions, including zero-resident
        retirement and missing-Pod shard cleanup, require this held connection.
        It keeps the SQLite fence across physical action, readback and CAS.
        """
        with writer_slot(self.path) as remaining:
            db = sqlite3.connect(self.path, timeout=remaining)
            marker = id(db)
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN EXCLUSIVE")
                self._exclusive_connections.add(marker)
                yield db
                guard = self._exclusive_precommit_guards.get(marker)
                if guard is not None:
                    guard()
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                self._exclusive_precommit_guards.pop(marker, None)
                self._exclusive_connections.discard(marker)
                db.close()

    def register_exclusive_precommit(self, db, guard):
        """Run a one-transaction guard immediately before the durable commit."""
        marker = id(db)
        if (not isinstance(db, sqlite3.Connection)
                or marker not in self._exclusive_connections
                or not db.in_transaction or not callable(guard)
                or marker in self._exclusive_precommit_guards):
            raise RoundConflict("Exclusive precommit guard authority changed")
        self._exclusive_precommit_guards[marker] = guard


    def current(self):
        # DELETE reads keep a coherent SHARED snapshot and the physical
        # EXCLUSIVE fence without reserving the sole writer. Other journal
        # modes retain _read_connection's existing writer reservation.
        with self._read_connection() as db:
            row = db.execute("SELECT snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            return batch_receipts.hydrate(db, json.loads(row[0]), copy_snapshot=False) if row else idle_snapshot()

    def control_snapshot(self):
        """Current durable control journal with references, never historical payloads.

        Cleanup needs all outstanding resource identities, including older shards.
        These journal references are not decoded traffic reports.
        """
        with self._read_connection() as db:
            row = db.execute("SELECT snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            snapshot = json.loads(row[0]) if row else idle_snapshot()
            batch_receipts.control_projection(db, snapshot)
            ReportReceiptRepository(db, consumed_ids=()).validate(snapshot)
            return snapshot

    def live_snapshot(self, round_id=None, *, _db=None, _texts=None, _raw=None):
        """Current owner, latest cohort and latest measurements; no historical payloads.

        This is a runtime input, not a complete round history or mutation patch.
        All consumed current raw/report hashes are checked in this read transaction.
        """
        with (self._read_connection() if _db is None else nullcontext(_db)) as db:
            raw = live_state.read(db, round_id, decode=False) if _raw is None else _raw
            snapshot = json.loads(raw)
            json.dumps(snapshot, allow_nan=False)
            if _texts is not None:
                _texts.append(raw)
            self._batch_control_projection.read(db, raw, ownership_only=True, current_only=True)
            windows = [a for a in snapshot['actions'] if a.get('kind') == 'traffic_window']
            if windows and windows[-1].get('shardCount') is not None:
                count = windows[-1]['shardCount']
                if (type(count) is not int or not 2 <= count <= 10 or len(windows) != count
                        or [a.get('shardIndex') for a in windows] != list(range(count))
                        or any(a.get('shardCount') != count or a.get('stage') != windows[-1].get('stage') for a in windows)):
                    raise RoundConflict('Current measurement cohort is incomplete')
            for action in windows:
                diagnostics = action.get('workerDiagnostics')
                if isinstance(diagnostics, dict) and 'rawRecordsRef' in diagnostics:
                    row = batch_receipts.receipt(db, snapshot, action)
                    if ('rawRecords' in diagnostics or diagnostics['rawRecordsRef'] != {'schema':'cityverse.batch-raw/v1','sha256':row[1]}):
                        raise ValueError('Current raw receipt reference changed')
                    if _texts is not None:
                        _texts.append(row[2])
                    diagnostics.pop('rawRecordsRef')
                    diagnostics['rawRecords'] = json.loads(row[2])
            reports = ReportReceiptRepository(db, action_ids=[a['id'] for a in windows])
            result = reports.hydrate(snapshot, copy_snapshot=False, consume_payloads=True)
            if _texts is not None:
                _texts.extend(reports.policy_inputs())
            return result

    def begin_measurement(self, round_id, generation, stage):
        """Reset only live measurement state; never hydrate historical receipts."""
        if stage not in {'measuring_baseline','measuring_result'} or type(generation) is not int:
            raise RoundConflict('Invalid current measurement transition')
        with self.connection() as db:
            row = db.execute('SELECT round_id,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if not row or row[0] != round_id or row[1]:
                raise RoundConflict('Current measurement owner changed')
            snapshot = live_state.read(db, round_id)
            if (type(snapshot.get('generation')) is not int or snapshot.get('generation') != generation or snapshot.get('stage') == 'completed'
                    or snapshot.get('executionOutcome') is not None or maintenance_pending(snapshot)):
                raise RoundConflict('Current measurement generation/state changed')
            sequence = snapshot['sequence']
            from backend.onprem.scenarios.intent_traffic import read_lease
            if read_lease(db,round_id) is not None:
                # Archive only completed samplers. Latest cohort and all pending
                # worker/cleanup ownership stay current; archive payloads never
                # enter Python or subsequent runtime reads.
                db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_window_archive '
                    '(round_id TEXT,action_id TEXT,receipt TEXT,PRIMARY KEY(round_id,action_id))')
                closed="""json_extract(value,'$.kind') IN ('traffic_window','traffic_aggregate')
                    AND json_extract(value,'$.accepted')=1 AND
                    (json_extract(value,'$.cleanupVerified')=1 OR
                     (coalesce(json_extract(value,'$.mode'),'')!='batch'
                      AND json_extract(value,'$.kind')='traffic_window'
                      AND json_extract(value,'$.installed')=1 AND json_extract(value,'$.effectVerified')=1
                      AND json_extract(value,'$.finishedAt') IS NOT NULL))"""
                latest=db.execute("SELECT coalesce(json_array_length(value,'$.shardActionIds'),json_extract(value,'$.shardCount')),json_extract(value,'$.kind') FROM "
                    "json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') WHERE "+closed+
                    " ORDER BY cast(key AS INTEGER) DESC LIMIT 1",(round_id,)).fetchone()
                retain=1 if latest is None else max(1,int(latest[0] or 1))+int(latest[1]=='traffic_aggregate')
                keep=[row[0] for row in db.execute("SELECT json_extract(value,'$.id') FROM "
                    "json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') WHERE "+closed+
                    " ORDER BY cast(key AS INTEGER) DESC LIMIT ?",(round_id,retain))]
                closed+=" AND json_extract(value,'$.id') NOT IN (SELECT value FROM json_each(?))"
                parameters=(round_id,json.dumps(keep))
                db.execute("INSERT OR IGNORE INTO intent_traffic_window_archive SELECT ?,json_extract(value,'$.id'),value "
                    "FROM json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') WHERE "+closed,
                    (round_id,*parameters))
                # Verify/transfer only the exact receipts being pruned now.
                # No archive history or old raw payload is a runtime input.
                for (text,) in db.execute("SELECT value FROM json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') WHERE " + closed, parameters):
                    batch_archive.transfer(db, snapshot, json.loads(text))
                actions=db.execute("SELECT coalesce(json_group_array(json(value)),'[]') FROM "
                    "json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') WHERE NOT ("+closed+")",
                    parameters).fetchone()[0]
                db.execute("UPDATE rounds SET snapshot=json_set(snapshot,'$.actions',json(?)) WHERE round_id=?",(actions,round_id))
            changed = db.execute("UPDATE rounds SET snapshot=json_set(snapshot,'$.stage',?,'$.measurements',json('[]'),'$.sequence',?) "
                "WHERE round_id=? AND json_extract(snapshot,'$.sequence')=? AND released=0",
                (stage,sequence+1,round_id,sequence)).rowcount
            if changed != 1:
                raise RoundConflict('Current measurement sequence superseded')
            return live_state.read(db, round_id)

    def live_summary_read(self):
        from backend.onprem.scenarios.presentation import project_round_summary, with_traffic_counts
        from backend.onprem.evidence.control_decode import parser_policy
        policy, texts = parser_policy(), []
        snapshot = self.live_snapshot(_texts=texts)
        marker = maintenance_pending(snapshot)
        projected = with_traffic_counts(project_round_summary(snapshot))
        if parser_policy() != policy:
            for text in texts:
                json.loads(text)
        return RoundDisplayRead(projected, marker,
            tuple(snapshot.get(k) for k in ('roundId','generation','sequence')))

    def live_observation_read(self, round_id):
        from backend.onprem.scenarios.observability import _derive_observation, _ObservationProjection
        from backend.onprem.evidence.control_decode import parser_policy
        policy, texts = parser_policy(), []
        # RTT belongs to the selected window only, not every earlier traffic action.
        with self._read_connection() as db:
            snapshot = self.live_snapshot(round_id, _db=db, _texts=texts)
            identifiers = [a['id'] for a in snapshot['actions'] if a.get('kind') == 'traffic_window']
            if identifiers and db.execute("SELECT 1 FROM sqlite_master WHERE name='rtt_observations'").fetchone():
                rows = db.execute('SELECT traffic_action_id,observation FROM rtt_observations '
                    'WHERE round_id=? AND generation=? AND traffic_action_id IN ('
                    + ','.join('?' for _ in identifiers) + ')',
                    (round_id, snapshot['generation'], *identifiers)).fetchall()
                snapshot['rttObservations'] = {key:json.loads(value) for key,value in rows}
                texts.extend(value for _,value in rows)
        result = _ObservationProjection(*_derive_observation(snapshot))
        if parser_policy() != policy:
            for text in texts:
                json.loads(text)
        return result

    def summary_read(self):
        """Fresh committed display input, never hydrated/native acceptance proof.

        Validate immutable reference bindings in the same read transaction, but
        do not load or validate raw bytes. Full current() remains authoritative.
        """
        with self._read_connection() as db:
            row = db.execute("SELECT snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            snapshot = json.loads(row[0]) if row else idle_snapshot()
            batch_receipts.control_projection(db, snapshot)
            snapshot = ReportReceiptRepository(db).hydrate(snapshot, copy_snapshot=False)
            return RoundDisplayRead(snapshot, maintenance_pending(snapshot),
                tuple(snapshot.get(k) for k in ('roundId', 'generation', 'sequence')))

    def summary_snapshot(self):
        """Keep the display dictionary port for existing consumers."""
        return self.summary_read().checked_snapshot()

    def projected_summary_read(self):
        """Already-derived display only; original summary/native ports stay raw."""
        with self._read_connection() as db:
            row = db.execute('SELECT snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if row is None:
                from backend.onprem.scenarios.presentation import project_round_summary, with_traffic_counts
                snapshot = idle_snapshot()
                batch_receipts.control_projection(db, snapshot)
                return RoundDisplayRead(with_traffic_counts(project_round_summary(snapshot)),
                    maintenance_pending(snapshot), tuple(snapshot.get(key) for key in ('roundId', 'generation', 'sequence')))
            snapshot, marker, scope = self._summary_projection.read(db, row[0], maintenance_pending)
            return RoundDisplayRead(snapshot, marker, scope)

    def has_pending_maintenance(self):
        """Fresh maintenance metadata under the existing read/exclusive fence.

        Checking quarantine never needs to reserve a writer or reconstruct
        native traffic reports. Unknown maintenance markers still block.
        """
        with self._read_connection() as db:
            return maintenance_pending(live_state.read(db))

    def observation_snapshot(self, round_id, *, hydrate=True):
        """Exact historical display evidence; never reserve a writer or hydrate a live probe."""
        with closing(sqlite3.connect(self.path.resolve(strict=True).as_uri() + "?mode=ro",
                             uri=True, timeout=10)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            row = db.execute("SELECT snapshot FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if row is None:
                raise KeyError(round_id)
            snapshot = json.loads(row[0])
            if hydrate:
                snapshot = batch_receipts.hydrate(db, snapshot, copy_snapshot=False)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='rtt_observations'").fetchone():
                    snapshot["rttObservations"] = {r[0]: json.loads(r[1]) for r in db.execute(
                        "SELECT traffic_action_id,observation FROM rtt_observations WHERE round_id=? AND generation=?",
                        (round_id, snapshot.get("generation")))}
            else:
                snapshot = ReportReceiptRepository(db).hydrate(snapshot, copy_snapshot=False)
            return snapshot

    def observation_metadata(self, round_id):
        """Exact journal metadata for a selected event; no historical report payloads."""
        with self._read_connection() as db:
            row = db.execute('SELECT snapshot FROM rounds WHERE round_id=?', (round_id,)).fetchone()
            if row is None:
                raise KeyError(round_id)
            return json.loads(row[0])

    def observation_event_scope(self, round_id):
        """Fresh historical event thread only; no receipt/native authority.

        Ordinary events previously used observation_snapshot(hydrate=False),
        which has this same read transaction and no receipt/maintenance gate.
        Keep rich action-filtered reads on that original port.
        """
        from backend.onprem.evidence.observation_scope import decode_scope
        with closing(sqlite3.connect(self.path.resolve(strict=True).as_uri() + "?mode=ro",
                             uri=True, timeout=10)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            row = db.execute("SELECT snapshot FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if row is None:
                raise KeyError(round_id)
            return decode_scope(row[0])

    def observation_display_snapshot(self, round_id):
        """Fresh display reports with raw binding/hash checks, without raw materialization.

        Public full snapshots and independent acceptance still hydrate every
        raw worker record. A display poll never grants mutation authority.
        """
        with self._read_connection() as db:
            row = db.execute('SELECT snapshot FROM rounds WHERE round_id=?',(round_id,)).fetchone()
            if row is None:
                raise KeyError(round_id)
            snapshot = json.loads(row[0])
            batch_receipts.control_projection(db, snapshot)
            for action in snapshot.get('actions',[]):
                diagnostics = action.get('workerDiagnostics')
                if isinstance(diagnostics,dict) and 'rawRecordsRef' in diagnostics:
                    batch_receipts.receipt(db,snapshot,action)
            snapshot = ReportReceiptRepository(db).hydrate(snapshot, copy_snapshot=False)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='rtt_observations'").fetchone():
                snapshot['rttObservations']={r[0]:json.loads(r[1]) for r in db.execute(
                    'SELECT traffic_action_id,observation FROM rtt_observations WHERE round_id=? AND generation=?',
                    (round_id,snapshot.get('generation')))}
            return snapshot

    def projected_observation_read(self, round_id):
        """Opt-in historical display only; preserve original evidence ports."""
        with self._read_connection() as db:
            row = db.execute('SELECT snapshot FROM rounds WHERE round_id=?', (round_id,)).fetchone()
            if row is None:
                raise KeyError(round_id)
            prepared = self._observation_projection.capture(db, row[0], round_id)
        # Avoid extending the original DB reservation across pure derivation.
        return self._observation_projection.project(prepared)

    def save_rtt_observation(self, round_id, generation, traffic_action_id, observation):
        """Sidecar diagnostics never advance the authoritative publication fence."""
        if not isinstance(observation, dict) or observation.get("roundId") != round_id or observation.get("generation") != generation:
            raise RoundConflict("RTT observation belongs to another round")
        with self.connection() as db:
            row = db.execute("SELECT snapshot FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            snapshot = json.loads(row[0]) if row else {}
            from backend.onprem.evidence.rtt import cohort_fingerprint
            if (snapshot.get("generation") != generation or snapshot.get("stage") not in {"measuring_baseline", "measuring_result"}
                    or observation.get("cohortSha256") != cohort_fingerprint(snapshot)
                    or not any(a.get("id") == traffic_action_id and a.get("kind") in {"traffic_window", "traffic_aggregate"}
                               and a.get("accepted") is True for a in snapshot.get("actions", []))):
                raise RoundConflict("RTT observation does not own a traffic window")
            encoded = json.dumps(observation, sort_keys=True, allow_nan=False)
            prior = db.execute("SELECT observation FROM rtt_observations WHERE round_id=? AND generation=? AND traffic_action_id=?",
                (round_id, generation, traffic_action_id)).fetchone()
            if prior and prior[0] != encoded:
                raise RoundConflict("RTT observation is immutable")
            db.execute("INSERT OR IGNORE INTO rtt_observations VALUES(?,?,?,?)", (round_id, generation, traffic_action_id, encoded))

    def released_snapshot_matches(self, snapshot):
        """Read-only proof that this exact snapshot is the latest released round."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("roundId"), str):
            return False
        with closing(sqlite3.connect(self.path.resolve(strict=True).as_uri() + "?mode=ro",
                             uri=True, timeout=10)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()
            return bool(row and row[0] == snapshot["roundId"] and row[2] == 1
                        and (json.loads(row[1]) == snapshot or self.live_snapshot(_db=db) == snapshot))

    def completed_snapshot(self, round_id):
        """Read one released, successful round from this ledger without mutation."""
        if not isinstance(round_id, str) or not round_id:
            raise RoundConflict("Completed round identity is invalid")
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT snapshot,released FROM rounds WHERE round_id=?",
                             (round_id,)).fetchone()
            if row is None or row[1] != 1:
                raise RoundConflict("Completed calibration round is unavailable")
            snapshot = batch_receipts.hydrate(db, json.loads(row[0]), copy_snapshot=False)
        if (snapshot.get("roundId") != round_id or snapshot.get("stage") != "completed"
                or snapshot.get("executionOutcome") != "passed"
                or snapshot.get("cleanupStatus") != "verified"):
            raise RoundConflict("Calibration round is not a passed completed round")
        return snapshot

    def batch_control(self):
        """Fresh compact ownership read, never a measurement/acceptance snapshot."""
        return self._read_control(ownership_only=False)

    def ownership_control(self):
        """Same fresh immutable reference checks; omit unused action copies."""
        return self._read_control(ownership_only=True)

    def native_publication_scope(self):
        """Read current native identity/owner metadata, without report hydration.

        This is not publication authority: the typed writer rechecks the actual
        current sequence, owner and private held allocation in its transaction.
        """
        fields=('roundId','requestId','generation','sequence','schemaVersion',
                'allocation','fleetLease','devices','perSession','perUe',
                'populationRevision','stage')
        selections=','.join("'"+key+"',json_extract(snapshot,'$."+key+"')" for key in fields)
        with self._read_connection() as db:
            row=db.execute('SELECT json_object('+selections+') FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if row is None:raise RoundConflict('Native publication requires a current round')
            return {key:value for key,value in json.loads(row[0]).items() if value is not None}

    def current_service_scope(self):
        """Current identity metadata only; no traffic reports or archived actions."""
        with self._read_connection() as db:
            row=db.execute("SELECT json_object('roundId',r.round_id,'generation',json_extract(l.snapshot,'$.generation'),"
                "'populationRevision',coalesce(json_extract(l.snapshot,'$.populationRevision'),0),"
                "'schemaVersion',json_extract(l.snapshot,'$.schemaVersion'),"
                "'devices',json(coalesce(json_extract(l.snapshot,'$.devices'),'[]')),"
                "'perSession',json(coalesce(json_extract(l.snapshot,'$.perSession'),'[]')),"
                "'perUe',json(coalesce(json_extract(l.snapshot,'$.perUe'),'[]'))) "
                "FROM rounds r JOIN round_live_state l ON l.round_id=r.round_id ORDER BY r.ordinal DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else {'roundId':None,'generation':None,'perUe':[]}

    def _read_control(self, *, ownership_only):
        with self._read_connection() as db:
            raw = live_state.read(db, decode=False)
            return self._batch_control_projection.read(db, raw, ownership_only=ownership_only, current_only=True)

    def replay_request(self, body, *, current_only=False):
        """Resolve retries before native reads, even after another round started."""
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            duplicate = db.execute("SELECT fingerprint,snapshot FROM rounds WHERE request_id=?",
                                   (body["requestId"],)).fetchone()
            if duplicate is None:
                return None
            if duplicate[0] != fingerprint:
                raise RequestIdConflict("requestId was already used for different input")
            return (self.live_snapshot(_db=db, _raw=db.execute('SELECT ' + live_state._derived('?1'), (duplicate[1],)).fetchone()[0])
                    if current_only else batch_receipts.hydrate(db, json.loads(duplicate[1])))

    def reserve(self, body, *, baseline_verified: bool, capabilities, fleet_lease=None, current_only=False,
                service_intent_lease=None,held_reservation=None):
        body = validate_round(body)
        if service_intent_lease is not None:
            from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
            if (type(service_intent_lease) is not ServiceIntentTrafficLease or body['strategy']!='none'
                    or service_intent_lease.request_id!=body['requestId'] or not service_intent_lease.active(time.time())):
                raise RoundConflict('Active exact service intent traffic lease required')
        if held_reservation is not None:
            from backend.onprem.runtime.ue_reservation import HeldUEAllocation
            if (type(held_reservation) is not HeldUEAllocation or service_intent_lease is None
                    or any(getattr(held_reservation,k)!=getattr(service_intent_lease,k) for k in ('owner_id','operation_id','request_id'))
                    or dict(held_reservation.counts)!=slice_counts({'baselineUes':body['baselineUes'],'events':body['events']})):
                raise RoundConflict('Exact trusted held allocation required')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            if reservation_index.pending(db, decode=decode_reservation_snapshot, predicate=maintenance_pending):
                raise RoundConflict("Maintenance reservation is unresolved")
            duplicate = db.execute("SELECT fingerprint,snapshot FROM rounds WHERE request_id=?",
                                   (body["requestId"],)).fetchone()
            if duplicate:
                if duplicate[0] != fingerprint:
                    raise RequestIdConflict("requestId was already used for different input")
                from backend.onprem.scenarios.intent_traffic import read_lease
                duplicate_id=json.loads(duplicate[1])['roundId']
                previous_lease=read_lease(db,duplicate_id)
                if ((previous_lease is None)!=(service_intent_lease is None)
                        or (previous_lease is not None and previous_lease['lease']!=service_intent_lease.to_dict())):
                    raise RequestIdConflict('requestId is owned by another traffic lease')
                return (self.live_snapshot(_db=db, _raw=db.execute('SELECT ' + live_state._derived('?1'), (duplicate[1],)).fetchone()[0])
                        if current_only else batch_receipts.hydrate(db, json.loads(duplicate[1])))
            if db.execute("SELECT 1 FROM rounds WHERE released=0").fetchone():
                raise RoundConflict("Previous round still owns reserved resources")
            if not baseline_verified or capabilities.get("start") is not True:
                raise RoundConflict("Fresh, verified clean resource baseline is required")
            if body["strategy"] != "none" and capabilities.get(body["strategy"] + "Policy") is not True:
                raise RoundConflict("Requested policy capability has not been verified")
            snapshot = idle_snapshot()
            previous = db.execute("SELECT snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            sequence = decode_reservation_snapshot(previous[0])["sequence"] + 1 if previous else 1
            snapshot.update({"roundId": str(uuid4()), "stage": "preflight", "sequence": sequence,
                             "strategy": body["strategy"], "capabilities": deepcopy(capabilities),
                             "requestId": body["requestId"], "fleetLease": deepcopy(fleet_lease)})
            snapshot["allocation"] = {"baselineUes": body["baselineUes"], "events": body["events"],
                                      "total": body["baselineUes"] + sum(body["events"].values())}
            if "demandGroups" in body:
                snapshot["allocation"].update(topologyRevision=body["topologyRevision"],
                    demandGroups=deepcopy(body["demandGroups"]))
                snapshot["spatialDemand"] = {"topologyRevision": body["topologyRevision"],
                    "requestedGroups": deepcopy(body["demandGroups"]),
                    "resolvedGroups": [], "actualBindings": []}
            snapshot["requestedSliceUes"] = slice_counts(snapshot["allocation"])
            if "trafficMode" in body:
                snapshot["trafficMode"] = body["trafficMode"]
            if "serviceRequirements" in body:
                from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
                snapshot["serviceRequirements"] = deepcopy(body["serviceRequirements"])
                snapshot["resolvedRequirements"] = ServiceRequirementCompiler.compile(
                    body["serviceRequirements"], snapshot["allocation"], traffic_mode=body.get("trafficMode"))
            from backend.onprem.scenarios.objectives import freeze_objectives
            frozen = freeze_objectives(getattr(self, "qos_objectives", None), snapshot["allocation"])
            if frozen is not None:
                snapshot["qosObjectives"] = frozen
            db.execute("INSERT INTO rounds(round_id,request_id,fingerprint,snapshot) VALUES(?,?,?,?)",
                       (snapshot["roundId"], body["requestId"], fingerprint, json.dumps(snapshot)))
            if service_intent_lease is not None:
                from backend.onprem.scenarios.intent_traffic import persist_lease
                persist_lease(db,snapshot['roundId'],service_intent_lease)
            if held_reservation is not None:
                db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_held (round_id TEXT PRIMARY KEY,handle TEXT NOT NULL)')
                db.execute('INSERT INTO intent_traffic_held VALUES (?,?)',(snapshot['roundId'],json.dumps(held_reservation.to_dict())))
            return snapshot

    def intent_traffic_lease(self,round_id=None):
        """Private current-round lease port; no archived reports are read."""
        from backend.onprem.scenarios.intent_traffic import read_lease
        with self._read_connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if current is None or (round_id is not None and round_id!=current[0]):
                return None
            return read_lease(db,current[0])

    def intent_traffic_held(self,round_id):
        with self._read_connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_traffic_held'").fetchone():
                return None
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if current is None or current[0]!=round_id:
                raise RoundConflict('Held traffic allocation is no longer current')
            row=db.execute('SELECT handle FROM intent_traffic_held WHERE round_id=?',(round_id,)).fetchone()
            if row is None:
                return None
            from backend.onprem.runtime.ue_reservation import HeldUEAllocation
            return HeldUEAllocation.from_dict(json.loads(row[0]))

    def request_intent_traffic_release(self,lease):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,read_lease
        if type(lease) is not ServiceIntentTrafficLease:
            raise RoundConflict('Trusted typed service traffic lease required')
        with self.connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            entry=None if current is None else read_lease(db,current[0])
            if entry is None or entry['lease']!=lease.to_dict():
                raise RoundConflict('Traffic lease ownership changed')
            if entry['state'] in {'active','restoring'}:
                db.execute("UPDATE intent_traffic_leases SET state='restoring' WHERE round_id=?",(current[0],))

    def renew_intent_traffic_lease(self,before,after):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,read_lease
        if type(before) is not ServiceIntentTrafficLease or type(after) is not ServiceIntentTrafficLease:
            raise RoundConflict('Trusted typed lease renewal required')
        fixed=('lease_id','intent_id','owner_id','operation_id','request_id','valid_from')
        if any(getattr(before,key)!=getattr(after,key) for key in fixed) or after.intent_revision<before.intent_revision:
            raise RoundConflict('Traffic lease renewal identity changed')
        with self.connection() as db:
            row=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            entry=None if row is None else read_lease(db,row[0])
            if entry is None or entry['state']!='active':
                raise RoundConflict('Only active current traffic lease may renew')
            if entry['lease']==after.to_dict():
                return entry
            if entry['lease']!=before.to_dict():
                raise RoundConflict('Traffic lease renewal version changed')
            db.execute('UPDATE intent_traffic_leases SET lease=? WHERE round_id=?',
                (json.dumps(after.to_dict(),sort_keys=True),row[0]))
            return dict(lease=after.to_dict(),state='active',restoration=None)

    def finish_intent_traffic(self,lease,proof):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficRestoration,read_lease
        if type(lease) is not ServiceIntentTrafficLease or type(proof) is not IntentTrafficRestoration:
            raise RoundConflict('Trusted typed restoration proof required')
        proof.validate(lease,time.time())
        with self.connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            entry=None if current is None else read_lease(db,current[0])
            if entry is None or entry['lease']!=lease.to_dict():
                raise RoundConflict('Restoration does not own current traffic lease')
            if entry['state']=='released':
                return entry
            db.execute("UPDATE intent_traffic_leases SET state='released',restoration=? WHERE round_id=?",
                       (json.dumps(proof.to_dict(),sort_keys=True),current[0]))
            return dict(lease=lease.to_dict(),state='released',restoration=proof.to_dict())

    def drain_intent_traffic(self,lease,permit):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficDrainPermit,read_lease
        if type(lease) is not ServiceIntentTrafficLease or type(permit) is not IntentTrafficDrainPermit:
            raise RoundConflict('Typed intent traffic drain authority required')
        permit.validate(lease,time.time())
        with self.connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            entry=None if current is None else read_lease(db,current[0])
            if entry is None or entry['lease']!=lease.to_dict():
                raise RoundConflict('Drain permit does not own current lease')
            if entry['state']=='released':
                snapshot_row=db.execute('SELECT snapshot,released FROM rounds WHERE round_id=?',(current[0],)).fetchone()
                snapshot=json.loads(snapshot_row[0])
                release=snapshot.get('releaseProof',{})
                if (snapshot['stage']!='completed' or snapshot['cleanupStatus']!='verified'
                        or not snapshot_row[1] or snapshot['requestId']!=lease.request_id):
                    raise RoundConflict('Released lease has no current terminal receipt')
                if release.get('uesReleased') is True and release.get('podsAbsent') is True:
                    # The caller independently re-observes native absence below;
                    # no UE mutation or completed-round reopening is necessary.
                    pass
                else:
                    allocations=[a for a in snapshot.get('actions',()) if a.get('kind')=='ue_allocation']
                    if (release.get('fleetPreserved') is not True or release.get('uesReleased') is not False
                            or snapshot.get('fleetLease') or len(allocations)!=1
                            or allocations[0].get('accepted') is not True or allocations[0].get('reused') is not False
                            or allocations[0].get('generation')!=snapshot.get('generation')
                            or type(snapshot.get('generation')) is not int):
                        raise RoundConflict('Terminal formal traffic cleanup ownership is uncertain')
                    db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_terminal_recovery '
                        '(round_id TEXT PRIMARY KEY,original_snapshot TEXT NOT NULL)')
                    db.execute('INSERT OR IGNORE INTO intent_traffic_terminal_recovery VALUES (?,?)',(current[0],snapshot_row[0]))
                    snapshot.update(stage='cleaning',cleanupStatus='pending',sequence=snapshot['sequence']+1)
                    snapshot.pop('releaseProof',None)
                    db.execute('UPDATE rounds SET snapshot=?,released=0 WHERE round_id=?',(json.dumps(snapshot),current[0]))
            db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_drain (round_id TEXT PRIMARY KEY,permit TEXT NOT NULL,proof TEXT)')
            previous=db.execute('SELECT permit FROM intent_traffic_drain WHERE round_id=?',(current[0],)).fetchone()
            if previous and json.loads(previous[0])['release_operation_id']!=permit.release_operation_id:
                raise RoundConflict('Another drain operation owns traffic')
            db.execute('INSERT OR IGNORE INTO intent_traffic_drain VALUES (?,?,NULL)',(current[0],json.dumps(permit.to_dict())))
            if previous and permit.observed_at >= json.loads(previous[0])['observed_at']:
                # A fresh owner-safe service callback can renew the same drain
                # operation's permit; an older callback cannot roll it back.
                db.execute('UPDATE intent_traffic_drain SET permit=? WHERE round_id=? AND proof IS NULL',
                    (json.dumps(permit.to_dict()),current[0]))
            if entry['state']!='drained':
                db.execute("UPDATE intent_traffic_leases SET state='draining' WHERE round_id=?",(current[0],))

    def intent_traffic_drain_authority(self,round_id):
        """Current durable typed cleanup authority, without native observations."""
        from backend.onprem.scenarios.intent_traffic import read_lease,ServiceIntentTrafficLease,IntentTrafficDrainPermit
        with self._read_connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if current is None or current[0]!=round_id:
                raise RoundConflict('Drain authority round is no longer current')
            entry=read_lease(db,round_id)
            if entry is None or entry['state']!='draining':
                return None
            row=db.execute('SELECT permit FROM intent_traffic_drain WHERE round_id=?',(round_id,)).fetchone()
            if row is None:
                raise RoundConflict('Current formal drain permit is missing')
            return dict(lease=ServiceIntentTrafficLease.from_dict(entry['lease']),
                permit=IntentTrafficDrainPermit(**json.loads(row[0])))

    def confirm_intent_traffic_drained(self,round_id,proof):
        from backend.onprem.scenarios.intent_traffic import read_lease
        from backend.onprem.runtime.ue import seconds
        required=('trafficStopped','policiesAbsent','rulesRestored','uesReleased','podsAbsent')
        # ISO timestamps round to microseconds; conversion may round a newly
        # observed native clock forward by less than one microsecond.
        if any(proof.get(k) is not True for k in required) or not -0.000001<=time.time()-seconds(proof.get('observedAt'))<=30:
            raise RoundConflict('Fresh owned UE disappearance proof required')
        with self.connection() as db:
            current=db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            entry=read_lease(db,round_id)
            if current is None or current[0]!=round_id or entry is None or entry['state'] not in {'draining','drained'}:
                raise RoundConflict('Current drain authority changed')
            db.execute('UPDATE intent_traffic_drain SET proof=? WHERE round_id=?',(json.dumps(proof),round_id))
            db.execute("UPDATE intent_traffic_leases SET state='drained' WHERE round_id=?",(round_id,))

    def reserve_population(self, lease, operation_id, request, idempotency_key):
        from backend.onprem.scenarios.population import reserve
        return reserve(self, lease, operation_id, request, idempotency_key)

    def population_readback(self, operation_id, owner):
        from backend.onprem.scenarios.population import read
        return read(self, operation_id, owner)

    def publish_native_evidence(self,publication):
        """Publish only a sealed complete native cohort; generic patches stay closed."""
        from backend.onprem.evidence.native_binding_publication import NativeBindingPublication
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation
        if type(publication) is not NativeBindingPublication:
            raise RoundConflict('Typed native evidence publication is required')
        with self.connection() as db:
            row=db.execute('SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if row is None or row[2]:raise RoundConflict('Native evidence requires the current active round')
            snapshot=json.loads(row[1])
            if maintenance_pending(snapshot) or snapshot.get('stage')=='completed':
                raise RoundConflict('Native evidence owner is blocked or completed')
            held_row=db.execute('SELECT handle FROM intent_traffic_held WHERE round_id=?',(row[0],)).fetchone()
            if held_row is None:raise RoundConflict('Native evidence requires the original typed held allocation')
            held=HeldUEAllocation.from_dict(json.loads(held_row[0]))
            projected=publication.validate(snapshot,held)
            ReportReceiptRepository(db,consumed_ids=()).validate(snapshot)
            snapshot.update(deepcopy(projected));snapshot['sequence']+=1
            db.execute('UPDATE rounds SET snapshot=? WHERE round_id=?',(json.dumps(snapshot,allow_nan=False),row[0]))
            return live_state.read(db,row[0])

    def update(self, round_id, expected_sequence, patch, *, current_only=False):
        allowed = {"generation", "policyRevision", "observedAt", "stage", "executionOutcome",
                   "cleanupStatus", "counts", "perUe", "actions", "measurements", "errors",
                   "connectivityEvidence", "schemaVersion", "devices", "perSession", "spatialDemand"}
        if set(patch) - allowed:
            raise ValueError("Patch attempts to change immutable allocation or ownership")
        with self.connection() as db:
            row = db.execute("SELECT snapshot,released FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                raise KeyError(round_id)
            snapshot = json.loads(row[0])
            if (snapshot.get('schemaVersion') == 'cityverse.current-state/v2'
                    and patch.get('schemaVersion', snapshot['schemaVersion']) != snapshot['schemaVersion']):
                raise RoundConflict('Current session schema cannot be downgraded')
            if any(key in patch for key in ('schemaVersion', 'devices', 'perSession')) or snapshot.get('schemaVersion') == 'cityverse.current-state/v2':
                from backend.onprem.runtime.current_sessions import session_rows
                session_rows({**snapshot, **patch})
            if maintenance_pending(snapshot):
                raise RoundConflict("Maintenance reservation is unresolved")
            if "actions" in patch:
                before = {a["id"]: a["maintenance"] for a in snapshot["actions"] if "maintenance" in a}
                after = {a["id"]: a["maintenance"] for a in patch["actions"] if "maintenance" in a}
                if before != after:
                    raise RoundConflict("Maintenance requires a typed ledger transition")
            if row[1] or snapshot["sequence"] != expected_sequence:
                raise RoundConflict("Round snapshot was superseded or already released")
            target = patch.get("stage", snapshot["stage"])
            if target not in STAGES or target == "completed":
                raise ValueError("Completion requires independently verified release")
            if STAGES.index(target) < STAGES.index(snapshot["stage"]):
                raise ValueError("Round stage cannot move backwards")
            outcome = patch.get("executionOutcome", snapshot["executionOutcome"])
            if outcome not in (None, "passed", "failed", "cancelled"):
                raise ValueError("Unknown execution outcome")
            if snapshot["executionOutcome"] is not None and outcome != snapshot["executionOutcome"]:
                raise ValueError("Cleanup must preserve execution outcome")
            report_repository = ReportReceiptRepository(db, consumed_ids=() if current_only else None)
            report_repository.validate(snapshot)
            snapshot.update(deepcopy(patch))
            snapshot = batch_receipts.compact(db, snapshot, report_repository=report_repository,
                                               accept_hydrated_reports=True, raw_ids=() if current_only else None)
            snapshot["sequence"] += 1
            db.execute("UPDATE rounds SET snapshot=? WHERE round_id=?", (json.dumps(snapshot), round_id))
            return live_state.read(db, round_id) if current_only else batch_receipts.hydrate(db, snapshot, report_repository=report_repository)

    def batch_checkpoint(self, round_id, generation, token, jobs, phase, diagnostics, *, raw=None, raw_sha=None):
        """Typed diagnostic CAS; raw is inserted once, never loaded for appends.

        BEGIN IMMEDIATE serializes current-sequence read and compact update.
        No accepted measurement/current-freshness claim is made by this receipt.
        """
        if (not batch_receipts.BatchCheckpoint.valid(phase, diagnostics)
                or not isinstance(token,str) or re.fullmatch(r'[a-f0-9]{32}',token) is None):
            raise ValueError('Invalid batch checkpoint')
        # Encode caller-owned payloads before reserving the database writer.
        manifest = batch_receipts.encoded(jobs)
        point = json.loads(batch_receipts.encoded({'phase': phase, **diagnostics}))
        raw_text = batch_receipts.encoded(raw) if raw is not None else None
        if raw_text is not None and (len(raw_text.encode()) > 8*1024*1024
                or not isinstance(raw, dict) or set(raw) - {j['key'] for j in jobs}):
            raise ValueError('Invalid raw receipt size/identity')
        from backend.onprem.evidence.full_decode import decode_full
        with self.connection() as db:
            row = db.execute('SELECT round_id,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if not row or row[0] != round_id or row[1]:
                raise RoundConflict('Batch checkpoint owner changed')
            snapshot = decode_full(live_state.read(db, round_id, decode=False))
            json.dumps(snapshot, allow_nan=False)
            if (snapshot.get('roundId') != round_id or type(generation) is not int
                    or snapshot['generation'] != generation or maintenance_pending(snapshot) or snapshot['stage']=='completed'):
                raise RoundConflict('Batch checkpoint generation/maintenance changed')
            # Diagnostics do not consume or promote historical measurement reports.
            matches = [a for a in snapshot['actions'] if a.get('kind')=='traffic_window' and a.get('token')==token]
            if len(matches)!=1 or batch_receipts.encoded(matches[0].get('jobs')) != manifest:
                raise RoundConflict('Batch checkpoint manifest changed')
            action = matches[0]
            bind = batch_receipts.binding(snapshot,action)
            stored = db.execute('SELECT binding_sha,raw_sha FROM batch_raw_receipts WHERE round_id=? AND action_id=?',
                                (round_id,action['id'])).fetchone()
            if stored is None:
                if not isinstance(raw,dict) or action.get('workerDiagnostics') is not None:
                    raise ValueError('Missing original immutable raw receipt')
                text = raw_text
                raw_sha = batch_receipts.digest(text)
                db.execute('INSERT INTO batch_raw_receipts VALUES(?,?,?,?,?)',
                           (round_id,action['id'],bind,raw_sha,text))
                points=[]
            else:
                if raw is not None or stored != (bind,raw_sha):
                    raise ValueError('Immutable raw receipt binding/hash changed')
                current=action.get('workerDiagnostics',{})
                if current.get('rawRecordsRef') != {'schema':'cityverse.batch-raw/v1','sha256':raw_sha}:
                    raise ValueError('Missing immutable raw receipt reference')
                points=current['checkpoints']
            if not points or points[-1]!=point: points.append(point)
            if len(points)>8: raise ValueError('Too many batch checkpoints')
            action['workerDiagnostics']={'schemaVersion':'cityverse.batch-unvalidated/v1',
                'validated':False,'effectVerified':False,'rawRecordsRef':{'schema':'cityverse.batch-raw/v1','sha256':raw_sha},
                'checkpoints':points}
            expected_sequence=snapshot['sequence']
            snapshot['sequence']+=1
            indices = db.execute("SELECT key FROM json_each((SELECT snapshot FROM rounds WHERE round_id=?),'$.actions') "
                                 "WHERE json_extract(value,'$.id')=?", (round_id,action['id'])).fetchall()
            if len(indices) != 1 or type(indices[0][0]) is not int:
                raise RoundConflict('Current checkpoint action changed')
            path = '$.actions[' + str(indices[0][0]) + '].workerDiagnostics'
            changed=db.execute("UPDATE rounds SET snapshot=json_set(snapshot,?,json(?),'$.sequence',?) "
                               "WHERE round_id=? AND json_extract(snapshot,'$.sequence')=? AND released=0",
                               (path,batch_receipts.encoded(action['workerDiagnostics']),expected_sequence+1,
                                round_id,expected_sequence)).rowcount
            if changed!=1: raise RoundConflict('Batch checkpoint sequence superseded')
            return {'sequence':expected_sequence+1,'rawSha256':raw_sha}

    @staticmethod
    def _failure_scope(snapshot, round_id, generation, token):
        if (snapshot.get('roundId') != round_id or type(generation) is not int
                or snapshot.get('generation') != generation or snapshot.get('stage') == 'completed'
                or maintenance_pending(snapshot)):
            raise RoundConflict('Failure diagnostic owner changed')
        matches = [a for a in snapshot.get('actions', [])
                   if a.get('kind') == 'traffic_window' and a.get('mode') == 'batch' and a.get('token') == token]
        if len(matches) != 1:
            raise RoundConflict('Failure diagnostic shard changed')
        a = matches[0]
        return {key: deepcopy(a[key]) for key in
                ('id', 'kind', 'mode', 'token', 'jobs', 'shardIndex', 'detachedReservation') if key in a}

    def batch_failure_scope(self, round_id, generation, token):
        """Read only the exact shard metadata; never materialize raw or reports."""
        with self._read_connection() as db:
            row = db.execute('SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
        if not row or row[0] != round_id or row[2]:
            raise RoundConflict('Failure diagnostic owner changed')
        return self._failure_scope(json.loads(row[1]), round_id, generation, token)

    def record_batch_failure(self, round_id, generation, token, scope, phase, code, exits):
        """Append a validated shard failure without a hydrated whole-round update.

        This cannot accept traffic, replace measurements or alter ownership.
        Unrelated historical reports are validated when consumed, not here.
        """
        from backend.onprem.evidence.traffic_failure import validate_failure
        from backend.onprem.runtime.ue import timestamp
        # Freeze and validate caller inputs before requesting the writer.
        scope_text = batch_receipts.encoded(scope)
        scope = json.loads(scope_text)
        exits = json.loads(batch_receipts.encoded(exits))
        if not validate_failure(scope, phase, code, exits):
            raise ValueError('Invalid shard failure diagnostics')
        receipt = dict(id=str(uuid4()), kind='traffic_failure', requested=True,
            accepted=False, installed=False, effectVerified=False, requestedAt=timestamp(),
            trafficActionId=scope['id'], phase=phase, code=code, workerExits=exits)
        if len(batch_receipts.encoded(receipt).encode()) > 128*1024:
            raise ValueError('Shard failure diagnostic exceeds storage bound')
        with self.connection() as db:
            row = db.execute('SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
            if not row or row[0] != round_id or row[2]:
                raise RoundConflict('Failure diagnostic owner changed')
            snapshot = json.loads(row[1])
            current = self._failure_scope(snapshot, round_id, generation, token)
            if batch_receipts.encoded(current) != scope_text:
                raise RoundConflict('Failure diagnostic manifest changed')
            for prior in snapshot['actions']:
                if (prior.get('kind') == 'traffic_failure' and prior.get('trafficActionId') == scope['id']
                        and prior.get('phase') == phase and prior.get('code') == code):
                    return {'sequence': snapshot['sequence'], 'actionId': prior['id']}
            snapshot['actions'].append(receipt)
            snapshot['sequence'] += 1
            changed = db.execute('UPDATE rounds SET snapshot=? WHERE round_id=? AND snapshot=? AND released=0',
                (json.dumps(snapshot, allow_nan=False), round_id, row[1])).rowcount
            if changed != 1:
                raise RoundConflict('Failure diagnostic sequence superseded')
            return {'sequence': snapshot['sequence'], 'actionId': receipt['id']}

    def _publication_owner(self, db, round_id, generation, *, report_repository=None, raw_ids=None):
        """Private transition input, never an API/evidence projection.

        Compact validates raw hashes for the transition's consumed actions,
        without parsing old report payloads. Historical hydration is unchanged.
        """
        row = db.execute('SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
        if not row or row[0] != round_id or row[2]:
            raise RoundConflict('Publication owner changed')
        snapshot = json.loads(row[1])
        if (type(generation) is not int or snapshot['generation'] != generation
                or maintenance_pending(snapshot) or snapshot['executionOutcome'] is not None):
            raise RoundConflict('Publication generation/maintenance/outcome changed')
        return row[1], batch_receipts.compact(db, snapshot, copy_snapshot=False,
                                            report_repository=report_repository, raw_ids=raw_ids)

    @staticmethod
    def _publication_commit(db, original, snapshot, *, report_repository=None, report_ids=()):
        (report_repository or ReportReceiptRepository(db)).normalize(
            snapshot, originate_ids=report_ids, strict_json=True)
        snapshot['sequence'] += 1
        changed = db.execute('UPDATE rounds SET snapshot=? WHERE round_id=? AND snapshot=? AND released=0',
            (json.dumps(snapshot, allow_nan=False), snapshot['roundId'], original)).rowcount
        if changed != 1:
            raise RoundConflict('Publication snapshot superseded')
        return {'controlOnly': True, **{k:snapshot[k] for k in ('roundId','generation','sequence','stage')}}

    def reserve_batch_action(self, round_id, generation, journal):
        """Append a launch intent without hydrating earlier immutable raw reports.

        This is ownership metadata only: it cannot accept a measurement or alter
        prior evidence. Launch consumes no previous measurement payload.
        """
        return self.reserve_batch_actions(round_id, generation, [journal])

    def reserve_batch_actions(self, round_id, generation, journals):
        """Commit all cohort launch intents atomically before any worker starts."""
        if not isinstance(journals, list) or not 1 <= len(journals) <= 10:
            raise RoundConflict('Invalid batch intent cohort')
        if len(journals) > 1 and any(j.get('shardIndex') != i or j.get('shardCount') != len(journals)
                                     for i, j in enumerate(journals) if isinstance(j, dict)):
            raise RoundConflict('Batch intent cohort is incomplete')
        with self.connection() as db:
            report_repository = ReportReceiptRepository(db, consumed_ids=())
            original, snapshot = self._publication_owner(db, round_id, generation,
                                                         report_repository=report_repository, raw_ids=())
            for journal in journals:
                if (not isinstance(journal, dict) or journal.get('kind') != 'traffic_window'
                    or journal.get('mode') != 'batch' or journal.get('stage') != snapshot['stage']
                    or snapshot['stage'] not in ('measuring_baseline', 'measuring_result')
                    or journal.get('requested') is not True
                    or any(journal.get(k) is not False for k in
                           ('accepted', 'installed', 'effectVerified', 'cleanupVerified'))
                    or not isinstance(journal.get('id'), str) or not journal['id']
                    or not isinstance(journal.get('token'), str)
                    or re.fullmatch(r'[0-9a-f]{32}', journal['token']) is None
                    or not isinstance(journal.get('jobs'), list) or not journal['jobs']
                    or any(k in journal for k in ('workerDiagnostics', 'reports', 'maintenance'))
                    or any(a.get('id') == journal['id'] or a.get('token') == journal['token']
                           for a in snapshot['actions'])):
                    raise RoundConflict('Invalid or duplicate batch launch intent')
                snapshot['actions'].append(deepcopy(journal))
            # Also fence malformed references/reservations in the new intent.
            batch_receipts.control_projection(db, snapshot)
            return self._publication_commit(db, original, snapshot, report_repository=report_repository)

    def finish_batch_action(self, round_id, generation, identifier, *, token, jobs,
                            stage, reports, measurements, observed_at, finished_at):
        """Persist already-validated batch result; does NOT validate measurements.

        Caller must retain all original reducers/native/cleanup gates. This
        typed transition cannot replace immutable diagnostic raw or identity.
        """
        if stage not in ('measuring_baseline','measuring_result'):
            raise RoundConflict('Invalid batch publication stage')
        with self.connection() as db:
            report_repository = ReportReceiptRepository(db, consumed_ids=(identifier,))
            original, snapshot = self._publication_owner(db, round_id, generation,
                                                         report_repository=report_repository, raw_ids=(identifier,))
            matches = [a for a in snapshot['actions'] if a.get('id') == identifier]
            if (snapshot['stage'] != stage or len(matches) != 1
                    or matches[0].get('kind') != 'traffic_window' or matches[0].get('mode') != 'batch'
                    or matches[0].get('stage') != stage or matches[0].get('token') != token
                    or not batch_receipts.same_json(matches[0].get('jobs'), jobs)
                    or not isinstance(matches[0].get('workerDiagnostics'), dict)
                    or 'rawRecordsRef' not in matches[0]['workerDiagnostics']
                    or matches[0].get('accepted') is True):
                raise RoundConflict('Batch publication identity/stage changed')
            matches[0].update(accepted=True, installed=True, effectVerified=True,
                cleanupVerified=True, finishedAt=finished_at, reports=deepcopy(reports))
            snapshot.update(measurements=deepcopy(measurements), observedAt=observed_at)
            return self._publication_commit(db, original, snapshot, report_repository=report_repository,
                report_ids=[identifier] if self.canonical_reports else ())

    def finish_batch_shards(self, round_id, generation, identifiers, *, stage, jobs, reports,
                            shard_receipts, aggregate, measurements, observed_at, finished_at):
        """Atomically publish one exact, independently reduced multi-shard window."""
        if stage not in ('measuring_baseline', 'measuring_result'):
            raise RoundConflict('Invalid shard publication stage')
        with self.connection() as db:
            report_repository = ReportReceiptRepository(db, consumed_ids=identifiers)
            original, snapshot = self._publication_owner(db, round_id, generation,
                                                         report_repository=report_repository, raw_ids=identifiers)
            prior_ids = {identifier for a in snapshot['actions']
                         if a.get('kind') == 'traffic_aggregate' and a.get('stage') == stage
                         for identifier in a.get('shardActionIds', [])}
            windows = [a for a in snapshot['actions'] if a.get('kind') == 'traffic_window'
                       and a.get('stage') == stage and a.get('id') not in prior_ids]
            if (snapshot['stage'] != stage or not isinstance(identifiers, list)
                    or len(windows) != len(identifiers) or not 2 <= len(windows) <= 10
                    or [a.get('id') for a in windows] != identifiers
                    or [a.get('shardIndex') for a in windows] != list(range(len(windows)))
                    or not isinstance(shard_receipts, list) or len(shard_receipts) != len(windows)
                    or any(a.get('shardCount') != len(windows) or a.get('mode') != 'batch'
                           or a.get('cleanupVerified') is not False or a.get('accepted') is True
                           or not isinstance(a.get('workerDiagnostics'), dict)
                           or 'rawRecordsRef' not in a['workerDiagnostics'] for a in windows)
                    or any(receipt != {'actionId': a['id'], 'token': a['token'],
                                         'shardIndex': index, 'cleanupVerified': True}
                           for index, (a, receipt) in enumerate(zip(windows, shard_receipts)))
                    or not batch_receipts.same_json([j for a in windows for j in a['jobs']], jobs)
                    or not isinstance(reports, list) or len(reports) != len(jobs)
                    or len(jobs) != 2 * len(snapshot.get('perUe', []))
                    or {(j.get('supi'), j.get('direction')) for j in jobs}
                       != {(r['supi'], direction) for r in snapshot['perUe'] for direction in ('ul', 'dl')}
                    or any(not isinstance(report, dict) or report.get('direction') != job.get('direction')
                           or not isinstance(report.get('binding'), dict)
                           or report['binding'].get('supi') != job.get('supi')
                           or report.get('source') != 'native-iperf3-receiver'
                           or any(type(report.get(k)) not in (int, float) or not math.isfinite(report[k])
                                  for k in ('effectiveStartedAt', 'effectiveFinishedAt'))
                           for job, report in zip(jobs, reports))
                    or not isinstance(aggregate, dict)
                    or set(aggregate) not in ({'effectiveStartedAt', 'effectiveFinishedAt', 'flowCount', 'source'},
                                             {'effectiveStartedAt', 'effectiveFinishedAt', 'flowCount', 'source', 'accountingObservation'})
                    or aggregate['flowCount'] != len(jobs)
                    or aggregate['source'] != 'native-iperf3-receiver'
                    or any(type(aggregate[k]) not in (int, float) or not math.isfinite(aggregate[k])
                           for k in ('effectiveStartedAt', 'effectiveFinishedAt'))
                    or aggregate['effectiveStartedAt'] != max(r['effectiveStartedAt'] for r in reports)
                    or aggregate['effectiveFinishedAt'] != min(r['effectiveFinishedAt'] for r in reports)
                    or aggregate['effectiveFinishedAt'] - aggregate['effectiveStartedAt'] < 8):
                raise RoundConflict('Incomplete or altered shard publication')
            from backend.onprem.traffic.protocol import AccountingObservation
            clocked = any('accounting_timestamp' in report.get(side, {}).get('start', {})
                          for report in reports for side in ('client', 'server'))
            observation = aggregate.get('accountingObservation')
            boot_ids = {a.get('detachedReservation', {}).get('clockBootId') for a in windows}
            if ((clocked and (len(boot_ids) != 1 or None in boot_ids or not AccountingObservation.valid(
                    observation, boot_id=next(iter(boot_ids)))))
                    or (not clocked and 'accountingObservation' in aggregate)):
                raise RoundConflict('Shard accounting observation is missing or belongs to another boot')
            offset = 0
            for window in windows:
                count = len(window['jobs'])
                window.update(accepted=True, installed=True, effectVerified=True,
                              cleanupVerified=True, finishedAt=finished_at,
                              reports=deepcopy(reports[offset:offset+count]))
                offset += count
            snapshot['actions'].append({'id': str(uuid4()), 'kind': 'traffic_aggregate',
                'stage': stage, 'shardActionIds': deepcopy(identifiers),
                **deepcopy(aggregate), 'accepted': True, 'cleanupVerified': True,
                'requested': False, 'installed': False,
                'effectVerified': True, 'finishedAt': finished_at})
            snapshot.update(measurements=deepcopy(measurements), observedAt=observed_at)
            return self._publication_commit(db, original, snapshot, report_repository=report_repository,
                report_ids=identifiers if self.canonical_reports else ())

    def publish_batch_results(self, cursor):
        """Exact successor of validated result publication, original clock kept."""
        if not isinstance(cursor, dict) or cursor.get('controlOnly') is not True:
            raise RoundConflict('Publication cursor required')
        with self.connection() as db:
            report_repository = ReportReceiptRepository(db, consumed_ids=())
            original, snapshot = self._publication_owner(db, cursor.get('roundId'), cursor.get('generation'),
                                                         report_repository=report_repository, raw_ids=())
            if (type(cursor.get('sequence')) is not int or snapshot['sequence'] != cursor['sequence']
                    or snapshot['stage'] != 'measuring_result' or cursor.get('stage') != 'measuring_result'):
                raise RoundConflict('Result publication sequence/stage changed')
            aggregates = [a for a in snapshot['actions'] if a.get('kind') == 'traffic_aggregate'
                          and a.get('stage') == 'measuring_result']
            latest_ids = aggregates[-1].get('shardActionIds', []) if aggregates else []
            windows = [a for a in snapshot['actions'] if a.get('kind') == 'traffic_window'
                       and a.get('stage') == 'measuring_result'
                       and (not aggregates or a.get('id') in latest_ids)]
            single = len(windows) == 1 and not aggregates
            sharded = (bool(aggregates) and 2 <= len(windows) <= 10
                       and latest_ids == [a.get('id') for a in windows]
                       and aggregates[-1].get('flowCount') == 2 * len(snapshot.get('perUe', []))
                       and all(aggregates[-1].get(k) is True for k in
                               ('accepted', 'cleanupVerified', 'effectVerified')))
            if (not (single or sharded) or any(a.get(k) is not True for a in windows
                    for k in ('accepted','installed','effectVerified','cleanupVerified'))):
                raise RoundConflict('Verified batch result required')
            for window in windows:
                batch_receipts.receipt(db, snapshot, window)
            report_repository = ReportReceiptRepository(db, consumed_ids=[a['id'] for a in windows])
            snapshot.update(stage='results_ready', executionOutcome='passed')
            return self._publication_commit(db, original, snapshot, report_repository=report_repository)

    def complete(self, round_id, expected_sequence, release_proof, *, current_only=False):
        common = {"trafficStopped", "policiesAbsent", "rulesRestored"}
        legacy_release = release_proof.get("uesReleased") is True and release_proof.get("podsAbsent") is True
        persistent_release = (release_proof.get("ueLeaseReleased") is True
                              and release_proof.get("fleetPreserved") is True
                              and release_proof.get("uesReleased") is False
                              and release_proof.get("podsAbsent") is False)
        if any(release_proof.get(key) is not True for key in common) or not (legacy_release or persistent_release):
            raise RoundConflict("Incomplete release proof; reservation remains active")
        with self.connection() as db:
            from backend.onprem.scenarios.intent_traffic import read_lease
            lease=read_lease(db,round_id)
            if lease is not None and lease['state']!='released':
                raise RoundConflict('Intent restoration must complete before traffic cleanup')
            if lease is not None and not legacy_release:
                raise RoundConflict('Formal traffic completion requires native UE and Pod disappearance')
            row = db.execute("SELECT snapshot FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if not row:
                raise KeyError(round_id)
            snapshot = json.loads(row[0])
            if maintenance_pending(snapshot):
                raise RoundConflict("Maintenance reservation is unresolved")
            if snapshot["sequence"] != expected_sequence or snapshot["stage"] != "cleaning":
                raise RoundConflict("Completion requires current cleaning snapshot")
            if snapshot["executionOutcome"] is None:
                raise RoundConflict("Execution outcome must be recorded before completion")
            report_repository = ReportReceiptRepository(db, consumed_ids=() if current_only else None)
            report_repository.normalize(snapshot)
            # This exact pending observation is resolved by the verified release;
            # preserve execution failures and all other cleanup diagnostics.
            snapshot["errors"] = [error for error in snapshot["errors"]
                                  if error != "Cleanup probe: release.known_resources_remain"]
            snapshot.update(stage="completed", cleanupStatus="verified", sequence=expected_sequence + 1,
                            releaseProof=deepcopy(release_proof))
            db.execute("UPDATE rounds SET snapshot=?,released=1 WHERE round_id=?", (json.dumps(snapshot), round_id))
            return live_state.read(db, round_id) if current_only else batch_receipts.hydrate(db, snapshot, report_repository=report_repository)


    def reserve_maintenance(self, plan, *, plan_sha256, drain, now):
        """Startup-only admission after OS fencing; neither dispatch nor network proof.

        The caller must independently establish graceful old-helper drain. The
        immutable operator plan contains ONLY the old epoch, never a guessed new ID.
        """
        required = {"schema", "id", "roundId", "generation", "actionId", "token", "jobsDigest",
                    "issuedAt", "expiresAt", "oldIdentity", "drainDigest"}
        if (not isinstance(plan, dict) or set(plan) != required or
                plan["schema"] != "cityverse.retired-epoch-plan/v1" or
                maintenance_digest(plan) != plan_sha256):
            raise RoundConflict("Invalid immutable maintenance plan")
        if not _maintenance_uuid(plan["id"]):
            raise RoundConflict("Invalid maintenance identity")
        old_fields = {"context", "namespace", "node", "nodeBootId", "pod", "podUid", "containerName",
                      "image", "oldContainerId", "oldPidNamespace", "oldPid", "oldStartTicks", "oldPid1",
                      "otherIdentitiesDigest", "bindingsDigest", "archiveDigest"}
        if not isinstance(plan["oldIdentity"], dict) or set(plan["oldIdentity"]) != old_fields:
            raise RoundConflict("Old container epoch is required")
        old = plan["oldIdentity"]
        from backend.onprem.runtime.kubeadm import TRAFFIC_IMAGE
        if (old["containerName"] != "traffic" or old["image"] != TRAFFIC_IMAGE or
                not re.fullmatch(r"containerd://[0-9a-f]{64}", str(old["oldContainerId"])) or
                not re.fullmatch(r"pid:\[[1-9][0-9]*\]", str(old["oldPidNamespace"])) or
                any(type(old[k]) is not int or old[k] <= 0 for k in ("oldPid", "oldStartTicks")) or
                any(not re.fullmatch(r"[0-9a-f]{64}", str(old[k])) for k in
                    ("otherIdentitiesDigest", "bindingsDigest", "archiveDigest")) or
                not isinstance(old["oldPid1"], dict) or set(old["oldPid1"]) != {"pid", "comm", "startTicks"} or
                type(old["oldPid1"].get("pid")) is not int or old["oldPid1"]["pid"] != 1 or
                type(old["oldPid1"].get("startTicks")) is not int or old["oldPid1"]["startTicks"] <= 0 or
                type(old["oldPid1"].get("comm")) is not str or
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", old["oldPid1"]["comm"]) or
                not _maintenance_uuid(old["nodeBootId"]) or not _maintenance_uuid(old["podUid"]) or
                any(not isinstance(old[k], str) or not old[k] or len(old[k]) > 256 for k in
                    ("context", "namespace", "node", "nodeBootId", "pod", "podUid"))):
            raise RoundConflict("Invalid original container identity")
        if (not isinstance(drain, dict) or maintenance_digest(drain) != plan["drainDigest"] or
                drain.get("schema") != "cityverse.native-helper-drain/v1" or
                any(drain.get(k) is not True for k in
                    ("graceful", "controllerDrained", "roundTaskDrained", "transportsDrained", "processExited")) or
                drain.get("roundId") != plan["roundId"] or
                drain.get("generation") != plan["generation"]):
            raise RoundConflict("Explicit graceful helper drain is required")
        now = _maintenance_time(now)
        issued, expires = _maintenance_time(plan["issuedAt"]), _maintenance_time(plan["expiresAt"])
        if not issued <= now < expires <= issued + 900 or not issued <= _maintenance_time(drain.get("observedAt")) <= now:
            raise RoundConflict("Maintenance authorization expired or future dated")
        def transition(snapshot, action):
            existing = action.get("maintenance")
            if existing:
                if existing.get("id") == plan["id"] and existing.get("planSha256") == plan_sha256:
                    return False
                raise RoundConflict("Existing maintenance cannot be replaced")
            if any("maintenance" in a and a is not action for a in snapshot["actions"]):
                raise RoundConflict("Another maintenance action exists")
            if maintenance_digest(action.get("jobs")) != plan["jobsDigest"]:
                raise RoundConflict("Maintenance jobs changed")
            if maintenance_digest(action.get("bindings")) != old["bindingsDigest"]:
                raise RoundConflict("Maintenance bindings changed")
            jobs = action.get("jobs")
            if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100 or any(
                    j.get("receiver_pod") != old["pod"] or j.get("receiver_uid") != old["podUid"] for j in jobs):
                raise RoundConflict("Maintenance receiver identity changed")
            action["maintenance"] = {"id": plan["id"], "state": "reserved",
                "planSha256": plan_sha256, "plan": deepcopy(plan), "drain": deepcopy(drain),
                "actionId": action["id"], "token": action["token"], "reservedAt": now}
            return True
        return self._maintenance_transition(plan["roundId"], plan["generation"], plan["actionId"], plan["token"], transition)

    def _maintenance_transition(self, round_id, generation, action_id, token, transition):
        if (type(generation) is not int or not 0 < generation < 2**63 or
                not _maintenance_uuid(round_id) or not _maintenance_uuid(action_id) or
                type(token) is not str or not re.fullmatch(r"[0-9a-f]{32}", token)):
            raise RoundConflict("Invalid maintenance owner identity")
        with self.connection() as db:
            row = db.execute("SELECT round_id,snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            if not row or row[0] != round_id or row[2]:
                raise RoundConflict("Maintenance does not own current round")
            snapshot = json.loads(row[1])
            if (snapshot["generation"] != generation or snapshot["stage"] != "cleaning" or
                    snapshot["executionOutcome"] not in ("failed", "cancelled")):
                raise RoundConflict("Maintenance requires failed owned cleanup")
            report_repository = ReportReceiptRepository(db)
            report_repository.normalize(snapshot, strict_json=True)
            matches = [a for a in snapshot["actions"] if a.get("id") == action_id]
            if (len(matches) != 1 or matches[0].get("kind") != "traffic_window" or
                    matches[0].get("mode") != "batch" or matches[0].get("token") != token or
                    matches[0].get("cleanupVerified") is not False):
                raise RoundConflict("Maintenance traffic ownership changed")
            if transition(snapshot, matches[0]):
                snapshot["sequence"] += 1
                db.execute("UPDATE rounds SET snapshot=? WHERE round_id=?", (json.dumps(snapshot, allow_nan=False), round_id))
            return batch_receipts.hydrate(db, snapshot, report_repository=report_repository)

    def claim_maintenance(self, approval, *, authorization_sha256, original_receipt, now):
        """One-shot claim only; replay cannot grant dispatch after lost acknowledgement."""
        now = _maintenance_time(now)
        if (maintenance_digest(approval) != authorization_sha256 or
                not _maintenance_time(approval["issuedAt"]) <= now < _maintenance_time(approval["expiresAt"]) <= approval["issuedAt"] + 900):
            raise RoundConflict("Invalid closure authorization")
        receipt_fields = {"schema", "approvalDigest", "approvalId", "reservationId",
                          "roundId", "generation", "actionId", "token", "oldContainerId", "newContainerId",
                          "proofDigest", "evidenceValidity"}
        expected_receipt = {key: approval[key] for key in ("approvalId", "reservationId", "roundId",
                            "generation", "actionId", "token", "oldContainerId", "newContainerId")}
        expected_receipt.update(schema="cityverse.retired-container-closure/v1", approvalDigest=authorization_sha256,
                                evidenceValidity=False)
        if (not isinstance(original_receipt, dict) or set(original_receipt) != receipt_fields or
                any(original_receipt.get(k) != v for k, v in expected_receipt.items()) or
                original_receipt.get("evidenceValidity") is not False or
                not re.fullmatch(r"[0-9a-f]{64}", str(original_receipt.get("proofDigest")))):
            raise RoundConflict("Original validated closure receipt required at claim")
        def transition(snapshot, action):
            record = action.get("maintenance", {})
            if (record.get("state") != "reserved" or record.get("id") != approval["reservationId"] or
                    record.get("planSha256") != approval["planDigest"]):
                raise RoundConflict("Closure claim is unavailable; do not replay")
            old = record["plan"]["oldIdentity"]
            if any(approval.get(key) != value for key, value in old.items()):
                raise RoundConflict("Closure authorization changed old epoch")
            record.update(state="closure_claimed", authorizationSha256=authorization_sha256,
                          approvalId=approval["approvalId"], authorization=deepcopy(approval),
                          originalReceipt=deepcopy(original_receipt), claimedAt=now)
            return True
        return self._maintenance_transition(approval["roundId"], approval["generation"], approval["actionId"], approval["token"], transition)

    def acknowledge_maintenance(self, round_id, generation, action_id, token, *, receipt, now):
        """Called only after trusted physical collector + fixed-worker readback.

        No startup or HTTP path calls this method in this implementation batch.
        """
        now = _maintenance_time(now)
        def transition(snapshot, action):
            record = action.get("maintenance", {})
            if record.get("state") not in ("closure_claimed", "unknown"):
                raise RoundConflict("No matching closure claim")
            expected = {"reservationId": record["id"], "planDigest": record["planSha256"],
                        "approvalDigest": record["authorizationSha256"], "actionId": action_id,
                        "token": token, "evidenceValidity": False}
            if (not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()) or
                    receipt.get("originalReceipt") != record.get("originalReceipt") or
                    receipt.get("evidenceValidity") is not False or
                    receipt.get("closedReadbackVerified") is not True or
                    not record["claimedAt"] <= _maintenance_time(receipt.get("observedAt")) <= now or
                    receipt["observedAt"] < record.get("unknownAt", record["claimedAt"]) or
                    now - receipt["observedAt"] > 15):
                raise RoundConflict("Fresh independent closure acknowledgement required")
            record.update(state="acknowledged", acknowledgement=deepcopy(receipt), acknowledgedAt=now)
            return True
        return self._maintenance_transition(round_id, generation, action_id, token, transition)

    def mark_maintenance_unknown(self, round_id, generation, action_id, token, *, now):
        """Persist uncertain dispatch; expiry and restart never reset launch permission."""
        now = _maintenance_time(now)
        def transition(snapshot, action):
            record = action.get("maintenance", {})
            if record.get("state") not in ("closure_claimed", "unknown"):
                raise RoundConflict("No dispatched maintenance claim")
            record.update(state="unknown", unknownAt=now)
            return True
        return self._maintenance_transition(round_id, generation, action_id, token, transition)
