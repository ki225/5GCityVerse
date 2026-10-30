"""Read-only generation access: no schema writes or historical recovery assumptions."""
from pathlib import Path
import json
import sqlite3

def decode_generation(row):
    if row is None:
        return None
    return {"generation": row[0], "desired": json.loads(row[1]), "allocation": json.loads(row[2]),
            "created": row[3], "result": json.loads(row[4]),
            "lastEstablishedAt": row[5] if len(row) > 5 else None}

class ReadOnlyGenerationStore:
    """Inspect an existing UE ledger without creating or migrating it."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve(strict=True)

    def _read(self, sql):
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute(sql).fetchone()

    def current(self):
        row = self._read("SELECT g.*, w.last_established FROM generations g "
                         "LEFT JOIN ue_reconciliation_watchdogs w ON w.generation_id=g.id "
                         "ORDER BY g.id DESC LIMIT 1")
        return decode_generation(row)
