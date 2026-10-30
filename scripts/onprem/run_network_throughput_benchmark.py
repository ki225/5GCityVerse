#!/usr/bin/env python3
"""Run one isolated, bounded UE/TUN to N6 UDP throughput sample.

Dry-run is the default and performs no cluster I/O.  ``--execute`` requires an
operator supplied identity receipt and live identity readback before traffic is
started.  Every attempted execution gets a new, exclusively-created artifact
directory; failures retain the raw evidence collected up to that point.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Mapping
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.evidence.throughput import build_manifest


PROFILE = "network-throughput"
MANIFEST_SCHEMA = "cityverse.network-throughput-manifest/v1"
RESULT_SCHEMA = "cityverse.network-throughput-benchmark-result/v1"
IDENTITY_SCHEMA = "cityverse.native-pdu-identity/v1"
IDENTITY_SOURCE = "native-ue-smf-upf-readback"
IDENTITY_MAX_AGE_SECONDS = 30
OWNER = "onprem-network-throughput-benchmark"
NAMESPACE = "free5gc-onprem"
TRAFFIC_IMAGE = "networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f"
DEFAULT_RATE_MBPS = 100
DEFAULT_DURATION_SECONDS = 30
DEFAULT_FLOW_COUNT = 1
DEFAULT_PACKET_BYTES = 1200
ENDPOINTS = {"embb": "172.22.61.20", "urllc": "172.22.62.20",
             "mmtc": "172.22.63.20", "v2x": "172.22.64.20"}


class BenchmarkBlocked(RuntimeError):
    pass


def _write_new(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _artifact_ref(run_id: str, path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{run_id}/{path.name}#sha256:{digest}"


def _json(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BenchmarkBlocked(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise BenchmarkBlocked(f"invalid {label} JSON object")
    return value


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise BenchmarkBlocked(f"invalid {label}")
    value = float(value)
    if value < 0 or (positive and value <= 0):
        raise BenchmarkBlocked(f"invalid {label}")
    return value


def _receiver(report: dict) -> dict:
    if report.get("error"):
        raise BenchmarkBlocked("receiver reported an error")
    row = report.get("end", {}).get("sum_received") or report.get("end", {}).get("sum")
    if not isinstance(row, dict) or row.get("sender") is not False:
        raise BenchmarkBlocked("receiver summary unavailable")
    for key in ("bits_per_second", "seconds", "bytes", "packets", "lost_packets"):
        _number(row.get(key), f"receiver {key}", positive=key in {"bits_per_second", "seconds", "bytes", "packets"})
    _number(row.get("out_of_order"), "receiver out_of_order")
    if type(row.get("out_of_order")) is not int:
        raise BenchmarkBlocked("invalid receiver out_of_order")
    if row["lost_packets"] > row["packets"]:
        raise BenchmarkBlocked("receiver loss exceeds packet count")
    return row


def _sender(report: dict) -> dict:
    if report.get("error"):
        raise BenchmarkBlocked("sender reported an error")
    row = report.get("end", {}).get("sum_sent")
    if not isinstance(row, dict) or row.get("sender") is not True:
        raise BenchmarkBlocked("sender summary unavailable")
    for key in ("bits_per_second", "seconds", "bytes", "packets"):
        _number(row.get(key), f"sender {key}", positive=True)
    return row


@dataclass(frozen=True)
class Request:
    context: str
    kubeconfig: str
    namespace: str
    owner: str
    ue_pod: str
    ue_uid: str
    receiver_pod: str
    receiver_uid: str
    supi: str
    pdu_session_id: str
    ue_ip: str
    tun: str
    endpoint: str
    port: int
    rate_mbps: int
    duration_seconds: int
    flow_count: int
    packet_bytes: int
    manifest_file: Path
    evidence_file: Path
    artifact_root: Path


class Kubectl:
    def __init__(self, request: Request, runner=subprocess.run):
        if not request.context or any(token in request.context.lower() for token in ("eks", "arn:", "kind", "minikube")):
            raise BenchmarkBlocked("explicit dedicated kubeadm context required")
        self.prefix = ["kubectl", "--kubeconfig", request.kubeconfig,
                       "--context", request.context, "-n", request.namespace]
        self.runner = runner

    def capture(self, args: list[str], timeout: int = 10) -> dict[str, object]:
        command = self.prefix + args
        try:
            result = self.runner(command, text=True, capture_output=True,
                                 timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return {"timestamp": time.time(), "command": command, "exitCode": 124,
                    "stdout": stdout, "stderr": stderr or "command timed out"}
        return {"timestamp": time.time(), "command": command, "exitCode": result.returncode,
                "stdout": result.stdout or "", "stderr": result.stderr or ""}

    def run(self, args: list[str], timeout: int = 10, *, allow_failure: bool = False) -> str:
        receipt = self.capture(args, timeout)
        if receipt["exitCode"] and not allow_failure:
            raise BenchmarkBlocked("scoped kubectl command failed")
        return str(receipt["stdout"])


def _manifest(req: Request):
    raw = _json(req.manifest_file.read_text(encoding="utf-8"), "manifest")
    if raw.get("schemaVersion") != MANIFEST_SCHEMA or not isinstance(raw.get("inputs"), dict):
        raise BenchmarkBlocked("manifest schema mismatch")
    fields = raw["inputs"]
    try:
        value = build_manifest(
            image=fields["image"], kernel=fields["kernel"], qemu=fields["qemu"],
            vcpus=fields["vcpus"], memory_mib=fields["memoryMiB"], cni=fields["cni"],
            mtu=fields["mtu"], offload=fields["offload"], workload=fields["workload"],
            flow_count=fields["flowCount"], packet_bytes=fields["packetBytes"],
            offered_rate_bps=fields["offeredRateBps"], duration_seconds=fields["durationSeconds"],
            warmup_seconds=fields["warmupSeconds"], window_seconds=fields["windowSeconds"],
            queue_count=fields["queueCount"], profile=fields.get("profile", PROFILE))
    except (KeyError, TypeError, ValueError) as exc:
        raise BenchmarkBlocked(f"invalid manifest: {exc}") from exc
    if (raw.get("manifestHash") != value.manifest_hash
            or raw.get("frozenInputsHash") != value.frozen_inputs_hash):
        raise BenchmarkBlocked("immutable manifest hash mismatch")
    if (value.offered_rate_bps != req.rate_mbps * 1_000_000
            or value.duration_seconds != req.duration_seconds
            or value.flow_count != req.flow_count or value.packet_bytes != req.packet_bytes
            or value.warmup_seconds + value.window_seconds > value.duration_seconds):
        raise BenchmarkBlocked("manifest traffic window differs from request")
    return value


def validate_request(req: Request) -> tuple[dict, object]:
    if req.owner != OWNER or req.namespace != NAMESPACE:
        raise BenchmarkBlocked("owner or namespace is outside benchmark scope")
    if not re.fullmatch(r"cv-ue-g[0-9]+-[0-9]+", req.ue_pod):
        raise BenchmarkBlocked("invalid UE Pod identity")
    if not re.fullmatch(r"n6-[a-z0-9-]+", req.receiver_pod):
        raise BenchmarkBlocked("invalid receiver Pod identity")
    if not re.fullmatch(r"(?:uesimtun|ueTun)[0-9]+", req.tun):
        raise BenchmarkBlocked("invalid UE TUN identity")
    if not re.fullmatch(r"imsi-[0-9]{10,16}", req.supi):
        raise BenchmarkBlocked("invalid SUPI")
    ipaddress.IPv4Address(req.ue_ip)
    ipaddress.IPv4Address(req.endpoint)
    if not 1024 <= req.port <= 65535:
        raise BenchmarkBlocked("port must be between 1024 and 65535")
    if not 1 <= req.rate_mbps <= 10_000 or not 5 <= req.duration_seconds <= 120:
        raise BenchmarkBlocked("rate or duration exceeds bounded profile")
    if req.flow_count != 1 or not 64 <= req.packet_bytes <= 1400:
        raise BenchmarkBlocked("this runner requires exactly one flow and a bounded packet size")
    if not req.pdu_session_id or len(req.pdu_session_id) > 256:
        raise BenchmarkBlocked("invalid PDU session identity")
    manifest = _manifest(req)
    receipt = _json(req.evidence_file.read_text(encoding="utf-8"), "identity receipt")
    expected = {"schemaVersion": IDENTITY_SCHEMA, "source": IDENTITY_SOURCE,
                "manifestHash": manifest.manifest_hash,
                "profile": PROFILE, "owner": req.owner, "namespace": req.namespace,
                "uePod": req.ue_pod, "uePodUid": req.ue_uid,
                "receiverPod": req.receiver_pod, "receiverPodUid": req.receiver_uid,
                "supi": req.supi, "pduSessionId": req.pdu_session_id,
                "ueIp": req.ue_ip, "tun": req.tun, "endpoint": req.endpoint,
                "pduHealthy": True, "sessionHealthy": True}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise BenchmarkBlocked("identity receipt mismatch")
    observed_at = receipt.get("observedAt")
    age = time.time() - observed_at if type(observed_at) in (int, float) else math.inf
    if not math.isfinite(age) or not 0 <= age <= IDENTITY_MAX_AGE_SECONDS:
        raise BenchmarkBlocked("identity receipt is stale or future-dated")
    if (type(receipt.get("generation")) is not int or receipt["generation"] < 1
            or not isinstance(receipt.get("upfPodUid"), str) or not receipt["upfPodUid"]
            or not isinstance(receipt.get("sessionEpoch"), str) or not receipt["sessionEpoch"]
            or not isinstance(receipt.get("vmEpoch"), str) or not receipt["vmEpoch"]
            or not isinstance(receipt.get("interfaceEpoch"), str) or not receipt["interfaceEpoch"]
            or receipt.get("workloadId") != manifest.workload
            or receipt.get("receiverId") != req.receiver_uid):
        raise BenchmarkBlocked("native PDU identity provenance incomplete")
    return receipt, manifest


def _verify_pod(kube: Kubectl, name: str, uid: str, *, ue: bool, req: Request,
                receipt: Mapping[str, object] | None = None) -> dict:
    pod = _json(kube.run(["get", "pod", name, "-o", "json"]), "Pod")
    meta = pod.get("metadata", {})
    if (meta.get("uid") != uid or meta.get("namespace") != req.namespace
            or meta.get("deletionTimestamp")):
        raise BenchmarkBlocked("Pod UID changed or Pod is terminating")
    containers = {row.get("name"): row.get("image") for row in pod.get("spec", {}).get("containers", [])}
    if containers.get("traffic") != TRAFFIC_IMAGE:
        raise BenchmarkBlocked("pinned traffic sidecar unavailable")
    labels = meta.get("labels", {})
    if ue:
        try:
            supis = json.loads(meta.get("annotations", {}).get("cityverse.io/supis", "null"))
        except json.JSONDecodeError as exc:
            raise BenchmarkBlocked("invalid UE ownership annotation") from exc
        if (labels.get("cityverse.io/managed-by") != "onprem-ue-controller" or req.supi not in supis
                or receipt is None
                or labels.get("cityverse.io/generation") != str(receipt.get("generation"))):
            raise BenchmarkBlocked("UE ownership mismatch")
    else:
        slice_name = next((key for key, value in ENDPOINTS.items() if value == req.endpoint), None)
        app = f"n6-{slice_name}" if slice_name else ""
        if (not slice_name or not re.fullmatch(re.escape(app) + r"-[a-z0-9-]+", name)
                or labels.get("app.kubernetes.io/name") != app
                or labels.get("cityverse.io/runtime") != "kubeadm"
                or labels.get("cityverse.io/role") != "upf" or app not in containers):
            raise BenchmarkBlocked("receiver runtime or slice identity mismatch")
        try:
            networks = json.loads(meta.get("annotations", {}).get(
                "k8s.v1.cni.cncf.io/network-status", "null"))
        except json.JSONDecodeError as exc:
            raise BenchmarkBlocked("invalid receiver network-status") from exc
        matches = [row for row in networks if row.get("interface") == "n6"
                   and req.endpoint in [str(ip).split("/", 1)[0] for ip in row.get("ips", [])]] 
        if len(matches) != 1:
            raise BenchmarkBlocked("receiver N6 network-status mismatch")
    return pod


def verify_identity(kube: Kubectl, req: Request, receipt: Mapping[str, object]) -> None:
    _verify_pod(kube, req.ue_pod, req.ue_uid, ue=True, req=req, receipt=receipt)
    _verify_pod(kube, req.receiver_pod, req.receiver_uid, ue=False, req=req)
    addresses = json.loads(kube.run(["exec", req.ue_pod, "-c", "ue", "--",
                                     "ip", "-j", "addr", "show", "dev", req.tun]))
    matches = [info.get("local") for row in addresses if row.get("ifname") == req.tun
               for info in row.get("addr_info", []) if info.get("family") == "inet"]
    if matches != [req.ue_ip]:
        raise BenchmarkBlocked("fresh UE TUN/IP binding unavailable")
    receiver_slice = next(key for key, value in ENDPOINTS.items() if value == req.endpoint)
    receiver_addresses = json.loads(kube.run(["exec", req.receiver_pod, "-c", f"n6-{receiver_slice}", "--",
                                              "ip", "-j", "addr", "show", "dev", "n6"]))
    receiver_matches = [info.get("local") for row in receiver_addresses if row.get("ifname") == "n6"
                        for info in row.get("addr_info", []) if info.get("family") == "inet"]
    if receiver_matches != [req.endpoint]:
        raise BenchmarkBlocked("live receiver N6 address mismatch")
    for pod in (req.ue_pod, req.receiver_pod):
        kube.run(["exec", pod, "-c", "traffic", "--", "iperf3", "--version"])


def _residual(kube: Kubectl, req: Request, epoch: str) -> tuple[list[str], dict[str, object]]:
    check = ("ps -eo args | awk -v p='" + str(req.port) +
             "' '$1==\"iperf3\" {for(i=1;i<=NF;i++) if($i==\"-p\" && $(i+1)==p) found=1} END{if(found) print \"residual\"}'")
    residual = []
    observations = []
    for pod in (req.ue_pod, req.receiver_pod):
        receipt = kube.capture(["exec", pod, "-c", "traffic", "--", "sh", "-c", check], 10)
        output = str(receipt["stdout"])
        observations.append({**receipt, "epoch": epoch, "pod": pod,
                             "podUid": req.ue_uid if pod == req.ue_pod else req.receiver_uid,
                             "port": req.port,
                             "state": ("probe-failed" if receipt["exitCode"] else
                                       "residual" if output.strip() else "absent")})
        if receipt["exitCode"] == 0 and output.strip():
            residual.append(pod)
    probes_complete = len(observations) == 2 and all(row["exitCode"] == 0 for row in observations)
    return residual, {"source": "owned-process-residual-probe/v1", "epoch": epoch,
                      "observations": observations,
                      "complete": probes_complete and not residual,
                      "residualResources": residual if probes_complete else None}


def _icmp_samples(raw: str) -> list[float]:
    samples = [float(value) for value in re.findall(r"time=([0-9]+(?:\.[0-9]+)?)\s*ms", raw)]
    if len(samples) < 5 or any(not math.isfinite(value) or value < 0 for value in samples):
        raise BenchmarkBlocked("insufficient ICMP RTT samples")
    return samples


def _percentile99(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * 0.99) - 1]


def _timed_capture(kube: Kubectl, args: list[str], timeout: int) -> tuple[dict[str, object], float, float]:
    started = time.monotonic()
    receipt = kube.capture(args, timeout)
    return receipt, started, time.monotonic()


def execute(req: Request, *, runner=subprocess.run, sleep=time.sleep,
            run_id: str | None = None) -> tuple[dict, Path]:
    receipt, manifest = validate_request(req)
    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
        raise BenchmarkBlocked("invalid preallocated runId")
    run_dir = req.artifact_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    identity = {"workloadId": receipt["workloadId"], "supi": req.supi,
                "pduSessionId": req.pdu_session_id, "ueIp": req.ue_ip, "tun": req.tun,
                "receiverId": receipt["receiverId"], "vmEpoch": receipt["vmEpoch"],
                "interfaceEpoch": receipt["interfaceEpoch"]}
    plan = {"runId": run_id, "profile": PROFILE, "owner": req.owner,
            "namespace": req.namespace, "rateMbps": req.rate_mbps,
            "durationSeconds": req.duration_seconds, "flowCount": req.flow_count,
            "packetBytes": req.packet_bytes, "manifestHash": manifest.manifest_hash,
            "frozenInputsHash": manifest.frozen_inputs_hash, "identity": identity,
            "latency": {"status": "pending",
            "source": "icmp-rtt-over-ue-tun"}}
    _write_new(run_dir / "plan.json", plan)
    _write_new(run_dir / "manifest.raw.json",
               _json(req.manifest_file.read_text(encoding="utf-8"), "manifest"))
    _write_new(run_dir / "identity.raw.json", receipt)
    kube = Kubectl(req, runner=runner)
    try:
        verify_identity(kube, req, receipt)
        server = ["exec", req.receiver_pod, "-c", "traffic", "--", "timeout", "-k", "2",
                  str(req.duration_seconds + 12), "iperf3", "-s", "-1", "-B", req.endpoint,
                  "-p", str(req.port), "-J"]
        client = ["exec", req.ue_pod, "-c", "traffic", "--", "timeout", "-k", "2",
                  str(req.duration_seconds + 8), "iperf3", "-c", req.endpoint, "-p", str(req.port),
                  "-u", "-b", str(req.rate_mbps * 1_000_000 // req.flow_count),
                  "-t", str(req.duration_seconds),
                  "-l", str(req.packet_bytes), "-P", str(req.flow_count), "-B", req.ue_ip,
                  "--bind-dev", req.tun, "-J", "--get-server-output"]
        ping = ["exec", req.ue_pod, "-c", "traffic", "--", "timeout", "-k", "2",
                str(req.duration_seconds + 4), "ping", "-I", req.tun, "-i", "0.2",
                "-c", str((req.duration_seconds + 2) * 5), "-W", "1", req.endpoint]
        with ThreadPoolExecutor(max_workers=2) as pool:
            server_future = pool.submit(kube.capture, server, req.duration_seconds + 15)
            latency_future = pool.submit(_timed_capture, kube, ping, req.duration_seconds + 7)
            sleep(0.5)
            load_started = time.monotonic()
            load_started_at = time.time()
            client_error = None
            try:
                client_receipt = kube.capture(client, req.duration_seconds + 11)
                client_receipt["epoch"] = receipt["sessionEpoch"]
                _write_new(run_dir / "sender.command.json", client_receipt)
                if client_receipt["exitCode"] != 0:
                    raise BenchmarkBlocked("bounded sender command timed out" if client_receipt["exitCode"] == 124
                                           else "bounded sender command failed")
                client_raw = str(client_receipt["stdout"])
                _write_new(run_dir / "sender.raw.json", _json(client_raw, "sender"))
            except Exception as exc:
                client_error = exc
            load_finished = time.monotonic()
            load_finished_at = time.time()
            server_error = latency_error = None
            try:
                server_receipt = server_future.result(timeout=req.duration_seconds + 16)
                server_receipt["epoch"] = receipt["sessionEpoch"]
                _write_new(run_dir / "receiver.command.json", server_receipt)
                if server_receipt["exitCode"] != 0:
                    raise BenchmarkBlocked("bounded receiver command timed out" if server_receipt["exitCode"] == 124
                                           else "bounded receiver command failed")
                server_raw = str(server_receipt["stdout"])
            except Exception as exc:
                server_error = exc
            try:
                latency_receipt, latency_started, latency_finished = latency_future.result(
                    timeout=req.duration_seconds + 8)
                latency_receipt["epoch"] = receipt["sessionEpoch"]
                _write_new(run_dir / "latency.command.json", latency_receipt)
                if latency_receipt["exitCode"] != 0:
                    raise BenchmarkBlocked("bounded latency command timed out" if latency_receipt["exitCode"] == 124
                                           else "bounded latency command failed")
                latency_raw = str(latency_receipt["stdout"])
                _write_new(run_dir / "latency.raw.txt.json", {"output": latency_raw})
            except Exception as exc:
                latency_error = exc
                latency_raw, latency_started, latency_finished = "", None, None
                _write_new(run_dir / "latency.error.json", {
                    "status": "unavailable", "errorType": type(exc).__name__})
            if client_error is not None:
                raise client_error
            if server_error is not None:
                raise server_error
        _write_new(run_dir / "receiver.raw.json", _json(server_raw, "receiver"))
        sender_report, receiver_report = _json(client_raw, "sender"), _json(server_raw, "receiver")
        sender, receiver = _sender(sender_report), _receiver(receiver_report)
        connected = sender_report.get("start", {}).get("connected", [])
        start = sender_report.get("start", {}).get("test_start", {})
        if (len(connected) != 1 or connected[0].get("local_host") != req.ue_ip
                or connected[0].get("remote_host") != req.endpoint
                or start.get("protocol") != "UDP" or start.get("blksize") != req.packet_bytes):
            raise BenchmarkBlocked("iperf identity or protocol mismatch")
        _verify_pod(kube, req.ue_pod, req.ue_uid, ue=True, req=req, receipt=receipt)
        _verify_pod(kube, req.receiver_pod, req.receiver_uid, ue=False, req=req)
        residual, cleanup_evidence = _residual(kube, req, receipt["sessionEpoch"])
        _write_new(run_dir / "cleanup.raw.json", cleanup_evidence)
        if cleanup_evidence["complete"] is not True and not residual:
            raise BenchmarkBlocked("owned residual probes incomplete")
        delivered = int(receiver["packets"] - receiver["lost_packets"])
        sender_pps = sender["packets"] / sender["seconds"]
        receiver_pps = delivered / receiver["seconds"]
        if not all(math.isfinite(value) and value > 0 for value in (sender_pps, receiver_pps)):
            raise BenchmarkBlocked("invalid measured packet rate")
        latency_reason = None
        try:
            latency_samples = _icmp_samples(latency_raw) if latency_error is None else []
        except BenchmarkBlocked:
            latency_samples = []
            latency_reason = "insufficient_samples"
        if latency_error is not None:
            latency_reason = "collector_failed"
        latency_overlap = bool(latency_samples and latency_started <= load_started
                               and latency_finished >= load_finished)
        if latency_samples and not latency_overlap:
            latency_reason = "overlap_unproven"
        latency = {"status": "available" if latency_overlap else "unavailable",
                   "source": "icmp-rtt-over-ue-tun", "sampleCount": len(latency_samples),
                   "p99Ms": _percentile99(latency_samples) if latency_overlap else None,
                   "overlapsLoadWindow": latency_overlap, "reason": latency_reason}
        complete = not residual and latency_overlap
        if not (run_dir / "latency.command.json").exists():
            raise BenchmarkBlocked("latency command provenance missing")
        raw_names = ["manifest.raw.json", "identity.raw.json", "sender.raw.json",
                     "sender.command.json", "receiver.raw.json", "receiver.command.json",
                     "latency.raw.txt.json" if latency_error is None else "latency.error.json",
                     "latency.command.json",
                     "cleanup.raw.json"]
        raw_refs = [_artifact_ref(run_id, run_dir / name) for name in raw_names]
        cleanup_ref = next(ref for ref in raw_refs if "/cleanup.raw.json#" in ref)
        result = {**plan, "schemaVersion": RESULT_SCHEMA,
                  "status": "complete" if complete else ("residual" if residual else "incomplete"),
                  "benchmarkComplete": complete,
                  "incompleteReasons": [] if complete else (["owned_process_residual"] if residual
                                                               else ["latency_overlap_unproven"]),
                  "latency": latency,
                  "startedAt": load_started_at, "finishedAt": load_finished_at,
                  "senderBps": sender["bits_per_second"],
                  "offeredBps": req.rate_mbps * 1_000_000, "senderPps": sender_pps,
                  "receiverGoodputBps": receiver["bits_per_second"],
                  "receivedPackets": receiver["packets"], "deliveredPackets": delivered,
                  "lostPackets": receiver["lost_packets"],
                  "lossPercent": 100 * receiver["lost_packets"] / receiver["packets"],
                  "reorderedPackets": receiver["out_of_order"],
                  "receiverPps": receiver_pps, "pps": receiver_pps,
                  "cleanupVerified": not residual, "residualResources": residual,
                  "cleanupArtifactRef": cleanup_ref,
                  "source": "native-iperf3-receiver",
                  "metricSources": {"offeredBps": "requested-rate",
                                    "senderBps": "native-iperf3-sender",
                                    "senderPps": "native-iperf3-sender",
                                    "receiverGoodputBps": "native-iperf3-receiver",
                                    "receiverPps": "native-iperf3-receiver"},
                  "rawArtifacts": raw_names, "rawArtifactRefs": raw_refs}
        _write_new(run_dir / "result.json", result)
        if residual:
            raise BenchmarkBlocked("owned benchmark process residual detected")
        return result, run_dir
    except Exception as exc:
        failure = {**plan, "status": "failed", "errorType": type(exc).__name__,
                   "error": str(exc), "rawEvidenceRetained": True}
        _write_new(run_dir / "failure.json", failure)
        raise


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true", help="perform the bounded live sample")
    p.add_argument("--run-id", help="preallocated unique operator runId")
    p.add_argument("--context", default="")
    p.add_argument("--kubeconfig", default="")
    p.add_argument("--namespace", default=NAMESPACE)
    p.add_argument("--owner", default=OWNER)
    for name in ("ue-pod", "ue-uid", "receiver-pod", "receiver-uid", "supi",
                 "pdu-session-id", "ue-ip", "tun", "endpoint"):
        p.add_argument("--" + name, default="")
    p.add_argument("--port", type=int, default=15201)
    p.add_argument("--rate-mbps", type=int, default=DEFAULT_RATE_MBPS)
    p.add_argument("--duration-seconds", type=int, default=DEFAULT_DURATION_SECONDS)
    p.add_argument("--flow-count", type=int, default=DEFAULT_FLOW_COUNT)
    p.add_argument("--packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    p.add_argument("--manifest", type=Path)
    p.add_argument("--identity-evidence", type=Path)
    p.add_argument("--artifact-root", type=Path, default=Path("artifacts/network-throughput"))
    return p


def request_from(args: argparse.Namespace) -> Request:
    if args.identity_evidence is None or args.manifest is None:
        raise BenchmarkBlocked("--manifest and --identity-evidence are required for execution")
    return Request(args.context, args.kubeconfig, args.namespace, args.owner,
                   args.ue_pod, args.ue_uid, args.receiver_pod, args.receiver_uid,
                   args.supi, args.pdu_session_id, args.ue_ip, args.tun, args.endpoint,
                   args.port, args.rate_mbps, args.duration_seconds, args.flow_count,
                   args.packet_bytes, args.manifest, args.identity_evidence, args.artifact_root)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "readOnly": True, "profile": PROFILE,
                          "executeRequiredForTraffic": True, "functionalContractChanged": False,
                          "defaults": {"rateMbps": args.rate_mbps,
                          "durationSeconds": args.duration_seconds,
                          "flowCount": args.flow_count, "packetBytes": args.packet_bytes}},
                         sort_keys=True))
        return 0
    try:
        result, run_dir = execute(request_from(args), run_id=args.run_id)
    except (BenchmarkBlocked, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "runId": result["runId"],
                      "artifactDirectory": str(run_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
