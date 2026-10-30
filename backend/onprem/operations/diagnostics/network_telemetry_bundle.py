"""Read-only RAN/UPF telemetry bundle without inventing a shared VM epoch."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from backend.onprem.evidence.network_telemetry import TelemetryValidationError
from backend.onprem.operations.diagnostics.network_telemetry_cli import build_plan, capture_snapshot, serialize_plan, write_new_artifact


BUNDLE_SCHEMA_VERSION = "cityverse.network-telemetry-cross-vm-bundle/v1"
RECEIVER_SCHEMA_VERSION = "cityverse.network-throughput-benchmark-result/v1"
_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class BundleArtifactWriteError(RuntimeError):
    """Exclusive bundle write failed after zero or more side artifacts."""

    def __init__(self, failed: Path, created: Sequence[Path]):
        self.failed = failed
        self.created = tuple(created)
        super().__init__(f"partial bundle write: failed={failed}; created={','.join(map(str, created))}")


def _side_plan(role: str, config: Mapping[str, Any]):
    identity = config.get("identity")
    if not isinstance(identity, Mapping):
        raise TelemetryValidationError(f"{role} identity missing")
    if role == "ran" and "workloadPodUid" not in identity:
        raise TelemetryValidationError("RAN workload Pod UID missing")
    if role == "upf" and "upfPodUid" not in identity:
        raise TelemetryValidationError("UPF Pod UID missing")
    cgroup = config.get("cgroupCpuStat") if role == "upf" else None
    if role == "upf" and not isinstance(cgroup, str):
        raise TelemetryValidationError("UPF cgroup cpu.stat missing")
    additional = config.get("additionalTargets", [])
    if not isinstance(additional, list):
        raise TelemetryValidationError(f"{role} additional targets invalid")
    targets = []
    for value in additional:
        if not (isinstance(value, Mapping) and type(value.get("qemuPid")) is int):
            raise TelemetryValidationError(f"{role} additional target invalid")
        targets.append((value.get("interface"), value["qemuPid"], value.get("qemuNetdevId")))
    return identity, build_plan(
        identity, qemu_pid=config.get("qemuPid"), qemu_netdev_id=config.get("qemuNetdevId"),
        cgroup_cpu_stat=cgroup, guest_ssh_target=config.get("guestSshTarget"),
        additional_targets=targets,
    )


def observe_bundle(config: Mapping[str, Any]) -> dict[str, Any]:
    sides = {}
    for role in ("ran", "upf"):
        identity, plan = _side_plan(role, config.get(role, {}))
        sides[role] = serialize_plan(identity, plan)
    return {"schemaVersion": BUNDLE_SCHEMA_VERSION, "mode": "observe", "readOnly": True,
            "sides": sides, "excludedEvidence": ["receiverResult", "cleanup"]}


def _receiver_result(value: Mapping[str, Any], expected_run_id: str | None,
                     identities: Mapping[str, Mapping[str, Any]],
                     before: Mapping[str, Any], sides: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a completed benchmark result inside the two-VM telemetry bracket."""
    if not isinstance(value, Mapping):
        raise TelemetryValidationError("receiver result missing")
    if value.get("schemaVersion") != RECEIVER_SCHEMA_VERSION:
        raise TelemetryValidationError("receiver result schema mismatch")
    if not isinstance(expected_run_id, str) or not _RUN_ID.fullmatch(expected_run_id) \
            or value.get("runId") != expected_run_id:
        raise TelemetryValidationError("receiver run ID mismatch")
    if value.get("profile") != "network-throughput" or value.get("status") != "complete" \
            or value.get("benchmarkComplete") is not True:
        raise TelemetryValidationError("receiver benchmark is not complete")
    identity = value.get("identity")
    if not isinstance(identity, Mapping):
        raise TelemetryValidationError("receiver identity missing")
    identity_fields = ("supi", "pduSessionId", "ueIp", "tun", "receiverId")
    for field in identity_fields:
        expected = identities["upf"].get(field)
        if (expected in (None, "")
                or identities["ran"].get(field) != expected
                or identity.get(field) != expected):
            raise TelemetryValidationError(f"receiver {field} identity mismatch")
    numeric = ("receiverGoodputBps", "receivedPackets", "deliveredPackets",
               "lostPackets", "reorderedPackets")
    if any(type(value.get(key)) not in (int, float) or value[key] < 0 for key in numeric):
        raise TelemetryValidationError("receiver counters invalid")
    if value["receiverGoodputBps"] <= 0 or value["receivedPackets"] <= 0:
        raise TelemetryValidationError("receiver result has no measured traffic")
    duration = value.get("durationSeconds")
    if type(duration) not in (int, float) or duration <= 0:
        raise TelemetryValidationError("receiver duration bound missing")
    refs = value.get("rawArtifactRefs")
    if (value.get("source") != "native-iperf3-receiver" or not isinstance(refs, list)
            or not any(isinstance(ref, str) and "/receiver.raw.json#sha256:" in ref
                       for ref in refs)):
        raise TelemetryValidationError("receiver raw provenance missing")
    started, finished = value.get("startedAt"), value.get("finishedAt")
    if type(started) not in (int, float) or type(finished) not in (int, float) or finished <= started:
        raise TelemetryValidationError("receiver measurement interval invalid")
    if finished - started > duration + 2:
        raise TelemetryValidationError("receiver measurement exceeded duration bound")
    before_fence = max(before["sides"][role]["snapshot"]["timestamp"] for role in ("ran", "upf"))
    after_fence = min(sides[role]["snapshot"]["timestamp"] for role in ("ran", "upf"))
    if started < before_fence or finished > after_fence:
        raise TelemetryValidationError("receiver measurement is outside telemetry bracket")
    return dict(value)


