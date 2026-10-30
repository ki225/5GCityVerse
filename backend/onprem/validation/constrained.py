"""One-use, non-admitting authority for a controlled QER fixture.

An armed record has no SMF write authority. The durable quarantine reference
is installed before a later owner may activate the guard or dispatch a write.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from collections import OrderedDict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from backend.onprem.validation import normal as common
from backend.onprem.policy.safety import policy_quarantine_clear


SCHEMA = "cityverse.scenario-constrained-fixture/v1"
PURPOSE = "controlled_qer_fixture"
PROFILE = "controlled_qer_probe_v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_ACTION = re.compile(r"(?:[0-9a-f]{64}|group-qer-[0-9a-f]{32})\Z")


class ConstrainedFixtureBlocked(ValueError):
    pass


class _VerifiedReplayCache:
    """Bounded pure archive projections; fresh bytes still fence every lookup."""
    def __init__(self):
        self._lock = Lock()
        self._keys = OrderedDict()

    def contains(self, key):
        with self._lock:
            if key not in self._keys:
                return False
            self._keys.move_to_end(key)
            return True

    def control(self, key):
        with self._lock:
            if key not in self._keys:
                return None
            self._keys.move_to_end(key)
            return deepcopy(self._keys[key])

    def remember(self, key, control):
        with self._lock:
            self._keys[key] = deepcopy(control)
            self._keys.move_to_end(key)
            while len(self._keys) > 16:
                self._keys.popitem(last=False)


_VERIFIED_REPLAYS = _VerifiedReplayCache()


def _require_registered_guard(record, journal):
    registry = _registry_path(record['manifest']['quarantineRegistry'])
    if {'journal': str(journal), 'actionId': record['guardId']} not in _read_registry(registry)['actions']:
        raise ConstrainedFixtureBlocked('Controlled fixture guard is not registered')


def fixture_path(journal_path):
    source = Path(journal_path)
    if not source.is_absolute() or source.suffix not in {".db", ".sqlite3"}:
        raise ConstrainedFixtureBlocked("Absolute QER journal path required")
    return source.with_name(source.stem + ".constrained-fixtures.db")


def probe_workload_contract_digest(events, probe_kbps=100):
    if not common._events(events) or type(probe_kbps) is not int or probe_kbps != 100:
        raise ConstrainedFixtureBlocked("Controlled 1..100 UE probe contract is invalid")
    return common._digest({"profileId": PROFILE, "probeKbpsPerDirection": probe_kbps,
                           "budgetKbpsPerDirection": 10000, "events": events})


def _database(path):
    db = sqlite3.connect(common._private_database(path), timeout=2)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS constrained_arms "
               "(request_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS policy_actions "
               "(id TEXT PRIMARY KEY, record TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS fixture_attempts "
               "(id TEXT PRIMARY KEY, request_id TEXT NOT NULL, phase TEXT NOT NULL, "
               "supi TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(request_id,phase,supi))")
    return db


def _registry_path(path):
    path = Path(path)
    parent = path.parent
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent):
        raise ConstrainedFixtureBlocked("Quarantine registry path is unsafe")
    if os.name == "posix":
        stat = parent.stat()
        # The fixed WSL launcher permits its operator-owned private state dir.
        # Fixture SQLite itself remains root-owned under its separate /var/lib tree.
        if stat.st_mode & 0o777 != 0o700:
            raise ConstrainedFixtureBlocked("Quarantine registry parent is not private")
    if path.exists():
        common._check_file(path, mode=0o600 if os.name == "posix" else None)
        if os.name == "posix" and path.stat().st_uid != common._OWNER_UID:
            raise ConstrainedFixtureBlocked("Quarantine registry owner changed")
    return path


@contextmanager
def _registry_lock(path):
    lock = path.with_name(path.name + ".fixture.lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock, flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl
            stat = os.fstat(fd)
            if stat.st_uid != common._OWNER_UID or stat.st_mode & 0o777 != 0o600 or stat.st_nlink != 1:
                raise ConstrainedFixtureBlocked("Quarantine lock owner changed")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_registry(path):
    if not path.exists():
        return {"version": 1, "actions": []}
    data = common._read_json(path.read_bytes())
    actions = data.get("actions") if type(data) is dict else None
    if (type(data) is not dict or set(data) != {"version", "actions"} or data.get("version") != 1
            or type(actions) is not list or not actions
            or any(type(item) is not dict or set(item) != {"journal", "actionId"}
                   or type(item["journal"]) is not str or type(item["actionId"]) is not str
                   for item in actions)
            or len({(item["journal"], item["actionId"]) for item in actions}) != len(actions)):
        raise ConstrainedFixtureBlocked("Quarantine registry is invalid")
    return data


def _append_registry(path, *, journal, guard_id):
    path = _registry_path(path)
    with _registry_lock(path):
        if not policy_quarantine_clear(path):
            raise ConstrainedFixtureBlocked("Existing policy quarantine is unresolved")
        data = _read_registry(path)
        item = {"journal": str(journal), "actionId": guard_id}
        if item in data["actions"]:
            raise ConstrainedFixtureBlocked("Quarantine guard was already registered")
        data["actions"].append(item)
        raw = common._canonical(data)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise ConstrainedFixtureBlocked("Quarantine registry write was incomplete")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        if not policy_quarantine_clear(path):
            raise ConstrainedFixtureBlocked("Reserved guard registration is incomplete")


def arm_constrained(journal_path, request_id, events, config_sha256,
                    workload_contract_sha256, *, round_db, ue_db,
                    quarantine_registry, fixture_cap_kbps=50,
                    probe_kbps=100, profile_id=PROFILE):
    if (not common._uuid(request_id) or not common._events(events)
            or type(config_sha256) is not str or not _HEX.fullmatch(config_sha256)
            or type(fixture_cap_kbps) is not int or not 1 <= fixture_cap_kbps <= 99
            or type(probe_kbps) is not int or probe_kbps != 100
            or profile_id != PROFILE
            or workload_contract_sha256 != probe_workload_contract_digest(events, probe_kbps)):
        raise ConstrainedFixtureBlocked("Controlled fixture manifest is invalid")
    registry = _registry_path(quarantine_registry)
    zero = common._zero_source(round_db, ue_db)
    manifest = {"schema": SCHEMA, "purpose": PURPOSE, "track": "constrained",
                "requestId": request_id, "events": events, "configSha256": config_sha256,
                "workloadContractDigest": workload_contract_sha256,
                "profileId": profile_id, "fixtureCapKbps": fixture_cap_kbps,
                "probeKbpsPerDirection": probe_kbps, "zeroSource": zero,
                "quarantineRegistry": str(registry)}
    manifest_digest = common._digest(manifest)
    guard_id = common._digest({"requestId": request_id, "manifestDigest": manifest_digest})
    journal = fixture_path(journal_path)
    record = {"manifest": manifest, "manifestDigest": manifest_digest,
              "guardId": guard_id, "state": "armed", "writePossible": False,
              "claim": None, "ownerSnapshot": None, "originalNative": None,
              "fixtureInstalledNative": None, "innerRestoredNative": None,
              "originalRestoredNative": None, "acceptedSmfAttempts": [],
              "modelActionId": None, "modelPolicyActionIds": []}
    prior = []
    if journal.exists():
        checked = common._check_file(journal, mode=0o600 if os.name == "posix" else None)
        with closing(sqlite3.connect(checked.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            prior = list(db.execute("SELECT request_id,record FROM constrained_arms"))
        for old_request_id, old_raw in prior:
            old = read_constrained_receipt(journal_path, old_request_id)
            if old is None or old["state"] not in {"released", "unwritten", "withdrawn", "reconciled_released"} or old != common._read_json(old_raw):
                raise ConstrainedFixtureBlocked("Earlier controlled fixture is not released")
            if old['state'] in {'unwritten','reconciled_released'}:
                with closing(sqlite3.connect(Path(round_db).as_uri()+'?mode=ro', uri=True, timeout=2)) as completed_db:
                    row = completed_db.execute('SELECT snapshot,released FROM rounds WHERE round_id=?',
                                               (old['claim']['roundId'],)).fetchone()
                completed = common._read_json(row[0]) if row else {}
                if (row is None or row[1] != 1 or completed.get('stage') != 'completed'
                        or completed.get('cleanupStatus') != 'verified'
                        or completed.get('executionOutcome') not in {'failed','cancelled'}
                        or any(completed.get(k) != old['claim'][k] for k in ('roundId','requestId','generation'))):
                    raise ConstrainedFixtureBlocked('Recovered fixture lacks completed owned cleanup')
    with closing(_database(journal)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if (any(item[0] == request_id for item in prior)
                    or list(db.execute("SELECT request_id,record FROM constrained_arms")) != prior
                    or db.execute("SELECT 1 FROM policy_actions WHERE id=?", (guard_id,)).fetchone()):
                raise ConstrainedFixtureBlocked("Controlled fixture source changed or request is reused")
            db.execute("INSERT INTO constrained_arms(request_id,record) VALUES (?,?)",
                       (request_id, common._canonical(record).decode()))
            db.execute("INSERT INTO policy_actions(id,record) VALUES (?,?)",
                       (guard_id, common._canonical({"state": "rejected", "purpose": PURPOSE,
                                                    "manifestDigest": manifest_digest}).decode()))
    _append_registry(registry, journal=journal, guard_id=guard_id)
    if common._zero_source(round_db, ue_db) != zero:
        raise ConstrainedFixtureBlocked("Verified zero changed after guard registration")
    return {"schema": SCHEMA, "purpose": PURPOSE, "track": "constrained",
            "requestId": request_id, "manifestDigest": manifest_digest, "guardId": guard_id}


def _fixture_control(record):
    """Private dispatch metadata, never a native receipt or write permission."""
    return deepcopy({key: record[key] for key in
        ('manifest','manifestDigest','guardId','state','writePossible','claim')})


def read_constrained_receipt(journal_path, request_id, *, _control_only=False):
    if not common._uuid(request_id):
        raise ConstrainedFixtureBlocked("Controlled fixture request is invalid")
    journal = common._check_file(fixture_path(journal_path), mode=0o600 if os.name == "posix" else None)
    if os.name == "posix" and journal.stat().st_uid != common._OWNER_UID:
        raise ConstrainedFixtureBlocked("Controlled fixture owner changed")
    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute('BEGIN')
        try:
            raw = db.execute("SELECT record,json_extract(record,'$.guardId') FROM constrained_arms WHERE request_id=?", (request_id,)).fetchone()
        except sqlite3.Error as exc:
            raise ConstrainedFixtureBlocked('Controlled fixture archive is unreadable') from exc
        if raw is None:
            return None
        guard = db.execute("SELECT record FROM policy_actions WHERE id=?",
                           (raw[1],)).fetchone()
        attempt_rows = list(db.execute(
            "SELECT id,request_id,phase,supi,record FROM fixture_attempts WHERE request_id=? ORDER BY rowid",
            (request_id,)))
        from backend.onprem.validation.partial import compensation_rows, validate_compensations
        compensations = compensation_rows(db, request_id)
    # All authoritative bytes are read on every call. Only successful pure
    # replay is memoized; adding/changing any intent, guard or archive misses.
    fingerprint = hashlib.sha256(b'cityverse.constrained-replay/v3')
    for part in (str(journal), raw[0], guard[0] if guard else None,
                 common._canonical(attempt_rows), common._canonical(compensations)):
        data = b'0' if part is None else b'1' + (part if isinstance(part,bytes) else part.encode())
        fingerprint.update(len(data).to_bytes(8,'big'));fingerprint.update(data)
    replay_key = fingerprint.hexdigest()
    if _control_only:
        control = _VERIFIED_REPLAYS.control(replay_key)
        if control is not None:
            _require_registered_guard(control, journal)
            return control
    record = common._read_json(raw[0])
    if _VERIFIED_REPLAYS.contains(replay_key):
        _require_registered_guard(record, journal)
        return _fixture_control(record) if _control_only else record
    manifest = record.get("manifest") if type(record) is dict else None
    if (type(record) is not dict or set(record) != {"manifest", "manifestDigest", "guardId",
                                                    "state", "writePossible", "claim",
                                                    "ownerSnapshot", "originalNative",
                                                    "fixtureInstalledNative", "innerRestoredNative",
                                                    "originalRestoredNative", "acceptedSmfAttempts",
                                                    "modelActionId", "modelPolicyActionIds"}
            or type(manifest) is not dict
            or set(manifest) != {"schema", "purpose", "track", "requestId", "events",
                                 "configSha256", "workloadContractDigest", "profileId",
                                 "fixtureCapKbps", "probeKbpsPerDirection", "zeroSource",
                                 "quarantineRegistry"}
            or manifest.get("schema") != SCHEMA or manifest.get("purpose") != PURPOSE
            or manifest.get("track") != "constrained" or manifest.get("requestId") != request_id
            or not common._events(manifest.get("events"))
            or type(manifest.get("configSha256")) is not str
            or not _HEX.fullmatch(manifest["configSha256"])
            or manifest.get("workloadContractDigest") != probe_workload_contract_digest(
                manifest["events"], manifest.get("probeKbpsPerDirection"))
            or type(manifest.get("fixtureCapKbps")) is not int
            or not 1 <= manifest["fixtureCapKbps"] <= 99
            or manifest.get("profileId") != PROFILE
            or type(manifest.get("zeroSource")) is not dict
            or set(manifest["zeroSource"]) != {"roundSha256", "ueStatusSha256"}
            or any(type(item) is not str or not _HEX.fullmatch(item)
                   for item in manifest["zeroSource"].values())
            or record.get("manifestDigest") != common._digest(manifest)
            or record.get("guardId") != common._digest({"requestId": request_id,
                                                         "manifestDigest": record["manifestDigest"]})
            or record.get("state") not in {"armed", "cohort_sealed", "active", "fixture_installed",
                                             "inner_restored", "original_restored", "released", "unwritten", "withdrawn",
                                             "reconciled", "reconciled_released"}
            or record.get("writePossible") is not (record.get("state") in {"active", "fixture_installed",
                                                                     "inner_restored"})
            or type(record.get("acceptedSmfAttempts")) is not list
            or type(record.get("modelPolicyActionIds")) is not list
            or any(type(item) is not str or not _MODEL_ACTION.fullmatch(item)
                   for item in record["modelPolicyActionIds"])
            or len(record["modelPolicyActionIds"]) != len(set(record["modelPolicyActionIds"]))
            or (record.get("state") in {"armed", "withdrawn", "cohort_sealed", "active", "fixture_installed"}
                and any(record.get(key) is not None for key in
                        ("innerRestoredNative", "originalRestoredNative", "modelActionId")))
            or (record.get("state") in {"armed", "withdrawn", "cohort_sealed", "active", "fixture_installed"}
                and record["modelPolicyActionIds"] != [])
            or (record.get("state") in {"inner_restored", "original_restored", "released"}
                and record.get("modelActionId") != (
                    record["modelPolicyActionIds"][0]
                    if len(record["modelPolicyActionIds"]) == 1 else None))
            or (record.get("state") not in {"fixture_installed", "inner_restored",
                                                 "original_restored", "released"}
                and record.get("fixtureInstalledNative") is not None)
            or (record.get("state") not in {"inner_restored", "original_restored", "released"}
                and record.get("innerRestoredNative") is not None)
            or (record.get("state") not in {"original_restored", "released", "unwritten", "reconciled", "reconciled_released"}
                and record.get("originalRestoredNative") is not None)
            or (record.get("state") in {"inner_restored", "original_restored", "released"}
                and record.get("modelActionId") is not None
                and (type(record["modelActionId"]) is not str
                     or not _MODEL_ACTION.fullmatch(record["modelActionId"])))
            or guard is None
            or common._read_json(guard[0]) != {
                "state": ("released" if record["state"] in {"released", "unwritten", "reconciled_released"} else
                          "intent" if record["state"] in {"active", "fixture_installed",
                                                         "inner_restored", "original_restored", "reconciled"}
                          else "rejected"),
                "purpose": PURPOSE, "manifestDigest": record["manifestDigest"]}):
        raise ConstrainedFixtureBlocked("Controlled fixture arm is invalid")
    accepted = []
    all_attempts = []
    for attempt_id, row_request_id, row_phase, row_supi, raw_attempt in attempt_rows:
        item = common._read_json(raw_attempt)
        if (type(item) is not dict or set(item) != {"intentId", "supi", "phase", "rates",
                                                   "state", "httpStatus", "pfcpAccepted",
                                                   "observedAt", "uncertain"}
                or (item.get("intentId"), row_request_id, item.get("phase"), item.get("supi"))
                   != (attempt_id, request_id, row_phase, row_supi)
                or item.get("phase") not in {"fixture_install", "fixture_restore", "fixture_restore_repair"}
                or type(item.get("rates")) is not dict
                or set(item["rates"]) != {"ul", "dl"}
                or any(type(item["rates"][d]) is not int or not 1 <= item["rates"][d] <= 500_000
                       for d in ("ul", "dl"))
                or item.get("state") not in {"intent", "dispatched", "accepted", "unknown"}):
            raise ConstrainedFixtureBlocked("Fixture write intent archive is invalid")
        if item["state"] == "accepted":
            if (item["httpStatus"] != 200 or item["pfcpAccepted"] is not True
                    or item["uncertain"] is not False
                    or type(item["observedAt"]) is not str):
                raise ConstrainedFixtureBlocked("Fixture accepted response is invalid")
            accepted.append({key: item[key] for key in ("intentId", "supi", "phase", "rates",
                                                        "httpStatus", "pfcpAccepted",
                                                        "observedAt", "uncertain")})
        elif (item["httpStatus"] is not None or item["pfcpAccepted"] is not False
              or item["observedAt"] is not None or item["uncertain"] is not True):
            raise ConstrainedFixtureBlocked("Fixture unresolved response was upgraded")
        all_attempts.append(item)
    if accepted != record["acceptedSmfAttempts"]:
        raise ConstrainedFixtureBlocked("Fixture accepted summary differs from durable intents")
    for item in all_attempts:
        if item['phase'] == 'fixture_restore_repair':
            originals = [a for a in all_attempts if a['phase'] == 'fixture_restore' and a['supi'] == item['supi']]
            if len(originals) != 1 or originals[0]['state'] != 'unknown' or originals[0]['rates'] != item['rates']:
                raise ConstrainedFixtureBlocked('Fixture repair lacks its unchanged uncertain original')
    claim = record["claim"]
    if record["state"] in {"armed", "withdrawn"}:
        if (claim is not None or record["ownerSnapshot"] is not None or record["originalNative"] is not None
                or all_attempts):
            raise ConstrainedFixtureBlocked("Unclaimed fixture carries an owner")
    elif (type(claim) is not dict
          or set(claim) != {"schema", "purpose", "track", "requestId", "roundId",
                            "generation", "events", "manifestDigest", "configSha256",
                            "workloadContractDigest", "fleetLeaseDigest",
                            "comparisonFingerprint", "cohortSha256", "profileId",
                            "fixtureCapKbps", "probeKbpsPerDirection"}
          or any(claim.get(key) != manifest.get(key) for key in
                 ("schema", "purpose", "track", "requestId", "events", "configSha256",
                  "workloadContractDigest", "profileId", "fixtureCapKbps",
                  "probeKbpsPerDirection"))
          or claim.get("manifestDigest") != record["manifestDigest"]
          or not common._uuid(claim.get("roundId"))
          or type(claim.get("generation")) is not int or claim["generation"] <= 0
          or any(type(claim.get(key)) is not str or not _HEX.fullmatch(claim[key])
                 for key in ("fleetLeaseDigest", "comparisonFingerprint", "cohortSha256"))):
        raise ConstrainedFixtureBlocked("Controlled fixture claim is invalid")
    if record["state"] not in {"armed", "withdrawn"}:
        owner = record["ownerSnapshot"]
        if (type(owner) is not dict or set(owner) != {"requestId", "roundId", "generation", "fleetLease", "perUe"}
                or owner["requestId"] != request_id
                or owner["roundId"] != claim["roundId"]
                or owner["generation"] != claim["generation"]
                or common._digest(owner["fleetLease"]) != claim["fleetLeaseDigest"]
                or common.cohort_digest(owner["perUe"]) != claim["cohortSha256"]):
            raise ConstrainedFixtureBlocked("Controlled fixture archived owner changed")
        if record["state"] == "cohort_sealed":
            if record["originalNative"] is not None:
                raise ConstrainedFixtureBlocked("Unactivated fixture has native source")
        else:
            from backend.onprem.validation.native import FixtureNativeBlocked, validate_native_receipt, same_rule_state
            try:
                validate_native_receipt(record["originalNative"], owner, claim, "original")
                if record["state"] in {"fixture_installed", "inner_restored",
                                       "original_restored", "released"}:
                    validate_native_receipt(record["fixtureInstalledNative"], owner, claim,
                                            "fixture_installed")
                    _check_installed(record)
                if record["state"] in {"inner_restored", "original_restored", "released"}:
                    validate_native_receipt(record["innerRestoredNative"], owner, claim,
                                            "inner_restored")
                    if not same_rule_state(record["innerRestoredNative"]["native"]["perUe"],
                                           record["fixtureInstalledNative"]["native"]["perUe"]):
                        raise ConstrainedFixtureBlocked("Inner model restoration differs from fixture cap")
                if record["state"] in {"original_restored", "released", "unwritten", "reconciled", "reconciled_released"}:
                    validate_native_receipt(record["originalRestoredNative"], owner, claim,
                                            "original_restored")
                    if not same_rule_state(record["originalRestoredNative"]["native"]["perUe"],
                                           record["originalNative"]["native"]["perUe"]):
                        raise ConstrainedFixtureBlocked("Outer restoration differs from original QER")
                    before_qos = record["originalNative"]["native"]["qosBySlice"]
                    after_qos = record["originalRestoredNative"]["native"]["qosBySlice"]
                    if (set(before_qos) != set(after_qos)
                            or any(any(before_qos[name].get(key) != after_qos[name].get(key)
                                       for key in ("epoch", "enabled", "readback"))
                                   for name in before_qos)):
                        raise ConstrainedFixtureBlocked("Outer restoration changed original QoS scope")
                    if record["state"] == "unwritten":
                        if (all_attempts or record['acceptedSmfAttempts'] or record['modelPolicyActionIds']
                                or record['modelActionId'] is not None or record['fixtureInstalledNative'] is not None
                                or record['innerRestoredNative'] is not None):
                            raise ConstrainedFixtureBlocked('Unwritten fixture has a dispatch or model action')
                    elif record['state'] not in {'reconciled', 'reconciled_released'}:
                        _check_restored(record)
            except FixtureNativeBlocked as exc:
                raise ConstrainedFixtureBlocked("Fixture native archive replay failed") from exc
    if (record["state"] not in {"inner_restored", "original_restored", "released"}
            and any(item["phase"] in {"fixture_restore", "fixture_restore_repair"} for item in all_attempts)):
        raise ConstrainedFixtureBlocked("Outer restoration precedes inner native seal")
    if record["state"] in {"inner_restored", "original_restored", "released"}:
        for item in all_attempts:
            if item["phase"] in {"fixture_restore", "fixture_restore_repair"} and item["rates"] != _original_rates(
                    record, item["supi"]):
                raise ConstrainedFixtureBlocked("Outer restoration intent changed original rate")
    validate_compensations(record, compensations)
    _require_registered_guard(record, journal)
    _VERIFIED_REPLAYS.remember(replay_key, _fixture_control(record))
    return _fixture_control(record) if _control_only else record


def _check_installed(record):
    """Only the selected AMBR QER MBR may differ from each owned original."""
    from backend.onprem.validation.native import same_rule_state
    original = record["originalNative"]["native"]["perUe"]
    installed = record["fixtureInstalledNative"]["native"]["perUe"]
    cap = record["claim"]["fixtureCapKbps"]
    expected_supis = {row["supi"] for row in original}
    attempts = [item for item in record["acceptedSmfAttempts"]
                if item["phase"] == "fixture_install"]
    if (len(attempts) != len(original)
            or {item["supi"] for item in attempts} != expected_supis
            or any(item["phase"] != "fixture_install" or item["rates"] != {"ul": cap, "dl": cap}
                   for item in attempts)
            or len(original) != len(installed)):
        raise ConstrainedFixtureBlocked("Installed fixture lacks exact accepted writes")
    for before, after in zip(original, installed):
        if (before["supi"] != after["supi"] or not same_rule_state(before["pdrs"], after["pdrs"])
                or before["originalAmbrQerId"] != after["originalAmbrQerId"]
                or before["qosEnabled"] is not True or after["qosEnabled"] is not True):
            raise ConstrainedFixtureBlocked("Installed native owner or PDR changed")
        expected = common._read_json(common._canonical(before["qers"]))
        selected = next((q for q in expected if q["id"] == before["originalAmbrQerId"]), None)
        if selected is None:
            raise ConstrainedFixtureBlocked("Original AMBR QER is absent")
        selected["mbrKbps"] = {"ul": cap, "dl": cap}
        if not same_rule_state(expected, after["qers"]):
            raise ConstrainedFixtureBlocked("Installed native QER differs from exact fixture cap")


def _original_rates(record, supi):
    row = next((item for item in record["originalNative"]["native"]["perUe"]
                if item["supi"] == supi), None)
    if row is None:
        raise ConstrainedFixtureBlocked("Original QER owner is missing")
    qer = next((item for item in row["qers"]
                if item["id"] == row["originalAmbrQerId"]), None)
    if qer is None or qer.get("mbrKbps") != row["originalMbrKbps"]:
        raise ConstrainedFixtureBlocked("Original selected QER changed")
    return row["originalMbrKbps"]


def _check_restored(record):
    original = record["originalNative"]["native"]["perUe"]
    attempts = [item for item in record["acceptedSmfAttempts"]
                if item["phase"] in {"fixture_restore", "fixture_restore_repair"}]
    if (len(attempts) != len(original)
            or len({item["supi"] for item in attempts}) != len(original)
            or {item["supi"] for item in attempts} != {row["supi"] for row in original}
            or any(item["rates"] != _original_rates(record, item["supi"])
                   for item in attempts)):
        raise ConstrainedFixtureBlocked("Outer restoration lacks exact accepted original rates")


def seal_fixture_installed(journal_path, snapshot, installed_native, *,
                           expected_quarantine_registry):
    """Publish a baseline-eligible native cap seal after every owned intent."""
    request_id = snapshot.get("requestId") if type(snapshot) is dict else None
    record = read_constrained_receipt(journal_path, request_id)
    claim = constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    if record["state"] != "active":
        raise ConstrainedFixtureBlocked("Fixture installation is not active")
    from backend.onprem.validation.native import validate_native_receipt
    validate_native_receipt(installed_native, snapshot, claim, "fixture_installed")
    new = {**record, "state": "fixture_installed", "fixtureInstalledNative": installed_native}
    _check_installed(new)
    with closing(_database(fixture_path(journal_path))) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (request_id,)).fetchone()
            if old is None or common._read_json(old[0]) != record:
                raise ConstrainedFixtureBlocked("Fixture changed before native seal")
            if db.execute("UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?",
                          (common._canonical(new).decode(), request_id, old[0])).rowcount != 1:
                raise ConstrainedFixtureBlocked("Fixture native seal lost durable CAS")
    return common._digest(installed_native)


def _claim(snapshot, record, expected_config_sha256):
    manifest = record["manifest"]
    if (record["state"] != "armed" or record["claim"] is not None
            or type(expected_config_sha256) is not str
            or manifest["configSha256"] != expected_config_sha256
            or snapshot.get("requestId") != manifest["requestId"]
            or snapshot.get("strategy") != "ai"
            or snapshot.get("allocation", {}).get("baselineUes") != 0
            or snapshot.get("allocation", {}).get("events") != manifest["events"]
            or snapshot.get("allocation", {}).get("total") != sum(manifest["events"].values())
            or not isinstance(snapshot.get("perUe"), list)
            or len(snapshot["perUe"]) != sum(manifest["events"].values())
            or not common._uuid(snapshot.get("roundId"))
            or type(snapshot.get("generation")) is not int or snapshot["generation"] <= 0
            or not common.fleet_ownership_valid(snapshot)):
        raise ConstrainedFixtureBlocked("Controlled fixture cannot claim this round")
    from backend.onprem.traffic.workload import probe_workload_metadata
    rows = snapshot.get("perUe")
    cohort = common.cohort_digest(rows)
    comparison = probe_workload_metadata(rows, probe_kbps=manifest["probeKbpsPerDirection"])[
        "comparisonFingerprint"]
    if type(comparison) is not str or not _HEX.fullmatch(comparison):
        raise ConstrainedFixtureBlocked("Controlled workload fingerprint is invalid")
    return {"schema": SCHEMA, "purpose": PURPOSE, "track": "constrained",
            "requestId": manifest["requestId"], "roundId": snapshot["roundId"],
            "generation": snapshot["generation"], "events": manifest["events"],
            "manifestDigest": record["manifestDigest"],
            "configSha256": expected_config_sha256,
            "workloadContractDigest": manifest["workloadContractDigest"],
            "fleetLeaseDigest": common._digest(snapshot["fleetLease"]),
            "comparisonFingerprint": comparison, "cohortSha256": cohort,
            "profileId": PROFILE, "fixtureCapKbps": manifest["fixtureCapKbps"],
            "probeKbpsPerDirection": manifest["probeKbpsPerDirection"]}


def _prior_is_released(record, round_db):
    if record['state'] in {'released', 'withdrawn'}:
        return True
    if record['state'] not in {'unwritten', 'reconciled_released'} or round_db is None:
        return False
    with closing(sqlite3.connect(Path(round_db).resolve(strict=True).as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        row=db.execute('SELECT snapshot,released FROM rounds WHERE round_id=?',
                       (record['claim']['roundId'],)).fetchone()
    completed=common._read_json(row[0]) if row else {}
    return (row is not None and row[1]==1 and completed.get('stage')=='completed'
        and completed.get('cleanupStatus')=='verified' and completed.get('executionOutcome') in {'failed','cancelled'}
        and all(completed.get(k)==record['claim'][k] for k in ('roundId','requestId','generation')))


def claim_constrained(journal_path, snapshot, *, expected_config_sha256,
                      expected_quarantine_registry, round_db=None):
    """Bind a non-writing fixture to the exact established UE cohort."""
    path = fixture_path(journal_path)
    if not path.exists():
        return None
    request_id = snapshot.get("requestId") if type(snapshot) is dict else None
    if not common._uuid(request_id):
        raise ConstrainedFixtureBlocked("Controlled fixture request is invalid")
    record = read_constrained_receipt(journal_path, request_id)
    if record is None:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            prior_ids = [row[0] for row in db.execute("SELECT request_id FROM constrained_arms")]
        if (not prior_ids or not all(
                (prior := read_constrained_receipt(journal_path, item)) is not None
                and _prior_is_released(prior, round_db) for item in prior_ids)
                or not policy_quarantine_clear(expected_quarantine_registry)):
            raise ConstrainedFixtureBlocked("Controlled fixture arm is missing or unresolved")
        return None
    if (record["manifest"]["quarantineRegistry"]
            != str(_registry_path(expected_quarantine_registry))):
        raise ConstrainedFixtureBlocked("Controlled fixture registry differs from startup")
    claim = _claim(snapshot, record, expected_config_sha256)
    with closing(_database(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (request_id,)).fetchone()
            if old is None or common._read_json(old[0]) != record:
                raise ConstrainedFixtureBlocked("Controlled fixture changed during claim")
            owner = {key: snapshot[key] for key in
                     ("requestId", "roundId", "generation", "fleetLease", "perUe")}
            new = {**record, "state": "cohort_sealed", "claim": claim,
                   "ownerSnapshot": owner}
            if db.execute("UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?",
                          (common._canonical(new).decode(), request_id, old[0])).rowcount != 1:
                raise ConstrainedFixtureBlocked("Controlled fixture claim lost durable CAS")
    return claim


def activate_constrained(journal_path, snapshot, original_native, *, expected_quarantine_registry):
    """Atomically make the registered exact guard active before any SMF dispatch.

    Activation grants no request by itself. A separate per-SUPI intent must be
    durably committed before the provider can send a fixture HTTP request.
    """
    path = fixture_path(journal_path)
    request_id = snapshot.get("requestId") if type(snapshot) is dict else None
    record = read_constrained_receipt(journal_path, request_id)
    if record is None or record["state"] != "cohort_sealed":
        raise ConstrainedFixtureBlocked("Controlled fixture is not ready for activation")
    claim = constrained_claim_for_snapshot(
        journal_path, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    from backend.onprem.validation.native import FixtureNativeBlocked, validate_native_receipt
    try:
        validate_native_receipt(original_native, snapshot, claim, "original")
    except FixtureNativeBlocked as exc:
        raise ConstrainedFixtureBlocked("Original native source cannot activate fixture") from exc
    if any(not row["qosEnabled"] for row in original_native["native"]["perUe"]):
        raise ConstrainedFixtureBlocked("Original kernel QoS flag cannot support fixture")
    registry = _registry_path(expected_quarantine_registry)
    with _registry_lock(registry):
        if not policy_quarantine_clear(registry):
            raise ConstrainedFixtureBlocked("Another policy action is unresolved")
        if {"journal": str(path), "actionId": record["guardId"]} not in _read_registry(registry)["actions"]:
            raise ConstrainedFixtureBlocked("Controlled guard is unregistered")
        with closing(_database(path)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                                 (request_id,)).fetchone()
                guard = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                   (record["guardId"],)).fetchone()
                if (old is None or common._read_json(old[0]) != record or guard is None
                        or common._read_json(guard[0]) != {"state": "rejected", "purpose": PURPOSE,
                                                           "manifestDigest": record["manifestDigest"]}):
                    raise ConstrainedFixtureBlocked("Controlled guard changed before activation")
                new = {**record, "state": "active", "writePossible": True,
                       "originalNative": original_native}
                if db.execute("UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?",
                              (common._canonical(new).decode(), request_id, old[0])).rowcount != 1:
                    raise ConstrainedFixtureBlocked("Controlled fixture activation lost durable CAS")
                db.execute("UPDATE policy_actions SET record=? WHERE id=?",
                           (common._canonical({"state": "intent", "purpose": PURPOSE,
                                               "manifestDigest": record["manifestDigest"]}).decode(),
                            record["guardId"]))
    if policy_quarantine_clear(registry):
        raise ConstrainedFixtureBlocked("Activated fixture did not quarantine global admission")
    return {"schema": SCHEMA, "requestId": request_id, "guardId": record["guardId"],
            "state": "active", "originalNativeSha256": common._digest(original_native)}


def constrained_claim_for_snapshot(journal_path, snapshot, *, expected_quarantine_registry):
    """Read the exact sealed owner without creating or activating write authority."""
    if type(snapshot) is not dict or not common._uuid(snapshot.get("requestId")):
        raise ConstrainedFixtureBlocked("Controlled fixture snapshot is invalid")
    record = read_constrained_receipt(journal_path, snapshot["requestId"])
    return _claim_from_record(record, snapshot, expected_quarantine_registry=expected_quarantine_registry)


def _claim_from_record(record, snapshot, *, expected_quarantine_registry):
    """Check exact scope against one freshly verified coherent record."""
    if record is None or record["state"] not in {"cohort_sealed", "active", "fixture_installed",
                                                  "inner_restored", "original_restored", "released", "unwritten",
                                                  "reconciled", "reconciled_released"}:
        raise ConstrainedFixtureBlocked("Controlled fixture owner is unsealed")
    if (record["manifest"]["quarantineRegistry"]
            != str(_registry_path(expected_quarantine_registry))):
        raise ConstrainedFixtureBlocked("Controlled fixture registry differs from startup")
    claim = record["claim"]
    from backend.onprem.traffic.workload import probe_workload_metadata
    rows = snapshot.get("perUe")
    if (claim["roundId"] != snapshot.get("roundId")
            or claim["generation"] != snapshot.get("generation")
            or claim["fleetLeaseDigest"] != common._digest(snapshot.get("fleetLease"))
            or claim["cohortSha256"] != common.cohort_digest(rows)
            or claim["comparisonFingerprint"] != probe_workload_metadata(
                rows, probe_kbps=claim["probeKbpsPerDirection"])["comparisonFingerprint"]):
        raise ConstrainedFixtureBlocked("Controlled fixture owner changed")
    return claim


def owned_fixture_quarantine_clear(journal_path, snapshot, *, guard_id=None,
                                   expected_config_sha256, expected_quarantine_registry):
    """Read one exact healthy owned exception; all other registry guards remain hard gates.

    This is an internal dispatch predicate, never a replacement for global
    admission or the separate mandatory policy inventory and lease fences.
    """
    if type(snapshot) is not dict or not common._uuid(snapshot.get('requestId')):
        raise ConstrainedFixtureBlocked('Controlled fixture snapshot is invalid')
    record = read_constrained_receipt(journal_path, snapshot["requestId"], _control_only=True)
    claim = _claim_from_record(record, snapshot, expected_quarantine_registry=expected_quarantine_registry)
    if guard_id is None:
        guard_id = record['guardId']
    if (record["state"] not in {"active", "fixture_installed"} or record["writePossible"] is not True
            or record["guardId"] != guard_id
            or claim["configSha256"] != expected_config_sha256):
        raise ConstrainedFixtureBlocked("Exact active owned fixture guard is unavailable")
    path = fixture_path(journal_path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        unresolved = db.execute(
            "SELECT 1 FROM fixture_attempts WHERE request_id=? AND "
            "json_extract(record,'$.state') != 'accepted' LIMIT 1",
            (snapshot["requestId"],)).fetchone()
        from backend.onprem.validation.partial import compensation_rows
        if compensation_rows(db, snapshot['requestId']):
            raise ConstrainedFixtureBlocked('Fixture original-rate compensation has started')
    if unresolved is not None:
        raise ConstrainedFixtureBlocked("Owned fixture has an unresolved write intent")
    registry = _registry_path(expected_quarantine_registry)
    items = _read_registry(registry)["actions"]
    own = {"journal": str(fixture_path(journal_path)), "actionId": guard_id}
    if sum(item == own for item in items) != 1:
        raise ConstrainedFixtureBlocked("Owned fixture guard registry entry changed")
    foreign = []
    for item in items:
        if item == own:
            continue
        path = Path(item["journal"])
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
            raise ConstrainedFixtureBlocked("Foreign policy guard path is unsafe")
        foreign.append(item)
    from backend.onprem.policy.safety import policy_guards_clear
    if not policy_guards_clear(foreign, parse_record=common._read_json):
        raise ConstrainedFixtureBlocked("Another policy guard is unresolved or unreadable")
    return True


def begin_fixture_install_intent(journal_path, snapshot, supi, *, expected_config_sha256,
                                 expected_quarantine_registry):
    """Durably reserve one 50 kbps SUPI write; an uncertain intent is never retried.

    This function makes no network call. The trusted provider must independently
    recheck this exact row and owner immediately before its sole HTTP POST.
    """
    if type(supi) is not str or not re.fullmatch(r"imsi-[0-9]{15}", supi):
        raise ConstrainedFixtureBlocked("Fixture SUPI selector is invalid")
    record = read_constrained_receipt(journal_path, snapshot.get("requestId"))
    if record is None:
        raise ConstrainedFixtureBlocked("Fixture source is unavailable")
    owned_fixture_quarantine_clear(journal_path, snapshot, guard_id=record["guardId"],
        expected_config_sha256=expected_config_sha256,
        expected_quarantine_registry=expected_quarantine_registry)
    if supi not in {row["supi"] for row in record["ownerSnapshot"]["perUe"]}:
        raise ConstrainedFixtureBlocked("Fixture SUPI is outside the owned cohort")
    attempt_id = uuid4().hex
    attempt = {"intentId": attempt_id, "supi": supi, "phase": "fixture_install",
               "rates": {"ul": record["claim"]["fixtureCapKbps"],
                         "dl": record["claim"]["fixtureCapKbps"]},
               "state": "intent", "httpStatus": None, "pfcpAccepted": False,
               "observedAt": None, "uncertain": True}
    path = fixture_path(journal_path)
    with closing(_database(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (snapshot["requestId"],)).fetchone()
            if old is None or common._read_json(old[0]) != record:
                raise ConstrainedFixtureBlocked("Fixture owner changed before write intent")
            prior = [common._read_json(row[0]) for row in db.execute(
                "SELECT record FROM fixture_attempts WHERE request_id=?",
                (snapshot["requestId"],))]
            if any(type(item) is not dict or item.get("state") != "accepted" for item in prior):
                raise ConstrainedFixtureBlocked("Prior fixture outcome is unresolved")
            try:
                db.execute("INSERT INTO fixture_attempts VALUES (?,?,?,?,?)",
                           (attempt_id, snapshot["requestId"], "fixture_install", supi,
                            common._canonical(attempt).decode()))
            except sqlite3.IntegrityError as exc:
                raise ConstrainedFixtureBlocked("Fixture SUPI write was already attempted") from exc
    return attempt


def reserve_fixture_install_dispatch(journal_path, snapshot, intent_id, *,
                                     fresh_snapshot_reader, expected_config_sha256,
                                     expected_quarantine_registry):
    """Consume one exact durable intent before the trusted provider's sole POST.

    The reader must be the runtime's current held owner source. This function
    does not send HTTP; a crash after its commit remains non-retriable.
    """
    if (type(intent_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", intent_id)
            or not callable(fresh_snapshot_reader)):
        raise ConstrainedFixtureBlocked("Fixture dispatch source is invalid")
    current = fresh_snapshot_reader()
    if type(current) is not dict or current != snapshot:
        raise ConstrainedFixtureBlocked("Fixture dispatch owner changed")
    claim = constrained_claim_for_snapshot(
        journal_path, current, expected_quarantine_registry=expected_quarantine_registry)
    record = read_constrained_receipt(journal_path, current["requestId"])
    if (record["state"] != "active" or record["writePossible"] is not True
            or claim["configSha256"] != expected_config_sha256):
        raise ConstrainedFixtureBlocked("Fixture dispatch guard is unavailable")
    registry = _registry_path(expected_quarantine_registry)
    own = {"journal": str(fixture_path(journal_path)), "actionId": record["guardId"]}
    items = _read_registry(registry)["actions"]
    if sum(item == own for item in items) != 1:
        raise ConstrainedFixtureBlocked("Fixture dispatch guard registration changed")
    for item in items:
        if item == own:
            continue
        path = Path(item["journal"])
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
            raise ConstrainedFixtureBlocked("Foreign policy guard path is unsafe")
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                row = db.execute("SELECT record FROM policy_actions WHERE id=?",
                                 (item["actionId"],)).fetchone()
            if row is None or common._read_json(row[0]).get("state") not in {
                    "released", "rejected", "guard_rejected"}:
                raise ConstrainedFixtureBlocked("Another policy guard is unresolved")
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ConstrainedFixtureBlocked):
                raise
            raise ConstrainedFixtureBlocked("Foreign policy guard is unreadable") from exc
    path = fixture_path(journal_path)
    with closing(_database(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current_row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                                     (current["requestId"],)).fetchone()
            attempts = list(db.execute(
                "SELECT id,record FROM fixture_attempts WHERE request_id=?",
                (current["requestId"],)))
            if current_row is None or common._read_json(current_row[0]) != record:
                raise ConstrainedFixtureBlocked("Fixture owner changed before dispatch")
            selected = None
            for row_id, raw in attempts:
                item = common._read_json(raw)
                if row_id == intent_id:
                    selected = (raw, item)
                    if item.get("state") != "intent" or item.get("phase") != "fixture_install":
                        raise ConstrainedFixtureBlocked("Exact fixture intent is not reserved")
                elif item.get("state") != "accepted":
                    raise ConstrainedFixtureBlocked("Another fixture outcome is unresolved")
            if selected is None:
                raise ConstrainedFixtureBlocked("Exact fixture intent is missing")
            raw, intent = selected
            dispatched = {**intent, "state": "dispatched"}
            if db.execute("UPDATE fixture_attempts SET record=? WHERE id=? AND record=?",
                          (common._canonical(dispatched).decode(), intent_id, raw)).rowcount != 1:
                raise ConstrainedFixtureBlocked("Fixture dispatch lost durable CAS")
    return dispatched


def finish_fixture_install_attempt(journal_path, snapshot, intent_id, status, body):
    """Persist only a strictly accepted SMF response; all other results stay non-retriable."""
    if type(intent_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
        raise ConstrainedFixtureBlocked("Fixture intent identity is invalid")
    path = fixture_path(journal_path)
    request_id = snapshot.get("requestId") if type(snapshot) is dict else None
    with closing(_database(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM constrained_arms WHERE request_id=?",
                             (request_id,)).fetchone()
            intent_row = db.execute("SELECT record FROM fixture_attempts WHERE id=? AND request_id=?",
                                    (intent_id, request_id)).fetchone()
            if row is None or intent_row is None:
                raise ConstrainedFixtureBlocked("Durable fixture intent is absent")
            record, intent = common._read_json(row[0]), common._read_json(intent_row[0])
            if (record.get("state") != "active" or intent.get("state") != "dispatched"
                    or intent.get("phase") != "fixture_install"
                    or record.get("ownerSnapshot", {}).get("roundId") != snapshot.get("roundId")
                    or record.get("claim", {}).get("generation") != snapshot.get("generation")
                    or record.get("claim", {}).get("fleetLeaseDigest")
                       != common._digest(snapshot.get("fleetLease"))
                    or record.get("claim", {}).get("cohortSha256")
                       != common.cohort_digest(snapshot.get("perUe"))):
                raise ConstrainedFixtureBlocked("Fixture write intent owner changed")
            owner = next((item for item in record["ownerSnapshot"]["perUe"]
                          if item["supi"] == intent["supi"]), None)
            from backend.onprem.policy.group_qer import GroupQerAdapter
            accepted = (owner is not None and GroupQerAdapter._response(
                status, body, owner, intent["rates"]))
            if accepted:
                stamp = datetime.now(timezone.utc).isoformat()
                settled = {**intent, "state": "accepted", "httpStatus": 200,
                           "pfcpAccepted": True, "observedAt": stamp, "uncertain": False}
                summary = {key: settled[key] for key in ("intentId", "supi", "phase", "rates",
                                                          "httpStatus", "pfcpAccepted",
                                                          "observedAt", "uncertain")}
                new = {**record, "acceptedSmfAttempts": [*record["acceptedSmfAttempts"], summary]}
                db.execute("UPDATE constrained_arms SET record=? WHERE request_id=?",
                           (common._canonical(new).decode(), request_id))
            else:
                settled = {**intent, "state": "unknown"}
            db.execute("UPDATE fixture_attempts SET record=? WHERE id=?",
                       (common._canonical(settled).decode(), intent_id))
    if not accepted:
        raise ConstrainedFixtureBlocked("Fixture SMF outcome is not accepted; no resend")
    return summary


def retire_unclaimed_constrained(journal_path, snapshot, *, expected_config_sha256,
                                expected_quarantine_registry):
    """Retire a never-claimed arm after its round releases policy/traffic ownership.

    A created fleet may remain online after cancellation. This closes only an
    unused fixture reservation; it does not claim that the fleet was retired.
    """
    from backend.onprem.policy.absence import scope_for_round
    proof = snapshot.get('releaseProof') or {}
    generation = snapshot.get('generation')
    full_release = all(proof.get(k) is True for k in ('uesReleased','podsAbsent'))
    lease_release = (type(generation) is int and generation > 0
        and proof.get('generation') == generation
        and proof.get('scope') in {'pre-established-round-resources-only','round-resources-only'}
        and proof.get('ueLeaseReleased') is True and proof.get('fleetPreserved') is True)
    if (snapshot.get('stage')!='completed' or snapshot.get('cleanupStatus')!='verified'
            or snapshot.get('executionOutcome') not in {'failed','cancelled'}
            or snapshot.get('strategy')!='ai' or not (full_release or lease_release)
            or any(proof.get(k) is not True for k in ('trafficStopped','policiesAbsent','rulesRestored'))
            or (generation is None and (snapshot.get('fleetLease') is not None
                or scope_for_round(snapshot)['kind']!='not_created'))
            or (generation is not None and (type(generation) is not int or generation < 1))):
        raise ConstrainedFixtureBlocked('Unclaimed arm has no verified empty resource closure')
    record=read_constrained_receipt(journal_path,snapshot['requestId'])
    if (record is None or record['state']!='armed' or record.get('claim') is not None
            or record['manifest']['configSha256']!=expected_config_sha256
            or record['manifest']['quarantineRegistry']!=str(_registry_path(expected_quarantine_registry))
            or snapshot['allocation']['baselineUes']!=0
            or record['manifest']['events']!=snapshot['allocation']['events']):
        raise ConstrainedFixtureBlocked('Unclaimed arm owner differs')
    with closing(sqlite3.connect(Path(journal_path).as_uri()+'?mode=ro',uri=True,timeout=2)) as models:
        if models.execute('SELECT 1 FROM group_qer_actions WHERE round_id=?',(snapshot['roundId'],)).fetchone():
            raise ConstrainedFixtureBlocked('Unclaimed arm has model policies')
    with closing(_database(fixture_path(journal_path))) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT record FROM constrained_arms WHERE request_id=?',(snapshot['requestId'],)).fetchone()
        if (row is None or common._read_json(row[0])!=record
                or db.execute('SELECT 1 FROM fixture_attempts WHERE request_id=?',(snapshot['requestId'],)).fetchone()):
            raise ConstrainedFixtureBlocked('Unclaimed arm changed before retirement')
        db.execute('UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?',
            (common._canonical({**record,'state':'withdrawn'}).decode(),snapshot['requestId'],row[0]))
    return {'schema':'cityverse.constrained-unclaimed-retirement/v1','requestId':snapshot['requestId'],
            'roundId':snapshot['roundId'],'roundSha256':common._digest(snapshot),'state':'withdrawn'}


def withdraw_unadmitted_constrained(journal_path, request_id, *, round_db, ue_db):
    """Withdraw a never-claimed, never-dispatched arm while durable fleet is zero."""
    record = read_constrained_receipt(journal_path, request_id)
    if record is None or record['state'] != 'armed' or record['claim'] is not None:
        raise ConstrainedFixtureBlocked('Fixture is not an unused arm')
    zero = common._zero_source(round_db, ue_db)
    rounds = common._check_file(Path(round_db))
    path = fixture_path(journal_path)
    with closing(_database(path)) as db:
        db.execute('ATTACH DATABASE ? AS round_ledger', (str(rounds),))
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM round_ledger.rounds WHERE json_extract(snapshot,'$.requestId')=?",
                          (request_id,)).fetchone():
                raise ConstrainedFixtureBlocked('Admitted fixture requires owned cleanup')
            row=db.execute('SELECT record FROM constrained_arms WHERE request_id=?',(request_id,)).fetchone()
            if (row is None or common._read_json(row[0]) != record
                    or db.execute('SELECT 1 FROM fixture_attempts WHERE request_id=?',(request_id,)).fetchone()
                    or common._zero_source(round_db, ue_db) != zero):
                raise ConstrainedFixtureBlocked('Unused fixture changed before withdrawal')
            new={**record,'state':'withdrawn'}
            db.execute('UPDATE constrained_arms SET record=? WHERE request_id=? AND record=?',
                       (common._canonical(new).decode(),request_id,row[0]))
    final=read_constrained_receipt(journal_path,request_id)
    if final['state'] != 'withdrawn':
        raise ConstrainedFixtureBlocked('Unused fixture withdrawal failed replay')
    return {'schema':'cityverse.constrained-unadmitted-withdrawal/v1','requestId':request_id,
            'manifestDigest':record['manifestDigest'],'zeroSource':zero,'state':'withdrawn'}


def _cli():
    parser = argparse.ArgumentParser(description="Controlled QER fixture authority")
    sub = parser.add_subparsers(dest="command", required=True)
    arm = sub.add_parser("arm-constrained")
    for flag in ("ai100-config", "config-sha256", "round-db", "ue-db", "request-id",
                 "events-json", "workload-contract-sha256", "quarantine-registry",
                 "fixture-cap-kbps", "probe-kbps", "profile-id"):
        arm.add_argument("--" + flag, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--journal-path", type=Path, required=True)
    inspect.add_argument("--request-id", required=True)
    withdraw = sub.add_parser('withdraw-constrained')
    for flag in ('ai100-config','config-sha256','round-db','ue-db','request-id'):
        withdraw.add_argument('--'+flag,required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        result = read_constrained_receipt(args.journal_path, args.request_id)
    else:
        config_path = common._check_file(Path(args.ai100_config))
        raw = config_path.read_bytes()
        if (len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.config_sha256):
            raise ConstrainedFixtureBlocked("Pinned AI100 configuration differs")
        config = common._read_json(raw)
        if (type(config) is not dict or type(config.get("journalPath")) is not str
                or config.get("schemaVersion") not in {
                    "cityverse.adaptive-ai100-trial/v1", "cityverse.adaptive-ai100-startup/v1"}):
            raise ConstrainedFixtureBlocked("Pinned AI100 journal is invalid")
        if args.command == 'withdraw-constrained':
            result=withdraw_unadmitted_constrained(config['journalPath'],args.request_id,
                round_db=Path(args.round_db),ue_db=Path(args.ue_db))
            print(common._canonical(result).decode())
            return
        result = arm_constrained(config["journalPath"], args.request_id,
            common._read_json(args.events_json), args.config_sha256,
            args.workload_contract_sha256, round_db=Path(args.round_db),
            ue_db=Path(args.ue_db), quarantine_registry=Path(args.quarantine_registry),
            fixture_cap_kbps=int(args.fixture_cap_kbps), probe_kbps=int(args.probe_kbps),
            profile_id=args.profile_id)
    print(common._canonical(result).decode())


if __name__ == "__main__":
    _cli()
