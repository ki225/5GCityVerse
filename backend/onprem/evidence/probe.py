"""Actual UE-network-namespace probes; no backend-generated latency/bytes."""
from __future__ import annotations

import ipaddress
import base64
import re
import subprocess
import json
import shlex
import time
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event

from backend.onprem.runtime.kubeadm import KubeadmDriver
from backend.onprem.runtime.ue import EvidenceUnavailable, timestamp


def fresh_probe_epoch(observed: float, started: float, now: float) -> bool:
    """Bound cross-host skew without accepting stale UE probe timestamps."""
    return started - 1 <= observed <= now + 5 and now - observed <= 15


class TunProber:
    def __init__(self, driver: KubeadmDriver, runner=subprocess.run):
        self.driver, self.runner = driver, runner

    def _exec(self, pod, *command):
        result = self.runner(["kubectl", "--context", self.driver.context, "--namespace", self.driver.namespace,
                              "exec", pod, "-c", "ue", "--", *command],
                             text=True, capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise EvidenceUnavailable("UE namespace probe command failed")
        return result.stdout

    def probe(self, pod: dict, supi: str, smf: dict, endpoint: str):
        import json
        if smf.get("established") is not True or not smf.get("sessionId"):
            raise EvidenceUnavailable("PDU session must be established before probing")
        ipaddress.IPv4Address(endpoint)  # fixed operator-configured N6 endpoint, no command flags
        ipaddress.IPv4Address(smf["ip"])
        metadata = pod["metadata"]
        name = metadata["name"]
        if metadata.get("labels", {}).get("cityverse.io/managed-by") != "onprem-ue-controller":
            raise EvidenceUnavailable("Probe target is not an owned UE pod")
        if supi not in json.loads(metadata.get("annotations", {}).get("cityverse.io/supis", "[]")):
            raise EvidenceUnavailable("SUPI does not belong to the target pod")
        # nr-cli enumerates actual UE nodes; annotation alone is not proof.
        nodes = self._exec(name, "/ueransim/nr-cli", "--dump")
        if supi not in nodes.split():
            raise EvidenceUnavailable("UERANSIM node is not running for the requested SUPI")
        addresses = json.loads(self._exec(name, "ip", "-j", "addr", "show"))
        interfaces = [item["ifname"] for item in addresses
                      if re.fullmatch(r"uesimtun[0-9]+", item.get("ifname", ""))
                      and any(addr.get("local") == smf["ip"] for addr in item.get("addr_info", []))]
        if len(interfaces) != 1:
            raise EvidenceUnavailable("PDU IP does not identify exactly one UE TUN interface")
        interface = interfaces[0]
        output = self._exec(name, "env", "LC_ALL=C", "ping", "-n", "-I", interface, "-c", "1", "-W", "2", endpoint)
        received = re.search(r"1 packets transmitted,\s*1 (?:packets )?received", output)
        if not received:
            raise EvidenceUnavailable("UE TUN probe did not receive a reply")
        common = {"podUid": metadata["uid"], "sessionId": smf["sessionId"], "observedAt": timestamp()}
        return {"ue": {**common, "running": True, "tun": interface, "ip": smf["ip"]},
                "traffic": {**common, "success": True, "interface": interface, "sourceIp": smf["ip"],
                            "endpoint": endpoint, "transmitted": 1, "received": 1}}

    def probe_pod(self, pod: dict, requests: list[dict], *, cancel: Event, deadline: float, fanout: int = 64):
        """Two exec calls per Pod, irrespective of its (at most 250) UE count.

        Inspect actual nr-cli nodes and interfaces once. Ping processes are
        bounded inside the UE namespace; only their actual exit results count.
        No Python/jq installation is assumed in the upstream UERANSIM image.
        """
        if not 1 <= fanout <= 64 or not 0 < len(requests) <= 250:
            raise EvidenceUnavailable("Invalid UE probe batch")
        metadata = pod["metadata"]
        name, uid = metadata["name"], metadata["uid"]
        labels = metadata.get("labels", {})
        if labels.get("cityverse.io/managed-by") != "onprem-ue-controller" or not isinstance(uid, str) or not uid:
            raise EvidenceUnavailable("Probe Pod ownership is unavailable")
        generation = int(labels["cityverse.io/generation"])
        owned = json.loads(metadata.get("annotations", {}).get("cityverse.io/supis", "[]"))
        if len(set(owned)) != len(owned):
            raise EvidenceUnavailable("Duplicate Pod SUPI ownership")
        started = time.time()

        def execute(script):
            if cancel.is_set() or time.monotonic() >= deadline:
                raise EvidenceUnavailable("Probe generation cancelled or timed out")
            remaining = max(0.01, min(12, deadline - time.monotonic()))
            result = self.runner(["kubectl", "--context", self.driver.context, "--namespace", self.driver.namespace,
                                  "exec", name, "-c", "ue", "--", "sh", "-c", script],
                                 text=True, capture_output=True, timeout=remaining, check=False)
            if cancel.is_set() or time.monotonic() >= deadline or result.returncode:
                raise EvidenceUnavailable("Batched UE probe cancelled, failed or timed out")
            return result.stdout

        discovery_commands = ["set -eu", "/ueransim/nr-cli --dump", "printf '\\n__CITYVERSE_ADDR__\\n'", "ip -j addr show", "printf '\\n__CITYVERSE_SESSIONS__\\n'"]
        for index, request in enumerate(requests):
            supi = request["supi"]
            if not isinstance(supi, str) or not re.fullmatch(r"imsi-[0-9]{15}", supi):
                raise EvidenceUnavailable("Invalid requested SUPI")
            discovery_commands.append(f"(value=$(/ueransim/nr-cli {shlex.quote(supi)} --exec ps-list); printf '%s\\t%s\\n' {shlex.quote(supi)} \"$(printf '%s' \"$value\" | base64 | tr -d '\\n')\") &")
            if (index + 1) % 32 == 0:
                discovery_commands.append("wait")
        discovery_commands.append("wait")
        discovery_commands.append("printf '\n__CITYVERSE_PINGS__\n'")
        for index, request in enumerate(requests):
            supi, ip, endpoint = request["supi"], request["smf"]["ip"], request["endpoint"]
            ipaddress.IPv4Address(ip)
            ipaddress.IPv4Address(endpoint)
            lookup = f"ip -o -4 addr show | awk '$4 ~ /^{re.escape(ip)}\\// {{print $2}}'"
            discovery_commands.append(
                f"(interface=$({lookup}); if [ -n \"$interface\" ] && "
                f"ping -n -I \"$interface\" -c 1 -W 2 {shlex.quote(endpoint)} >/dev/null 2>&1; "
                "then code=0; else code=1; fi; "
                f"printf '%s\\t%s\\t%s\\t%s\\n' {shlex.quote(supi)} \"$interface\" \"$code\" \"$(date +%s)\") &")
            if (index + 1) % fanout == 0:
                discovery_commands.append("wait")
        discovery_commands.append("wait")
        discovery = execute("\n".join(discovery_commands))
        parts = discovery.split("\n__CITYVERSE_ADDR__\n")
        if len(parts) != 2:
            raise EvidenceUnavailable("Invalid UE discovery response")
        nodes = parts[0].split()
        if len(nodes) != len(set(nodes)) or any(node not in owned for node in nodes):
            raise EvidenceUnavailable("Unexpected or duplicate UERANSIM node in owned Pod")
        tail = parts[1].split("\n__CITYVERSE_SESSIONS__\n")
        if len(tail) != 2:
            raise EvidenceUnavailable("Missing per-SUPI session discovery")
        addresses = json.loads(tail[0])
        session_and_ping = tail[1].split("\n__CITYVERSE_PINGS__\n")
        if len(session_and_ping) != 2:
            raise EvidenceUnavailable("Missing per-SUPI TUN probe response")
        ue_sessions = {}
        for line in session_and_ping[0].splitlines():
            record = line.split("\t")
            if len(record) != 2 or record[0] in ue_sessions:
                raise EvidenceUnavailable("Duplicate or malformed UE session response")
            ue_sessions[record[0]] = yaml.safe_load(base64.b64decode(record[1], validate=True))
        if set(ue_sessions) != {request["supi"] for request in requests}:
            raise EvidenceUnavailable("Missing or unexpected per-SUPI session responses")
        matched = {}
        for request in requests:
            supi, smf, endpoint = request["supi"], request["smf"], request["endpoint"]
            if not re.fullmatch(r"imsi-[0-9]{15}", supi) or supi not in owned or nodes.count(supi) != 1 or supi in matched:
                raise EvidenceUnavailable("SUPI does not uniquely match a running Pod node")
            if smf.get("established") is not True or not isinstance(smf.get("sessionId"), str) or not smf["sessionId"].isdigit():
                raise EvidenceUnavailable("PDU session is not established")
            observed_sessions = ue_sessions[supi]
            expected_name = f"PDU Session{smf['sessionId']}"
            if not isinstance(observed_sessions, dict) or set(observed_sessions) != {expected_name}:
                raise EvidenceUnavailable("UE does not own exactly the expected PDU session")
            session = observed_sessions[expected_name]
            snssai = session.get("s-nssai", {})
            sd = snssai.get("sd")
            if isinstance(sd, str):
                sd = int(sd, 16)
            if (session.get("state") != "PS-ACTIVE" or session.get("session-type") != "IPv4"
                    or session.get("address") != smf["ip"] or session.get("apn") != smf.get("dnn")
                    or type(snssai.get("sst")) is not int or snssai["sst"] != smf.get("sst")
                    or type(sd) is not int or sd != int(smf["sd"], 16)):
                raise EvidenceUnavailable("UE PDU identity/address/DNN/slice does not match native SMF")
            ipaddress.IPv4Address(endpoint)
            ipaddress.IPv4Address(smf["ip"])
            interfaces = [entry["ifname"] for entry in addresses if re.fullmatch(r"uesimtun[0-9]+", entry.get("ifname", ""))
                          and any(address.get("local") == smf["ip"] for address in entry.get("addr_info", []))]
            if len(interfaces) != 1 or interfaces[0] in {row["interface"] for row in matched.values()}:
                raise EvidenceUnavailable("PDU IP does not uniquely identify its UE TUN")
            matched[supi] = {"interface": interfaces[0], "smf": smf, "endpoint": endpoint,
                             "ueSessionId": next(iter(observed_sessions)).removeprefix("PDU Session"), "ueIp": session["address"]}
        result = []
        seen = set()
        for line in session_and_ping[1].splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                raise EvidenceUnavailable("Invalid per-UE probe response")
            supi, interface, code, epoch = parts
            if supi not in matched or supi in seen or interface != matched[supi]["interface"] or code not in {"0", "1"} or not epoch.isdigit():
                raise EvidenceUnavailable("Probe response identity mismatch")
            observed = float(epoch)
            # The UE command runs on the RAN VM while verification runs on the
            # Windows host.  NTP-synchronised clocks can still cross the old
            # one-second future boundary; keep the same bounded 2s allowance
            # used by native UPF evidence.
            if not fresh_probe_epoch(observed, started, time.time()):
                raise EvidenceUnavailable("Probe timestamp is stale or future dated")
            seen.add(supi)
            row = matched[supi]
            # POSIX date has whole-second resolution. The operation began at
            # started, so use that lower bound when truncation crosses intent.
            common = {"podUid": uid, "sessionId": row["ueSessionId"], "observedAt": timestamp(max(started, observed))}
            result.append({"supi": supi, "generation": generation,
                           "ue": {**common, "running": True, "tun": interface, "ip": row["ueIp"]},
                           "traffic": {**common, "success": code == "0", "interface": interface, "sourceIp": row["smf"]["ip"],
                                       "endpoint": row["endpoint"], "transmitted": 1, "received": int(code == "0")}})
        if seen != set(matched):
            raise EvidenceUnavailable("Missing per-UE probe response; sampling is not allowed")
        return result

    def probe_many(self, batches: list[tuple[dict, list[dict]]], *, cancel: Event | None = None,
                   deadline: float | None = None, workers: int = 8):
        if not 1 <= workers <= 16:
            raise ValueError("Probe worker limit must be between 1 and 16")
        cancel = cancel or Event()
        deadline = deadline or time.monotonic() + 12
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
                raise EvidenceUnavailable("Batched UE evidence collection failed or exceeded its deadline") from None
        return result
