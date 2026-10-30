"""Private, one-use authority for an explicit AI100 validation track.

The normal track carries no network write authority. A constrained fixture
requires a separate guarded state machine; this module does not grant it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import time
from uuid import UUID

from backend.onprem.scenarios.contracts import EVENT_SLICES, slice_counts
from backend.onprem.runtime.ue import seconds


SCHEMA = "cityverse.scenario-fixture/v1"
PURPOSE_NORMAL = "normal_no_write"
_OWNER_UID = 0
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COHORT_KEYS = ("supi", "scenario", "slice", "podUid", "ueIp", "tun",
                "sessionId", "upf", "smContextRef", "pfcpSeid", "dnn",
                "sst", "sd", "upfEpoch")


class FixtureBlocked(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def fixture_path(journal_path):
    source = Path(journal_path)
    if not source.is_absolute() or source.suffix not in {".db", ".sqlite3"}:
        raise FixtureBlocked("Absolute QER journal path required")
    return source.with_name(source.stem + ".fixtures.db")


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _events(value):
    return (type(value) is dict and set(value) == set(EVENT_SLICES)
            and all(type(count) is int and 0 <= count <= 100 for count in value.values())
            and value.get("realtime_control") == 0 and 1 <= sum(value.values()) <= 100)


def workload_contract_digest(events):
    if not _events(events):
        raise FixtureBlocked("Exact event counts totaling 1..100 UEs required")
    return _digest({"profileId": "scenario-offered-load-v1",
                    "budgetKbpsPerDirection": 10000, "events": events})


def _check_file(path, *, mode=None):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise FixtureBlocked("Private fixture source path is unsafe")
    if os.name == "posix":
        stat = path.stat()
        if stat.st_nlink != 1 or (mode is not None and stat.st_mode & 0o777 != mode):
            raise FixtureBlocked("Private fixture source mode is unsafe")
    return path


def _private_database(path):
    path = Path(path)
    parent = path.parent
    if (not parent.is_absolute() or parent.is_symlink() or parent.resolve() != parent
            or not parent.is_dir()):
        raise FixtureBlocked("Fixture journal parent is unsafe")
    if os.name == "posix":
        stat = parent.stat()
        if stat.st_uid != _OWNER_UID or stat.st_mode & 0o777 != 0o700:
            raise FixtureBlocked("Fixture journal parent is not root private")
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name == "posix":
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    _check_file(path, mode=0o600 if os.name == "posix" else None)
    if os.name == "posix" and path.stat().st_uid != _OWNER_UID:
        raise FixtureBlocked("Fixture journal is not root owned")
    return path


def _db(path):
    db = sqlite3.connect(_private_database(path), timeout=2)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS fixture_arms (request_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
    return db


def _read_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise FixtureBlocked("Duplicate fixture field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(FixtureBlocked("Invalid fixture number")))


def _zero_status_identity(value):
    """Exclude observation timestamps; keep generation, stability and every identity.

    Each read independently checks freshness below. A poll refreshing its time
    is not a resource change, including timestamps inside the RAN evidence.
    """
    if isinstance(value, dict):
        return {key:_zero_status_identity(item) for key,item in value.items() if key != 'observedAt'}
    if isinstance(value, list):
        return [_zero_status_identity(item) for item in value]
    return value


def _zero_source(round_db, ue_db):
    paths = (_check_file(round_db), _check_file(ue_db))
    with sqlite3.connect(paths[0].as_uri() + "?mode=ro", uri=True, timeout=2) as rounds, \
         sqlite3.connect(paths[1].as_uri() + "?mode=ro", uri=True, timeout=2) as ue:
        rounds.execute("BEGIN")
        ue.execute("BEGIN")
        latest = rounds.execute("SELECT snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
        generation = ue.execute("SELECT desired,allocation,status,id FROM generations ORDER BY id DESC LIMIT 1").fetchone()
        if not latest or latest[1] != 1 or not generation:
            raise FixtureBlocked("Verified zero is unavailable")
        snapshot = _read_json(latest[0]); desired = _read_json(generation[0])
        allocation = _read_json(generation[1]); status = _read_json(generation[2])
        if (snapshot.get("stage") != "completed" or snapshot.get("cleanupStatus") != "verified"
                or desired != {key: 0 for key in ("embb", "urllc", "mmtc", "v2x")}
                or allocation != [] or status.get("status") != "stopped"
                or status.get("requested") != 0 or status.get("allocated") != 0
                or status.get("perUe") != [] or status.get("errors") != []):
            raise FixtureBlocked("Verified zero is unavailable")
        try:
            if not -1 <= time.time() - seconds(status.get("observedAt")) <= 15:
                raise FixtureBlocked("Verified zero evidence is stale")
            if time.time() - seconds(status.get("stableSince")) < 60:
                raise FixtureBlocked("Verified zero stability is incomplete")
        except (TypeError, ValueError, KeyError):
            raise FixtureBlocked("Verified zero timing is invalid") from None
        return {"roundSha256": hashlib.sha256(latest[0].encode()).hexdigest(),
                "ueStatusSha256": _digest({'generation':generation[3], 'desired':desired,
                    'allocation':allocation, 'status':_zero_status_identity(status)})}


def arm_normal(journal_path, request_id, events, config_sha256, workload_contract_sha256,
               *, round_db, ue_db):
    if (not _uuid(request_id) or not _events(events)
            or type(config_sha256) is not str or not _HEX.fullmatch(config_sha256)
            or type(workload_contract_sha256) is not str
            or workload_contract_sha256 != workload_contract_digest(events)):
        raise FixtureBlocked("Normal fixture manifest is invalid")
    zero = _zero_source(round_db, ue_db)
    manifest = {"schema": SCHEMA, "purpose": PURPOSE_NORMAL, "track": "normal",
                "requestId": request_id, "events": events,
                "configSha256": config_sha256,
                "workloadContractDigest": workload_contract_sha256,
                "zeroSource": zero}
    manifest_digest = _digest(manifest)
    path = fixture_path(journal_path)
    with closing(_db(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            records = [_read_json(row[0]) for row in db.execute("SELECT record FROM fixture_arms")]
            if (any(record.get("state") != "released" for record in records)
                    or db.execute("SELECT 1 FROM fixture_arms WHERE request_id=?", (request_id,)).fetchone()):
                raise FixtureBlocked("Another unfinished or reused fixture record exists")
            record = {"manifest": manifest, "manifestDigest": manifest_digest,
                      "state": "armed", "claim": None}
            db.execute("INSERT INTO fixture_arms(request_id,record) VALUES (?,?)",
                       (request_id, _canonical(record).decode()))
    try:
        stable = _zero_source(round_db, ue_db) == zero
    except (FixtureBlocked, OSError, sqlite3.Error):
        stable = False
    if not stable:
        with closing(_db(path)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE fixture_arms SET record=? WHERE request_id=? AND record=?",
                           (_canonical({**record, "state": "blocked"}).decode(), request_id,
                            _canonical(record).decode()))
        raise FixtureBlocked("Verified zero changed while arming")
    return {"schema": SCHEMA, "purpose": PURPOSE_NORMAL, "track": "normal",
            "requestId": request_id, "manifestDigest": manifest_digest}


def read_normal_receipt(journal_path, request_id):
    if not _uuid(request_id):
        raise FixtureBlocked("Invalid fixture request identity")
    path = _check_file(fixture_path(journal_path), mode=0o600 if os.name == "posix" else None)
    if os.name == "posix" and (path.stat().st_uid != _OWNER_UID
                               or path.parent.stat().st_uid != _OWNER_UID
                               or path.parent.stat().st_mode & 0o777 != 0o700):
        raise FixtureBlocked("Fixture journal is not root private")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT record FROM fixture_arms WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    record = _read_json(row[0])
    manifest = record.get("manifest")
    if (type(record) is not dict or set(record) not in (
            {"manifest", "manifestDigest", "state", "claim"},
            {"manifest", "manifestDigest", "state", "claim", "withdrawal"},
            {"manifest", "manifestDigest", "state", "claim", "retirement"})
            or type(manifest) is not dict
            or set(manifest) != {"schema", "purpose", "track", "requestId", "events", "configSha256",
                                 "workloadContractDigest", "zeroSource"}
            or manifest.get("schema") != SCHEMA or manifest.get("purpose") != PURPOSE_NORMAL
            or manifest.get("track") != "normal" or manifest.get("requestId") != request_id
            or not _events(manifest.get("events"))
            or type(manifest.get("configSha256")) is not str
            or not _HEX.fullmatch(manifest["configSha256"])
            or manifest.get("workloadContractDigest") != workload_contract_digest(manifest["events"])
            or type(manifest.get("zeroSource")) is not dict
            or set(manifest["zeroSource"]) != {"roundSha256", "ueStatusSha256"}
            or any(type(value) is not str or not _HEX.fullmatch(value)
                   for value in manifest["zeroSource"].values())
            or record.get("manifestDigest") != _digest(manifest)
            or record.get("state") not in {"armed", "claimed", "cohort_sealed", "released", "blocked"}):
        raise FixtureBlocked("Normal fixture record is invalid")
    claim = record["claim"]
    if "retirement" in record:
        proof = record["retirement"]
        if (record["state"] != "released" or claim is not None or type(proof) is not dict
                or set(proof) != {"schema", "track", "requestId", "roundId", "generation", "manifestDigest", "roundSha256"}
                or proof.get("schema") not in {"cityverse.normal-unclaimed-retirement/v1", "cityverse.normal-unwritten-retirement/v1"}
                or proof.get("track") != "normal"
                or proof.get("requestId") != request_id or not _uuid(proof.get("roundId"))
                or (proof.get('generation') is not None if proof.get('schema') == 'cityverse.normal-unwritten-retirement/v1'
                    else type(proof.get("generation")) is not int or proof["generation"] <= 0)
                or proof.get("manifestDigest") != record["manifestDigest"]
                or type(proof.get("roundSha256")) is not str or not _HEX.fullmatch(proof["roundSha256"])):
            raise FixtureBlocked("Unclaimed fixture retirement is invalid")
        return record
    if "withdrawal" in record:
        proof = record["withdrawal"]
        if (record["state"] != "released" or claim is not None or type(proof) is not dict
                or set(proof) != {"schema", "requestId", "zeroSource"}
                or proof.get("schema") != "cityverse.normal-unadmitted-withdrawal/v1"
                or proof.get("requestId") != request_id or type(proof.get("zeroSource")) is not dict
                or set(proof["zeroSource"]) != {"roundSha256", "ueStatusSha256"}
                or any(type(value) is not str or not _HEX.fullmatch(value)
                       for value in proof["zeroSource"].values())):
            raise FixtureBlocked("Unadmitted fixture withdrawal is invalid")
        return record
    if record["state"] in {"armed", "blocked"}:
        if claim is not None:
            raise FixtureBlocked("Unclaimed normal fixture has a claim")
    elif (type(claim) is not dict
          or set(claim) != {"schema", "purpose", "track", "requestId", "roundId",
                            "generation", "events", "manifestDigest", "configSha256",
                            "workloadContractDigest", "fleetLeaseDigest",
                            "comparisonFingerprint", "cohortSha256"}
          or any(claim.get(key) != manifest.get(key) for key in
                 ("schema", "purpose", "track", "requestId", "events", "configSha256",
                  "workloadContractDigest"))
          or claim.get("manifestDigest") != record["manifestDigest"]
          or not _uuid(claim.get("roundId"))
          or type(claim.get("generation")) is not int or claim["generation"] <= 0
          or any(type(claim.get(key)) is not str or not _HEX.fullmatch(claim[key])
                 for key in ("fleetLeaseDigest", "comparisonFingerprint", "cohortSha256"))):
        raise FixtureBlocked("Normal fixture claim is invalid")
    return record


def cohort_digest(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100 or any(not isinstance(row, dict) for row in rows):
        raise FixtureBlocked("Exact owned cohort of 1..100 UEs required")
    supis = [row.get("supi") for row in rows]
    if any(not isinstance(supi, str) or not supi for supi in supis) or len(set(supis)) != len(rows):
        raise FixtureBlocked("Owned cohort identity is ambiguous")
    if any(any(key not in row or row[key] is None or row[key] == ""
                   for key in _COHORT_KEYS) for row in rows):
        raise FixtureBlocked("Owned cohort PDU identity is incomplete")
    text_keys = set(_COHORT_KEYS) - {"sst"}
    if any(any(type(row[key]) is not str or not row[key] for key in text_keys)
           or type(row["sst"]) is not int or not 1 <= row["sst"] <= 255
           for row in rows):
        raise FixtureBlocked("Owned cohort PDU identity types are invalid")
    return _digest(sorted(({key: row[key] for key in _COHORT_KEYS} for row in rows),
                          key=lambda row: row["supi"]))


_cohort_digest = cohort_digest


def normal_claim_for_snapshot(journal_path, snapshot):
    """Return an exact active no-write claim, or reject an incompatible arm."""
    if not isinstance(snapshot, dict):
        raise FixtureBlocked("Normal fixture snapshot is invalid")
    published = [item for item in snapshot.get("actions", [])
                 if isinstance(item, dict) and item.get("kind") == "scenario_validation_track"
                 and not (isinstance(item.get('fixtureClaim'), dict)
                          and item['fixtureClaim'].get('schema') == 'cityverse.scenario-constrained-fixture/v1'
                          and item['fixtureClaim'].get('purpose') == 'controlled_qer_fixture'
                          and item['fixtureClaim'].get('track') == 'constrained')]
    path = fixture_path(journal_path)
    if not path.exists():
        if published:
            raise FixtureBlocked("Published normal fixture source is missing")
        return None
    request_id = snapshot.get("requestId")
    if not _uuid(request_id):
        raise FixtureBlocked("Normal fixture snapshot request is invalid")
    record = read_normal_receipt(journal_path, request_id)
    if record is None:
        if published:
            raise FixtureBlocked("Published normal fixture record is missing")
        with closing(sqlite3.connect(_check_file(path).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            if any(_read_json(row[0]).get("state") != "released"
                   for row in db.execute("SELECT record FROM fixture_arms")):
                raise FixtureBlocked("Another fixture arm owns admission")
        return None
    if record["state"] in {"armed", "blocked"}:
        raise FixtureBlocked("Normal fixture has no active claim")
    claim = record["claim"]
    from backend.onprem.traffic.workload import workload_metadata
    rows = snapshot.get("perUe")
    if (record["state"] != "cohort_sealed" or claim["roundId"] != snapshot.get("roundId")
            or claim["generation"] != snapshot.get("generation")
            or claim["fleetLeaseDigest"] != _digest(snapshot.get("fleetLease"))
            or claim["cohortSha256"] != cohort_digest(rows)
            or claim["comparisonFingerprint"] != workload_metadata(rows)["comparisonFingerprint"]):
        raise FixtureBlocked("Normal fixture owner or PDU identity changed")
    if published and (len(published) != 1 or published[0].get("accepted") is not True
                      or published[0].get("fixtureClaim") != claim):
        raise FixtureBlocked("Published normal fixture claim changed")
    return claim


def fleet_ownership_valid(snapshot):
    """Bind either an existing lease or this round's actual new allocation."""
    if not isinstance(snapshot, dict):
        return False
    lease = snapshot.get('fleetLease')
    if isinstance(lease, dict):
        return bool(lease)
    if 'fleetLease' not in snapshot or lease is not None:
        return False
    actions = snapshot.get('actions')
    if not isinstance(actions, list):
        return False
    allocations = [item for item in actions
                   if isinstance(item, dict) and item.get('kind') == 'ue_allocation']
    if len(allocations) != 1:
        return False
    allocation = allocations[0]
    try:
        counts = slice_counts(snapshot['allocation'])
    except (KeyError, TypeError, ValueError):
        return False
    return (allocation.get('requested') is True and allocation.get('accepted') is True
            and allocation.get('reused') is False
            and type(allocation.get('generation')) is int
            and allocation['generation'] == snapshot.get('generation')
            and type(allocation.get('counts')) is dict
            and allocation['counts'] == counts
            and all(type(value) is int for value in allocation['counts'].values()))


