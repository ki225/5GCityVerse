"""Run the two fixed, read-only lifecycle evidence producers.

This adapter records raw command provenance.  It does not create ownership
registries, mutate live resources, or turn incomplete evidence into a bundle.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "cityverse.network-lifecycle-receipts/v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,180}\Z")
_COMMANDS = (
    ("pduSessionEvidence", "cityverse-native-session-readback/v1",
     "backend.onprem.operations.diagnostics.native_session_readback", "cityverse.native-session-readback/v1"),
    ("cleanupEvidence", "cityverse-owned-resource-cleanup/v1",
     "backend.onprem.operations.diagnostics.owned_resource_cleanup", "cityverse.owned-resource-cleanup/v1"),
)
_REQUIRED_ENV = (
    "CITYVERSE_NATIVE_SESSION_GENERATION", "CITYVERSE_ONPREM_AMF_OAM_URL",
    "CITYVERSE_ONPREM_SMF_OAM_URL", "CITYVERSE_ONPREM_UPF_EVIDENCE_URLS",
    "CITYVERSE_OWNED_RESOURCE_REGISTRY",
)
_OPTIONAL_ENV = frozenset({
    "CITYVERSE_ONPREM_UE_LEDGER", "CITYVERSE_ONPREM_UE_STATE_FILE",
    "CITYVERSE_ONPREM_RAN_BACKEND", "CITYVERSE_ONPREM_KUBE_CONTEXT",
    "CITYVERSE_ONPREM_NAMESPACE", "CITYVERSE_ONPREM_UE_BATCH_SIZE",
    "CITYVERSE_ONPREM_UE_BATCH_INTERVAL", "CITYVERSE_ONPREM_UE_TEMPO_MS",
    "CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE", "CITYVERSE_ONPREM_UE_INIT_IMAGE",
    "CITYVERSE_ONPREM_OAM_TOKEN", "CITYVERSE_ONPREM_SMF_INVENTORY_URL",
    "CITYVERSE_ONPREM_SMF_INVENTORY_COMMAND", "CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE",
})
_HOST_ENV = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR"})


class LifecycleReceiptError(ValueError):
    """Raised when no trustworthy two-command bundle can be produced."""

    def __init__(self, message: str, receipts: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.receipts = dict(receipts or {})


def _execute(argv: Sequence[str], timeout: float,
             environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, check=False,
                              env=dict(environment))
    except (FileNotFoundError, PermissionError) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(list(argv), 124, stdout, stderr or "command timed out")


def _receipt(executor: Callable[[Sequence[str], float, Mapping[str, str]], Any],
             argv: Sequence[str], *, environment: Mapping[str, str],
             source: str, epoch: str, timeout: float, clock: Callable[[], float]) -> dict[str, Any]:
    result = executor(tuple(argv), timeout, environment)
    return {"source": source, "command": list(argv), "exitCode": int(result.returncode),
            "stdout": result.stdout or "", "stderr": result.stderr or "",
            "timestamp": clock(), "epoch": epoch,
            "environmentKeys": sorted(environment)}


def _producer_environment(configured: Mapping[str, str], identity: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(configured, Mapping):
        raise LifecycleReceiptError("producer environment missing")
    missing = [key for key in _REQUIRED_ENV if not isinstance(configured.get(key), str)
               or not configured[key]]
    if not (configured.get("CITYVERSE_ONPREM_UE_LEDGER")
            or configured.get("CITYVERSE_ONPREM_UE_STATE_FILE")):
        missing.append("CITYVERSE_ONPREM_UE_LEDGER|CITYVERSE_ONPREM_UE_STATE_FILE")
    if missing:
        raise LifecycleReceiptError("producer environment incomplete: " + ",".join(missing))
    try:
        generation = int(configured["CITYVERSE_NATIVE_SESSION_GENERATION"])
        urls = json.loads(configured["CITYVERSE_ONPREM_UPF_EVIDENCE_URLS"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleReceiptError("producer generation or UPF URL inventory invalid") from exc
    if generation < 0 or not isinstance(urls, dict) or not urls:
        raise LifecycleReceiptError("producer generation or UPF URL inventory invalid")
    allowed = set(_REQUIRED_ENV) | _OPTIONAL_ENV
    environment = {key: value for key, value in configured.items()
                   if key in allowed and isinstance(value, str)}
    environment.update({key: os.environ[key] for key in _HOST_ENV if key in os.environ})
    environment.update({
        "CITYVERSE_ONPREM_EVIDENCE_MODE": "native",
        "CITYVERSE_NATIVE_SESSION_SUPI": str(identity["supi"]),
        "CITYVERSE_NATIVE_SESSION_PDU_ID": str(identity["pduSessionId"]),
        "CITYVERSE_NATIVE_SESSION_UE_IP": str(identity["ueIp"]),
        "CITYVERSE_NATIVE_SESSION_TUN": str(identity["tun"]),
        "PYTHONIOENCODING": "utf-8",
    })
    return environment


def _parse_success(receipt: Mapping[str, Any], *, schema: str, run_id: str,
                   epoch: str) -> Mapping[str, Any]:
    if receipt.get("exitCode") != 0:
        raise LifecycleReceiptError("lifecycle producer failed")
    try:
        value = json.loads(receipt.get("stdout", ""))
    except (json.JSONDecodeError, TypeError) as exc:
        raise LifecycleReceiptError("lifecycle producer stdout is invalid JSON") from exc
    if (not isinstance(value, Mapping) or value.get("schemaVersion") != schema
            or value.get("runId") != run_id or value.get("epoch") != epoch):
        raise LifecycleReceiptError("lifecycle producer schema/run/epoch mismatch")
    return value


def _native_success(value: Mapping[str, Any]) -> None:
    """Validate the native producer's own established-session success contract."""
    amf, smf, upf, ue, native = (value.get(key) for key in
                                  ("amf", "smf", "upf", "ue", "native"))
    provenance = value.get("provenance")
    if (type(value.get("generation")) is not int or value["generation"] < 0
            or not all(isinstance(row, Mapping) for row in (amf, smf, upf, ue, native))
            or amf.get("state") != "registered" or smf.get("state") != "active"
            or upf.get("state") != "active"
            or not isinstance(native.get("pfcpSeid"), str) or not native["pfcpSeid"]
            or not isinstance(native.get("sourceEpochs"), Mapping)
            or not isinstance(native.get("sourceTimestamps"), Mapping)
            or not isinstance(provenance, list) or len(provenance) != 2):
        raise LifecycleReceiptError("native lifecycle producer did not prove established status")
    for receipt in provenance:
        request = receipt.get("request") if isinstance(receipt, Mapping) else None
        if (not isinstance(request, Mapping)
                or request.get("transport") != "in-process"
                or request.get("method") != "NativeCollector.observe"
                or request.get("generation") != value["generation"]
                or not isinstance(receipt.get("responseSchemaVersion"), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("responseSha256"))) is None
                or type(receipt.get("receivedAt")) not in (int, float)
                or not isinstance(receipt.get("sourceTimestamps"), Mapping)):
            raise LifecycleReceiptError("native lifecycle verifier provenance incomplete")


