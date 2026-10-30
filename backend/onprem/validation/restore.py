"""One-use restoration for the controlled QER fixture.

The model action is reconciled first. Each outer SMF write then gets its own
durable intent; an ambiguous response is terminal and never sent again.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sqlite3
from uuid import uuid4

from backend.onprem.validation import normal as common
from backend.onprem.validation import constrained as state
from backend.onprem.validation.native import validate_native_receipt, same_rule_state


@dataclass(frozen=True)
class _RestorePin:
    request_id: str
    static_sha256: str
    claim_sha256: str


def _static_sha(record):
    return common._digest({key: value for key, value in record.items()
                           if key != "acceptedSmfAttempts"})


def create_restore_pin(journal_path, snapshot, *, expected_quarantine_registry):
    """Deep replay once before the bounded per-UE batch begins."""
    record = state.read_constrained_receipt(journal_path, snapshot.get("requestId"))
    claim = state.constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    if record["state"] != "inner_restored":
        raise state.ConstrainedFixtureBlocked("Original-rate batch is not ready")
    return _RestorePin(snapshot["requestId"], _static_sha(record), common._digest(claim))


def _compact_record(journal_path, snapshot, pin):
    """Recheck private bytes and exact owner without replaying 100 native rows."""
    if type(pin) is not _RestorePin or pin.request_id != snapshot.get("requestId"):
        raise state.ConstrainedFixtureBlocked("Restore batch pin changed")
    path = common._check_file(state.fixture_path(journal_path),
                              mode=0o600 if state.os.name == "posix" else None)
    if state.os.name == "posix" and path.stat().st_uid != common._OWNER_UID:
        raise state.ConstrainedFixtureBlocked("Restore batch owner changed")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                         (pin.request_id,)).fetchone()
        guard = db.execute("SELECT record FROM policy_actions WHERE id=(SELECT json_extract(record,'$.guardId') "
                           "FROM constrained_arms WHERE request_id=?)", (pin.request_id,)).fetchone()
        attempts = list(db.execute("SELECT record FROM fixture_attempts WHERE request_id=? ORDER BY rowid",
                                   (pin.request_id,)))
    if row is None:
        raise state.ConstrainedFixtureBlocked("Restore batch record is missing")
    record = common._read_json(row[0])
    if (type(record) is not dict or record.get("state") != "inner_restored"
            or _static_sha(record) != pin.static_sha256
            or common._digest(record.get("claim")) != pin.claim_sha256
            or guard is None or common._read_json(guard[0]) != {
                "state": "intent", "purpose": state.PURPOSE,
                "manifestDigest": record["manifestDigest"]}):
        raise state.ConstrainedFixtureBlocked("Restore batch static source changed")
    claim = record["claim"]
    if (claim["roundId"] != snapshot.get("roundId")
            or claim["generation"] != snapshot.get("generation")
            or claim["fleetLeaseDigest"] != common._digest(snapshot.get("fleetLease"))
            or claim["cohortSha256"] != common.cohort_digest(snapshot.get("perUe"))):
        raise state.ConstrainedFixtureBlocked("Restore batch current owner changed")
    accepted = []
    for (raw,) in attempts:
        item = common._read_json(raw)
        if type(item) is not dict or item.get("state") not in {
                "intent", "dispatched", "accepted", "unknown"}:
            raise state.ConstrainedFixtureBlocked("Restore batch intent is malformed")
        if item["state"] == "accepted":
            accepted.append({key: item[key] for key in ("intentId", "supi", "phase", "rates",
                                                        "httpStatus", "pfcpAccepted",
                                                        "observedAt", "uncertain")})
    if accepted != record["acceptedSmfAttempts"]:
        raise state.ConstrainedFixtureBlocked("Restore batch accepted summary changed")
    return record


def _update_record(journal_path, request_id, old, new):
    path = state.fixture_path(journal_path)
    with closing(state._database(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (request_id,)).fetchone()
            if row is None or common._read_json(row[0]) != old:
                raise state.ConstrainedFixtureBlocked("Fixture restoration lost owner CAS")
            if db.execute("UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?",
                          (common._canonical(new).decode(), request_id, row[0])).rowcount != 1:
                raise state.ConstrainedFixtureBlocked("Fixture restoration lost durable CAS")


def accepted_restore_supis(journal_path, snapshot):
    """Return only fully replayed accepted restore rows for crash continuation.

    A pending, dispatched or uncertain row is terminal for this attempt. The
    caller may skip accepted SUPIs, but may never resend one of them.
    """
    record = state.read_constrained_receipt(journal_path, snapshot.get("requestId"))
    if record is None or record["state"] != "inner_restored":
        raise state.ConstrainedFixtureBlocked("Original-rate continuation has no inner seal")
    path = state.fixture_path(journal_path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        rows = list(db.execute(
            "SELECT record FROM fixture_attempts WHERE request_id=? AND phase IN ('fixture_restore','fixture_restore_repair')",
            (snapshot["requestId"],)))
    accepted = set()
    attempts = [common._read_json(raw) for (raw,) in rows]
    for item in attempts:
        if item.get('phase') == 'fixture_restore_repair':
            if item.get('state') != 'accepted':
                raise state.ConstrainedFixtureBlocked('Fixture compensation is unresolved')
            continue
        if (type(item) is not dict or not _settled_restore(item, attempts)
                or item.get("supi") in accepted):
            raise state.ConstrainedFixtureBlocked("Original-rate outcome is unresolved or repeated")
        accepted.add(item["supi"])
    if accepted != {item["supi"] for item in record["acceptedSmfAttempts"]
                    if item["phase"] in {"fixture_restore", "fixture_restore_repair"}}:
        raise state.ConstrainedFixtureBlocked("Original-rate accepted summary changed")
    return accepted


def _settled_restore(item, attempts):
    if item.get('state') == 'accepted':
        return True
    return (item.get('phase') == 'fixture_restore' and item.get('state') == 'unknown'
            and len([a for a in attempts if a.get('phase') == 'fixture_restore_repair'
                     and a.get('supi') == item.get('supi') and a.get('rates') == item.get('rates')
                     and a.get('state') == 'accepted']) == 1)


def seal_inner_restored(journal_path, snapshot, native_receipt, *,
                        expected_quarantine_registry):
    """Prove the model's own cleanup restored the installed 50 kbps fixture."""
    record = state.read_constrained_receipt(journal_path, snapshot.get("requestId"))
    claim = state.constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    if record["state"] != "fixture_installed":
        raise state.ConstrainedFixtureBlocked("Model cleanup is not ready for inner seal")
    cleanup = [item for item in snapshot.get("actions", [])
               if type(item) is dict and item.get("kind") == "adaptive_qer_cleanup"]
    if (len(cleanup) != 1 or cleanup[0].get("cleanupVerified") is not True
            or cleanup[0].get("reconciliationRequired") is not False
            or type(cleanup[0].get("policyActionIds")) is not list
            or any(type(value) is not str or not state._MODEL_ACTION.fullmatch(value)
                   for value in cleanup[0]["policyActionIds"])):
        raise state.ConstrainedFixtureBlocked("Model cleanup has no exact durable proof")
    validate_native_receipt(native_receipt, snapshot, claim, "inner_restored")
    if not same_rule_state(native_receipt["native"]["perUe"],
                           record["fixtureInstalledNative"]["native"]["perUe"]):
        raise state.ConstrainedFixtureBlocked("Model cleanup did not restore exact fixture rates")
    ids = cleanup[0]["policyActionIds"]
    new = {**record, "state": "inner_restored", "innerRestoredNative": native_receipt,
           "modelActionId": ids[0] if len(ids) == 1 else None,
           "modelPolicyActionIds": ids}
    _update_record(journal_path, snapshot["requestId"], record, new)
    return {"innerRestoredNativeSha256": common._digest(native_receipt),
            "modelInterventionCount": len(ids),
            "scopeError": ("fixture.multiple_interventions_unsupported" if len(ids) > 1 else
                           "fixture.no_intervention" if not ids else None)}


