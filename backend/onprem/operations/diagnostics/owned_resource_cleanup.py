"""Read-only, ownership-scoped cleanup status producer.

The command intentionally accepts only ``--run-id``, ``--epoch`` and
``--status``.  Its durable ownership registry is selected by the operator via
``CITYVERSE_OWNED_RESOURCE_REGISTRY``; callers cannot add arbitrary commands,
selectors, pods, containers or URLs to the command line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "cityverse.owned-resource-cleanup/v1"
SOURCE = "cityverse-owned-resource-cleanup/v1"
_SAFE = re.compile(r"[A-Za-z0-9_.:@/-]{1,180}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,63}\Z")
_SELECTOR = re.compile(r"(?:[A-Za-z0-9_.-]{1,63}/)?[A-Za-z0-9_.-]{1,63}=[A-Za-z0-9_.-]{1,128}\Z")


class CleanupEvidenceError(ValueError):
    pass


def _safe(name: str, value: Any, pattern: re.Pattern[str] = _SAFE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or ".." in value:
        raise CleanupEvidenceError(f"invalid {name}")
    return value


def _execute(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, PermissionError) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(list(argv), 124, stdout, stderr or "command timed out")


def _command(executor: Callable[[Sequence[str], float], Any], argv: Sequence[str],
             *, epoch: str, clock: Callable[[], float], timeout: float) -> dict[str, Any]:
    result = executor(tuple(argv), timeout)
    return {"command": list(argv), "exitCode": int(result.returncode),
            "stdout": result.stdout or "", "stderr": result.stderr or "",
            "timestamp": clock(), "epoch": epoch}


def _json(row: Mapping[str, Any], name: str) -> Any:
    if row.get("exitCode") != 0:
        raise CleanupEvidenceError(f"{name} query failed")
    try:
        return json.loads(row.get("stdout", ""))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CleanupEvidenceError(f"{name} query returned invalid JSON") from exc


def _matches(row: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    return all(row.get(key) == value for key, value in selector.items())


def _observed_seconds(value: Any) -> float:
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.astimezone(timezone.utc).timestamp()
        except ValueError as exc:
            raise CleanupEvidenceError("UPF observedAt invalid") from exc
    raise CleanupEvidenceError("UPF observedAt invalid")


def produce_status(registry: Mapping[str, Any], *, run_id: str, epoch: str,
                   executor: Callable[[Sequence[str], float], Any] = _execute,
                   clock: Callable[[], float] = time.time, timeout: float = 3.0) -> dict[str, Any]:
    """Query only registered resources and fail closed unless every scope is clean."""
    _safe("run ID", run_id); _safe("epoch", epoch)
    entry = registry.get("runs", {}).get(run_id) if isinstance(registry, Mapping) else None
    reasons: list[str] = []
    residuals: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    identity = entry.get("identity") if isinstance(entry, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("epoch") != epoch or not isinstance(identity, Mapping):
        reasons.append("matching durable ownership record unavailable")
        entry = {}
        identity = None
    try:
        namespace = _safe("namespace", entry["namespace"])
        selector = _safe("owned Pod selector", entry["ownedPodSelector"], _SELECTOR)
        if selector != f"cityverse.io/benchmark-run-id={run_id}":
            raise CleanupEvidenceError("owned Pod selector is not bound to run ID")
        pod_argv = ("kubectl", "--namespace", namespace, "get", "pods", "--selector", selector, "-o", "json")
        before = _command(executor, pod_argv, epoch=epoch, clock=clock, timeout=timeout)
        observations.append({"kind": "ownedPodsBefore", **before})
        pods = _json(before, "owned Pod")
        items = pods.get("items") if isinstance(pods, Mapping) else None
        if not isinstance(items, list):
            raise CleanupEvidenceError("owned Pod query shape unknown")
        for pod in items:
            meta = pod.get("metadata", {}) if isinstance(pod, Mapping) else {}
            residuals.append({"kind": "ownedPod", "namespace": namespace,
                              "name": meta.get("name"), "podUid": meta.get("uid")})

        checks = entry.get("processChecks")
        if not isinstance(checks, list):
            raise CleanupEvidenceError("process ownership scope unavailable")
        for check in checks:
            if not isinstance(check, Mapping):
                raise CleanupEvidenceError("process check malformed")
            pod = _safe("process Pod", check.get("pod")); uid = _safe("process Pod UID", check.get("podUid"))
            container = _safe("process container", check.get("container"))
            forbidden = check.get("forbiddenExecutables")
            if not isinstance(forbidden, list) or not forbidden or any(not _LABEL.fullmatch(str(x)) for x in forbidden):
                raise CleanupEvidenceError("forbidden process ownership scope unavailable")
            get_argv = ("kubectl", "--namespace", namespace, "get", "pod", pod, "-o", "json")
            pin = _command(executor, get_argv, epoch=epoch, clock=clock, timeout=timeout)
            observations.append({"kind": "processPodIdentity", "podUid": uid, **pin})
            pin_value = _json(pin, "process Pod identity")
            if pin_value.get("metadata", {}).get("uid") != uid:
                raise CleanupEvidenceError("process Pod UID changed")
            ps_argv = ("kubectl", "--namespace", namespace, "exec", pod, "--container", container,
                       "--", "ps", "-eo", "pid=,comm=,args=")
            ps = _command(executor, ps_argv, epoch=epoch, clock=clock, timeout=timeout)
            observations.append({"kind": "ownedProcesses", "podUid": uid, **ps})
            if ps["exitCode"] != 0:
                raise CleanupEvidenceError("owned process query failed")
            for line in ps["stdout"].splitlines():
                columns = line.strip().split(None, 2)
                if len(columns) >= 2 and columns[1] in forbidden:
                    residuals.append({"kind": "ownedProcess", "pod": pod, "podUid": uid,
                                      "executable": columns[1], "pid": columns[0]})
            repin = _command(executor, get_argv, epoch=epoch, clock=clock, timeout=timeout)
            observations.append({"kind": "processPodIdentityAfter", "podUid": uid, **repin})
            if _json(repin, "process Pod identity recheck").get("metadata", {}).get("uid") != uid:
                raise CleanupEvidenceError("process Pod UID changed during query")

        upfs = entry.get("upfChecks")
        if not isinstance(upfs, list) or not upfs:
            raise CleanupEvidenceError("UPF residual scope unavailable")
        native_pfcp_seid = entry.get("nativePfcpSeid")
        if not isinstance(native_pfcp_seid, str) or not native_pfcp_seid:
            raise CleanupEvidenceError("native PFCP identity unavailable")
        for check in upfs:
            url = _safe("UPF status URL", check.get("url"))
            if not (url.startswith("http://") or url.startswith("https://")):
                raise CleanupEvidenceError("invalid UPF status URL")
            selectors = check.get("selectors")
            expected_upf = _safe("UPF identity", check.get("upf"))
            expected_epoch = _safe("UPF epoch", check.get("epoch"))
            if (not isinstance(selectors, list) or not selectors
                    or any(not isinstance(x, Mapping) or set(x) != {"ip", "pfcpSeid"}
                           or not all(isinstance(x.get(k), str) and x[k] for k in ("ip", "pfcpSeid"))
                           for x in selectors)):
                raise CleanupEvidenceError("UPF ownership selectors unavailable")
            if any(x["ip"] != identity.get("ueIp") or x["pfcpSeid"] != native_pfcp_seid
                   for x in selectors):
                raise CleanupEvidenceError("UPF ownership selector does not match run identity")
            argv = ("curl", "--fail", "--silent", "--show-error", "--max-time", "2", url)
            raw = _command(executor, argv, epoch=epoch, clock=clock, timeout=timeout)
            observations.append({"kind": "upfSessions", **raw})
            dump = _json(raw, "UPF status")
            observed = _observed_seconds(dump.get("observedAt"))
            age = raw["timestamp"] - observed
            if (dump.get("schemaVersion") != "cityverse.upf/v1" or dump.get("available") is not True
                    or dump.get("source") != "gtp5g-netlink" or dump.get("upf") != expected_upf
                    or dump.get("epoch") != expected_epoch or dump.get("errors") != []
                    or not -2 <= age <= 15
                    or any(not isinstance(dump.get(field), list)
                           for field in ("sessions", "residuals", "pdrs", "qers"))):
                raise CleanupEvidenceError("UPF status incomplete")
            if any(not isinstance(value, Mapping) for field in ("sessions", "residuals", "pdrs", "qers")
                   for value in dump[field]):
                raise CleanupEvidenceError("UPF status row malformed")
            for owned in selectors:
                active = [value for value in dump["sessions"] if _matches(value, owned)]
                if len(active) != 1 or active[0].get("upf") != expected_upf:
                    raise CleanupEvidenceError("exact active UPF session unavailable")
                owned_pdrs = [value for value in dump["pdrs"]
                              if str(value.get("seid")) == owned["pfcpSeid"]
                              and value.get("ip") == owned["ip"]]
                if not owned_pdrs:
                    raise CleanupEvidenceError("owned UPF PDR scope unavailable")
                for value in dump["residuals"]:
                    if (value.get("pfcpSeid") == owned["pfcpSeid"]
                            or value.get("ip") == owned["ip"]):
                        residuals.append({"kind": "upf-residual", "selector": dict(owned),
                                          "value": dict(value)})

        after = _command(executor, pod_argv, epoch=epoch, clock=clock, timeout=timeout)
        observations.append({"kind": "ownedPodsAfter", **after})
        if _json(after, "owned Pod recheck") != pods:
            raise CleanupEvidenceError("owned Pod scope changed during query")
    except (KeyError, CleanupEvidenceError, TypeError, ValueError) as exc:
        reasons.append(str(exc) or type(exc).__name__)
    if residuals:
        reasons.append("owned resources remain")
    complete = not reasons and not residuals
    return {"schemaVersion": SCHEMA_VERSION, "source": SOURCE, "runId": run_id,
            "epoch": epoch, "identity": identity, "complete": complete,
            "status": "complete" if complete else "incomplete", "residuals": residuals,
            "reasons": reasons, "observedAt": clock(), "observations": observations,
            "scope": "registered-owned-resources", "globalZeroClaimed": False,
            "readOnly": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--status", action="store_true", required=True)
    args = parser.parse_args(argv)
    path = os.environ.get("CITYVERSE_OWNED_RESOURCE_REGISTRY")
    if not path:
        result = produce_status({}, run_id=args.run_id, epoch=args.epoch)
    else:
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                registry = json.load(handle)
        except (OSError, json.JSONDecodeError):
            registry = {}
        result = produce_status(registry, run_id=args.run_id, epoch=args.epoch)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
