"""Current native resource telemetry. Missing evidence is returned, never filled with estimates."""
from __future__ import annotations

import math
import time
from threading import RLock
from concurrent.futures import ThreadPoolExecutor

from .actuators import ActionRequest
from .observations import Metric
from .domain import ContractError
from .resource_adapters import parse_cpu_millis, parse_memory_bytes, _version
from backend.onprem.policy.group_qer import _ambr_qer


class NativeResourceTelemetry:
    def __init__(self, providers, *, qos_targets=None, nf_targets=None,
                 path_capacity_reader=None, resource_metric_reader=None, clock=time.time,
                 maximum_age_seconds=15):
        self.providers = providers
        self.qos_targets, self.nf_targets = qos_targets or {}, nf_targets or {}
        self.path_capacity_reader = path_capacity_reader
        self.resource_metric_reader = resource_metric_reader
        self.clock = clock
        if type(maximum_age_seconds) not in (int, float) or not 0 < maximum_age_seconds <= 15:
            raise ContractError('native_resource_freshness_outside_bound')
        self.maximum_age = maximum_age_seconds
        self._cpu_previous = {}
        self._lock = RLock()
        self._nf_collectors = {}
        self._skip_nodes = False

    def collect(self, session_bindings, current_receiver_rows=()):
        with self._lock:
            return self._collect(session_bindings, current_receiver_rows)

    def _collect(self, session_bindings, current_receiver_rows):
        now = self.clock()
        sessions = tuple(session_bindings)
        by_key = {s.key.token: s for s in sessions}
        metrics, versions, resources, missing = [], {}, {}, []

        def emit(name, value, unit, source, stamp, target, window=1, scope="resource"):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                missing.append(f"{target}:{name}:invalid")
                return
            valid = "valid" if 0 <= self.clock() - stamp <= self.maximum_age else "stale"
            metrics.append(Metric(name, value, unit, source, stamp, window, target, valid, scope=scope))

        qos = self.providers.get("qos")
        if qos and by_key:
            try:
                request = ActionRequest("update_qos", "native-current-sessions", "telemetry",
                    "read-only", "read-only", session_keys=tuple(sorted(by_key)))
                inventory = qos.inspect(request)
                rows = inventory["sessions"]
                stamp = inventory["observed_at"]
                if set(rows) != set(by_key) or not 0 <= self.clock() - stamp <= self.maximum_age:
                    raise ValueError('Incomplete or stale native QER cohort')
                for key, row in rows.items():
                    rates = _ambr_qer(row["qers"])["mbrKbps"]
                    emit("qer_cap_bps", (rates["ul"] + rates["dl"]) * 1000,
                         "bps", "native-upf-qer", stamp, key, scope="per_session")
                    for direction in ("ul", "dl"):
                        emit(f"qer_{direction}_cap_bps", rates[direction] * 1000,
                             "bps", "native-upf-qer", stamp, key, scope="per_session")
                for target, selector in self.qos_targets.items():
                    selected = tuple(sorted(key for key, session in by_key.items()
                        if all(getattr(session, field, None) == value for field, value in selector.items())))
                    if selected:
                        versions[target] = _version({key: rows[key] for key in selected})
                for key in rows:
                    versions[key] = _version({key: rows[key]})
            except Exception:
                missing.append("native-qer-inventory:unavailable")
        elif by_key:
            missing.append("native-qer-inventory:not_configured")

        # Offered load is emitted only for explicitly accepted, current, exact-session rows.
        for row in current_receiver_rows:
            key = row.get("session_key")
            stamp = row.get("observed_at")
            if (key not in by_key or row.get("accepted") is not True
                    or row.get("source") != "native-accepted-traffic-window"
                    or type(stamp) not in (int, float) or not 0 <= now - stamp <= self.maximum_age):
                continue
            for name in ("offered_bps", "received_bps"):
                if name in row:
                    emit(name, row[name], "bps", row["source"], stamp, key,
                         row.get("window_seconds", 1), "per_session")

        if self.path_capacity_reader:
            batch_reader = getattr(self.path_capacity_reader, 'current_capacities', None)
            batch_capacities = None
            if callable(batch_reader):
                try:
                    batch_capacities = batch_reader(sessions)
                except Exception:
                    batch_capacities = {}
            for session in sessions:
                try:
                    proof = (batch_capacities[session.key.token] if batch_capacities is not None
                             else self.path_capacity_reader(session))
                    stamp = proof.get("observed_at")
                    if (proof.get("source") != "commissioned-path-capacity"
                            or proof.get("session_key") != session.key.token
                            or proof.get("path_revision") != session.path_revision
                            or proof.get("verified") is not True
                            or type(stamp) not in (int, float) or not 0 <= self.clock() - stamp <= self.maximum_age):
                        raise ValueError("Unbound path capacity")
                    emit("path_capacity_bps", proof.get("capacity_bps"), "bps", proof["source"],
                         stamp, session.key.token, scope="per_session")
                    for direction in ("ul", "dl"):
                        if direction + "_capacity_bps" in proof:
                            emit(f"path_{direction}_capacity_bps", proof[direction + "_capacity_bps"],
                                 "bps", proof["source"], stamp, session.key.token, scope="per_session")
                except Exception:
                    missing.append(f"{session.key.token}:path_capacity_bps:unavailable")
        elif sessions:
            missing.append("path_capacity_bps:not_configured")

        kubernetes = self.providers.get("kubernetes")
        collected_nodes = set()
        if kubernetes:
            nodes = set()
            current_cpu = {}
            parallel = len(self.nf_targets) > 1
            if parallel:
                def read_nf(item):
                    target, specification = item
                    child = self._nf_collectors.get(target)
                    if child is None:
                        child = NativeResourceTelemetry({'kubernetes': kubernetes},
                            nf_targets={target: specification}, clock=self.clock,
                            maximum_age_seconds=self.maximum_age)
                        child._skip_nodes = True
                        self._nf_collectors[target] = child
                    return child.collect(())
                with ThreadPoolExecutor(max_workers=min(4, len(self.nf_targets)),
                                        thread_name_prefix='native-resource-read') as pool:
                    parts = list(pool.map(read_nf, self.nf_targets.items()))
                for part in parts:
                    metrics.extend(part['metrics'])
                    versions.update(part['target_versions'])
                    missing.extend(part['missing'])
                    nodes.update(part['_nodes'])
            for nf_id, specification in (() if parallel else self.nf_targets.items()):
                pod = specification.get("pod") if isinstance(specification, dict) else specification
                selected_container = specification.get("container") if isinstance(specification, dict) else None
                try:
                    pod_started = self.clock()
                    req = ActionRequest("resize_nf", pod, "telemetry", "read-only", "read-only")
                    native = kubernetes.inspect(req)
                    versions[pod] = str(native["version"])
                    versions[nf_id] = str(native["version"])
                    if native.get("node"):
                        nodes.add(native["node"])
                    statuses = native.get("status", {}).get("containerStatuses", [])
                    selected_statuses = [s for s in statuses
                        if selected_container is None or s.get("name") == selected_container]
                    if selected_statuses:
                        ready = all(s.get("ready") is True for s in selected_statuses)
                        restarts = sum(s.get("restartCount", 0) for s in selected_statuses)
                        for target in set((nf_id, pod)):
                            emit("nf_ready", int(ready), "ratio", "kubernetes-pod-status", pod_started, target)
                            emit("nf_restarts", restarts, "count", "kubernetes-pod-status", pod_started, target)
                    # A city batch is not proof of every user of a shared NF.
                    # Only a complete native-cohort collector may emit NF total load.
                    for container in native["containers"]:
                        for category in ("requests", "limits"):
                            values = container.get("resources", {}).get(category, {})
                            target = f"{nf_id}/{container['name']}"
                            if "cpu" in values:
                                emit(f"cpu_{category}", parse_cpu_millis(values["cpu"]) / 1000,
                                     "cores", "kubernetes-pod-spec", pod_started, target)
                            if "memory" in values:
                                emit(f"memory_{category}", parse_memory_bytes(values["memory"]),
                                     "bytes", "kubernetes-pod-spec", pod_started, target)
                        try:
                            if (selected_container and container["name"] != selected_container):
                                continue
                            if not selected_container and len(native["containers"]) != 1:
                                raise ValueError("NF container selection is ambiguous")
                            if kubernetes.memory_reader is not None:
                                try:
                                    memory = kubernetes.memory_reader(pod, container["name"])
                                    if (memory.get("fresh") is not True or memory.get("uid") != native["uid"]
                                            or memory.get("source") != "native-container-cgroup"
                                            or type(memory.get("observed_at")) not in (int, float)
                                            or memory.get('container') != container['name']
                                            or not 0 <= self.clock() - memory["observed_at"] <= self.maximum_age):
                                        raise ValueError("Unbound memory sample")
                                    for target in set((nf_id, pod)):
                                        emit("memory_working_set_bytes", memory["working_set_bytes"], "bytes",
                                             memory["source"], memory["observed_at"], target)
                                except Exception:
                                    missing.append(f"{nf_id}:memory_working_set_bytes:unavailable")
                            else:
                                missing.append(f"{nf_id}:memory_working_set_bytes:not_configured")
                            counter = kubernetes.client.cpu_stat(pod, container["name"])
                            counter_at = self.clock()
                            statuses = native.get("status", {}).get("containerStatuses", [])
                            restart = next((s.get("restartCount", 0) for s in statuses
                                            if s["name"] == container["name"]), 0)
                            after = kubernetes.inspect(req)
                            after_restart = next((s.get("restartCount", 0) for s in after.get("status", {}).get("containerStatuses", [])
                                                  if s["name"] == container["name"]), 0)
                            if (after["uid"] != native["uid"] or restart != after_restart
                                    or after['version'] != native['version']
                                    or not 0 <= self.clock() - pod_started <= self.maximum_age):
                                raise ValueError("Pod identity changed during cgroup read")
                            identity = (pod, native["uid"], container["name"], restart)
                            old = self._cpu_previous.get(identity)
                            current_cpu[identity] = (counter_at, counter)
                            if old and counter_at > old[0]:
                                periods = counter["nr_periods"] - old[1]["nr_periods"]
                                throttled = counter["nr_throttled"] - old[1]["nr_throttled"]
                                if periods > 0 and 0 <= throttled <= periods:
                                    for target in set((nf_id, pod)):
                                        emit("cpu_throttled_percent", throttled * 100 / periods,
                                             "percent", "native-container-cgroup", counter_at, target, counter_at - old[0])
                            else:
                                missing.append(f"{nf_id}:cpu_throttled_percent:needs_second_sample")
                        except Exception:
                            missing.append(f"{nf_id}:cpu_throttled_percent:unavailable")
                    closing_nf = kubernetes.inspect(req)
                    if (closing_nf['uid'] != native['uid'] or closing_nf['version'] != native['version']
                            or not 0 <= self.clock() - pod_started <= self.maximum_age):
                        raise ValueError('NF changed during acquisition')
                except Exception:
                    metrics = [m for m in metrics if m.resource_id not in {nf_id, pod}
                               and not m.resource_id.startswith(nf_id + '/')]
                    versions.pop(nf_id, None)
                    versions.pop(pod, None)
                    missing.append(f"{nf_id}:native_nf:unavailable")
            self._cpu_previous = current_cpu
            collected_nodes = nodes
            for node in (() if self._skip_nodes else sorted(nodes)):
                try:
                    node_started = self.clock()
                    native = kubernetes.client.call("get", "node", node, "-o", "json")
                    pods = kubernetes.client.call("get", "pods", "--all-namespaces",
                        "--field-selector", "spec.nodeName=" + node, "-o", "json")
                    closing = kubernetes.client.call("get", "node", node, "-o", "json")
                    def identity(value):
                        return (value.get('metadata', {}).get('uid'), value.get('spec'), value.get('status'))
                    if (not native.get('metadata', {}).get('uid') or identity(native) != identity(closing)
                            or native.get('metadata', {}).get('deletionTimestamp')
                            or closing.get('metadata', {}).get('deletionTimestamp')
                            or not 0 <= self.clock() - node_started <= self.maximum_age):
                        raise ValueError('Node identity or capacity changed during acquisition')
                    total = native["status"]["allocatable"]
                    cpu = parse_cpu_millis(total["cpu"]) / 1000
                    memory = parse_memory_bytes(total["memory"])
                    for pod in pods.get("items", []):
                        if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                            continue
                        spec = pod.get("spec", {})
                        for container in spec.get("containers", []) + spec.get("initContainers", []):
                            reserved = container.get("resources", {}).get("requests", {})
                            cpu -= parse_cpu_millis(reserved.get("cpu", "0")) / 1000
                            memory -= parse_memory_bytes(reserved.get("memory", "0"))
                        cpu -= parse_cpu_millis(spec.get("overhead", {}).get("cpu", "0")) / 1000
                        memory -= parse_memory_bytes(spec.get("overhead", {}).get("memory", "0"))
                    conditions = {c.get("type"): c.get("status") for c in native["status"].get("conditions", [])}
                    healthy = (conditions.get("Ready") == "True"
                               and native.get("spec", {}).get("unschedulable") is not True
                               and not any(t.get("effect") in {"NoSchedule", "NoExecute"}
                                           for t in native.get("spec", {}).get("taints", []))
                               and all(conditions.get(k) != "True" for k in ("MemoryPressure", "DiskPressure", "PIDPressure")))
                    if healthy:
                        resources[f"{node}:cpu_cores"] = max(0., cpu)
                        resources[f"{node}:memory_bytes"] = max(0., memory)
                        emit("node_free_cpu", max(0., cpu), "cores", "kubernetes-allocatable-minus-requests", node_started, node)
                        emit("node_free_memory", max(0., memory), "bytes", "kubernetes-allocatable-minus-requests", node_started, node)
                    else:
                        missing.append(f"{node}:node_capacity:not_ready")
                except Exception:
                    missing.append(f"{node}:node_capacity:unavailable")
        elif self.nf_targets:
            missing.extend(f'{target}:native_nf:not_configured' for target in self.nf_targets)

        if self.resource_metric_reader:
            try:
                for raw in self.resource_metric_reader(sessions):
                    # Unit-qualified samples must come from a trusted, current native collector.
                    if (raw.get("source") and raw.get("validity", "valid") == "valid"
                            and raw.get("name") in {"cpu_throttled_percent", "nf_pending_cpu",
                                "link_utilization_percent", "rtt", "rtt_target", "memory_working_set_bytes"}):
                        metric = Metric(**raw)
                        if metric.usable(self.clock(), self.maximum_age):
                            metrics.append(metric)
            except Exception:
                missing.append("native-resource-metrics:unavailable")

        # Multiple sources cannot silently compete for the same semantic metric.
        identities, unique = set(), []
        for metric in metrics:
            if not metric.usable(self.clock(), self.maximum_age):
                missing.append(f'{metric.resource_id}:{metric.name}:stale')
                continue
            identity = (metric.name, metric.resource_id, metric.statistic, metric.scope)
            if identity in identities:
                missing.append(f"{metric.resource_id}:{metric.name}:ambiguous")
                unique = [m for m in unique if (m.name, m.resource_id, m.statistic, m.scope) != identity]
            else:
                identities.add(identity)
                unique.append(metric)
        for key in tuple(resources):
            node, resource = key.rsplit(':', 1)
            name = 'node_free_cpu' if resource == 'cpu_cores' else 'node_free_memory'
            if not any(m.resource_id == node and m.name == name for m in unique):
                resources.pop(key)
        return {"metrics": tuple(unique), "available_resources": resources,
                "target_versions": versions, "missing": tuple(sorted(set(missing))),
                "_nodes": tuple(sorted(collected_nodes))}
