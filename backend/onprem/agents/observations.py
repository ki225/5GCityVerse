"""Bounded Planner observation tools. No hidden probes or mutation commands."""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import re
import time

from backend.onprem.agents.roles import StageTool, object_schema
from backend.onprem.agents.contracts import AgentLoopBlocked, _seconds
from backend.onprem.scenarios.contracts import SCENARIO_SLICES
from backend.onprem.runtime.ue import SLICES


CATALOG = {
    "get_environment_overview": "Environment summary, abnormalities and missing observations.",
    "get_observation_capabilities": "Configured read sources and unavailable observations.",
    "get_k8s_cluster_health": "Node readiness and resource pressure.",
    "get_k8s_workloads": "Workload readiness, restarts, OOM, requests and limits.",
    "get_k8s_events": "Bounded scheduling, eviction and readiness event categories.",
    "get_k8s_resource_usage": "Actual pod/container CPU and memory with measurement timestamps.",
    "get_nf_inventory": "Deployed NF instances, role and readiness; NRF registration separately.",
    "get_nf_health": "NF instance readiness and available application evidence.",
    "get_nf_resource_usage": "Actual NF container usage, requests and limits; no guessed slice attribution.",
    "get_nf_peer_status": "Observed control-plane peer links and missing SBI/PFCP observations.",
    "get_control_plane_failures": "Known registration/PDU failure codes; no rates without denominators.",
    "get_nf_log_excerpt": "Bounded normalized error categories from a known NF instance's logs.",
    "get_slice_topology": "Configured S-NSSAI/DNN and observed UPF/session paths, with sharing unknowns.",
    "get_slice_status": "Round-selected UE registration, sessions and measured traffic per slice.",
    "get_slice_qos": "Native QER rates and unsupported QoS fields.",
    "get_ue_session_summary": "Round-scoped session counts and explicitly unknown measurements.",
    "get_upf_dataplane_state": "Native selected-session/QER summary and UPF epochs.",
    "get_ran_status": "Committed RAN peer health; no implicit ping or new UE.",
    "get_n6_status": "Receiver workload readiness and last observed traffic; listener evidence separately.",
    "get_traffic_metrics": "Receiver metrics and actual offered load for the selected group.",
    "get_metric_trend": "Same-round, same-epoch samples, coverage and gaps.",
    "get_packet_path_metrics": "Configured read-only packet drop/throttling reader, if available.",
}
QUERY_SCHEMA = object_schema({
    "target": {"anyOf": [{"type": "string", "maxLength": 128}, {"type": "null"}]},
    "slice_id": {"anyOf": [{"type": "string", "enum": list(SLICES)}, {"type": "null"}]},
    "window_seconds": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 3600}, {"type": "null"}]},
    "limit": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 100}, {"type": "null"}]},
})
NF_TYPES = ("amf", "smf", "upf", "nrf", "nssf", "pcf", "ausf", "udm", "udr", "nef", "chf")


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _ref(prefix, value):
    return prefix + ":" + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _quantity(value, *, cpu=False):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(n|u|m|Ki|Mi|Gi|Ti|K|M|G|T)?", value)
    if not match:
        return None
    scale = {None: 1, "n": 1e-9, "u": 1e-6, "m": .001,
             "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}[match[2]]
    if cpu and match[2] not in (None, "n", "u", "m"):
        return None
    return float(match[1]) * scale


class PlannerObservations:
    def __init__(self, snapshot_reader, *, driver=None, qer_reader=None, readers=None,
                 clock=time.time, cache_seconds=5, maximum_age=120):
        self.snapshot_reader, self.driver, self.qer_reader = snapshot_reader, driver, qer_reader
        self.readers = readers or {}
        if set(self.readers) - set(CATALOG):
            raise ValueError("Unknown configured observation reader")
        self.clock, self.cache_seconds, self.maximum_age = clock, cache_seconds, maximum_age
        self.cache = {}
        self.history = deque(maxlen=256)
        self.log_evidence = {}  # Private side channel; never inserted in the LLM view.

    def tools(self):
        return [StageTool(name, description, QUERY_SCHEMA,
            lambda _name=name, **args: self.read(_name, **args)) for name, description in CATALOG.items()]

    def _cached(self, key, read):
        now = self.clock()
        cached = self.cache.get(key)
        if cached and 0 <= now - cached[0] <= self.cache_seconds:
            return deepcopy(cached[1])
        value = read()
        self.cache[key] = (now, deepcopy(value))
        while len(self.cache) > 64:
            del self.cache[next(iter(self.cache))]
        return value

    def _get(self, resource):
        if self.driver is None:
            raise LookupError("not_configured")
        return self._cached(resource, lambda: self.driver.kubectl("get", resource, "-o", "json"))

    @staticmethod
    def _role(pod):
        meta = pod.get("metadata", {})
        name = (meta.get("labels", {}).get("app.kubernetes.io/name") or meta.get("name", "")).lower()
        return next((role for role in NF_TYPES if re.search(r"(?:^|[-_])" + role + r"(?:$|[-_])", name)), None)

    def _pods(self):
        return self._get("pods").get("items", [])

    def _targets(self, target=None):
        pods = self._pods()
        if target is None:
            return pods
        selected = [p for p in pods if self._role(p) == target or _ref("pod", p.get("metadata", {}).get("uid")) == target]
        if not selected:
            raise AgentLoopBlocked("observation.target_outside_inventory")
        return selected

    def _workloads(self, target=None, only_nfs=False):
        rows = []
        for pod in self._targets(target):
            role = self._role(pod)
            if only_nfs and role is None:
                continue
            meta, status = pod.get("metadata", {}), pod.get("status", {})
            containers = status.get("containerStatuses", [])
            desired, owner_ready = None, None
            owner = next((o for o in meta.get("ownerReferences", []) if o.get("controller") is True), None)
            kinds = {"ReplicaSet": "replicasets", "Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}
            if isinstance(owner, dict) and owner.get("kind") in kinds:
                try:
                    controllers = self._get(kinds[owner["kind"]]).get("items", [])
                    matches = [c for c in controllers if c.get("metadata", {}).get("uid") == owner.get("uid")
                               and c.get("metadata", {}).get("name") == owner.get("name")]
                    if len(matches) == 1:
                        controller = matches[0]
                        desired = (controller.get("status", {}).get("desiredNumberScheduled") if owner["kind"] == "DaemonSet"
                                   else controller.get("spec", {}).get("replicas"))
                        owner_ready = controller.get("status", {}).get("numberReady" if owner["kind"] == "DaemonSet" else "readyReplicas")
                        desired = desired if type(desired) is int and desired >= 0 else None
                        owner_ready = owner_ready if type(owner_ready) is int and owner_ready >= 0 else None
                except Exception:
                    pass  # Controller availability must not fabricate counts.
            rows.append({"workload_ref": _ref("pod", meta.get("uid")), "nf_type": role,
                "pod_uid": meta.get("uid"), "pod_name": meta.get("name"),
                "owner_refs": [{"kind": o.get("kind"), "uid": o.get("uid"), "name": o.get("name")}
                               for o in meta.get("ownerReferences", []) if isinstance(o, dict)][:4],
                "desired_pods": desired, "owner_ready_pods": owner_ready,
                "replica_scope": "controller" if desired is not None else "unknown",
                "phase": status.get("phase"), "ready": bool(containers) and all(c.get("ready") is True for c in containers),
                "restarts": sum(c.get("restartCount", 0) for c in containers),
                "waiting_reasons": [c.get("state", {}).get("waiting", {}).get("reason") for c in containers
                                    if c.get("state", {}).get("waiting", {}).get("reason")],
                "termination_reasons": [c.get("lastState", {}).get("terminated", {}).get("reason") for c in containers
                                        if c.get("lastState", {}).get("terminated", {}).get("reason")],
                "node_ref": _ref("node", pod.get("spec", {}).get("nodeName")),
                "deleting": meta.get("deletionTimestamp") is not None,
                "containers": [{"name": c.get("name"), "requests": c.get("resources", {}).get("requests", {}),
                                "limits": c.get("resources", {}).get("limits", {})}
                               for c in pod.get("spec", {}).get("containers", [])],
                "nrf_registration": None, "application_health": None})
        return rows

    def _upf_bindings(self, snapshot):
        """Fence a pod-specific native exporter to a Kubernetes incarnation.

        Use the same deployment labels and incarnation fields as the existing
        daily observer; neither an UPF name nor its kernel epoch is a Pod UID.
        Only an explicitly executed observation tool performs these reads.
        """
        from backend.onprem.evidence.pod_identity import pod_incarnation, select_unique_pod
        owners = snapshot.get("perUe", [])
        keys = sorted({(r.get("slice"), r.get("upf"), r.get("upfEpoch")) for r in owners
                       if all(isinstance(r.get(k), str) and r[k] for k in ("slice", "upf", "upfEpoch"))})
        output = []
        for slice_id, upf, epoch in keys[:16]:
            binding = {"slice": slice_id, "upf": upf, "upfEpoch": epoch, "status": "unknown",
                       "podUid": None, "workloadRef": None, "sharing": "unknown", "source": "pod-native-exporter+incarnation"}
            shared = len({s for s, u, _ in keys if u == upf}) > 1
            if shared:
                binding["sharing"] = "shared"
                output.append(binding)
                continue
            try:
                pod = select_unique_pod(self._pods(), app=upf, role="upf")
                name = pod["metadata"]["name"]
                if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name):
                    raise ValueError
                def incarnation(value):
                    return pod_incarnation(value, name=name, container=upf, namespace=self.driver.namespace, now=self.clock())
                before = incarnation(pod)
                def resolve():
                    path = f"/api/v1/namespaces/{self.driver.namespace}/pods/{name}:9081/proxy/v1/upf-state"
                    native = self.driver.kubectl("get", "--raw", path)
                    after = self.driver.kubectl("get", "pod", name, "-o", "json")
                    if (incarnation(after) != before or native.get("schemaVersion") != "cityverse.upf/v1"
                            or native.get("source") != "gtp5g-netlink" or native.get("available") is not True
                            or native.get("upf") != upf or native.get("epoch") != epoch or native.get("errors") != []
                            or not -1 <= self.clock() - _seconds(native.get("observedAt")) <= 15):
                        raise ValueError
                    return {"podUid": before["podUid"], "workloadRef": _ref("pod", before["podUid"]),
                            "observedAt": native["observedAt"], "status": "bound", "sharing": "dedicated"}
                binding.update(self._cached(("upf-binding", tuple(before.values()), epoch), resolve))
            except Exception:
                pass  # Unknown is display data and never a permission fallback.
            output.append(binding)
        return output

    def _metrics(self, target, *, only_nfs=False):
        if self.driver is None:
            raise LookupError("not_configured")
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.driver.namespace}/pods"
        pods = {p["metadata"]["name"]: p for p in self._targets(target)}
        def collect():
            data = self.driver.kubectl("get", "--raw", path)
            self._metrics_pod_uids = {p["metadata"]["name"]: p["metadata"].get("uid") for p in self._pods()}
            return data
        data = self._cached("metrics", collect)
        rows, times = [], []
        for item in data.get("items", []):
            pod = pods.get(item.get("metadata", {}).get("name"))
            if pod is None or (only_nfs and self._role(pod) is None):
                continue
            uid = pod["metadata"].get("uid")
            metric_uid = item.get("metadata", {}).get("uid")
            if (getattr(self, "_metrics_pod_uids", {}).get(item.get("metadata", {}).get("name")) != uid
                    or metric_uid is not None and metric_uid != uid):
                continue  # A recycled name must never inherit its old Pod's usage.
            identity_status = "uid_matched" if metric_uid == uid and uid else "unverified"
            if metric_uid is None and uid:
                try:
                    window = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m)", item.get("window", ""))
                    duration = float(window[1]) * {"ms": .001, "s": 1, "m": 60}[window[2]] if window else None
                    created = _seconds(pod["metadata"]["creationTimestamp"])
                    if duration is not None and _seconds(item["timestamp"]) - duration >= created:
                        identity_status = "creation_time_fenced"
                except (ValueError, TypeError, KeyError):
                    pass
            rows.append({"workload_ref": _ref("pod", pod["metadata"].get("uid")), "nf_type": self._role(pod),
                "pod_uid": uid, "identity_status": identity_status,
                "observed_at": item.get("timestamp"), "window": item.get("window"),
                "containers": [{"name": c.get("name"), "cpu_cores": _quantity(c.get("usage", {}).get("cpu"), cpu=True),
                                "memory_bytes": _quantity(c.get("usage", {}).get("memory")),
                                "requests": next((s.get("resources", {}).get("requests", {}) for s in pod.get("spec", {}).get("containers", []) if s.get("name") == c.get("name")), {}),
                                "limits": next((s.get("resources", {}).get("limits", {}) for s in pod.get("spec", {}).get("containers", []) if s.get("name") == c.get("name")), {})}
                               for c in item.get("containers", [])]})
            if item.get("timestamp"):
                times.append(_seconds(item["timestamp"]))
        return rows, min(times) if len(times) == len(rows) and rows else None

    @staticmethod
    def _traffic(snapshot, slice_id=None, group=None):
        rows = [r for r in snapshot.get("perUe", []) if (slice_id is None or r.get("slice") == slice_id)
                and (group is None or r.get("eventId") == group)]
        result = {"selected_ues": len(rows)}
        for key in ("receivedUplinkMbps", "receivedDownlinkMbps", "offeredUplinkMbps", "offeredDownlinkMbps", "lossPercent", "rttMs"):
            values = [r.get(key) for r in rows]
            complete = bool(rows) and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values)
            result[key] = (sum(values) if key.endswith("Mbps") else sum(values) / len(values)) if complete else None
        result["observed_transmitting_ues"] = (sum((r["receivedUplinkMbps"] + r["receivedDownlinkMbps"]) > 0 for r in rows)
            if rows and all(type(r.get(k)) in (int, float) and math.isfinite(r[k]) and r[k] >= 0
                            for r in rows for k in ("receivedUplinkMbps", "receivedDownlinkMbps")) else None)
        return result

    def record(self, snapshot):
        identity = (snapshot.get("roundId"), snapshot.get("generation"), snapshot.get("sequence"))
        if not self.history or self.history[-1][0] != identity:
            epochs = sorted({str(r.get("upfEpoch")) for r in snapshot.get("perUe", [])})
            self.history.append((identity, epochs, snapshot.get("observedAt"),
                {name: self._traffic(snapshot, name) for name in SLICES}))

    def environment_snapshot(self):
        """Planner preload needs metadata; explicit tools keep native bindings."""
        return self.read('get_environment_overview', include_bindings=False)

    def resource_snapshot(self):
        """Explicit numeric Planner inputs; unknown/stale resources never become zero."""
        observation = self.read('get_k8s_resource_usage', include_bindings=False)
        result = {'source': observation['source'], 'observed_at': observation['observed_at'],
                  'status': observation['status'], 'scope': 'shared-workloads-not-slice-attributed',
                  'cpu': {'usage_cores': None}, 'memory': {'usage_bytes': None}, 'workloads': [],
                  'missing_fields': list(observation['missing_fields'])}
        if observation['status'] not in {'available', 'partial'}:
            return result
        for row in observation.get('data') or []:
            valid = row.get('identity_status') in {'uid_matched', 'creation_time_fenced'}
            for container in row.get('containers', []):
                requests, limits = container.get('requests') or {}, container.get('limits') or {}
                result['workloads'].append({
                    'workload_ref': row.get('workload_ref'), 'nf_type': row.get('nf_type'),
                    'observed_at': row.get('observed_at'), 'identity_status': row.get('identity_status'),
                    'container': container.get('name'),
                    'cpu_usage_cores': container.get('cpu_cores') if valid else None,
                    'cpu_request_cores': _quantity(requests.get('cpu'), cpu=True),
                    'cpu_limit_cores': _quantity(limits.get('cpu'), cpu=True),
                    'memory_usage_bytes': container.get('memory_bytes') if valid else None,
                    'memory_request_bytes': _quantity(requests.get('memory')),
                    'memory_limit_bytes': _quantity(limits.get('memory')),
                    'cpu_throttling': None, 'shared': True})
        for name, field, target in [('cpu', 'cpu_usage_cores', 'usage_cores'), ('memory', 'memory_usage_bytes', 'usage_bytes')]:
            values = [row[field] for row in result['workloads']]
            if values and all(type(v) in (int, float) and math.isfinite(v) for v in values):
                result[name][target] = sum(values)
            else:
                result['missing_fields'].append(field)
        if not result['workloads'] or result['missing_fields']:
            result['status'] = 'partial'
        return result

    def read(self, name, *, target=None, slice_id=None, window_seconds=None, limit=None, include_bindings=True):
        from backend.onprem.agents.roles import check_arguments
        if name not in CATALOG:
            raise AgentLoopBlocked("tool.not_allowed")
        check_arguments({"target": target, "slice_id": slice_id, "window_seconds": window_seconds, "limit": limit}, QUERY_SCHEMA)
        if target is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", target):
            raise AgentLoopBlocked("observation.invalid_target")
        limit, window_seconds = limit or 50, window_seconds or 300
        snapshot = self.snapshot_reader()
        self.record(snapshot)
        now = self.clock()
        source_time = _seconds(snapshot["observedAt"]) if snapshot.get("observedAt") else None
        envelope = {"status": "available", "scope": "slice" if slice_id else "round", "source": name,
            "observation_ref": _ref("observation", (snapshot.get("roundId"), snapshot.get("generation"), snapshot.get("sequence"))),
            "observed_at": snapshot.get("observedAt"), "collected_started_at": _stamp(now),
            "generation": snapshot.get("generation"), "policy_revision": snapshot.get("policyRevision"),
            "missing_fields": [], "errors": [], "data": None}
        try:
            if name in self.readers:
                # Trusted injected readers must already return sanitized, structured results.
                result = self.readers[name](target=target, slice_id=slice_id, window_seconds=window_seconds, limit=limit)
                if not isinstance(result, dict) or set(result) - {"data", "status", "observed_at", "missing_fields"}:
                    raise ValueError
                envelope.update(result)
                source_time = _seconds(envelope["observed_at"]) if envelope.get("observed_at") else None
            elif name == "get_observation_capabilities":
                kube = {"get_k8s_cluster_health", "get_k8s_workloads", "get_k8s_events", "get_k8s_resource_usage",
                        "get_nf_inventory", "get_nf_health", "get_nf_resource_usage", "get_nf_log_excerpt", "get_n6_status"}
                native = {"get_slice_qos", "get_upf_dataplane_state"}
                envelope["data"] = {tool: {"configured": tool in self.readers or
                    (self.driver is not None if tool in kube else self.qer_reader is not None if tool in native
                     else False if tool == "get_packet_path_metrics" else True)} for tool in CATALOG}
                source_time = now
            elif name == "get_environment_overview":
                parts = {}
                for tool in ("get_k8s_cluster_health", "get_nf_inventory", "get_slice_status", "get_ran_status"):
                    parts[tool] = self.read(tool, slice_id=slice_id, limit=limit, include_bindings=include_bindings)
                envelope["data"] = parts
                if any(p["status"] != "available" for p in parts.values()):
                    envelope["status"] = "partial"
            elif name == "get_k8s_cluster_health":
                nodes = self._get("nodes").get("items", [])
                envelope["data"] = [{"node_ref": _ref("node", n.get("metadata", {}).get("uid")),
                    "conditions": {c.get("type"): c.get("status") for c in n.get("status", {}).get("conditions", [])},
                    "allocatable": n.get("status", {}).get("allocatable", {})} for n in nodes[:limit]]
                source_time = self.cache["nodes"][0]
            elif name in {"get_k8s_workloads", "get_nf_inventory", "get_nf_health"}:
                envelope["data"] = self._workloads(target, only_nfs=name != "get_k8s_workloads")[:limit]
                source_time = self.cache["pods"][0]
                if name != "get_k8s_workloads":
                    envelope.update(status="partial", missing_fields=["nrf_registration", "application_health"])
            elif name == "get_k8s_events":
                pods = self._targets(target)
                allowed = {p["metadata"].get("uid") for p in pods}
                events = self._get("events").get("items", [])
                rows = []
                for event in events:
                    timestamp = event.get("lastTimestamp") or event.get("eventTime") or event.get("metadata", {}).get("creationTimestamp")
                    if not timestamp or not 0 <= now - _seconds(timestamp) <= window_seconds:
                        continue
                    owner = event.get("involvedObject", event.get("regarding", {}))
                    if owner.get("uid") in allowed:
                        rows.append({"workload_ref": _ref("pod", owner["uid"]), "type": event.get("type"),
                                     "reason": event.get("reason"), "count": event.get("count"), "observed_at": timestamp})
                envelope["data"] = rows[-limit:]
                source_time = self.cache["events"][0]
            elif name in {"get_k8s_resource_usage", "get_nf_resource_usage"}:
                rows, source_time = self._metrics(target, only_nfs=name == "get_nf_resource_usage")
                envelope["data"] = rows[:limit]
                if source_time is None:
                    envelope.update(status="partial", missing_fields=["measurement_timestamp_or_samples"])
            elif name == "get_nf_log_excerpt":
                self.log_evidence.pop(target, None)
                pods = self._targets(target)
                if target is None or len(pods) != 1 or self._role(pods[0]) is None:
                    raise AgentLoopBlocked("observation.exact_nf_instance_required")
                pod = pods[0]
                containers = [c.get("name") for c in pod.get("spec", {}).get("containers", [])
                    if c.get("name") in {self._role(pod), pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")}]
                if len(containers) != 1:
                    raise LookupError("unsupported")
                container = containers[0]
                command = ["kubectl", "--context", self.driver.context, "--namespace", self.driver.namespace,
                    "logs", pod["metadata"]["name"], "--container", container,
                    "--since=" + str(window_seconds) + "s", "--tail=" + str(limit)]
                result = self.driver.runner(command, text=True, capture_output=True, timeout=10, check=False)
                if result.returncode:
                    raise ValueError
                # Bind the captured text to the exact Pod incarnation. This is
                # collected only by the explicitly chosen tool, never by GET.
                live = self.driver.kubectl("get", "pod", pod["metadata"]["name"], "-o", "json")
                if live.get("metadata", {}).get("uid") != pod.get("metadata", {}).get("uid"):
                    raise AgentLoopBlocked("observation.log_pod_changed")
                from backend.onprem.agents.topology_observability import redact_log_lines
                self.log_evidence[target] = {"workloadRef": _ref("pod", pod["metadata"]["uid"]),
                    "podUid": pod["metadata"]["uid"], "container": container, "observedAt": _stamp(now),
                    "lines": redact_log_lines(result.stdout, limit=limit)}
                while len(self.log_evidence) > 32:
                    del self.log_evidence[next(iter(self.log_evidence))]
                # Only normalized diagnostic categories leave this reader. Raw
                # log text can contain credentials even without recognizable keys.
                categories = Counter()
                for line in result.stdout.splitlines()[-limit:]:
                    for code in ("authentication", "registration", "pfcp", "ngap", "timeout", "error", "warning", "oom"):
                        if code in line.lower():
                            categories[code] += 1
                envelope["data"] = {"workload_ref": target, "categories": dict(categories), "raw_text_included": False}
                source_time = now
            elif name in {"get_slice_topology", "get_slice_status", "get_ue_session_summary"}:
                raw = snapshot.get("_actuatorSnapshot", snapshot)
                owners = snapshot.get("perUe", [])
                names = [slice_id] if slice_id else SLICES
                envelope["data"] = {key: {"sst": SLICES[key]["sst"], "sd": SLICES[key]["sd"], "dnn": "internet",
                    "selected_ues": sum(r.get("slice") == key for r in owners),
                    "observed_upfs": sorted({r["upf"] for r in owners if r.get("slice") == key and r.get("upf")}),
                    "requested_ues": sum(count for group, count in snapshot.get("allocation", {}).get("events", {}).items() if SCENARIO_SLICES[group] == key)
                        + (snapshot.get("allocation", {}).get("baselineUes", 0) if key == "embb" else 0),
                    "registration_counts_scope": "round", "counts": raw.get("counts", {}),
                    "traffic": self._traffic(snapshot, key), "shared_control_nf_binding": None} for key in names}
                envelope["missing_fields"] = ["per_slice_control_nf_binding", "per_slice_registration_if_not_observed"]
                envelope["status"] = "partial"
            elif name in {"get_slice_qos", "get_upf_dataplane_state"}:
                if self.qer_reader is None:
                    raise LookupError("not_configured")
                proof = self._cached(("qer", snapshot.get("sequence")), lambda: self.qer_reader(snapshot))
                source_time = _seconds(proof["observedAt"])
                if proof.get("source") != "native-upf-qer":
                    raise ValueError
                rows = [r for r in proof.get("sessions", []) if slice_id is None or r.get("slice") == slice_id]
                envelope["data"] = {"scope": "selected-round-sessions", "session_count": len(rows),
                    "qer_count": sum(len(r.get("qers", [])) for r in rows),
                    "rates_kbps": sorted({(q["mbrKbps"]["ul"], q["mbrKbps"]["dl"]) for r in rows for q in r.get("qers", [])}),
                    "upf_epochs": sorted({r.get("upfEpoch") for r in rows if r.get("upfEpoch")})}
                envelope.update(status="partial", missing_fields=["5qi", "arp", "far_details", "pfcp_peer_heartbeat"])
            elif name in {"get_ran_status", "get_nf_peer_status"}:
                raw = snapshot.get("_actuatorSnapshot", snapshot)
                health = raw.get("ranHealth") or raw.get("connectivityEvidence", {}).get("ranHealth")
                # Return only declared summary fields, never arbitrary endpoint payloads.
                envelope["data"] = ({key: health[key] for key in ("source", "connected", "observedAt") if key in health}
                                    if isinstance(health, dict) else None)
                envelope.update(status="partial" if health else "unavailable", missing_fields=["sbi_metrics", "pfcp_peer_heartbeat", "per_ue_ngap_binding"])
            elif name == "get_control_plane_failures":
                raw = snapshot.get("_actuatorSnapshot", snapshot)
                failures = [r.get("failureCode") for r in raw.get("perUe", []) if r.get("failureCode")]
                known = {"ue.registration_rejected", "ue.pdu_rejected", "ue.authentication_failed"}
                envelope["data"] = {"observed_failure_codes": dict(Counter(c if c in known else "unclassified" for c in failures)),
                                    "failure_rate": None, "coverage": "last-committed-selected-rows"}
                envelope.update(status="partial", missing_fields=["interval_counters_and_denominators"])
            elif name == "get_n6_status":
                workloads = self._workloads()
                n6 = {_ref("pod", p["metadata"].get("uid")) for p in self._pods() if "n6" in p["metadata"].get("name", "")}
                envelope["data"] = {"workloads": [w for w in workloads if w["workload_ref"] in n6][:limit],
                                    "traffic": self._traffic(snapshot, slice_id), "listener_health": None}
                envelope.update(status="partial", missing_fields=["listener_readback"])
                source_time = min(source_time, self.cache["pods"][0]) if source_time is not None else None
            elif name == "get_traffic_metrics":
                if target and target not in SCENARIO_SLICES:
                    raise AgentLoopBlocked("observation.unknown_group")
                envelope["data"] = self._traffic(snapshot, slice_id, target)
            elif name == "get_metric_trend":
                current = self.history[-1]
                rows = [item for item in self.history if item[0][:2] == current[0][:2] and item[1] == current[1]
                        and item[2] and 0 <= now - _seconds(item[2]) <= window_seconds]
                envelope["data"] = {"samples": [{"observed_at": item[2], "slices": {slice_id: item[3][slice_id]} if slice_id else item[3]}
                                               for item in rows[-limit:]], "cross_epoch_samples_excluded": True}
                if len(rows) < 2:
                    envelope.update(status="partial", missing_fields=["sufficient_same_epoch_samples"])
            else:
                raise LookupError("not_configured")
            if source_time is None and envelope["status"] == "available":
                envelope.update(status="partial", missing_fields=[*envelope["missing_fields"], "observation_timestamp"])
            elif source_time is not None and not -1 <= now - source_time <= self.maximum_age:
                envelope["status"] = "stale"
        except AgentLoopBlocked:
            raise
        except LookupError as exc:
            envelope.update(status=str(exc) if str(exc) in {"not_configured", "unsupported"} else "unavailable", data=None)
        except Exception:
            envelope.update(status="unavailable", errors=["reader_unavailable"], data=None)
        envelope["observed_at"] = _stamp(source_time) if source_time is not None else None
        envelope["collected_finished_at"] = _stamp(self.clock())
        if include_bindings and name in {"get_k8s_workloads", "get_nf_inventory", "get_nf_health", "get_k8s_resource_usage", "get_nf_resource_usage"}:
            envelope["upf_bindings"] = self._upf_bindings(snapshot) if self.driver is not None else []
        return envelope
