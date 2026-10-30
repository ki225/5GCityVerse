"""Strict, read-only packet-path telemetry records for network A/B trials.

This module parses already captured command results.  It neither executes a
command nor changes a host, guest, Pod, or Kubernetes object.  Missing and
unsupported data remain explicit states; a caller-provided parsed value is
never accepted as a substitute for raw stdout.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "cityverse.network-telemetry/v1"
RUN_SCHEMA_VERSION = "cityverse.network-throughput-telemetry-run/v1"
SCOPES = {"host", "guest", "pod-netns", "cgroup"}
COUNTER_KINDS = {"softnet", "udp", "link", "interrupts", "softirqs", "per_cpu", "cgroup_cpu",
                 "driver_counters"}
REQUIRED_KINDS = {
    "queue_inventory", "softnet", "udp", "link", "interrupts", "softirqs",
    "offload", "per_cpu", "cpu_count", "clock_ticks", "guest_identity", "cgroup_cpu", "qemu",
}
OPTIONAL_ETHTOOL_KINDS = {"offload", "queue_capability", "ring", "driver_counters"}
_TOKEN_FORBIDDEN = re.compile(r"[\x00\r\n;&|><`$()]")
_IFACE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_HEX_ID = re.compile(r"[a-fA-F0-9]{12,128}\Z")
_SSH_TARGET = re.compile(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.:-]{1,253}\Z")
_SSH_REMOTE_TOKEN = re.compile(r"[A-Za-z0-9_./:=+@-]+\Z")


class TelemetryValidationError(ValueError):
    """Telemetry is malformed, unsafe, or cannot produce a truthful delta."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise TelemetryValidationError(message)


def _number(value: Any, *, positive: bool = False) -> float | int:
    _need(type(value) in (int, float) and math.isfinite(value), "finite numeric value required")
    _need(value > 0 if positive else value >= 0, "nonnegative numeric value required")
    return value


