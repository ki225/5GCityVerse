"""Read current cgroup memory and a complete commissioned NF service inventory."""
from __future__ import annotations

import math
import re
import time

from .actuators import ActuationBlocked


class NativeKubernetesObserver:
    """Fixed argv and exact Pod generation; never infer services from a city subset."""

    def __init__(self, client, *, pod_allowlist, service_inventory_reader=None,
                 maximum_age_seconds=15, clock=time.time):
        self.client = client
        self.pods = frozenset(pod_allowlist)
        self.service_inventory_reader = service_inventory_reader
        self.maximum_age = maximum_age_seconds
        self.clock = clock

    def _pod(self, pod, container=None):
        if pod not in self.pods or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", pod):
            raise ActuationBlocked("Pod is outside native observer scope")
        value = self.client.call("get", "pod", pod, "-o", "json")
        if value.get("metadata", {}).get("name") != pod or not value["metadata"].get("uid"):
            raise ActuationBlocked("Exact Pod identity is unavailable")
        if container is None:
            return value, None
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", container):
            raise ActuationBlocked("Exact container name is required")
        rows = [s for s in value.get("status", {}).get("containerStatuses", ())
                if s.get("name") == container]
        if len(rows) != 1 or rows[0].get("ready") is not True or "running" not in rows[0].get("state", {}):
            raise ActuationBlocked("Exact running container is unavailable")
        return value, rows[0]

    def memory(self, pod, container):
        started = self.clock()
        before, old = self._pod(pod, container)
        argv = ["kubectl", "--context", self.client.context, "--namespace", self.client.namespace,
                f"--request-timeout={self.client.timeout}s", "exec", pod, "-c", container, "--", "cat"]
        result = None
        for paths in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat"),
                      ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat")):
            try:
                result = self.client.runner([*argv, *paths], capture_output=True, text=True,
                                           timeout=self.client.timeout + 2, check=False)
            except Exception:
                continue
            if result.returncode == 0:
                break
        if result is None or result.returncode or len(result.stdout.encode()) > 16384:
            raise ActuationBlocked("Native cgroup memory is unavailable")
        try:
            lines = result.stdout.splitlines()
            usage = int(lines[0])
            counters = {}
            for line in lines[1:]:
                key, raw = line.split()
                if key in counters:
                    raise ValueError("Duplicate memory counter")
                counters[key] = int(raw)
            if usage < 0 or any(value < 0 for value in counters.values()):
                raise ValueError("Negative memory counter")
            inactive = counters.get("total_inactive_file", counters.get("inactive_file"))
            if inactive is None or inactive > usage:
                raise ValueError("Working set counter is unavailable")
        except (ValueError, IndexError):
            raise ActuationBlocked("Native cgroup memory is invalid") from None
        observed = self.clock()
        after, current = self._pod(pod, container)
        if (after["metadata"]["uid"] != before["metadata"]["uid"]
                or current.get("restartCount") != old.get("restartCount")
                or not 0 <= self.clock() - started <= self.maximum_age):
            raise ActuationBlocked("Container generation changed during memory observation")
        return {"fresh": True, "uid": after["metadata"]["uid"], "container": container,
                "restart_count": current.get("restartCount", 0),
                "working_set_bytes": usage - inactive, "usage_bytes": usage,
                "observed_at": observed, "source": "native-container-cgroup"}

    def services(self, pod):
        if self.service_inventory_reader is None:
            raise ActuationBlocked("Complete native NF service inventory is not configured")
        before, _ = self._pod(pod)
        proof = self.service_inventory_reader(pod)
        at = proof.get("observed_at")
        if (proof.get("source") != "native-smf-active-sessions" or proof.get("complete") is not True
                or proof.get("pod_uid") != before["metadata"]["uid"]
                or not proof.get("smf_boot_epoch") or not proof.get("upf_epoch")
                or type(at) not in (int, float) or not math.isfinite(at)
                or not 0 <= self.clock() - at <= self.maximum_age
                or not isinstance(proof.get("sessions"), list)):
            raise ActuationBlocked("Fresh complete native service inventory is required")
        seen, services = set(), set()
        for row in proof["sessions"]:
            key = row.get("session_key")
            ids = row.get("service_ids")
            if (not isinstance(key, str) or not key or key in seen
                    or not isinstance(ids, list) or not ids
                    or any(not isinstance(v, str) or not v for v in ids)):
                raise ActuationBlocked("Native service classification is incomplete")
            seen.add(key)
            services.update(ids)
        after, _ = self._pod(pod)
        if after["metadata"]["uid"] != before["metadata"]["uid"]:
            raise ActuationBlocked("NF identity changed during service inventory")
        return tuple(sorted(services))
