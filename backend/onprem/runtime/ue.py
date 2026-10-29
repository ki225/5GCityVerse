"""Durable on-prem UE intent and identity-correlated evidence.

No observed counter is derived from intent, pod replicas, or success log text.
The collector contract is deliberately versioned; missing collectors fail closed.
"""
from __future__ import annotations
from backend.onprem.runtime.errors import EvidenceUnavailable, RequestGuardDiagnostic
from backend.onprem.runtime.topology import BASE_IMSI, ENDPOINTS, SLICES
from backend.onprem.storage.readonly import decode_generation
from backend.onprem.storage.recovery_guard import assert_recovery_clear

import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.request import Request, urlopen

SOURCES = ("ue", "amf", "smf", "upf", "traffic")
COUNTERS = ("requested", "allocated", "registered", "pduEstablished", "trafficVerified", "failed")
logger = logging.getLogger(__name__)


def _postcapture_timing(evidence, reconcile_elapsed_ms, phase):
    """Numeric diagnostics only; logging must never change reconciliation."""
    try:
        if phase not in {"before_evaluate", "reconcile_failed"}:
            return
        now = time.time()
        source_ages = {}
        for name in SOURCES:
            try:
                value = (now - seconds(evidence.get("sources", {}).get(name, {}).get("observedAt"))) * 1000
                source_ages[name] = round(value) if math.isfinite(value) else None
            except (ValueError, TypeError, AttributeError, OverflowError):
                source_ages[name] = None
        try:
            capture_age = (now - seconds(evidence.get("observedAt"))) * 1000
            capture_age = round(capture_age) if math.isfinite(capture_age) else None
        except (ValueError, TypeError, AttributeError, OverflowError):
            capture_age = None
        known_ages = [value for value in source_ages.values() if value is not None]
        elapsed = reconcile_elapsed_ms if type(reconcile_elapsed_ms) in (int, float) else None
        elapsed = round(elapsed) if elapsed is not None and math.isfinite(elapsed) and elapsed >= 0 else None
        row = {"phase": phase, "driverReconcileElapsedMs": elapsed, "captureEndAgeMs": capture_age,
               "sourceAgeMs": source_ages, "maximumSourceAgeMs": max(known_ages) if known_ages else None}
        logger.warning("ue_postcapture_timing %s", json.dumps(row, sort_keys=True, allow_nan=False))
    except Exception:
        pass  # Diagnostic sink failures cannot alter native or status behavior.


def timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def seconds(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("observation timestamp must be ISO 8601")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("observation timestamp must include timezone")
    return dt.timestamp()


def validate_counts(body: Any) -> dict[str, int]:
    if not isinstance(body, dict) or set(body) != set(SLICES):
        raise ValueError("Provide exactly embb, urllc, mmtc and v2x")
    if any(type(value) is not int or not 0 <= value <= 1000 for value in body.values()):
        raise ValueError("Each slice count must be an integer between 0 and 1000")
    if sum(body.values()) > 1000:
        raise ValueError("Total UE count must not exceed 1000")
    return {key: body[key] for key in SLICES}


def allocate(counts: dict[str, int]) -> list[dict[str, Any]]:
    """Stable credentials belong to immutable SUPI groups, not mutable slices."""
    result = []
    offset = 0
    for name, count in counts.items():
        for _ in range(count):
            result.append({"supi": f"imsi-{BASE_IMSI + offset}", "slice": name,
                           "credentialGroup": offset // 250})
            offset += 1
    return result


def shards(allocation: list[dict[str, Any]], batch_size: int = 50) -> list[dict[str, Any]]:
    """A fixed-count UERANSIM process is a launch unit (at most 250 UEs).

    Smaller units enforce the registration ramp; never cross credential groups.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 250:
        raise ValueError("batch_size must be between 1 and 250")
    result: list[dict[str, Any]] = []
    for item in allocation:
        if 'device' in item:
            from .device_sessions import DeviceAllocation
            device=DeviceAllocation.from_dict(item['device'])
            if (device.supi,device.access_slice,device.credential_group)!=(item['supi'],item['slice'],item['credentialGroup']):
                raise ValueError('Device generation allocation identity mismatch')
            result.append({'slice':item['slice'],'credentialGroup':item['credentialGroup'],
                           'supis':[item['supi']],'device':device.to_dict(),
                           **{k:item[k] for k in ('podIndex','deviceBirthId','deviceBornAt',
                               'spatialAccess','sourceLocation','demandGroupId','servingCellId','demandProfile') if k in item}})
            continue
        if (not result or result[-1]["slice"] != item["slice"]
                or 'device' in result[-1]
                or result[-1].get('spatialAccess') != item.get('spatialAccess')
                or result[-1]["credentialGroup"] != item["credentialGroup"]
                or len(result[-1]["supis"]) >= batch_size):
            result.append({"slice": item["slice"], "credentialGroup": item["credentialGroup"], "supis": [], **({"spatialAccess": item["spatialAccess"]} if "spatialAccess" in item else {})})
        result[-1]["supis"].append(item["supi"])
    return result


def blank_status(generation: int = 0, counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = counts or {name: 0 for name in SLICES}
    slices = {name: {**{key: 0 for key in COUNTERS}, "requested": count,
                     "allocated": count if generation else 0, "status": "requested" if generation else "not_configured"}
              for name, count in counts.items()}
    return {"generation": generation, "status": "requested" if generation else "not_configured",
            **{key: sum(item[key] for item in slices.values()) for key in COUNTERS},
            "slices": slices, "observedAt": None, "evidenceSources": [], "errors": [],
            "stableSince": None, "perUe": [], "runtime": "kubeadm",
            "credentialModel": "shared-per-immutable-250-SUPI-group"}


def ran_connected(status, now=None):
    """RAN connectivity is independent of a clean/stopped zero-UE lifecycle."""
    now=time.time() if now is None else now
    health=status.get('ranHealth',{})
    trusted_slice_sources={'native-ngap-sctp/v2','free-ran-ue-api+native-ngap-sctp/v1'}
    try:
        return (health.get('source')=='native-ngap-sctp/v2' and health.get('connected') is True
                and set(health.get('slices',{}))==set(SLICES)
                and all(row.get('available') is True and row.get('ngapAssociated') is True
                        and row.get('source') in trusted_slice_sources
                        and row.get('gnbIncarnation') and row.get('amfIncarnation') and row.get('peer',{}).get('ngSetupState')=='CONNECTED'
                        and -1<=now-seconds(row.get('observedAt'))<=15 for row in health['slices'].values()))
    except (ValueError,TypeError,KeyError):return False


def population_revision(value):
    """Membership metadata is an exact integer, never a truthy/defaulted proof."""
    revision=value.get('populationRevision',0)
    if type(revision) is not int or revision<0:
        raise EvidenceUnavailable('Invalid current population revision')
    return revision


class GenerationStore:
    """SQLite transactions serialize intent writers across server processes."""


    decode = staticmethod(decode_generation)

    def _assert_recovery_clear(self, db, *, require_fresh=False, require_release=False):
        assert_recovery_clear(self, db, require_fresh=require_fresh, require_release=require_release)

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            generations_present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                             "AND name='generations'").fetchone()
            if generations_present:
                self._assert_recovery_clear(db)
            elif (self.path.with_name(self.path.name + ".host-loss-intent.json").exists()
                  or self.path.with_name(self.path.name + ".host-loss-intent.json").is_symlink()
                  or db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='ue_host_state_loss_reconciliations'").fetchone()):
                raise EvidenceUnavailable("Host-state-loss external intent forbids empty ledger bootstrap")
            elif (self.path.with_name(self.path.name + ".unrecorded-reboot-quarantine.json").exists()
                  or self.path.with_name(self.path.name + ".unrecorded-reboot-quarantine.json").is_symlink()
                  or db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='ue_unrecorded_host_reboots'").fetchone()):
                raise EvidenceUnavailable("Unrecorded host reboot quarantine forbids empty ledger bootstrap")
            db.execute("CREATE TABLE IF NOT EXISTS generations (id INTEGER PRIMARY KEY AUTOINCREMENT, desired TEXT NOT NULL, allocation TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL)")
            from .ue_reservation import HeldUERepository
            HeldUERepository.initialize(db)
            from .population import PopulationRepository
            PopulationRepository.initialize(db)
            from .owned_drain import OwnedDrainRepository
            OwnedDrainRepository.initialize(db)
            db.execute("CREATE TABLE IF NOT EXISTS ue_reconciliation_watchdogs (generation_id INTEGER PRIMARY KEY, last_established REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS ue_exceptional_recoveries (generation_id INTEGER PRIMARY KEY, plan_sha TEXT NOT NULL, baseline_sha TEXT NOT NULL, phase TEXT NOT NULL, record TEXT NOT NULL)")
            legacy = db.execute("SELECT g.id,g.desired,g.created,g.status FROM generations g LEFT JOIN ue_reconciliation_watchdogs w ON w.generation_id=g.id WHERE w.generation_id IS NULL AND g.id=(SELECT MAX(id) FROM generations)").fetchone()
            if legacy:
                result = json.loads(legacy[3])
                if result.get("generation") == legacy[0]:
                    # Migration retains an original historical observation, not
                    # restart time. It does not make public evidence fresh.
                    self._checkpoint(db, result, legacy[1], legacy[2], historical=True)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)


    def current(self):
        with self.connect() as db:
            value=self.decode(db.execute("SELECT g.*, w.last_established FROM generations g LEFT JOIN ue_reconciliation_watchdogs w ON w.generation_id=g.id ORDER BY g.id DESC LIMIT 1").fetchone())
            from .population import PopulationRepository
            population=PopulationRepository._read(db)
            if value:
                if population and population['generation']==value['generation']:
                    value['populationRevision']=population_revision({'populationRevision':population['revision']})
                else:
                    if population_revision(value['result'])!=0:
                        raise EvidenceUnavailable('Current positive population journal is unavailable')
                    value['populationRevision']=0
            return value

    def read_generation(self, generation):
        """Read immutable allocation ownership for a historical generation."""
        if type(generation) is not int or generation <= 0:
            raise ValueError("A positive generation identity is required")
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM generations WHERE id=?", (generation,)).fetchone())


    def exceptional_record(self, generation):
        with self.connect() as db:
            row = db.execute("SELECT plan_sha,baseline_sha,phase,record FROM ue_exceptional_recoveries WHERE generation_id=?", (generation,)).fetchone()
            return None if row is None else {"planSha256": row[0], "baselineSha256": row[1],
                "phase": row[2], "record": json.loads(row[3])}

    def assert_no_active_exceptional(self):
        """Normal controller operations cannot replace unresolved recovery ownership."""
        with self.connect() as db:
            self._assert_recovery_clear(db)
            row = db.execute("SELECT id FROM generations ORDER BY id DESC LIMIT 1").fetchone()
            if row and db.execute("SELECT 1 FROM ue_exceptional_recoveries WHERE generation_id=? AND phase!='completed'",
                                  (row[0],)).fetchone():
                raise EvidenceUnavailable("Exceptional recovery owns the current generation")


    def history_allocations(self) -> set[str]:
        with self.connect() as db:
            return {item["supi"] for row in db.execute("SELECT allocation FROM generations") for item in json.loads(row[0])}

    def request(self, counts: dict[str, int], *, allocation_builder=None):
        counts = validate_counts(counts)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_recovery_clear(db, require_fresh=True)
            from .ue_reservation import HeldUERepository
            HeldUERepository.assert_no_held(db)
            from .population import PopulationRepository
            PopulationRepository.assert_no_pending(db,allow_stop=not any(counts.values()))
            current = self.decode(db.execute("SELECT * FROM generations ORDER BY id DESC LIMIT 1").fetchone())
            if current and db.execute("SELECT 1 FROM ue_exceptional_recoveries WHERE generation_id=? AND phase!='completed'", (current["generation"],)).fetchone():
                raise EvidenceUnavailable("Exceptional recovery owns the current generation")
            allocation = allocate(counts)
            if allocation_builder is not None:
                allocation = allocation_builder(allocation)
            if current and current["desired"] == counts and current["allocation"] == allocation:
                return current, False
            cursor = db.execute("INSERT INTO generations(desired, allocation, created, status) VALUES (?, ?, ?, ?)",
                                (json.dumps(counts), json.dumps(allocation), time.time(), "{}"))
            result = blank_status(cursor.lastrowid, counts)
            db.execute("UPDATE generations SET status=? WHERE id=?", (json.dumps(result), cursor.lastrowid))
            return self.decode(db.execute("SELECT * FROM generations WHERE id=?", (cursor.lastrowid,)).fetchone()), True


    def save(self, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_recovery_clear(db)
            row = db.execute("SELECT id,status FROM generations ORDER BY id DESC LIMIT 1").fetchone()
            if row and db.execute("SELECT 1 FROM ue_exceptional_recoveries WHERE generation_id=? AND phase!='completed'",
                                  (row[0],)).fetchone():
                raise EvidenceUnavailable("Exceptional recovery owns the current generation")
            if row and row[0] == result.get("generation"):
                durable = json.loads(row[1])
                if (durable.get("exceptionalRecovery", {}).get("state") == "verified"
                        or durable.get("hostStateLoss", {}).get("state") == "verified"):
                    return  # Ordinary reconciliation cannot erase a terminal receipt.
            self._assert_recovery_clear(db, require_release=True)
            # An old reconciliation must never overwrite the active generation.
            updated = db.execute("UPDATE generations SET status=? WHERE id=? AND id=(SELECT MAX(id) FROM generations)",
                                 (json.dumps(result), result["generation"]))
            if not updated.rowcount:
                return
            desired, created = db.execute("SELECT desired, created FROM generations WHERE id=?", (result["generation"],)).fetchone()
            self._checkpoint(db, result, desired, created)

    @staticmethod
    def _checkpoint(db, result, desired, created, *, historical=False):
        requested = sum(json.loads(desired).values())
        try:
            observed = seconds(result.get("observedAt"))
        except (ValueError, TypeError, OverflowError):
            return
        # This is private watchdog state, never fresh public network evidence.
        age = time.time() - observed
        if (result.get("status") == "established" and requested > 0
                and created <= observed and age >= 0 and (historical or age <= 15)
                and all(type(result.get(key)) is int and result[key] == requested
                        for key in ("requested", "allocated", "registered", "pduEstablished", "trafficVerified"))
                and type(result.get("failed")) is int and result["failed"] == 0):
            db.execute("INSERT INTO ue_reconciliation_watchdogs(generation_id,last_established) VALUES (?,?) ON CONFLICT(generation_id) DO UPDATE SET last_established=MAX(last_established,excluded.last_established)",
                       (result["generation"], observed))


class HttpEvidenceAdapter:
    """Trusted read-only collector, NOT an API accepting evidence from the UI.

    v1 snapshot: generation, observedAt, sources (all five with available,
    source, observedAt), ues (one per SUPI), gnb (per-slice NGAP observation).
    The deployment must provide a native NF/PFCP collector implementing this
    contract. No generic Prometheus count is accepted as per-UE proof.
    """
    def __init__(self, url: str = "", token: str = ""):
        self.url, self.token = url, token

    def observe(self, generation: int):
        if not self.url:
            raise EvidenceUnavailable("CITYVERSE_ONPREM_EVIDENCE_URL is not configured")
        request = Request(f"{self.url.rstrip('/')}/v1/generations/{generation}/evidence",
                          headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        try:
            with urlopen(request, timeout=10) as response:
                data = json.load(response)
        except Exception:
            raise EvidenceUnavailable("NF evidence collector is unavailable") from None
        if not isinstance(data, dict) or data.get("schemaVersion") != "cityverse.evidence/v1":
            raise EvidenceUnavailable("Unsupported NF evidence collector schema")
        return data


class EvidenceVerifier:
    def __init__(self, max_age: float = 15, stable_seconds: float = 60, endpoints=None):
        self.max_age, self.stable_seconds = max_age, stable_seconds
        self.endpoints = dict(ENDPOINTS if endpoints is None else endpoints)
        self._generation = None
        self._epoch = None
        self._since: float | None = None
        self._last: float | None = None

    def reset(self):
        self._since = self._last = None

    def fresh(self, observed: Any, now: float, created: float, *, future_skew: float = 1):
        value = seconds(observed)
        return created <= value <= now + future_skew and now - value <= self.max_age

    def evaluate(self, generation: dict, evidence: dict, now: float | None = None):
        now = time.time() if now is None else now
        if any('device' in r for r in generation['allocation']):
            try:
                revision=population_revision(generation)
                if population_revision(evidence)!=revision:
                    raise EvidenceUnavailable('Native device population revision changed')
                from .device_verification import verified_device_state
                result,epoch=verified_device_state(generation,evidence,now=now,maximum_age=self.max_age)
                result['populationRevision']=revision
                if self._generation!=generation['generation'] or self._epoch!=epoch or self._last is None or now-self._last>self.max_age:
                    self._since=None
                self._since=now if self._since is None else self._since
                self._generation,self._epoch,self._last=generation['generation'],epoch,now
                result['stableSince']=timestamp(self._since)
                duration=max(0,min(now-self._since,seconds(result['observedAt'])-seconds(result['stableSince'])))
                result['status']='established' if duration>=self.stable_seconds else 'verifying_traffic'
            except (EvidenceUnavailable,ValueError,TypeError,KeyError,AttributeError):
                self.reset()
                result=blank_status(generation['generation'],generation['desired'])
                result.update(status='verification_unavailable',errors=['Native device establishment proof unavailable'])
            for item in result['slices'].values():
                item['status']=result['status']
            return result
        result = blank_status(generation["generation"], generation["desired"])
        # Retain original native source times/identities for UI and AI. Zero UE
        # cleanup can still be verified while transport health is unavailable.
        try:
            result['ranHealth']={'source':'native-ngap-sctp/v2','connected':True,
                                 'slices':json.loads(json.dumps(evidence.get('gnb',{})))}
            result['ranHealth']['connected']=bool(ran_connected(result,now))
            if evidence.get("generation") != generation["generation"]:
                raise EvidenceUnavailable("Evidence belongs to another generation")
            if not self.fresh(evidence.get("observedAt"), now, generation["created"]):
                raise EvidenceUnavailable("Evidence is stale or predates the generation")
            if result["requested"] == 0 and evidence.get("sources", {}).get("smf", {}).get("inventoryComplete") is False:
                raise EvidenceUnavailable("SMF exposes no complete context inventory; zero-session completion cannot be verified")
            for name in SOURCES:
                if name == "traffic" and result["requested"] == 0:
                    # A release-only snapshot deliberately does not run a UE
                    # traffic probe once no bearer should exist.
                    continue
                source = evidence.get("sources", {}).get(name, {})
                if source.get("available") is not True or not source.get("source") or not self.fresh(
                        source.get("observedAt"), now, generation["created"],
                        future_skew=5 if name in {"ue", "upf", "traffic"} else 1):
                    raise EvidenceUnavailable(f"{name} identity evidence is unavailable or stale")
                result["evidenceSources"].append(source["source"])
            rows = evidence.get("ues")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("supi"), str) for row in rows):
                raise EvidenceUnavailable("Malformed per-UE evidence")
            identities = Counter(row["supi"] for row in rows)
            addresses = Counter(row.get("smf", {}).get("ip") for row in rows if row.get("smf", {}).get("ip"))
            interfaces = Counter((row.get("ue", {}).get("podUid"), row.get("ue", {}).get("tun")) for row in rows)
            pfcp_sessions = Counter((row.get("upf", {}).get("upf"), row.get("upf", {}).get("pfcpSeid")) for row in rows)
            uplink_tunnels = Counter((row.get("upf", {}).get("upf"), row.get("upf", {}).get("uplinkTeid")) for row in rows)
            expected = {item["supi"]: item["slice"] for item in generation["allocation"]}
            unexpected = [row["supi"] for row in rows if row["supi"] not in expected]
            if unexpected:
                result["errors"].append(f"Residual runtime contexts: {len(unexpected)}")
            by_supi = {row["supi"]: row for row in rows}
            try:
                upf_epochs = json.loads(evidence.get("sources", {}).get("upf", {}).get("epoch", "{}"))
            except (TypeError, ValueError):
                upf_epochs = {}
            if not isinstance(upf_epochs, dict):
                upf_epochs = {}
            for supi, name in expected.items():
                row = by_supi.get(supi, {})
                counts = result["slices"][name]
                amf, smf, upf, ue, probe = [row.get(source, {}) for source in ("amf", "smf", "upf", "ue", "traffic")]
                unique = identities[supi] == 1
                if row and (type(row.get("generation")) is not int or row["generation"] != generation["generation"]):
                    unique = False
                    result["errors"].append("Residual UE evidence from another generation")
                registered = unique and amf.get("registered") is True and self.fresh(amf.get("observedAt"), now, generation["created"])
                counts["registered"] += int(registered)
                ip = smf.get("ip")
                try:
                    network = ipaddress.ip_network(SLICES[name]["pool"])
                    address = ipaddress.ip_address(ip)
                    valid_ip = address in network and address not in {network.network_address, network.broadcast_address} and addresses[ip] == 1
                except (ValueError, TypeError):
                    valid_ip = False
                session_id = smf.get("sessionId")
                valid_session = isinstance(session_id, str) and session_id.isdigit() and 1 <= int(session_id) <= 255
                pdu = bool(registered and valid_session and type(smf.get("sessionCount")) is int and smf["sessionCount"] == 1
                           and smf.get("established") is True and type(smf.get("sst")) is int and smf["sst"] == SLICES[name]["sst"]
                           and smf.get("sd") == SLICES[name]["sd"] and smf.get("dnn") == "internet"
                           and smf.get("upf") == f"upf-{name}" and valid_ip
                           and self.fresh(smf.get("observedAt"), now, generation["created"]))
                counts["pduEstablished"] += int(pdu)
                traffic = bool(pdu and ue.get("running") is True and ue.get("podUid") and ue.get("tun")
                               and interfaces[(ue.get("podUid"), ue.get("tun"))] == 1
                               and ue.get("ip") == ip and ue.get("sessionId") == session_id
                               and upf.get("upf") == f"upf-{name}" and upf.get("sessionId") == session_id
                               and upf.get("ip") == ip and isinstance(upf.get("pfcpSeid"), str)
                               and upf["pfcpSeid"].isdigit() and 0 < int(upf["pfcpSeid"]) < 2**64
                               and pfcp_sessions[(upf.get("upf"), upf.get("pfcpSeid"))] == 1
                               and uplink_tunnels[(upf.get("upf"), upf.get("uplinkTeid"))] == 1
                               and all(type(upf.get(key)) is int and 0 < upf[key] < 2**32 for key in ("uplinkTeid", "downlinkTeid"))
                               and probe.get("success") is True and probe.get("interface") == ue.get("tun")
                               and probe.get("podUid") == ue.get("podUid") and probe.get("sourceIp") == ip
                               and probe.get("sessionId") == session_id and probe.get("endpoint") == self.endpoints.get(name)
                               and type(probe.get("received")) is int and type(probe.get("transmitted")) is int
                               and probe["received"] >= 1
                               and probe.get("transmitted", 0) >= probe.get("received", 0)
                               and self.fresh(ue.get("observedAt"), now, generation["created"], future_skew=5)
                               and self.fresh(upf.get("observedAt"), now, generation["created"], future_skew=5)
                               and self.fresh(probe.get("observedAt"), now, generation["created"], future_skew=5))
                counts["trafficVerified"] += int(traffic)
                nas = row.get('nas', {})
                nas_valid = (isinstance(nas, dict) and unique
                    and row.get('generation') == generation['generation']
                    and type(nas.get('generation')) is int and nas.get('generation') == generation['generation']
                    and nas.get('supi') == supi and bool(row.get('ownedPodUid'))
                    and nas.get('podUid') == row.get('ownedPodUid')
                    and nas.get('source') == 'owned-ueransim-nr-cli-status'
                    and self.fresh(nas.get('observedAt'), now, generation['created']))
                denied = bool(nas_valid and nas.get('rmState') == 'RM-DEREGISTERED'
                    and nas.get('updateState') == '5U3-ROAMING-NOT-ALLOWED'
                    and isinstance(nas.get('mmState'), str) and nas['mmState'].startswith('MM-DEREGISTERED/'))
                counts["failed"] += int(denied or bool(row.get("failed")) or not unique and supi in by_supi)
                # Whitelist every field; never forward arbitrary collector payload/secrets.
                result["perUe"].append({"supi": supi, "slice": name, "generation": row.get("generation"), "registered": registered,
                                        "pduEstablished": pdu, "trafficVerified": traffic,
                                        "ip": ip if valid_ip else None, "sessionId": session_id,
                                        "podUid": ue.get("podUid") or (nas.get('podUid') if nas_valid else None), "interface": ue.get("tun"),
                                        "failureCode": 'ue.registration_rejected' if denied else None,
                                        "nas": {key: nas.get(key) for key in ('source', 'supi', 'generation', 'podUid', 'observedAt', 'rmState', 'mmState', 'updateState')} if nas_valid else None,
                                        "upf": smf.get("upf"), "endpoint": probe.get("endpoint"),
                                        "smContextRef": smf.get("smContextRef"), "dnn": smf.get("dnn"),
                                        "sst": smf.get("sst"), "sd": smf.get("sd"),
                                        "pfcpSeid": upf.get("pfcpSeid"), "upfEpoch": upf_epochs.get(name),
                                        "observedAt": probe.get("observedAt") if traffic else None})
            for key in COUNTERS:
                result[key] = sum(item[key] for item in result["slices"].values())
            complete = (not result["errors"] and not unexpected and result["failed"] == 0 and all(result[key] == result["requested"] for key in COUNTERS[:-1]))
            epoch = evidence.get("sources", {}).get("upf", {}).get("epoch")
            if self._generation != result["generation"] or self._epoch != epoch or self._last is None or now - self._last > self.max_age:
                self._since = None
            self._epoch = epoch
            self._generation, self._last = result["generation"], now
            if complete:
                self._since = now if self._since is None else self._since
                result["stableSince"] = timestamp(self._since)
                # Collection can take seconds: evaluation time cannot extend
                # the native observation window, nor may tolerated clock skew.
                # Compare durations within their own precision domains: raw
                # evaluation clocks versus serialized observation clocks.
                observed_duration = max(0, min(now - self._since,
                    seconds(evidence["observedAt"]) - seconds(result["stableSince"])))
                result["status"] = ("established" if result["requested"] else "stopped") if observed_duration >= self.stable_seconds else "verifying_traffic"
            else:
                self._since = None
                result["status"] = "degraded" if unexpected else "registering" if result["registered"] < result["requested"] else "establishing_pdu" if result["pduEstablished"] < result["requested"] else "verifying_traffic"
            result["observedAt"] = evidence["observedAt"]
        except (EvidenceUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.reset()
            result = blank_status(generation["generation"], generation["desired"])
            result.update(status="verification_unavailable", errors=[str(exc) if isinstance(exc, EvidenceUnavailable) else "Malformed identity evidence"])
        for item in result["slices"].values():
            item["status"] = result["status"]
        return result


class UEController:
    def __init__(self, store: GenerationStore, adapter=None, driver=None, poll_seconds: float = 2):
        self.store = store
        if adapter is None and os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") == "native":
            from backend.onprem.evidence.collector import configured_native_collector
            adapter = configured_native_collector(store, driver)
        self.adapter = adapter or HttpEvidenceAdapter(os.environ.get("CITYVERSE_ONPREM_EVIDENCE_URL", ""), os.environ.get("CITYVERSE_ONPREM_EVIDENCE_TOKEN", ""))
        if adapter is None and os.environ.get("CITYVERSE_ONPREM_AMF_OAM_URL"):
            from backend.onprem.integrations.oam import CombinedEvidenceAdapter, NativeOamAdapter
            self.adapter = CombinedEvidenceAdapter(NativeOamAdapter(
                os.environ["CITYVERSE_ONPREM_AMF_OAM_URL"], os.environ.get("CITYVERSE_ONPREM_SMF_OAM_URL", ""),
                os.environ.get("CITYVERSE_ONPREM_OAM_TOKEN", ""), reference_db=store.path.with_suffix(".oam.sqlite3")), self.adapter)
        self.driver = driver
        self.verifier = EvidenceVerifier()
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        import threading
        self._owned_native_thread=threading.local()
        self._owned_native_adapter=None
        self._owned_native_factory=None
        self._process_lock = None
        self.timeout = float(os.environ.get("CITYVERSE_ONPREM_UE_TIMEOUT", "900"))
        # Never write recovery state until this process owns the OS fence.
        self._needs_recovery = True
        self._bootstrap = {"generation": 0, "desired": {name: 0 for name in SLICES}, "allocation": [], "created": time.time()}
        self._bootstrap_result = blank_status()

    def status(self):
        current = self.store.current()
        if not current:
            if self._bootstrap_result.get("status") == "stopped" and time.time() - seconds(self._bootstrap_result.get("observedAt")) > self.verifier.max_age:
                self._bootstrap_result = blank_status()
                self._bootstrap_result.update(status="verification_unavailable", errors=["Native bootstrap observation expired"])
            return dict(self._bootstrap_result,populationRevision=0)
        revision=population_revision(current)
        if self._needs_recovery:
            result = blank_status(current["generation"], current["desired"])
            result.update(status="verification_unavailable", errors=["Runtime reconciliation pending after backend restart"])
            for item in result["slices"].values():
                item["status"] = result["status"]
            return dict(result,populationRevision=revision)
        result = dict(current["result"])
        if population_revision(result)!=revision:
            # Current membership is metadata; old native proof cannot certify it.
            result=blank_status(current['generation'],current['desired'])
            result.update(status='verification_unavailable',errors=['Current population revision lacks matching native evidence'])
            for item in result['slices'].values():item['status']=result['status']
        if result.get("status") in {"established", "stopped"} and (not result.get("observedAt") or time.time() - seconds(result["observedAt"]) > self.verifier.max_age):
            self.verifier.reset()
            result = blank_status(current["generation"], current["desired"])
            result.update(status="verification_unavailable", errors=["Last observation expired"])
            for item in result["slices"].values():
                item["status"] = result["status"]
        return dict(result,populationRevision=revision)

    def acquire(self):
        """One controller process owns a state DB; OS releases lock on crash."""
        handle = open(str(self.store.path) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Another on-prem UE controller already owns this state database") from None
        self._process_lock = handle
        try:
            self.store.assert_no_active_exceptional()
            # A durable offline retirement intent must prevent even the
            # acquire-time status rewrite. The controller's OS lock is already
            # held, but reconciliation and save remain quarantined until a
            # trusted startup gate decides whether this process may run.
            guard = getattr(self, "startup_quarantine_check", None)
            self._startup_quarantined = bool(guard()) if guard is not None else False
            if not self._startup_quarantined:
                current = self.store.current()
                if current:
                    self.store.save(self.status())
                self._needs_recovery = False
        except Exception:
            self.release()
            raise

    def release(self):
        if self._process_lock:
            self._process_lock.close()
            self._process_lock = None

    def _assert_startup_not_quarantined(self):
        guard = getattr(self, "startup_quarantine_check", None)
        if getattr(self, "_startup_quarantined", False) or (guard is not None and guard()):
            raise EvidenceUnavailable("Durable UE retirement quarantine blocks controller mutation")

    async def reserve(self,counts,*,owner_id,operation_id,request_id,network_scope,valid_until,profile=None,launch_contract=None):
        """Observe an empty fleet, then hold identities without a runnable generation."""
        from .ue_reservation import HeldUERepository,UEAdmissionLaunchProfile
        async with self._lock:
            self._assert_startup_not_quarantined()
            if launch_contract is not None and getattr(self.driver,'backend',None)!='free-ran-ue':
                raise EvidenceUnavailable('current_ue_launch_contract_requires_free_ran')
            if getattr(self.driver,'backend',None)=='free-ran-ue':
                from .current_launch import current_device_launch_contract
                current_contract=current_device_launch_contract(self.driver)
                if launch_contract is not None and launch_contract!=current_contract:
                    raise EvidenceUnavailable('current_ue_launch_contract_changed')
                # Archived profiles are diagnostic inputs only on this path.
                launch_contract,profile=current_contract,None
            if self.driver is None or (launch_contract is None and type(profile) is not UEAdmissionLaunchProfile):
                raise EvidenceUnavailable('needs_measurement:commissioned_ue_launch_profile_missing')
            launch=launch_contract or profile
            import importlib
            source=getattr(importlib.import_module(type(self.driver).__module__),'CONFIG_INIT',None)
            actual_digest=getattr(self.driver,'reservation_configuration_digest',None)
            actual_digest=actual_digest or (hashlib.sha256(source.encode()).hexdigest() if isinstance(source,str) else None)
            image=getattr(self.driver,'image','').split('@')[-1]
            if actual_digest!=launch.configuration_digest or image!=launch.image_digest:
                raise EvidenceUnavailable('needs_measurement:ue_launch_profile_differs_from_driver')
            if launch.device_sessions:
                device_profile=getattr(self.driver,'device_profile',None)
                if device_profile is None or device_profile.driver!=getattr(self.driver,'backend','ueransim') or device_profile.image_digest!=image or any(len(rows)>device_profile.maximum_sessions for _,rows in launch.device_sessions):
                    raise EvidenceUnavailable('needs_measurement:multi_session_driver_profile_missing')
            current=self.store.current()
            if current and (current['allocation'] or any(current['desired'].values())):
                raise EvidenceUnavailable('Empty native fleet is required before reserving new sessions')
            evidence=await asyncio.to_thread(self.adapter.observe,current['generation'] if current else 0)
            observed=self.verifier.evaluate(current or self._bootstrap,evidence)
            if observed.get('status')!='stopped' or observed.get('errors') or observed.get('perUe'):
                raise EvidenceUnavailable('needs_measurement:fresh_empty_native_fleet_required')
            if launch_contract is not None and not ran_connected(observed):
                raise EvidenceUnavailable('needs_measurement:fresh_core_ran_evidence_required')
            if current:
                self.store.save(observed)
            else:
                self._bootstrap_result=observed
            return HeldUERepository(self.store).reserve(counts,owner_id=owner_id,operation_id=operation_id,
                request_id=request_id,network_scope=network_scope,valid_until=valid_until,profile=profile,launch_contract=launch_contract,
                runtime_policy_digest=getattr(self.driver, 'runtime_policy_digest', ''))

    async def request(self, counts, *, expected_generation=None, admission_guard=None,reservation=None,spatial_request=None):
        async with self._lock:
            self._assert_startup_not_quarantined()
            if expected_generation is not None:
                current = self.store.current()
                if current is None or current.get('generation') != expected_generation:
                    failure = EvidenceUnavailable('UE allocation generation changed during native refresh')
                    failure.nativeRequestDiagnostic = {'sourceStatusCode': 'generation_changed',
                        'generation': current.get('generation') if current else 0}
                    raise failure
            diagnostic = {}
            blocker = self.mutation_blocker(counts, diagnostic=diagnostic)
            if blocker:
                # All callers, including internal round/runtime adapters, must
                # obey the same residual-generation quarantine as the HTTP
                # route.  Do not let a new durable generation hide the failed
                # one before a separately authorized operator reconciliation.
                failure = EvidenceUnavailable(blocker)
                failure.nativeRequestDiagnostic = diagnostic
                raise failure
            if admission_guard is not None:
                admission_guard()
            if (reservation is not None and getattr(reservation, 'devices', ())
                    and getattr(getattr(self.driver, 'device_profile', None), 'driver', None) == 'free-ran-ue'
                    and spatial_request is None):
                # Formal V2 FreeRAN readback pins a commissioned city gNB UID.
                # Reject before consuming held subscriptions, never after launch.
                raise EvidenceUnavailable('commissioned_free_ran_spatial_request_required')
            allocation_builder = None
            if spatial_request is not None:
                from .spatial_ran import configured_registry, native_readouts, resolve_groups, assign_allocation
                registry = configured_registry()
                resolution = await asyncio.to_thread(lambda: resolve_groups(spatial_request, registry, native_readouts(self.driver, registry)))
                allocation_builder = lambda allocation: assign_allocation(allocation, spatial_request, resolution)
            if reservation is not None:
                from .ue_reservation import HeldUERepository
                if (getattr(reservation, 'devices', ()) and getattr(reservation, 'runtime_policy_digest', '')
                        != getattr(self.driver, 'runtime_policy_digest', '')):
                    raise EvidenceUnavailable('held_device_runtime_policy_changed')
                current,changed=HeldUERepository(self.store).consume(reservation,counts,allocation_builder=allocation_builder)
            else:
                current, changed = self.store.request(counts, allocation_builder=allocation_builder)
            if changed:
                self.verifier.reset()
            return current["result"]

    def _owned_native_guard(self):
        from contextlib import contextmanager
        @contextmanager
        def guarded():
            if not getattr(self._owned_native_thread,'authorized',False) or not self._lock.locked():
                raise EvidenceUnavailable('Native teardown requires the existing controller mutation fence')
            yield
        return guarded()

    def _owned_native_call(self,callback,*args):
        # Only called by this controller while holding its asyncio mutation lock.
        previous=getattr(self._owned_native_thread,'authorized',False)
        self._owned_native_thread.authorized=True
        try:return callback(*args)
        finally:self._owned_native_thread.authorized=previous

    async def _owned_native_async(self,callback,*args):
        # Cancellation cannot release the sole writer while native thread work
        # is still running. Drain the exact worker, never start a replacement.
        worker=asyncio.create_task(asyncio.to_thread(self._owned_native_call,callback,*args))
        cancelled=False
        while True:
            try:
                value=await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled=True
                if worker.cancelled():raise
        if cancelled:raise asyncio.CancelledError()
        return value

    async def request_owned_native_stop(self,snapshot,lease,permit,*,admission_guard,factory):
        """Healthy ordinary stop with a pre-NAS baseline and atomic typed zero receipt."""
        from .owned_drain import OwnedNormalStopRepository
        async with self._lock:
            self._assert_startup_not_quarantined()
            admission_guard()
            adapter=await self._owned_native_async(factory,snapshot,self._owned_native_guard)
            if adapter.source.driver is not self.driver:
                raise EvidenceUnavailable('Native teardown must use the existing sole UE driver')
            baseline=await self._owned_native_async(adapter.prepare_drain_baseline,snapshot)
            if baseline.lease!=lease.to_dict() or baseline.permit['release_operation_id']!=permit.release_operation_id:
                raise EvidenceUnavailable('Native baseline does not own the requested formal stop')
            journal=OwnedNormalStopRepository(self.store)
            receipt=journal.replay(baseline)
            if receipt is None:
                diagnostic={}
                blocker=self.mutation_blocker(dict.fromkeys(SLICES,0),diagnostic=diagnostic)
                if blocker:
                    failure=EvidenceUnavailable(blocker);failure.nativeRequestDiagnostic=diagnostic
                    raise failure
                permit.validate(lease,time.time())
                admission_guard()
                result,receipt=journal.stop(baseline)
                self.verifier.reset()
            else:result=journal.validate(receipt)['result']
            await self._owned_native_async(adapter.bind_stop_receipt,receipt)
            self._owned_native_adapter=adapter
            self.driver.owned_native_teardown=self._retire_owned_native
            self.driver.owned_native_nas=self._owned_native_nas
            self.driver.owned_native_nas_ack=adapter.acknowledge_nas
            return result

    def _owned_native_nas(self,pod,dispatch):
        with self._owned_native_guard():
            if self._owned_native_adapter is None:raise EvidenceUnavailable('Native NAS baseline unavailable')
            return self._owned_native_adapter.nas_shutdown(pod,dispatch)

    def _retire_owned_native(self):
        if self._owned_native_adapter is None:
            raise EvidenceUnavailable('Durable native teardown adapter is not hydrated')
        with self._owned_native_guard():
            results=self._owned_native_adapter.retire()
            if not results or any(r.get('core_contexts_gone') is not True for r in results):
                raise EvidenceUnavailable('Owned native core teardown awaits independently verified absence')
            return results

    def _hydrate_owned_native(self,current):
        # Reconstruct only the persisted baseline/stop pair after restart.
        from .owned_drain import OwnedNormalStopRepository
        stop_journal=OwnedNormalStopRepository(self.store)
        receipt=stop_journal.read()
        if receipt is not None and current['generation']==receipt.zero_generation and stop_journal.active():
            if self._owned_native_adapter is None:
                if not callable(self._owned_native_factory):
                    raise EvidenceUnavailable('Owned native teardown restart factory is unavailable')
                snapshot=self._owned_native_snapshot_reader()
                self._owned_native_adapter=self._owned_native_factory(snapshot,self._owned_native_guard)
                baseline=self._owned_native_adapter.prepare_drain_baseline()
                if baseline!=receipt.baseline:
                    raise EvidenceUnavailable('Restarted native teardown baseline changed')
            self._owned_native_adapter.bind_stop_receipt(receipt)
            self.driver.owned_native_teardown=self._retire_owned_native
            self.driver.owned_native_nas=self._owned_native_nas
            self.driver.owned_native_nas_ack=self._owned_native_adapter.acknowledge_nas
        else:
            self._owned_native_adapter=None
            self.driver.owned_native_teardown=None
            self.driver.owned_native_nas=None
            self.driver.owned_native_nas_ack=None
        return receipt if self._owned_native_adapter is not None else None

    def _advance_owned_stop_before_observe(self,current):
        """Progress exact typed shutdown, never manufacture release evidence."""
        from .owned_drain import OwnedNormalStopRepository
        from .owned_native_teardown import TeardownBlocked
        journal=OwnedNormalStopRepository(self.store)
        receipt=journal.read()
        if receipt is None or not journal.active():return
        if current['generation']!=receipt.zero_generation:
            raise EvidenceUnavailable('Typed owned stop current generation changed')
        journal.validate(receipt)
        self._hydrate_owned_native(current)
        adapter=self._owned_native_adapter
        # This checks current operation/lease and the immutable pre-NAS baseline
        # even when the in-memory adapter was already hydrated on a prior poll.
        if adapter.prepare_drain_baseline()!=receipt.baseline:
            raise EvidenceUnavailable('Typed owned stop baseline changed')
        _,_,scope=adapter.authority.check(adapter.round_id)
        pods=adapter.source.pods()
        adapter.source.validate_inventory(pods,scope['devices'])
        owned={d['podUid'] for d in scope['devices']}
        cohort=[p for p in pods if p.get('metadata',{}).get('uid') in owned]
        try:
            # Existing ACK-only mode strips all source proof. It can progress
            # journalled NAS/terminal-core stages, but cannot authorize DELETE.
            self.driver._retire_cohort(cohort,{},maximum_generation=receipt.baseline.original_generation,
                                       watchdog_ack_only=True)
        except (EvidenceUnavailable,TeardownBlocked):
            pass  # Original observer below must still independently prove release.

    def _reconcile_owned_native(self,current,evidence):
        self._hydrate_owned_native(current)
        return self.driver.reconcile(current,evidence)

    async def request_owned_drain(self,lease,permit,*,expected_generation,original_allocation,owned_snapshot,admission_guard=None):
        """Private native-zero quarantine recovery for the original formal owner.

        Ordinary active UE shutdown keeps using request(zeros). This exceptional
        entry cannot terminate a running UE or reassign quarantined identities.
        """
        from .owned_drain import OwnedDrainRepository,authority,native_zero,sha
        async with self._lock:
            self._assert_startup_not_quarantined()
            self.store.assert_no_active_exceptional()
            if not callable(admission_guard) or self.driver is None or not callable(getattr(self.driver,'owned_failed_drain_pods',None)):
                raise EvidenceUnavailable('Owned drain durable authority guard/native driver unavailable')
            admission_guard()
            allocation_sha=authority(lease,permit,expected_generation,original_allocation,owned_snapshot,time.time())
            repository=OwnedDrainRepository(self.store)
            replay=repository.replay(lease,expected_generation,allocation_sha)
            if replay is not None:
                admission_guard()
                return replay  # Command readback, never a native-gone claim.
            current=self.store.current()
            if not current or current['generation']!=expected_generation or sha(current['allocation'])!=allocation_sha:
                raise EvidenceUnavailable('Owned drain original generation/allocation changed')
            if any(k in current['result'] for k in ('recoveryMode','exceptionalRecovery','hostStateLoss')):
                raise EvidenceUnavailable('Exceptional recovery cannot use owned quarantine drain')
            pods=await asyncio.to_thread(self.driver.owned_failed_drain_pods,current,owned_snapshot)
            observer=getattr(self.adapter,'observe_release',None)
            if not callable(observer):
                raise EvidenceUnavailable('Owned drain native release reader unavailable')
            evidence=await asyncio.to_thread(observer,expected_generation)
            native_zero(evidence,expected_generation,original_allocation)
            closing=await asyncio.to_thread(self.driver.owned_failed_drain_pods,current,owned_snapshot)
            if closing!=pods:
                raise EvidenceUnavailable('Owned drain residual Pod incarnation changed during native observation')
            admission_guard()
            authority(lease,permit,expected_generation,original_allocation,owned_snapshot,time.time())
            proof_clock=native_zero(evidence,expected_generation,original_allocation)
            result=repository.stop(lease,permit,expected_generation,allocation_sha,proof_clock,pods)
            self.verifier.reset()
            return result

    async def reserve_population(self,add_counts,remove_device_ids,*,lease,expected_population_revision,
                                 operation_id,profile=None,idempotency_key,valid_until,replacement_constraint=None):
        from .population import PopulationRuntime,PopulationRepository
        async with self._lock:
            self._assert_startup_not_quarantined()
            self.store.assert_no_active_exceptional()
            from .ue_reservation import UEAdmissionLaunchProfile
            from .current_launch import CurrentDeviceLaunchContract,current_device_launch_contract
            if type(profile) is CurrentDeviceLaunchContract and getattr(self.driver,'backend',None)!='free-ran-ue':
                raise EvidenceUnavailable('current_population_launch_contract_requires_free_ran')
            if getattr(self.driver,'backend',None)=='free-ran-ue' and replacement_constraint is None:
                current_contract=current_device_launch_contract(self.driver)
                if type(profile) is CurrentDeviceLaunchContract and profile!=current_contract:
                    raise EvidenceUnavailable('current_population_launch_contract_changed')
                profile=current_contract
            if type(profile) not in (UEAdmissionLaunchProfile,CurrentDeviceLaunchContract):
                raise EvidenceUnavailable('typed_population_profile_required')
            if self.driver is None or getattr(self.driver,'device_profile',None) is None:
                raise EvidenceUnavailable('v2_device_population_driver_required')
            if (profile.image_digest!=self.driver.image.split('@')[-1]
                    or profile.configuration_digest!=self.driver.reservation_configuration_digest):
                raise EvidenceUnavailable('population_profile_driver_mismatch')
            current=self.store.current()
            if not current:
                raise EvidenceUnavailable('population_requires_active_generation')
            recorded=PopulationRepository(self.store).current()
            if recorded and recorded.get('idempotency_key')==idempotency_key:
                baseline=recorded.get('baseline_snapshot')
            else:
                observer=PopulationRuntime(self).observer()
                if (not current['allocation'] and recorded and recorded['generation']==current['generation']
                        and (recorded.get('delta') or {}).get('state')=='committed' and not recorded['births']):
                    from .population import HeldPopulationDelta
                    baseline=await asyncio.to_thread(observer.observe_population,HeldPopulationDelta.from_dict(recorded['delta']),())
                else:
                    baseline=await asyncio.to_thread(observer.observe,current['generation'])
            from .current_sessions import session_rows
            session_rows(baseline)
            if not (recorded and recorded.get('idempotency_key')==idempotency_key) and not -1<=time.time()-seconds(baseline.get('observedAt'))<=15:
                raise EvidenceUnavailable('population_baseline_expired')
            return PopulationRepository(self.store).reserve(add_counts,remove_device_ids,lease=lease,
                expected_population_revision=expected_population_revision,operation_id=operation_id,
                profile=profile,idempotency_key=idempotency_key,valid_until=valid_until,baseline_snapshot=baseline,
                replacement_constraint=replacement_constraint)

    async def consume_population(self,delta):
        from .population import PopulationRepository,PopulationRuntime
        async with self._lock:
            self._assert_startup_not_quarantined()
            PopulationRepository(self.store).authorize(delta,'adding')
            return await asyncio.to_thread(PopulationRuntime(self).readback,delta)

    async def retire_population(self,delta):
        from .population import PopulationRepository,PopulationRuntime
        async with self._lock:
            self._assert_startup_not_quarantined()
            PopulationRepository(self.store).authorize(delta,'retiring')
            return await asyncio.to_thread(PopulationRuntime(self).readback,delta)

    async def population_readback(self,delta):
        from .population import PopulationRuntime
        async with self._lock:
            return await asyncio.to_thread(PopulationRuntime(self).readback,delta)

    def population_empty_readback(self,delta):
        """Synchronous read-only native refresh for a committed empty cohort."""
        from .population import PopulationRepository,PopulationRuntime
        repository=PopulationRepository(self.store)
        with self.store.connect() as db:
            before=repository._owned(db,delta)
        if before['delta']['state']!='committed' or before['births']:
            raise EvidenceUnavailable('Current committed empty population required')
        result=PopulationRuntime(self).readback(delta)
        with self.store.connect() as db:
            after=repository._owned(db,delta)
        if (after['delta']['state']!='committed' or after['births'] or after['revision']!=before['revision']
                or after['delta']['version']!=before['delta']['version'] or not result.ready
                or not result.current_snapshot or not result.current_snapshot.get('nativeEmptyPopulation')):
            raise EvidenceUnavailable('Empty population changed during native refresh')
        return result

    async def quiesce_population(self,lease):
        from .population import PopulationRepository
        async with self._lock:
            self._assert_startup_not_quarantined()
            PopulationRepository(self.store).quiesce(lease)

    async def confirm_population_stopped(self,lease,proof):
        from .population import PopulationRepository
        async with self._lock:
            self._assert_startup_not_quarantined()
            PopulationRepository(self.store).confirm_stopped(lease,proof)

    @staticmethod
    def request_guard_diagnostic(current, status):
        code = ('observation_expired' if status.get('errors') == ['Last observation expired'] else
            'reconciliation_pending' if status.get('errors') == ['Runtime reconciliation pending after backend restart'] else
            'ready' if status.get('status') in {'stopped', 'established'} and not status.get('errors') else
            'native_status_unavailable')
        result = {'sourceStatusCode': code, 'generation': current.get('generation') if current else 0}
        try:
            observed = status.get('observedAt') or (current or {}).get('result', {}).get('observedAt')
            result['observationAgeSeconds'] = time.time() - seconds(observed)
        except (ValueError, TypeError, AttributeError):
            pass
        return RequestGuardDiagnostic.project(result) or {}

    def expired_stopped_generation(self, counts):
        """Only an ordinary empty fleet with a previously valid expired stop."""
        current = self.store.current()
        if (not any(counts.values()) or current is None or current.get('desired') != dict.fromkeys(SLICES, 0)
                or current.get('allocation') != [] or current['result'].get('status') != 'stopped'
                or current['result'].get('errors') != [] or any(key in current['result'] for key in
                    ('recoveryMode', 'hostStateLoss', 'exceptionalRecovery'))):
            return None
        status = self.status()
        if (status.get('generation') == current['generation'] and status.get('status') == 'verification_unavailable'
                and status.get('errors') == ['Last observation expired']):
            return current
        return None

    def mutation_blocker(self, counts, *, diagnostic=None):
        """Return a fixed fail-closed reason for unsafe generation changes."""
        current = self.store.current()
        if current is None or current.get("desired") == counts:
            return None
        status = self.status()
        if diagnostic is not None:
            diagnostic.update(self.request_guard_diagnostic(current, status))
        recovery = current["result"].get("exceptionalRecovery", {})
        record = self.store.exceptional_record(current["generation"]) if recovery else None
        if (current["result"].get("status") == "failed"
                and current["result"].get("recoveryMode") == "exceptional-state-loss"
                and isinstance(recovery, dict) and recovery.get("state") == "verified"
                and record is not None and record["phase"] == "completed"
                and record["planSha256"] == recovery.get("planSha256")):
            with self.store.connect() as db:
                self.store._assert_recovery_clear(db, require_fresh=True)
            return None
        if (current["result"].get("status") == "failed"
                and current["result"].get("recoveryMode") == "host-state-loss"
                and current["result"].get("hostStateLoss", {}).get("state") == "verified"):
            # The durable release receipt, including a fresh post-terminal
            # native preflight, is checked again under GenerationStore's
            # transaction when request() actually inserts a generation.
            with self.store.connect() as db:
                self.store._assert_recovery_clear(db, require_fresh=True)
            return None
        rows = status.get("perUe", [])
        if not any(counts.values()) and status.get("status") in {"registering", "establishing_pdu", "verifying_traffic"} and not recovery:
            # Stopping a registering or not-yet-stable fleet is an explicit stop intent,
            # not proof that its Pods or native sessions have disappeared.
            # The ordinary retirement driver still verifies their release.
            try:
                observed = datetime.fromisoformat(status["observedAt"].replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError, AttributeError):
                observed = float("-inf")
            expected = {(row["supi"], row["slice"]) for row in current.get("allocation", [])}
            actual = {(row.get("supi"), row.get("slice")) for row in rows}
            if (status.get("generation") == current.get("generation") and not status.get("errors")
                    and 0 <= time.time() - observed < 5
                    and len(rows) == len(actual) == len(expected) == sum(current["desired"].values())
                    and actual == expected
                    and all(row.get("generation") == current["generation"] for row in rows)):
                return None
        if (status.get("generation") != current.get("generation")
                or status.get("status") not in {"established", "stopped"}
                or status.get("errors")
                or any(row.get("generation") != current.get("generation") for row in rows)):
            return "Current UE generation requires operator reconciliation before resize or retirement"
        return None

    async def reconcile(self):
        async with self._lock:
            await asyncio.to_thread(self._assert_startup_not_quarantined)
            self.store.assert_no_active_exceptional()
            self._needs_recovery = False
            current = self.store.current()
            from .population import PopulationRepository,PopulationRuntime
            population=PopulationRepository(self.store).current()
            if (population and population['generation']==(current or {}).get('generation') and population.get('delta')
                    and population['delta']['state'] not in {'committed','expired'}):
                # Keep the preceding proved cohort's status while a newcomer
                # is pending. Never run global timeout cleanup for this delta.
                from .population import HeldPopulationDelta
                try:
                    await asyncio.to_thread(PopulationRuntime(self).reconcile)
                    state=await asyncio.to_thread(PopulationRuntime(self).readback,HeldPopulationDelta.from_dict(population['delta']))
                    snapshot=state.current_snapshot if state.ready else state.committed_snapshot
                    if snapshot is not None:
                        ids={row['deviceId'] for row in snapshot['devices']}
                        latest=self.store.current()
                        subset=[row for row in latest['allocation'] if row['device']['device_id'] in ids]
                        projection=dict(latest,allocation=subset,desired={name:sum(row['slice']==name for row in subset) for name in SLICES})
                        result=self.verifier.evaluate(projection,snapshot)
                        result.update(populationRevision=state.population_revision,populationPending=not state.ready)
                        self.store.save(result)
                        return result
                except (EvidenceUnavailable,ValueError):
                    pass  # Keep existing proof's original timestamp; never refresh by intent.
                return self.status()
            if current is None:
                if os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") == "native":
                    try:
                        evidence = await asyncio.to_thread(self.adapter.observe, 0)
                        self._bootstrap_result = self.verifier.evaluate(self._bootstrap, evidence)
                    except Exception as exc:
                        self.verifier.reset()
                        self._bootstrap_result = blank_status()
                        self._bootstrap_result.update(status="verification_unavailable", errors=[str(exc) if isinstance(exc, EvidenceUnavailable) else "Native bootstrap evidence unavailable"])
                return self.status()
            if current["result"].get("recoveryMode") == "host-state-loss":
                # Terminal proof preserves the failed 500/498 status. It does
                # not authorize ordinary timeout cleanup or UE deployment.
                self.verifier.reset()
                result = blank_status(current["generation"], current["desired"])
                result.update(status="verification_unavailable",
                              errors=["Host-state-loss terminal requires fresh backend preflight"])
                return result
            try:
                # Zero is a persistent stop intent, not an expiring launch.
                # Its old-generation Pods must continue through ordinary guarded
                # retirement; cleanup(current_generation) cannot find those Pods.
                timeout_anchor = current["created"]
                last_established = current.get("lastEstablishedAt")
                if (type(last_established) in (int, float) and math.isfinite(last_established)
                        and timeout_anchor <= last_established <= time.time()):
                    timeout_anchor = last_established
                if self.driver is not None and sum(current["desired"].values()) > 0 and time.time() - timeout_anchor > self.timeout and current["result"].get("status") not in {"established", "stopped"}:
                    try:
                        evidence = await asyncio.to_thread(getattr(self.adapter, 'observe_release', self.adapter.observe), current["generation"])
                    except Exception:
                        evidence = None
                    cleanup_error = None
                    try:
                        self.store.assert_no_active_exceptional()
                        await asyncio.to_thread(self.driver.cleanup, current["generation"], evidence)
                    except EvidenceUnavailable as exc:
                        cleanup_error = str(exc)
                    result = self.verifier.evaluate(current, evidence) if evidence else dict(current["result"])
                    result.update(status="degraded" if evidence is None or evidence.get("ues") else "failed",
                                  errors=["Reconciliation timed out; graceful UE shutdown requested; release remains independently verified"] + ([cleanup_error] if cleanup_error else []))
                    for item in result["slices"].values():
                        item["status"] = result["status"]
                    self.verifier.reset()
                    self.store.save(result)
                    return result
                self.store.assert_no_active_exceptional()
                if self.driver is not None:
                    await self._owned_native_async(self._advance_owned_stop_before_observe,current)
                retirement = self.driver is not None and hasattr(self.driver, 'retirement_required') and await asyncio.to_thread(self.driver.retirement_required, current)
                observer = getattr(self.adapter, 'observe_release', self.adapter.observe) if retirement else self.adapter.observe
                evidence = await asyncio.to_thread(observer, current["generation"])
                reconcile_started = time.monotonic()
                reconcile_phase = "reconcile_failed"
                try:
                    if self.driver is not None:
                        self.store.assert_no_active_exceptional()
                        await self._owned_native_async(self._reconcile_owned_native,current,evidence)
                    reconcile_phase = "before_evaluate"
                finally:
                    _postcapture_timing(evidence, (time.monotonic() - reconcile_started) * 1000, reconcile_phase)
                result = self.verifier.evaluate(current, evidence)
            except Exception as exc:
                self.verifier.reset()
                result = blank_status(current["generation"], current["desired"])
                result.update(status="verification_unavailable", errors=[str(exc) if isinstance(exc, EvidenceUnavailable) else "Runtime or identity evidence adapter is unavailable; no completion inferred"])
                for item in result["slices"].values():
                    item["status"] = result["status"]
            self.store.save(result)
            return result

    async def run(self, broadcast, stop=None):
        stop = stop if stop is not None else asyncio.Event()
        while not stop.is_set():
            # Cancelling a to_thread await does not stop its native operation.
            # Keep reconciliation alive until it finishes before releasing the
            # process fence, even if the surrounding server is cancelled.
            active = asyncio.create_task(self.reconcile())
            result = None
            try:
                result = await asyncio.shield(active)
            except asyncio.CancelledError:
                await asyncio.shield(active)
                raise
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                if type(code) is not int or code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                # No new status/timestamp is saved or broadcast on this failure.
                # Exclusive maintenance still blocks every physical operation.
                logger.warning('UE observation deferred: durable ledger is busy')
            if result is not None:
                await broadcast({"type": "ue_status", "payload": result})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
