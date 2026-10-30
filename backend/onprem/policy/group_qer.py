"""Exact-SUPI group QER installation. Installation is not a traffic-effect claim.

The injected transport and native reader are production responsibilities. This
module has no implicit HTTP client, token source, or capability fallback.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from backend.onprem.scenarios.contracts import SCENARIO_SLICES as EVENT_SLICES
from backend.onprem.policy.errors import InvalidPolicyDecision
from backend.onprem.policy.identity import lease_identity
from backend.onprem.runtime.ue import SLICES, seconds


class GroupQerBlocked(RuntimeError):
    pass


def _fresh(stamp, now):
    try:
        return -1 <= now - seconds(stamp) <= 15
    except (TypeError, ValueError, KeyError):
        return False


def _rate(value):
    return type(value) is int and 1 <= value <= 500_000


def _ambr_qer(qers):
    """Pin the sole rate-bearing QER in the deployed AMBR/default-QER topology.

    The commissioned SMF writes AMBRQerMap only. Its separate non-GBR
    default-flow QER has zero MBR and must remain unchanged. Ambiguous
    positive-rate QoS topologies require a different actuator capability.
    """
    bounded = [q for q in qers if q["mbrKbps"] != {"ul": 0, "dl": 0}]
    if (len(bounded) != 1 or set(bounded[0]["mbrKbps"]) != {"ul", "dl"}
            or not all(_rate(bounded[0]["mbrKbps"].get(d)) for d in ("ul", "dl"))):
        raise GroupQerBlocked("Native session has no unambiguous rate-bearing AMBR QER")
    return bounded[0]


class GroupQerAdapter:
    """One-at-a-time SUPI writes, durable intent, native readback and rollback.

    native_reader(snapshot) returns an independently collected inventory with
    source='native-upf-qer', observedAt and sessions. Each session carries the
    snapshot identity, qers=[{id, pdrIds, mbrKbps:{ul,dl}}]. The transport's
    request(supi, uplink_kbps, downlink_kbps) returns (HTTP status, SMF body).
    The transport owns its token and must never include it in returned data.
    """

    def __init__(self, journal_path, *, round_reader, native_reader, transport,
                 capability_reader, profiles, clock=time.time):
        self.path = Path(journal_path)
        self.round_reader = round_reader
        self.native_reader = native_reader
        self.transport = transport
        self.capability_reader = capability_reader
        self.clock = clock
        self._lock = threading.RLock()
        self._authorities = {}
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("Precommissioned QER profiles are required")
        for identifier, profile in profiles.items():
            if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier)
                    or not isinstance(profile, dict) or set(profile) != {"ul", "dl"}
                    or not all(_rate(profile[d]) for d in ("ul", "dl"))):
                raise ValueError("Invalid bounded QER profile")
        self.profiles = deepcopy(profiles)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS group_qer_actions (id TEXT PRIMARY KEY, round_id TEXT NOT NULL, record TEXT NOT NULL)")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _put(self, record):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO group_qer_actions VALUES (?, ?, ?)",
                       (record["id"], record["roundId"], json.dumps(record, sort_keys=True, allow_nan=False)))

    def _records(self, round_id):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT record FROM group_qer_actions WHERE round_id=? ORDER BY rowid", (round_id,))]

    def _fence(self, snapshot):
        return {key: deepcopy(snapshot.get(key)) for key in
                ("roundId", "generation", "sequence", "policyRevision", "fleetLease", "perUe")}

    @staticmethod
    def _owner_fence(value):
        # Measurements and cleanup advance sequence/revision after installation.
        # Restoration still requires the exact round, UE lease and native cohort.
        return {key: deepcopy(value.get(key)) for key in
                ("roundId", "generation", "fleetLease", "perUe")}

    def _current(self, fence, *, restore=False):
        current = self._fence(self.round_reader())
        if (self._owner_fence(current) != self._owner_fence(fence) if restore else current != fence):
            raise GroupQerBlocked("Round, revision, or immutable lease changed")

    def _capable(self, snapshot):
        try:
            proof = self.capability_reader(snapshot)
        except Exception:
            return False
        return (isinstance(proof, dict) and proof.get("verified") is True
                and proof.get("source") == "onprem-smf-group-qer-commissioning"
                and proof.get("scope") == "supi-only"
                and isinstance(proof.get("imageDigest"), str)
                and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", proof["imageDigest"]) is not None
                and _fresh(proof.get("observedAt"), self.clock())
                and isinstance(proof.get("profileIds"), list)
                and all(isinstance(identifier, str) for identifier in proof["profileIds"])
                and set(self.profiles) <= set(proof["profileIds"]))

    def available(self, snapshot):
        return self._capable(snapshot) and not any(
            r["state"] in {"intent", "applying", "rollback", "quarantined"}
            for r in self._records(snapshot.get("roundId", "")))

    def _inventory(self, snapshot, *, action_id=None, phase=None):
        try:
            lease_identity(snapshot)
        except InvalidPolicyDecision as exc:
            raise GroupQerBlocked("Immutable UE lease is invalid") from exc
        if action_id is not None and getattr(self.transport, "native_receipt_context", False):
            value = self.native_reader(deepcopy(snapshot), action_id=action_id, phase=phase)
        else:
            value = self.native_reader(deepcopy(snapshot))
        if (not isinstance(value, dict) or value.get("source") != "native-upf-qer"
                or not _fresh(value.get("observedAt"), self.clock())
                or not isinstance(value.get("sessions"), list)):
            raise GroupQerBlocked("Fresh independent native QER inventory is unavailable")
        rows = snapshot.get("perUe")
        if not isinstance(rows, list) or not rows:
            raise GroupQerBlocked("Round UE cohort is unavailable")
        expected = {r.get("supi"): r for r in rows if isinstance(r, dict)}
        if len(expected) != len(rows) or None in expected:
            raise GroupQerBlocked("Round cohort has missing or duplicate SUPIs")
        actual = {r.get("supi"): r for r in value["sessions"] if isinstance(r, dict)}
        if len(actual) != len(value["sessions"]) or set(actual) != set(expected):
            raise GroupQerBlocked("Native inventory has a missing or extra session")
        for supi, row in expected.items():
            native = actual[supi]
            for field in ("sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf"):
                if not row.get(field) or native.get(field) != row[field]:
                    raise GroupQerBlocked("Native session, SEID, epoch or lease mismatch")
            spec = SLICES.get(row["slice"])
            if (spec is None or row.get("sst") != spec["sst"] or row.get("sd") != spec["sd"]
                    or native.get("sst") != spec["sst"] or native.get("sd") != spec["sd"]):
                raise GroupQerBlocked("Native S-NSSAI mismatch")
            qers = native.get("qers")
            if (not isinstance(qers, list) or not qers or len(qers) != len({q.get("id") for q in qers if isinstance(q, dict)})
                    or any(not isinstance(q, dict) or type(q.get("id")) is not int
                           or not isinstance(q.get("pdrIds"), list) or not q["pdrIds"]
                           or any(type(i) is not int for i in q["pdrIds"])
                           or not isinstance(q.get("mbrKbps"), dict)
                           or set(q["mbrKbps"]) != {"ul", "dl"}
                           or any(type(q["mbrKbps"].get(d)) is not int
                                  or not 0 <= q["mbrKbps"][d] <= 500_000
                                  for d in ("ul", "dl")) for q in qers)):
                raise GroupQerBlocked("Native QER/PDR baseline is incomplete")
            _ambr_qer(qers)
        return actual

    @staticmethod
    def _shape(row):
        return {"identity": {key: row[key] for key in
                ("supi", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf", "sst", "sd")},
                "qers": sorted(deepcopy(row["qers"]), key=lambda q: q["id"])}

    def _guard_inventory(self, record, expected_rates=None, *, restore=False):
        self._current(record["fence"], restore=restore)
        actual = self._inventory(record["snapshot"], action_id=record["id"],
                                 phase="restored" if restore else "installed")
        for supi, pinned in record["baseline"].items():
            current = self._shape(actual[supi])
            if current["identity"] != pinned["identity"]:
                raise GroupQerBlocked("Pinned native session changed")
            original = pinned["qers"]
            now_qers = current["qers"]
            if [(q["id"], q["pdrIds"]) for q in now_qers] != [(q["id"], q["pdrIds"]) for q in original]:
                raise GroupQerBlocked("Native QER/PDR identity changed")
            wanted = (expected_rates if supi in record["treatmentSupis"] and expected_rates else None)
            target_id = _ambr_qer(original)["id"]
            for q, old in zip(now_qers, original):
                expected = wanted if wanted and q["id"] == target_id else old["mbrKbps"]
                if q["mbrKbps"] != expected:
                    raise GroupQerBlocked("Native QER readback differs")
        return True

    @staticmethod
    def _response(status, body, supi, profile):
        if type(status) is not int or status != 200 or not isinstance(body, dict):
            return False
        results = body.get("results")
        return (body.get("matchedSessions") == body.get("appliedSessions") == 1
                and body.get("uplinkMbrKbps") == profile["ul"]
                and body.get("downlinkMbrKbps") == profile["dl"]
                and isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict)
                and results[0].get("status") == "applied"
                and results[0].get("smContextRef") == supi["smContextRef"]
                and results[0].get("sst") == supi["sst"]
                and results[0].get("sd") == supi["sd"])

    def _request(self, record, supi, rates, phase, *, journal_phase=None):
        # Never persist the raw response: server errors can echo secrets.
        try:
            status, body = self.transport.request(supi, rates["ul"], rates["dl"],
                                                  authority=self._authorities[record["id"]], phase=phase)
            accepted = self._response(status, body, record["baseline"][supi]["identity"], rates)
        except Exception:
            status, accepted = None, False
        if phase == "apply" and status in (None, 207):
            record["uncertain"] = True
        record["attempts"].append({"supi": supi, "phase": journal_phase or phase,
                                   "httpStatus": status if type(status) is int else None,
                                   "pfcpAccepted": accepted, "observedAt": self.clock()})
        self._put(record)
        return accepted

    def _restore(self, record):
        record["state"] = "rollback"
        self._put(record)
        # Only a still-owned identity can safely receive a compensating write.
        try:
            # An interrupted apply may still hold its apply-only grant. Revoke
            # it before obtaining a separately fenced restoration authority.
            # A failed revocation must stop compensation without a new grant.
            previous = self._authorities.pop(record["id"], None)
            if previous is not None:
                self.transport.end(previous)
            self._authorities[record["id"]] = self.transport.begin_restore(record, self.round_reader)
            self._current(record["fence"], restore=True)
            actual = self._inventory(record["snapshot"], action_id=record["id"], phase="pre_restore")
            for supi in record["attemptedSupis"]:
                if self._shape(actual[supi])["identity"] != record["baseline"][supi]["identity"]:
                    raise GroupQerBlocked("Rollback owner changed")
            for supi in reversed(record["attemptedSupis"]):
                rates = _ambr_qer(record["baseline"][supi]["qers"])["mbrKbps"]
                self._request(record, supi, rates, "restore")
            self._guard_inventory(record, restore=True)
        except Exception:
            record["state"] = "quarantined"
        else:
            record["state"] = "quarantined" if record["uncertain"] else "restored"
        self._put(record)
        authority = self._authorities.pop(record["id"], None)
        if authority is not None:
            self.transport.end(authority)
        return self.view(record)

    @staticmethod
    def view(record):
        return {"id": record["id"], "roundId": record["roundId"], "eventId": record["eventId"],
                "profileId": record["profileId"], "treatmentSupis": deepcopy(record["treatmentSupis"]),
                "holdoutSupis": deepcopy(record["holdoutSupis"]), "state": record["state"],
                "installed": record["state"] == "installed", "effectVerified": False,
                "attempts": deepcopy(record["attempts"])}

    def execute(self, snapshot, *, event_id, treatment_supis, holdout_supis, profile_id,
                model_response_id=None, candidate_id=None):
        with self._lock:
            if ((model_response_id is None) != (candidate_id is None)
                    or (model_response_id is not None and
                        (not isinstance(model_response_id, str) or not model_response_id
                         or not isinstance(candidate_id, str) or not candidate_id))):
                raise GroupQerBlocked("Model decision link is incomplete")
            if not self.available(snapshot) or profile_id not in self.profiles:
                raise GroupQerBlocked("Group QER capability or candidate is unavailable")
            if (event_id not in EVENT_SLICES or not isinstance(treatment_supis, list)
                    or not isinstance(holdout_supis, list) or not treatment_supis
                    or (not holdout_supis and len(treatment_supis) != 1)
                    or any(not isinstance(s, str) or not s for s in treatment_supis + holdout_supis)
                    or len(set(treatment_supis + holdout_supis)) != len(treatment_supis + holdout_supis)):
                raise GroupQerBlocked("Invalid exact treatment/holdout partition")
            rows = snapshot.get("perUe", [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise GroupQerBlocked("Round UE cohort is invalid")
            event_rows = {r["supi"]: r for r in rows if r.get("scenario") == event_id}
            if (set(treatment_supis + holdout_supis) != set(event_rows)
                    or len(event_rows) != len(treatment_supis + holdout_supis)
                    or any(r.get("slice") != EVENT_SLICES[event_id] for r in event_rows.values())):
                raise GroupQerBlocked("Treatment and holdout do not equal the event cohort")
            fence = self._fence(snapshot)
            self._current(fence)
            digest = hashlib.sha256(json.dumps([fence, event_id, sorted(treatment_supis),
                sorted(holdout_supis), profile_id, uuid.uuid4().hex], sort_keys=True).encode()).hexdigest()
            action_id = "group-qer-" + digest[:32]
            native = self._inventory(snapshot, action_id=action_id, phase="baseline")
            baseline = {s: self._shape(row) for s, row in native.items()}
            record = {"id": action_id, "roundId": snapshot["roundId"],
                      "eventId": event_id, "profileId": profile_id,
                      "modelResponseId": model_response_id, "candidateId": candidate_id,
                      "treatmentSupis": sorted(treatment_supis), "holdoutSupis": sorted(holdout_supis),
                      "profileRates": deepcopy(self.profiles[profile_id]),
                      "fence": fence, "snapshot": deepcopy(snapshot), "baseline": baseline,
                      "attemptedSupis": [], "attempts": [], "uncertain": False,
                      "state": "intent", "createdAt": self.clock()}
            previous = self._records(snapshot["roundId"])
            if (any(r["state"] not in {"installed", "restored"} for r in previous)
                    or any(r["state"] == "installed" and
                           (r["eventId"] == event_id
                            or set(r["treatmentSupis"]) & set(treatment_supis + holdout_supis))
                           for r in previous)):
                raise GroupQerBlocked("Existing round QER action is unresolved or overlaps this event")
            for prior in previous:
                if prior["state"] == "restored":
                    self._guard_inventory(prior, restore=True)
            self._put(record)  # Durable intent precedes the first external write.
            try:
                self._authorities[record["id"]] = self.transport.begin(record, self.round_reader, self.profiles)
            except Exception:
                record["state"] = "restored"  # No external write occurred.
                self._put(record)
                raise GroupQerBlocked("Commissioned action authority is unavailable") from None
            rates = self.profiles[profile_id]
            for supi in record["treatmentSupis"]:
                try:
                    self._current(fence)
                    self._inventory(snapshot, action_id=record["id"], phase="pre_apply")
                except Exception:
                    return self._restore(record)
                record["state"] = "applying"
                record["attemptedSupis"].append(supi)
                self._put(record)  # Journal each potentially ambiguous write first.
                if not self._request(record, supi, rates, "apply"):
                    return self._restore(record)
                try:
                    # A 200 alone is not installation; independent UPF readback is required.
                    actual = self._inventory(snapshot, action_id=record["id"], phase="installed")
                    current = self._shape(actual[supi])
                    pinned = baseline[supi]
                    target_id = _ambr_qer(pinned["qers"])["id"]
                    if (current["identity"] != pinned["identity"]
                            or [(q["id"], q["pdrIds"]) for q in current["qers"]]
                            != [(q["id"], q["pdrIds"]) for q in pinned["qers"]]
                            or any(q["mbrKbps"] != (rates if q["id"] == target_id else old["mbrKbps"])
                                   for q, old in zip(current["qers"], pinned["qers"]))):
                        raise GroupQerBlocked("Native QER installation mismatch")
                except Exception:
                    return self._restore(record)
            try:
                self._guard_inventory(record, rates)
            except Exception:
                return self._restore(record)
            record["state"] = "installed"
            self._put(record)
            self.transport.end(self._authorities.pop(record["id"]))
            return self.view(record)

    def reconcile(self, action_id):
        with self._lock:
            with self._db() as db:
                row = db.execute("SELECT record FROM group_qer_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise GroupQerBlocked("Unknown group QER action")
            record = json.loads(row[0])
            if record["state"] in {"quarantined", "rollback", "applying", "intent"} and not record.get("uncertain"):
                try:
                    self._guard_inventory(record, restore=True)
                except Exception:
                    record["state"] = "quarantined"
                else:
                    record["state"] = "restored"
                self._put(record)
            return self.view(record)

    def repair_restore(self, action_id):
        """One observed-state compensation after an incomplete restore.

        Never replay the original batch. Preserve unknown HTTP outcomes and
        write only still-owned AMBR rules observed at this action's installed
        rate. A durable one-use intent prevents another write after a crash.
        """
        with self._lock:
            with self._db() as db:
                row = db.execute("SELECT record FROM group_qer_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise GroupQerBlocked("Unknown group QER action")
            record = json.loads(row[0])
            if (record["state"] != "quarantined" or record.get("uncertain") is not False
                    or record.get("restoreRepair") is not None):
                return self.reconcile(action_id)
            self._current(record["fence"], restore=True)
            actual = self._inventory(record["snapshot"], action_id=action_id, phase="pre_restore")
            pending = []
            for supi, pinned in record["baseline"].items():
                current = self._shape(actual[supi])
                if current == pinned:
                    continue
                expected = deepcopy(pinned)
                _ambr_qer(expected["qers"])["mbrKbps"] = record["profileRates"]
                if (supi not in record["attemptedSupis"] or supi not in record["treatmentSupis"]
                        or current != expected):
                    raise GroupQerBlocked("Restore compensation encountered foreign native state")
                pending.append(supi)
            if not pending:
                return self.reconcile(action_id)
            prior = row[0]
            record["state"] = "rollback"
            record["restoreRepair"] = {"state": "intent", "supis": sorted(pending),
                "createdAt": self.clock(), "nativeBeforeSha256": hashlib.sha256(
                    json.dumps(actual, sort_keys=True, allow_nan=False).encode()).hexdigest()}
            with self._db() as db:
                changed = db.execute("UPDATE group_qer_actions SET record=? WHERE id=? AND record=?",
                    (json.dumps(record, sort_keys=True, allow_nan=False), action_id, prior)).rowcount
                if changed != 1:
                    raise GroupQerBlocked("Restore compensation lost durable ownership")
            try:
                self._authorities[action_id] = self.transport.begin_restore(record, self.round_reader)
                for supi in pending:
                    self._current(record["fence"], restore=True)
                    current = self._inventory(record["snapshot"], action_id=action_id, phase="pre_restore")
                    if self._shape(current[supi]) != self._shape(actual[supi]):
                        raise GroupQerBlocked("Restore compensation native owner changed")
                    rates = _ambr_qer(record["baseline"][supi]["qers"])["mbrKbps"]
                    if not self._request(record, supi, rates, "restore", journal_phase="restore_repair"):
                        raise GroupQerBlocked("Restore compensation outcome unresolved")
                self._guard_inventory(record, restore=True)
                record["restoreRepair"]["state"] = "verified"
                record["state"] = "restored"
            except Exception:
                record["restoreRepair"]["state"] = "unresolved"
                record["state"] = "quarantined"
            finally:
                self._put(record)
                authority = self._authorities.pop(action_id, None)
                if authority is not None:
                    self.transport.end(authority)
            return self.view(record)

    def cleanup(self, round_id):
        with self._lock:
            records = self._records(round_id)
            for record in reversed(records):
                if record["state"] == "installed":
                    record["attemptedSupis"] = record["treatmentSupis"]
                    self._restore(record)
                elif record["state"] != "restored":
                    self.reconcile(record["id"])
                    if next(r for r in self._records(round_id) if r["id"] == record["id"])["state"] == "quarantined":
                        self.repair_restore(record["id"])
                else:
                    # A durable "restored" label is not a fresh UPF readback.
                    # Recheck on every cleanup/restart before releasing the round.
                    try:
                        self._guard_inventory(record, restore=True)
                    except Exception:
                        record["state"] = "quarantined"
                        self._put(record)
            records = self._records(round_id)
            return {"roundId": round_id, "cleanupVerified": all(r["state"] == "restored" for r in records),
                    "actions": [self.view(r) for r in records]}