def _claim(snapshot, record, expected_config_sha256):
    manifest = record["manifest"]
    from backend.onprem.traffic.workload import workload_metadata
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
            or not _uuid(snapshot.get("roundId"))
            or type(snapshot.get("generation")) is not int
            or snapshot["generation"] <= 0
            or not fleet_ownership_valid(snapshot)):
        raise FixtureBlocked("Normal fixture cannot claim this round")
    rows = snapshot.get("perUe")
    cohort = _cohort_digest(rows)
    comparison = workload_metadata(rows)["comparisonFingerprint"]
    if not _HEX.fullmatch(comparison):
        raise FixtureBlocked("Normal workload fingerprint is invalid")
    return {"schema": SCHEMA, "purpose": PURPOSE_NORMAL, "track": "normal",
            "requestId": manifest["requestId"], "roundId": snapshot["roundId"],
            "generation": snapshot["generation"], "events": manifest["events"],
            "manifestDigest": record["manifestDigest"],
            "configSha256": expected_config_sha256,
            "workloadContractDigest": manifest["workloadContractDigest"],
            "fleetLeaseDigest": _digest(snapshot["fleetLease"]),
            "comparisonFingerprint": comparison, "cohortSha256": cohort}


def claim_normal(journal_path, snapshot, *, expected_config_sha256):
    """Bind a single arm to the actual established round before baseline.

    No arm means the existing legacy AI policy path. An invalid existing arm
    blocks before measurement; it never silently becomes a legacy run.
    """
    path = fixture_path(journal_path)
    if not path.exists():
        return None
    request_id = snapshot.get("requestId")
    if not _uuid(request_id):
        raise FixtureBlocked("Normal fixture request identity is invalid")
    record = read_normal_receipt(journal_path, request_id)
    if record is None:
        with closing(sqlite3.connect(_check_file(path).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            others = [_read_json(row[0]) for row in db.execute("SELECT record FROM fixture_arms")]
        if any(item.get("state") != "released" for item in others):
            raise FixtureBlocked("Another fixture arm owns admission")
        return None
    claim = _claim(snapshot, record, expected_config_sha256)
    with closing(_db(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM fixture_arms WHERE request_id=?", (request_id,)).fetchone()
            if old is None or _read_json(old[0]) != record:
                raise FixtureBlocked("Normal fixture arm changed during claim")
            new = {**record, "state": "cohort_sealed", "claim": claim}
            if db.execute("UPDATE fixture_arms SET record=? WHERE request_id=? AND record=?",
                          (_canonical(new).decode(), request_id, old[0])).rowcount != 1:
                raise FixtureBlocked("Normal fixture claim lost durable CAS")
    return claim


def complete_normal(journal_path, snapshot):
    """Retire a no-write fixture only after the owned round has verified release."""
    path = fixture_path(journal_path)
    if not path.exists():
        return None
    request_id = snapshot.get("requestId")
    record = read_normal_receipt(journal_path, request_id)
    if record is None:
        return None
    if "retirement" in record:
        proof = record["retirement"]
        if (proof["roundId"] != snapshot.get("roundId") or proof["generation"] != snapshot.get("generation")
                or proof["roundSha256"] != _digest(snapshot)):
            raise FixtureBlocked("Retired fixture round changed")
        return proof
    claim = record.get("claim")
    from backend.onprem.traffic.workload import workload_metadata
    rows = snapshot.get("perUe")
    cleanup = [item for item in snapshot.get("actions", [])
               if isinstance(item, dict) and item.get("kind") == "adaptive_qer_cleanup"]
    published = [item for item in snapshot.get("actions", [])
                 if isinstance(item, dict) and item.get("kind") == "scenario_validation_track"]
    unclaimed = record.get("state") == "armed" and claim is None
    if unclaimed:
        # A pre-AI failure has no published validation claim. Measurement
        # windows may exist, but each must have verified cleanup. Retire the
        # unused arm using its owned request and durable cleanup. Registration
        # may have failed before a complete PDU cohort existed;
        # preserve the failed/cancelled execution and create no effect receipt.
        allowed = {"ue_allocation", "execution_failure", "adaptive_qer_cleanup",
                   "traffic_window", "traffic_failure", "ue_stop"}
        if (snapshot.get("executionOutcome") not in {"failed", "cancelled"}
                or published or any(type(item) is not dict or item.get("kind") not in allowed
                                    or (item.get("kind") == "traffic_window"
                                        and item.get("cleanupVerified") is not True)
                                    or (item.get("kind") == "ue_stop" and item.get("accepted") is not True)
                                    for item in snapshot.get("actions", []))):
            raise FixtureBlocked("Unclaimed normal fixture has execution side effects")
        manifest = record["manifest"]
        allocation = snapshot.get("allocation", {})
        intents = [a for a in snapshot.get('actions', []) if a.get('kind') == 'ue_allocation']
        unwritten = (snapshot.get('generation') is None and snapshot.get('fleetLease') is None
            and snapshot.get('perUe') == [] and snapshot.get('measurements') == []
            and snapshot.get('policyRevision') == 0 and len(intents) == 1
            and intents[0].get('requested') is True
            and all(intents[0].get(k) is False for k in ('accepted', 'installed', 'effectVerified'))
            and intents[0].get('generation') is None
            and intents[0].get('reconciliation') == 'native_write_not_observed'
            and all(snapshot.get('releaseProof', {}).get(k) is True for k in
                ('trafficStopped','policiesAbsent','rulesRestored','uesReleased','podsAbsent'))
            and all(a.get('kind') in {'ue_allocation','execution_failure','adaptive_qer_cleanup'}
                    for a in snapshot.get('actions', [])))
        if (snapshot.get("requestId") != manifest["requestId"] or snapshot.get("strategy") != "ai"
                or allocation.get("baselineUes") != 0 or allocation.get("events") != manifest["events"]
                or allocation.get("total") != sum(manifest["events"].values())
                or not _uuid(snapshot.get("roundId")) or (not unwritten and
                    (type(snapshot.get("generation")) is not int or snapshot["generation"] <= 0
                     or not fleet_ownership_valid(snapshot)))):
            raise FixtureBlocked("Unclaimed fixture retirement does not own this round")
        closure = {"schema": 'cityverse.normal-unwritten-retirement/v1' if unwritten else "cityverse.normal-unclaimed-retirement/v1", "track": "normal", "requestId": request_id,
                   "roundId": snapshot["roundId"], "generation": snapshot["generation"],
                   "manifestDigest": record["manifestDigest"], "roundSha256": _digest(snapshot)}
    elif (record.get("state") not in {"cohort_sealed", "released"} or not isinstance(claim, dict)
            or snapshot.get("requestId") != claim.get("requestId")
            or snapshot.get("roundId") != claim.get("roundId")
            or snapshot.get("generation") != claim.get("generation")
            or snapshot.get("allocation", {}).get("events") != claim.get("events")
            or _digest(snapshot.get("fleetLease")) != claim.get("fleetLeaseDigest")
            or cohort_digest(rows) != claim.get("cohortSha256")
            or workload_metadata(rows)["comparisonFingerprint"] != claim.get("comparisonFingerprint")
            or (not (not published and snapshot.get('executionOutcome') in {'failed','cancelled'}
                         and snapshot.get('measurements') == []
                         and all(a.get('kind') in {'ue_allocation','execution_failure','adaptive_qer_cleanup','ue_stop'}
                                 for a in snapshot.get('actions', [])))
                and (len(published) != 1 or published[0].get("accepted") is not True
                     or published[0].get("fixtureClaim") != claim))):
        raise FixtureBlocked("Normal fixture release is not verified")
    else:
        closure = claim
    if (snapshot.get("stage") != "completed" or snapshot.get("cleanupStatus") != "verified"
            or snapshot.get("executionOutcome") not in {"passed", "failed", "cancelled"}
            or len(cleanup) != 1 or cleanup[0].get("cleanupVerified") is not True
            or cleanup[0].get("policyActionIds") != []):
        raise FixtureBlocked("Normal fixture release is not verified")
    with closing(_db(path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM fixture_arms WHERE request_id=?", (request_id,)).fetchone()
            if old is None or _read_json(old[0]) != record:
                raise FixtureBlocked("Normal fixture changed before release")
            new = {**record, "state": "released", "claim": claim}
            if unclaimed:
                new["retirement"] = closure
            if db.execute("UPDATE fixture_arms SET record=? WHERE request_id=? AND record=?",
                          (_canonical(new).decode(), request_id, old[0])).rowcount != 1:
                raise FixtureBlocked("Normal fixture release lost durable CAS")
    return closure


def withdraw_unadmitted_normal(journal_path, request_id, *, round_db, ue_db):
    """Retire only an unused arm absent from all durable round requests at zero."""
    record = read_normal_receipt(journal_path, request_id)
    if record is None or record.get("state") not in {"armed", "blocked"} or record.get("claim") is not None:
        raise FixtureBlocked("Normal fixture is not an unused arm")
    zero = _zero_source(round_db, ue_db)
    path = _check_file(round_db)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        admitted = db.execute("SELECT 1 FROM rounds WHERE json_extract(snapshot,'$.requestId')=? LIMIT 1",
                              (request_id,)).fetchone()
    if admitted is not None:
        raise FixtureBlocked("Admitted fixture requires owned round cleanup")
    if _zero_source(round_db, ue_db) != zero:
        raise FixtureBlocked("Verified zero changed before fixture withdrawal")
    proof = {"schema": "cityverse.normal-unadmitted-withdrawal/v1",
             "requestId": request_id, "zeroSource": zero}
    with closing(_db(fixture_path(journal_path))) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM fixture_arms WHERE request_id=?", (request_id,)).fetchone()
            if row is None or _read_json(row[0]) != record:
                raise FixtureBlocked("Normal fixture changed before withdrawal")
            new = {**record, "state": "released", "withdrawal": proof}
            if db.execute("UPDATE fixture_arms SET record=? WHERE request_id=? AND record=?",
                          (_canonical(new).decode(), request_id, row[0])).rowcount != 1:
                raise FixtureBlocked("Normal fixture withdrawal lost durable CAS")
    return proof


def _cli():
    parser = argparse.ArgumentParser(description="Private scenario validation fixture authority")
    sub = parser.add_subparsers(dest="command", required=True)
    arm = sub.add_parser("arm-normal")
    arm.add_argument("--ai100-config", type=Path, required=True)
    arm.add_argument("--config-sha256", required=True)
    arm.add_argument("--round-db", type=Path, required=True)
    arm.add_argument("--ue-db", type=Path, required=True)
    arm.add_argument("--request-id", required=True)
    arm.add_argument("--events-json", required=True)
    arm.add_argument("--workload-contract-sha256", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--journal-path", type=Path, required=True)
    inspect.add_argument("--request-id", required=True)
    release = sub.add_parser("release-normal")
    release.add_argument("--ai100-config", type=Path, required=True)
    release.add_argument("--config-sha256", required=True)
    release.add_argument("--round-db", type=Path, required=True)
    release.add_argument("--round-id", required=True)
    release.add_argument("--request-id", required=True)
    withdraw = sub.add_parser("withdraw-normal")
    withdraw.add_argument("--ai100-config", type=Path, required=True)
    withdraw.add_argument("--config-sha256", required=True)
    withdraw.add_argument("--round-db", type=Path, required=True)
    withdraw.add_argument("--ue-db", type=Path, required=True)
    withdraw.add_argument("--request-id", required=True)
    args = parser.parse_args()
    if args.command == "arm-normal":
        source = _check_file(args.ai100_config)
        raw = source.read_bytes()
        if (len(raw) > 65536 or not _HEX.fullmatch(args.config_sha256 or "")
                or hashlib.sha256(raw).hexdigest() != args.config_sha256):
            raise FixtureBlocked("Pinned AI100 configuration differs")
        config = _read_json(raw)
        if (type(config) is not dict or not isinstance(config.get("journalPath"), str)
                or config.get("schemaVersion") not in {"cityverse.adaptive-ai100-trial/v1",
                                                      "cityverse.adaptive-ai100-startup/v1"}):
            raise FixtureBlocked("Pinned AI100 configuration is invalid")
        value = arm_normal(config["journalPath"], args.request_id, _read_json(args.events_json),
                           args.config_sha256, args.workload_contract_sha256,
                           round_db=args.round_db, ue_db=args.ue_db)
    elif args.command in {"release-normal", "withdraw-normal"}:
        raw = _check_file(args.ai100_config).read_bytes()
        if (len(raw) > 65536 or not _HEX.fullmatch(args.config_sha256 or "")
                or hashlib.sha256(raw).hexdigest() != args.config_sha256):
            raise FixtureBlocked("Pinned AI100 configuration differs")
        config = _read_json(raw)
        if type(config) is not dict or not isinstance(config.get("journalPath"), str):
            raise FixtureBlocked("Pinned AI100 configuration is invalid")
        if args.command == "withdraw-normal":
            value = withdraw_unadmitted_normal(config["journalPath"], args.request_id,
                                               round_db=args.round_db, ue_db=args.ue_db)
            print(_canonical(value).decode())
            return
        path = _check_file(args.round_db)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT snapshot,released FROM rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
        if row is None or row[1] != 1:
            raise FixtureBlocked("Normal fixture round release is unavailable")
        snapshot = _read_json(row[0])
        if snapshot.get("roundId") != args.round_id or snapshot.get("requestId") != args.request_id:
            raise FixtureBlocked("Normal fixture release does not own the current round")
        value = complete_normal(config["journalPath"], snapshot)
    else:
        value = read_normal_receipt(args.journal_path, args.request_id)
    print(_canonical(value).decode())


if __name__ == "__main__":
    _cli()
