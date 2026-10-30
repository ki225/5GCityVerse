"""Native resource providers. Dependencies are injected; construction never mutates infrastructure."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from backend.onprem.intents.actuators import ActuationBlocked, Capability
from backend.onprem.policy.group_qer import _ambr_qer


def _version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ActuationBlocked(f"{name} must be a positive integer")
    return value


class SessionQerApi(Protocol):
    def capability(self) -> dict: ...
    def inventory(self, session_keys: tuple[str, ...]) -> dict: ...
    def write(self, identity: dict, rates: dict, *, owner_id: str, operation_id: str,
              expected_version: str, restore: bool, restore_metadata: dict | None = None) -> dict: ...


class SessionQerProvider:
    """All selected sessions, no holdout. Never downgrade an exact selector to SUPI-only."""
    identity_fields = ("session_key", "networkScope", "supi", "sessionId", "establishedEpoch", "smContextRef", "pfcpSeid",
                       "upfEpoch", "sst", "sd", "dnn")

    def __init__(self, api: SessionQerApi, *, clock=time.time):
        self.api, self.clock = api, clock

    def capabilities(self):
        proof = self.api.capability()
        status = ("configured" if proof.get("verified") is True
                  and proof.get("selector") == "exact-pdu-session"
                  and proof.get("compare_and_swap") is True else "not_configured")
        return (Capability("update_qos", status,
                           "Exact session selector, ownership and version CAS must be commissioned"),)

    def _inventory(self, request, keys=None, *, with_stamp=False,include_native_metadata=False):
        keys = request.session_keys if keys is None else keys
        value = self.api.inventory(keys)
        rows = value.get("sessions", [])
        stamp = value.get("observed_at")
        if (value.get("source") != "native-upf-qer" or type(stamp) not in (int, float)
                or not -1 <= self.clock() - stamp <= 15
                or not isinstance(rows, list) or not rows
                or len(rows) != len(keys)):
            raise ActuationBlocked("Fresh complete native session inventory is required")
        mapped = {r.get("session_key"): r for r in rows}
        if set(mapped) != set(keys) or len(mapped) != len(rows):
            raise ActuationBlocked("Native inventory does not match exact session cohort")
        for key, row in mapped.items():
            if any(row.get(f) is None or row.get(f) == "" for f in self.identity_fields):
                raise ActuationBlocked("Native session identity is incomplete")
            decoded = json.loads(key)
            if (not isinstance(decoded, list) or len(decoded) != 4
                    or decoded[0] != row["networkScope"] or decoded[1] != row["supi"] or str(decoded[2]) != str(row["sessionId"])
                    or str(decoded[3]) != str(row["establishedEpoch"])
                    or not row.get("version")):
                raise ActuationBlocked("Native session identity differs from requested key")
            qers = row.get("qers")
            if (not isinstance(qers, list) or not qers
                    or any(not isinstance(q, dict) or type(q.get("id")) is not int
                           or not q.get("pdrIds") or not isinstance(q.get("mbrKbps"), dict)
                           for q in qers)
                    or len({q["id"] for q in qers}) != len(qers)):
                raise ActuationBlocked("Native QER/PDR inventory is incomplete")
            _ambr_qer(qers)
        allowed = {*self.identity_fields, "version", "owner_id", "operation_id", "qers"}
        if include_native_metadata:
            allowed.update(('upf_id','upf_node_ip','smf_boot_epoch','created_at'))
        clean = {key: {f: deepcopy(v) for f, v in row.items() if f in allowed}
                 for key, row in mapped.items()}
        return (clean, stamp) if with_stamp else clean

    def current_policy(self,session_keys):
        """Read exact native policies; this creates no command or rate proposal."""
        from types import SimpleNamespace
        keys=tuple(session_keys)
        if not keys or len(set(keys))!=len(keys):
            raise ActuationBlocked('Unique nonempty exact session policy keys are required')
        rows,stamp=self._inventory(SimpleNamespace(session_keys=keys),with_stamp=True,include_native_metadata=True)
        original_fields={*self.identity_fields,'version','owner_id','operation_id','qers'}
        versions={key:_version({key:{k:v for k,v in row.items() if k in original_fields}}) for key,row in rows.items()}
        return {'source':'native-upf-qer','sessions':rows,'observed_at':stamp,'version':_version(rows),
            'session_versions':versions}

    def inspect(self, request):
        rows, stamp = self._inventory(request, with_stamp=True)
        return {"version": _version(rows), "sessions": rows, "observed_at": stamp}

    def preflight(self, request, before):
        if not request.session_keys:
            raise ActuationBlocked("QoS operation requires sessions")
        rates = request.parameters.get("rates_by_session")
        if rates is None:
            rates = {k: request.parameters.get("rates_kbps") for k in request.session_keys}
        if not isinstance(rates, dict) or set(rates) != set(request.session_keys):
            raise ActuationBlocked("Rate allocation must cover all selected sessions")
        for rate in rates.values():
            if (not isinstance(rate, dict) or set(rate) != {"ul", "dl"}
                    or any(type(v) is not int or not 1 <= v <= 500_000 for v in rate.values())):
                raise ActuationBlocked("QoS rates exceed commissioned range")
        if request.parameters.get("flow_ids"):
            raise ActuationBlocked("unsupported_granularity: session AMBR cannot isolate QoS flows")
        if any(r.get("owner_id") not in (None, "", request.owner_id)
               for r in before["sessions"].values()):
            raise ActuationBlocked("Session has a different effective policy owner")
        return {"target_count": len(rates), "rates": deepcopy(rates)}

    def mutate(self, request, before):
        rates = self.preflight(request, before)["rates"]
        attempted = []
        for key in request.session_keys:
            current = self._inventory(request, (key,))[key]
            old = before["sessions"][key]
            if current != old:
                raise ActuationBlocked("Session changed before QER write")
            reply = self.api.write({f: old[f] for f in self.identity_fields}, rates[key],
                owner_id=request.owner_id, operation_id=request.operation_id,
                expected_version=old["version"], restore=False)
            attempted.append(key)
            if reply.get("accepted") is not True or reply.get("session_key") != key:
                raise ActuationBlocked("SMF did not acknowledge the exact session")
        return {"attempted_session_keys": attempted}

    def observe(self, request, mutation):
        return self.inspect(request)

    def confirmed(self, request, observed):
        rates = self.preflight(request, observed)["rates"]
        return all(r.get("owner_id") == request.owner_id
                   and r.get("operation_id") == request.operation_id
                   and _ambr_qer(r["qers"])["mbrKbps"] == rates[key]
                   for key, r in observed["sessions"].items())

    def identity_preserved(self, before, after):
        for key, old in before["sessions"].items():
            new = after["sessions"].get(key, {})
            if any(new.get(f) != old.get(f) for f in self.identity_fields):
                return False
            old_qers = sorted(old["qers"], key=lambda q: q["id"])
            new_qers = sorted(new.get("qers", []), key=lambda q: q["id"])
            if [(q["id"], q["pdrIds"]) for q in old_qers] != [(q["id"], q["pdrIds"]) for q in new_qers]:
                return False
            target = _ambr_qer(old_qers)["id"]
            if any(q != old_qers[i] for i, q in enumerate(new_qers) if q["id"] != target):
                return False
        return True

    def restoration_state(self, request, before, installed):
        """Pure exact-session readback after an uncertain restoration response."""
        actual=self._inventory(request)
        restored=all(all(actual[k].get(f)==row.get(f) for f in self.identity_fields)
            and actual[k]['qers']==row['qers']
            and actual[k].get('owner_id')==row.get('owner_id')
            and actual[k].get('operation_id')==row.get('operation_id')
            for k,row in before['sessions'].items())
        return {'restored':restored,'source':'native-upf-qer','version':_version(actual)}

    def restore(self, request, before, installed):
        current = self._inventory(request)
        rates = self.preflight(request, before)["rates"]
        if not self.identity_preserved(before, {"sessions": current}):
            raise ActuationBlocked("Native QER/PDR topology changed; compensation blocked")
        for key in reversed(request.session_keys):
            old, now = before["sessions"][key], current[key]
            if all(now.get(f) == old.get(f) for f in self.identity_fields) is False:
                raise ActuationBlocked("Session identity changed; refusing compensation")
            if {k: v for k, v in now.items() if k != "version"} == {k: v for k, v in old.items() if k != "version"}:
                continue
            if (now.get("owner_id") != request.owner_id or now.get("operation_id") != request.operation_id
                    or _ambr_qer(now["qers"])["mbrKbps"] not in
                       (rates[key], _ambr_qer(old["qers"])["mbrKbps"])):
                raise ActuationBlocked("Foreign policy must not be overwritten during compensation")
            self.api.write({f: old[f] for f in self.identity_fields},
                _ambr_qer(old["qers"])["mbrKbps"], owner_id=request.owner_id,
                operation_id=request.operation_id, expected_version=now["version"], restore=True,
                restore_metadata={"owner_id": old.get("owner_id"), "operation_id": old.get("operation_id")})
        actual = self._inventory(request)
        restored = all(all(actual[k].get(f) == row.get(f) for f in self.identity_fields)
                       and actual[k]["qers"] == row["qers"]
                       and actual[k].get("owner_id") == row.get("owner_id")
                       and actual[k].get("operation_id") == row.get("operation_id")
                       for k, row in before["sessions"].items())
        return {"restored": restored, "source": "native-upf-qer", "version": _version(actual)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FixedOriginSessionQerApi:
    """HTTP transport for a commissioned exact-session SMF endpoint, never the legacy SUPI endpoint.

    Existing deployments advertising only supi-only remain not_configured. The
    commissioning reader must verify exact selectors AND native version/owner CAS.
    """
    def __init__(self, origin, *, token_reader, capability_reader=None, inventory_reader=None,
                 timeout=5, opener=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not 0 < timeout <= 10):
            raise ValueError("A fixed native SMF origin and bounded timeout are required")
        self.origin, self.timeout = origin.rstrip("/"), timeout
        self.token_reader, self.capability_reader = token_reader, capability_reader
        self.inventory_reader = inventory_reader
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def capability(self):
        if self.capability_reader:
            return self.capability_reader()
        return self._native_read("/nsmf-oam/v1/session-qer-capability")

    def inventory(self, session_keys):
        if self.inventory_reader:
            return self.inventory_reader(session_keys)
        return self._native_read("/nsmf-oam/v1/session-qer-inventory", {"session_keys": session_keys})

    def active_inventory(self):
        return self._native_read("/nsmf-oam/v1/session-active-inventory")

    def _native_read(self, path, payload=None):
        req = Request(self.origin + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method="GET" if payload is None else "POST",
            headers={"Content-Type": "application/json", "X-SMF-QER-Actuator-Token": self.token_reader()})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw, status = response.read(8 * 1024 * 1024 + 1), response.status
            if len(raw) > 8 * 1024 * 1024 or status != 200:
                raise ValueError("Native read rejected")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Native object required")
            return value
        except Exception:
            raise ActuationBlocked("Native session capability or independent inventory is unavailable") from None

    def write(self, identity, rates, *, owner_id, operation_id, expected_version, restore, restore_metadata=None):
        proof = self.capability()
        if (proof.get("verified") is not True or proof.get("selector") != "exact-pdu-session"
                or proof.get("compare_and_swap") is not True):
            raise ActuationBlocked("Exact-session native endpoint is not commissioned")
        payload = {"identity": identity, "ratesKbps": rates, "ownerId": owner_id,
                   "operationId": operation_id, "expectedVersion": expected_version,
                   "phase": "restore" if restore else "apply"}
        if restore:
            payload["restoreMetadata"] = restore_metadata
        req = Request(self.origin + "/nsmf-oam/v1/session-qer-actuation",
            data=json.dumps(payload, allow_nan=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-SMF-QER-Actuator-Token": self.token_reader()})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw, status = response.read(65537), response.status
            if len(raw) > 65536 or status != 200:
                raise ValueError("Unaccepted native response")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Invalid native response")
            return {"accepted": value.get("accepted") is True,
                    "session_key": value.get("session_key")}
        except Exception:
            raise ActuationBlocked("Native session QER outcome requires readback") from None


class KubectlResourceClient:
    """Bounded argv-only Kubernetes IO; no shell, manifest text or credentials in receipts."""
    def __init__(self, *, context, namespace, runner=None, timeout=15):
        if (not context or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", namespace)
                or not 0 < timeout <= 30):
            raise ValueError("Fixed context, namespace and bounded timeout required")
        self.context, self.namespace = context, namespace
        self.runner, self.timeout = runner or subprocess.run, timeout

    def call(self, *args, body=None):
        argv = ["kubectl", "--context", self.context, "--namespace", self.namespace,
                f"--request-timeout={self.timeout}s", *args]
        try:
            result = self.runner(argv, input=None if body is None else json.dumps(body),
                capture_output=True, text=True, timeout=self.timeout + 2, check=False)
            if result.returncode:
                raise ValueError("Native Kubernetes call failed")
            return json.loads(result.stdout)
        except Exception:
            raise ActuationBlocked("Bounded Kubernetes request failed") from None

    def cpu_stat(self, pod, container):
        if any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", v)
               for v in (pod, container)):
            raise ActuationBlocked("Exact Pod and container names required")
        argv = ["kubectl", "--context", self.context, "--namespace", self.namespace,
            f"--request-timeout={self.timeout}s", "exec", pod, "-c", container, "--", "cat"]
        try:
            result = None
            for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
                result = self.runner([*argv, path], capture_output=True, text=True,
                                     timeout=self.timeout + 2, check=False)
                if result.returncode == 0:
                    break
            if result.returncode or len(result.stdout) > 4096:
                raise ValueError("Bounded cgroup response unavailable")
            values = {}
            for line in result.stdout.splitlines():
                name, value = line.split()
                if name in {"nr_periods", "nr_throttled"}:
                    values[name] = int(value)
            if set(values) != {"nr_periods", "nr_throttled"} or min(values.values()) < 0:
                raise ValueError("Incomplete cgroup counters")
            return values
        except Exception:
            raise ActuationBlocked("Native CPU throttling counters unavailable") from None


def _cpu_millis(value):
    text = str(value)
    return int(text[:-1]) if text.endswith("m") else int(float(text) * 1000)


def _memory_bytes(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]i|[kMGT])?", str(value))
    if not match:
        raise ActuationBlocked("Unsupported Kubernetes memory quantity")
    multipliers = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                   "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    return int(float(match[1]) * multipliers[match[2]])


parse_cpu_millis = _cpu_millis
parse_memory_bytes = _memory_bytes


class KubernetesResourceProvider:
    """In-place Pod resize only. Rolling replacements need a separately tested continuity path."""
    owner_annotation = "cityverse.io/intent-owner"
    operation_annotation = "cityverse.io/intent-operation"

    def __init__(self, client: KubectlResourceClient, *, pod_allowlist, in_place_resize=False,
                 memory_reader=None, service_reader=None):
        self.client, self.pods = client, frozenset(pod_allowlist)
        self.in_place_resize, self.memory_reader = in_place_resize, memory_reader
        self.service_reader = service_reader

    def capabilities(self):
        return (Capability("resize_nf", "configured" if self.in_place_resize and self.pods
                           and self.memory_reader and self.service_reader else "not_configured",
                           "Requires in-place resize, measured memory and service inventory"),)

    def inspect(self, request):
        if request.target_id not in self.pods:
            raise ActuationBlocked("NF Pod is outside commissioned scope")
        pod = self.client.call("get", "pod", request.target_id, "-o", "json")
        meta, spec = pod["metadata"], pod["spec"]
        status = pod.get("status", {})
        return {"version": meta["resourceVersion"], "uid": meta["uid"],
                "owner_id": meta.get("annotations", {}).get(self.owner_annotation),
                "operation_id": meta.get("annotations", {}).get(self.operation_annotation),
                "node": spec.get("nodeName"), "containers": [
                    {k: deepcopy(c[k]) for k in ("name", "resources", "resizePolicy") if k in c}
                    for c in spec["containers"]],
                "status": {"conditions": [{k: c.get(k) for k in ("type", "status")}
                                          for c in status.get("conditions", [])],
                    "containerStatuses": [{k: deepcopy(c[k]) for k in
                        ("name", "ready", "resources", "restartCount") if k in c}
                        for c in status.get("containerStatuses", [])]}}

    def preflight(self, request, before):
        if not self.in_place_resize or not self.memory_reader or not self.service_reader:
            raise ActuationBlocked("Native in-place resize and observations are not configured")
        if before.get("owner_id") not in (None, request.owner_id):
            raise ActuationBlocked("NF has a foreign effective owner")
        container = request.parameters.get("container")
        candidates = [c for c in before["containers"] if c["name"] == container]
        if len(candidates) != 1 or not before.get("node"):
            raise ActuationBlocked("Resize requires an exact running NF container")
        old = candidates[0]
        policies = {p.get("resourceName"): p.get("restartPolicy") for p in old.get("resizePolicy", [])}
        resources = request.parameters.get("resources", {})
        if set(resources) != {"requests", "limits"}:
            raise ActuationBlocked("Explicit CPU/memory requests and limits are required")
        for category in ("requests", "limits"):
            if set(resources[category]) != {"cpu", "memory"}:
                raise ActuationBlocked("Only CPU and memory resize is supported")
            if _cpu_millis(resources[category]["cpu"]) <= 0 or _memory_bytes(resources[category]["memory"]) <= 0:
                raise ActuationBlocked("Resource quantities must be positive")
        previous_resources = old.get("resources", {})
        memory_changed = any(_memory_bytes(resources[c]["memory"]) !=
            _memory_bytes(previous_resources.get(c, {}).get("memory", "0")) for c in ("requests", "limits"))
        cpu_changed = any(_cpu_millis(resources[c]["cpu"]) !=
            _cpu_millis(previous_resources.get(c, {}).get("cpu", "0")) for c in ("requests", "limits"))
        if memory_changed and policies.get("memory") != "NotRequired":
            raise ActuationBlocked("Memory resize continuity has not been commissioned")
        if cpu_changed and policies.get("cpu", "NotRequired") != "NotRequired":
            raise ActuationBlocked("CPU resize would restart the container")
        if (_cpu_millis(resources["requests"]["cpu"]) > _cpu_millis(resources["limits"]["cpu"])
                or _memory_bytes(resources["requests"]["memory"]) > _memory_bytes(resources["limits"]["memory"])):
            raise ActuationBlocked("Requests exceed limits")
        memory = self.memory_reader(request.target_id, container)
        if memory.get("fresh") is not True or memory.get("uid") != before["uid"]:
            raise ActuationBlocked("Fresh exact-Pod memory observation required")
        if _memory_bytes(resources["limits"]["memory"]) < memory["working_set_bytes"]:
            raise ActuationBlocked("RAM shrink would exceed current working set")
        impacted = set(self.service_reader(request.target_id))
        if set(request.parameters.get("impacted_service_ids", ())) != impacted:
            raise ActuationBlocked("Shared NF service impact scope is incomplete")
        node = self.client.call("get", "node", before["node"], "-o", "json")
        if not any(c.get("status") == "True" and c.get("type") == "Ready"
                   for c in node.get("status", {}).get("conditions", [])):
            raise ActuationBlocked("Node is not Ready")
        if any(c.get("status") == "True" and c.get("type") in {"MemoryPressure", "DiskPressure", "PIDPressure"}
               for c in node.get("status", {}).get("conditions", [])):
            raise ActuationBlocked("Node is under resource pressure")
        pods = self.client.call("get", "pods", "--all-namespaces",
                                "--field-selector", "spec.nodeName=" + before["node"], "-o", "json")
        allocated_cpu = allocated_memory = 0
        for pod in pods.get("items", []):
            if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            pod_spec = pod.get("spec", {})
            # Conservatively include all init requests, including restartable sidecars.
            for row in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
                used = row.get("resources", {}).get("requests", {})
                allocated_cpu += _cpu_millis(used.get("cpu", "0"))
                allocated_memory += _memory_bytes(used.get("memory", "0"))
            overhead = pod_spec.get("overhead", {})
            allocated_cpu += _cpu_millis(overhead.get("cpu", "0"))
            allocated_memory += _memory_bytes(overhead.get("memory", "0"))
        previous = old.get("resources", {}).get("requests", {})
        total = node["status"]["allocatable"]
        if (allocated_cpu - _cpu_millis(previous.get("cpu", "0"))
                + _cpu_millis(resources["requests"]["cpu"]) > _cpu_millis(total["cpu"])
                or allocated_memory - _memory_bytes(previous.get("memory", "0"))
                + _memory_bytes(resources["requests"]["memory"]) > _memory_bytes(total["memory"])):
            raise ActuationBlocked("Node has insufficient allocatable capacity")
        return {"uid": before["uid"], "impacted_service_ids": sorted(impacted), "capacity_checked": True}

    def _resize(self, request, current, resources):
        patch = {"metadata": {"resourceVersion": current["version"], "uid": current["uid"]},
                 "spec": {"containers": [{"name": request.parameters["container"], "resources": resources}]}}
        return self.client.call("patch", "pod", request.target_id, "--subresource=resize",
            "--type=strategic", "--patch", json.dumps(patch), "-o", "json")

    def mutate(self, request, before):
        self.preflight(request, before)
        current = self.inspect(request)
        if current != before:
            raise ActuationBlocked("Pod changed before resize")
        patch = {"metadata": {"resourceVersion": before["version"], "uid": before["uid"],
                 "annotations": {self.owner_annotation: request.owner_id,
                                 self.operation_annotation: request.operation_id}}}
        self.client.call("patch", "pod", request.target_id, "--type=merge", "--patch", json.dumps(patch), "-o", "json")
        owned = self.inspect(request)
        self._resize(request, owned, request.parameters["resources"])
        return {"uid": before["uid"]}

    def observe(self, request, mutation):
        return self.inspect(request)

    def confirmed(self, request, observed):
        if observed.get("owner_id") != request.owner_id or observed.get("operation_id") != request.operation_id:
            return False
        name = request.parameters["container"]
        desired = request.parameters["resources"]
        native = next((c for c in observed["containers"] if c["name"] == name), {})
        status = next((c for c in observed.get("status", {}).get("containerStatuses", []) if c["name"] == name), {})
        actual = status.get("resources")
        return (native.get("resources") == desired and actual == desired
                and status.get("ready") is True
                and not any(c.get("status") == "True" and c.get("type", "").startswith("PodResize")
                            for c in observed.get("status", {}).get("conditions", [])))

    def identity_preserved(self, before, after):
        if before["uid"] != after["uid"] or before["node"] != after["node"]:
            return False
        restarts = lambda value: {r["name"]: r.get("restartCount", 0)
            for r in value.get("status", {}).get("containerStatuses", [])}
        return restarts(before) == restarts(after)

    def restore(self, request, before, installed):
        current = self.inspect(request)
        name = request.parameters["container"]
        old = next(c for c in before["containers"] if c["name"] == name)["resources"]
        native = next(c for c in current["containers"] if c["name"] == name)["resources"]
        runtime = next((s for s in current.get("status", {}).get("containerStatuses", []) if s["name"] == name), {})
        if (current["uid"] == before["uid"] and current.get("owner_id") == before.get("owner_id")
                and current.get("operation_id") == before.get("operation_id")
                and native == old and runtime.get("resources") == old and runtime.get("ready") is True):
            return {"restored": True, "source": "kubernetes-pod-runtime"}
        if (current["uid"] != before["uid"] or current.get("owner_id") != request.owner_id
                or current.get("operation_id") != request.operation_id):
            raise ActuationBlocked("Pod ownership changed; compensation blocked")
        if native not in (old, request.parameters["resources"]):
            raise ActuationBlocked("Pod resources were changed by another operator")
        memory = self.memory_reader(request.target_id, name)
        if (memory.get("fresh") is not True or memory.get("uid") != before["uid"]
                or memory["working_set_bytes"] > _memory_bytes(old["limits"]["memory"])):
            raise ActuationBlocked("Rollback RAM would be unsafe")
        self._resize(request, current, old)
        actual = self.inspect(request)
        statuses = actual.get("status", {}).get("containerStatuses", [])
        runtime = next((s for s in statuses if s["name"] == name), {})
        restored = next(c for c in actual["containers"] if c["name"] == name)["resources"] == old
        restored = restored and runtime.get("resources") == old and runtime.get("ready") is True
        if restored:
            patch = {"metadata": {"resourceVersion": actual["version"], "uid": actual["uid"],
                "annotations": {self.owner_annotation: before.get("owner_id"),
                                self.operation_annotation: before.get("operation_id")}}}
            self.client.call("patch", "pod", request.target_id, "--type=merge", "--patch", json.dumps(patch), "-o", "json")
        return {"restored": restored, "source": "kubernetes-pod-runtime"}


    def restoration_state(self, request, before, installed):
        """Independently read a completed resize rollback without native writes."""
        current = self.inspect(request)
        name = request.parameters['container']
        old = next(c for c in before['containers'] if c['name'] == name)['resources']
        native = next((c for c in current['containers'] if c['name'] == name), {})
        runtime = next((c for c in current.get('status', {}).get('containerStatuses', []) if c['name'] == name), {})
        restored = (self.identity_preserved(before, current)
            and current.get('owner_id') == before.get('owner_id')
            and current.get('operation_id') == before.get('operation_id')
            and native.get('resources') == old and runtime.get('resources') == old
            and runtime.get('ready') is True
            and not any(c.get('status') == 'True' and c.get('type', '').startswith('PodResize')
                        for c in current.get('status', {}).get('conditions', [])))
        return {'restored': restored, 'source': 'kubernetes-pod-runtime'}


class UpfControlApi(Protocol):
    """Native controller contract; all methods enforce expected-version and owner CAS."""
    def capabilities(self) -> dict: ...
    def inspect(self, target_id: str) -> dict: ...
    def deploy(self, target_id: str, *, template_id: str, node_id: str, owner_id: str,
               operation_id: str, expected_version: str) -> dict: ...
    def configure_smf(self, target_id: str, *, accepting: bool, owner_id: str,
                      operation_id: str, expected_version: str) -> dict: ...
    def assign(self, target_id: str, session_keys: tuple[str, ...], *, mode: str, pending_admissions=(),
               owner_id: str, operation_id: str, expected_version: str) -> dict: ...
    def remove(self, target_id: str, *, owner_id: str, operation_id: str, expected_version: str) -> dict: ...
    def restore(self, target_id: str, before: dict, *, owner_id: str, operation_id: str,
                expected_version: str) -> dict: ...


class UpfLifecycleProvider:
    """Deploy/path/SMF/assignment/drain state machine, never a blind replica increment."""
    actions = ("add_upf", "place_nf", "assign_sessions", "drain_and_remove")

    def __init__(self, api: UpfControlApi, *, templates):
        self.api, self.templates = api, frozenset(templates)

    def capabilities(self):
        proof = self.api.capabilities()
        return tuple(Capability(a, "configured" if proof.get(a) is True else "not_configured",
            "Requires commissioned PFCP, SMF selection and native session inventory") for a in self.actions)

    def inspect(self, request):
        value = self.api.inspect(request.target_id)
        if (not value.get("version") or value.get("source") != "native-upf-controller"
                or value.get("target_id") != request.target_id):
            raise ActuationBlocked("Native UPF state unavailable")
        return value

    def preflight(self, request, before):
        if before.get("owner_id") not in (None, request.owner_id):
            raise ActuationBlocked("UPF belongs to another intent")
        if request.action in {"add_upf", "place_nf"}:
            if before.get("exists") is True and before.get("operation_id") != request.operation_id:
                raise ActuationBlocked("UPF creation cannot overwrite an existing instance")
            if (request.parameters.get("template_id") not in self.templates
                    or not request.parameters.get("node_id")
                    or before.get("placement_capacity_verified") is not True):
                raise ActuationBlocked("Commissioned template and verified placement capacity required")
        if request.action == "assign_sessions":
            from .pending_admission import AdmissionBlocked, PendingAdmission, SessionAssignment
            try:
                if set(request.parameters) - {"mode", "pending_admissions"}:
                    raise AdmissionBlocked("Unknown assignment parameters")
                assignment = SessionAssignment(request.target_id, request.parameters.get("mode", "new_sessions"),
                    tuple(PendingAdmission.from_dict(v) for v in request.parameters.get("pending_admissions", ())),
                    request.session_keys)
                assignment.validate_authority(owner_id=request.owner_id,
                    native_target_upf_id=before.get("native_upf_id"), now=time.time(),
                    migration_supported=self.api.capabilities().get("session_migration") is True)
            except (AdmissionBlocked, ValueError, TypeError, KeyError) as exc:
                raise ActuationBlocked(str(exc)) from None
            if not all(before.get(k) is True for k in ("ready", "pfcp_associated", "path_verified", "smf_selected")):
                raise ActuationBlocked("UPF data path must be ready before session assignment")
        return {"action": request.action, "version": before["version"]}

    def mutate(self, request, before):
        self.preflight(request, before)
        kw = {"owner_id": request.owner_id, "operation_id": request.operation_id,
              "expected_version": before["version"]}
        if request.action in {"add_upf", "place_nf"}:
            self.api.deploy(request.target_id, template_id=request.parameters["template_id"],
                            node_id=request.parameters["node_id"], **kw)
            actual = self.inspect(request)
            if not all(actual.get(k) is True for k in ("ready", "pfcp_associated", "path_verified")):
                return {"phase": "waiting_for_path"}
            kw["expected_version"] = actual["version"]
            self.api.configure_smf(request.target_id, accepting=True, **kw)
        elif request.action == "assign_sessions":
            self.api.assign(request.target_id, request.session_keys,
                mode=request.parameters.get("mode", "new_sessions"),
                pending_admissions=request.parameters.get("pending_admissions", ()), **kw)
        elif request.action == "drain_and_remove":
            self.api.configure_smf(request.target_id, accepting=False, **kw)
            actual = self.inspect(request)
            if actual.get("active_session_keys") != [] or actual.get("accepting") is not False:
                return {"phase": "draining"}
            kw["expected_version"] = actual["version"]
            self.api.remove(request.target_id, **kw)
        return {"phase": "native_request_complete"}

    def observe(self, request, mutation):
        return self.inspect(request)

    def activation_preflight(self, request, observed):
        """Read-only continuation proof after a previously dispatched deployment."""
        if request.action not in {'add_upf', 'place_nf'}:
            return None
        if (observed.get('owner_id') != request.owner_id
                or observed.get('operation_id') != request.operation_id
                or observed.get('node_id') != request.parameters.get('node_id')
                or observed.get('template_id') != request.parameters.get('template_id')
                or not observed.get('pod_uid') or observed.get('exists') is not True):
            return None
        if (not all(observed.get(k) is True for k in ('ready', 'pfcp_associated', 'path_verified'))
                or observed.get('smf_selected') is True):
            return None
        return {'expected_version': observed['version'], 'pod_uid': observed['pod_uid']}

    def deploy_for_placement(self, request, before):
        """Placement journals deployment and later SMF selection separately."""
        self.preflight(request, before)
        return self.api.deploy(request.target_id, template_id=request.parameters['template_id'],
            node_id=request.parameters['node_id'], owner_id=request.owner_id,
            operation_id=request.operation_id, expected_version=before['version'])

    def activate_deployed(self, request, proof):
        """Executor-only continuation; never deploy or replace an existing UPF."""
        actual = self.inspect(request)
        if self.activation_preflight(request, actual) != proof:
            raise ActuationBlocked('UPF activation proof changed before SMF selection')
        return self.api.configure_smf(request.target_id, accepting=True, owner_id=request.owner_id,
            operation_id=request.operation_id, expected_version=proof['expected_version'])

    def confirmed(self, request, observed):
        if observed.get("owner_id") != request.owner_id or observed.get("operation_id") != request.operation_id:
            return False
        if request.action == "drain_and_remove":
            return observed.get("exists") is False and observed.get("active_session_keys") == []
        if request.action == "assign_sessions":
            if request.parameters.get("mode", "new_sessions") == "new_sessions":
                expected = {p["identity"]["nonce"] for p in request.parameters.get("pending_admissions", ())}
                handles = observed.get("admission_handles", ())
                return bool(expected) and observed.get("admissions_installed") is True and {
                    h.get("admission", {}).get("identity", {}).get("nonce") for h in handles
                    if h.get("admission", {}).get("owner_id") == request.owner_id
                    and h.get("operation_id") == request.operation_id} == expected
            return (bool(request.session_keys)
                    and set(request.session_keys) <= set(observed.get("active_session_keys", ()))
                    and all(observed.get("receiver_bytes_by_session", {}).get(k, 0) > 0 for k in request.session_keys))
        return (all(observed.get(k) is True for k in ("ready", "pfcp_associated", "path_verified", "smf_selected"))
                and observed.get("node_id") == request.parameters.get("node_id")
                and observed.get("template_id") == request.parameters.get("template_id"))

    def restore(self, request, before, installed):
        now = self.inspect(request)
        if now.get("owner_id") != request.owner_id or now.get("operation_id") != request.operation_id:
            raise ActuationBlocked("UPF operation owner changed")
        result = self.api.restore(request.target_id, before, owner_id=request.owner_id,
                         operation_id=request.operation_id, expected_version=now["version"])
        if result.get("pending") is True:
            return {"restored": False, "pending": True, "source": "native-upf-controller"}
        actual = self.inspect(request)
        fields = ("exists", "owner_id", "operation_id", "active_session_keys", "accepting", "node_id")
        if before.get('ownership_scope')=='pending-admissions':
            fields=('exists','owner_id','operation_id','node_id','pod_uid','native_upf_id')
        return {"restored": all(actual.get(k) == before.get(k) for k in fields),
                "source": "native-upf-controller"}

    def prepare_drain(self,request,before,installed):
        actual=self.inspect(request)
        if actual.get('owner_id')!=request.owner_id or actual.get('operation_id')!=request.operation_id:
            raise ActuationBlocked('UPF drain owner changed')
        if actual.get('admissions_closed') is True and actual.get('selection_closed') is True:
            return dict(admissions_closed=True,selection_closed=True,native_ues_gone=False,
                source='native-upf-controller')
        self.api.prepare_drain(request.target_id,owner_id=request.owner_id,
            operation_id=request.operation_id,expected_version=actual['version'])
        observed=self.inspect(request)
        closed=(observed.get('owner_id')==request.owner_id and observed.get('operation_id')==request.operation_id
            and observed.get('admissions_closed') is True and observed.get('selection_closed') is True)
        return dict(admissions_closed=closed,selection_closed=closed,native_ues_gone=False,
            source='native-upf-controller')

    def restoration_state(self, request, before, installed):
        actual = self.inspect(request)
        if (actual.get("restoration_owner_id") != request.owner_id
                or actual.get("restoration_operation_id") != request.operation_id):
            return {"restored": False, "pending": False, "source": "native-upf-controller"}
        fields = ("exists", "owner_id", "operation_id", "active_session_keys", "accepting", "node_id")
        if before.get('ownership_scope')=='pending-admissions':
            fields=('exists','owner_id','operation_id','node_id','pod_uid','native_upf_id')
        if before.get('exists') is True:
            restored=(actual.get('existing_baseline_restored')==_version(before)
                and all(actual.get(k)==before.get(k) for k in fields) and not actual.get('admission_handles'))
            return dict(restored=restored,pending=False,source='native-upf-controller')
        restored = (actual.get("restored") is True and actual.get("cleanup_pending") is False
                    and all(actual.get(k) == before.get(k) for k in fields))
        return {"restored": restored, "pending": not restored and (
            actual.get("restoration_pending") is True or actual.get("cleanup_pending") is True),
            "source": "native-upf-controller"}

    def resolve_effect_session_keys(self, receipt):
        """Only newly created, native-confirmed bindings may become effect targets."""
        request = receipt.request
        if request.action != "assign_sessions" or request.parameters.get("mode", "new_sessions") != "new_sessions":
            return request.session_keys
        observed = self.inspect(request)
        if not self.confirmed(request, observed) or observed.get("bindings_complete") is not True:
            return None
        keys = observed.get("bound_session_keys", ())
        expected = len(request.parameters.get("pending_admissions", ()))
        if not keys or len(keys) != expected or len(set(keys)) != expected:
            return None
        return tuple(keys)


class NodeProvisioner:
    """Provider-backed node acquisition; VM creation alone is never a capacity receipt."""
    def __init__(self, provider, *, pools):
        self.provider, self.pools = provider, frozenset(pools)

    def capabilities(self):
        configured = self.provider.capabilities().get("provision_node") is True and bool(self.pools)
        return (Capability("provision_node", "configured" if configured else "not_configured"),)

    def inspect(self, request):
        value = self.provider.inspect(request.target_id)
        if (not value.get("version") or value.get("source") != "native-node-provider"
                or value.get("target_id") != request.target_id):
            raise ActuationBlocked("Native node state unavailable")
        return value

    def preflight(self, request, before):
        if request.parameters.get("pool_id") not in self.pools:
            raise ActuationBlocked("Node pool is not commissioned")
        if before.get("exists") is True:
            raise ActuationBlocked("Node acquisition requires a new provider resource identity")
        if before.get("owner_id") not in (None, request.owner_id):
            raise ActuationBlocked("Node has a foreign owner")
        if before.get("physical_capacity_available") is not True or before.get("budget_available") is not True:
            raise ActuationBlocked("Provider capacity or budget is exhausted")
        return {"capacity_checked": True, "budget_checked": True}

    def mutate(self, request, before):
        self.preflight(request, before)
        return self.provider.acquire(request.target_id, pool_id=request.parameters["pool_id"],
            owner_id=request.owner_id, operation_id=request.operation_id, expected_version=before["version"])

    def observe(self, request, mutation):
        return self.inspect(request)

    def confirmed(self, request, observed):
        return (observed.get("owner_id") == request.owner_id and observed.get("operation_id") == request.operation_id
                and all(observed.get(k) is True for k in ("exists", "ready", "network_verified", "allocatable_verified"))
                and observed.get("additional_physical_capacity") is True)

    def restore(self, request, before, installed):
        now = self.inspect(request)
        if now.get("owner_id") != request.owner_id or now.get("operation_id") != request.operation_id:
            raise ActuationBlocked("Node operation owner changed")
        if now.get("workload_count") != 0 or now.get("active_session_keys") != []:
            raise ActuationBlocked("Node must be drained before release")
        self.provider.release(request.target_id, owner_id=request.owner_id,
            operation_id=request.operation_id, expected_version=now["version"])
        actual = self.inspect(request)
        return {"restored": actual.get("exists") == before.get("exists"), "source": "native-node-provider"}


class NativeControllerClient:
    """Fixed-origin JSON controller API with a pinned set of routes and bounded responses."""
    routes = frozenset({"capabilities", "upf/read", "upf/deploy", "upf/smf", "upf/assign",
                        "upf/remove", "upf/restore", "upf/prepare-drain", "nodes/read", "nodes/acquire", "nodes/release",
                        "qer/inventory", "metrics/memory", "services/read", "upf/registry",
                        "applications/read", "applications/endpoint-read", "applications/deploy", "applications/drain", "applications/remove", "applications/restore",
                        "applications/endpoint-bindings/read", "applications/endpoint-bindings/set",
                        "applications/endpoint-bindings/drain", "applications/endpoint-bindings/restore"})

    def __init__(self, origin, *, token_reader, timeout=10, opener=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not 0 < timeout <= 30):
            raise ValueError("Fixed controller origin and bounded timeout required")
        self.origin, self.token_reader, self.timeout = origin.rstrip("/"), token_reader, timeout
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def call(self, route, payload, *, timeout=None):
        if route not in self.routes:
            raise ActuationBlocked("Native controller route is outside configured scope")
        req = Request(self.origin + "/cityverse-resource/v1/" + route,
            data=json.dumps(payload, allow_nan=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token_reader()})
        try:
            request_timeout=self.timeout if timeout is None else min(self.timeout,timeout)
            if not 0<request_timeout<=30:
                raise ValueError('Bounded timeout required')
            with self.opener.open(req, timeout=request_timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                pending=response.status==202 and route in {'upf/read','applications/read'}
                if (response.status != 200 and not pending) or len(raw) > 1024 * 1024:
                    raise ValueError("Bounded native response rejected")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Native object required")
            if pending and value.get('readback_pending') is not True:
                raise ValueError('Invalid pending native readback')
            return value
        except Exception:
            # HTTP bodies and exception strings can contain credentials; never propagate them.
            raise ActuationBlocked("Native controller outcome requires readback") from None

    def commissioned(self, subsystem):
        proof = self.call("capabilities", {"subsystem": subsystem})
        if proof.get("verified") is not True or proof.get("compare_and_swap") is not True:
            return {}
        return proof


class FixedOriginUpfControlApi:
    """Concrete authenticated native UPF controller client; templates stay on the controller."""
    state_fields = frozenset({"source", "target_id", "template_id", "version", "exists", "owner_id", "operation_id", "ready",
        "pfcp_associated", "path_verified", "smf_selected", "active_session_keys", "receiver_bytes_by_session",
        "accepting", "node_id", "placement_capacity_verified", "native_upf_id",
        "admission_handles", "admissions_installed", "bound_session_keys", "bindings_complete",
        "restored", "cleanup_pending", "cleanup_remaining", "restoration_pending",
        "restoration_owner_id", "restoration_operation_id", "admissions_closed", "selection_closed", "existing_baseline_restored", "ownership_scope", "nf_owner_id"})

    def __init__(self, client: NativeControllerClient):
        self.client = client

    def current_registry(self):
        from .commissioned_upfs import (RegistrySnapshot,CommissionedUpfObservation,
            CommissionedUpfSlot,ActiveUpfBinding,UpfEvidenceRoute)
        value=self.client.call('upf/registry',{})
        if not isinstance(self.client,NativeControllerClient) or value.get('schema')!='cityverse.commissioned-upfs/v1':
            raise ActuationBlocked('Authenticated current UPF registry required')
        try:
            observations=[]
            for raw in value['observations']:
                slot=dict(raw['slot'])
                if slot.get('evidence_route'):slot['evidence_route']=UpfEvidenceRoute(**slot['evidence_route'])
                observations.append(CommissionedUpfObservation(CommissionedUpfSlot(**slot),raw['state'],
                    ActiveUpfBinding(**raw['binding']) if raw['binding'] else None,
                    raw['inflight_pod_uid'],raw['native_registered']))
            result=RegistrySnapshot(tuple(observations),value['observed_at'],value['maximum_age_seconds'])
            result.require_fresh(time.time())
            if result.revision!=value['revision']:raise ValueError()
            return result
        except (KeyError,TypeError,ValueError):
            raise ActuationBlocked('Complete current UPF registry response unavailable') from None

    def capabilities(self):
        proof = self.client.commissioned("upf")
        return {name: proof.get(name) is True for name in (*UpfLifecycleProvider.actions, "session_migration")}

    def inspect(self, target_id):
        deadline=time.monotonic()+30
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise ActuationBlocked('Native UPF readback pending; bounded deadline elapsed')
            kwargs={'timeout':min(5,remaining)} if isinstance(self.client,NativeControllerClient) else {}
            value=self.client.call('upf/read',{'target_id':target_id},**kwargs)
            if value.get('readback_pending') is not True:
                stamp=value.get('observed_at')
                if (value.get('source')!='native-upf-controller' or value.get('target_id')!=target_id
                        or type(stamp) not in (int,float) or not 0<=time.time()-stamp<=15
                        or type(value.get('journal_revision')) is not int or value['journal_revision']<0
                        or not value.get('version')
                        or (value.get('exists') is True and not value.get('pod_uid'))
                        or ((value.get('pfcp_associated') is True or value.get('path_verified') is True) and not value.get('kernel_epoch'))):
                    raise ActuationBlocked('Fresh current native UPF readback required')
                fields=self.state_fields|{'observed_at','journal_revision','pod_uid','kernel_epoch'}
                return {k:v for k,v in value.items() if k in fields}
            if value.get('target_id')!=target_id:
                raise ActuationBlocked('Native UPF readback target changed')
            time.sleep(min(.1,max(0,deadline-time.monotonic())))

    def deploy(self, target_id, *, template_id, node_id, **authority):
        return self.client.call("upf/deploy", {"target_id": target_id, "template_id": template_id,
                                              "node_id": node_id, **authority})

    def configure_smf(self, target_id, *, accepting, **authority):
        return self.client.call("upf/smf", {"target_id": target_id, "accepting": accepting, **authority})

    def prepare_drain(self,target_id,**authority):
        return self.client.call('upf/prepare-drain',{'target_id':target_id,**authority})

    def assign(self, target_id, session_keys, *, mode, pending_admissions=(), **authority):
        return self.client.call("upf/assign", {"target_id": target_id, "session_keys": session_keys,
                                              "mode": mode, "pending_admissions": pending_admissions, **authority})

    def remove(self, target_id, **authority):
        return self.client.call("upf/remove", {"target_id": target_id, **authority})

    def restore(self, target_id, before, **authority):
        return self.client.call("upf/restore", {"target_id": target_id, "before": before, **authority})


class FixedOriginNodeApi:
    """Concrete node supplier interface, covering provider creation and Kubernetes admission proof."""
    state_fields = frozenset({"source", "target_id", "version", "exists", "owner_id", "operation_id", "ready",
        "network_verified", "allocatable_verified", "additional_physical_capacity",
        "physical_capacity_available", "budget_available", "workload_count", "active_session_keys"})

    def __init__(self, client: NativeControllerClient):
        self.client = client

    def capabilities(self):
        proof = self.client.commissioned("nodes")
        return {"provision_node": proof.get("provision_node") is True}

    def inspect(self, target_id):
        value = self.client.call("nodes/read", {"target_id": target_id})
        return {k: v for k, v in value.items() if k in self.state_fields}

    def acquire(self, target_id, *, pool_id, **authority):
        return self.client.call("nodes/acquire", {"target_id": target_id, "pool_id": pool_id, **authority})

    def release(self, target_id, **authority):
        return self.client.call("nodes/release", {"target_id": target_id, **authority})


def _token_file_reader(path):
    token_path = Path(path)
    if not token_path.is_absolute():
        raise ValueError("Native credential file must be an absolute operator-controlled path")

    def read():
        try:
            if token_path.is_symlink() or token_path.stat().st_size > 4096:
                raise ValueError("Invalid credential file")
            value = token_path.read_text(encoding="utf-8").strip()
            if not value or any(c.isspace() for c in value):
                raise ValueError("Invalid credential value")
            return value
        except Exception:
            raise ActuationBlocked("Native credential file is unavailable") from None
    return read


def configured_resource_providers(context=None, config=None):
    """Bind server-owned integrations. Never feed request-body configuration into this factory.

    Keys: qos(origin,token_file,controller_origin), kubernetes(context,namespace,
    pod_allowlist,in_place_resize,observer_origin,token_file), upf(origin,token_file,
    templates), nodes(origin,token_file,pools). Missing integrations stay absent.
    """
    from .resource_provider_configuration import validate_provider_configuration
    config = validate_provider_configuration(config)
    providers = dict(getattr(context, "intent_resource_providers", {}) or {})

    def client(values, origin_key="origin"):
        return NativeControllerClient(values[origin_key],
            token_reader=_token_file_reader(values["token_file"]), timeout=values.get("timeout", 10))

    if config.get("qos"):
        values = config["qos"]
        control = client(values, "controller_origin") if values.get("controller_origin") else None
        api = FixedOriginSessionQerApi(values["origin"], token_reader=_token_file_reader(values["token_file"]),
            capability_reader=(lambda: control.commissioned("qos")) if control else None,
            inventory_reader=(lambda keys: control.call("qer/inventory", {"session_keys": keys})) if control else None,
            timeout=min(values.get("timeout", 5), 10))
        providers["qos"] = SessionQerProvider(api)
    if config.get("kubernetes"):
        values = config["kubernetes"]
        namespace = values["namespace"]
        kubernetes = KubectlResourceClient(context=values["context"], namespace=values["namespace"])
        if values.get("observer_origin"):
            observer = client(values, "observer_origin")
            memory_reader = lambda pod, container: observer.call("metrics/memory", {"pod": pod,
                "container": container, "namespace": namespace})
            service_reader = lambda pod: observer.call("services/read", {"pod": pod,
                "namespace": namespace}).get("service_ids", ())
        else:
            from .native_kubernetes_observer import NativeKubernetesObserver
            inventory_reader = getattr(context, "intent_nf_service_inventory_reader", None)
            native_observer = NativeKubernetesObserver(kubernetes, pod_allowlist=values["pod_allowlist"],
                service_inventory_reader=inventory_reader)
            memory_reader = native_observer.memory
            service_reader = native_observer.services if inventory_reader else None
        providers["kubernetes"] = KubernetesResourceProvider(kubernetes,
            pod_allowlist=values["pod_allowlist"], in_place_resize=values.get("in_place_resize") is True,
            memory_reader=memory_reader, service_reader=service_reader)
    if config.get("upf"):
        values = config["upf"]
        providers["upf"] = UpfLifecycleProvider(FixedOriginUpfControlApi(client(values)), templates=values["templates"])
    if config.get("applications"):
        from .application_placement import ApplicationPlacementProvider, FixedOriginApplicationControlApi
        values = config["applications"]
        providers["applications"] = ApplicationPlacementProvider(
            FixedOriginApplicationControlApi(client(values)), templates=values["templates"])
    if config.get("nodes"):
        values = config["nodes"]
        providers["nodes"] = NodeProvisioner(FixedOriginNodeApi(client(values)), pools=values["pools"])
    from .service_route_configuration import configure_service_routes
    configure_service_routes(context, config, providers)
    return providers
