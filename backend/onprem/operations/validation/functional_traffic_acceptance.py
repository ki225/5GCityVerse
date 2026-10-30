"""Assemble functional acceptance only from raw pinned iperf and lifecycle evidence."""
from __future__ import annotations

import json
import math
import argparse
import os
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from backend.onprem.capacity.performance import _flow


SCHEMA_VERSION = "cityverse.functional-traffic-acceptance/v1"
_PRODUCER_MODULES = {
    "cityverse-native-session-readback": "backend.onprem.operations.diagnostics.native_session_readback",
    "cityverse-owned-resource-cleanup": "backend.onprem.operations.diagnostics.owned_resource_cleanup",
}


def _provenance(value: Any, name: str, *, epoch: str, run_id: str, source: str,
                now: float, maximum_age: float) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} evidence missing")
    module = _PRODUCER_MODULES.get(source)
    if module is None:
        raise ValueError(f"{name} producer is unknown")
    expected_command = ["python", "-m", module, "--run-id", run_id,
                        "--epoch", epoch, "--status"]
    if (value.get("source") != f"{source}/v1"
            or type(value.get("exitCode")) is not int or value["exitCode"] != 0
            or value.get("command") != expected_command
            or not isinstance(value.get("stdout"), str) or not value["stdout"].strip()
            or not isinstance(value.get("stderr"), str) or value.get("epoch") != epoch):
        raise ValueError(f"{name} raw provenance incomplete")
    timestamp = value.get("timestamp")
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not 0 <= now - timestamp <= maximum_age:
        raise ValueError(f"{name} evidence stale")
    try:
        parsed = json.loads(value["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} raw stdout invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} raw stdout invalid")
    return parsed


def _native_source_times(value: Any, *, now: float, maximum_age: float) -> dict:
    if not isinstance(value, Mapping) or set(value) != {"amf", "smf", "upf", "ue", "traffic"}:
        raise ValueError("PDU/session source timestamps incomplete")
    for observed in value.values():
        if not isinstance(observed, str):
            raise ValueError("PDU/session source timestamp invalid")
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            sampled = parsed.timestamp()
        except (ValueError, OSError, OverflowError) as exc:
            raise ValueError("PDU/session source timestamp invalid") from exc
        if not math.isfinite(sampled) or not 0 <= now - sampled <= maximum_age:
            raise ValueError("PDU/session source timestamp stale")
    return dict(value)


def _native_readback(pdu: Mapping[str, Any], identity: Mapping[str, Any],
                     binding: Mapping[str, Any], *, now: float, maximum_age: float) -> None:
    generation = binding.get("generation")
    pod_uid = binding.get("podUid")
    if (type(generation) is not int or generation < 0
            or not isinstance(pod_uid, str) or not pod_uid
            or binding.get("slice") not in {"embb", "urllc", "mmtc", "v2x"}
            or not isinstance(binding.get("upf"), str) or not binding["upf"]
            or not isinstance(binding.get("pfcpSeid"), str) or not binding["pfcpSeid"].isdigit()
            or not isinstance(binding.get("upfEpoch"), str) or not binding["upfEpoch"]):
        raise ValueError("Functional binding lacks native generation/Pod identity")
    if pdu.get("generation") != generation:
        raise ValueError("PDU/session generation mismatch")
    if pdu.get("ue") != {"supi": identity.get("supi"), "podUid": pod_uid,
                          "ueIp": identity.get("ueIp"), "tun": identity.get("tun")}:
        raise ValueError("PDU/session UE identity mismatch")
    native = pdu.get("native")
    if not isinstance(native, Mapping):
        raise ValueError("PDU/session native identity missing")
    epochs = native.get("sourceEpochs")
    if (native.get("upf") != binding.get("upf")
            or native.get("pfcpSeid") != binding.get("pfcpSeid")
            or not isinstance(epochs, Mapping) or set(epochs) != {"embb", "urllc", "mmtc", "v2x"}
            or any(not isinstance(value, str) or not value for value in epochs.values())
            or epochs.get(binding.get("slice")) != binding.get("upfEpoch")):
        raise ValueError("PDU/session native UPF identity mismatch")
    native_times = _native_source_times(native.get("sourceTimestamps"), now=now,
                                        maximum_age=maximum_age)
    receipts = pdu.get("provenance")
    if not isinstance(receipts, list) or len(receipts) != 2:
        raise ValueError("PDU/session dual-snapshot provenance missing")
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ValueError("PDU/session snapshot provenance invalid")
        request = receipt.get("request")
        received = receipt.get("receivedAt")
        if (request != {"transport": "in-process", "method": "NativeCollector.observe",
                        "generation": generation}
                or receipt.get("responseSchemaVersion") != "cityverse.evidence/v1"
                or not isinstance(receipt.get("responseSha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["responseSha256"]) is None
                or type(received) not in (int, float) or not math.isfinite(received)
                or not 0 <= now - received <= maximum_age):
            raise ValueError("PDU/session snapshot provenance invalid")
        _native_source_times(receipt.get("sourceTimestamps"), now=now,
                             maximum_age=maximum_age)
    if receipts[-1].get("sourceTimestamps") != native_times:
        raise ValueError("PDU/session source snapshot mismatch")


def assemble_functional_acceptance(raw: Mapping[str, Any], *, now: float,
                                   maximum_age: float = 300) -> dict[str, Any]:
    """Return complete/incomplete without trusting caller acceptance booleans."""
    reasons = []
    flows = []
    run_id = raw.get("runId") if isinstance(raw, Mapping) else None
    identity = raw.get("identity") if isinstance(raw, Mapping) else None
    epoch = raw.get("epoch") if isinstance(raw, Mapping) else None
    try:
        if not isinstance(run_id, str) or not run_id or not isinstance(epoch, str) or not epoch:
            raise ValueError("benchmark run/epoch identity missing")
        if not isinstance(identity, Mapping) or not identity:
            raise ValueError("benchmark identity missing")
        reports = raw.get("reports")
        if not isinstance(reports, list) or len(reports) != 2 or {row.get("direction") for row in reports} != {"ul", "dl"}:
            raise ValueError("complete bidirectional raw reports required")
        if reports[0].get("binding") != reports[1].get("binding"):
            raise ValueError("functional report identity mismatch")
        for report in reports:
            binding = report.get("binding")
            if (not isinstance(binding, Mapping)
                    or binding.get("supi") != identity.get("supi")
                    or binding.get("sessionId") != identity.get("pduSessionId")
                    or binding.get("ueIp", binding.get("ip")) != identity.get("ueIp")
                    or binding.get("tun") != identity.get("tun")):
                raise ValueError("functional report identity mismatch")
            start, finish = report.get("startedAt"), report.get("finishedAt")
            if (type(start) not in (int, float) or type(finish) not in (int, float)
                    or not all(math.isfinite(value) for value in (start, finish))
                    or not 4 <= finish - start <= 8 or not 0 <= now - finish <= maximum_age):
                raise ValueError("functional report stale or incomplete")
            server_peer = report.get("server", {}).get("start", {}).get("connected", [])
            if len(server_peer) != 1:
                raise ValueError("receiver peer evidence missing")
            job = SimpleNamespace(direction=report["direction"], offered_kbps=10_000,
                                  ue_ip=identity.get("ueIp"), endpoint=report.get("endpoint"),
                                  port=report.get("port"), receiver_peer=server_peer[0].get("remote_host"),
                                  supi=identity.get("supi"))
            result = _flow(job, report, "functional_acceptance")
            if not result.passed or report.get("offeredTargetMbps") != 10:
                raise ValueError("10 Mbps functional threshold failed")
            flows.append({"direction": result.direction,
                          "offeredAppliedRatio": result.offered_applied_ratio,
                          "receivedAppliedRatio": result.received_applied_ratio,
                          "lossPercent": result.computed_loss_percent})
        pdu = _provenance(raw.get("pduSessionEvidence"), "PDU/session", epoch=epoch,
                          run_id=run_id, source="cityverse-native-session-readback",
                          now=now, maximum_age=maximum_age)
        expected_session = {"supi": identity.get("supi"),
                            "pduSessionId": identity.get("pduSessionId")}
        if (pdu.get("schemaVersion") != "cityverse.native-session-readback/v1"
                or pdu.get("runId") != run_id or pdu.get("epoch") != epoch
                or pdu.get("amf") != {**expected_session, "state": "registered"}
                or pdu.get("smf") != {**expected_session, "state": "active"}
                or pdu.get("upf") != {**expected_session, "state": "active",
                                      "ueIp": identity.get("ueIp"), "tun": identity.get("tun")}):
            raise ValueError("PDU/session raw evidence mismatch")
        _native_readback(pdu, identity, reports[0]["binding"], now=now,
                         maximum_age=maximum_age)
        cleanup = _provenance(raw.get("cleanupEvidence"), "cleanup", epoch=epoch,
                              run_id=run_id, source="cityverse-owned-resource-cleanup",
                              now=now, maximum_age=maximum_age)
        if (cleanup.get("schemaVersion") != "cityverse.owned-resource-cleanup/v1"
                or cleanup.get("runId") != run_id or cleanup.get("epoch") != epoch
                or cleanup.get("identity") != identity or cleanup.get("residuals") != []
                or cleanup.get("complete") is not True):
            raise ValueError("cleanup raw evidence mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        reasons.append(str(exc) or type(exc).__name__)
    complete = not reasons
    finished = max((row.get("finishedAt", 0) for row in raw.get("reports", [])), default=0) \
        if isinstance(raw, Mapping) else 0
    return {"schemaVersion": SCHEMA_VERSION, "benchmarkRunId": run_id, "identity": identity,
            "epoch": epoch, "status": "complete" if complete else "incomplete",
            "accepted": complete, "profile": "functional-traffic",
            "settings": {"rateMbps": 10, "packetBytes": 1200, "flowCount": 1, "gso": False},
            "finishedAt": finished, "pduHealthy": complete, "sessionHealthy": complete,
            "cleanupVerified": complete, "residualResources": [] if complete else None,
            "flows": flows if complete else [], "reasons": reasons,
            "rawEvidence": {"reports": raw.get("reports") if isinstance(raw, Mapping) else None,
                            "pduSessionEvidence": raw.get("pduSessionEvidence") if isinstance(raw, Mapping) else None,
                            "cleanupEvidence": raw.get("cleanupEvidence") if isinstance(raw, Mapping) else None}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    with args.input.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    result = assemble_functional_acceptance(raw, now=time.time())
    payload = json.dumps(result, sort_keys=True, indent=2) + "\n"
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
