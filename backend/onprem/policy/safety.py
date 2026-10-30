"""Runtime-wide fence for unresolved policy experiments and durable actions.

Registry entries are retained after release for audit. Missing/corrupt referenced
journals are not interpreted as empty policy state. No HTTP unlock is provided.
"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3


def policy_inventory_clear(inventory: Path) -> bool:
    """Mandatory scenario inventory: every action in every journal must be safe.

    Policy writers register the journal before their first intent. An existing
    empty table is valid; a missing manifest, empty inventory, or absent table is
    not evidence of a clean runtime. The older optional quarantine is separate.
    """
    try:
        data = json.loads(inventory.read_text(encoding="utf-8"))
        journals = data.get("journals")
        if data.get("version") != 1 or not isinstance(journals, list) or not journals:
            return False
        if any(not isinstance(item, str) or not Path(item).is_absolute() for item in journals):
            return False
        if len({str(Path(item).resolve()) for item in journals}) != len(journals):
            return False
        for name in journals:
            db = sqlite3.connect(Path(name).as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                rows = db.execute("SELECT id,record FROM policy_actions").fetchall()
            finally:
                db.close()
            if len({row[0] for row in rows}) != len(rows):
                return False
            for identifier, raw in rows:
                record = json.loads(raw)
                if (not isinstance(identifier, str) or not identifier or not isinstance(record, dict)
                        or record.get("state") not in {"released", "rejected", "guard_rejected"}):
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return False


def policy_guards_clear(items, *, parse_record=json.loads) -> bool:
    """Read every guard afresh using one short transaction per journal.

    Released history shares a journal. Reopening it for every historical guard
    multiplies connection work across concurrent traffic controls. No guard
    state or read authority survives this call.
    """
    try:
        journals, seen = {}, set()
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("journal"), str)
                    or not isinstance(item.get("actionId"), str) or not item["actionId"]):
                return False
            path = Path(item["journal"])
            identity = (path, item["actionId"])
            if not path.is_absolute() or identity in seen:
                return False
            seen.add(identity)
            journals.setdefault(path, []).append(item["actionId"])
        for path, identifiers in journals.items():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.execute("BEGIN")
                # One snapshot, bounded bind counts, and all exact guard bytes
                # read afresh. No result or dispatch authority survives the call.
                for offset in range(0, len(identifiers), 256):
                    selected = identifiers[offset:offset+256]
                    placeholders = ','.join('?' for _ in selected)
                    # Transfer one row across the SQLite/Python boundary. Keep
                    # record as a JSON string: json(record) would alter the
                    # original duplicate-key/NaN/custom-parser semantics.
                    envelope = db.execute('SELECT json_group_array(json_array(id,record)) '
                        'FROM policy_actions WHERE id IN ('+placeholders+')', selected).fetchone()
                    rows = json.loads(envelope[0])
                    actual = {row[0] for row in rows}
                    if len(rows) != len(selected) or actual != set(selected):
                        return False
                    for _, raw in rows:
                        if parse_record(raw).get("state") not in {"released", "rejected", "guard_rejected"}:
                            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return False


def policy_quarantine_clear(registry: Path) -> bool:
    if not registry.exists():
        return True
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("actions"), list) or not data["actions"]:
            return False
        return policy_guards_clear(data["actions"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return False