def begin_fixture_restore_intent(journal_path, snapshot, supi, *,
                                 expected_quarantine_registry, _pin=None, _repair=False):
    """Reserve the original per-UE rate, never a hardcoded nominal rate."""
    record = (_compact_record(journal_path, snapshot, _pin) if _pin is not None else
              state.read_constrained_receipt(journal_path, snapshot.get("requestId")))
    if _pin is None:
        state.constrained_claim_for_snapshot(
            journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    elif record["manifest"]["quarantineRegistry"] != str(
            state._registry_path(expected_quarantine_registry)):
        raise state.ConstrainedFixtureBlocked("Restore batch registry changed")
    if record["state"] != "inner_restored":
        raise state.ConstrainedFixtureBlocked("Inner native restoration is not sealed")
    rates = state._original_rates(record, supi)
    intent = {"intentId": uuid4().hex, "supi": supi,
              "phase": "fixture_restore_repair" if _repair else "fixture_restore",
              "rates": rates, "state": "intent", "httpStatus": None,
              "pfcpAccepted": False, "observedAt": None, "uncertain": True}
    with closing(state._database(state.fixture_path(journal_path))) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (snapshot["requestId"],)).fetchone()
            if row is None or common._read_json(row[0]) != record:
                raise state.ConstrainedFixtureBlocked("Fixture owner changed before restore intent")
            prior = [common._read_json(item[0]) for item in db.execute(
                "SELECT record FROM fixture_attempts WHERE request_id=?",
                (snapshot["requestId"],))]
            originals = [a for a in prior if a.get('phase') == 'fixture_restore' and a.get('supi') == supi]
            repair_target = originals[0] if _repair and len(originals) == 1 else None
            if _repair and (repair_target is None or repair_target.get('state') != 'unknown'
                    or repair_target.get('rates') != rates
                    or any(a.get('phase') == 'fixture_restore_repair' and a.get('supi') == supi for a in prior)):
                raise state.ConstrainedFixtureBlocked('Fixture compensation is unavailable or already attempted')
            if any(type(item) is not dict or (not _settled_restore(item, prior)
                    and item != repair_target) for item in prior):
                raise state.ConstrainedFixtureBlocked("Prior fixture outcome is unresolved")
            try:
                db.execute("INSERT INTO fixture_attempts VALUES (?,?,?,?,?)",
                           (intent["intentId"], snapshot["requestId"], intent["phase"], supi,
                            common._canonical(intent).decode()))
            except sqlite3.IntegrityError as exc:
                raise state.ConstrainedFixtureBlocked("Original rate was already attempted") from exc
    return intent


