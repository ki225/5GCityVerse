"""Independent free-ran-ue Pod TUN and N6 evidence, bound to owned SUPIs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event
import ipaddress
import json
import re
import shlex
import subprocess
import time

from backend.onprem.runtime.ue import EvidenceUnavailable, timestamp
from backend.onprem.evidence.free_ran_read_transport import ping_command, LocalCriUeReadTransport
from backend.onprem.evidence.read_diagnostics import diagnosed, stage, failure

PDU_COMPLETE = re.compile(r"\bUE ([0-9]{15}) PDU session establishment complete\b")


class FreeRanTunProber:
    def __init__(self, driver, runner=subprocess.run, *, read_transport=None):
        self.driver, self.runner, self.read_transport = driver, runner, read_transport

    def _run(self, *args, timeout=12):
        result = self.runner(["kubectl", "--context", self.driver.context,
                              "--namespace", self.driver.namespace, *args],
                             text=True, capture_output=True, timeout=timeout, check=False)
        if result.returncode or len(result.stdout.encode()) > 4 * 1024 * 1024:
            raise EvidenceUnavailable("Free-RAN UE probe command failed or exceeded output bound")
        return result.stdout

    @diagnosed('ue_tun')
    def probe_pod(self, pod: dict, requests: list[dict], *, cancel: Event,
                  deadline: float, fanout: int = 64):
        if not 1 <= fanout <= 64 or not 1 <= len(requests) <= 250:
            raise EvidenceUnavailable("Invalid UE probe batch")
        meta = pod.get("metadata", {})
        name, uid = meta.get("name"), meta.get("uid")
        labels = meta.get("labels", {})
        stage('input_owner', deadline=deadline, requestedCount=len(requests))
        if (not isinstance(name, str) or not isinstance(uid, str) or not uid
                or labels.get("cityverse.io/managed-by") != "onprem-ue-controller"
                or not re.fullmatch(r"[1-9][0-9]*", labels.get("cityverse.io/generation", ""))
                or meta.get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running"):
            raise EvidenceUnavailable("Free-RAN UE Pod ownership unavailable")
        try:
            owned = json.loads(meta.get("annotations", {}).get("cityverse.io/supis", "[]"))
        except (TypeError, ValueError) as error:
            failure(error)
            raise EvidenceUnavailable("Owned SUPI annotation malformed") from None
        if (not isinstance(owned, list) or not 1 <= len(owned) <= 250
                or len(set(owned)) != len(owned)
                or any(not isinstance(s, str) or not re.fullmatch(r"imsi-[0-9]{15}", s)
                       for s in owned)):
            raise EvidenceUnavailable("Owned SUPI list malformed")
        if any(not isinstance(row, dict) for row in requests):
            raise EvidenceUnavailable("Probe request malformed")
        requested = {row.get("supi") for row in requests}
        if len(requested) != len(requests) or not requested.issubset(owned):
            raise EvidenceUnavailable("Probe request does not match owned SUPIs")
        if cancel.is_set() or time.monotonic() >= deadline:
            raise EvidenceUnavailable("Free-RAN UE probe cancelled")
        def remaining(maximum):
            budget = deadline-time.monotonic()
            if cancel.is_set() or budget <= 0:
                raise EvidenceUnavailable("Free-RAN UE probe cancelled")
            return min(maximum, budget)
        stage('logs_read', generation=int(labels['cityverse.io/generation']))
        started = time.time()
        budget = remaining(30)
        logs = (self.read_transport.execute('logs', pod, timeout=budget) if self.read_transport else
                self._run("logs", name, "-c", "ue", timeout=budget))
        stage('logs_validate')
        completed = ["imsi-" + value for value in PDU_COMPLETE.findall(logs)]
        if any(completed.count(supi) != 1 for supi in requested):
            raise EvidenceUnavailable("Per-SUPI UE PDU completion log missing or duplicated")
        stage('addresses_read')
        budget = remaining(12)
        raw = (self.read_transport.execute('addresses', pod, timeout=budget) if self.read_transport else
               self._run("exec", name, "-c", "ue", "--", "ip", "-j", "addr", "show", timeout=budget))
        stage('addresses_decode')
        try:
            addresses = json.loads(raw)
        except (TypeError, ValueError) as error:
            failure(error)
            raise EvidenceUnavailable("UE interface readback malformed") from None
        if not isinstance(addresses, list):
            raise EvidenceUnavailable("UE interface readback malformed")
        stage('binding_match')
        bindings = {}
        for row in requests:
            try:
                supi, smf, endpoint = row["supi"], row["smf"], row["endpoint"]
            except KeyError:
                raise EvidenceUnavailable("Probe request malformed") from None
            if not isinstance(smf, dict):
                raise EvidenceUnavailable("Native SMF session malformed")
            if smf.get("established") is not True or not isinstance(smf.get("sessionId"), str) or not smf["sessionId"].isdigit():
                raise EvidenceUnavailable("Native SMF session not established")
            try:
                ipaddress.IPv4Address(smf["ip"])
                ipaddress.IPv4Address(endpoint)
            except (KeyError, TypeError, ValueError):
                raise EvidenceUnavailable("Native SMF or N6 IPv4 address malformed") from None
            interface = "ueTun" + str(owned.index(supi))
            matches = [item for item in addresses if item.get("ifname") == interface
                       and any(addr.get("local") == smf["ip"] for addr in item.get("addr_info", []))]
            if len(matches) != 1:
                raise EvidenceUnavailable("Free-RAN UE TUN/IP does not match owned SUPI and native SMF")
            bindings[supi] = (interface, smf, endpoint)
        stage('ping_read')
        budget = remaining(12)
        output = (self.read_transport.execute('ping', pod, bindings=bindings, fanout=fanout, timeout=budget)
                  if self.read_transport else self._run("exec", name, "-c", "ue", "--",
                                                       *ping_command(bindings, fanout), timeout=budget))
        stage('ping_decode')
        outcomes = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if (len(fields) != 3 or fields[0] in outcomes or fields[0] not in bindings
                    or fields[1] != bindings[fields[0]][0] or fields[2] not in {"0", "1"}):
                raise EvidenceUnavailable("Free-RAN UE ping readback malformed")
            outcomes[fields[0]] = fields[2] == "0"
        stage('population_check', returnedCount=len(outcomes))
        if set(outcomes) != requested or cancel.is_set() or time.monotonic() >= deadline:
            raise EvidenceUnavailable("Free-RAN UE ping readback incomplete or expired")
        read_pod = getattr(self.driver, 'read_pod', None)
        stage('pod_postflight')
        if self.read_transport is not None and type(self.read_transport) is LocalCriUeReadTransport:
            after = self.read_transport._pod(name, deadline=deadline)
        else:
            # Legacy/custom adapters keep their existing identity check; only
            # the trusted local port promises deadline propagation.
            after = (read_pod(name) if callable(read_pod) else
                     self.driver.kubectl("get", "pod", name, "-o", "json"))
        if cancel.is_set() or time.monotonic() >= deadline:
            raise EvidenceUnavailable("Free-RAN UE ping readback incomplete or expired")
        if (after.get("metadata", {}).get("uid") != uid
                or after.get("metadata", {}).get("annotations", {}).get("cityverse.io/supis")
                   != meta.get("annotations", {}).get("cityverse.io/supis")
                or after.get("metadata", {}).get("deletionTimestamp")):
            raise EvidenceUnavailable("Free-RAN UE Pod changed during probe")
        # Preserve the earliest capture time; never make a slow probe appear
        # fresher by stamping it only after all subprocesses return.
        observed = timestamp(started)
        result = []
        for supi, (interface, smf, endpoint) in bindings.items():
            common = {"podUid": uid, "sessionId": smf["sessionId"], "observedAt": observed}
            success = outcomes[supi]
            result.append({"supi": supi, "generation": int(labels["cityverse.io/generation"]),
                           "ue": {**common, "running": True, "tun": interface, "ip": smf["ip"]},
                           "traffic": {**common, "success": success, "interface": interface,
                                       "sourceIp": smf["ip"], "endpoint": endpoint,
                                       "transmitted": 1, "received": int(success)}})
        return result

    @diagnosed('ue_tun')
    def probe_many(self, batches, *, cancel: Event | None = None,
                   deadline: float | None = None, workers: int = 8):
        if not 1 <= workers <= 16:
            raise ValueError("Probe worker limit must be between 1 and 16")
        cancel = cancel or Event()
        deadline = deadline or time.monotonic() + 12
        stage('batch_wait', deadline=deadline)
        result = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self.probe_pod, pod, rows, cancel=cancel, deadline=deadline)
                       for pod, rows in batches if rows]
            try:
                for future in as_completed(futures, timeout=max(0.01, deadline - time.monotonic())):
                    result.extend(future.result())
            except Exception:
                cancel.set()
                for future in futures:
                    future.cancel()
                raise EvidenceUnavailable("Free-RAN UE batched probe failed") from None
        return result