def _actual_command(argv: Sequence[str]) -> tuple[str, ...]:
    _need(not isinstance(argv, (str, bytes)) and isinstance(argv, Sequence) and argv,
          "command must be a nonempty argv sequence")
    command = tuple(argv)
    _need(all(isinstance(part, str) and part and not _TOKEN_FORBIDDEN.search(part) for part in command),
          "unsafe command token")
    if command[:3] == ("sudo", "-n", "--"):
        command = command[3:]
    if (len(command) > 7 and command[:6] ==
            ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", "--")):
        _need(bool(_SSH_TARGET.fullmatch(command[6])),
              "unsafe SSH telemetry target")
        command = command[7:]
        _need(command and all(_SSH_REMOTE_TOKEN.fullmatch(part) for part in command),
              "unsafe SSH remote command token")
    _need(command, "empty command")
    return command


def validate_read_only_command(argv: Sequence[str]) -> tuple[str, ...]:
    """Return normalized argv only for the telemetry read allowlist.

    Shells, redirects, writes, mutation verbs, and ethtool setter forms are not
    accepted.  Privilege elevation may only be the exact ``sudo -n --`` prefix.
    """
    command = _actual_command(argv)
    program = os.path.basename(command[0])
    args = command[1:]
    allowed = False
    if program == "cat" and len(args) == 1:
        path = args[0]
        allowed = path in {
            "/proc/interrupts", "/proc/softirqs", "/proc/net/softnet_stat",
            "/proc/net/snmp", "/proc/stat",
        }
        if not allowed and path.startswith("/sys/fs/cgroup/") and path.endswith("/cpu.stat"):
            pure = PurePosixPath(path)
            allowed = ".." not in pure.parts
    elif program == "ls" and len(args) == 2 and args[0] == "-1":
        allowed = bool(re.fullmatch(r"/sys/class/net/[A-Za-z0-9_.:-]{1,64}/queues", args[1]))
    elif program == "ethtool" and len(args) == 2:
        allowed = args[0] in {"-l", "-k", "-g", "-S"} and bool(_IFACE.fullmatch(args[1]))
    elif program == "ip":
        allowed = (len(args) == 6 and args[:4] == ("-j", "-s", "link", "show")
                   and args[4] == "dev" and bool(_IFACE.fullmatch(args[5])))
    elif program == "ss":
        allowed = args == ("-u", "-a", "-m", "-n")
    elif program in {"nproc", "uname", "hostname"}:
        allowed = ((program in {"nproc", "hostname"} and not args)
                   or (program == "uname" and args == ("-r",)))
    elif program == "getconf":
        allowed = args == ("CLK_TCK",)
    elif program == "ps" and len(args) == 5:
        allowed = args[0] == "-ww" and args[1] == "-p" and args[2].isdigit() and args[3:] == ("-o", "args=")
    elif program == "readlink" and len(args) == 2:
        allowed = args[0] == "-f" and bool(re.fullmatch(r"/proc/[1-9][0-9]*/ns/net", args[1]))
    elif program == "crictl" and len(args) == 2:
        allowed = args[0] == "inspect" and bool(_HEX_ID.fullmatch(args[1]))
    elif program in {"kubectl", "kubectl.exe"} and args:
        allowed = args[0] in {"get", "version"} and not any(
            token in {"--raw", "--server", "--as", "--as-group"} or token.startswith("--raw=")
            for token in args[1:]
        )
    _need(allowed, "command is outside the read-only telemetry allowlist")
    return tuple(argv)


def _parse_queue(stdout: str) -> dict[str, Any]:
    names = [line.strip() for line in stdout.splitlines() if line.strip()]
    _need(names and len(names) == len(set(names)), "queue inventory missing or duplicated")
    rx = sorted(int(name[3:]) for name in names if re.fullmatch(r"rx-[0-9]+", name))
    tx = sorted(int(name[3:]) for name in names if re.fullmatch(r"tx-[0-9]+", name))
    _need(rx and tx and len(rx) + len(tx) == len(names), "invalid RX/TX queue inventory")
    return {"rx": rx, "tx": tx, "rxCount": len(rx), "txCount": len(tx)}


def _parse_softnet(stdout: str) -> dict[str, Any]:
    rows = []
    for cpu, line in enumerate(line for line in stdout.splitlines() if line.strip()):
        fields = line.split()
        _need(len(fields) >= 3 and all(re.fullmatch(r"[0-9a-fA-F]+", value) for value in fields[:3]),
              "invalid softnet row")
        rows.append({"cpu": cpu, "processed": int(fields[0], 16), "dropped": int(fields[1], 16),
                     "timeSqueeze": int(fields[2], 16)})
    _need(rows, "softnet evidence missing")
    return {"perCpu": rows}


def _parse_udp(stdout: str) -> dict[str, int]:
    lines = [line.split() for line in stdout.splitlines() if line.startswith("Udp:")]
    _need(len(lines) == 2 and len(lines[0]) == len(lines[1]), "UDP SNMP header/value pair required")
    names, values = lines
    _need(names[0] == values[0] == "Udp:" and all(value.isdigit() for value in values[1:]),
          "invalid UDP counters")
    parsed = dict(zip(names[1:], map(int, values[1:])))
    _need({"InDatagrams", "NoPorts", "InErrors", "OutDatagrams", "RcvbufErrors", "SndbufErrors"} <= set(parsed),
          "required UDP drop counters missing")
    return parsed


def _parse_link(stdout: str) -> dict[str, Any]:
    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TelemetryValidationError("invalid link JSON") from exc
    _need(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict), "one link required")
    row = rows[0]
    stats = row.get("stats64", row.get("stats"))
    _need(isinstance(stats, dict), "link statistics missing")
    result = {"ifname": row.get("ifname"), "mtu": row.get("mtu")}
    for direction in ("rx", "tx"):
        source = stats.get(direction)
        _need(isinstance(source, dict), "link direction statistics missing")
        required = ("bytes", "packets", "errors", "dropped")
        _need(all(type(source.get(key)) is int and source[key] >= 0 for key in required),
              "invalid link counters")
        result[direction] = {key: source[key] for key in required}
    _need(isinstance(result["ifname"], str) and result["ifname"], "link identity missing")
    return result