def reserve_fixture_restore_dispatch(journal_path, snapshot, intent_id, *,
                                     fresh_snapshot_reader, expected_quarantine_registry,
                                     _pin=None, _repair=False):
    """Consume one exact intent after a fresh owner and foreign-guard fence."""
    if type(intent_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
        raise state.ConstrainedFixtureBlocked("Restore intent identity is invalid")
    current = fresh_snapshot_reader()
    if current != snapshot:
        raise state.ConstrainedFixtureBlocked("Restore owner changed before dispatch")
    record = (_compact_record(journal_path, current, _pin) if _pin is not None else
              state.read_constrained_receipt(journal_path, current["requestId"]))
    if _pin is None:
        state.constrained_claim_for_snapshot(
            journal_path, current, expected_quarantine_registry=expected_quarantine_registry)
    elif record["manifest"]["quarantineRegistry"] != str(
            state._registry_path(expected_quarantine_registry)):
        raise state.ConstrainedFixtureBlocked("Restore dispatch registry changed")
    if record["state"] != "inner_restored":
        raise state.ConstrainedFixtureBlocked("Outer restoration is not active")
    # The exact owner may have one unresolved intent, but no foreign guard or
    # second unresolved fixture intent may be ignored.
    registry = state._registry_path(expected_quarantine_registry)
    own = {"journal": str(state.fixture_path(journal_path)), "actionId": record["guardId"]}
    items = state._read_registry(registry)["actions"]
    if sum(item == own for item in items) != 1:
        raise state.ConstrainedFixtureBlocked("Restore guard registry changed")
    for item in items:
        if item == own:
            continue
        path = state.Path(item["journal"])
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
            raise state.ConstrainedFixtureBlocked("Foreign policy guard path is unsafe")
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                row = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                 (item["actionId"],)).fetchone()
            if row is None or common._read_json(row[0]).get("state") not in {
                    "released", "rejected", "guard_rejected"}:
                raise state.ConstrainedFixtureBlocked("Foreign policy guard is unresolved")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, state.ConstrainedFixtureBlocked):
                raise
            raise state.ConstrainedFixtureBlocked("Foreign policy guard is unreadable") from exc
    with closing(state._database(state.fixture_path(journal_path))) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            owner_row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                                   (current["requestId"],)).fetchone()
            rows = list(db.execute("SELECT id,record FROM fixture_attempts WHERE request_id=?",
                                   (current["requestId"],)))
            if owner_row is None or common._read_json(owner_row[0]) != record:
                raise state.ConstrainedFixtureBlocked("Restore owner changed before dispatch")
            selected = None
            attempts = [common._read_json(raw) for _, raw in rows]
            target = next((a for a in attempts if a.get('intentId') == intent_id), None)
            originals = [a for a in attempts if target is not None
                         and a.get('phase') == 'fixture_restore' and a.get('supi') == target.get('supi')]
            repair_original = originals[0] if _repair and len(originals) == 1 else None
            if _repair and (repair_original is None or repair_original.get('state') != 'unknown'):
                raise state.ConstrainedFixtureBlocked('Fixture compensation original changed')
            for row_id, raw in rows:
                item = common._read_json(raw)
                if row_id == intent_id:
                    if (item.get("state") != "intent" or item.get("phase") != (
                            'fixture_restore_repair' if _repair else 'fixture_restore')
                            or item.get("rates") != state._original_rates(record, item.get("supi"))):
                        raise state.ConstrainedFixtureBlocked("Exact restore intent changed")
                    selected = (raw, item)
                elif not _settled_restore(item, attempts) and item != repair_original:
                    raise state.ConstrainedFixtureBlocked("Another fixture outcome is unresolved")
            if selected is None:
                raise state.ConstrainedFixtureBlocked("Exact restore intent is missing")
            raw, intent = selected
            dispatched = {**intent, "state": "dispatched"}
            if db.execute("UPDATE fixture_attempts SET record=? WHERE id=? AND record=?",
                          (common._canonical(dispatched).decode(), intent_id, raw)).rowcount != 1:
                raise state.ConstrainedFixtureBlocked("Restore dispatch lost durable CAS")
    return dispatched


