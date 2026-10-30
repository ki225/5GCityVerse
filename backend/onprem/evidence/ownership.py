"""Read-only round and policy ownership evidence, independent of recovery generations."""
from pathlib import Path
import hashlib
import json
import sqlite3
import time
from backend.onprem.runtime.errors import EvidenceUnavailable
from backend.onprem.runtime.topology import SLICES
from backend.onprem.policy.safety import policy_inventory_clear, policy_quarantine_clear

class ReadOnlyRoundOwner:
    def __init__(self, path: Path):
        self.path = Path(path).resolve(strict=True)

    def observe(self):
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            open_count = db.execute("SELECT count(*) FROM rounds WHERE released=0").fetchone()[0]
            latest = db.execute("SELECT snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
            history = db.execute("SELECT snapshot FROM rounds ORDER BY ordinal").fetchall()
        if open_count:
            raise EvidenceUnavailable("Active scenario round or batch owns resources")
        if latest:
            snapshot = json.loads(latest[0])
            if latest[1] != 1 or snapshot.get("stage") != "completed":
                raise EvidenceUnavailable("Latest scenario round is unresolved")
        group_actions = 0
        for (raw,) in history:
            snapshot = json.loads(raw)
            actions = snapshot.get("actions")
            if not isinstance(actions, list) or any(not isinstance(a, dict) for a in actions):
                raise EvidenceUnavailable("Durable round action history incomplete")
            group_actions += sum(a.get("kind") in {"group_policy_plan", "group_policy_cleanup"}
                                 for a in actions)
        return {"available": True, "activeRound": False, "activeBatch": False,
                "observedAt": time.time(), "scope": "durable-round-ledger",
                "groupPolicyActions": group_actions}

class ReadOnlyPolicyOwner:
    """Inspect registered durable policy journals without inventing global zero."""

    def __init__(self, inventory: Path, quarantine: Path, *, clock=time.time):
        self.inventory = Path(inventory).resolve(strict=True)
        self.quarantine = Path(quarantine).resolve(strict=True)
        self.clock = clock

    def observe(self, *, round_source=None, upf_sources=None):
        if not policy_inventory_clear(self.inventory) or not policy_quarantine_clear(self.quarantine):
            raise EvidenceUnavailable("Registered policy journal or quarantine unresolved")
        manifest = json.loads(self.inventory.read_text(encoding="utf-8"))
        rows = []
        for name in manifest["journals"]:
            path = Path(name).resolve(strict=True)
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
                db.execute("PRAGMA query_only=ON")
                entries = db.execute("SELECT id,record FROM policy_actions ORDER BY id").fetchall()
            rows.append(entries)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if round_source is not None or upf_sources is not None:
            if (not isinstance(round_source, dict)
                    or round_source.get("groupPolicyActions") != 0
                    or not isinstance(upf_sources, dict) or set(upf_sources) != set(SLICES)
                    or any(row.get("qers") != 0 or row.get("available") is not True
                           for row in upf_sources.values())):
                raise EvidenceUnavailable("Durable group QER and native UPF zero proof incomplete")
        return {"available": True, "source": "registered-durable-policy-journals/v1",
                "observedAt": self.clock(), "journalCount": len(rows),
                "actionCount": sum(len(row) for row in rows), "recordsSha256": digest,
                "registeredActionsSafe": True, "groupQerInventoryComplete": False,
                "externalPolicyState": "unknown",
                "serviceGroupQerOwnerState": "unknown",
                "roundGroupActions": round_source["groupPolicyActions"] if round_source else None,
                "nativeQers": sum(row["qers"] for row in upf_sources.values()) if upf_sources else None,
                "liveRuntimeProvenance": "unavailable"}
