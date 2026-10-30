"""Recognize durable recovery state without reserving a numeric generation.

Ordinary ledgers never import archived recovery implementations. Existing
historical journals retain their original validation through a lazy adapter;
moving code is not permission to discard unresolved ownership records.
"""
import json

from backend.onprem.runtime.errors import EvidenceUnavailable


def assert_recovery_clear(store, db, *, require_fresh=False, require_release=False):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    markers = (store.path.with_name(store.path.name + suffix) for suffix in (
        ".host-loss-intent.json", ".unrecorded-reboot-quarantine.json"))
    legacy_state = any(path.exists() or path.is_symlink() for path in markers)
    legacy_state |= bool(tables.intersection({
        "ue_gen263_migration", "ue_host_state_loss_reconciliations", "ue_unrecorded_host_reboots",
    }))
    if "ue_exceptional_recoveries" in tables:
        legacy_state |= db.execute("SELECT 1 FROM ue_exceptional_recoveries LIMIT 1").fetchone() is not None
    latest = (db.execute("SELECT id,status FROM generations ORDER BY id DESC LIMIT 1").fetchone()
              if "generations" in tables else None)
    if latest:
        try:
            status = json.loads(latest[1])
            mode = status.get("recoveryMode")
            if mode in {"host-state-loss", "exceptional-state-loss"}:
                legacy_state = True
                table = ("ue_host_state_loss_reconciliations" if mode == "host-state-loss"
                         else "ue_exceptional_recoveries")
                if table not in tables or db.execute(
                        f"SELECT 1 FROM {table} WHERE generation_id=?", (latest[0],)).fetchone() is None:
                    raise EvidenceUnavailable("Recovery status has no matching durable recovery record")
        except (TypeError, ValueError, AttributeError):
            raise EvidenceUnavailable("Generation recovery status is malformed") from None
    if not legacy_state:
        return

    # The adapter is constructed without __init__: the caller already owns the
    # transaction and this check must not create schemas or open another writer.
    from archive.legacy.runtime.generation_store import GenerationStore as LegacyGenerationStore
    archived = LegacyGenerationStore.__new__(LegacyGenerationStore)
    archived.path = store.path
    archived._assert_gen263_migration(db)
    host_is_current = (latest is not None and "ue_host_state_loss_reconciliations" in tables
                       and db.execute("SELECT 1 FROM ue_host_state_loss_reconciliations WHERE generation_id=?",
                                      (latest[0],)).fetchone() is not None)
    if require_release or require_fresh:
        archived._assert_host_release(db, require_fresh=bool(require_fresh and host_is_current))