def finish_fixture_restore_attempt(journal_path, snapshot, intent_id, status, body, *, _repair=False):
    """A rejected, timed-out or ambiguous result remains one-shot unknown."""
    with closing(state._database(state.fixture_path(journal_path))) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (snapshot.get("requestId"),)).fetchone()
            attempt_row = db.execute("SELECT record FROM fixture_attempts WHERE id=? AND request_id=?",
                                     (intent_id, snapshot.get("requestId"))).fetchone()
            if row is None or attempt_row is None:
                raise state.ConstrainedFixtureBlocked("Restore intent is absent")
            record, intent = common._read_json(row[0]), common._read_json(attempt_row[0])
            if (record.get("state") != "inner_restored" or intent.get("state") != "dispatched"
                    or intent.get("phase") != ('fixture_restore_repair' if _repair else 'fixture_restore')
                    or record.get("claim", {}).get("roundId") != snapshot.get("roundId")
                    or record.get("claim", {}).get("generation") != snapshot.get("generation")
                    or record.get("claim", {}).get("fleetLeaseDigest")
                       != common._digest(snapshot.get("fleetLease"))
                    or record.get("claim", {}).get("cohortSha256")
                       != common.cohort_digest(snapshot.get("perUe"))
                    or intent.get("rates") != state._original_rates(record, intent.get("supi"))):
                raise state.ConstrainedFixtureBlocked("Restore intent owner changed")
            owner = next((item for item in record["ownerSnapshot"]["perUe"]
                          if item["supi"] == intent["supi"]), None)
            from backend.onprem.policy.group_qer import GroupQerAdapter
            accepted = (owner is not None and GroupQerAdapter._response(
                status, body, owner, intent["rates"]))
            if accepted:
                settled = {**intent, "state": "accepted", "httpStatus": 200,
                           "pfcpAccepted": True,
                           "observedAt": datetime.now(timezone.utc).isoformat(),
                           "uncertain": False}
                summary = {key: settled[key] for key in ("intentId", "supi", "phase", "rates",
                                                          "httpStatus", "pfcpAccepted",
                                                          "observedAt", "uncertain")}
                new = {**record, "acceptedSmfAttempts": [*record["acceptedSmfAttempts"], summary]}
                db.execute("UPDATE constrained_arms SET record=? WHERE request_id=?",
                           (common._canonical(new).decode(), snapshot["requestId"]))
            else:
                settled = {**intent, "state": "unknown"}
            db.execute("UPDATE fixture_attempts SET record=? WHERE id=?",
                       (common._canonical(settled).decode(), intent_id))
    if not accepted:
        raise state.ConstrainedFixtureBlocked("SMF restore outcome is unknown; no resend")
    return summary


