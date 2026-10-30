#!/usr/bin/env python3
"""Fail-closed coordinator for separate throughput and functional traffic lanes.

The CLI defaults to an observe-only plan and executes no subprocesses.  The
Throughput telemetry consumes only the isolated benchmark receiver result.  A
separate pinned 10 Mbps bidirectional run produces functional traffic reports.
Both lanes must prove the same preallocated run and native session identity
before post-run session readback and owned cleanup.  This module never creates
an ownership registry or starts a lab.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.operations.diagnostics.network_telemetry_bundle import capture_bundle, observe_bundle
from backend.onprem.operations.validation.network_lifecycle_receipts import LifecycleReceiptError, collect_network_lifecycle_receipts
from backend.onprem.operations.validation.functional_traffic_acceptance import assemble_functional_acceptance
from backend.onprem.operations.validation.functional_traffic_adapter import FunctionalTrafficBlocked, run_functional_traffic, validate_functional_traffic_config
from scripts.onprem import run_network_throughput_benchmark as benchmark_runner


SCHEMA = "cityverse.network-throughput-operator/v1"
RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
OWNERSHIP_IDENTITY_KEYS = frozenset({
    "workloadId", "supi", "pduSessionId", "ueIp", "tun", "receiverId",
    "vmEpoch", "interfaceEpoch",
})


class OperatorBlocked(RuntimeError):
    pass


_RUNNER_TEXT_FIELDS = ("context", "kubeconfig", "namespace", "owner", "uePod", "ueUid",
                       "receiverPod", "receiverUid", "supi", "pduSessionId", "ueIp", "tun",
                       "endpoint", "manifest", "identityEvidence", "artifactRoot")
_RUNNER_INT_FIELDS = ("port", "rateMbps", "durationSeconds", "flowCount", "packetBytes")


def _write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def build_plan(config: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(config, Mapping):
        raise OperatorBlocked("operator config missing")
    telemetry = config.get("telemetry")
    runner = config.get("runner")
    if not isinstance(telemetry, Mapping) or not isinstance(runner, Mapping):
        raise OperatorBlocked("telemetry and runner config required")
    observation = observe_bundle(telemetry)
    required_runner = {"manifest", "identityEvidence", "artifactRoot"}
    if not required_runner <= set(runner) or not all(
            isinstance(runner[key], str) and runner[key] for key in required_runner):
        raise OperatorBlocked("runner immutable input paths missing")
    return {"schemaVersion": SCHEMA, "mode": "observe", "readOnly": True,
            "commandsExecuted": 0,
            "sequence": ["readiness", "telemetry-before", "throughput-runner",
                         "telemetry-after", "functional-traffic-runner",
                         "post-run-lifecycle", "functional-acceptance",
                         "ready-for-assembly"],
            "telemetryPlan": observation,
            "runnerInputs": {key: runner[key] for key in sorted(required_runner)},
            "liveCapability": {
                "status": "available-with-live-prerequisites",
                "throughputRunner": {
                    "status": "available",
                    "producer": "scripts.onprem.run_network_throughput_benchmark.execute",
                    "profile": "network-throughput",
                    "telemetryReceiverSchema": "completed benchmark result"},
                "functionalTrafficReports": {
                    "status": "available",
                    "producer": "backend.onprem.operations.validation.functional_traffic_adapter.run_functional_traffic",
                    "profile": {"directions": ["ul", "dl"], "rateMbps": 10,
                                "packetBytes": 1200, "flowCount": 1, "gso": False},
                    "requirements": ["fresh-native-pdu-tun-guard", "exact-ue-pod-uid",
                                     "exact-n6-pod-uid", "pinned-traffic-sidecars"]},
                "lifecycleReceipts": {
                    "status": "available",
                    "producer": "backend.onprem.operations.validation.network_lifecycle_receipts.collect_network_lifecycle_receipts",
                    "commands": ["backend.onprem.operations.diagnostics.native_session_readback",
                                 "backend.onprem.operations.diagnostics.owned_resource_cleanup"]},
                "functionalAcceptance": {
                    "status": "available",
                    "producer": "backend.onprem.operations.validation.functional_traffic_acceptance.assemble_functional_acceptance"},
                "missingRequiredAdapters": [],
                "livePrerequisites": ["existing-owned-resource-registry",
                                      "native-oam-and-upf-evidence-endpoints",
                                      "existing-ue-ledger-or-state-file"],
                "reason": "live prerequisites are supplied by the operator environment"}}


def _collect_lifecycle(config: Mapping[str, object], run_id: str,
                       traffic_result: Mapping[str, object]) -> dict[str, object]:
    environment = config.get("producerEnvironment")
    epoch = traffic_result.get("epoch")
    identity = traffic_result.get("identity")
    if not isinstance(environment, Mapping):
        raise OperatorBlocked("producerEnvironment required; ownership registry is not synthesized")
    if not isinstance(epoch, str) or not epoch:
        raise OperatorBlocked("traffic epoch missing")
    if not isinstance(identity, Mapping):
        raise OperatorBlocked("traffic identity missing")
    try:
        return collect_network_lifecycle_receipts(
            run_id=run_id, epoch=epoch, identity=identity,
            producer_environment=environment)
    except LifecycleReceiptError as exc:
        raise OperatorBlocked(str(exc)) from exc


def _ownership_preflight(config: Mapping[str, object], run_id: str) -> tuple[str, dict[str, object]]:
    """Read and pin an existing ownership record before any live adapter runs."""
    environment = config.get("producerEnvironment")
    path_value = (environment.get("CITYVERSE_OWNED_RESOURCE_REGISTRY")
                  if isinstance(environment, Mapping) else None)
    if not isinstance(path_value, str) or not path_value:
        raise OperatorBlocked("existing ownership registry path required")
    try:
        registry = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorBlocked("existing ownership registry unavailable") from exc
    runs = registry.get("runs") if isinstance(registry, Mapping) else None
    entry = runs.get(run_id) if isinstance(runs, Mapping) else None
    identity = entry.get("identity") if isinstance(entry, Mapping) else None
    epoch = entry.get("epoch") if isinstance(entry, Mapping) else None
    if (not isinstance(entry, Mapping) or not isinstance(epoch, str) or not epoch
            or not isinstance(identity, Mapping)
            or set(identity) != OWNERSHIP_IDENTITY_KEYS
            or any(value is None or value == "" for value in identity.values())
            or not isinstance(entry.get("nativePfcpSeid"), str)
            or not entry["nativePfcpSeid"]):
        raise OperatorBlocked("exact runId/epoch/identity ownership record unavailable")
    return epoch, dict(identity)


def _throughput_request(config: Mapping[str, object]) -> benchmark_runner.Request:
    runner = config.get("runner")
    if not isinstance(runner, Mapping):
        raise OperatorBlocked("runner config required")
    missing = [key for key in _RUNNER_TEXT_FIELDS
               if not isinstance(runner.get(key), str) or not runner[key]]
    missing += [key for key in _RUNNER_INT_FIELDS if type(runner.get(key)) is not int]
    if missing:
        raise OperatorBlocked("explicit throughput runner config missing or invalid: "
                              + ",".join(sorted(missing)))
    return benchmark_runner.Request(
        context=runner["context"], kubeconfig=runner["kubeconfig"],
        namespace=runner["namespace"], owner=runner["owner"],
        ue_pod=runner["uePod"], ue_uid=runner["ueUid"],
        receiver_pod=runner["receiverPod"], receiver_uid=runner["receiverUid"],
        supi=runner["supi"], pdu_session_id=runner["pduSessionId"],
        ue_ip=runner["ueIp"], tun=runner["tun"], endpoint=runner["endpoint"],
        port=runner["port"], rate_mbps=runner["rateMbps"],
        duration_seconds=runner["durationSeconds"], flow_count=runner["flowCount"],
        packet_bytes=runner["packetBytes"], manifest_file=Path(runner["manifest"]),
        evidence_file=Path(runner["identityEvidence"]),
        artifact_root=Path(runner["artifactRoot"]))


def _capture_telemetry(config: Mapping[str, object], phase: str, run_id: str,
                       before: Mapping[str, object] | None,
                       receiver: Mapping[str, object] | None) -> dict[str, object]:
    telemetry = config.get("telemetry")
    if not isinstance(telemetry, Mapping):
        raise OperatorBlocked("telemetry config required")
    if phase == "before" and (before is not None or receiver is not None):
        raise OperatorBlocked("before telemetry received foreign evidence")
    if phase == "after" and (not isinstance(before, Mapping)
                              or not isinstance(receiver, Mapping)):
        raise OperatorBlocked("after telemetry requires before bundle and throughput receiver")
    return capture_bundle(phase=phase, config=telemetry, before=before,
                          receiver_result=receiver, run_id=run_id)


def _run_throughput(request: benchmark_runner.Request, run_id: str) -> dict[str, object]:
    result, _ = benchmark_runner.execute(request, run_id=run_id)
    return dict(result)


def _functional_epoch(before: Mapping[str, object]) -> str:
    sides = before.get("sides")
    if not isinstance(sides, Mapping):
        raise OperatorBlocked("before telemetry session epoch missing")
    values = set()
    for role in ("ran", "upf"):
        side = sides.get(role)
        snapshot = side.get("snapshot") if isinstance(side, Mapping) else None
        identity = snapshot.get("identity") if isinstance(snapshot, Mapping) else None
        if not isinstance(identity, Mapping):
            raise OperatorBlocked("before telemetry session epoch missing")
        values.add(identity.get("sessionEpoch"))
    if len(values) != 1 or None in values or not isinstance(next(iter(values)), str):
        raise OperatorBlocked("before telemetry session epoch is inconsistent")
    return next(iter(values))


def _accept_functional(run_id: str, functional_result: Mapping[str, object],
                       lifecycle_result: Mapping[str, object]) -> dict[str, object]:
    raw = {"runId": run_id, "epoch": functional_result.get("epoch"),
           "identity": functional_result.get("identity"),
           "reports": functional_result.get("reports"),
           "pduSessionEvidence": lifecycle_result.get("pduSessionEvidence"),
           "cleanupEvidence": lifecycle_result.get("cleanupEvidence")}
    return assemble_functional_acceptance(raw, now=time.time())


def _common_session(before: Mapping[str, object], throughput: Mapping[str, object],
                    functional: Mapping[str, object], run_id: str) -> None:
    """Reject foreign or cross-session lane results before post-run cleanup."""
    sides = before.get("sides")
    if not isinstance(sides, Mapping) or set(sides) != {"ran", "upf"}:
        raise OperatorBlocked("before telemetry lacks both session identities")
    identities = []
    for role in ("ran", "upf"):
        side = sides.get(role)
        snapshot = side.get("snapshot") if isinstance(side, Mapping) else None
        identity = snapshot.get("identity") if isinstance(snapshot, Mapping) else None
        if not isinstance(identity, Mapping):
            raise OperatorBlocked("before telemetry session identity missing")
        identities.append(identity)
    epochs = {row.get("sessionEpoch") for row in identities}
    pdus = {row.get("pduSessionId") for row in identities}
    if len(epochs) != 1 or None in epochs or len(pdus) != 1 or None in pdus:
        raise OperatorBlocked("before telemetry session identity is inconsistent")
    throughput_identity = throughput.get("identity")
    functional_identity = functional.get("identity")
    if not isinstance(throughput_identity, Mapping) or not isinstance(functional_identity, Mapping):
        raise OperatorBlocked("traffic lane identity missing")
    keys = ("supi", "pduSessionId", "ueIp", "tun")
    if any(throughput_identity.get(key) != functional_identity.get(key) for key in keys):
        raise OperatorBlocked("throughput and functional traffic identities differ")
    if (throughput_identity.get("pduSessionId") not in pdus
            or functional.get("epoch") not in epochs
            or functional.get("runId") != run_id):
        raise OperatorBlocked("traffic lane session/run identity mismatch")


def _run_id(factory: Callable[[], str] | None = None) -> str:
    if factory is None:
        raise OperatorBlocked("explicit preallocated runId required")
    value = factory()
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise OperatorBlocked("invalid preallocated runId")
    return value


def orchestrate(config: Mapping[str, object], *, execute: bool = False,
                output_root: Path | None = None,
                planner: Callable[[Mapping[str, object]], Mapping[str, object]] = build_plan,
                capture: Callable[[str, str, Mapping[str, object] | None,
                                   Mapping[str, object] | None], Mapping[str, object]] | None = None,
                traffic: Callable[[str], Mapping[str, object]] | None = None,
                lifecycle: Callable[[str, Mapping[str, object]], Mapping[str, object]] | None = None,
                functional_traffic: Callable[[str], Mapping[str, object]] | None = None,
                acceptance: Callable[[str, Mapping[str, object], Mapping[str, object]], Mapping[str, object]] | None = None,
                run_id_factory: Callable[[], str] | None = None,
                ownership_preflight: Callable[[Mapping[str, object], str],
                                              tuple[str, dict[str, object]]] = _ownership_preflight,
                ) -> dict[str, object]:
    plan = dict(planner(config))
    if not execute:
        return plan
    run_id = _run_id(run_id_factory)
    if output_root is None:
        raise OperatorBlocked("execution output root required")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_new(run_dir / "00-readiness.json", plan)
    try:
        ownership_epoch, ownership_identity = ownership_preflight(config, run_id)
    except OperatorBlocked as exc:
        failure = {"schemaVersion": SCHEMA, "runId": run_id, "status": "incomplete",
                   "stage": "readiness", "commandsExecuted": 0, "reason": str(exc)}
        _write_new(run_dir / "failure.json", failure)
        return failure
    throughput_request = None
    if traffic is None:
        try:
            throughput_request = _throughput_request(config)
        except OperatorBlocked as exc:
            failure = {"schemaVersion": SCHEMA, "runId": run_id, "status": "incomplete",
                       "stage": "readiness", "commandsExecuted": 0,
                       "reason": str(exc), "missingAdapters": ["traffic"]}
            _write_new(run_dir / "failure.json", failure)
            return failure
        traffic = lambda current_run_id: _run_throughput(throughput_request, current_run_id)
    if capture is None and isinstance(config.get("telemetry"), Mapping):
        capture = lambda phase, current_run_id, before, receiver: _capture_telemetry(
            config, phase, current_run_id, before, receiver)
    if lifecycle is None and isinstance(config.get("producerEnvironment"), Mapping):
        lifecycle = lambda current_run_id, traffic_result: _collect_lifecycle(
            config, current_run_id, traffic_result)
    if acceptance is None:
        acceptance = _accept_functional
    default_functional = functional_traffic is None
    if default_functional:
        try:
            validate_functional_traffic_config(config)
        except FunctionalTrafficBlocked as exc:
            failure = {"schemaVersion": SCHEMA, "runId": run_id, "status": "incomplete",
                       "stage": "readiness", "commandsExecuted": 0,
                       "reason": str(exc), "missingAdapters": ["functional_traffic"]}
            _write_new(run_dir / "failure.json", failure)
            return failure
    missing = [name for name, adapter in (("capture", capture), ("traffic", traffic),
                                           ("lifecycle", lifecycle),
                                           ("functional_traffic", functional_traffic if not default_functional else True),
                                           ("acceptance", acceptance)) if adapter is None]
    if missing:
        failure = {"schemaVersion": SCHEMA, "runId": run_id, "status": "incomplete",
                   "stage": "readiness", "commandsExecuted": 0,
                   "reason": "safe producer adapters unavailable", "missingAdapters": missing}
        _write_new(run_dir / "failure.json", failure)
        return failure
    steps = []
    try:
        before = dict(capture("before", run_id, None, None))
        _write_new(run_dir / "10-telemetry-before.json", before)
        steps.append("telemetry-before")
        if before.get("status") != "complete":
            raise OperatorBlocked("before telemetry incomplete; traffic not started")
        if _functional_epoch(before) != ownership_epoch:
            raise OperatorBlocked("before telemetry epoch differs from ownership record")
        traffic_result = dict(traffic(run_id))
        _write_new(run_dir / "20-throughput.json", traffic_result)
        steps.append("throughput-runner")
        if traffic_result.get("runId") != run_id:
            raise OperatorBlocked("runner runId mismatch")
        if (traffic_result.get("profile") != "network-throughput"
                or traffic_result.get("status") != "complete"
                or traffic_result.get("benchmarkComplete") is not True
                or traffic_result.get("identity") != ownership_identity):
            raise OperatorBlocked("throughput runner result incomplete")
        after = dict(capture("after", run_id, before, traffic_result))
        _write_new(run_dir / "30-telemetry-after.json", after)
        steps.append("telemetry-after")
        if after.get("status") != "complete":
            raise OperatorBlocked("after telemetry incomplete")
        functional_result = dict(
            run_functional_traffic(config, run_id=run_id, epoch=_functional_epoch(before),
                                   identity=traffic_result["identity"])
            if default_functional else functional_traffic(run_id))
        _write_new(run_dir / "40-functional-traffic.json", functional_result)
        steps.append("functional-traffic-runner")
        if (functional_result.get("status") != "complete"
                or not isinstance(functional_result.get("epoch"), str)
                or functional_result.get("epoch") != ownership_epoch
                or functional_result.get("identity") != ownership_identity
                or not isinstance(functional_result.get("reports"), list)):
            raise OperatorBlocked("functional traffic result incomplete")
        _common_session(before, traffic_result, functional_result, run_id)
        lifecycle_result = dict(lifecycle(run_id, functional_result))
        _write_new(run_dir / "50-lifecycle-receipts.json", lifecycle_result)
        steps.append("post-run-lifecycle")
        if (lifecycle_result.get("runId") != run_id
                or lifecycle_result.get("epoch") != functional_result.get("epoch")
                or lifecycle_result.get("identity") != functional_result.get("identity")
                or lifecycle_result.get("status") != "complete"
                or lifecycle_result.get("complete") is not True
                or lifecycle_result.get("residuals") != []):
            raise OperatorBlocked("lifecycle receipt bundle is foreign")
        accepted = dict(acceptance(run_id, functional_result, lifecycle_result))
        _write_new(run_dir / "60-functional-acceptance.json", accepted)
        steps.append("functional-acceptance")
        if (accepted.get("status") != "complete"
                or accepted.get("benchmarkRunId") != run_id):
            raise OperatorBlocked("functional acceptance incomplete or foreign")
        result = {"schemaVersion": SCHEMA, "runId": run_id, "status": "ready-for-assembly",
                  "steps": steps, "cleanupVerified": accepted.get("cleanupVerified") is True,
                  "finishedAt": time.time(), "artifactDirectory": str(run_dir)}
        if not result["cleanupVerified"]:
            raise OperatorBlocked("runner cleanup incomplete")
        _write_new(run_dir / "result.json", result)
        return result
    except Exception as exc:
        failure = {"schemaVersion": SCHEMA, "runId": run_id, "status": "incomplete",
                   "stage": steps[-1] if steps else "telemetry-before", "steps": steps,
                   "errorType": type(exc).__name__, "error": str(exc),
                   "artifactsRetained": True}
        _write_new(run_dir / "failure.json", failure)
        return failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = orchestrate(config, execute=args.execute, output_root=args.output_root,
                             run_id_factory=(lambda: args.run_id) if args.run_id else None)
    except (OSError, ValueError, OperatorBlocked, json.JSONDecodeError) as exc:
        result = {"schemaVersion": SCHEMA, "status": "incomplete", "stage": "readiness",
                  "commandsExecuted": 0, "error": str(exc)}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result.get("status") in {"ready-for-assembly"} or not args.execute else 2


if __name__ == "__main__":
    raise SystemExit(main())