def _parse_interrupts(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    _need(lines and "CPU" in lines[0], "interrupt CPU header missing")
    cpus = re.findall(r"CPU[0-9]+", lines[0])
    rows = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        tokens = rest.split()
        counts = []
        for token in tokens:
            if not token.isdigit():
                break
            counts.append(int(token))
        if len(counts) == len(cpus):
            rows.append({"irq": name.strip(), "counts": counts, "label": " ".join(tokens[len(counts):])})
    _need(cpus and rows, "interrupt counters missing")
    return {"cpus": cpus, "rows": rows}


def _parse_softirqs(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    _need(lines and "CPU" in lines[0], "softirq CPU header missing")
    cpus = re.findall(r"CPU[0-9]+", lines[0])
    rows = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        values = raw.split()
        if len(values) == len(cpus) and all(value.isdigit() for value in values):
            rows[name.strip()] = list(map(int, values))
    _need({"NET_RX", "NET_TX"} <= set(rows), "network softirq counters missing")
    return {"cpus": cpus, "rows": rows}


def _parse_key_values(stdout: str, required: Iterable[str] = ()) -> dict[str, int]:
    result = {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            result[fields[0]] = int(fields[1])
    _need(result and set(required) <= set(result), "required counter fields missing")
    return result


def _parse_per_cpu(stdout: str) -> dict[str, Any]:
    rows = {}
    for line in stdout.splitlines():
        fields = line.split()
        if re.fullmatch(r"cpu[0-9]+", fields[0] if fields else ""):
            _need(len(fields) >= 8 and all(value.isdigit() for value in fields[1:]), "invalid per-CPU counters")
            rows[fields[0]] = list(map(int, fields[1:]))
    _need(rows, "per-CPU evidence missing")
    return {"rows": rows}


def _parse_cpu_count(stdout: str) -> dict[str, int]:
    value = stdout.strip()
    _need(value.isdigit() and int(value) > 0, "guest vCPU count missing")
    return {"count": int(value)}


def _parse_clock_ticks(stdout: str) -> dict[str, int]:
    value = stdout.strip()
    _need(value.isdigit() and 1 <= int(value) <= 1_000_000, "guest CLK_TCK missing")
    return {"ticksPerSecond": int(value)}


def _parse_guest_identity(stdout: str) -> dict[str, str]:
    value = stdout.strip()
    _need(bool(_SSH_TARGET.fullmatch(value)) and "@" not in value,
          "guest hostname identity missing")
    return {"hostname": value}


def _parse_offload(stdout: str) -> dict[str, Any]:
    features = {}
    for line in stdout.splitlines():
        match = re.fullmatch(r"\s*([a-zA-Z0-9_-]+):\s+(on|off)(?:\s+\[fixed\])?\s*", line)
        if match:
            features[match.group(1)] = match.group(2) == "on"
    _need(features, "offload capability output missing")
    return {"features": features}


def _parse_ethtool_sections(stdout: str) -> dict[str, Any]:
    section, result = "values", {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not re.match(r"[A-Za-z0-9_-]+:\s+[0-9]+$", stripped):
            section = stripped[:-1]
            result.setdefault(section, {})
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+):\s+([0-9]+)", stripped)
        if match:
            result.setdefault(section, {})[match.group(1)] = int(match.group(2))
    _need(any(values for values in result.values()), "ethtool capability/counters missing")
    return {"sections": result}


def _parse_qemu(stdout: str, subject: str | None = None) -> dict[str, Any]:
    text = stdout.strip()
    _need(text, "QEMU configuration missing")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        parts = subject.split("/") if isinstance(subject, str) else []
        _need(len(parts) == 3 and all(parts),
              "targeted QEMU NIC JSON readback required or subject must be vm/interface/netdevId")
        vm, interface, netdev_id = parts
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            raise TelemetryValidationError("invalid QEMU argv readback") from exc
        _need(argv and os.path.basename(argv[0]).startswith("qemu-system-"),
              "target process is not QEMU")
        names = []
        netdevs = []
        frontends = []
        for index, token in enumerate(argv[:-1]):
            if token == "-name":
                raw_name = argv[index + 1]
                name_fields = dict(item.split("=", 1) for item in raw_name.split(",")
                                   if "=" in item)
                names.append(name_fields.get("guest", raw_name.split(",", 1)[0]))
            elif token == "-netdev":
                fields = dict(item.split("=", 1) for item in argv[index + 1].split(",")[1:]
                              if "=" in item)
                if fields.get("id") == netdev_id:
                    netdevs.append(fields)
            elif token == "-device":
                fields = dict(item.split("=", 1) for item in argv[index + 1].split(",")[1:]
                              if "=" in item)
                if fields.get("netdev") == netdev_id:
                    frontends.append(fields)
        _need(len(names) == 1 and names[0] == vm, "QEMU VM identity mismatch")
        _need(len(netdevs) == len(frontends) == 1, "QEMU NIC readback is ambiguous")
        actual_netdev_id = netdevs[0].get("id")
        _need(actual_netdev_id == netdev_id and frontends[0].get("netdev") == actual_netdev_id,
              "QEMU netdev identity mismatch")
        backend_raw = netdevs[0].get("queues", "1")
        vectors_raw = frontends[0].get("vectors", "")
        _need(backend_raw.isdigit(), "invalid QEMU backend queue readback")
        backend = int(backend_raw)
        if frontends[0].get("mq") == "on":
            _need(vectors_raw.isdigit() and int(vectors_raw) >= 4 and int(vectors_raw) % 2 == 0,
                  "invalid QEMU frontend vector readback")
            frontend = (int(vectors_raw) - 2) // 2
        else:
            frontend = 1
        value = {"vm": names[0], "interface": interface, "netdevId": actual_netdev_id,
                 "matchCount": 1, "frontendQueues": frontend, "backendQueues": backend}
    _need(isinstance(value, dict), "QEMU configuration object required")
    required = {"vm", "interface", "netdevId", "matchCount", "frontendQueues", "backendQueues"}
    _need(required <= set(value), "targeted QEMU NIC identity/readback missing")
    _need(all(isinstance(value[key], str) and value[key] for key in ("vm", "interface", "netdevId"))
          and value["matchCount"] == 1, "QEMU NIC readback is ambiguous")
    frontend, backend = value["frontendQueues"], value["backendQueues"]
    _need(type(frontend) is int and frontend > 0 and type(backend) is int and backend > 0,
          "invalid QEMU queue readback")
    return {key: value[key] for key in sorted(required)}


_PARSERS = {
    "queue_inventory": _parse_queue, "softnet": _parse_softnet, "udp": _parse_udp,
    "link": _parse_link, "interrupts": _parse_interrupts, "softirqs": _parse_softirqs,
    "offload": _parse_offload, "per_cpu": _parse_per_cpu, "cpu_count": _parse_cpu_count,
    "clock_ticks": _parse_clock_ticks,
    "guest_identity": _parse_guest_identity,
    "queue_capability": _parse_ethtool_sections, "ring": _parse_ethtool_sections,
    "driver_counters": _parse_ethtool_sections,
    "cgroup_cpu": lambda value: _parse_key_values(value, ("usage_usec", "system_usec", "nr_throttled", "throttled_usec")),
    "qemu": _parse_qemu,
}


def command_record(kind: str, scope: str, subject: str, epoch: str, timestamp: float,
                   command: Sequence[str], exit_code: int, stdout: str, stderr: str,
                   *, optional: bool = False) -> dict[str, Any]:
    """Validate and parse one exact command result into a JSON-safe record."""
    raw = {"kind": kind, "scope": scope, "subject": subject, "epoch": epoch,
           "timestamp": timestamp, "command": list(command), "exitCode": exit_code,
           "stdout": stdout, "stderr": stderr, "optional": optional}
    return parse_observation(raw)


def parse_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(raw, Mapping), "telemetry observation required")
    if "value" in raw or "state" in raw:
        _need("value" in raw and "state" in raw, "partial derived observation")
        source = {key: value for key, value in raw.items() if key not in {"value", "state"}}
        parsed = parse_observation(source)
        _need(raw["state"] == parsed["state"] and raw["value"] == parsed["value"],
              "parsed values cannot replace raw evidence")
        return parsed
    kind, scope = raw.get("kind"), raw.get("scope")
    _need(kind in _PARSERS and scope in SCOPES, "unknown telemetry kind or scope")
    subject, epoch = raw.get("subject"), raw.get("epoch")
    _need(isinstance(subject, str) and subject and isinstance(epoch, str) and epoch,
          "telemetry identity missing")
    timestamp = _number(raw.get("timestamp"), positive=True)
    command = list(validate_read_only_command(raw.get("command")))
    exit_code, stdout, stderr = raw.get("exitCode"), raw.get("stdout"), raw.get("stderr")
    _need(type(exit_code) is int and isinstance(stdout, str) and isinstance(stderr, str),
          "exact command outcome required")
    optional = raw.get("optional", False)
    _need(type(optional) is bool, "optional flag must be boolean")
    base = {"kind": kind, "scope": scope, "subject": subject, "epoch": epoch,
            "timestamp": timestamp, "command": command, "exitCode": exit_code,
            "stdout": stdout, "stderr": stderr, "optional": optional}
    if exit_code != 0:
        state = "unsupported" if optional and kind in OPTIONAL_ETHTOOL_KINDS and os.path.basename(_actual_command(command)[0]) == "ethtool" else "missing"
        return {**base, "state": state, "value": None}
    _need(stdout.strip(), "successful command cannot fabricate empty zero evidence")
    value = _parse_qemu(stdout, subject) if kind == "qemu" else _PARSERS[kind](stdout)
    return {**base, "state": "observed", "value": value}


def parse_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(raw, Mapping) and raw.get("schemaVersion") == SCHEMA_VERSION,
          "telemetry snapshot schema mismatch")
    phase = raw.get("phase")
    _need(phase in {"before", "after"}, "snapshot phase must be before or after")
    epoch, identity = raw.get("epoch"), raw.get("identity")
    _need(isinstance(epoch, str) and epoch and isinstance(identity, dict) and identity,
          "snapshot epoch/identity missing")
    timestamp = _number(raw.get("timestamp"), positive=True)
    source = raw.get("observations")
    _need(isinstance(source, list), "snapshot observations required")
    observations = [parse_observation(value) for value in source]
    keys = [(value["kind"], value["scope"], value["subject"]) for value in observations]
    _need(len(keys) == len(set(keys)), "duplicate telemetry observation")
    _need(all(value["epoch"] == epoch and abs(value["timestamp"] - timestamp) <= 30 for value in observations),
          "mixed epoch or stale snapshot observation")
    return {"schemaVersion": SCHEMA_VERSION, "phase": phase, "epoch": epoch,
            "identity": deepcopy(identity), "timestamp": timestamp, "observations": observations}


def _flatten_numbers(value: Any, prefix: str = "") -> dict[str, float | int]:
    result = {}
    if type(value) in (int, float):
        result[prefix] = value
    elif isinstance(value, dict):
        for key, child in value.items():
            result.update(_flatten_numbers(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(_flatten_numbers(child, f"{prefix}[{index}]"))
    return result


def derive_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Derive monotonic deltas; resets/wraps are invalid rather than guessed."""
    first, second = parse_snapshot(before), parse_snapshot(after)
    _need(first["phase"] == "before" and second["phase"] == "after", "snapshot order mismatch")
    _need(first["epoch"] == second["epoch"] and first["identity"] == second["identity"],
          "mixed telemetry epoch or identity")
    _need(second["timestamp"] > first["timestamp"], "non-increasing telemetry timestamps")
    left = {(row["kind"], row["scope"], row["subject"]): row for row in first["observations"]}
    right = {(row["kind"], row["scope"], row["subject"]): row for row in second["observations"]}
    keys = sorted(set(left) | set(right))
    values = []
    for key in keys:
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            values.append({"kind": key[0], "scope": key[1], "subject": key[2], "state": "missing", "value": None})
            continue
        if a["state"] != "observed" or b["state"] != "observed":
            state = "unsupported" if a["state"] == b["state"] == "unsupported" else "missing"
            values.append({"kind": key[0], "scope": key[1], "subject": key[2], "state": state, "value": None})
            continue
        if key[0] in COUNTER_KINDS:
            old, new = _flatten_numbers(a["value"]), _flatten_numbers(b["value"])
            _need(old.keys() == new.keys(), "counter shape changed")
            _need(all(new[name] >= old[name] for name in old), "counter reset or wrap detected")
            value = {name: new[name] - old[name] for name in old}
        else:
            value = {"before": a["value"], "after": b["value"], "changed": a["value"] != b["value"]}
        values.append({"kind": key[0], "scope": key[1], "subject": key[2], "state": "observed", "value": value})
    return {"epoch": first["epoch"], "identity": deepcopy(first["identity"]),
            "startedAt": first["timestamp"], "finishedAt": second["timestamp"], "observations": values}


def _external_result(value: Any, name: str, epoch: str) -> None:
    _need(isinstance(value, dict), f"{name} evidence missing")
    for field in ("timestamp", "command", "exitCode", "stdout", "stderr", "epoch"):
        _need(field in value, f"{name} raw provenance missing")
    _number(value["timestamp"], positive=True)
    _need(isinstance(value["command"], list) and value["command"]
          and all(isinstance(part, str) and part for part in value["command"]), f"{name} command missing")
    _need(type(value["exitCode"]) is int and isinstance(value["stdout"], str)
          and isinstance(value["stderr"], str) and value["epoch"] == epoch,
          f"{name} outcome/epoch mismatch")


def validate_run_evidence(run: Mapping[str, Any], *, now: float | None = None,
                          max_age_seconds: float = 300) -> dict[str, Any]:
    """Return evidence state without manufacturing a performance claim."""
    reasons: list[str] = []
    configured = read_back = observed = False
    delta = None
    try:
        _need(isinstance(run, Mapping) and run.get("schemaVersion") == RUN_SCHEMA_VERSION,
              "throughput telemetry run schema mismatch")
        _need(isinstance(run.get("runId"), str) and run["runId"], "run identity missing")
        identity = run.get("identity")
        required_identity = {"vm", "interface", "vmEpoch", "interfaceEpoch", "sessionEpoch", "pduSessionId", "upfPodUid"}
        _need(isinstance(identity, dict) and required_identity <= set(identity)
              and all(identity[key] not in (None, "") for key in required_identity), "identity evidence missing")
        interfaces = identity.get("interfaces", [identity["interface"]])
        _need(isinstance(interfaces, list) and interfaces and identity["interface"] in interfaces
              and len(interfaces) == len(set(interfaces))
              and all(isinstance(value, str) and value for value in interfaces),
              "interface identity evidence missing")
        configuration = run.get("configuration")
        _need(isinstance(configuration, dict), "configuration evidence missing")
        queues = configuration.get("requestedQueues")
        _need(type(queues) is int and queues > 0 and isinstance(configuration.get("configurationHash"), str)
              and configuration["configurationHash"], "configured queue request missing")
        configured = True
        before, after = parse_snapshot(run.get("before")), parse_snapshot(run.get("after"))
        _need(before["identity"] == after["identity"] == identity, "snapshot identity mismatch")
        delta = derive_delta(run["before"], run["after"])
        timing = run.get("timing")
        _need(isinstance(timing, dict), "timing evidence missing")
        times = [timing.get(key) for key in ("startedAt", "measurementStartedAt", "measurementFinishedAt", "finishedAt")]
        _need(all(type(value) in (int, float) and math.isfinite(value) for value in times)
              and times == sorted(times) and len(set(times)) == 4, "invalid measurement timing")
        _need(before["timestamp"] <= times[1] and after["timestamp"] >= times[2]
              and times[1] - before["timestamp"] <= max_age_seconds
              and after["timestamp"] - times[2] <= max_age_seconds, "stale telemetry bracket")
        if now is not None:
            _need(0 <= now - times[3] <= max_age_seconds, "stale run evidence")
        before_rows = {(row["kind"], row["scope"], row["subject"]): row for row in before["observations"]}
        after_rows = {(row["kind"], row["scope"], row["subject"]): row for row in after["observations"]}
        present = {row["kind"] for row in before_rows.values() if row["state"] == "observed"}
        present &= {row["kind"] for row in after_rows.values() if row["state"] == "observed"}
        _need((REQUIRED_KINDS - {"offload"}) <= present,
              "required queue/drop/CPU/capability evidence missing")
        for kind in OPTIONAL_ETHTOOL_KINDS:
            first_keys = {key for key in before_rows if key[0] == kind}
            second_keys = {key for key in after_rows if key[0] == kind}
            _need(first_keys and first_keys == second_keys, f"{kind} capability evidence missing")
            _need(all(before_rows[key]["state"] == after_rows[key]["state"]
                      and before_rows[key]["state"] in {"observed", "unsupported"}
                      for key in first_keys), f"{kind} capability evidence missing")
            if kind != "driver_counters":
                _need(all(before_rows[key]["value"] == after_rows[key]["value"] for key in first_keys
                          if before_rows[key]["state"] == "observed"),
                      f"{kind} readback changed during run")
        queue_rows = [row for row in before_rows.values() if row["kind"] == "queue_inventory" and row["state"] == "observed"]
        queue_rows += [row for row in after_rows.values() if row["kind"] == "queue_inventory" and row["state"] == "observed"]
        channel_rows = [row for row in before_rows.values() if row["kind"] == "queue_capability"]
        channel_rows += [row for row in after_rows.values() if row["kind"] == "queue_capability"]
        qemu_rows = [row for row in before_rows.values() if row["kind"] == "qemu" and row["state"] == "observed"]
        qemu_rows += [row for row in after_rows.values() if row["kind"] == "qemu" and row["state"] == "observed"]
        _need(queue_rows and qemu_rows and channel_rows
              and all(row["state"] == "observed" for row in channel_rows),
              "active queue channel readback missing")
        _need(all(row["value"]["sections"].get("Current hardware settings", {}).get("Combined") == queues
                  and row["value"]["sections"].get("Pre-set maximums", {}).get("Combined", -1) >= queues
                  for row in channel_rows), "active queue channel readback mismatch")
        ticks_before = [row for row in before_rows.values() if row["kind"] == "clock_ticks"]
        ticks_after = [row for row in after_rows.values() if row["kind"] == "clock_ticks"]
        _need(ticks_before and ticks_after
              and all(row["state"] == "observed" for row in ticks_before + ticks_after)
              and len({row["value"]["ticksPerSecond"] for row in ticks_before + ticks_after}) == 1,
              "guest CLK_TCK readback missing or changed")
        _need(all(row["value"]["rxCount"] == row["value"]["txCount"] == queues for row in queue_rows)
              and all(row["value"]["frontendQueues"] == row["value"]["backendQueues"] == queues
                      and row["value"]["vm"] == identity["vm"]
                      and row["value"]["interface"] in interfaces for row in qemu_rows),
              "configured/live queue readback mismatch")
        read_back = True
        receiver = run.get("receiverResult")
        _external_result(receiver, "receiver", before["epoch"])
        _need(receiver.get("status") == "complete" and receiver["exitCode"] == 0,
              "receiver result incomplete")
        for field in ("receivedBps", "receivedPps"):
            _number(receiver.get(field), positive=True)
        _need(type(receiver.get("lossPercent")) in (int, float) and 0 <= receiver["lossPercent"] <= 100
              and type(receiver.get("reorderedPackets")) is int and receiver["reorderedPackets"] >= 0
              and isinstance(receiver.get("latencyLabel"), str) and receiver["latencyLabel"]
              and type(receiver.get("p99LatencyMs")) in (int, float) and receiver["p99LatencyMs"] >= 0,
              "receiver counters/latency missing")
        cleanup = run.get("cleanup")
        _external_result(cleanup, "cleanup", before["epoch"])
        _need(cleanup.get("complete") is True and cleanup.get("residuals") == [] and cleanup["exitCode"] == 0,
              "cleanup evidence incomplete")
        observed = True
    except (KeyError, TypeError, TelemetryValidationError) as exc:
        reasons.append(str(exc) or type(exc).__name__)
    complete = configured and read_back and observed and not reasons
    return {"status": "complete" if complete else "incomplete", "configured": configured,
            "read_back": read_back, "observed": observed, "improved": False,
            "claimState": "benchmark_complete" if complete else ("read_back" if read_back else "configured" if configured else "unavailable"),
            "reasons": reasons, "delta": delta}
