"""Private evidence references and durable operation intents beside checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from backend.onprem.agents.contracts import AgentLoopBlocked, _digest


class GraphArtifacts:
    def __init__(self, path: Path, thread_id: str):
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Graph evidence requires an absolute non-symlink path")
        self.path, self.thread_id = path, thread_id
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS graph_artifacts (
                    thread_id TEXT, ref TEXT, value TEXT, PRIMARY KEY(thread_id,ref));
                CREATE TABLE IF NOT EXISTS graph_operations (
                    thread_id TEXT, op TEXT, value TEXT, PRIMARY KEY(thread_id,op));
                CREATE TABLE IF NOT EXISTS graph_snapshots (
                    thread_id TEXT, snapshot_key TEXT, value TEXT,
                    PRIMARY KEY(thread_id,snapshot_key));
                CREATE TABLE IF NOT EXISTS graph_events (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, value TEXT);
                CREATE TABLE IF NOT EXISTS graph_event_retention (
                    thread_id TEXT PRIMARY KEY, pruned_through INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS graph_events_thread_ordinal ON graph_events(thread_id,ordinal);
                CREATE TABLE IF NOT EXISTS graph_outcomes (
                    thread_id TEXT, cycle INTEGER, value TEXT, PRIMARY KEY(thread_id,cycle));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind, value):
        ref = kind + ":" + _digest(value)
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO graph_artifacts VALUES(?,?,?)", (self.thread_id, ref, encoded))
        return ref

    def get(self, ref):
        with self.db() as db:
            row = db.execute("SELECT value FROM graph_artifacts WHERE thread_id=? AND ref=?", (self.thread_id, ref)).fetchone()
        if row is None:
            raise AgentLoopBlocked("graph.reference_outside_round")
        return json.loads(row[0])

    def operation(self, op, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT INTO graph_operations VALUES(?,?,?) ON CONFLICT(thread_id,op) DO UPDATE SET value=excluded.value",
                           (self.thread_id, op, json.dumps(value, sort_keys=True, allow_nan=False)))
            row = db.execute("SELECT value FROM graph_operations WHERE thread_id=? AND op=?", (self.thread_id, op)).fetchone()
        return json.loads(row[0]) if row else None

    def freeze(self, key, value=None):
        """Read a snapshot, or persist a non-None first writer's replay input."""
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("Invalid snapshot key")
        with self.db() as db:
            if value is not None:
                encoded = json.dumps(value, sort_keys=True, allow_nan=False)
                db.execute("INSERT OR IGNORE INTO graph_snapshots VALUES(?,?,?)",
                           (self.thread_id, key, encoded))
            row = db.execute("SELECT value FROM graph_snapshots WHERE thread_id=? AND snapshot_key=?",
                             (self.thread_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def audit(self, **event):
        allowed = {"role", "phase", "tool", "response_id", "status", "cycle", "code", "model",
                   "kind", "call_id", "turn", "started_at", "ended_at", "duration_ms",
                   "input_summary", "output_summary", "plan_ref", "before_ref", "after_ref",
                   "action_ref", "verification_ref", "display_ref", "action", "targetRefs"}
        if set(event) - allowed:
            raise ValueError("Unbounded audit field")
        event.setdefault("tool", None)
        if "targetRefs" in event:
            from backend.onprem.agents.topology_observability import clean_targets
            if clean_targets(event["targetRefs"]) != event["targetRefs"]:
                raise ValueError("Invalid audit targets")
        # Do not accept prompt/model/tool blobs through the bounded event channel.
        encoded = json.dumps(event, allow_nan=False)
        if len(encoded) > 4096 or any(isinstance(value, str) and len(value) > 256 for value in event.values()):
            raise ValueError("Unbounded audit value")
        for key in ("input_summary", "output_summary"):
            if key in event and (type(event[key]) is not dict or set(event[key]) - {"type", "fieldCount", "itemCount"}
                    or event[key].get("type") not in {"object", "array", "string", "number", "boolean", "null"}
                    or any(type(v) is not int or not 0 <= v <= 1000000 for k, v in event[key].items() if k != "type")):
                raise ValueError("Unbounded audit summary")
        event["at"] = time.time()
        with self.db() as db:
            inserted = db.execute("INSERT INTO graph_events(thread_id,value) VALUES(?,?)", (self.thread_id, json.dumps(event)))
            cutoff = db.execute("SELECT ordinal FROM graph_events WHERE thread_id=? ORDER BY ordinal DESC LIMIT 1 OFFSET 10000",
                                (self.thread_id,)).fetchone()
            if cutoff:
                db.execute("INSERT INTO graph_event_retention VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET "
                           "pruned_through=MAX(pruned_through,excluded.pruned_through)", (self.thread_id, cutoff[0]))
            db.execute("DELETE FROM graph_events WHERE thread_id=? AND ordinal NOT IN "
                       "(SELECT ordinal FROM graph_events WHERE thread_id=? ORDER BY ordinal DESC LIMIT 10000)",
                       (self.thread_id, self.thread_id))
        return {**event, "ordinal": inserted.lastrowid}

    @staticmethod
    def read_events(path: Path, thread_id: str, *, after=0, limit=100, role=None, cycle=None, call_id=None, cycles=None):
        """Read an existing event cursor without initializing or mutating storage."""
        if (not isinstance(path, Path) or not path.is_absolute() or path.is_symlink()
                or not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 256
                or type(after) is not int or after < 0
                or type(limit) is not int or not 1 <= limit <= 500
                or role is not None and role not in {"planner", "executor", "validator"}
                or cycle is not None and (type(cycle) is not int or not 0 <= cycle <= 10000)
                or call_id is not None and (not isinstance(call_id, str) or not 1 <= len(call_id) <= 256)
                or cycles is not None and (not isinstance(cycles, (set, list, tuple)) or len(cycles) > 10000
                    or any(type(c) is not int or not 0 <= c <= 10000 for c in cycles))):
            raise ValueError("Invalid event cursor")
        empty = {"events": [], "nextCursor": after, "hasMore": False, "gap": False,
                 "oldestCursor": None, "prunedThroughCursor": 0}
        if not path.is_file():
            return empty
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            # A read transaction keeps the retention watermark and page coherent.
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "graph_events" not in tables:
                return empty
            oldest = db.execute("SELECT MIN(ordinal) FROM graph_events WHERE thread_id=?", (thread_id,)).fetchone()[0]
            retention = db.execute("SELECT pruned_through FROM graph_event_retention WHERE thread_id=?", (thread_id,)).fetchone() if "graph_event_retention" in tables else None
            pruned = retention[0] if retention else 0
            query, parameters = "SELECT ordinal,value FROM graph_events WHERE thread_id=? AND ordinal>?", [thread_id, after]
            for key, value in (("role", role), ("cycle", cycle), ("call_id", call_id)):
                if value is not None:
                    query += " AND json_extract(value,'$." + key + "')=?"
                    parameters.append(value)
            if cycles is not None:
                query += " AND json_extract(value,'$.cycle') IN (" + ",".join("?" for _ in cycles) + ")"
                parameters.extend(sorted(cycles))
            rows = db.execute(query + " ORDER BY ordinal LIMIT ?", [*parameters, limit + 1]).fetchall()
            events = [{**json.loads(row[1]), "ordinal": row[0]} for row in rows[:limit]]
            return {"events": events, "nextCursor": events[-1]["ordinal"] if events else after,
                    "hasMore": len(rows) > limit, "gap": after < pruned,
                    "oldestCursor": oldest, "prunedThroughCursor": pruned}
        finally:
            db.close()

    def outcome(self, cycle, value):
        with self.db() as db:
            encoded = json.dumps(value, sort_keys=True, allow_nan=False)
            prior = db.execute("SELECT value FROM graph_outcomes WHERE thread_id=? AND cycle=?", (self.thread_id, cycle)).fetchone()
            if prior and prior[0] != encoded:
                raise AgentLoopBlocked("graph.outcome_changed_on_replay")
            db.execute("INSERT OR IGNORE INTO graph_outcomes VALUES(?,?,?)", (self.thread_id, cycle, encoded))

    def outcomes(self):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM graph_outcomes WHERE thread_id=? ORDER BY cycle", (self.thread_id,))]

    def events(self):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM graph_events WHERE thread_id=? ORDER BY ordinal", (self.thread_id,))]
