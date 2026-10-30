"""Bounded, exact-UE RTT probes in a separate window; never invoked by GET."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import ipaddress
import hashlib
import json
import math
import re
import shlex
import subprocess
import time

from backend.onprem.runtime.errors import EvidenceUnavailable
from backend.onprem.runtime.topology import ENDPOINTS
from backend.onprem.runtime.current_sessions import V2, session_rows, session_identity, canonical_session_key, IDENTITY_FIELDS


def cohort_fingerprint(snapshot):
    if snapshot.get('schemaVersion') == V2:
        rows = sorted(({k: r.get(k) for k in IDENTITY_FIELDS + ('scenario',)}
                       for r in session_rows(snapshot)), key=lambda r: r['sessionKey'])
        value = {'roundId': snapshot.get('roundId'), 'generation': snapshot.get('generation'),
                 'policyRevision': snapshot.get('policyRevision'), 'populationRevision': snapshot.get('populationRevision', 0),
                 'schemaVersion': V2, 'perSession': rows}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    fields = ("supi", "scenario", "slice", "sessionId", "podUid", "ueIp", "tun", "upf", "upfEpoch",
              "smContextRef", "pfcpSeid", "dnn", "sst", "sd")
    rows = sorted(({k: r.get(k) for k in fields} for r in snapshot.get("perUe", [])), key=lambda r: r["supi"])
    value = {"roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
             "policyRevision": snapshot.get("policyRevision"), "perUe": rows}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def rtt_command(bindings, fanout=64):
    """Fixed 3-packet probe; one compact summary line per bounded TUN target."""
    if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 100 or type(fanout) is not int or not 1 <= fanout <= 64:
        raise EvidenceUnavailable("rtt.invalid_bindings")
    script = ["set -eu"]
    for index, (supi, row) in enumerate(bindings.items()):
        interface, source_ip, endpoint = row[0], row[1], row[2]
        try:
            identity_valid = isinstance(supi, str) and (re.fullmatch(r'imsi-[0-9]{15}', supi) is not None
                                                       or canonical_session_key(supi).token == supi)
        except ValueError:
            identity_valid = False
        if (not identity_valid
                or not isinstance(interface, str) or re.fullmatch(r"(?:ueTun|uesimtun)[0-9]{1,3}", interface) is None):
            raise EvidenceUnavailable("rtt.invalid_identity")
        try:
            if str(ipaddress.IPv4Address(endpoint)) != endpoint:
                raise ValueError
            if str(ipaddress.IPv4Address(source_ip)) != source_ip:
                raise ValueError
        except (ValueError, TypeError):
            raise EvidenceUnavailable("rtt.invalid_endpoint") from None
        # Parse only numeric packet/mean fields. No arbitrary command, file,
        # endpoint, raw ping output or container log can escape this renderer.
        script.append("(local_ip=$(ip -o -4 addr show dev " + shlex.quote(interface)
            + " 2>/dev/null | awk '{split($4,a,\"/\");print a[1]}'); reply=''; if [ \"$local_ip\" = "
            + shlex.quote(source_ip) + " ]; then reply=$(LC_ALL=C ping -n -q -I " + shlex.quote(interface)
            + " -c 3 -i 0.2 -W 1 -w 2 " + shlex.quote(endpoint) + " 2>/dev/null || true); fi; "
            + "printf '%s\\n' \"$reply\" | awk -v supi=" + shlex.quote(supi) + " -v tun=" + shlex.quote(interface)
            + " 'BEGIN {tx=0;rx=0;mean=\"-\"} /packets transmitted/ {tx=$1;rx=$4} /min\\/avg\\/max/ {split($4,r,\"/\");mean=r[2]} "
            + "END {printf \"%s\\t%s\\t%d\\t%d\\t%s\\n\",supi,tun,tx,rx,mean}') &")
        if (index + 1) % fanout == 0:
            script.append("wait")
    script.append("wait")
    return ["sh", "-c", "\n".join(script)]


def parse_rtt(output, bindings):
    if not isinstance(output, str) or len(output.encode()) > 65536:
        raise EvidenceUnavailable("rtt.invalid_output")
    rows = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or parts[0] not in bindings or parts[0] in rows or parts[1] != bindings[parts[0]][0]:
            raise EvidenceUnavailable("rtt.foreign_reply")
        try:
            tx, rx = int(parts[2]), int(parts[3])
            mean = float(parts[4]) if parts[4] != "-" else None
        except ValueError:
            raise EvidenceUnavailable("rtt.invalid_statistics") from None
        if not 0 <= rx <= tx <= 3 or (rx > 0 and (mean is None or not math.isfinite(mean) or not 0 <= mean <= 60000)):
            raise EvidenceUnavailable("rtt.invalid_statistics")
        rows[parts[0]] = {"transmitted": tx, "received": rx, "sampleCount": rx, "meanMs": mean if rx else None}
    if set(rows) != set(bindings):
        raise EvidenceUnavailable("rtt.incomplete_reply")
    return rows


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _run(driver, args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("rtt.deadline")
    result = driver.runner(["kubectl", "--context", driver.context, "--namespace", driver.namespace, *args],
        text=True, capture_output=True, timeout=remaining, check=False)
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout.encode()) > 4 * 1024 * 1024:
        raise EvidenceUnavailable("rtt.read_unavailable")
    return result.stdout


def _epoch(pod, driver, owner_supis):
    meta, status = pod.get("metadata", {}), pod.get("status", {})
    if (meta.get("namespace") != driver.namespace or meta.get("deletionTimestamp")
            or meta.get("labels", {}).get("cityverse.io/managed-by") != "onprem-ue-controller"
            or status.get("phase") != "Running" or not isinstance(meta.get("uid"), str)):
        raise EvidenceUnavailable("rtt.pod_identity")
    owned = json.loads(meta.get("annotations", {}).get("cityverse.io/supis", "null"))
    if not isinstance(owned, list) or not set(owner_supis) <= set(owned):
        raise EvidenceUnavailable("rtt.pod_cohort")
    containers = [c for c in status.get("containerStatuses", []) if c.get("name") == "ue"]
    if len(containers) != 1 or containers[0].get("ready") is not True or not containers[0].get("containerID"):
        raise EvidenceUnavailable("rtt.container_identity")
    c = containers[0]
    return meta["uid"], c["containerID"], c.get("restartCount"), tuple(owned)


def collect_rtt(driver, snapshot, *, cancelled=lambda: False, deadline=None, read_transport=None):
    """At most eight Pods concurrently; Pod-internal probes have a two-second cap.

    Existing native generation/lease guards are supplied by the caller. Each
    command independently pins Pod UID/container/restart before and after.
    Cooperative cancellation skips undispatched Pods and drains bounded execs.
    """
    start = time.time()
    deadline = min(time.monotonic() + 12, deadline if deadline is not None else float("inf"))
    base = {"source": "native-ue-tun-icmp", "statistic": "mean", "startedAt": _stamp(start),
            "finishedAt": None, "relation": "adjacent-probe-window", "sameTrafficWindow": False,
            "roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"), "rows": []}
    base["cohortSha256"] = cohort_fingerprint(snapshot)
    owners = session_rows(snapshot)
    if driver is None or not callable(getattr(driver, "runner", None)) or not 1 <= len(owners) <= 100:
        return {**base, "status": "unsupported", "reason": "rtt.driver_not_configured", "finishedAt": _stamp(time.time())}
    try:
        if cancelled() or time.monotonic() >= deadline:
            return {**base, "status": "timeout", "reason": "rtt.cancelled_or_deadline", "finishedAt": _stamp(time.time())}
        inventory = json.loads(_run(driver, ["get", "pods", "-o", "json"], deadline)).get("items", [])
        pods = {p.get("metadata", {}).get("uid"): p for p in inventory}
        groups = {}
        for owner in owners:
            if owner.get("podUid") not in pods or owner.get("slice") not in ENDPOINTS:
                raise EvidenceUnavailable("rtt.owner_missing")
            if pods[owner["podUid"]].get("metadata", {}).get("labels", {}).get("cityverse.io/generation") != str(snapshot.get("generation")):
                raise EvidenceUnavailable("rtt.foreign_generation")
            groups.setdefault(owner["podUid"], []).append(owner)
        def sample(uid, rows):
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("rtt.cancelled_or_deadline")
            pod = pods[uid]
            name = pod["metadata"]["name"]
            bindings = {session_identity(r): (r["tun"], r["ueIp"], ENDPOINTS[r["slice"]]) for r in rows}
            owner_supis = {r['supi'] for r in rows}
            pinned = _epoch(pod, driver, owner_supis)
            fresh = json.loads(_run(driver, ["get", "pod", name, "-o", "json"], deadline))
            if _epoch(fresh, driver, owner_supis) != pinned:
                raise EvidenceUnavailable("rtt.pod_changed")
            if cancelled():
                raise TimeoutError("rtt.cancelled")
            started = time.time()
            if read_transport is not None:
                output = read_transport.execute("rtt", pod, timeout=min(8, deadline-time.monotonic()), bindings=bindings, fanout=64)
            else:
                output = _run(driver, ["exec", name, "-c", "ue", "--", *rtt_command(bindings)], deadline)
            finished = time.time()
            measured = parse_rtt(output, bindings)
            fresh = json.loads(_run(driver, ["get", "pod", name, "-o", "json"], deadline))
            if _epoch(fresh, driver, owner_supis) != pinned or cancelled():
                raise EvidenceUnavailable("rtt.pod_changed_or_cancelled")
            return [{**{k: r.get(k) for k in ("supi", "scenario", "slice", "sessionId", "podUid", "ueIp", "tun", "upf", "upfEpoch")},
                **{k: r[k] for k in IDENTITY_FIELDS if k in r},
                **measured[session_identity(r)], "startedAt": _stamp(started), "finishedAt": _stamp(finished)} for r in rows]
        with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
            futures = [pool.submit(sample, uid, rows) for uid, rows in groups.items()]
            for future in as_completed(futures):
                base["rows"].extend(future.result())
        return {**base, "status": "observed", "reason": None, "finishedAt": _stamp(time.time())}
    except (TimeoutError, subprocess.TimeoutExpired):
        return {**base, "rows": [], "status": "timeout", "reason": "rtt.cancelled_or_deadline", "finishedAt": _stamp(time.time())}
    except Exception:
        if cancelled() or time.monotonic() >= deadline:
            return {**base, "rows": [], "status": "timeout", "reason": "rtt.cancelled_or_deadline", "finishedAt": _stamp(time.time())}
        return {**base, "rows": [], "status": "missing", "reason": "rtt.probe_unavailable", "finishedAt": _stamp(time.time())}