def seal_original_restored(journal_path, snapshot, native_receipt, *,
                           expected_quarantine_registry):
    """Seal exact original QER/PDR and 100 accepted original-rate responses."""
    record = state.read_constrained_receipt(journal_path, snapshot["requestId"])
    claim = state.constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    if record["state"] != "inner_restored":
        raise state.ConstrainedFixtureBlocked("Outer native restoration is not ready")
    validate_native_receipt(native_receipt, snapshot, claim, "original_restored")
    new = {**record, "state": "original_restored", "writePossible": False,
           "originalRestoredNative": native_receipt}
    if not same_rule_state(native_receipt["native"]["perUe"], record["originalNative"]["native"]["perUe"]):
        raise state.ConstrainedFixtureBlocked("Outer native restoration differs from original")
    before_qos = record["originalNative"]["native"]["qosBySlice"]
    after_qos = native_receipt["native"]["qosBySlice"]
    if (set(before_qos) != set(after_qos)
            or any(any(before_qos[name].get(key) != after_qos[name].get(key)
                       for key in ("epoch", "enabled", "readback"))
                   for name in before_qos)):
        raise state.ConstrainedFixtureBlocked("Outer native QoS scope differs from original")
    state._check_restored(new)
    _update_record(journal_path, snapshot["requestId"], record, new)
    return {"originalRestoredNativeSha256": common._digest(native_receipt)}


def restored_fixture_cleanup_clear(journal_path, snapshot, *,
                                   expected_config_sha256,
                                   expected_quarantine_registry):
    """Allow only exact owner cleanup after all original QER values are proven.

    This internal predicate does not release the global quarantine entry.
    """
    if type(snapshot) is not dict or snapshot.get("stage") != "cleaning":
        raise state.ConstrainedFixtureBlocked("Fixture cleanup stage is not owned")
    claim = state.constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    record = state.read_constrained_receipt(journal_path, snapshot["requestId"])
    if (record["state"] not in {"original_restored", "reconciled"} or record["writePossible"] is not False
            or claim["configSha256"] != expected_config_sha256):
        raise state.ConstrainedFixtureBlocked("Original native restoration is not closed")
    own = {"journal": str(state.fixture_path(journal_path)), "actionId": record["guardId"]}
    entries = state._read_registry(state._registry_path(expected_quarantine_registry))["actions"]
    if sum(entry == own for entry in entries) != 1:
        raise state.ConstrainedFixtureBlocked("Restored fixture guard entry changed")
    for entry in entries:
        if entry == own:
            continue
        path = state.Path(entry["journal"])
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
            raise state.ConstrainedFixtureBlocked("Foreign policy guard path is unsafe")
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                row = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                 (entry["actionId"],)).fetchone()
            if row is None or common._read_json(row[0]).get("state") not in {
                    "released", "rejected", "guard_rejected"}:
                raise state.ConstrainedFixtureBlocked("Foreign policy guard is unresolved")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, state.ConstrainedFixtureBlocked):
                raise
            raise state.ConstrainedFixtureBlocked("Foreign policy guard is unreadable") from exc
    return True