def capture_bundle(*, phase: str, config: Mapping[str, Any], before: Mapping[str, Any] | None = None,
                   receiver_result: Mapping[str, Any] | None = None,
                   run_id: str | None = None,
                   executors: Mapping[str, Callable] | None = None,
                   clock: Callable[[], float] | None = None) -> dict[str, Any]:
    reasons = []
    if phase == "after":
        valid_before = (isinstance(before, Mapping)
                        and before.get("schemaVersion") == BUNDLE_SCHEMA_VERSION
                        and before.get("phase") == "before"
                        and before.get("status") == "complete"
                        and isinstance(before.get("sides"), Mapping)
                        and set(before["sides"]) == {"ran", "upf"}
                        and all(isinstance(before["sides"].get(role), Mapping)
                                and before["sides"][role].get("status") == "complete"
                                and isinstance(before["sides"][role].get("snapshot"), Mapping)
                                for role in ("ran", "upf")))
        if not valid_before:
            return {"schemaVersion": BUNDLE_SCHEMA_VERSION, "phase": phase,
                    "status": "incomplete", "reasons": ["complete before bundle required"],
                    "sides": {}, "excludedEvidence": ["receiverResult", "cleanup"]}
    elif before is not None:
        reasons.append("before input is invalid for before phase")
    if phase == "before" and receiver_result is not None:
        reasons.append("receiver result is invalid for before phase")
    sides = {}
    identities = {}
    for role in ("ran", "upf"):
        try:
            identity, plan = _side_plan(role, config.get(role, {}))
            identities[role] = identity
            prior = None
            if before is not None:
                prior_side = before.get("sides", {}).get(role)
                if not isinstance(prior_side, Mapping):
                    raise TelemetryValidationError(f"before bundle missing {role} side")
                prior = prior_side.get("snapshot")
            kwargs: dict[str, Any] = {"phase": phase, "epoch": identity.get("vmEpoch"),
                                      "identity": identity, "plan": plan, "before": prior}
            if executors is not None and role in executors:
                kwargs["executor"] = executors[role]
            if clock is not None:
                kwargs["clock"] = clock
            sides[role] = capture_snapshot(**kwargs)
            if sides[role]["status"] != "complete":
                reasons.append(f"{role} capture incomplete")
        except (TelemetryValidationError, TypeError, ValueError, KeyError) as exc:
            reasons.append(f"{role}: {exc}")
    if set(identities) == {"ran", "upf"}:
        sessions = {identities[role].get("sessionEpoch") for role in identities}
        pdus = {identities[role].get("pduSessionId") for role in identities}
        if len(sessions) != 1 or None in sessions:
            reasons.append("RAN/UPF sessionEpoch mismatch")
        if len(pdus) != 1 or None in pdus:
            reasons.append("RAN/UPF PDU session mismatch")
        if identities["ran"].get("vmEpoch") == identities["upf"].get("vmEpoch"):
            reasons.append("RAN/UPF VM epochs must remain distinct")
    else:
        reasons.append("cross-VM bundle requires both RAN and UPF sides")
    receiver = None
    if phase == "after" and not reasons:
        try:
            receiver = _receiver_result(receiver_result, run_id, identities, before, sides)
        except (TelemetryValidationError, TypeError, ValueError, KeyError) as exc:
            reasons.append(str(exc))
    result = {"schemaVersion": BUNDLE_SCHEMA_VERSION, "phase": phase,
            "status": "complete" if not reasons else "incomplete", "reasons": reasons,
            "sides": sides, "excludedEvidence": ["receiverResult", "cleanup"]}
    if receiver is not None:
        result["receiverResult"] = receiver
        result["excludedEvidence"] = ["cleanup"]
    return result


