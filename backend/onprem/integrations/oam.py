"""Native free5GC v4.2.2 AMF/SMF OAM adapter.

Verified sources: AMF 02675d949f207a95379af458c7e80fabe802d728,
SMF 2232d2992f204207e58830ac8ee55bf0cd832f92 plus the historical
read-only inventory-v1 patch (retained in Git history).
SMF OAM does NOT populate Tunnel. This adapter never invents PFCP evidence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import re
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from backend.onprem.runtime.ue import BASE_IMSI, EvidenceUnavailable, timestamp
from backend.onprem.evidence.read_diagnostics import diagnosed, stage, failure


def sm_context_reference(value):
    """Preserve native UUID URNs; absolute API URLs contribute only a path ID.

    Pinned AMF prepends urn:uuid: to its SMF Location path component. Treating
    that opaque identifier as a URL would silently remove the urn scheme.
    The returned value is always quoted under the configured SMF origin.
    """
    if not isinstance(value, str):
        raise EvidenceUnavailable("AMF session lacks a reference")
    if value.startswith(("http://", "https://")):
        value = unquote(urlsplit(value).path.rstrip("/").split("/")[-1])
    if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]*", value):
        raise EvidenceUnavailable("Malformed SMF context reference")
    return value


class NativeOamAdapter:
    def __init__(self, amf_url: str, smf_url: str, token: str = "", fetch=None, reference_db: Path | None = None,
                 prototype_inventory: bool = False, authoritative_inventory: bool = False,
                 inventory_max_age_seconds: float = 30.0, inventory_future_skew_seconds: float = 10.0,
                 wall_time=None, inventory_token: str = ""):
        if prototype_inventory and authoritative_inventory:
            raise ValueError("Prototype and authoritative SMF inventory modes are mutually exclusive")
        self.amf_url, self.smf_url = amf_url.rstrip("/"), smf_url.rstrip("/")
        self.token = token
        self.inventory_token = inventory_token
        self.fetch = fetch or self._fetch
        self.reference_db = reference_db
        self.prototype_inventory = prototype_inventory
        self.authoritative_inventory = authoritative_inventory
        self.inventory_max_age_seconds = float(inventory_max_age_seconds)
        self.inventory_future_skew_seconds = float(inventory_future_skew_seconds)
        self.wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._references = {}
        if reference_db is not None:
            reference_db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(reference_db) as database:
                database.execute("CREATE TABLE IF NOT EXISTS smf_refs (origin TEXT, ref TEXT, supi TEXT, session_id TEXT, PRIMARY KEY(origin, ref))")

    def _known(self):
        if self.reference_db is None:
            return dict(self._references)
        with sqlite3.connect(self.reference_db) as database:
            return {ref: (supi, session) for ref, supi, session in database.execute("SELECT ref,supi,session_id FROM smf_refs WHERE origin=?", (self.smf_url,))}

    def _remember(self, ref, supi, session):
        if self.reference_db is None:
            self._references[ref] = (supi, session)
        else:
            with sqlite3.connect(self.reference_db) as database:
                database.execute("INSERT OR REPLACE INTO smf_refs VALUES (?,?,?,?)", (self.smf_url, ref, supi, session))

    def _forget(self, ref):
        if self.reference_db is None:
            self._references.pop(ref, None)
        else:
            with sqlite3.connect(self.reference_db) as database:
                database.execute("DELETE FROM smf_refs WHERE origin=? AND ref=?", (self.smf_url, ref))

    @diagnosed('oam')
    def _fetch(self, url):
        stage('request')
        try:
            request = Request(url, headers=self._authorization_headers(url))
            with urlopen(request, timeout=5) as response:
                stage('json_decode')
                return json.load(response)
        except HTTPError as exc:
            # Pinned SMF OAM returns JSON null (not generic router text) for a
            # missing reference. Only this exact response proves its release.
            if exc.code == 404 and "/ue-pdu-session-info/" in url and exc.read().strip() == b"null":
                return None
            failure(exc, http_status=exc.code)
            raise EvidenceUnavailable("Native AMF/SMF OAM request failed") from None
        except Exception as error:
            failure(error)
            raise EvidenceUnavailable("Native AMF/SMF OAM request failed") from None

    def _authorization_headers(self, url):
        inventory_url = f"{self.smf_url}/nsmf-oam/v1/sm-contexts"
        if url == inventory_url and self.inventory_token:
            return {"Authorization": f"Bearer {self.inventory_token}"}
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def collect_registered_devices(self):
        """AMF registration only; never flatten multiple PDU sessions by SUPI.

        The device observer joins its independent atomic native SMF census.
        This port makes no SMF completeness or session-establishment claim.
        """
        contexts=self.fetch(f"{self.amf_url}/namf-oam/v1/registered-ue-context")
        observed=timestamp()
        if contexts is None:
            contexts=[]
        if (not isinstance(contexts,list) or len(contexts)>4096
                or any(not isinstance(row,dict) or not isinstance(row.get('Supi'),str)
                       or row.get('AccessType')!='3GPP_ACCESS' for row in contexts)
                or len({row['Supi'] for row in contexts})!=len(contexts)):
            raise EvidenceUnavailable('Complete AMF device registration schema unavailable')
        return {'ues':[{'supi':row['Supi'],'amf':{'registered':True,'observedAt':observed}}
                       for row in contexts],
                'sources':{'amf':{'available':True,'observedAt':observed,
                    'source':'free5gc-v4.2.2:namf-oam/v1','contextCount':len(contexts),
                    'scope':'all-native-amf-registered-contexts'}}}

    @diagnosed('oam')
    def collect(self, *, cancel=None, deadline=None):
        """Read current registered identities and join real SMF contexts.

        Restrict SMF requests to the configured origin; never follow an AMF
        supplied arbitrary URL. Retain all managed pool SUPIs, including stale
        generations, so a scale-down cannot hide residual registrations.
        """
        deadline = deadline or time.monotonic() + 12
        stage('deadline', deadline=deadline)
        if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
            raise EvidenceUnavailable("Native OAM collection cancelled or timed out")
        if not self.amf_url or not self.smf_url:
            raise EvidenceUnavailable("AMF and SMF OAM URLs are required")
        stage('schema_validate', kind='smf_health')
        health = self.fetch(f"{self.smf_url}/nsmf-oam/v1/")
        if not isinstance(health, dict) or health.get("status") != "Service Available":
            raise EvidenceUnavailable("Native SMF OAM capability probe failed")
        smf_capability_observed = timestamp()
        inventory_by_ref = {}
        inventory_metadata = None
        inventory_received_at = None
        if self.prototype_inventory or self.authoritative_inventory:
            stage('schema_validate', kind='smf_inventory_open')
            inventory = self.fetch(f"{self.smf_url}/nsmf-oam/v1/sm-contexts")
            # Consumer freshness is measured at receipt on this host. The SMF
            # producer's linearization timestamp remains separately exposed.
            inventory_received_at = timestamp()
            if self.authoritative_inventory:
                inventory_by_ref, inventory_metadata = self._validate_authoritative_inventory(inventory)
            else:
                inventory_by_ref = self._validate_inventory(inventory)
        stage('schema_validate', kind='amf_contexts')
        contexts = self.fetch(f"{self.amf_url}/namf-oam/v1/registered-ue-context")
        amf_observed = timestamp()
        # Pinned AMF declares a nil UEContexts slice; HTTP 200 serializes its
        # empty registered pool as JSON null, not []. HTTP failures still raise.
        if contexts is None:
            contexts = []
        if not isinstance(contexts, list):
            raise EvidenceUnavailable("AMF OAM returned an incompatible schema")
        native_amf_context_count=len(contexts)  # Before managed-SUPI/access projection.
        pool = {f"imsi-{BASE_IMSI + index}" for index in range(1000)}
        contexts = [item for item in contexts if item.get("Supi") in pool and item.get("AccessType") == "3GPP_ACCESS"]
        if len(contexts) > 1000 or len({item['Supi'] for item in contexts}) != len(contexts):
            raise EvidenceUnavailable("Duplicate or excessive managed AMF contexts")
        if self.authoritative_inventory:
            unknown = sorted({str(item["Supi"]) for item in inventory_by_ref.values()} - pool)
            if unknown:
                raise EvidenceUnavailable("Authoritative SMF inventory contains an unknown residual identity")
        stage('reference_join')
        active_refs = set()
        for context in contexts:
            for session in context.get("PduSessions") or []:
                ref = sm_context_reference(session.get("SmContextRef", ""))
                if not ref:
                    raise EvidenceUnavailable("AMF session lacks a reference")
                active_refs.add(ref)
        remembered = [(self.smf_url, ref, data["Supi"], str(data["PDUSessionID"]))
                      for ref, data in inventory_by_ref.items()]
        remembered.extend((self.smf_url, ref, context["Supi"], str(session["PduSessionId"]))
                          for context in contexts for session in context.get("PduSessions") or []
                          if (ref := sm_context_reference(session.get("SmContextRef", ""))))
        durable = self._known()
        for _, ref, supi, session in remembered:
            if ref in durable and durable[ref] != (supi, session):
                raise EvidenceUnavailable("SMF context reference changed durable identity")
        stage('reference_persist')
        if self.reference_db is None:
            self._references.update({ref: (supi, session) for _, ref, supi, session in remembered})
        else:
            with sqlite3.connect(self.reference_db) as database:
                database.executemany("INSERT OR REPLACE INTO smf_refs VALUES (?,?,?,?)", remembered)
        # The prototype Range inventory is not atomic. It may add release
        # obligations, but never removes an old one. Only the individual SMF
        # endpoint's exact native 404 can discharge a remembered reference.
        residual_contexts = [{"Supi": supi, "PduSessions": [{"SmContextRef": ref, "PduSessionId": session}], "_residual": True}
                             for ref, (supi, session) in self._known().items() if ref not in active_refs]
        with ThreadPoolExecutor(max_workers=32) as executor:
            @diagnosed('oam')
            def collect_context(context):
                stage('context_schema', kind='smf_context', deadline=deadline)
                if cancel is not None and cancel.is_set() or time.monotonic() >= deadline:
                    raise EvidenceUnavailable("Native OAM collection cancelled or timed out")
                row = (self._context(context, inventory_by_ref=inventory_by_ref,
                                     inventory_observed_at=inventory_received_at)
                       if self.authoritative_inventory else self._context(context))
                if row is not None:
                    row['amf']['observedAt'] = amf_observed
                return row
            futures = [executor.submit(collect_context, item) for item in [*contexts, *residual_contexts]]
            stage('context_wait', requestedCount=len(futures))
            try:
                rows = [row for future in as_completed(futures, timeout=max(0.01, deadline - time.monotonic())) if (row := future.result()) is not None]
            except Exception:
                if cancel is not None:
                    cancel.set()
                for future in futures:
                    future.cancel()
                raise EvidenceUnavailable("Native OAM evidence collection failed or exceeded deadline") from None
        stage('deadline')
        if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
            raise EvidenceUnavailable("Native OAM collection cancelled or timed out")
        if self.authoritative_inventory:
            stage('schema_validate', kind='smf_inventory_close')
            closing_inventory = self.fetch(f"{self.smf_url}/nsmf-oam/v1/sm-contexts")
            closing_contexts, closing_metadata = self._validate_authoritative_inventory(closing_inventory)
            stage('boot_epoch_check')
            if closing_metadata["bootEpoch"] != inventory_metadata["bootEpoch"]:
                raise EvidenceUnavailable("SMF boot epoch changed during evidence collection")
            stage('inventory_seal')
            if closing_contexts != inventory_by_ref:
                raise EvidenceUnavailable("Authoritative SMF inventory changed during evidence collection")
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                raise EvidenceUnavailable("Native OAM collection cancelled or timed out")
        observed = min((row['smf'].get('observedAt', smf_capability_observed) for row in rows), default=smf_capability_observed)
        if self.authoritative_inventory:
            observed = inventory_received_at
        return {"ues": rows, "sources": {
            "amf": {"available": True, "observedAt": amf_observed, "source": "free5gc-v4.2.2:namf-oam/v1",
                    "contextCount":native_amf_context_count,"scope":"all-native-amf-registered-contexts"},
            "smf": {"available": True, "observedAt": observed, "source": "free5gc-v4.2.2:nsmf-oam/v1",
                    "inventoryComplete": bool(self.authoritative_inventory),
                    **({"schemaVersion": "cityverse.smf-oam/v2",
                        "consistency": "atomic-membership-and-projection",
                        "bootEpoch": inventory_metadata["bootEpoch"],
                        "nativeObservedAt": inventory_metadata["observedAt"],
                        "contextCount": len(inventory_by_ref)} if self.authoritative_inventory else
                       ({"schemaVersion": "cityverse.smf-oam/v1", "contextCount": len(inventory_by_ref),
                         "limitation": "Prototype SMF inventory uses non-atomic sync.Map.Range; zero is not proof of absence"}
                        if self.prototype_inventory else
                        {"limitation": "Pinned SMF exposes individual references but no complete context inventory"}))}}}

    @staticmethod
    def _parse_inventory_time(value):
        if not isinstance(value, str) or not value.endswith("Z"):
            raise EvidenceUnavailable("SMF context inventory observation time is invalid")
        # Go emits RFC3339Nano while datetime accepts at most microseconds.
        match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z", value)
        if not match:
            raise EvidenceUnavailable("SMF context inventory observation time is invalid")
        fraction = ((match.group(2) or "") + "000000")[:6]
        try:
            return datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")
        except ValueError:
            raise EvidenceUnavailable("SMF context inventory observation time is invalid") from None

    def _validate_authoritative_inventory(self, inventory):
        if (not isinstance(inventory, dict)
                or inventory.get("SchemaVersion") != "cityverse.smf-oam/v2"
                or inventory.get("InventoryComplete") is not True
                or inventory.get("Consistency") != "atomic-membership-and-projection"):
            raise EvidenceUnavailable("Authoritative SMF context inventory schema is unavailable or incompatible")
        try:
            epoch = uuid.UUID(inventory.get("BootEpoch", ""))
        except (AttributeError, TypeError, ValueError):
            raise EvidenceUnavailable("Authoritative SMF boot epoch is invalid") from None
        if epoch.version != 4 or str(epoch) != inventory.get("BootEpoch", "").lower():
            raise EvidenceUnavailable("Authoritative SMF boot epoch is invalid")
        observed = self._parse_inventory_time(inventory.get("ObservedAt"))
        now = self.wall_time()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if age > self.inventory_max_age_seconds or age < -self.inventory_future_skew_seconds:
            raise EvidenceUnavailable("Authoritative SMF context inventory is stale or future-dated")
        contexts = self._validate_inventory_entries(inventory, "Authoritative SMF")
        return contexts, {"bootEpoch": str(epoch), "observedAt": inventory["ObservedAt"]}

    @staticmethod
    def _validate_inventory(inventory):
        if (not isinstance(inventory, dict) or inventory.get("SchemaVersion") != "cityverse.smf-oam/v1"
                or inventory.get("InventoryComplete") is not False):
            raise EvidenceUnavailable("SMF context inventory schema is unavailable or incompatible")
        return NativeOamAdapter._validate_inventory_entries(inventory, "SMF")

    @staticmethod
    def _validate_inventory_entries(inventory, label):
        count, contexts = inventory.get("Count"), inventory.get("Contexts")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(contexts, list) or count != len(contexts):
            raise EvidenceUnavailable(f"{label} context inventory count is invalid")
        result = {}
        identities = set()
        supis = set()
        required = {"SmContextRef", "Supi", "PDUSessionID", "Dnn", "Sst", "Sd", "AnType", "PDUAddress", "UpCnxState"}
        for data in contexts:
            if not isinstance(data, dict) or not required.issubset(data):
                raise EvidenceUnavailable(f"{label} context inventory entry is incomplete")
            ref = sm_context_reference(data["SmContextRef"])
            identity = (data["Supi"], str(data["PDUSessionID"]))
            if ref in result or identity in identities or not isinstance(data["Supi"], str) or not data["Supi"]:
                raise EvidenceUnavailable(f"{label} context inventory contains duplicate or invalid identity")
            if label.startswith("Authoritative") and data["Supi"] in supis:
                raise EvidenceUnavailable("Authoritative SMF inventory cannot represent multiple sessions for one identity")
            result[ref] = data
            identities.add(identity)
            supis.add(data["Supi"])
        return result

    def _context(self, context, *, inventory_by_ref=None, inventory_observed_at=None):
        supi = context["Supi"]
        sessions = context.get("PduSessions") or []
        row = {"supi": supi, "amf": {"registered": not context.get("_residual", False), "observedAt": timestamp()}, "smf": {"sessionCount": len(sessions)}}
        if len(sessions) != 1:
            return row
        reference = sessions[0].get("SmContextRef", "")
        reference = sm_context_reference(reference)
        if not reference:
            raise EvidenceUnavailable("AMF session lacks SMF context reference")
        # The explicit v2 producer atomically projects these same native fields.
        # Reuse only this collection's validated snapshot, whose membership,
        # projection and boot epoch are checked again before returning. Missing
        # references still require an individual native 404 to retire a durable
        # obligation; a missing inventory entry alone never proves absence.
        projected = inventory_by_ref is not None and reference in inventory_by_ref
        data = (inventory_by_ref[reference] if projected else
                self.fetch(f"{self.smf_url}/nsmf-oam/v1/ue-pdu-session-info/{quote(reference, safe='')}"))
        if data is None:
            self._forget(reference)
            return None if context.get("_residual") else row
        if not isinstance(data, dict) or data.get("Supi") != supi or str(data.get("PDUSessionID")) != str(sessions[0].get("PduSessionId")):
            raise EvidenceUnavailable("SMF OAM identity/session mismatch")
        row["smf"].update({"sessionId": str(data["PDUSessionID"]), "sst": int(data["Sst"]), "sd": data["Sd"],
                           "dnn": data["Dnn"], "ip": data["PDUAddress"], "established": data.get("UpCnxState") == "ACTIVATED",
                            "smContextRef": reference,
                            "observedAt": inventory_observed_at if projected else timestamp()})
        return row


class CombinedEvidenceAdapter:
    """Use native OAM for AMF/SMF; independent collector proves UPF and UE path."""
    def __init__(self, native: NativeOamAdapter, data_plane):
        self.native, self.data_plane = native, data_plane

    def observe(self, generation):
        data = self.data_plane.observe(generation)
        native = self.native.collect()
        native_rows = {row["supi"]: row for row in native["ues"]}
        rows = {row["supi"]: row for row in data["ues"]}
        if len(rows) != len(data["ues"]) or len(native_rows) != len(native["ues"]):
            raise EvidenceUnavailable("Duplicate identity in native/collector data")
        for supi in set(rows) | set(native_rows):
            row = rows.setdefault(supi, {"supi": supi})
            nf = native_rows.get(supi, {})
            row["amf"] = nf.get("amf", {"registered": False})
            row["smf"] = nf.get("smf", {})
            # UPF identity comes from independent PFCP/session evidence, not
            # S-NSSAI-to-UPF desired configuration.
            upf = row.get("upf", {})
            if upf.get("sessionId") == row["smf"].get("sessionId") and upf.get("ip") == row["smf"].get("ip"):
                row["smf"]["upf"] = upf.get("upf")
        data["ues"] = list(rows.values())
        data["sources"].update(native["sources"])
        return data