def _cleanup_success(value: Mapping[str, Any]) -> None:
    if (value.get("status") != "complete" or value.get("complete") is not True
            or value.get("residuals") != [] or value.get("reasons") != []):
        raise LifecycleReceiptError("cleanup producer did not prove complete no-residual status")


def _native_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    amf, upf, ue = value.get("amf"), value.get("upf"), value.get("ue")
    if not all(isinstance(row, Mapping) for row in (amf, upf, ue)):
        raise LifecycleReceiptError("native lifecycle identity missing")
    identity = {"supi": amf.get("supi"), "pduSessionId": amf.get("pduSessionId"),
                "ueIp": upf.get("ueIp"), "tun": upf.get("tun")}
    if (upf.get("supi") != identity["supi"] or upf.get("pduSessionId") != identity["pduSessionId"]
            or ue.get("supi") != identity["supi"] or ue.get("ueIp") != identity["ueIp"]
            or ue.get("tun") != identity["tun"] or any(not item for item in identity.values())):
        raise LifecycleReceiptError("native lifecycle identity mismatch")
    return identity


def collect_network_lifecycle_receipts(*, run_id: str, epoch: str,
                                       identity: Mapping[str, Any],
                                       producer_environment: Mapping[str, str],
                                       timeout: float = 15.0,
                                       executor: Callable[[Sequence[str], float,
                                                           Mapping[str, str]], Any] = _execute,
                                       clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Return a bundle only after both fixed producers pass exact readback gates."""
    if (not isinstance(run_id, str) or _SAFE_ID.fullmatch(run_id) is None
            or not isinstance(epoch, str) or _SAFE_ID.fullmatch(epoch) is None):
        raise LifecycleReceiptError("run ID or epoch invalid")
    if (not isinstance(identity, Mapping) or any(not identity.get(key)
            for key in ("supi", "pduSessionId", "ueIp", "tun"))):
        raise LifecycleReceiptError("expected lifecycle identity incomplete")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise LifecycleReceiptError("lifecycle command timeout invalid")
    environment = _producer_environment(producer_environment, identity)

    receipts: dict[str, dict[str, Any]] = {}
    parsed: dict[str, Mapping[str, Any]] = {}
    for key, source, module, _ in _COMMANDS:
        argv = ["python", "-m", module, "--run-id", run_id, "--epoch", epoch, "--status"]
        receipts[key] = _receipt(executor, argv, environment=environment,
                                 source=source, epoch=epoch,
                                 timeout=float(timeout), clock=clock)
    try:
        for key, _, _, schema in _COMMANDS:
            parsed[key] = _parse_success(receipts[key], schema=schema, run_id=run_id, epoch=epoch)
        _native_success(parsed["pduSessionEvidence"])
        _cleanup_success(parsed["cleanupEvidence"])
        expected_native = {key: identity.get(key) for key in ("supi", "pduSessionId", "ueIp", "tun")}
        if _native_identity(parsed["pduSessionEvidence"]) != expected_native:
            raise LifecycleReceiptError("native lifecycle identity differs from expected identity")
        if parsed["cleanupEvidence"].get("identity") != dict(identity):
            raise LifecycleReceiptError("cleanup lifecycle identity differs from expected identity")
    except LifecycleReceiptError as exc:
        raise LifecycleReceiptError(str(exc), receipts) from exc
    return {"schemaVersion": SCHEMA_VERSION, "runId": run_id, "epoch": epoch,
            "identity": dict(identity), "status": "complete", "complete": True,
            "residuals": [], **receipts}