def write_bundle_artifacts(directory: Path, run_id: str, bundle: Mapping[str, Any]) -> list[Path]:
    if not _RUN_ID.fullmatch(run_id):
        raise TelemetryValidationError("invalid bundle run ID")
    phase = bundle.get("phase")
    if phase not in {"before", "after"}:
        raise TelemetryValidationError("bundle phase missing")
    paths = [directory / f"{run_id}.{phase}.{role}.json" for role in ("ran", "upf")]
    paths.append(directory / f"{run_id}.{phase}.bundle.json")
    error_path = directory / f"{run_id}.{phase}.partial-error.json"
    if any(path.exists() for path in [*paths, error_path]):
        raise FileExistsError("bundle artifact already exists")
    created = []
    try:
        for role, path in zip(("ran", "upf"), paths[:2]):
            write_new_artifact(path, bundle.get("sides", {}).get(role, {"status": "missing"}))
            created.append(path)
        write_new_artifact(paths[2], bundle)
        created.append(paths[2])
    except (FileExistsError, OSError):
        error = {"schemaVersion": BUNDLE_SCHEMA_VERSION, "status": "partial",
                 "failedArtifact": str(paths[len(created)]),
                 "createdArtifacts": [str(path) for path in created]}
        try:
            write_new_artifact(error_path, error)
        except (FileExistsError, OSError):
            pass
        raise BundleArtifactWriteError(paths[len(created)], created)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    observe = sub.add_parser("observe")
    capture = sub.add_parser("capture")
    for command in (observe, capture):
        command.add_argument("--config", required=True, type=Path)
    capture.add_argument("--phase", choices=("before", "after"), required=True)
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--output-dir", required=True, type=Path)
    capture.add_argument("--before", type=Path)
    capture.add_argument("--receiver", type=Path,
                         help="completed network-throughput benchmark result (required for after)")
    args = parser.parse_args(argv)
    with args.config.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if args.mode == "observe":
        print(json.dumps(observe_bundle(config), sort_keys=True, indent=2))
        return 0
    prior = None
    if args.before:
        with args.before.open("r", encoding="utf-8") as handle:
            prior = json.load(handle)
    receiver = None
    if args.receiver:
        with args.receiver.open("r", encoding="utf-8") as handle:
            receiver = json.load(handle)
    bundle = capture_bundle(phase=args.phase, config=config, before=prior,
                            receiver_result=receiver, run_id=args.run_id)
    write_bundle_artifacts(args.output_dir, args.run_id, bundle)
    return 0 if bundle["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
