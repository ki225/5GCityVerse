"""Pure offline validation and A/B comparison for network-throughput artifacts.

This module performs no I/O and starts no traffic.  It deliberately does not
accept the functional traffic profile.  Configuration, capability readback,
complete benchmark evidence, and a proven improvement are separate states.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from statistics import median
from typing import Mapping, Sequence


PROFILE_ID = "network-throughput"
CLAIM_CONFIGURED = "configured"
CLAIM_READ_BACK = "read_back"
CLAIM_BENCHMARK_COMPLETE = "benchmark_complete"
CLAIM_IMPROVEMENT_PROVEN = "improvement_proven"
RESULT_INVALID = "invalid"
RESULT_NO_IMPROVEMENT = "no_improvement"
RESULT_REGRESSION = "regression"
IMPROVEMENT_THRESHOLD = 0.15


def _exact_int(value: object, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise ValueError(f"{name} must be an exact integer >= {minimum}")
    return value


def _finite(value: object, name: str, *, minimum: float = 0.0) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a nonempty bounded string")
    return value


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ThroughputManifest:
    """Immutable inputs for one queue-count case.

    ``frozen_inputs_hash`` excludes only queue_count. ``manifest_hash`` includes
    it, so an A/B pair has one common frozen hash and two distinct case hashes.
    """

    image: str
    kernel: str
    qemu: str
    vcpus: int
    memory_mib: int
    cni: str
    mtu: int
    offload: tuple[tuple[str, bool], ...]
    workload: str
    flow_count: int
    packet_bytes: int
    offered_rate_bps: int
    duration_seconds: int
    warmup_seconds: int
    window_seconds: int
    queue_count: int
    frozen_inputs_hash: str
    manifest_hash: str
    profile: str = PROFILE_ID
    claim_level: str = CLAIM_CONFIGURED


def build_manifest(*, image: str, kernel: str, qemu: str, vcpus: int,
                   memory_mib: int, cni: str, mtu: int,
                   offload: Mapping[str, bool], workload: str, flow_count: int,
                   packet_bytes: int, offered_rate_bps: int,
                   duration_seconds: int, warmup_seconds: int,
                   window_seconds: int, queue_count: int,
                   profile: str = PROFILE_ID) -> ThroughputManifest:
    """Validate inputs and construct deterministic nonqueue and case hashes."""
    if profile != PROFILE_ID:
        raise ValueError("Only the network-throughput profile is accepted")
    if not isinstance(offload, Mapping) or not offload:
        raise ValueError("offload must be a nonempty exact capability map")
    normalized_offload = []
    for key, enabled in offload.items():
        if type(enabled) is not bool:
            raise ValueError("offload values must be booleans")
        normalized_offload.append((_text(key, "offload key"), enabled))
    normalized_offload.sort()
    if len({key for key, _ in normalized_offload}) != len(normalized_offload):
        raise ValueError("duplicate offload key")

    vcpus = _exact_int(vcpus, "vcpus")
    queue_count = _exact_int(queue_count, "queue_count")
    if queue_count > vcpus:
        raise ValueError("queue_count cannot exceed vcpus")
    frozen = {
        "profile": PROFILE_ID,
        "image": _text(image, "image"),
        "kernel": _text(kernel, "kernel"),
        "qemu": _text(qemu, "qemu"),
        "vcpus": vcpus,
        "memoryMiB": _exact_int(memory_mib, "memory_mib"),
        "cni": _text(cni, "cni"),
        "mtu": _exact_int(mtu, "mtu", minimum=576),
        "offload": dict(normalized_offload),
        "workload": _text(workload, "workload"),
        "flowCount": _exact_int(flow_count, "flow_count"),
        "packetBytes": _exact_int(packet_bytes, "packet_bytes", minimum=64),
        "offeredRateBps": _exact_int(offered_rate_bps, "offered_rate_bps"),
        "durationSeconds": _exact_int(duration_seconds, "duration_seconds"),
        "warmupSeconds": _exact_int(warmup_seconds, "warmup_seconds", minimum=0),
        "windowSeconds": _exact_int(window_seconds, "window_seconds"),
    }
    frozen_hash = _digest(frozen)
    case = {**frozen, "queueCount": queue_count}
    return ThroughputManifest(
        image=frozen["image"], kernel=frozen["kernel"], qemu=frozen["qemu"],
        vcpus=vcpus, memory_mib=frozen["memoryMiB"], cni=frozen["cni"],
        mtu=frozen["mtu"], offload=tuple(normalized_offload),
        workload=frozen["workload"], flow_count=frozen["flowCount"],
        packet_bytes=frozen["packetBytes"],
        offered_rate_bps=frozen["offeredRateBps"],
        duration_seconds=frozen["durationSeconds"],
        warmup_seconds=frozen["warmupSeconds"],
        window_seconds=frozen["windowSeconds"], queue_count=queue_count,
        frozen_inputs_hash=frozen_hash, manifest_hash=_digest(case))


def validate_manifest(manifest: ThroughputManifest) -> None:
    """Recompute both hashes so a replaced field cannot bypass A/B freezing."""
    if not isinstance(manifest, ThroughputManifest):
        raise ValueError("typed manifest is required")
    expected = build_manifest(
        image=manifest.image, kernel=manifest.kernel, qemu=manifest.qemu,
        vcpus=manifest.vcpus, memory_mib=manifest.memory_mib,
        cni=manifest.cni, mtu=manifest.mtu, offload=dict(manifest.offload),
        workload=manifest.workload, flow_count=manifest.flow_count,
        packet_bytes=manifest.packet_bytes,
        offered_rate_bps=manifest.offered_rate_bps,
        duration_seconds=manifest.duration_seconds,
        warmup_seconds=manifest.warmup_seconds,
        window_seconds=manifest.window_seconds,
        queue_count=manifest.queue_count, profile=manifest.profile)
    if manifest != expected:
        raise ValueError("manifest fields or hashes are inconsistent")


@dataclass(frozen=True)
class EvidenceIdentity:
    workload_id: str
    supi: str
    pdu_session_id: str
    ue_ip: str
    tun: str
    receiver_id: str

    def __post_init__(self) -> None:
        for name in ("workload_id", "supi", "pdu_session_id", "ue_ip", "tun", "receiver_id"):
            _text(getattr(self, name), name)


@dataclass(frozen=True)
class ThroughputRun:
    run_id: str
    profile: str
    manifest_hash: str
    frozen_inputs_hash: str
    capability_state: str
    vm_epoch: str
    interface_epoch: str
    identity: EvidenceIdentity
    sender_bps: float
    receiver_goodput_bps: float
    delivered_packets: int
    cpu_seconds: float
    loss_percent: float
    p99_latency_ms: float
    kernel_drops: int
    pdu_healthy: bool
    session_healthy: bool
    cleanup_verified: bool
    residual_resources: tuple[str, ...]
    functional_acceptance_passed: bool
    raw_artifact_refs: tuple[str, ...]

    @property
    def cpu_per_delivered_packet(self) -> float:
        return self.cpu_seconds / self.delivered_packets


def validate_run(run: ThroughputRun, manifest: ThroughputManifest) -> None:
    """Fail closed on malformed, sender-only, foreign, or incomplete evidence."""
    if not isinstance(run, ThroughputRun) or not isinstance(manifest, ThroughputManifest):
        raise ValueError("typed run and manifest are required")
    validate_manifest(manifest)
    if run.profile != PROFILE_ID or manifest.profile != PROFILE_ID:
        raise ValueError("functional or foreign profile artifact rejected")
    if (run.manifest_hash != manifest.manifest_hash
            or run.frozen_inputs_hash != manifest.frozen_inputs_hash):
        raise ValueError("run does not belong to manifest")
    if run.capability_state != CLAIM_READ_BACK:
        raise ValueError("queue capability was not read back")
    for name in ("run_id", "vm_epoch", "interface_epoch"):
        _text(getattr(run, name), name)
    _finite(run.sender_bps, "sender_bps")
    _finite(run.receiver_goodput_bps, "receiver_goodput_bps")
    _exact_int(run.delivered_packets, "delivered_packets")
    _finite(run.cpu_seconds, "cpu_seconds")
    _finite(run.loss_percent, "loss_percent")
    _finite(run.p99_latency_ms, "p99_latency_ms")
    _exact_int(run.kernel_drops, "kernel_drops", minimum=0)
    if run.receiver_goodput_bps <= 0 or run.sender_bps <= 0:
        raise ValueError("positive sender and receiver evidence required")
    if run.cpu_seconds <= 0:
        raise ValueError("positive CPU evidence required")
    if run.receiver_goodput_bps > run.sender_bps:
        raise ValueError("receiver goodput cannot exceed sender rate")
    if run.loss_percent > 100:
        raise ValueError("loss_percent cannot exceed 100")
    if any(type(value) is not bool for value in (
            run.pdu_healthy, run.session_healthy, run.cleanup_verified,
            run.functional_acceptance_passed)):
        raise ValueError("health and cleanup fields must be explicit booleans")
    if not isinstance(run.identity, EvidenceIdentity):
        raise ValueError("receiver identity is required")
    if run.identity.workload_id != manifest.workload:
        raise ValueError("run workload identity differs from manifest")
    if not isinstance(run.residual_resources, tuple):
        raise ValueError("residual_resources must be explicit")
    for residual in run.residual_resources:
        _text(residual, "residual resource")
    if (not isinstance(run.raw_artifact_refs, tuple) or not run.raw_artifact_refs
            or len(set(run.raw_artifact_refs)) != len(run.raw_artifact_refs)):
        raise ValueError("unique raw receiver artifact references are required")
    for reference in run.raw_artifact_refs:
        _text(reference, "raw artifact reference")


@dataclass(frozen=True)
class MetricMedians:
    sender_bps: float
    receiver_goodput_bps: float
    cpu_per_delivered_packet: float
    loss_percent: float
    p99_latency_ms: float
    kernel_drops: float


@dataclass(frozen=True)
class MetricDeltas:
    receiver_goodput_ratio: float
    cpu_per_delivered_packet_reduction: float
    loss_percent: float
    p99_latency_ms: float
    kernel_drops: float


@dataclass(frozen=True)
class ThroughputComparison:
    status: str
    claim_level: str
    improvement_proven: bool
    frozen_inputs_hash: str | None
    baseline_manifest_hash: str | None
    candidate_manifest_hash: str | None
    baseline_samples: int
    candidate_samples: int
    baseline_medians: MetricMedians | None
    candidate_medians: MetricMedians | None
    deltas: MetricDeltas | None
    raw_artifact_refs: tuple[str, ...]
    errors: tuple[str, ...]


def _medians(runs: Sequence[ThroughputRun]) -> MetricMedians:
    return MetricMedians(
        median(run.sender_bps for run in runs),
        median(run.receiver_goodput_bps for run in runs),
        median(run.cpu_per_delivered_packet for run in runs),
        median(run.loss_percent for run in runs),
        median(run.p99_latency_ms for run in runs),
        median(run.kernel_drops for run in runs))


def _invalid(errors: Sequence[str], refs: Sequence[str] = ()) -> ThroughputComparison:
    return ThroughputComparison(
        RESULT_INVALID, CLAIM_CONFIGURED, False, None, None, None, 0, 0,
        None, None, None, tuple(refs), tuple(errors))


def compare_ab(baseline_manifest: ThroughputManifest,
               candidate_manifest: ThroughputManifest,
               baseline_runs: Sequence[ThroughputRun],
               candidate_runs: Sequence[ThroughputRun]) -> ThroughputComparison:
    """Compare complete 3+3 evidence while changing only queue count.

    VM/interface epochs must be internally consistent within each case because a
    queue-count change may require VM recreation. Stable workload/PDU/receiver
    identity is required across both cases.
    """
    errors: list[str] = []
    all_runs = tuple(baseline_runs) + tuple(candidate_runs)
    refs = tuple(
        ref for run in all_runs if isinstance(run, ThroughputRun)
        and isinstance(run.raw_artifact_refs, tuple)
        for ref in run.raw_artifact_refs if isinstance(ref, str))
    if not isinstance(baseline_manifest, ThroughputManifest) or not isinstance(candidate_manifest, ThroughputManifest):
        return _invalid(("typed_manifests_required",), refs)
    try:
        validate_manifest(baseline_manifest)
        validate_manifest(candidate_manifest)
    except ValueError as exc:
        return _invalid((f"invalid_manifest:{exc}",), refs)
    if baseline_manifest.profile != PROFILE_ID or candidate_manifest.profile != PROFILE_ID:
        return _invalid(("functional_or_foreign_profile_rejected",), refs)
    if baseline_manifest.frozen_inputs_hash != candidate_manifest.frozen_inputs_hash:
        errors.append("nonqueue_inputs_differ")
    if baseline_manifest.queue_count == candidate_manifest.queue_count:
        errors.append("queue_count_did_not_change")
    if len(baseline_runs) < 3 or len(candidate_runs) < 3:
        errors.append("at_least_three_complete_runs_per_case_required")
    if len({run.run_id for run in all_runs if isinstance(run, ThroughputRun)}) != len(all_runs):
        errors.append("duplicate_run_id")
    if len(set(refs)) != len(refs):
        errors.append("duplicate_raw_artifact_reference")
    for label, manifest, runs in (
            ("baseline", baseline_manifest, baseline_runs),
            ("candidate", candidate_manifest, candidate_runs)):
        for index, run in enumerate(runs):
            try:
                validate_run(run, manifest)
            except (TypeError, ValueError, AttributeError) as exc:
                errors.append(f"{label}[{index}]:{exc}")
        valid_runs = [run for run in runs if isinstance(run, ThroughputRun)]
        if len({run.vm_epoch for run in valid_runs}) > 1 or len({run.interface_epoch for run in valid_runs}) > 1:
            errors.append(f"{label}_mixed_epoch")
    typed_runs = [run for run in all_runs if isinstance(run, ThroughputRun)]
    if len({run.identity for run in typed_runs}) > 1:
        errors.append("mixed_workload_or_session_identity")
    if errors:
        return _invalid(errors, refs)

    base = _medians(baseline_runs)
    candidate = _medians(candidate_runs)
    goodput_ratio = candidate.receiver_goodput_bps / base.receiver_goodput_bps - 1
    cpu_reduction = 1 - candidate.cpu_per_delivered_packet / base.cpu_per_delivered_packet
    deltas = MetricDeltas(
        goodput_ratio, cpu_reduction,
        candidate.loss_percent - base.loss_percent,
        candidate.p99_latency_ms - base.p99_latency_ms,
        candidate.kernel_drops - base.kernel_drops)

    baseline_gate_failed = any(
        not run.pdu_healthy or not run.session_healthy or not run.cleanup_verified
        or run.residual_resources or not run.functional_acceptance_passed
        for run in baseline_runs)
    candidate_gate_failed = any(
        not run.pdu_healthy or not run.session_healthy or not run.cleanup_verified
        or run.residual_resources or not run.functional_acceptance_passed
        for run in candidate_runs)
    if baseline_gate_failed:
        return ThroughputComparison(
            RESULT_INVALID, CLAIM_BENCHMARK_COMPLETE, False,
            baseline_manifest.frozen_inputs_hash, baseline_manifest.manifest_hash,
            candidate_manifest.manifest_hash, len(baseline_runs), len(candidate_runs),
            base, candidate, deltas, refs, ("unhealthy_baseline_gate",))

    lower_is_worse = (candidate.loss_percent > base.loss_percent
                      or candidate.p99_latency_ms > base.p99_latency_ms
                      or candidate.kernel_drops > base.kernel_drops)
    primary_regression = (candidate.receiver_goodput_bps < base.receiver_goodput_bps
                          or candidate.cpu_per_delivered_packet > base.cpu_per_delivered_packet)
    regression = candidate_gate_failed or lower_is_worse or primary_regression
    goodput_has_headroom = (
        base.receiver_goodput_bps < baseline_manifest.offered_rate_bps
        and candidate.receiver_goodput_bps < candidate_manifest.offered_rate_bps)
    equivalent_goodput = abs(goodput_ratio) <= 0.05
    threshold_met = (
        goodput_has_headroom and (goodput_ratio >= IMPROVEMENT_THRESHOLD
        or math.isclose(goodput_ratio, IMPROVEMENT_THRESHOLD,
                        rel_tol=1e-12, abs_tol=1e-15))
        or equivalent_goodput and (cpu_reduction >= IMPROVEMENT_THRESHOLD
        or math.isclose(cpu_reduction, IMPROVEMENT_THRESHOLD,
                        rel_tol=1e-12, abs_tol=1e-15)))
    if regression:
        status, claim, proven = RESULT_REGRESSION, CLAIM_BENCHMARK_COMPLETE, False
    elif threshold_met:
        status = claim = CLAIM_IMPROVEMENT_PROVEN
        proven = True
    else:
        status, claim, proven = RESULT_NO_IMPROVEMENT, CLAIM_BENCHMARK_COMPLETE, False
    return ThroughputComparison(
        status, claim, proven, baseline_manifest.frozen_inputs_hash,
        baseline_manifest.manifest_hash, candidate_manifest.manifest_hash,
        len(baseline_runs), len(candidate_runs), base, candidate, deltas, refs, ())

