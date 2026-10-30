"""Transactionally derived current state. Historical evidence is never a live input.

SQL triggers cover ordinary and offline writers. Readers do not extend evidence
clocks or reuse an earlier accepted window when the newest window is incomplete.
"""
import json
from backend.onprem.scenarios.contracts import idle_snapshot


def _projection(source):
    return f"""WITH actions AS (
      SELECT key,value,json_extract(value,'$.kind') AS kind
      FROM json_each({source},'$.actions')),
    latest AS (SELECT value FROM actions WHERE kind='traffic_window' ORDER BY key DESC LIMIT 1),
    windows AS (SELECT key,value FROM actions WHERE kind='traffic_window' ORDER BY key DESC
      LIMIT coalesce((SELECT CASE WHEN json_type(value,'$.shardCount')='integer' AND json_extract(value,'$.shardCount') BETWEEN 2 AND 10
        THEN json_extract(value,'$.shardCount') ELSE 1 END FROM latest),1)),
    selected AS (SELECT key,value FROM actions WHERE
      kind IS NULL OR kind NOT IN ('traffic_window','traffic_aggregate','traffic_failure')
      OR json_extract(value,'$.id') IN (SELECT json_extract(value,'$.id') FROM windows)
      OR (kind='traffic_aggregate' AND CASE WHEN json_type(value,'$.shardActionIds')='array'
          THEN json(json_extract(value,'$.shardActionIds')) END=
          (SELECT json_group_array(json_extract(value,'$.id')) FROM (SELECT value FROM windows ORDER BY key)))
      OR (kind='traffic_failure' AND json_extract(value,'$.trafficActionId') IN
          (SELECT json_extract(value,'$.id') FROM windows))
      OR json_type(value,'$.maintenance') IS NOT NULL)
    SELECT json_set(json_remove({source},'$.actions'),'$.actions',
      json((SELECT json_group_array(json(value)) FROM (SELECT value FROM selected ORDER BY key))))"""


def _derived(source):
    return f"""CASE WHEN json_valid({source}) THEN
        CASE WHEN json_type({source},'$.actions')='array'
          AND NOT EXISTS(SELECT 1 FROM json_each({source},'$.actions') WHERE type!='object')
        THEN ({_projection(source)}) ELSE {source} END ELSE {source} END"""


_SPECS = {
    'round_live_state': '''CREATE TABLE round_live_state (
        round_id TEXT PRIMARY KEY NOT NULL, snapshot TEXT NOT NULL)''',
    'round_live_insert': f'''CREATE TRIGGER round_live_insert AFTER INSERT ON rounds BEGIN
        INSERT OR REPLACE INTO round_live_state VALUES(NEW.round_id,{_derived('NEW.snapshot')}); END''',
    'round_live_update': f'''CREATE TRIGGER round_live_update AFTER UPDATE OF snapshot,round_id ON rounds BEGIN
        DELETE FROM round_live_state WHERE round_id=OLD.round_id;
        INSERT OR REPLACE INTO round_live_state VALUES(NEW.round_id,{_derived('NEW.snapshot')}); END''',
    'round_live_delete': '''CREATE TRIGGER round_live_delete AFTER DELETE ON rounds BEGIN
        DELETE FROM round_live_state WHERE round_id=OLD.round_id; END''',
}


def _schema(db):
    return dict(db.execute('SELECT name,sql FROM sqlite_master WHERE name IN ('
                          + ','.join('?' for _ in _SPECS) + ')', tuple(_SPECS)))


def install(db):
    """One-time migration in the constructor's normal writer transaction."""
    if _schema(db):
        return
    for sql in _SPECS.values():
        db.execute(sql)
    row = db.execute('SELECT round_id,snapshot FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
    if row is not None:
        db.execute('INSERT INTO round_live_state VALUES(?1,' + _derived('?2') + ')', row)


def read(db, round_id=None, *, decode=True):
    """Return only the latest owner and selected window, never a historical round.

    Old read-only ledgers derive the same projection without creating tables.
    A missing materialized row falls back to fresh SQL derivation, not old data.
    """
    owner = db.execute('SELECT round_id FROM rounds ORDER BY ordinal DESC LIMIT 1').fetchone()
    if owner is None:
        if round_id is not None:
            raise KeyError(round_id)
        return idle_snapshot() if decode else json.dumps(idle_snapshot())
    if round_id is not None and round_id != owner[0]:
        raise KeyError(round_id)
    row = (db.execute('SELECT snapshot FROM round_live_state WHERE round_id=?', owner).fetchone()
           if _schema(db) == _SPECS else None)
    if row is None:
        raw = db.execute('SELECT snapshot FROM rounds WHERE round_id=?', owner).fetchone()[0]
        row = db.execute('SELECT ' + _derived('?1'), (raw,)).fetchone()
    if not decode:
        return row[0]
    snapshot = json.loads(row[0])
    return snapshot
