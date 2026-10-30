"""On-prem SMF QER transport and independent UPF readback providers.

Construction performs no live mutation. Capability requires the exact deployed
image, token mount, service and healthy endpoint for the current round.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from backend.onprem.policy.group_qer import GroupQerAdapter, GroupQerBlocked, _ambr_qer
from backend.onprem.scenarios.contracts import SCENARIO_SLICES as EVENT_SLICES
from backend.onprem.runtime.ue import SLICES, seconds


TOKEN_PATH = "/var/run/secrets/5gcityverse/qer-actuator/token"
QER_PATH = "/nsmf-oam/v1/qer-actuation"
DEFAULT_UPF_SERVICES = {name: f"upf-{name}-evidence" for name in SLICES}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class OnpremGroupQerProvider:
    """Use only fixed, operator configured SMF/UPF endpoints and a K8s Secret.

    Runtime checks bind the capability to the current round and fixed image.
    """

    def __init__(self, driver, *, smf_url, patched_image, smf_service,
                 secret_name="smf-qer-actuator-token",
                 upf_services=None, upf_urls=None, clock=time.time, timeout=5, opener=None):
        parsed = urlsplit(smf_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Fixed SMF OAM origin required")
        if not isinstance(patched_image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", patched_image):
            raise ValueError("An immutable patched SMF image is required")
        if not all(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
                   for name in (secret_name, smf_service)):
            raise ValueError("Invalid fixed Kubernetes resource name")
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ValueError("Bounded SMF HTTP timeout required")
        services = DEFAULT_UPF_SERVICES if upf_services is None else upf_services
        if (not isinstance(services, dict) or set(services) != set(SLICES)
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value)
                       for value in services.values())):
            raise ValueError("Four fixed UPF evidence services required")
        self.driver = driver
        self.smf_url = smf_url.rstrip("/")
        self.patched_image = patched_image
        self.secret_name, self.smf_service = secret_name, smf_service
        self.upf_services = dict(services)
        self.upf_urls = None
        if upf_urls is not None:
            if not isinstance(upf_urls, dict) or set(upf_urls) != set(SLICES):
                raise ValueError("Four fixed loopback UPF origins required")
            origins = {}
            for name, url in upf_urls.items():
                if not isinstance(url, str):
                    raise ValueError("Invalid fixed UPF origin")
                endpoint = urlsplit(url)
                if (endpoint.scheme not in {"http", "https"} or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
                        or endpoint.username or endpoint.password or endpoint.path not in {"", "/"}
                        or endpoint.query or endpoint.fragment or endpoint.port is None):
                    raise ValueError("Fixed loopback UPF origin required")
                origins[name] = url.rstrip("/")
            if len(set(origins.values())) != len(SLICES):
                raise ValueError("UPF origins must identify four distinct relays")
            self.upf_urls = origins
        self.clock, self.timeout = clock, timeout
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self._journal_path = None
        self._profiles = None
        self._grants = {}
        self._fixture_batches = {}
        self._round_reader = None

    def _service(self):
        service = self.driver.kubectl("get", "service", self.smf_service, "-o", "json")
        return (service.get("metadata", {}).get("namespace") == self.driver.namespace
                and any(p.get("port") == 8000 and p.get("protocol", "TCP") == "TCP"
                        for p in service.get("spec", {}).get("ports", [])))

    @staticmethod
    def _fence(snapshot):
        return {key: snapshot.get(key) for key in
                ("roundId", "generation", "sequence", "policyRevision", "fleetLease", "perUe")}

    @staticmethod
    def _owner_fence(value):
        return {key: value.get(key) for key in
                ("roundId", "generation", "fleetLease", "perUe")}

    def _bound_record(self, record, round_reader, *, mode):
        if (self._journal_path is None or not isinstance(record, dict)
                or not isinstance(record.get("id"), str)
                or not callable(round_reader)):
            raise GroupQerBlocked("Durable group action authority is unavailable")
        with sqlite3.connect(self._journal_path) as db:
            row = db.execute("SELECT record FROM group_qer_actions WHERE id=?", (record["id"],)).fetchone()
        if row is None or json.loads(row[0]) != record:
            raise GroupQerBlocked("Group action differs from durable intent")
        snapshot = record.get("snapshot")
        current = self._fence(round_reader())
        bound = (self._owner_fence(current) == self._owner_fence(record["fence"])
                 if mode == "restore" else current == record["fence"])
        if (not isinstance(snapshot, dict) or self._fence(snapshot) != record.get("fence")
                or not bound):
            raise GroupQerBlocked("Group action round or lease was superseded")
        event = record.get("eventId")
        rows = snapshot.get("perUe")
        treatment, holdout = record.get("treatmentSupis"), record.get("holdoutSupis")
        if (event not in EVENT_SLICES or not isinstance(rows, list)
                or any(not isinstance(r, dict) for r in rows)
                or not isinstance(treatment, list) or not isinstance(holdout, list)
                or not treatment or (not holdout and len(treatment) != 1)
                or any(not isinstance(s, str) for s in treatment + holdout)
                or len(set(treatment + holdout)) != len(treatment + holdout)):
            raise GroupQerBlocked("Group action exact cohort is invalid")
        actual = {r.get("supi"): r for r in rows if r.get("scenario") == event}
        if (len(actual) != len(treatment + holdout) or set(actual) != set(treatment + holdout)
                or any(r.get("slice") != EVENT_SLICES[event] for r in actual.values())):
            raise GroupQerBlocked("Group action treatment or holdout was changed")
        return snapshot

    def _grant(self, record, round_reader, *, mode):
        self._bound_record(record, round_reader, mode=mode)
        baseline = record.get("baseline")
        if not isinstance(baseline, dict) or set(record["treatmentSupis"]) - set(baseline):
            raise GroupQerBlocked("Group action baseline unavailable")
        restore = {}
        for supi in record["treatmentSupis"]:
            qers = baseline[supi].get("qers") if isinstance(baseline[supi], dict) else None
            if (not isinstance(qers, list) or not qers
                    or any(not isinstance(q, dict) or not isinstance(q.get("mbrKbps"), dict)
                           for q in qers)):
                raise GroupQerBlocked("Group action baseline QER unavailable")
            restore[supi] = _ambr_qer(qers)["mbrKbps"]
        authority = secrets.token_urlsafe(32)
        self._grants[authority] = {"roundReader": round_reader, "fence": record["fence"],
            "mode": mode,
            "treatment": set(record["treatmentSupis"]), "restore": restore,
            "snapshot": record["snapshot"], "profileIds": tuple(self._profiles),
            "apply": self._profiles[record["profileId"]] if mode == "apply" else None}
        return authority

    def begin(self, record, round_reader, profiles):
        """Authorize only a durable exact-cohort action with live commissioning."""
        self._reject_normal_write(record.get("snapshot") if isinstance(record, dict) else None)
        if (self._profiles is None or round_reader is not self._round_reader or profiles != self._profiles
                or record.get("state") != "intent"
                or record.get("profileId") not in self._profiles
                or self.capability(record["snapshot"], tuple(self._profiles)) is None):
            raise GroupQerBlocked("Commissioned QER action unavailable")
        return self._grant(record, round_reader, mode="apply")

    def begin_restore(self, record, round_reader):
        """Revoked commissioning cannot prevent restoration of a durable owner."""
        self._reject_normal_write(record.get("snapshot") if isinstance(record, dict) else None)
        if (round_reader is not self._round_reader or record.get("state") != "rollback"
                or record.get("profileId") not in (self._profiles or {})):
            raise GroupQerBlocked("No durable QER restoration authority")
        return self._grant(record, round_reader, mode="restore")

    def end(self, authority):
        self._grants.pop(authority, None)

    def _reject_normal_write(self, snapshot):
        if self._journal_path is None:
            return
        from backend.onprem.validation.normal import normal_claim_for_snapshot
        try:
            claim = normal_claim_for_snapshot(self._journal_path, snapshot)
        except Exception as exc:
            raise GroupQerBlocked("Scenario fixture write authority unavailable") from exc
        if claim is not None:
            raise GroupQerBlocked("Normal validation track forbids QER writes")

    def _token(self):
        secret = self.driver.kubectl("get", "secret", self.secret_name, "-o", "json")
        if secret.get("metadata", {}).get("namespace") != self.driver.namespace:
            raise GroupQerBlocked("Actuator Secret belongs to another namespace")
        encoded = secret.get("data", {}).get("token")
        if not isinstance(encoded, str) or not encoded:
            raise GroupQerBlocked("Actuator Secret token unavailable")
        try:
            token = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise GroupQerBlocked("Actuator Secret token invalid") from None
        if not token or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise GroupQerBlocked("Actuator Secret token invalid")
        return token

    def _image_and_mount(self):
        deployment = self.driver.kubectl("get", "deployment", "smf", "-o", "json")
        template = deployment.get("spec", {}).get("template", {}).get("spec", {})
        pods = self.driver.kubectl("get", "pods", "-l", "app.kubernetes.io/name=smf", "-o", "json").get("items", [])
        if not isinstance(pods, list) or len(pods) != 1:
            return False
        pod = pods[0]
        if (pod.get("metadata", {}).get("namespace") != self.driver.namespace
                or pod.get("metadata", {}).get("deletionTimestamp")
                or pod.get("status", {}).get("phase") != "Running"):
            return False
        spec = pod.get("spec", {})
        selected = [c for c in spec.get("containers", []) if c.get("name") == "smf"]
        status = [c for c in pod.get("status", {}).get("containerStatuses", []) if c.get("name") == "smf"]
        deployed = [c for c in template.get("containers", []) if c.get("name") == "smf"]
        digest = self.patched_image.rsplit("@", 1)[1]
        if (len(selected) != 1 or selected[0].get("image") != self.patched_image
                or deployed[0].get("image") != self.patched_image
                or status[0].get("ready") is not True
                or not str(status[0].get("imageID", "")).endswith("@" + digest)):
            return False
        def mounted(pod_spec, container):
            env = [e for e in container.get("env", []) if e.get("name") == "SMF_QER_ACTUATOR_TOKEN_FILE"]
            supi_only = [e for e in container.get("env", []) if e.get("name") == "SMF_QER_ACTUATOR_SUPI_ONLY"]
            mount = [m for m in container.get("volumeMounts", [])
                     if m.get("name") == "qer-actuator-token" and m.get("mountPath") == TOKEN_PATH.rsplit("/", 1)[0]
                     and m.get("readOnly") is True]
            volume = [v for v in pod_spec.get("volumes", []) if v.get("name") == "qer-actuator-token"]
            secret = volume[0].get("secret", {}) if volume else {}
            return (len(env) == len(mount) == len(volume) == 1
                    and env[0].get("value") == TOKEN_PATH
                    and len(supi_only) == 1 and supi_only[0].get("value") == "true"
                    and secret.get("secretName") == self.secret_name
                    and {item.get("key"): item.get("path") for item in secret.get("items", [])} == {"token": "token"})
        return mounted(template, deployed[0]) and mounted(spec, selected[0])

    def _health(self):
        req = Request(self.smf_url + "/nsmf-oam/v1/", method="GET")
        with self.opener.open(req, timeout=self.timeout) as response:
            if response.status != 200:
                return False
            raw = response.read(4097)
        return len(raw) <= 4096 and json.loads(raw) == {"status": "Service Available"}

    def commissioning_scope(self, snapshot, profile_ids):
        return {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                "smfImage": self.patched_image, "smfService": self.smf_service,
                "smfEndpoint": self.smf_url + QER_PATH,
                "profileIds": sorted(profile_ids), "secretName": self.secret_name}

    def capability(self, snapshot, profile_ids):
        """Return a round-scoped receipt after checking the live SMF actuator."""
        try:
            scope = self.commissioning_scope(snapshot, profile_ids)
            if (not isinstance(scope["roundId"], str) or not scope["roundId"]
                    or type(scope["generation"]) is not int
                    or not isinstance(scope["profileIds"], list) or not scope["profileIds"]
                    or not self._service() or not self._image_and_mount()
                    or not self._token() or not self._health()):
                return None
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now):
                return None
            import hashlib
            receipt_id = hashlib.sha256(json.dumps(scope, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            return {"verified": True, "source": "onprem-smf-group-qer-commissioning",
                    "scope": "supi-only", "imageDigest": self.patched_image,
                    "observedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "profileIds": sorted(profile_ids), "proofId": receipt_id}
        except Exception:
            return None

    def request(self, supi, uplink_kbps, downlink_kbps, *, authority=None, phase=None):
        """Single SUPI selector. Response body is sanitized at the adapter boundary."""
        current_snapshot = self._round_reader() if self._round_reader is not None else None
        self._reject_normal_write(current_snapshot)
        grant = self._grants.get(authority)
        current = self._fence(current_snapshot) if isinstance(grant, dict) else None
        owned = (self._owner_fence(current) == self._owner_fence(grant["fence"])
                 if isinstance(grant, dict) and grant.get("mode") == "restore"
                 else current == grant["fence"] if isinstance(grant, dict) else False)
        if (not isinstance(grant, dict) or supi not in grant["treatment"]
                or not owned or grant.get("mode") != phase
                or phase not in {"apply", "restore"}):
            raise GroupQerBlocked("QER request has no current commissioned action authority")
        expected = grant["apply"] if phase == "apply" else grant["restore"].get(supi)
        if (expected is None or expected != {"ul": uplink_kbps, "dl": downlink_kbps}):
            raise GroupQerBlocked("QER request exceeds its action profile or pinned baseline")
        if phase == "apply" and self.capability(grant["snapshot"], grant["profileIds"]) is None:
            raise GroupQerBlocked("Commissioned QER capability expired before write")
        if (not isinstance(supi, str) or not re.fullmatch(r"imsi-[0-9]{15}", supi)
                or any(type(v) is not int or not 1 <= v <= 500_000
                       for v in (uplink_kbps, downlink_kbps))):
            raise GroupQerBlocked("Invalid commissioned SUPI QER write")
        token = self._token()
        body = {"supi": supi, "uplinkMbrKbps": uplink_kbps, "downlinkMbrKbps": downlink_kbps}
        req = Request(self.smf_url + QER_PATH, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-SMF-QER-Actuator-Token": token})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status, raw = response.status, response.read(65537)
        except HTTPError as error:
            status, raw = error.code, error.read(65537)
        except Exception:
            raise GroupQerBlocked("SMF QER transport outcome unknown") from None
        if len(raw) > 65536:
            raise GroupQerBlocked("SMF QER response exceeded bounded size")
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def begin_fixture_batch(self, journal_path, snapshot, *, expected_config_sha256,
                            expected_quarantine_registry, phase="fixture_install"):
        """Commission fixed SMF once for a separate, non-model fixture batch."""
        from backend.onprem.validation.constrained import ConstrainedFixtureBlocked, constrained_claim_for_snapshot, read_constrained_receipt
        if self._round_reader is None or self._profiles is None:
            raise GroupQerBlocked("Fixture provider has no trusted round source")
        current = self._round_reader()
        self._reject_normal_write(current)
        if current != snapshot or self.capability(current, tuple(self._profiles)) is None:
            raise GroupQerBlocked("Fixture SMF or owner commissioning is unavailable")
        try:
            claim = constrained_claim_for_snapshot(
                journal_path, current, expected_quarantine_registry=expected_quarantine_registry)
            record = read_constrained_receipt(journal_path, current["requestId"])
        except ConstrainedFixtureBlocked as exc:
            raise GroupQerBlocked("Fixture batch source is unavailable") from exc
        expected_state = {"fixture_install": "active", "fixture_restore": "inner_restored"}.get(phase)
        if (expected_state is None or record["state"] != expected_state
                or record["writePossible"] is not True
                or claim["configSha256"] != expected_config_sha256):
            raise GroupQerBlocked("Fixture batch is not active")
        token = self._token()
        authority = secrets.token_urlsafe(32)
        restore_pin = None
        if phase == "fixture_restore":
            from backend.onprem.validation.restore import create_restore_pin
            restore_pin = create_restore_pin(
                journal_path, current,
                expected_quarantine_registry=expected_quarantine_registry)
        self._fixture_batches[authority] = {
            "journalPath": str(journal_path), "requestId": claim["requestId"],
            "guardId": record["guardId"], "roundId": claim["roundId"],
            "generation": claim["generation"], "fleetLeaseDigest": claim["fleetLeaseDigest"],
            "cohortSha256": claim["cohortSha256"], "configSha256": expected_config_sha256,
            "registry": str(expected_quarantine_registry), "token": token,
            "phase": phase, "restorePin": restore_pin}
        return authority

    def end_fixture_batch(self, authority):
        self._fixture_batches.pop(authority, None)

    def begin_fixture_restore_intent(self, journal_path, snapshot, supi, *,
                                     batch_authority, expected_quarantine_registry, _repair=False):
        """Reserve one original-rate intent under a deep-verified batch pin."""
        from backend.onprem.validation.restore import begin_fixture_restore_intent
        batch = self._fixture_batches.get(batch_authority)
        if (type(batch) is not dict or batch.get("phase") != "fixture_restore"
                or batch.get("journalPath") != str(journal_path)
                or batch.get("registry") != str(expected_quarantine_registry)
                or batch.get("requestId") != snapshot.get("requestId")
                or not callable(self._round_reader)
                or self._round_reader() != snapshot):
            raise GroupQerBlocked("Original-rate batch source changed")
        return begin_fixture_restore_intent(
            journal_path, snapshot, supi,
            expected_quarantine_registry=expected_quarantine_registry,
            _pin=batch["restorePin"], _repair=_repair)

    def _log_fixture_transport_failure(self, snapshot, phase, error, status=None):
        """Keep a bounded failure category without response, URL or token text."""
        import logging
        known = {'TimeoutError','URLError','ConnectionRefusedError','ConnectionResetError',
                 'RemoteDisconnected','HTTPError','JSONDecodeError','OSError','BrokenPipeError'}
        category = type(error).__name__
        reason = type(getattr(error, 'reason', None)).__name__
        number = getattr(getattr(error, 'reason', error), 'errno', None)
        logging.getLogger(__name__).warning(
            'SMF fixture transport failure generation=%s phase=%s errorType=%s reasonType=%s reasonErrno=%s httpStatus=%s',
            snapshot.get('generation'), phase, category if category in known else 'other',
            reason if reason in known else 'none_or_other', number if type(number) is int else None,
            status if type(status) is int else None)

    def request_fixture_install(self, journal_path, snapshot, intent_id, *,
                                batch_authority, expected_config_sha256,
                                expected_quarantine_registry):
        """Send one controlled fixture POST under a distinct durable authority.

        This path never creates a model grant or adds 50 kbps to model profiles.
        A committed dispatch intent is consumed even if HTTP never returns.
        """
        from backend.onprem.validation.constrained import ConstrainedFixtureBlocked, finish_fixture_install_attempt, reserve_fixture_install_dispatch
        batch = self._fixture_batches.get(batch_authority)
        if (not isinstance(snapshot, dict) or not isinstance(batch, dict) or self._round_reader is None
                or not callable(self._round_reader)
                or batch["journalPath"] != str(journal_path)
                or batch["requestId"] != snapshot.get("requestId")
                or batch["configSha256"] != expected_config_sha256
                or batch["registry"] != str(expected_quarantine_registry)
                or batch["phase"] != "fixture_install"):
            raise GroupQerBlocked("Fixture provider has no trusted round source")
        current = self._round_reader()
        self._reject_normal_write(current)
        if current != snapshot:
            raise GroupQerBlocked("Fixture owner changed before dispatch")
        from backend.onprem.validation import normal as fixture_common
        if (batch["roundId"] != snapshot.get("roundId")
                or batch["generation"] != snapshot.get("generation")
                or batch["fleetLeaseDigest"] != fixture_common._digest(snapshot.get("fleetLease"))
                or batch["cohortSha256"] != fixture_common.cohort_digest(snapshot.get("perUe"))):
            raise GroupQerBlocked("Fixture batch owner changed")
        token = batch["token"]
        try:
            dispatched = reserve_fixture_install_dispatch(
                journal_path, snapshot, intent_id, fresh_snapshot_reader=self._round_reader,
                expected_config_sha256=expected_config_sha256,
                expected_quarantine_registry=expected_quarantine_registry)
        except ConstrainedFixtureBlocked as exc:
            raise GroupQerBlocked("Exact fixture dispatch authority is unavailable") from exc
        rates, supi = dispatched["rates"], dispatched["supi"]
        if self._round_reader() != snapshot:
            try:
                finish_fixture_install_attempt(journal_path, snapshot, intent_id, None, None)
            except ConstrainedFixtureBlocked:
                pass
            raise GroupQerBlocked("Fixture owner changed after dispatch; no resend")
        body = {"supi": supi, "uplinkMbrKbps": rates["ul"],
                "downlinkMbrKbps": rates["dl"]}
        req = Request(self.smf_url + QER_PATH, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json",
                               "X-SMF-QER-Actuator-Token": token})
        status, parsed = None, None
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status, raw = response.status, response.read(65537)
            if len(raw) <= 65536:
                decoded = json.loads(raw)
                parsed = decoded if type(decoded) is dict else None
        except HTTPError as error:
            status = error.code
            self._log_fixture_transport_failure(snapshot, 'fixture_install', error, status)
        except Exception as error:
            self._log_fixture_transport_failure(snapshot, 'fixture_install', error, status)
        try:
            return finish_fixture_install_attempt(journal_path, snapshot, intent_id,
                                                  status, parsed)
        except ConstrainedFixtureBlocked as exc:
            raise GroupQerBlocked("Fixture SMF outcome is not accepted; no resend") from exc

    def request_fixture_restore(self, journal_path, snapshot, intent_id, *,
                                batch_authority, expected_config_sha256,
                                expected_quarantine_registry, _repair=False):
        """Send one exact original-rate POST after its durable restore dispatch."""
        from backend.onprem.validation.restore import reserve_fixture_restore_dispatch, finish_fixture_restore_attempt
        from backend.onprem.validation.constrained import ConstrainedFixtureBlocked
        from backend.onprem.validation import normal as fixture_common
        batch = self._fixture_batches.get(batch_authority)
        if (type(snapshot) is not dict or type(batch) is not dict
                or batch.get("phase") != "fixture_restore"
                or batch.get("journalPath") != str(journal_path)
                or batch.get("requestId") != snapshot.get("requestId")
                or batch.get("configSha256") != expected_config_sha256
                or batch.get("registry") != str(expected_quarantine_registry)
                or not callable(self._round_reader)):
            raise GroupQerBlocked("Original-rate batch authority is unavailable")
        current = self._round_reader()
        self._reject_normal_write(current)
        if (current != snapshot or batch["roundId"] != snapshot.get("roundId")
                or batch["generation"] != snapshot.get("generation")
                or batch["fleetLeaseDigest"] != fixture_common._digest(snapshot.get("fleetLease"))
                or batch["cohortSha256"] != fixture_common.cohort_digest(snapshot.get("perUe"))):
            raise GroupQerBlocked("Original-rate owner changed before dispatch")
        try:
            dispatched = reserve_fixture_restore_dispatch(
                journal_path, snapshot, intent_id, fresh_snapshot_reader=self._round_reader,
                expected_quarantine_registry=expected_quarantine_registry,
                _pin=batch["restorePin"], _repair=_repair)
        except ConstrainedFixtureBlocked as exc:
            raise GroupQerBlocked("Exact original-rate dispatch is unavailable") from exc
        if self._round_reader() != snapshot:
            try:
                finish_fixture_restore_attempt(journal_path, snapshot, intent_id, None, None, _repair=_repair)
            except ConstrainedFixtureBlocked:
                pass
            raise GroupQerBlocked("Original-rate owner changed after dispatch; no resend")
        rates, supi = dispatched["rates"], dispatched["supi"]
        body = {"supi": supi, "uplinkMbrKbps": rates["ul"],
                "downlinkMbrKbps": rates["dl"]}
        req = Request(self.smf_url + QER_PATH, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json",
                               "X-SMF-QER-Actuator-Token": batch["token"]})
        status, parsed = None, None
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status, raw = response.status, response.read(65537)
            if len(raw) <= 65536:
                decoded = json.loads(raw)
                parsed = decoded if type(decoded) is dict else None
        except HTTPError as error:
            status = error.code
            self._log_fixture_transport_failure(snapshot, 'fixture_restore', error, status)
        except Exception as error:
            self._log_fixture_transport_failure(snapshot, 'fixture_restore', error, status)
        try:
            return finish_fixture_restore_attempt(journal_path, snapshot, intent_id,
                                                  status, parsed, _repair=_repair)
        except ConstrainedFixtureBlocked as exc:
            raise GroupQerBlocked("Original-rate SMF outcome is unknown; no resend") from exc

    native_receipt_context = True

    def native(self, snapshot, *, action_id=None, phase=None, _fixture_raw_sink=None):
        """Join current round identities to independent UPF kernel QER/PDR state."""
        if _fixture_raw_sink is not None and (type(_fixture_raw_sink) is not dict or _fixture_raw_sink):
            raise GroupQerBlocked("Fixture raw capture must be empty")
        if (action_id is None) != (phase is None) or (action_id is not None and
                (not isinstance(action_id, str) or not action_id or
                 phase not in {"baseline", "pre_apply", "installed", "pre_restore", "restored"})):
            raise GroupQerBlocked("Native QER action receipt context is invalid")
        rows = snapshot.get("perUe")
        if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
            raise GroupQerBlocked("Round UE identities unavailable")
        names = sorted({row.get("slice") for row in rows})
        if any(name not in SLICES for name in names):
            raise GroupQerBlocked("Unknown round slice")
        def read(name):
            if self.upf_urls is not None:
                try:
                    # Fixed startup origins only; never send the SMF actuator token.
                    req = Request(self.upf_urls[name] + "/v1/upf-state", method="GET")
                    with self.opener.open(req, timeout=self.timeout) as response:
                        raw = response.read(8 * 1024 * 1024 + 1)
                        if response.status != 200 or len(raw) > 8 * 1024 * 1024:
                            raise ValueError
                    return name, json.loads(raw)
                except Exception:
                    raise GroupQerBlocked("Independent UPF loopback read unavailable") from None
            path = (f"/api/v1/namespaces/{self.driver.namespace}/services/"
                    f"http:{self.upf_services[name]}:9081/proxy/v1/upf-state")
            return name, self.driver.kubectl("get", "--raw", path)
        with ThreadPoolExecutor(max_workers=min(2, len(names))) as pool:
            dumps = dict(pool.map(read, names))
        joined, stamps = [], []
        for name in names:
            dump = dumps[name]
            if (not isinstance(dump, dict) or dump.get("schemaVersion") != "cityverse.upf/v1"
                    or dump.get("source") != "gtp5g-netlink" or dump.get("available") is not True
                    or dump.get("upf") != "upf-" + name or dump.get("errors") != []
                    or dump.get("residuals") != [] or not isinstance(dump.get("epoch"), str)
                    or not dump["epoch"] or not isinstance(dump.get("sessions"), list)
                    or not isinstance(dump.get("qers"), list) or not isinstance(dump.get("pdrs"), list)):
                raise GroupQerBlocked("Independent UPF inventory is unavailable")
            try:
                stamp = seconds(dump["observedAt"])
            except (KeyError, ValueError, TypeError):
                raise GroupQerBlocked("UPF observation time invalid") from None
            if not -1 <= self.clock() - stamp <= 15:
                raise GroupQerBlocked("UPF observation stale")
            stamps.append(stamp)
            for row in (r for r in rows if r["slice"] == name):
                session = [s for s in dump["sessions"] if isinstance(s, dict)
                           and s.get("ip") == row.get("ueIp") and str(s.get("pfcpSeid")) == row.get("pfcpSeid")]
                if (len(session) != 1 or session[0].get("evidence") != "kernel-installed-pdr-far"
                        or session[0].get("upf") != row.get("upf")
                        or row.get("upfEpoch") != dump["epoch"]):
                    raise GroupQerBlocked("UPF session/SEID/epoch does not match round")
                seid = row["pfcpSeid"]
                qers = [q for q in dump["qers"] if isinstance(q, dict) and str(q.get("pfcpSeid")) == seid]
                pdrs = [p for p in dump["pdrs"] if isinstance(p, dict) and str(p.get("seid")) == seid]
                if not qers or not pdrs:
                    raise GroupQerBlocked("UPF QER/PDR session is incomplete")
                if (any(type(q.get("id")) is not int or not isinstance(q.get("mbrKbps"), dict)
                        or any(type(q["mbrKbps"].get(d)) is not int
                               or not 0 <= q["mbrKbps"][d] <= 500_000 for d in ("ul", "dl"))
                        or not isinstance(q.get("pdrIds"), list)
                        or any(type(i) is not int for i in q["pdrIds"]) for q in qers)
                        or any(type(p.get("id")) is not int or not isinstance(p.get("qerIds"), list)
                               or any(type(i) is not int for i in p["qerIds"]) for p in pdrs)):
                    raise GroupQerBlocked("UPF QER/PDR fields are malformed")
                qer_ids = {q.get("id") for q in qers}
                pdr_ids = {p.get("id") for p in pdrs}
                if (len(qer_ids) != len(qers) or len(pdr_ids) != len(pdrs)
                        or any(not isinstance(q.get("pdrIds"), list) or not q["pdrIds"]
                               or not set(q["pdrIds"]) <= pdr_ids for q in qers)
                        or any(not isinstance(p.get("qerIds"), list) or not set(p["qerIds"]) <= qer_ids
                               for p in pdrs)
                        or any(set(q["pdrIds"]) != {p["id"] for p in pdrs if q["id"] in p["qerIds"]}
                               for q in qers)):
                    raise GroupQerBlocked("UPF QER/PDR references are inconsistent")
                _ambr_qer(qers)
                joined.append({**{key: row[key] for key in
                           ("supi", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch", "slice", "upf", "sst", "sd")},
                    "qers": [{"id": q["id"], "pdrIds": q["pdrIds"], "mbrKbps": q["mbrKbps"]} for q in qers]})
        observed = datetime.fromtimestamp(min(stamps), timezone.utc).isoformat()
        proof = {"source": "native-upf-qer", "observedAt": observed, "sessions": joined}
        self._record_native(snapshot, proof, action_id=action_id, phase=phase)
        if _fixture_raw_sink is not None:
            _fixture_raw_sink.update(dumps)
        return proof

    def _record_native(self, snapshot, proof, *, action_id=None, phase=None):
        """Persist each independent read before callers can treat it as evidence.

        The QER journal is private and separate from the RoundLedger sequence,
        so a read does not invalidate an in-flight actuator fence.
        """
        if self._journal_path is None:
            return
        identity = {key: snapshot.get(key) for key in
                    ("roundId", "generation", "sequence", "policyRevision")}
        if (not isinstance(identity["roundId"], str) or not identity["roundId"]
                or any(type(identity[key]) is not int or identity[key] < 0
                       for key in ("generation", "sequence", "policyRevision"))):
            raise GroupQerBlocked("Native QER receipt has no exact round fence")
        cohort = snapshot.get("perUe")
        if not isinstance(cohort, list) or not cohort:
            raise GroupQerBlocked("Native QER receipt has no owned cohort")
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False).encode()
        stable = ("supi", "scenario", "slice", "sessionId", "smContextRef", "pfcpSeid",
                  "upfEpoch", "upf", "sst", "sd", "ueIp")
        owners = sorted(({key: row.get(key) for key in stable} for row in cohort),
                        key=lambda row: str(row["supi"]))
        receipt = {**identity, "cohortSha256": hashlib.sha256(canonical(owners)).hexdigest(),
                   "actionId": action_id, "phase": phase, "capturedAt": self.clock(),
                   "proof": proof}
        with closing(sqlite3.connect(self._journal_path)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS native_qer_receipts ("
                           "id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL, "
                           "generation INTEGER NOT NULL, receipt TEXT NOT NULL)")
                db.execute("INSERT INTO native_qer_receipts (round_id, generation, receipt) VALUES (?, ?, ?)",
                           (identity["roundId"], identity["generation"], canonical(receipt).decode()))

    def adapter(self, journal_path, round_reader, profiles):
        if self._journal_path is not None:
            raise GroupQerBlocked("Provider is already bound to one durable group journal")
        self._journal_path = Path(journal_path)
        self._round_reader = round_reader
        self._profiles = dict(profiles)
        ids = tuple(profiles)
        return GroupQerAdapter(journal_path, round_reader=round_reader,
            native_reader=self.native, transport=self,
            capability_reader=lambda snapshot: self.capability(snapshot, ids),
            profiles=profiles, clock=self.clock)