def original_restored_model_records(journal, snapshot, record):
    """Replay the inner restore that the later full fixture restore superseded."""
    if record['state'] != 'original_restored' or record.get('innerRestoredNative') is None:
        raise state.ConstrainedFixtureBlocked('Outer restoration has no inner proof')
    with closing(sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
        actions = [common._read_json(row[0]) for row in db.execute(
            'SELECT record FROM group_qer_actions WHERE round_id=?', (snapshot['roundId'],))]
    if sorted(a.get('id') for a in actions) != sorted(record['modelPolicyActionIds']):
        raise state.ConstrainedFixtureBlocked('Outer restoration model set changed')
    for action in actions:
        attempted = action.get('attemptedSupis')
        restores = [a for a in action.get('attempts', []) if a.get('phase') == 'restore']
        repairs = [a for a in action.get('attempts', []) if a.get('phase') == 'restore_repair']
        repair = action.get('restoreRepair')
        repaired = set()
        if repairs:
            if (not isinstance(repair, dict) or repair.get('state') != 'verified'
                    or sorted(a.get('supi') for a in repairs) != repair.get('supis')
                    or len({a.get('supi') for a in repairs}) != len(repairs)
                    or any(a.get('httpStatus') != 200 or a.get('pfcpAccepted') is not True for a in repairs)):
                raise state.ConstrainedFixtureBlocked('Inner restore compensation is unresolved')
            repaired = {a['supi'] for a in repairs}
        if (action.get('state') not in {'restored', 'quarantined'} or action.get('uncertain') is not False
                or not isinstance(attempted, list) or len(set(attempted)) != len(attempted)
                or sorted(a.get('supi') for a in restores) != sorted(attempted)
                or repaired - set(attempted)
                or any((a.get('httpStatus') != 200 or a.get('pfcpAccepted') is not True)
                       and a.get('supi') not in repaired for a in restores)):
            raise state.ConstrainedFixtureBlocked('Inner model restore is unresolved')
    return actions


def reconcile_original_restored_model(journal, snapshot, native_receipt, *,
                                     expected_config_sha256, expected_quarantine_registry):
    """Fresh original-rate proof replaces the superseded intermediate cap check.

    Performs no network writes. Keeps both the model baseline and every restore
    attempt unchanged, and records the outer receipt that supersedes that cap.
    """
    restored_fixture_cleanup_clear(journal, snapshot,
        expected_config_sha256=expected_config_sha256,
        expected_quarantine_registry=expected_quarantine_registry)
    record = state.read_constrained_receipt(journal, snapshot['requestId'])
    actions = original_restored_model_records(journal, snapshot, record)
    validate_native_receipt(native_receipt, snapshot, record['claim'], 'original_restored')
    if not same_rule_state(native_receipt['native']['perUe'], record['originalNative']['native']['perUe']):
        raise state.ConstrainedFixtureBlocked('Current original rates changed after restoration')
    for name, old in record['originalNative']['native']['qosBySlice'].items():
        current = native_receipt['native']['qosBySlice'].get(name, {})
        if any(old.get(k) != current.get(k) for k in ('epoch', 'enabled', 'readback')):
            raise state.ConstrainedFixtureBlocked('Current original QoS scope changed')
    with closing(sqlite3.connect(journal, timeout=2)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        for action in actions:
            row = db.execute('SELECT record FROM group_qer_actions WHERE id=?', (action['id'],)).fetchone()
            if row is None or common._read_json(row[0]) != action:
                raise state.ConstrainedFixtureBlocked('Superseded model restore lost CAS')
            updated = {**action, 'state': 'restored',
                       'supersededByOriginalFixtureRestore': common._digest(record['originalRestoredNative'])}
            db.execute('UPDATE group_qer_actions SET record=? WHERE id=? AND record=?',
                       (common._canonical(updated).decode(), action['id'], row[0]))
    return {'roundId': snapshot['roundId'], 'cleanupVerified': True,
            'actions': [{'id': a['id'], 'state': 'restored'} for a in actions]}


def restored_cleanup_boot_allowed(round_db, registry, config_file, config_sha256):
    """Read-only startup exception for one failed owner with fully restored native rules.

    New admission stays quarantined. This permits the backend to finish its
    normal worker/lease cleanup and only then release the original guard.
    """
    try:
        import hashlib
        from pathlib import Path
        from backend.onprem.storage.rounds import RoundLedger
        if not isinstance(config_sha256, str) or not state._HEX.fullmatch(config_sha256):
            return False
        path = common._check_file(Path(config_file), mode=0o600 if state.os.name == 'posix' else None)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != config_sha256:
            return False
        config = common._read_json(raw)
        journal = Path(config['journalPath'])
        snapshot = RoundLedger.open_existing(Path(round_db)).current()
        if (snapshot.get('stage') != 'cleaning' or snapshot.get('cleanupStatus') != 'blocked'
                or snapshot.get('executionOutcome') not in {'failed', 'cancelled'}):
            return False
        record = state.read_constrained_receipt(journal, snapshot.get('requestId'))
        if record is None or record['state'] not in {'original_restored','reconciled'}:
            return False
        cleanup = [a for a in snapshot.get('actions', []) if a.get('kind') == 'adaptive_qer_cleanup']
        if (len(cleanup) != 1 or cleanup[0].get('policyActionIds') != record['modelPolicyActionIds']):
            return False
        if record['state'] == 'reconciled':
            with closing(sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_qer_actions'").fetchone():
                    if db.execute('SELECT 1 FROM group_qer_actions WHERE round_id=? LIMIT 1',(snapshot['roundId'],)).fetchone():
                        return False
        else:
            original_restored_model_records(journal, snapshot, record)
        return restored_fixture_cleanup_clear(journal, snapshot,
            expected_config_sha256=config_sha256, expected_quarantine_registry=Path(registry))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def retire_unwritten_fixture(journal_path, snapshot, native_receipt, *,
                             expected_config_sha256, expected_quarantine_registry):
    """Close a cancelled fixture only when no durable dispatch exists and QER is unchanged."""
    if (type(snapshot) is not dict or snapshot.get('stage') != 'cleaning'
            or snapshot.get('executionOutcome') not in {'failed', 'cancelled'}):
        raise state.ConstrainedFixtureBlocked('Unwritten fixture is not cancelled cleanup')
    claim = state.constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    record = state.read_constrained_receipt(journal_path, snapshot['requestId'])
    cleanups = [a for a in snapshot.get('actions', []) if a.get('kind') == 'adaptive_qer_cleanup']
    if (record['state'] != 'active' or claim['configSha256'] != expected_config_sha256
            or record['acceptedSmfAttempts'] or record['modelPolicyActionIds']
            or len(cleanups) != 1 or cleanups[0].get('cleanupVerified') is not True
            or cleanups[0].get('reconciliationRequired') is not False
            or cleanups[0].get('policyActionIds') != []):
        raise state.ConstrainedFixtureBlocked('Unwritten fixture has policy obligations')
    validate_native_receipt(native_receipt, snapshot, claim, 'original_restored')
    if not same_rule_state(native_receipt['native']['perUe'], record['originalNative']['native']['perUe']):
        raise state.ConstrainedFixtureBlocked('Unwritten fixture QER changed')
    before = record['originalNative']['native']['qosBySlice']
    after = native_receipt['native']['qosBySlice']
    if (set(before) != set(after) or any(any(before[s].get(k) != after[s].get(k)
            for k in ('epoch', 'enabled', 'readback')) for s in before)):
        raise state.ConstrainedFixtureBlocked('Unwritten fixture QoS scope changed')
    registry = state._registry_path(expected_quarantine_registry)
    path = state.fixture_path(journal_path)
    own = {'journal': str(path), 'actionId': record['guardId']}
    with state._registry_lock(registry):
        if sum(item == own for item in state._read_registry(registry)['actions']) != 1:
            raise state.ConstrainedFixtureBlocked('Unwritten fixture registry changed')
        with closing(state._database(path)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT record FROM constrained_arms WHERE request_id=?',
                                 (snapshot['requestId'],)).fetchone()
                attempts = db.execute('SELECT 1 FROM fixture_attempts WHERE request_id=? LIMIT 1',
                                      (snapshot['requestId'],)).fetchone()
                guard = db.execute('SELECT record FROM policy_actions WHERE id=?', (record['guardId'],)).fetchone()
                if (old is None or common._read_json(old[0]) != record or attempts is not None
                        or guard is None or common._read_json(guard[0]) != {
                            'state': 'intent', 'purpose': state.PURPOSE, 'manifestDigest': record['manifestDigest']}):
                    raise state.ConstrainedFixtureBlocked('Unwritten fixture has a dispatch or changed owner')
                updated = {**record, 'state': 'unwritten', 'writePossible': False,
                           'originalRestoredNative': native_receipt}
                db.execute('UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?',
                           (common._canonical(updated).decode(), snapshot['requestId'], old[0]))
                db.execute('UPDATE policy_actions SET record=? WHERE id=?', (common._canonical({
                    'state': 'released', 'purpose': state.PURPOSE, 'manifestDigest': record['manifestDigest']}).decode(), record['guardId']))
    final = state.read_constrained_receipt(journal_path, snapshot['requestId'])
    if final['state'] != 'unwritten':
        raise state.ConstrainedFixtureBlocked('Unwritten fixture failed replay')
    return {'claim': claim, 'unwritten': True, 'originalUnchangedNativeSha256': common._digest(native_receipt)}


def release_restored_fixture(journal_path, completed_snapshot, *,
                             expected_config_sha256,
                             expected_quarantine_registry):
    """Release only the already restored guard after official round cleanup."""
    if (type(completed_snapshot) is not dict
            or completed_snapshot.get("stage") != "completed"
            or completed_snapshot.get("cleanupStatus") != "verified"):
        raise state.ConstrainedFixtureBlocked("Round cleanup is not officially verified")
    claim = state.constrained_claim_for_snapshot(
        journal_path, completed_snapshot,
        expected_quarantine_registry=expected_quarantine_registry)
    record = state.read_constrained_receipt(journal_path, completed_snapshot["requestId"])
    if record['state'] == 'unwritten' and claim['configSha256'] == expected_config_sha256:
        return {'schema': state.SCHEMA, 'requestId': completed_snapshot['requestId'],
                'state': 'unwritten', 'manifestDigest': record['manifestDigest'],
                'originalUnchangedNativeSha256': common._digest(record['originalRestoredNative'])}
    if (record["state"] not in {"original_restored", "released", "reconciled", "reconciled_released"}
            or record["writePossible"] is not False
            or claim["configSha256"] != expected_config_sha256):
        raise state.ConstrainedFixtureBlocked("Original QER restoration is not complete")
    registry = state._registry_path(expected_quarantine_registry)
    path = state.fixture_path(journal_path)
    own = {"journal": str(path), "actionId": record["guardId"]}
    with state._registry_lock(registry):
        entries = state._read_registry(registry)["actions"]
        if sum(entry == own for entry in entries) != 1:
            raise state.ConstrainedFixtureBlocked("Restored guard registry changed")
        released_state = 'reconciled_released' if record['state'] in {'reconciled','reconciled_released'} else 'released'
        if record["state"] in {"released", "reconciled_released"}:
            with closing(state._database(path)) as db:
                guard = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                   (record["guardId"],)).fetchone()
            if (guard is None or common._read_json(guard[0]) != {
                    "state": "released", "purpose": state.PURPOSE,
                    "manifestDigest": record["manifestDigest"]}):
                raise state.ConstrainedFixtureBlocked("Released guard readback changed")
            return {"schema": state.SCHEMA, "requestId": completed_snapshot["requestId"],
                    "state": released_state, "manifestDigest": record["manifestDigest"],
                    "originalRestoredNativeSha256": common._digest(record["originalRestoredNative"])}
        with closing(state._database(path)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                                 (completed_snapshot["requestId"],)).fetchone()
                guard = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                   (record["guardId"],)).fetchone()
                if (row is None or common._read_json(row[0]) != record or guard is None
                        or common._read_json(guard[0]) != {
                            "state": "intent", "purpose": state.PURPOSE,
                            "manifestDigest": record["manifestDigest"]}):
                    raise state.ConstrainedFixtureBlocked("Restored guard changed before release")
                released = {**record, "state": released_state}
                if db.execute("UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?",
                              (common._canonical(released).decode(),
                               completed_snapshot["requestId"], row[0])).rowcount != 1:
                    raise state.ConstrainedFixtureBlocked("Restored fixture release lost CAS")
                db.execute("UPDATE policy_actions SET record=? WHERE id=?",
                           (common._canonical({"state": "released", "purpose": state.PURPOSE,
                                               "manifestDigest": record["manifestDigest"]}).decode(),
                            record["guardId"]))
    final = state.read_constrained_receipt(journal_path, completed_snapshot["requestId"])
    if final["state"] != released_state:
        raise state.ConstrainedFixtureBlocked("Released fixture failed private replay")
    return {"schema": state.SCHEMA, "requestId": completed_snapshot["requestId"],
            "state": released_state, "manifestDigest": final["manifestDigest"],
            "originalRestoredNativeSha256": common._digest(final["originalRestoredNative"])}
