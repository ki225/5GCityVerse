"""Plan and capture bounded, read-only on-prem packet-path telemetry.

``observe`` prints the exact command plan and performs no subprocess calls.
``capture`` executes only the generated allowlisted argv and creates one JSON
artifact with exclusive-create semantics.  It never starts traffic and never
adds receiver or cleanup evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from backend.onprem.evidence.network_telemetry import (
    REQUIRED_KINDS,
    SCHEMA_VERSION,
    TelemetryValidationError,
    derive_delta,
    parse_observation,
    parse_snapshot,
    validate_read_only_command,
)


CAPTURE_SCHEMA_VERSION = "cityverse.network-telemetry-capture/v1"
PLAN_SCHEMA_VERSION = "cityverse.network-telemetry-plan/v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,128}\Z")


@dataclass(frozen=True)
class CommandSpec:
    kind: str
    scope: str
    subject: str
    argv: tuple[str, ...]
    optional: bool = False


def _require_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise TelemetryValidationError(f"invalid {name}")
    return value


def _validate_cgroup(path: str) -> str:
    pure = PurePosixPath(path)
    if not path.startswith("/sys/fs/cgroup/") or pure.name != "cpu.stat" or ".." in pure.parts:
        raise TelemetryValidationError("invalid cgroup cpu.stat path")
    return path


def build_plan(identity: Mapping[str, Any], *, qemu_pid: int, qemu_netdev_id: str,
               cgroup_cpu_stat: str | None, guest_ssh_target: str,
               additional_targets: Sequence[tuple[str, int, str]] = ()) -> list[CommandSpec]:
    """Generate the complete fixed argv set from validated target identities."""
    required = ("node", "podNamespace", "pod", "vm", "interface",
                "vmEpoch", "interfaceEpoch", "sessionEpoch")
    for field in required:
        _require_id(field, identity.get(field))
    if type(identity.get("pduSessionId")) is not int or identity["pduSessionId"] < 0:
        raise TelemetryValidationError("invalid pduSessionId")
    if type(qemu_pid) is not int or qemu_pid <= 0:
        raise TelemetryValidationError("invalid qemu pid")
    netdev = _require_id("qemu netdev id", qemu_netdev_id)
    ssh_target = _require_id("guest SSH target", guest_ssh_target)
    workload_uid = identity.get("upfPodUid", identity.get("workloadPodUid"))
    _require_id("workload Pod UID", workload_uid)
    cgroup = _validate_cgroup(cgroup_cpu_stat) if cgroup_cpu_stat is not None else None
    targets = [(identity["interface"], qemu_pid, netdev)] + list(additional_targets)
    normalized_targets = []
    for iface, pid, target_netdev in targets:
        _require_id("interface", iface)
        _require_id("qemu netdev id", target_netdev)
        if type(pid) is not int or pid <= 0:
            raise TelemetryValidationError("invalid qemu pid")
        normalized_targets.append((iface, pid, target_netdev))
    if len({iface for iface, _, _ in normalized_targets}) != len(normalized_targets):
        raise TelemetryValidationError("duplicate interface target")
    pod = f"{identity['podNamespace']}/{identity['pod']}/{workload_uid}"
    guest_prefix = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", "--",
                    ssh_target)
    rows = []
    for iface, pid, target_netdev in normalized_targets:
        guest = f"{identity['vm']}/{iface}"
        qemu = f"{identity['vm']}/{iface}/{target_netdev}"
        rows.extend([
            CommandSpec("queue_inventory", "guest", guest,
                        guest_prefix + ("ls", "-1", f"/sys/class/net/{iface}/queues")),
            CommandSpec("queue_capability", "guest", guest, guest_prefix + ("ethtool", "-l", iface), True),
            CommandSpec("offload", "guest", guest, guest_prefix + ("ethtool", "-k", iface), True),
            CommandSpec("ring", "guest", guest, guest_prefix + ("ethtool", "-g", iface), True),
            CommandSpec("driver_counters", "guest", guest, guest_prefix + ("ethtool", "-S", iface), True),
            CommandSpec("link", "guest", guest,
                        guest_prefix + ("ip", "-j", "-s", "link", "show", "dev", iface)),
            CommandSpec("qemu", "host", qemu,
                        ("ps", "-ww", "-p", str(pid), "-o", "args=")),
        ])
    primary_guest = f"{identity['vm']}/{identity['interface']}"
    rows.extend([
        CommandSpec("softnet", "guest", primary_guest, guest_prefix + ("cat", "/proc/net/softnet_stat")),
        CommandSpec("udp", "guest", primary_guest, guest_prefix + ("cat", "/proc/net/snmp")),
        CommandSpec("interrupts", "guest", primary_guest, guest_prefix + ("cat", "/proc/interrupts")),
        CommandSpec("softirqs", "guest", primary_guest, guest_prefix + ("cat", "/proc/softirqs")),
        CommandSpec("per_cpu", "guest", primary_guest, guest_prefix + ("cat", "/proc/stat")),
        CommandSpec("cpu_count", "guest", primary_guest, guest_prefix + ("nproc",)),
        CommandSpec("clock_ticks", "guest", primary_guest, guest_prefix + ("getconf", "CLK_TCK")),
        CommandSpec("guest_identity", "guest", identity["vm"], guest_prefix + ("hostname",)),
    ])
    if cgroup is not None:
        rows.append(CommandSpec("cgroup_cpu", "cgroup", pod, guest_prefix + ("cat", cgroup)))
    for row in rows:
        validate_read_only_command(row.argv)
    return rows


def serialize_plan(identity: Mapping[str, Any], plan: Sequence[CommandSpec]) -> dict[str, Any]:
    return {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "mode": "observe",
        "readOnly": True,
        "identity": dict(identity),
        "commands": [{"kind": row.kind, "scope": row.scope, "subject": row.subject,
                      "argv": list(row.argv), "optional": row.optional} for row in plan],
    }


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


def _capture_status(observations: Sequence[Mapping[str, Any]], errors: Sequence[str],
                    plan: Sequence[CommandSpec]) -> tuple[str, list[str]]:
    reasons = list(errors)
    indexed = {(row.get("kind"), row.get("scope"), row.get("subject")): row
               for row in observations if isinstance(row, Mapping)}
    for item in plan:
        row = indexed.get((item.kind, item.scope, item.subject))
        states = {"observed", "unsupported"} if item.optional else {"observed"}
        if row is None or row.get("state") not in states:
            reasons.append(f"required {item.kind} evidence missing for {item.subject}")
    return ("complete" if not reasons else "incomplete", reasons)


def capture_snapshot(*, phase: str, epoch: str, identity: Mapping[str, Any],
                     plan: Sequence[CommandSpec], executor: Callable[[Sequence[str], float], Any] = _execute,
                     clock: Callable[[], float] = time.time, timeout: float = 2.0,
                     before: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if phase not in {"before", "after"}:
        raise TelemetryValidationError("phase must be before or after")
    _require_id("epoch", epoch)
    errors: list[str] = []
    observations: list[dict[str, Any]] = []
    for item in plan:
        argv = validate_read_only_command(item.argv)
        result = executor(argv, timeout)
        raw = {"kind": item.kind, "scope": item.scope, "subject": item.subject,
               "epoch": epoch, "timestamp": clock(), "command": list(argv),
               "exitCode": int(result.returncode), "stdout": result.stdout or "",
               "stderr": result.stderr or "", "optional": item.optional}
        try:
            observations.append(parse_observation(raw))
        except (TelemetryValidationError, TypeError, ValueError) as exc:
            observations.append(raw)
            errors.append(f"{item.kind}: {exc}")
    snapshot = {"schemaVersion": SCHEMA_VERSION, "phase": phase, "epoch": epoch,
                "identity": dict(identity), "timestamp": clock(), "observations": observations}
    try:
        parse_snapshot(snapshot)
    except (TelemetryValidationError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    guest_identity = next((row for row in observations
                           if row.get("kind") == "guest_identity"), None)
    if (guest_identity is None or guest_identity.get("state") != "observed"
            or guest_identity.get("value", {}).get("hostname") != identity.get("vm")):
        errors.append("guest SSH identity does not match requested VM")
    observed_by_key = {(row.get("kind"), row.get("subject")): row for row in observations}
    for item in plan:
        if item.kind != "qemu":
            continue
        qemu_row = observed_by_key.get(("qemu", item.subject))
        guest_subject = "/".join(item.subject.split("/")[:2])
        channel = observed_by_key.get(("queue_capability", guest_subject))
        if qemu_row is None or qemu_row.get("state") != "observed":
            continue
        queues = qemu_row["value"]["backendQueues"]
        channel_value = channel.get("value") if channel else None
        sections = channel_value.get("sections", {}) if isinstance(channel_value, dict) else {}
        current = sections.get("Current hardware settings", {}).get("Combined")
        maximum = sections.get("Pre-set maximums", {}).get("Combined")
        if channel is None or channel.get("state") != "observed" or current != queues \
                or type(maximum) is not int or maximum < queues:
            errors.append(f"active queue channel readback mismatch for {guest_subject}")
    if before is not None:
        try:
            if phase != "after":
                raise TelemetryValidationError("before artifact is valid only for after capture")
            parsed_before = parse_snapshot(before)
            if parsed_before["epoch"] != epoch or parsed_before["identity"] != dict(identity):
                raise TelemetryValidationError("mixed telemetry epoch or identity")
            snapshot["delta"] = derive_delta(before, snapshot)
        except (TelemetryValidationError, TypeError, ValueError) as exc:
            errors.append(str(exc))
    elif phase == "after":
        errors.append("after capture requires a before artifact for epoch validation")
    status, reasons = _capture_status(observations, errors, plan)
    return {"schemaVersion": CAPTURE_SCHEMA_VERSION, "captureMode": "read-only",
            "status": status, "reasons": reasons, "snapshot": snapshot,
            "excludedEvidence": ["receiverResult", "cleanup"]}


def write_new_artifact(path: Path, artifact: Mapping[str, Any]) -> None:
    """Create exactly one artifact and refuse any existing destination."""
    payload = json.dumps(artifact, sort_keys=True, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _identity(args: argparse.Namespace) -> dict[str, Any]:
    additional = [_parse_target(value) for value in args.additional_target]
    return {"node": args.node, "podNamespace": args.pod_namespace, "pod": args.pod,
            "upfPodUid": args.pod_uid, "vm": args.vm, "interface": args.interface,
            "vmEpoch": args.vm_epoch, "interfaceEpoch": args.interface_epoch,
            "sessionEpoch": args.session_epoch, "pduSessionId": args.pdu_session_id,
            "interfaces": [args.interface] + [value[0] for value in additional]}


def _parse_target(value: str) -> tuple[str, int, str]:
    parts = value.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        raise argparse.ArgumentTypeError("target must be INTERFACE:QEMU_PID:NETDEV_ID")
    return parts[0], int(parts[1]), parts[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("observe", "capture"):
        command = sub.add_parser(name)
        command.add_argument("--node", required=True)
        command.add_argument("--pod-namespace", required=True)
        command.add_argument("--pod", required=True)
        command.add_argument("--pod-uid", required=True)
        command.add_argument("--vm", required=True)
        command.add_argument("--interface", required=True)
        command.add_argument("--vm-epoch", required=True)
        command.add_argument("--interface-epoch", required=True)
        command.add_argument("--session-epoch", required=True)
        command.add_argument("--pdu-session-id", required=True, type=int)
        command.add_argument("--qemu-pid", required=True, type=int)
        command.add_argument("--qemu-netdev-id", required=True)
        command.add_argument("--additional-target", action="append", default=[],
                             metavar="INTERFACE:QEMU_PID:NETDEV_ID")
        command.add_argument("--guest-ssh-target", required=True)
        command.add_argument("--cgroup-cpu-stat", required=True)
    capture = sub.choices["capture"]
    capture.add_argument("--phase", choices=("before", "after"), required=True)
    capture.add_argument("--epoch", required=True)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--before", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    identity = _identity(args)
    additional = [_parse_target(value) for value in args.additional_target]
    plan = build_plan(identity, qemu_pid=args.qemu_pid, qemu_netdev_id=args.qemu_netdev_id,
                      cgroup_cpu_stat=args.cgroup_cpu_stat, guest_ssh_target=args.guest_ssh_target,
                      additional_targets=additional)
    if args.mode == "observe":
        print(json.dumps(serialize_plan(identity, plan), sort_keys=True, indent=2))
        return 0
    before = None
    if args.before is not None:
        with args.before.open("r", encoding="utf-8") as handle:
            source = json.load(handle)
        before = source.get("snapshot") if source.get("schemaVersion") == CAPTURE_SCHEMA_VERSION else source
    artifact = capture_snapshot(phase=args.phase, epoch=args.epoch, identity=identity,
                                plan=plan, before=before)
    write_new_artifact(args.output, artifact)
    return 0 if artifact["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
