#!/usr/bin/env python3
"""Read-only assembler for complete network-throughput A/B evidence.

The CLI reads immutable JSON artifacts and writes only its JSON verdict to
stdout.  It never executes traffic or fills absent evidence with defaults.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import sys
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.evidence.network_telemetry import validate_run_evidence
from backend.onprem.evidence.throughput import (
    CLAIM_READ_BACK, EvidenceIdentity, PROFILE_ID, ThroughputManifest,
    ThroughputRun, build_manifest, compare_ab,
)


MANIFEST_SCHEMA = "cityverse.network-throughput-manifest/v1"
FUNCTIONAL_SCHEMA = "cityverse.functional-traffic-acceptance/v1"


class AssemblyError(ValueError):
    pass


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise AssemblyError(message)


def _number(value: object, name: str, *, positive: bool = False) -> float:
    _need(type(value) in (int, float) and math.isfinite(value), f"{name} missing")
    value = float(value)
    _need(value > 0 if positive else value >= 0, f"{name} invalid")
    return value


def load_manifest(raw: Mapping[str, object]) -> ThroughputManifest:
    _need(isinstance(raw, Mapping) and raw.get("schemaVersion") == MANIFEST_SCHEMA,
          "manifest schema mismatch")
    fields = raw.get("inputs")
    _need(isinstance(fields, Mapping), "manifest inputs missing")
    try:
        manifest = build_manifest(
            image=fields["image"], kernel=fields["kernel"], qemu=fields["qemu"],
            vcpus=fields["vcpus"], memory_mib=fields["memoryMiB"], cni=fields["cni"],
            mtu=fields["mtu"], offload=fields["offload"], workload=fields["workload"],
            flow_count=fields["flowCount"], packet_bytes=fields["packetBytes"],
            offered_rate_bps=fields["offeredRateBps"],
            duration_seconds=fields["durationSeconds"], warmup_seconds=fields["warmupSeconds"],
            window_seconds=fields["windowSeconds"], queue_count=fields["queueCount"],
            profile=fields.get("profile", PROFILE_ID))
    except (KeyError, TypeError, ValueError) as exc:
        raise AssemblyError(f"invalid manifest inputs: {exc}") from exc
    _need(raw.get("manifestHash") == manifest.manifest_hash
          and raw.get("frozenInputsHash") == manifest.frozen_inputs_hash,
          "immutable manifest hash mismatch")
    return manifest


def _identity(benchmark: Mapping[str, object], telemetry: Mapping[str, object],
              functional: Mapping[str, object], manifest: ThroughputManifest) -> EvidenceIdentity:
    source = benchmark.get("identity")
    _need(isinstance(source, Mapping), "benchmark identity missing")
    required = ("workloadId", "supi", "pduSessionId", "ueIp", "tun", "receiverId",
                "vmEpoch", "interfaceEpoch")
    _need(all(isinstance(source.get(key), str) and source[key] for key in required),
          "benchmark identity incomplete")
    _need(source["workloadId"] == manifest.workload, "workload identity mismatch")
    tele = telemetry.get("identity")
    _need(isinstance(tele, Mapping)
          and tele.get("pduSessionId") == source["pduSessionId"]
          and tele.get("vmEpoch") == source["vmEpoch"]
          and tele.get("interfaceEpoch") == source["interfaceEpoch"],
          "telemetry identity or epoch mismatch")
    _need(functional.get("identity") == dict(source), "functional identity mismatch")
    return EvidenceIdentity(source["workloadId"], source["supi"], source["pduSessionId"],
                            source["ueIp"], source["tun"], source["receiverId"])


def _cpu_and_drops(delta: Mapping[str, object]) -> tuple[float, int]:
    rows = delta.get("observations")
    _need(isinstance(rows, list), "telemetry delta observations missing")
    observed = [row for row in rows if row.get("state") == "observed"
                and isinstance(row.get("value"), Mapping)]
    cgroups = [row for row in observed if row.get("kind") == "cgroup_cpu"]
    per_cpu = [row for row in observed if row.get("kind") == "per_cpu"]
    clock = [row for row in observed if row.get("kind") == "clock_ticks"]
    _need(len(cgroups) == len(per_cpu) == len(clock) == 1,
          "unique cgroup, per-CPU, and CLK_TCK evidence required")
    usage = cgroups[0]["value"].get("usage_usec")
    _number(usage, "UPF cgroup usage delta", positive=True)
    clock_value = clock[0]["value"]
    _need(clock_value.get("changed") is False
          and clock_value.get("before") == clock_value.get("after"),
          "guest CLK_TCK changed")
    ticks = clock_value.get("before", {}).get("ticksPerSecond")
    _need(type(ticks) is int and ticks > 0, "guest CLK_TCK missing")
    cpu_rows = {}
    for key, value in per_cpu[0]["value"].items():
        match = re.fullmatch(r"rows\.(cpu[0-9]+)\[([0-9]+)\]", key)
        if match:
            _need(type(value) is int and value >= 0, "invalid per-CPU tick delta")
            cpu_rows.setdefault(match[1], {})[int(match[2])] = value
    _need(cpu_rows and all(set(values) >= set(range(8)) for values in cpu_rows.values()),
          "per-CPU counter shape incomplete")
    # Linux /proc/stat active execution fields: user,nice,system,irq,softirq.
    # Exclude idle/iowait/steal and guest fields (already included in user/nice).
    active_ticks = sum(values[index] for values in cpu_rows.values()
                       for index in (0, 1, 2, 5, 6))
    _need(active_ticks > 0, "whole guest active CPU tick delta missing")
    cpu_seconds = active_ticks / ticks

    total = 0
    required_sources = set()
    for row in observed:
        kind = row.get("kind")
        for key, value in row["value"].items():
            if type(value) not in (int, float):
                continue
            leaf = key.rsplit(".", 1)[-1].lower()
            include = ((kind == "softnet" and leaf == "dropped")
                       or (kind == "udp" and leaf in {"rcvbuferrors", "sndbuferrors", "inerrors"})
                       or (kind == "link" and leaf == "dropped"))
            if include:
                _need(value >= 0 and float(value).is_integer(), "invalid drop delta")
                total += int(value)
                if kind in {"softnet", "udp", "link"}:
                    required_sources.add(kind)
    _need(required_sources == {"softnet", "udp", "link"},
          "softnet, UDP, and link drop deltas required")
    return cpu_seconds, total


def validate_benchmark_artifact(manifest: ThroughputManifest,
                                benchmark: Mapping[str, object]) -> dict[str, object]:
    _need(benchmark.get("profile") == PROFILE_ID and benchmark.get("status") == "complete"
          and benchmark.get("benchmarkComplete") is True, "benchmark result incomplete")
    run_id = benchmark.get("runId")
    _need(isinstance(run_id, str) and run_id, "benchmark runId missing")
    _need(benchmark.get("manifestHash") == manifest.manifest_hash
          and benchmark.get("frozenInputsHash") == manifest.frozen_inputs_hash,
          "benchmark manifest mismatch")
    identity = benchmark.get("identity")
    required = ("workloadId", "supi", "pduSessionId", "ueIp", "tun", "receiverId",
                "vmEpoch", "interfaceEpoch")
    _need(isinstance(identity, Mapping)
          and all(isinstance(identity.get(key), str) and identity[key] for key in required),
          "benchmark identity incomplete")
    _need(identity["workloadId"] == manifest.workload, "benchmark workload mismatch")
    started = _number(benchmark.get("startedAt"), "benchmark startedAt", positive=True)
    finished = _number(benchmark.get("finishedAt"), "benchmark finishedAt", positive=True)
    _need(started < finished, "benchmark load window invalid")
    _need(benchmark.get("metricSources") == {
        "offeredBps": "requested-rate", "senderBps": "native-iperf3-sender",
        "senderPps": "native-iperf3-sender",
        "receiverGoodputBps": "native-iperf3-receiver",
        "receiverPps": "native-iperf3-receiver"}, "metric provenance mismatch")
    _need(benchmark.get("offeredBps") == manifest.offered_rate_bps,
          "requested offered rate differs from manifest")
    for field in ("senderBps", "senderPps", "receiverGoodputBps", "receiverPps"):
        _number(benchmark.get(field), field, positive=True)
    _need(benchmark.get("cleanupVerified") is True
          and benchmark.get("residualResources") == [], "benchmark cleanup incomplete")
    refs = benchmark.get("rawArtifactRefs")
    _need(isinstance(refs, list) and refs and len(refs) == len(set(refs)),
          "immutable raw artifact references missing")
    pattern = re.compile(re.escape(run_id) + r"/[A-Za-z0-9_.-]+#sha256:[0-9a-f]{64}")
    _need(all(isinstance(ref, str) and pattern.fullmatch(ref) for ref in refs),
          "raw artifact reference identity/hash mismatch")
    _need(benchmark.get("cleanupArtifactRef") in refs
          and "/cleanup.raw.json#sha256:" in benchmark["cleanupArtifactRef"],
          "raw cleanup receipt reference missing")
    return {"runId": run_id, "identity": dict(identity), "startedAt": started,
            "finishedAt": finished, "rawArtifactRefs": list(refs)}


def assemble_run(manifest: ThroughputManifest, benchmark: Mapping[str, object],
                 telemetry: Mapping[str, object], functional: Mapping[str, object]) -> ThroughputRun:
    contract = validate_benchmark_artifact(manifest, benchmark)
    run_id = contract["runId"]
    _need(isinstance(run_id, str) and run_id and telemetry.get("runId") == run_id,
          "runId mismatch")
    telemetry_state = validate_run_evidence(telemetry)
    _need(telemetry_state["status"] == "complete" and telemetry_state["read_back"] is True,
          "telemetry evidence incomplete: " + ";".join(telemetry_state["reasons"]))
    _need(telemetry.get("configuration", {}).get("requestedQueues") == manifest.queue_count,
          "telemetry queue count differs from manifest")
    identity = _identity(benchmark, telemetry, functional, manifest)

    timing = telemetry.get("timing")
    started = _number(benchmark.get("startedAt"), "benchmark startedAt", positive=True)
    finished = _number(benchmark.get("finishedAt"), "benchmark finishedAt", positive=True)
    _need(started < finished and timing["measurementStartedAt"] <= started
          and finished <= timing["measurementFinishedAt"], "benchmark/telemetry timing mismatch")
    receiver = telemetry.get("receiverResult")
    latency = benchmark.get("latency")
    _need(isinstance(receiver, Mapping) and isinstance(latency, Mapping)
          and latency.get("status") == "available"
          and latency.get("source") == "icmp-rtt-over-ue-tun"
          and latency.get("overlapsLoadWindow") is True,
          "independent p99 latency evidence incomplete")
    receiver_bps = _number(benchmark.get("receiverGoodputBps"), "receiver goodput", positive=True)
    p99 = _number(latency.get("p99Ms"), "p99 latency")
    _need(receiver.get("receivedBps") == receiver_bps
          and receiver.get("receivedPps") == benchmark.get("pps")
          and receiver.get("lossPercent") == benchmark.get("lossPercent")
          and receiver.get("reorderedPackets") == benchmark.get("reorderedPackets")
          and receiver.get("latencyLabel") == "icmp-rtt-over-ue-tun"
          and receiver.get("p99LatencyMs") == p99,
          "receiver result cross-check mismatch")
    _need(receiver.get("receiverId") == benchmark["identity"]["receiverId"],
          "receiver identity mismatch")

    _need(functional.get("schemaVersion") == FUNCTIONAL_SCHEMA
          and functional.get("benchmarkRunId") == run_id
          and functional.get("profile") == "functional-traffic"
          and functional.get("accepted") is True
          and functional.get("pduHealthy") is True
          and functional.get("sessionHealthy") is True
          and functional.get("cleanupVerified") is True
          and functional.get("residualResources") == [], "functional acceptance incomplete")
    settings = functional.get("settings")
    _need(settings == {"rateMbps": 10, "packetBytes": 1200, "flowCount": 1,
                       "gso": False}, "functional 10 Mbps/no-GSO contract mismatch")
    _need(_number(functional.get("finishedAt"), "functional finishedAt", positive=True) >= finished,
          "functional acceptance predates benchmark")
    cpu_seconds, kernel_drops = _cpu_and_drops(telemetry_state["delta"])
    raw_refs = contract["rawArtifactRefs"]
    residuals = benchmark.get("residualResources")
    _need(benchmark.get("cleanupVerified") is True and residuals == [],
          "benchmark cleanup incomplete")
    delivered = benchmark.get("deliveredPackets")
    _need(type(delivered) is int and delivered > 0, "delivered packet count invalid")
    return ThroughputRun(
        run_id=run_id, profile=PROFILE_ID, manifest_hash=manifest.manifest_hash,
        frozen_inputs_hash=manifest.frozen_inputs_hash, capability_state=CLAIM_READ_BACK,
        vm_epoch=benchmark["identity"]["vmEpoch"],
        interface_epoch=benchmark["identity"]["interfaceEpoch"], identity=identity,
        sender_bps=_number(benchmark.get("senderBps"), "sender rate", positive=True),
        receiver_goodput_bps=receiver_bps,
        delivered_packets=delivered,
        cpu_seconds=cpu_seconds, loss_percent=_number(benchmark.get("lossPercent"), "loss percent"),
        p99_latency_ms=p99, kernel_drops=kernel_drops,
        pdu_healthy=True, session_healthy=True, cleanup_verified=True,
        residual_resources=(), functional_acceptance_passed=True,
        raw_artifact_refs=tuple(raw_refs))


def assemble_ab(raw: Mapping[str, object]) -> dict[str, object]:
    errors = []
    manifests = {}
    runs = {"baseline": [], "candidate": []}
    for case in ("baseline", "candidate"):
        entry = raw.get(case)
        try:
            _need(isinstance(entry, Mapping), f"{case} case missing")
            manifests[case] = load_manifest(entry.get("manifest"))
            samples = entry.get("runs")
            _need(isinstance(samples, list) and len(samples) >= 3,
                  f"{case} requires at least three runs")
            for index, sample in enumerate(samples):
                try:
                    runs[case].append(assemble_run(manifests[case], sample["benchmark"],
                                                   sample["telemetry"], sample["functional"]))
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(f"{case}[{index}]:{exc}")
        except (TypeError, ValueError) as exc:
            errors.append(f"{case}:{exc}")
    if errors:
        return {"status": "incomplete", "claimLevel": "configured",
                "improvementProven": False, "errors": errors}
    result = compare_ab(manifests["baseline"], manifests["candidate"],
                        runs["baseline"], runs["candidate"])
    return {"status": result.status, "claimLevel": result.claim_level,
            "improvementProven": result.improvement_proven,
            "comparison": asdict(result), "errors": list(result.errors),
            "metricDefinitions": {
                "cpuSeconds": "whole-UPF-guest busy CPU seconds from /proc/stat active ticks / CLK_TCK; includes guest noise",
                "kernelDrops": "fixed softnet+UDP+link drop-signal aggregate; not unique dropped packets; driver counters excluded"}}


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _expand(raw: Mapping[str, object], base: Path) -> dict[str, object]:
    result = {}
    for case in ("baseline", "candidate"):
        entry = raw[case]
        result[case] = {"manifest": _load(base / entry["manifest"]), "runs": [
            {name: _load(base / sample[name]) for name in ("benchmark", "telemetry", "functional")}
            for sample in entry["runs"]]}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = _load(args.spec)
        result = assemble_ab(_expand(spec, args.spec.parent))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "incomplete", "claimLevel": "unavailable",
                  "improvementProven": False, "errors": [str(exc)]}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] != "incomplete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
