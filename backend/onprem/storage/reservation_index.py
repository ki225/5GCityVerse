"""Transactionally invalidated maintenance metadata for reservation admission.

Only SQL-derived maintenance results are retained, never native evidence or
receipt validation. Every rounds INSERT/UPDATE/DELETE invalidates its entry in
the same SQLite transaction, including writes by older/offline ledger clients.
Missing/incompatible schema falls back to the original full history scan.
"""
from __future__ import annotations

from contextlib import closing
import json

from backend.onprem.evidence.control_decode import parser_policy


_TABLE = 'reservation_maintenance'
_POLICY = 'reservation_maintenance_policy'
_SPECS = {
    _TABLE: """CREATE TABLE reservation_maintenance (
        round_id TEXT PRIMARY KEY NOT NULL,
        dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
        pending INTEGER NOT NULL CHECK(pending IN (0,1)))""",
    _POLICY: """CREATE TABLE reservation_maintenance_policy (
        id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL)""",
    'reservation_maintenance_dirty': """CREATE INDEX reservation_maintenance_dirty
        ON reservation_maintenance(round_id) WHERE dirty=1""",
    'reservation_maintenance_pending': """CREATE INDEX reservation_maintenance_pending
        ON reservation_maintenance(round_id) WHERE pending=1""",
    'reservation_maintenance_insert': """CREATE TRIGGER reservation_maintenance_insert
        AFTER INSERT ON rounds BEGIN
        INSERT OR REPLACE INTO reservation_maintenance VALUES(NEW.round_id,1,0);
        END""",
    'reservation_maintenance_update': """CREATE TRIGGER reservation_maintenance_update
        AFTER UPDATE OF snapshot,round_id ON rounds BEGIN
        DELETE FROM reservation_maintenance WHERE round_id=OLD.round_id;
        INSERT OR REPLACE INTO reservation_maintenance VALUES(NEW.round_id,1,0);
        END""",
    'reservation_maintenance_delete': """CREATE TRIGGER reservation_maintenance_delete
        AFTER DELETE ON rounds BEGIN
        DELETE FROM reservation_maintenance WHERE round_id=OLD.round_id;
        END""",
}


def _schema(db):
    return {name: sql for name, sql in db.execute(
        'SELECT name,sql FROM sqlite_master WHERE name IN (' + ','.join('?' for _ in _SPECS) + ')',
        tuple(_SPECS))}


def install(db):
    """Called only by the normal schema-creating constructor, in its transaction."""
    existing = _schema(db)
    if existing:
        # Never quietly trust/recreate a partially invalidated index. Existing
        # incompatible objects leave admission on its original full scan.
        return
    for sql in _SPECS.values():
        db.execute(sql)
    db.execute('INSERT INTO reservation_maintenance SELECT round_id,1,0 FROM rounds')


def pending(db, *, decode, predicate):
    """Use the caller's held writer fence; preserve predicate/errors/row order."""
    def original():
        with closing(db.execute('SELECT snapshot FROM rounds')) as rows:
            return any(predicate(decode(row[0])) for row in rows)

    if _schema(db) != _SPECS:
        return original()
    policy = parser_policy()
    policy_text = json.dumps(['reservation-maintenance/v1', *policy])
    previous = db.execute('SELECT policy FROM reservation_maintenance_policy WHERE id=1').fetchone()
    if previous != (policy_text,):
        # Interpreter numeric/depth policies can change while a process runs.
        db.execute('UPDATE reservation_maintenance SET dirty=1')
        db.execute('INSERT OR REPLACE INTO reservation_maintenance_policy VALUES(1,?)', (policy_text,))
    # Reconcile metadata coverage through the small covering round_id index,
    # without reading historical snapshot blobs. Also handles removed entries.
    db.execute('INSERT OR IGNORE INTO reservation_maintenance SELECT round_id,1,0 FROM rounds')
    with closing(db.execute('SELECT r.round_id FROM reservation_maintenance AS m '
                            'JOIN rounds AS r ON r.round_id=m.round_id '
                            'WHERE m.dirty=1 OR m.pending=1 ORDER BY r.ordinal')) as rows:
        identifiers = [row[0] for row in rows]
    # Do not mutate the table while its SELECT is active. Retain only small
    # identifiers here, never a list of potentially large snapshot blobs.
    for round_id in identifiers:
        raw = db.execute('SELECT snapshot FROM rounds WHERE round_id=?', (round_id,)).fetchone()[0]
        blocked = predicate(decode(raw))
        db.execute('UPDATE reservation_maintenance SET dirty=0,pending=? WHERE round_id=?',
                   (int(blocked), round_id))
        if blocked:
            return True
    if parser_policy() != policy:
        db.execute('UPDATE reservation_maintenance SET dirty=1')
        return original()
    return False
