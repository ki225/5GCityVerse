"""Default-off native batch provider with durable exact-token recovery.

No caller-supplied shell or evidence. Public status identities originate in the
native collector; remote worker execution begins only after the round journal
contains the full immutable job set. Enabling this does not raise admission.
"""
from backend.onprem.storage.current_state import read_current, read_cleanup, mutate_current
from backend.onprem.traffic.planning import rate_budget
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import asdict
from functools import wraps
import hashlib
import json
import logging
import math
import re
import sqlite3
import subprocess
from threading import Event, Lock
import time
from uuid import uuid4
from backend.onprem.traffic.timing import MeasurementTiming, PhaseTimeline, span, timed

from backend.onprem.traffic.protocol import (BATCH_ERROR_CODES, DETACHED_SCHEMA,
    SHARD_DURATION_SECONDS, SHARD_EVIDENCE_MAX_AGE_SECONDS, SHARD_POST_CLEANUP_MAX_AGE_SECONDS, BatchReleaseBarrier,
    BatchTraffic, LEGACY_PROFILE, SHARD_PROFILE, aggregate_shards, build_jobs, build_shards,
    detached_worker_command, parse_worker_output, reduce_evidence, worker_command, worker_manifest,
    detached_receiver_contract)
from backend.onprem.runtime.kubeadm import (TRAFFIC_CLIENT_MEMORY_LIMIT, TRAFFIC_IMAGE,
                                            TRAFFIC_RECEIVER_MEMORY_LIMIT)
from backend.onprem.runtime.errors import LocalKubernetesError
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.capability import receiver_summary
from backend.onprem.runtime.errors import RoundRuntimeBlocked
from backend.onprem.runtime.round_resources import action, fresh, native_receiver_pods
from backend.onprem.scenarios.contracts import RoundConflict
from backend.onprem.traffic.workload import applied_rates, plan_for_stage, validate_record_workload, protocol_options
from backend.onprem.runtime.ue import EvidenceVerifier, SLICES, seconds
from backend.onprem.traffic.tuning import (
    DEFAULT_TUNING, TUNED_SCHEMA, tuning_manifest, validate_tuning,
    verify_host_socket_tuning, verify_report_tuning)


IDENTITY_FIELDS = ("supi", "generation", "ip", "sessionId", "podUid", "interface", "upf",
                   "smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch")
LEASE_FIELDS = ("supi", "slice", "generation", "podUid", "ip", "sessionId", "interface", "upf",
                "smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch")
DETACHED_RESERVATION_SCHEMA = DETACHED_SCHEMA
DETACHED_STATES = frozenset(("ARMED", "LISTENING", "RUNNING", "SEALED", "CANCELLED", "FAILED"))
DETACHED_RECORD_WIRE_MAX = 66 * 1024
DETACHED_COLLECT_WIRE_MAX = 7 * 1024 * 1024 + 512
DETACHED_ADMISSION_SECONDS = 52
DETACHED_CLIENT_CHILD_SECONDS = 64
DETACHED_SERVER_CHILD_SECONDS = 68
DETACHED_TRANSPORT_SECONDS = 73
DETACHED_LEGACY_DEADLINE_SECONDS = frozenset(((47, 40, 48, 52), (52, 58, 63, 68)))
logger = logging.getLogger(__name__)
_ownership_timing_source = ContextVar('ownership_timing_source', default={})


def _ownership_span(part):
    return span('ownership.' + part, **_ownership_timing_source.get())


def _measurement_context(function):
    """Propagate diagnostics only into an existing executor task."""
    recorder = MeasurementTiming.current()
    if recorder is None:
        return function
    @wraps(function)
    def invoke(*args, **kwargs):
        with recorder.bind():
            return function(*args, **kwargs)
    return invoke


class _DetachedIdentityObservations:
    """Reuse only container-invariant identity within one measurement window.

    Every reuse still performs the full fresh Pod/jobs and round ownership
    fences. This object never caches readiness, admission or command receipts.
    """

    def __init__(self, provider, snapshot, cancelled):
        self.provider, self.cancelled = provider, cancelled
        self.scope = (snapshot.get("roundId"), snapshot.get("generation"))
        self.window = uuid4().hex
        self._rows, self._lock = {}, Lock()

    def observe(self, identity, jobs, snapshot, shard_index):
        role, name, uid = identity
        started = time.monotonic()
        try:
            if ((snapshot.get("roundId"), snapshot.get("generation")) != self.scope
                    or self.cancelled() or not self.provider._owned(snapshot)):
                raise RoundRuntimeBlocked("Detached identity window ownership changed or cancelled")
            namespace = self.provider.resources.controller.driver.namespace
            key = (namespace, role, name, uid)
            with self._lock:
                if any(k[:3] == key[:3] and k[3] != uid for k in self._rows):
                    raise RoundRuntimeBlocked("Detached identity window Pod UID changed")
                cached = deepcopy(self._rows.get(key))
            if cached is None:
                observed = self.provider._detached_identity(name, uid, jobs, snapshot)
            else:
                # _pod validates current namespace/UID/readiness/image and the
                # complete current shard's UE ownership before reusing anything.
                pod = self.provider._pod(name, uid, jobs)
                if self.provider._traffic_epoch(pod) != (cached["containerID"], cached["restartCount"]):
                    raise RoundRuntimeBlocked("Detached identity window container epoch changed")
                observed = cached
            if self.cancelled() or not self.provider._owned(snapshot):
                raise RoundRuntimeBlocked("Detached identity window ownership changed or cancelled")
            if cached is None:
                with self._lock:
                    self._rows[key] = deepcopy(observed)
            return deepcopy(observed)
        except Exception as exc:
            # Fixed metadata only. Exception text/stdout/stderr may contain a
            # private worker program, manifest, token or subscriber identity.
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            logger.warning('Detached identity observation failed window=%s generation=%s '
                           'shard=%s role=%s podUid=%s observedAt=%.6f '
                           'elapsedSeconds=%.3f execTimeoutSeconds=2 errorType=%s',
                           self.window, self.scope[1], shard_index, role, uid, time.time(),
                           time.monotonic() - started, type(cause).__name__)
            raise


class _ReceiverStartupSlots:
    """One bounded server preparation per exact receiver UID in this window."""

    def __init__(self, uids):
        self._slots = {uid: Lock() for uid in uids}

    def run(self, uid, reservation, *, cancelled, owned, dispatch, finish=None):
        slot = self._slots.get(uid)
        if slot is None:
            raise RoundRuntimeBlocked("Detached receiver startup identity is unknown")
        deadlines = [reservation.get(key) for key in
                     ("admissionDeadline", "serverChildDeadline", "transportDeadline")]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in deadlines):
            raise RoundRuntimeBlocked("Detached receiver startup deadlines are invalid")
        deadline = min(deadlines)

        def remaining():
            if cancelled():
                raise RoundRuntimeBlocked("Detached receiver startup cancelled")
            value = deadline - detached_boottime()
            if value <= 0:
                raise RoundRuntimeBlocked("Detached receiver startup admission deadline expired")
            return value

        remaining()
        if not owned():
            raise RoundRuntimeBlocked("Detached receiver startup ownership changed")
        # Waiting grants no dispatch authority. Re-reading SQLite/native fixture
        # archives on each 50 ms wait made every contender delay the current
        # holder. Check authority again only after this waiter owns the slot.
        while not slot.acquire(timeout=min(.05, remaining())):
            pass
        acquired_at = time.monotonic()
        try:
            if not owned():
                raise RoundRuntimeBlocked("Detached receiver startup ownership changed")
            remaining()  # Cancellation/deadlines can change during ownership reads.
            result = dispatch()  # Exactly once; actual exec retains its own <=10s bound.
        finally:
            held_seconds = time.monotonic() - acquired_at
            slot.release()
        # Only socket preparation is serialized. A successful HTTP reply grants
        # no acceptance: the caller still finishes its fresh identity fences.
        if finish is None:
            return result
        finishing_at = time.monotonic()
        try:
            return finish(result)
        finally:
            logger.warning('Detached receiver startup slot timing generation=%s podUid=%s '
                           'heldSeconds=%.6f postflightSeconds=%.6f',
                           reservation.get('generation'), uid, held_seconds,
                           time.monotonic() - finishing_at)


def detached_boottime():
    """Return Linux CLOCK_BOOTTIME or fail closed before detached dispatch."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    getter = getattr(time, "clock_gettime", None)
    if clock is None or getter is None:
        raise RoundRuntimeBlocked("CLOCK_BOOTTIME is unavailable for detached deadlines")
    value = getter(clock)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RoundRuntimeBlocked("CLOCK_BOOTTIME observation is invalid")
    return value


def detached_clock_boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as stream:
            return stream.read(128).strip()
    except OSError:
        raise RoundRuntimeBlocked('Collector Linux boot identity is unavailable') from None


def parse_detached_response(action_name, raw, *, token, epoch=0):
    """Parse the closed detached-control wire protocol; reject free-form output."""
    if (re.fullmatch(r"[a-f0-9]{32}", str(token)) is None or not isinstance(raw, str)
            or not re.fullmatch(r"[\x09\x0a\x20-\x7e]*", raw)):
        raise RoundRuntimeBlocked("Detached control response is invalid")
    token = re.escape(token)
    digest = r"[a-f0-9]{64}"
    pid = r"[1-9][0-9]*"
    boot = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
    netns = r"net:\[[1-9][0-9]*\]"
    if action_name == "capability":
        match = re.fullmatch(rf"CAPABLEv1 ({token}) ({digest})\n?", raw)
        if match:
            return {"token": match[1], "stateDigest": match[2]}
    elif action_name == "start":
        pattern = rf"STARTEDv1 ({token}) ({digest}) ({pid}) ({pid}) ({pid}) ({boot}) ({netns})\n?"
        match = re.fullmatch(pattern, raw)
        if match:
            return {"token": match[1], "stateDigest": match[2], "supervisorPid": int(match[3]),
                    "supervisorStarttime": int(match[4]), "supervisorPgid": int(match[5]),
                    "bootId": match[6], "netns": match[7]}
    elif action_name == "status":
        pattern = rf"STATUSv1 ({token}) ({'|'.join(sorted(DETACHED_STATES))}) ({digest}) ({pid}) ({pid}) ({pid}) ({boot}) ({netns}) (-|[1-9][0-9]*)\n?"
        match = re.fullmatch(pattern, raw)
        if match:
            return {"token": match[1], "state": match[2], "stateDigest": match[3],
                    "supervisorPid": int(match[4]), "supervisorStarttime": int(match[5]),
                    "supervisorPgid": int(match[6]), "bootId": match[7], "netns": match[8],
                    "releaseEpoch": None if match[9] == "-" else int(match[9])}
    elif action_name == "release" and type(epoch) is int and epoch > 0:
        match = re.fullmatch(rf"RELEASEDv1 ({token}) ({digest}) ({epoch})\n?", raw)
        if match:
            return {"token": match[1], "stateDigest": match[2], "releaseEpoch": int(match[3])}
    elif action_name == "cancel":
        match = re.fullmatch(rf"ABSENTv2 ({token}) ({digest}) ({digest})\n?", raw)
        if match:
            return {"token": match[1], "stateDigest": match[2], "closureDigest": match[3]}
    elif action_name == "collect":
        lines = raw.splitlines()
        match = re.fullmatch(rf"SEALEDv1 ({token}) ({digest}) ({digest}) ([1-9][0-9]*)", lines[0] if lines else "")
        batch_output = "\n".join(lines[1:]) + "\n"
        if (match and len(lines) == int(match[4]) + 1
                and all(line.startswith("BATCHv1\t") for line in lines[1:])
                and hashlib.sha256(batch_output.encode()).hexdigest() == match[3]):
            return {"token": match[1], "stateDigest": match[2], "sealDigest": match[3],
                    "count": int(match[4]), "batchOutput": batch_output}
    raise RoundRuntimeBlocked("Detached control response is invalid")


def detached_manifest_sha256(jobs, tuning=None):
    """Digest the exact fixed-profile worker manifest, including row order."""
    manifest = worker_manifest(jobs, profile=SHARD_PROFILE)
    return hashlib.sha256((manifest if tuning is None else tuning_manifest(manifest, tuning)).encode()).hexdigest()


def detached_job_rows(jobs):
    """Canonical executable data; the worker derives fixed iperf argv from it."""
    return [{"key": job.key, "endpoint": job.endpoint, "port": job.port,
             "ueIp": job.ue_ip, "tun": job.tun, "direction": job.direction,
             "offeredKbps": job.offered_kbps, "duration": job.duration,
             "uePod": job.ue_pod, "ueUid": job.ue_uid,
             "receiverPod": job.receiver_pod, "receiverUid": job.receiver_uid}
            for job in jobs]


def validate_detached_reservation(value, *, round_id, generation, shard_index, shard_count, token, jobs):
    """Validate the complete durable identity needed before a detached start.

    Monotonic deadlines are meaningful only inside the recorded Linux boot.
    Recovery must reject the record if any role reports another boot/container
    epoch; it must never recreate these deadlines from current time.
    """
    tuned = isinstance(value, dict) and value.get("schemaVersion") == TUNED_SCHEMA
    if not isinstance(value, dict) or set(value) != {
            "schemaVersion", "profile", "roundId", "generation", "shardIndex", "shardCount", "token",
            "manifestSha256", "jobs", "createdMonotonic", "admissionDeadline", "clientChildDeadline",
            "serverChildDeadline", "transportDeadline", "clockSource", "clockBootId", "roles"} | ({"trafficTuning"} if tuned else set()):
        return False
    try:
        tuning = validate_tuning(value["trafficTuning"]) if tuned else None
    except PolicyBlocked:
        return False
    if (value.get("schemaVersion") not in {DETACHED_RESERVATION_SCHEMA, TUNED_SCHEMA}
            or value.get("profile") != SHARD_PROFILE or value.get("roundId") != round_id
            or value.get("generation") != generation or value.get("shardIndex") != shard_index
            or value.get("shardCount") != shard_count or type(shard_count) is not int
            or not 2 <= shard_count <= 10 or not 0 <= shard_index < shard_count
            or value.get("token") != token or value.get("manifestSha256") != detached_manifest_sha256(jobs, tuning)
            or value.get("jobs") != detached_job_rows(jobs) or value.get("clockSource") != "CLOCK_BOOTTIME"):
        return False
    times = [value.get(k) for k in ("createdMonotonic", "admissionDeadline", "clientChildDeadline",
                                    "serverChildDeadline", "transportDeadline")]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in times):
        return False
    origin, admission, client_child, server_child, transport = times
    # Reproduce the builder's addition: subtracting across a binary exponent
    # boundary can round a valid 52-second offset to 52.000000000000114.
    # Exact forward comparison rejects even one-ULP deadline alterations.
    allowed_offsets = DETACHED_LEGACY_DEADLINE_SECONDS | {
        (DETACHED_ADMISSION_SECONDS, DETACHED_CLIENT_CHILD_SECONDS,
         DETACHED_SERVER_CHILD_SECONDS, DETACHED_TRANSPORT_SECONDS)}
    if not any((admission, client_child, server_child, transport)
               == tuple(origin + offset for offset in offsets)
               for offsets in allowed_offsets):
        return False
    roles = value.get("roles")
    expected = {("server", j.receiver_pod, j.receiver_uid) for j in jobs} | {
        ("client", j.ue_pod, j.ue_uid) for j in jobs}
    if not isinstance(roles, list) or len(roles) != len(expected):
        return False
    seen = set()
    for row in roles:
        if not isinstance(row, dict) or set(row) != {"role", "namespace", "pod", "podUid",
                                                    "containerID", "restartCount", "bootId", "netns"}:
            return False
        identity = (row.get("role"), row.get("pod"), row.get("podUid"))
        if (identity not in expected or identity in seen
                or not isinstance(row.get("namespace"), str) or not row["namespace"]
                or not isinstance(row.get("containerID"), str)
                or not re.fullmatch(r"(?:containerd|docker|cri-o)://[A-Za-z0-9_.:-]{8,256}", row["containerID"])
                or type(row.get("restartCount")) is not int or row["restartCount"] < 0
                or not isinstance(row.get("bootId"), str)
                or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", row["bootId"])
                or not isinstance(row.get("netns"), str)
                or not re.fullmatch(r"net:\[[1-9][0-9]*\]", row["netns"])):
            return False
        seen.add(identity)
    boot_ids = {row["bootId"] for row in roles}
    return (seen == expected and len(boot_ids) == 1
            and value.get("clockBootId") == next(iter(boot_ids)))


def status_error_diagnostic(errors):
    """Persist bounded local reason codes, never arbitrary adapter exception text."""
    known = {
        "Last observation expired": "last_observation_expired",
        "Native bootstrap observation expired": "bootstrap_observation_expired",
        "Runtime reconciliation pending after backend restart": "reconciliation_pending",
        "Runtime or identity evidence adapter is unavailable; no completion inferred": "adapter_unavailable",
        "Evidence belongs to another generation": "foreign_generation",
        "Evidence is stale or predates the generation": "stale_or_predating_evidence",
        "Malformed per-UE evidence": "malformed_per_ue_evidence",
        "Malformed identity evidence": "malformed_identity_evidence",
        "Native UPF evidence unavailable or incompatible": "upf_evidence_unavailable",
        "Native UPF dump is stale": "upf_dump_stale",
        "Native source snapshot exceeded freshness deadline": "source_snapshot_deadline",
        "Native evidence snapshot exceeded freshness deadline": "evidence_snapshot_deadline",
        "Generation or Pod UID changed during observation": "generation_or_uid_changed",
        "Native OAM evidence collection failed or exceeded deadline": "oam_collection_deadline",
        "Native OAM collection cancelled or timed out": "oam_collection_cancelled_or_timeout",
        "Native AMF/SMF OAM request failed": "oam_request_failed",
        "Current RAN identity unavailable": "ran_identity_unavailable",
        "SMF exposes no complete context inventory; zero-session completion cannot be verified": "smf_global_inventory_unavailable",
    }
    for source in ("ue", "amf", "smf", "upf", "probe"):
        known[f"{source} identity evidence is unavailable or stale"] = source + "_identity_unavailable_or_stale"
    phases = {"native_sources", "kubernetes_pods", "native_oam", "subscriber_pool", "gnb_ngap",
              "pod_ownership", "source_join", "ue_tun_probes", "generation_uid_recheck",
              "native_upf_embb", "native_upf_urllc", "native_upf_mmtc", "native_upf_v2x"}
    error_types = {"EvidenceUnavailable", "TimeoutExpired", "TimeoutError", "CalledProcessError", "OSError", "ValueError",
                   "KeyError", "TypeError", "RuntimeError", "JSONDecodeError", "ConnectionError"}
    if type(errors) is not list:
        return {"statusErrorCodes": ["malformed_error_list"], "statusErrorCount": None,
                "statusErrorsTruncated": False}
    codes = []
    for error in errors[:8]:
        code = "unclassified"
        if type(error) is str and len(error) <= 256:
            code = known.get(error, code)
            match = re.fullmatch(r"Native evidence unavailable: phase=([a-z_]+); errorType=([A-Za-z]+)", error)
            if match and match[1] in phases and match[2] in error_types:
                code = "native_evidence_unavailable." + match[1] + "." + match[2]
        codes.append(code)
    return {"statusErrorCodes": codes, "statusErrorCount": len(errors),
            "statusErrorsTruncated": len(errors) > 8}


class NativeBatchSnapshotBlocked(RoundRuntimeBlocked):
    def __init__(self, diagnostic):
        super().__init__("Fresh native batch snapshot required")
        self.code = "batch.native_snapshot_unavailable"
        self.nativeSnapshotDiagnostic = diagnostic
        # The exception may be normalized by outer runtime boundaries. Keep its
        # bounded local diagnosis in the existing measurement record as well.
        recorder = MeasurementTiming.current()
        if recorder is not None:
            try:
                recorder.record('native_snapshot.rejected', time.perf_counter(), status='error',
                                nativeSnapshotDiagnostic=deepcopy(diagnostic))
            except Exception:
                pass  # Diagnostics must never change the native rejection.


class NativeRoundBatch:
    _NATIVE_GUARD_TTL_SECONDS = .75

    def __init__(self, resources, ledger, *, enabled=False, detached=False, traffic_tuning=None,
                 detached_read_transport=None, detached_release_transport=None,
                 detached_postflight_transport=None, manager_transport=None):
        self.resources, self.ledger, self.enabled = resources, ledger, enabled
        self.detached = detached is True
        self.traffic_tuning = validate_tuning(DEFAULT_TUNING if traffic_tuning is None else traffic_tuning)
        self.detached_read_transport = detached_read_transport
        self.detached_release_transport = detached_release_transport
        self.detached_postflight_transport = detached_postflight_transport
        self.manager_transport = manager_transport
        self._native_guard_lock = Lock()
        self._native_guard_cache = None
        self._native_guard_inflight = {}

    def retired_epoch_command(self, raw, pinned_digest, physical, *, enabled=False, now=None, artifacts=None):
        """Unwired preparation only: never exec, claim, acknowledge or clean here.

        Future caller MUST own maintenance admission and drained coordinator work.
        Normal transport deliberately does not accept this exceptional command.
        """
        from backend.onprem.operations.recovery.retired_epoch import validate, require
        from backend.onprem.operations.recovery.retired_epoch_worker import command
        current = read_current(self.ledger)
        require(enabled is True and self._owned(current))
        matches = [a for a in current['actions'] if a.get('maintenance')]
        require(len(matches) == 1)
        record = matches[0]
        jobs = build_jobs(record['bindings'], aggregate_kbps=record['aggregateKbps'],
                          offered_by_supi=record['offeredBySupi'], **protocol_options(record.get('workload', {})))
        require([asdict(job) for job in jobs] == record['jobs'])
        validate_record_workload(record, current, allow_legacy=True)
        maintenance = record['maintenance']
        reservation = {'state': maintenance.get('state'), 'approvalDigest': maintenance.get('authorizationSha256'),
                       'actionId': record['id'], 'token': record['token'],
                       'approvalId': maintenance.get('approvalId'), 'reservationId': maintenance.get('id'),
                       'planDigest': maintenance.get('planSha256')}
        approval, receipt = validate(raw, pinned_digest, current, physical, reservation,
                                     now=time.time() if now is None else now, enabled=enabled, artifacts=artifacts)
        pod = self._pod(approval['pod'], approval['podUid'], jobs)
        statuses = [c for c in pod['status']['containerStatuses'] if c['name'] == 'traffic']
        require(len(statuses) == 1 and statuses[0].get('containerID') == approval['newContainerId']
                and type(statuses[0].get('restartCount')) is int
                and statuses[0]['restartCount'] == approval['restartCount'])
        # Construction is not authorization to execute after an arbitrary delay.
        # Batch2 must redo fresh runtime/Pod/expiry fences around actual dispatch.
        return command(approval, receipt), receipt

    def _owned(self, snapshot):
        with _ownership_span('control_read'):
            current = self.ledger.ownership_control()
        with _ownership_span('generation_read'):
            generation = self.resources.current_generation()
        return self._ownership_matches(snapshot, current, generation)

    def _ownership_matches(self, snapshot, current, generation):
        """Evaluate one freshly read owner pair; never retain dispatch authority."""
        with _ownership_span('lease_fences'):
            current_fence, expected_fence = self._lease_fence(current), self._lease_fence(snapshot)
        with _ownership_span('policy_quiet'):
            policy_quiet = self.resources.policy_quiet() is True
        if not policy_quiet:
            scoped = getattr(self.resources, "fixture_policy_quiet", None)
            with _ownership_span('fixture_policy_quiet'):
                policy_quiet = callable(scoped) and scoped(snapshot) is True
        return (current.get("roundId") == snapshot.get("roundId")
                and current.get("generation") == snapshot.get("generation")
                and current_fence is not False and expected_fence is not False
                and current_fence == expected_fence
                and current.get("stage") != "completed" and generation is not None
                and generation.get("generation") == snapshot.get("generation")
                and policy_quiet)

    def _detached_recovery_owned(self, record, snapshot):
        """Fence cleanup to the exact durable detached action, not live policy state."""
        current = self.ledger.batch_control()
        current_fence, expected_fence = self._lease_fence(current), self._lease_fence(snapshot)
        if (current.get("roundId") != snapshot.get("roundId")
                or current.get("generation") != snapshot.get("generation")
                or current.get("stage") == "completed"
                or current_fence is False or expected_fence is False
                or current_fence != expected_fence):
            return False
        matches = [row for row in current.get("actions", [])
                   if row.get("id") == record.get("id")]
        return (len(matches) == 1
                and matches[0].get("token") == record.get("token")
                and matches[0].get("executionMode") == record.get("executionMode")
                and matches[0].get("detachedReservation") == record.get("detachedReservation"))

    @staticmethod
    def _lease_fence(snapshot):
        lease = snapshot.get("fleetLease")
        if lease is None:
            return None
        if (not isinstance(lease, dict) or type(lease.get("generation")) is not int
                or lease["generation"] != snapshot.get("generation")
                or not isinstance(lease.get("fleetDigest"), str) or not re.fullmatch(r"[0-9a-f]{64}", lease["fleetDigest"])
                or not isinstance(lease.get("supis"), list) or lease["supis"] != sorted(lease["supis"])
                or len(set(lease["supis"])) != len(lease["supis"])
                or not isinstance(lease.get("counts"), dict) or set(lease["counts"]) != set(SLICES)
                or any(type(value) is not int or value < 0 for value in lease["counts"].values())
                or sum(lease["counts"].values()) != len(lease["supis"])
                or not isinstance(lease.get("identities"), list)
                or len(lease["identities"]) != len(lease["supis"])
                or any(not isinstance(row, dict) or set(row) != set(LEASE_FIELDS) for row in lease["identities"])
                or [row["supi"] for row in lease["identities"]] != lease["supis"]
                or {name: sum(row["slice"] == name for row in lease["identities"]) for name in SLICES} != lease["counts"]):
            return False
        return (snapshot.get("roundId"), lease["generation"], lease["fleetDigest"],
                tuple(lease["supis"]), tuple(tuple(row[field] for field in LEASE_FIELDS) for row in lease["identities"]))

    def _selected_rows(self, snapshot, rows):
        fence = self._lease_fence(snapshot)
        if fence is False:
            raise RoundRuntimeBlocked("Batch fleet lease identity is invalid")
        if fence is None:
            selected = rows
        else:
            by_supi = {row.get("supi"): row for row in rows}
            if len(by_supi) != len(rows) or any(supi not in by_supi for supi in fence[3]):
                raise RoundRuntimeBlocked("Leased batch SUPI is missing or duplicated")
            selected = [by_supi[supi] for supi in fence[3]]
            identities = snapshot["fleetLease"]["identities"]
            if any(tuple(row.get(field) for field in LEASE_FIELDS) != tuple(identity[field] for field in LEASE_FIELDS)
                   for row, identity in zip(selected, identities)):
                raise RoundRuntimeBlocked("Leased batch PDU identity changed")
        return selected

    def _jobs_owned(self, jobs, snapshot, *, shard=False):
        if not self._owned(snapshot):
            return False
        return self._job_cohort_matches(jobs, snapshot, shard=shard)

    @staticmethod
    def _job_cohort_matches(jobs, snapshot, *, shard=False):
        expected = set(snapshot.get("fleetLease", {}).get("supis", [])) if snapshot.get("fleetLease") else {
            row.get("supi") for row in snapshot.get("perUe", [])}
        actual = {job.supi for job in jobs}
        return bool(actual) and (actual <= expected if shard else actual == expected)

    def _pod(self, name, uid, jobs):
        pod = self.resources.traffic.pod(name, uid)
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {})
        if (metadata.get("namespace") != self.resources.controller.driver.namespace
                or metadata.get("name") != name or metadata.get("uid") != uid
                or metadata.get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running"
                or not any(c.get("name") == "traffic" and c.get("image") == TRAFFIC_IMAGE
                           for c in pod.get("spec", {}).get("containers", []))
                or not any(c.get("name") == "traffic" and c.get("ready") is True
                           and c.get("state", {}).get("running") is not None
                           for c in pod.get("status", {}).get("containerStatuses", []))):
            raise RoundRuntimeBlocked("Batch Pod UID, readiness, namespace or pinned image changed")
        ue_jobs = [j for j in jobs if (j.ue_pod, j.ue_uid) == (name, uid)]
        receiver_jobs = [j for j in jobs if (j.receiver_pod, j.receiver_uid) == (name, uid)]
        if bool(ue_jobs) == bool(receiver_jobs):
            raise RoundRuntimeBlocked("Batch Pod has no unique recorded role")
        traffic = next(c for c in pod["spec"]["containers"] if c.get("name") == "traffic")
        limit = traffic.get("resources", {}).get("limits", {}).get("memory")
        if ue_jobs:
            if limit != TRAFFIC_CLIENT_MEMORY_LIMIT:
                raise RoundRuntimeBlocked("Batch UE traffic memory capacity changed")
            supis = json.loads(metadata.get("annotations", {}).get("cityverse.io/supis", "[]"))
            if (labels.get("cityverse.io/managed-by") != "onprem-ue-controller"
                    or any(labels.get("cityverse.io/generation") != str(j.generation) or j.supi not in supis for j in ue_jobs)
                    or not any(c.get("name") == "ue" and c.get("image") == self.resources.controller.driver.image
                               for c in pod.get("spec", {}).get("containers", []))):
                raise RoundRuntimeBlocked("Batch UE ownership or pinned image changed")
        elif (limit != TRAFFIC_RECEIVER_MEMORY_LIMIT
              or labels.get("cityverse.io/runtime") != "kubeadm" or labels.get("cityverse.io/role") != "upf"
              or any(labels.get("app.kubernetes.io/name") != "n6-" + j.upf_name.removeprefix("upf-") for j in receiver_jobs)):
            raise RoundRuntimeBlocked("Batch N6 receiver ownership or memory capacity changed")
        return pod

    @staticmethod
    def _traffic_epoch(pod):
        statuses = [row for row in pod.get("status", {}).get("containerStatuses", [])
                    if row.get("name") == "traffic"]
        if len(statuses) != 1:
            raise RoundRuntimeBlocked("Unique traffic container epoch required")
        row = statuses[0]
        if (row.get("ready") is not True or not isinstance(row.get("containerID"), str)
                or not re.fullmatch(r"(?:containerd|docker|cri-o)://[A-Za-z0-9_.:-]{8,256}", row["containerID"])
                or type(row.get("restartCount")) is not int or row["restartCount"] < 0):
            raise RoundRuntimeBlocked("Running ready traffic container epoch required")
        return row["containerID"], row["restartCount"]

    def _evicted_client_terminal_closure(self, record, jobs, snapshot, *, role, name, uid):
        """Prove an exact evicted client or N6 receiver has no surviving sandbox.

        This is cleanup-only evidence.  It cannot execute, collect, or turn the
        failed round into accepted measurement evidence.
        """
        if role not in {"client", "server"} or not self._detached_recovery_owned(record, snapshot):
            raise RoundRuntimeBlocked("Detached terminal absence lacks cleanup ownership")
        pod = self.resources.traffic.pod(name, uid)
        metadata, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        client_jobs = [j for j in jobs if (j.ue_pod, j.ue_uid) == (name, uid)]
        receiver_jobs = [j for j in jobs if (j.receiver_pod, j.receiver_uid) == (name, uid)]
        expected_jobs = client_jobs if role == "client" else receiver_jobs
        if (not expected_jobs or bool(client_jobs) == bool(receiver_jobs)
                or metadata.get("namespace") != self.resources.controller.driver.namespace
                or metadata.get("name") != name or metadata.get("uid") != uid
                or metadata.get("deletionTimestamp")):
            raise RoundRuntimeBlocked("Detached evicted Pod ownership changed")
        containers = spec.get("containers", [])
        if not any(row.get("name") == "traffic" and row.get("image") == TRAFFIC_IMAGE for row in containers):
            raise RoundRuntimeBlocked("Detached evicted Pod pinned identity changed")
        labels = metadata.get("labels", {})
        if role == "client":
            try:
                supis = json.loads(metadata.get("annotations", {}).get("cityverse.io/supis", "[]"))
            except (TypeError, ValueError):
                raise RoundRuntimeBlocked("Detached evicted Pod ownership changed") from None
            if (labels.get("cityverse.io/managed-by") != "onprem-ue-controller"
                    or any(labels.get("cityverse.io/generation") != str(j.generation) or j.supi not in supis for j in expected_jobs)
                    or not any(row.get("name") == "ue" and row.get("image") == self.resources.controller.driver.image for row in containers)):
                raise RoundRuntimeBlocked("Detached evicted Pod ownership changed")
            limit = "16Mi"
        else:
            traffic = next(row for row in containers if row.get("name") == "traffic")
            volumes = [v for v in spec.get("volumes", []) if v.get("name") == "traffic-tmp"]
            if (labels.get("cityverse.io/runtime") != "kubeadm" or labels.get("cityverse.io/role") != "upf"
                    or any(labels.get("app.kubernetes.io/name") != "n6-" + j.upf_name.removeprefix("upf-") for j in expected_jobs)
                    or traffic.get("resources", {}).get("limits", {}).get("memory") != TRAFFIC_RECEIVER_MEMORY_LIMIT
                    or len(volumes) != 1 or volumes[0].get("emptyDir", {}).get("sizeLimit") != "64Mi"):
                raise RoundRuntimeBlocked("Detached evicted receiver ownership changed")
            limit = "64Mi"
        message = status.get("message")
        if (status.get("phase") != "Failed" or status.get("reason") != "Evicted"
                or not isinstance(message, str)
                or re.fullmatch(r'Usage of EmptyDir volume "traffic-tmp" exceeds the limit "' + limit + r'"\.\s*', message) is None):
            raise RoundRuntimeBlocked("Detached Pod is not an exact traffic-tmp eviction")
        conditions = {row.get("type"): row for row in status.get("conditions", [])}
        if (conditions.get("PodReadyToStartContainers", {}).get("status") != "False"
                or conditions.get("Ready", {}).get("status") != "False"
                or conditions.get("ContainersReady", {}).get("status") != "False"):
            raise RoundRuntimeBlocked("Detached evicted Pod sandbox is not terminal")
        app_names = {row.get("name") for row in containers}
        app_statuses = status.get("containerStatuses", [])
        if ({row.get("name") for row in app_statuses} != app_names
                or any(row.get("ready") is not False or row.get("started") not in (False, None)
                       or row.get("containerID") not in (None, "")
                       or not isinstance(row.get("state", {}).get("terminated"), dict)
                       for row in app_statuses)):
            raise RoundRuntimeBlocked("Detached evicted Pod containers are not terminal")
        traffic = next(row for row in app_statuses if row.get("name") == "traffic")
        selected = next(row for row in record["detachedReservation"]["roles"]
                        if row.get("role") == role and row.get("podUid") == uid)
        if type(traffic.get("restartCount")) is not int or traffic["restartCount"] < selected["restartCount"]:
            raise RoundRuntimeBlocked("Detached evicted traffic epoch is inconsistent")
        evidence = {
            "kind": "kubernetes-terminal-pod-absence-v1", "pod": name, "podUid": uid,
            "phase": "Failed", "reason": "Evicted", "volume": "traffic-tmp", "limit": limit,
            "podSandboxReady": False, "appContainersTerminal": sorted(app_names),
            "recordedContainerID": selected["containerID"],
            "recordedRestartCount": selected["restartCount"],
            "observedRestartCount": traffic["restartCount"],
        }
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"closureDigest": digest, "terminalAbsence": evidence}

    def _detached_identity(self, name, uid, jobs, snapshot):
        """Pure-read short exec fenced by one unchanged Pod/container epoch."""
        if not self._owned(snapshot):
            raise RoundRuntimeBlocked("Detached identity ownership changed")
        before = self._pod(name, uid, jobs)
        before_epoch = self._traffic_epoch(before)
        command = detached_worker_command("identity")
        started = time.monotonic()
        try:
            if self.manager_transport is not None:
                raw = self.manager_transport.execute("identity", before, timeout=2, maximum=128)
            elif self.detached_read_transport is not None:
                raw = self.detached_read_transport.execute(
                    "identity", before_epoch[0], command, timeout=2, maximum=128)
            else:
                raw = self.resources.traffic._run(
                    ["exec", name, "-c", "traffic", "--", *command], timeout=2)
        except (PolicyBlocked, TimeoutError, LocalKubernetesError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Do not log the exception string, command, stdout or stderr: an
            # exec exception can contain the whole worker program/manifest.
            logger.warning('Detached identity read failed phase=exec errorType=%s '
                           'elapsedSeconds=%.3f timeoutSeconds=2 returnCode=%s',
                           type(exc).__name__, time.monotonic() - started,
                           exc.returncode if isinstance(exc, subprocess.CalledProcessError) else None)
            raise RoundRuntimeBlocked("Detached identity probe failed") from exc
        if time.monotonic() - started > 2 or len(raw.encode()) > 128:
            logger.warning('Detached identity read rejected phase=readback reasonCode=%s '
                           'elapsedSeconds=%.3f timeoutSeconds=2',
                           'output_bound' if len(raw.encode()) > 128 else 'deadline',
                           time.monotonic() - started)
            raise RoundRuntimeBlocked("Detached identity probe exceeded its fixed bound")
        match = re.fullmatch(r"IDENTITYv1 ([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}) (net:\[[1-9][0-9]*\])\n?", raw)
        if match is None:
            raise RoundRuntimeBlocked("Detached identity response is invalid")
        after = self._pod(name, uid, jobs)
        if self._traffic_epoch(after) != before_epoch or not self._owned(snapshot):
            raise RoundRuntimeBlocked("Detached identity container epoch changed")
        return {"containerID": before_epoch[0], "restartCount": before_epoch[1],
                "bootId": match[1], "netns": match[2]}

    def _build_detached_reservation(self, jobs, snapshot, *, token, shard_index, shard_count,
                                    identity_observations=None):
        """Observe every role epoch, then create one immutable pre-start intent."""
        identities = ({("server", job.receiver_pod, job.receiver_uid) for job in jobs} |
                      {("client", job.ue_pod, job.ue_uid) for job in jobs})
        if not 1 <= len(identities) <= 11:
            raise RoundRuntimeBlocked("Detached role population is out of bounds")
        observed = {}
        def observe(identity):
            if identity_observations is None:
                return self._detached_identity(identity[1], identity[2], jobs, snapshot)
            return identity_observations.observe(identity, jobs, snapshot, shard_index)
        with ThreadPoolExecutor(max_workers=min(4, len(identities))) as pool:
            futures = {identity: pool.submit(
                _measurement_context(observe), identity)
                       for identity in sorted(identities)}
            for identity, future in futures.items():
                observed[identity] = future.result()
        namespace = self.resources.controller.driver.namespace
        roles = [{"role": role, "namespace": namespace, "pod": pod, "podUid": uid,
                  **observed[(role, pod, uid)]} for role, pod, uid in sorted(identities)]
        if any(role['bootId'] != detached_clock_boot_id() for role in roles):
            raise RoundRuntimeBlocked('Detached roles do not share the collector kernel clock')
        origin = detached_boottime()
        reservation = {
            "schemaVersion": TUNED_SCHEMA, "profile": SHARD_PROFILE,
            "roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
            "shardIndex": shard_index, "shardCount": shard_count, "token": token,
            "manifestSha256": detached_manifest_sha256(jobs, self.traffic_tuning), "jobs": detached_job_rows(jobs),
            "trafficTuning": deepcopy(self.traffic_tuning),
            "createdMonotonic": origin,
            "admissionDeadline": origin + DETACHED_ADMISSION_SECONDS,
            "clientChildDeadline": origin + DETACHED_CLIENT_CHILD_SECONDS,
            "serverChildDeadline": origin + DETACHED_SERVER_CHILD_SECONDS,
            "transportDeadline": origin + DETACHED_TRANSPORT_SECONDS,
            "clockSource": "CLOCK_BOOTTIME",
            "clockBootId": roles[0]["bootId"], "roles": roles,
        }
        if not validate_detached_reservation(
                reservation, round_id=snapshot.get("roundId"), generation=snapshot.get("generation"),
                shard_index=shard_index, shard_count=shard_count, token=token, jobs=jobs):
            raise RoundRuntimeBlocked("Detached reservation construction failed")
        return reservation

    @staticmethod
    def _selected_detached_reservation(base, *, role, pod_uid):
        matches = [row for row in base.get("roles", [])
                   if row.get("role") == role and row.get("podUid") == pod_uid]
        if len(matches) != 1:
            raise RoundRuntimeBlocked("Detached command role identity is ambiguous")
        selected = matches[0]
        return {**deepcopy(base), "selectedRole": role, "selectedPodUid": pod_uid,
                "selectedBootId": selected["bootId"], "selectedNetns": selected["netns"]}

    def _detached_control(self, action_name, record, jobs, snapshot, *, role, name, uid,
                          epoch=0, recovery=False, _defer_postflight=False):
        """Execute one short detached control command with exact durable identity.

        Recovery never replays a start or release, even when a previous receipt
        is absent. Every response is parsed before it can affect coordination.
        """
        if recovery and action_name in {"start", "release"}:
            raise RoundRuntimeBlocked("Detached start or release cannot be replayed during recovery")
        if _defer_postflight and (action_name != 'start' or role != 'server' or recovery):
            raise RoundRuntimeBlocked('Only live receiver start may defer postflight')
        if action_name not in {"capability", "start", "status", "release", "collect", "cancel"}:
            raise RoundRuntimeBlocked("Invalid detached control action")
        if (action_name == "release") != (type(epoch) is int and epoch > 0):
            raise RoundRuntimeBlocked("Detached control epoch is invalid")
        base = record.get("detachedReservation")
        if (record.get("executionMode") != "detached-shard-v1"
                or not validate_detached_reservation(
                    base, round_id=snapshot.get("roundId"), generation=snapshot.get("generation"),
                    shard_index=record.get("shardIndex"), shard_count=record.get("shardCount"),
                    token=record.get("token"), jobs=jobs)):
            raise RoundRuntimeBlocked("Detached control lacks exact durable reservation")
        if recovery and not self._detached_recovery_owned(record, snapshot):
            raise RoundRuntimeBlocked("Detached recovery durable ownership changed")
        reservation = self._selected_detached_reservation(base, role=role, pod_uid=uid)
        selected = next(row for row in base["roles"]
                        if row["role"] == role and row["podUid"] == uid)
        if selected["pod"] != name:
            raise RoundRuntimeBlocked("Detached command Pod name changed")
        command = detached_worker_command(action_name, reservation, epoch=epoch)
        def control_span(part):
            return span('transport.'+part, action=action_name, role=role, shardIndex=record['shardIndex'])
        def blocked(substage, exc):
            failure = RoundRuntimeBlocked("Detached control command failed")
            failure.code = "batch.detached_control_failed"
            failure.detached_diagnostic = {
                "kind": "detached_control", "role": role, "action": action_name,
                "substage": substage,
                "errorCategory": ("timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                                  else "nonzero" if substage == "exec" else "blocked"),
            }
            return failure
        try:
            with control_span('pod_preflight'):
                before = self._pod(name, uid, jobs)
        except (RoundRuntimeBlocked, PolicyBlocked, LocalKubernetesError,
                subprocess.TimeoutExpired) as exc:
            if recovery and action_name == "cancel":
                try:
                    return self._evicted_client_terminal_closure(
                        record, jobs, snapshot, role=role, name=name, uid=uid)
                except (RoundRuntimeBlocked, PolicyBlocked, LocalKubernetesError,
                        subprocess.TimeoutExpired):
                    pass
            raise blocked("preflight_pod", exc) from exc
        if self._traffic_epoch(before) != (selected["containerID"], selected["restartCount"]):
            raise RoundRuntimeBlocked("Detached command container epoch changed")
        selected_count = sum(
            (job.receiver_uid if role == "server" else job.ue_uid) == uid for job in jobs)
        if action_name == "start":
            child_deadline = base["serverChildDeadline" if role == "server" else "clientChildDeadline"]
            remaining = min(base["admissionDeadline"], child_deadline) - detached_boottime()
            timeout = min(10 if role == "server" else 8, remaining)
            if timeout <= 0:
                raise RoundRuntimeBlocked("Detached start admission deadline expired")
        elif action_name == "collect":
            timeout = 10
        elif action_name in {"release", "status", "cancel"}:
            timeout = 5
        else:
            timeout = 2
        maximum = (min(DETACHED_COLLECT_WIRE_MAX,
                       selected_count * DETACHED_RECORD_WIRE_MAX + 512)
                   if action_name == "collect" else 1024)
        exec_started = time.monotonic()
        try:
            with control_span('request'):
                if self.manager_transport is not None:
                    raw = self.manager_transport.execute(action_name, before, reservation=reservation,
                        epoch=epoch, timeout=timeout, maximum=maximum)
                elif self.detached_read_transport is not None and action_name in {"capability", "status"}:
                    raw = self.detached_read_transport.execute(
                        action_name, selected["containerID"], command, reservation=reservation,
                        timeout=timeout, maximum=1024)
                elif action_name == "release" and self.detached_release_transport is not None:
                    raw = self.detached_release_transport.execute(
                        action_name, selected["containerID"], command, reservation=reservation,
                        epoch=epoch, timeout=timeout, maximum=1024)
                elif action_name in {"collect", "cancel"} and self.detached_postflight_transport is not None:
                    raw = self.detached_postflight_transport.execute(
                        action_name, selected["containerID"], command, reservation=reservation,
                        timeout=timeout, maximum=maximum)
                else:
                    raw = self.resources.traffic._run(
                        ["exec", name, "-c", "traffic", "--", *command], timeout=timeout)
        except (PolicyBlocked, TimeoutError, LocalKubernetesError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as exc:
            logger.warning('Detached control failed action=%s role=%s errorType=%s '
                           'elapsedSeconds=%.3f timeoutSeconds=%.3f',
                           action_name, role, type(exc).__name__,
                           time.monotonic()-exec_started, timeout)
            raise blocked("exec", exc) from exc
        if len(raw.encode()) > maximum:
            raise RoundRuntimeBlocked("Detached control response is oversized")
        finished = False
        def finish():
            nonlocal finished
            if finished:
                raise RoundRuntimeBlocked('Detached postflight cannot be consumed twice')
            finished = True
            try:
                with control_span('pod_postflight'):
                    after = self._pod(name, uid, jobs)
            except (PolicyBlocked, LocalKubernetesError, subprocess.TimeoutExpired) as exc:
                raise blocked("postflight_pod", exc) from exc
            with control_span('ownership'):
                source = _ownership_timing_source.set(dict(action=action_name, role=role,
                    shardIndex=record['shardIndex']))
                try:
                    owned = (self._detached_recovery_owned(record, snapshot) if recovery
                             else self._owned(snapshot))
                finally:
                    _ownership_timing_source.reset(source)
            if (self._traffic_epoch(after) != (selected["containerID"], selected["restartCount"])
                    or not owned):
                raise RoundRuntimeBlocked("Detached command postflight identity changed")
            with control_span('parse'):
                result = parse_detached_response(action_name, raw, token=record["token"], epoch=epoch)
            if action_name == "capability":
                encoded = json.dumps(reservation, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                if result["stateDigest"] != hashlib.sha256(encoded).hexdigest():
                    raise RoundRuntimeBlocked("Detached capability tuning digest changed")
            return result
        return finish if _defer_postflight else finish()

    def _transport(self, jobs, snapshot, cancelled=lambda: False, *, shard=False):
        max_timeout = 52 if shard else 38
        def execute(name, uid, argv, timeout):
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= max_timeout:
                raise RoundRuntimeBlocked("Batch command differs from validated job manifest")
            role = argv[4] if len(argv) > 4 and isinstance(argv[4], str) else "unknown"
            started = time.monotonic()
            deadline = time.monotonic() + timeout
            def blocked(code, message, substage):
                elapsed = time.monotonic() - started
                exc = RoundRuntimeBlocked(message)
                exc.code = code
                exc.transport_diagnostic = {
                    "kind": "transport",
                    "role": role if role in {"server", "client", "ready", "client_ready", "release", "cancel"} else "unknown",
                    "pod": name if isinstance(name, str) else "unknown",
                    "transportSubstage": substage,
                    "elapsedSeconds": elapsed,
                    "budgetSeconds": timeout,
                }
                return exc
            def remaining(substage):
                value = deadline - time.monotonic()
                if value <= 0:
                    raise blocked('batch.transport_deadline_expired', "Batch transport deadline expired", substage)
                return value
            if not self._jobs_owned(jobs, snapshot, shard=shard):
                raise RoundRuntimeBlocked("Batch round ownership changed")
            # BatchTraffic is the sole command builder; still reject accidental
            # command substitution or a token different from the durable intent.
            if (len(argv) != 8 or argv[:2] != ["bash", "-c"] or argv[3] != "cityverse-batch"
                    or argv[4] not in {"server", "client", "ready", "client_ready", "release", "cancel"}):
                raise blocked('batch.transport_command_invalid', "Invalid batch worker command", "command_validate")
            token = argv[5]
            recorded = [a for a in self.ledger.batch_control()["actions"]
                        if a.get("kind") == "traffic_window" and a.get("mode") == "batch" and a.get("token") == token]
            if len(recorded) != 1 or recorded[0].get("jobs") != [asdict(j) for j in jobs]:
                raise RoundRuntimeBlocked("Batch execution lacks its exact durable intent")
            current = self.ledger.batch_control()
            if argv[4] in {"server", "client", "client_ready", "release"} and (
                    current.get("stage") not in {"measuring_baseline", "measuring_result"}
                    or current.get("stage") != recorded[0].get("stage")
                    or current.get("executionOutcome") is not None
                    or recorded[0].get("cleanupVerified") is not False or cancelled()):
                raise RoundRuntimeBlocked("Batch launch reservation is no longer active")
            validate_record_workload(recorded[0], current, allow_legacy=argv[4] in {"ready", "cancel"})
            group = [j for j in jobs if (j.receiver_pod, j.receiver_uid) == (name, uid)] if argv[4] == "server" else [j for j in jobs if (j.ue_pod, j.ue_uid) == (name, uid)]
            if argv[4] in {"ready", "cancel"}:
                group = []
            if (argv != worker_command(argv[4], token, group, epoch=int(argv[6]),
                                       profile=SHARD_PROFILE if shard else LEGACY_PROFILE)
                    or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= max_timeout):
                raise RoundRuntimeBlocked("Batch command differs from validated job manifest")
            remaining("before_preflight_read")
            # Existing Pod/native read APIs are not interruptible by this budget.
            # Recheck after every such read: never dispatch late or claim a hard
            # wall-time bound on the read itself. Post-dispatch expiry is unknown.
            try:
                before = self._pod(name, uid, jobs)
            except RoundRuntimeBlocked as exc:
                raise blocked('batch.worker_failed',
                              "Batch preflight Pod identity changed", "preflight_fence") from exc
            remaining("after_preflight_read")
            if argv[4] in {"server", "client", "client_ready", "release"} and cancelled():
                raise RoundRuntimeBlocked("Batch launch was cancelled during Pod fencing")
            try:
                if self.manager_transport is not None:
                    raw = self.manager_transport.execute_batch(argv, before,
                        timeout=remaining("dispatch"), maximum=DETACHED_COLLECT_WIRE_MAX)
                else:
                    raw = self.resources.traffic._run(["exec", name, "-c", "traffic", "--", *argv], timeout=remaining("dispatch"))
            except (TimeoutError, subprocess.TimeoutExpired) as exc:
                raise blocked('batch.transport_deadline_expired', "Batch transport deadline expired", "dispatch") from exc
            except (PolicyBlocked, LocalKubernetesError, subprocess.CalledProcessError) as exc:
                # Preserve a bounded typed category without persisting kubectl
                # stderr/stdout, command text, token, or adapter exception.
                raise blocked('batch.worker_failed', "Batch transport command failed", "dispatch_nonzero") from exc
            remaining("after_dispatch")
            try:
                after = self._pod(name, uid, jobs)
                if self.manager_transport is not None and self._traffic_epoch(after) != self._traffic_epoch(before):
                    raise RoundRuntimeBlocked('Batch traffic manager container epoch changed')
            except RoundRuntimeBlocked as exc:
                raise blocked('batch.worker_failed',
                              "Batch postflight Pod identity changed", "postflight_fence") from exc
            remaining("after_postflight_read")
            if not self._owned(snapshot):
                raise RoundRuntimeBlocked("Batch ownership changed during execution")
            remaining("complete")
            return raw
        return execute

    def _native_guard_scope(self, snapshot):
        """Return the exact live round/generation/stage scope, or fail closed."""
        with _ownership_span('control_read'):
            current = self.ledger.ownership_control()
        with _ownership_span('generation_read'):
            generation = self.resources.current_generation()
        stage = snapshot.get("stage")
        if (not self._ownership_matches(snapshot, current, generation) or not isinstance(stage, str)
                or current.get("roundId") != snapshot.get("roundId")
                or current.get("generation") != snapshot.get("generation")
                or current.get("stage") != stage or generation is None
                or generation.get("generation") != snapshot.get("generation")):
            return None
        return (snapshot.get("roundId"), snapshot.get("generation"), stage)

    def _native_guard_observation(self, key, generation, snapshot, *, refresh=False):
        """Singleflight one immutable native observation for a very short scope."""
        with self._native_guard_lock:
            now = time.monotonic()
            cached = self._native_guard_cache
            if (not refresh and cached is not None and cached[0] == key
                    and 0 <= now - cached[1] <= self._NATIVE_GUARD_TTL_SECONDS):
                with span('native_guard.cache_copy', cacheMode='hit'):
                    return deepcopy(cached[2])
            event = self._native_guard_inflight.get(key)
            leader = event is None
            if leader:
                event = Event()
                self._native_guard_inflight[key] = event
                # An expired success must never survive a newer failed read.
                self._native_guard_cache = None
        if not leader:
            with span('native_guard.singleflight_wait'):
                event.wait()
            with self._native_guard_lock:
                cached = self._native_guard_cache
                if cached is None or cached[0] != key:
                    return None
                with span('native_guard.cache_copy', cacheMode='joined'):
                    return deepcopy(cached[2])

        status = None
        try:
            with span('native_guard.capture'):
                observed = self.resources.controller.adapter.observe(generation["generation"])
            with span('native_guard.evidence_copy'):
                evidence = deepcopy(observed)
            del observed  # Restore the original temporary capture's lifetime.
            with span('native_guard.evaluate'):
                candidate = EvidenceVerifier(stable_seconds=0).evaluate(generation, evidence)
            if self._native_guard_scope(snapshot) == key:
                with span('native_guard.candidate_copy'):
                    status = deepcopy(candidate)
        except Exception:
            # Unavailable or malformed native evidence remains a closed guard.
            status = None
        finally:
            with self._native_guard_lock:
                cached_at = time.monotonic()
                with span('native_guard.cache_store_copy'):
                    cached_status = deepcopy(status)
                self._native_guard_cache = (key, cached_at, cached_status)
                self._native_guard_inflight.pop(key, None)
                event.set()
        with span('native_guard.return_copy'):
            return deepcopy(status)

    def _native_guard(self, jobs, snapshot, *, shard=False):
        if not self._jobs_owned(jobs, snapshot, shard=shard):
            return False
        key = self._native_guard_scope(snapshot)
        if key is None:
            return False
        generation = self.resources.current_generation()
        if generation is None or generation.get("generation") != key[1]:
            return False
        status = self._native_guard_observation(key, generation, snapshot)
        with span('native_guard.identity_check'):
            return self._guard_from_observation(jobs, snapshot, key, status, shard=shard)

    def _guard_from_observation(self, jobs, snapshot, key, status, *, shard, diagnostic=None):
        """Validate an immutable source without changing its original freshness."""
        def decline(code, **details):
            if diagnostic is not None:
                diagnostic.update(failedConjuncts=[code], **details)
            return False
        if self._native_guard_scope(snapshot) != key:
            return decline('ownership')
        if status is None:
            return decline('observation_unavailable')
        if status.get('status') != 'established':
            return decline('status', status=status.get('status'), **status_error_diagnostic(status.get('errors')))
        if not fresh(status.get('observedAt')):
            return decline('freshness', observedAt=status.get('observedAt'), observerNow=time.time(), freshnessMaxSeconds=15)
        try:
            selected = self._selected_rows(snapshot, status["perUe"])
        except RoundRuntimeBlocked:
            return decline('lease_identity')
        rows = {r["supi"]: r for r in selected}
        if not ({j.supi for j in jobs} <= set(rows) if shard else set(rows) == {j.supi for j in jobs}):
            return decline('cohort_membership', observedCount=len(rows), expectedCount=len({j.supi for j in jobs}))
        for job in jobs:
            if job.route_binding_id:
                try:
                    route = self.resources.service_route(snapshot, rows[job.supi])
                    if (route['binding_id'], route['version'], route['path_revision'], route['application_pod_uid'], route['address'], route['receiver_peer']) != (
                            job.route_binding_id, job.route_version, job.route_path_revision, job.receiver_uid, job.endpoint, job.receiver_peer):
                        return decline('native_service_route')
                except Exception:
                    return decline('native_service_route')
            expected = (job.supi, job.generation, job.ue_ip, job.session_id, job.ue_uid, job.tun,
                        job.upf_name, job.sm_context_ref, job.pfcp_seid, job.dnn, job.sst, job.sd, job.upf_epoch)
            if tuple(rows[job.supi].get(k) for k in IDENTITY_FIELDS) != expected:
                return decline('native_identity', fields=[k for k,v in zip(IDENTITY_FIELDS,expected) if rows[job.supi].get(k)!=v])
        if (self._native_guard_scope(snapshot) != key
                or not self._job_cohort_matches(jobs, snapshot, shard=shard)):
            return decline('ownership')
        return True

    def _prepare_detached_native_preflight(self, jobs, snapshot):
        key = self._native_guard_scope(snapshot)
        generation = self.resources.current_generation()
        if key is None or generation is None or generation.get('generation') != key[1]:
            raise RoundRuntimeBlocked('Detached cohort native scope changed')
        for attempt in range(2):
            diagnostic = {'phase':'cohort_preflight','attempt':attempt+1,'expectedGeneration':key[1]}
            status = self._native_guard_observation(key, generation, snapshot, refresh=attempt>0)
            if self._guard_from_observation(jobs, snapshot, key, status, shard=False, diagnostic=diagnostic):
                break
            retryable = diagnostic['failedConjuncts'][0] in {'observation_unavailable','status','freshness'}
            logger.warning('Detached cohort native preflight rejected generation=%s attempt=%s diagnostic=%s',
                           key[1], attempt+1, json.dumps(diagnostic, sort_keys=True))
            if attempt or not retryable or self._native_guard_scope(snapshot) != key:
                raise NativeBatchSnapshotBlocked(diagnostic)
        else:
            raise NativeBatchSnapshotBlocked(diagnostic)
        def check_shard(selected):
            details={'phase':'shard_preflight','expectedGeneration':key[1]}
            started=time.monotonic()
            accepted=self._guard_from_observation(selected, snapshot, key, status, shard=True, diagnostic=details)
            if not accepted:
                details.update(elapsedSeconds=round(time.monotonic()-started,6),
                               observedAt=status.get('observedAt') if isinstance(status,dict) else None,
                               observerNow=time.time())
                logger.warning('Detached shard native preflight rejected generation=%s diagnostic=%s',
                               key[1],json.dumps(details,sort_keys=True))
            return accepted
        return check_shard

    def _require_snapshot(self, snapshot, status):
        owned = self._owned(snapshot)
        observed = status.get("observedAt")
        now = time.time()
        status_fresh = fresh(observed, now=now)
        status_matches = status.get("status") == "established"
        generation_matches = status.get("generation") == snapshot["generation"]
        if not (owned and generation_matches and status_fresh and status_matches):
            failed = []
            if not owned:
                failed.append("ownership")
            if not generation_matches:
                failed.append("generation")
            if not status_fresh:
                failed.append("freshness")
            if not status_matches:
                failed.append("status")
            try:
                age = now - seconds(observed)
            except (TypeError, ValueError):
                age = None
            raise NativeBatchSnapshotBlocked({
                "reason": "snapshot_rejected",
                "failedConjuncts": failed,
                "owned": owned,
                "expectedGeneration": snapshot["generation"],
                "observedGeneration": status.get("generation"),
                "status": status.get("status"),
                "observedAt": observed,
                "observerNow": now,
                "ageSeconds": age,
                "freshnessMaxSeconds": 15,
                **status_error_diagnostic(status.get("errors")),
            })

    def _bindings(self, snapshot):
        # Controller.status() is a polling cache. A valid prior traffic window
        # can outlive that cache; admission must read current native evidence,
        # not promote/re-date the cache or retry the failed traffic window.
        if not self._owned(snapshot):
            raise RoundRuntimeBlocked("Batch round ownership changed before native observation")
        generation = self.resources.current_generation()
        if generation is None or generation.get("generation") != snapshot.get("generation"):
            raise RoundRuntimeBlocked("Batch generation changed before native observation")
        acquisition_phase = 'observe'
        try:
            with span('bindings.observe'):
                observed = self.resources.controller.adapter.observe(generation["generation"])
                if not isinstance(observed, dict):
                    raise ValueError('Native snapshot is not an object')
            acquisition_phase = 'evidence_copy'
            with span('bindings.evidence_copy'):
                evidence = deepcopy(observed)
            del observed
        except Exception as exc:
            error_type = type(exc).__name__
            if error_type not in {'EvidenceUnavailable', 'TimeoutError', 'TimeoutExpired', 'OSError',
                                  'ValueError', 'TypeError', 'KeyError', 'AttributeError', 'RuntimeError',
                                  'JSONDecodeError', 'ConnectionError', 'RemoteDisconnected', 'HTTPError', 'URLError'}:
                error_type = 'UnexpectedError'
            owned = self._owned(snapshot)
            # No snapshot exists to age or evaluate. In particular a missing
            # observedAt must not turn an acquisition error into a stale claim.
            raise NativeBatchSnapshotBlocked(dict(reason='acquisition_failed',
                failureCode='native_snapshot.acquisition_failed', acquisitionPhase=acquisition_phase,
                errorType=error_type, failedConjuncts=['acquisition'] + ([] if owned else ['ownership']),
                owned=owned, expectedGeneration=snapshot['generation'])) from None
        with span('bindings.evaluate_before_pods'):
            status = EvidenceVerifier(stable_seconds=0).evaluate(generation, evidence)
        self._require_snapshot(snapshot, status)
        with span('bindings.pods'):
            pods = self.resources._pods()  # Pod GET only; no pre-intent worker exec.
        # Re-evaluate the same immutable source snapshot after Pod I/O. Its
        # oldest individual source can expire even when observedAt is fresh.
        with span('bindings.evaluate_after_pods'):
            status = EvidenceVerifier(stable_seconds=0).evaluate(generation, evidence)
        self._require_snapshot(snapshot, status)
        selected = self._selected_rows(snapshot, status.get("perUe", []))
        rows = {r["supi"]: r for r in selected}
        if len(rows) != len(selected) or set(rows) != {r["supi"] for r in snapshot["perUe"]}:
            raise RoundRuntimeBlocked("Batch native population changed")
        bindings = []
        for owner in snapshot["perUe"]:
            row = rows[owner["supi"]]
            if (row.get("trafficVerified") is not True or any(row.get(k) != owner[v] for k, v in
                    (("ip", "ueIp"), ("sessionId", "sessionId"), ("podUid", "podUid"), ("interface", "tun"), ("upf", "upf")))):
                raise RoundRuntimeBlocked("Batch native PDU differs from round identity")
            if any(owner.get(k) is None or row.get(k) != owner[k] for k in
                   ("smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch")):
                raise RoundRuntimeBlocked("Batch immutable PDU identity differs from established round")
            ue = [p for p in pods if p.get("metadata", {}).get("uid") == owner["podUid"]]
            if len(ue) != 1:
                raise RoundRuntimeBlocked("Unique batch UE Pod required")
            receiver_pod, endpoint, route = self.resources.service_receiver(snapshot, owner, pods)
            receiver = receiver_pod["metadata"]
            bindings.append({**{k: row.get(k) for k in IDENTITY_FIELDS if k != "interface"},
                             "slice": owner["slice"], "tun": row["interface"], "pod": ue[0]["metadata"]["name"],
                             "receiverPod": receiver["name"], "receiverUid": receiver["uid"],
                              **({"serviceRoute": route} if route else {})})
        self._require_snapshot(snapshot, status)
        return bindings

    async def sample(self, snapshot, reserve, *, cancelled=lambda: False):
        if not self.enabled:
            raise RoundRuntimeBlocked("Batch traffic is disabled")
        return await asyncio.to_thread(self._sample, deepcopy(snapshot), reserve, cancelled)

    async def sample_shards(self, snapshot, reserve_shard, *, cancelled=lambda: False):
        """Execute all pre-journaled shard intents concurrently and drain on failure."""
        if not self.enabled:
            raise RoundRuntimeBlocked('Batch traffic is disabled')
        return await asyncio.to_thread(self._sample_shards, deepcopy(snapshot), reserve_shard, cancelled)

    def _preflight_detached_tuning(self, reserved_shards, snapshot, cancelled):
        """Drain all pure-read capability probes before any shard may start."""
        candidates = [(record, jobs, role) for record, jobs in reserved_shards
                      if record.get("detachedReservation", {}).get("schemaVersion") == TUNED_SCHEMA
                      for role in record["detachedReservation"]["roles"]]
        unique_probes, role_epochs = {}, {}
        for record, jobs, role in candidates:
            identity = (role["role"], role["podUid"])
            epoch = json.dumps(role, sort_keys=True, separators=(",", ":"))
            if identity in role_epochs and role_epochs[identity] != epoch:
                raise RoundRuntimeBlocked("Detached capability role epoch differs across shards")
            role_epochs[identity] = epoch
            tuning = json.dumps(record["detachedReservation"]["trafficTuning"], sort_keys=True)
            unique_probes.setdefault((epoch, tuning), (record, jobs, role))
        probes = list(unique_probes.values())
        if probes:
            # Probe the immutable reserved value, never mutable process config.
            for tuning in {json.dumps(record["detachedReservation"]["trafficTuning"], sort_keys=True)
                           for record, _, _ in probes}:
                verify_host_socket_tuning(json.loads(tuning))
            def probe(item):
                record, jobs, role = item
                if cancelled() or not self._owned(snapshot):
                    raise RoundRuntimeBlocked("Detached tuning preflight cancelled")
                return self._detached_control("capability", record, jobs, snapshot,
                    role=role["role"], name=role["pod"], uid=role["podUid"])
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_measurement_context(probe), probes))

    @timed('batch.total')
    def _sample_shards(self, snapshot, reserve_shard, cancelled):
        with span('batch.plan'):
            bindings = sorted(self._bindings(snapshot), key=lambda row: row['supi'])
            if len(bindings) < getattr(self, 'minimum_shard_population', 50):
                raise RoundRuntimeBlocked('Sharded population is below the trusted provider minimum')
            workload = plan_for_stage(snapshot, current_only=True)
            rates = applied_rates(workload)
            budget = max(workload['appliedUplinkKbps'], workload['appliedDownlinkKbps'])
            shards = build_shards(bindings, round_id=snapshot['roundId'], generation=snapshot['generation'],
                                  aggregate_kbps=budget, offered_by_supi=rates,
                                  minimum_shards=getattr(self, 'minimum_shards', 1), **protocol_options(workload))
        intents = []
        identity_observations = _DetachedIdentityObservations(self, snapshot, cancelled) if self.detached else None
        reservations = [None] * len(shards)
        native_preflight = None
        if self.detached:
            # Observe each container once before starting any admission clock.
            # Later shard reads still check its current Pod/container epoch.
            identities = ({("server", job.receiver_pod, job.receiver_uid) for jobs in shards for job in jobs} |
                          {("client", job.ue_pod, job.ue_uid) for jobs in shards for job in jobs})
            all_jobs = [job for jobs in shards for job in jobs]
            with span('prepare.identities'):
                with ThreadPoolExecutor(max_workers=min(4, len(identities))) as pool:
                    list(pool.map(_measurement_context(lambda identity: identity_observations.observe(
                        identity, all_jobs, snapshot, -1)), sorted(identities)))
            # Serial role observations previously consumed early shards' fixed
            # child deadlines while constructing later shards. Prepare the
            # independent intents together; journal all of them before starts.
            tokens = [uuid4().hex for _ in shards]
            def prepare(index):
                return self._build_detached_reservation(
                    shards[index], snapshot, token=tokens[index], shard_index=index,
                    shard_count=len(shards), identity_observations=identity_observations)
            with span('prepare.reservations'):
                with ThreadPoolExecutor(max_workers=min(4, len(shards))) as pool:
                    reservations = list(pool.map(_measurement_context(prepare), range(len(shards))))
            # Capability reads create no worker or dispatch authority. Finish
            # them before reserving a worker's fixed lifetime, then re-observe
            # the same role epochs and commit newly timed immutable intents.
            probe_reservations = reservations
            with span('prepare.capabilities'):
                self._preflight_detached_tuning([
                    ({'executionMode': 'detached-shard-v1', 'token': tokens[index],
                      'shardIndex': index, 'shardCount': len(shards),
                      'detachedReservation': reservation}, shards[index])
                    for index, reservation in enumerate(probe_reservations)], snapshot, cancelled)
            with span('prepare.refence'):
                with ThreadPoolExecutor(max_workers=min(4, len(shards))) as pool:
                    reservations = list(pool.map(_measurement_context(prepare), range(len(shards))))
                clock_fields = {'createdMonotonic', 'admissionDeadline', 'clientChildDeadline',
                                'serverChildDeadline', 'transportDeadline'}
                for probed, reserved in zip(probe_reservations, reservations):
                    if ({k: v for k, v in probed.items() if k not in clock_fields}
                            != {k: v for k, v in reserved.items() if k not in clock_fields}):
                        raise RoundRuntimeBlocked('Detached capability scope changed before reservation')
            with span('prepare.native_preflight'):
                native_preflight = self._prepare_detached_native_preflight(all_jobs, snapshot)
            # No worker exists yet. Every role and capability has been checked;
            # assign a common immutable origin only after all preparation drains.
            origin = detached_boottime()
            for reservation in reservations:
                reservation.update(createdMonotonic=origin,
                    admissionDeadline=origin+DETACHED_ADMISSION_SECONDS,
                    clientChildDeadline=origin+DETACHED_CLIENT_CHILD_SECONDS,
                    serverChildDeadline=origin+DETACHED_SERVER_CHILD_SECONDS,
                    transportDeadline=origin+DETACHED_TRANSPORT_SECONDS)
        requests = []
        for index, jobs in enumerate(shards):
            # Shards are deliberately striped across immutable UE Pod owners;
            # reconstruct the reserved rows from their exact compiled SUPIs.
            shard_supis = {job.supi for job in jobs}
            rows = [row for row in bindings if row['supi'] in shard_supis]
            if {row['supi'] for row in rows} != shard_supis:
                raise RoundRuntimeBlocked('Shard binding partition changed')
            shard_rates = {row['supi']: rates[row['supi']] for row in rows}
            token = tokens[index] if self.detached else uuid4().hex
            detached_reservation = reservations[index]
            reserve_kwargs = dict(workload=deepcopy(workload), shard_index=index,
                                  shard_count=len(shards))
            if detached_reservation is not None:
                reserve_kwargs["detached_reservation"] = detached_reservation
            requests.append(dict(token=token, bindings=deepcopy(rows), jobs=[asdict(j) for j in jobs],
                                 rates=shard_rates, budget=rate_budget(shard_rates), **reserve_kwargs))
        with span('batch.reserve_and_verify'):
            reserve_many = getattr(reserve_shard, 'reserve_many', None)
            if cancelled():
                raise RoundRuntimeBlocked('Shard intent reservation cancelled')
            if callable(reserve_many):
                if reserve_many(requests) is not True:
                    raise RoundRuntimeBlocked('Shard cohort reservation failed')
            else:
                for request in requests:
                    if cancelled() or reserve_shard(request['token'], request['bindings'], request['jobs'],
                            request['rates'], request['budget'], **{k:v for k,v in request.items()
                            if k not in {'token','bindings','jobs','rates','budget'}}) is not True:
                        raise RoundRuntimeBlocked('Shard intent reservation failed')
            committed = self.ledger.batch_control()['actions']
            for index, (jobs, request) in enumerate(zip(shards, requests)):
                token, rows, shard_rates = request['token'], request['bindings'], request['rates']
                action_rows = [a for a in committed if a.get('kind') == 'traffic_window' and a.get('token') == token]
                if len(action_rows) != 1 or action_rows[0].get('jobs') != [asdict(j) for j in jobs]:
                    raise RoundRuntimeBlocked('Shard intent reservation failed')
                intents.append((token, action_rows[0]['id'], rows, shard_rates, action_rows[0]))
        release_barrier = BatchReleaseBarrier(
            len(shards), cancelled=cancelled,
            monotonic=detached_boottime if self.detached else time.monotonic)
        startup_slots = _ReceiverStartupSlots(
            job.receiver_uid for jobs in shards for job in jobs) if self.detached else None
        startup_aborted = Event()
        failure_lock, first_failure = Lock(), []
        timing = MeasurementTiming.current()
        def run(index):
            # ThreadPoolExecutor does not propagate ContextVar state.
            if timing is not None:
                with timing.bind(), timing.span('shard.total', shardIndex=index):
                    return run_shard(index)
            return run_shard(index)
        def run_shard(index):
            jobs = shards[index]
            token, _, rows, shard_rates, record = intents[index]
            try:
                if self.detached:
                    return self._run_detached_shard(
                        record, jobs, snapshot, cancelled, release_barrier.schedule,
                        startup_slots=startup_slots,
                        startup_cancelled=lambda: cancelled() or startup_aborted.is_set(),
                        native_preflight=native_preflight)
                batch = BatchTraffic(
                    execute=self._transport(jobs, snapshot, cancelled, shard=True),
                    fence=lambda: self._owned(snapshot),
                    native_guard=lambda observed: not cancelled() and observed == jobs
                        and self._native_guard(jobs, snapshot, shard=True),
                    diagnostic_reporter=self._diagnostic_reporter(snapshot, jobs),
                    failure_reporter=self._failure_reporter(snapshot), cancelled=cancelled,
                    release_barrier=release_barrier.schedule)
                return batch.sample(rows, token=token,
                                    reserve=lambda actual, observed: actual == token and observed == jobs,
                                    aggregate_kbps=rate_budget(shard_rates), offered_by_supi=shard_rates,
                                    profile=SHARD_PROFILE, shard_index=index, **protocol_options(workload))
            except BaseException as exc:
                # Save the cause before waking peers: their barrier/queue
                # cancellation failures must not mask it by submission order.
                with failure_lock:
                    if not first_failure:
                        first_failure.append(exc)
                startup_aborted.set()
                release_barrier.abort()
                raise
        with span('batch.workers_drain'):
            with ThreadPoolExecutor(max_workers=len(shards)) as pool:
                futures = [pool.submit(run, index) for index in range(len(shards))]
                results, errors = [], []
                for future in futures:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(None)
                        errors.append(exc)
        if errors:
            raise first_failure[0]  # All submitted workers have drained.
        receipts = [dict(roundId=snapshot['roundId'], generation=snapshot['generation'],
                         shardIndex=index, token=intents[index][0], jobs=shards[index],
                         records=results[index]['rawRecords']) for index in range(len(shards))]
        if self.detached:
            for index, receipt in enumerate(receipts):
                record = intents[index][4]
                receipt['receiverContract'] = detached_receiver_contract(
                    record['detachedReservation'], shards[index], round_id=snapshot['roundId'],
                    generation=snapshot['generation'], shard_index=index, shard_count=len(shards),
                    token=record['token'])
        with span('batch.aggregate'):
            aggregate = aggregate_shards(shards, receipts, round_id=snapshot['roundId'],
                                         generation=snapshot['generation'], now=time.time(),
                                         execution_mode='detached-shard-v1' if self.detached else 'attached',
                                         observer_boottime=detached_boottime() if self.detached else None)
        with span('batch.reports'):
            owners = {row['supi']: row for row in snapshot['perUe']}
            reports = []
            for sample in aggregate['samples']:
                job = sample['job']
                receiver = receiver_summary(sample['server'] if job.direction == 'ul' else sample['client'])
                reports.append({**{key: value for key, value in sample.items() if key != 'job'},
                    'binding': {**owners[job.supi], 'ip': job.ue_ip, 'generation': job.generation},
                    'direction': job.direction, 'receiverPodUid': job.receiver_uid,
                    'endpoint': job.endpoint, 'port': job.port, 'durationSeconds': job.duration,
                    'offeredTargetMbps': job.offered_kbps / 1000, 'receivedBytes': receiver['bytes'],
                    'source': 'native-iperf3-receiver'})
            if workload.get('serviceContractRevision'):
                from backend.onprem.evidence.sla import native_intervals
                for report in reports:
                    report['slaIntervals'] = native_intervals(report['client'], report['server'], report['direction'],
                        started_at=report['effectiveStartedAt'], finished_at=report['effectiveFinishedAt'])
        return {'shardActionIds': [item[1] for item in intents],
                'shardReceipts': [{'actionId': intents[i][1], 'token': intents[i][0],
                                   'shardIndex': i, 'cleanupVerified': results[i]['cleanupVerified']}
                                  for i in range(len(shards))],
                'aggregate': {key: aggregate[key] for key in
                              ('effectiveStartedAt', 'effectiveFinishedAt', 'flowCount', 'source', 'accountingObservation')
                              if key in aggregate},
                'reports': reports, 'jobs': [asdict(job) for shard in shards for job in shard],
                'workload': workload, 'cleanupVerified': True}

    def _run_detached_shard(self, record, jobs, snapshot, cancelled, release_schedule=None,
                            *, startup_slots=None, startup_cancelled=None, native_preflight=None):
        """Run one opt-in shard through bounded, short detached control execs."""
        roles = [(row["role"], row["pod"], row["podUid"])
                 for row in record["detachedReservation"]["roles"]]
        if len(set(roles)) != len(roles) or not roles:
            raise RoundRuntimeBlocked("Detached shard role set is invalid")
        receiver_contract = detached_receiver_contract(record['detachedReservation'], jobs,
            round_id=snapshot['roundId'], generation=snapshot['generation'],
            shard_index=record['shardIndex'], shard_count=record['shardCount'], token=record['token'])
        if startup_slots is None:
            startup_slots = _ReceiverStartupSlots(uid for role, _, uid in roles if role == "server")
        preparation_started = time.monotonic()
        preparation_marks, preparation_controls, timing_lock = {}, [], Lock()
        timeline = PhaseTimeline(record['shardIndex'])
        timeline.advance('shard.native_preflight')
        def control(action_name, identity, *, epoch=0):
            role, name, uid = identity
            requested_counter, requested_phase = time.perf_counter(), phase
            requested_at, dispatched_at, accepted = time.monotonic(), None, False
            def dispatch():
                nonlocal dispatched_at, accepted
                dispatched_at = time.monotonic()
                result = self._detached_control(action_name, record, jobs, snapshot,
                                                role=role, name=name, uid=uid, epoch=epoch,
                                                _defer_postflight=action_name == 'start' and role == 'server')
                accepted = not callable(result)
                return result
            def finish(completion):
                nonlocal accepted
                result = completion()
                accepted = True
                return result
            try:
                if action_name == "start" and role == "server":
                    return startup_slots.run(
                        uid, record["detachedReservation"], cancelled=startup_cancelled or cancelled,
                        owned=lambda: self._owned(snapshot), dispatch=dispatch,
                        finish=finish)
                return dispatch()
            finally:
                if timeline.recorder is not None:
                    finished = time.monotonic()
                    timeline.recorder.record('control.'+action_name, requested_counter,
                        status='ok' if accepted else 'error', shardIndex=record['shardIndex'],
                        role=role, phaseAtRequest=requested_phase, dispatched=dispatched_at is not None,
                        queueMs=((dispatched_at if dispatched_at is not None else finished)-requested_at)*1000,
                        dispatchMs=(finished-dispatched_at)*1000 if dispatched_at is not None else 0.)
                if action_name in {"collect", "cancel"}:
                    logger.warning('Detached postflight timing control generation=%s stage=%s shard=%s phase=%s action=%s role=%s '
                                'elapsedSeconds=%.6f accepted=%s',
                                snapshot.get("generation"), snapshot.get("stage"), record.get("shardIndex"), phase, action_name, role,
                                time.monotonic() - requested_at, accepted)
                if action_name in {"start", "status"} and phase == "receiver_prepare":
                    finished_at = time.monotonic()
                    with timing_lock:
                        preparation_controls.append({"action": action_name, "role": role, "podUid": uid,
                            "requestedAfterSeconds": requested_at - preparation_started,
                            "queueSeconds": (dispatched_at if dispatched_at is not None else finished_at) - requested_at,
                            "dispatchSeconds": finished_at - dispatched_at if dispatched_at is not None else 0.,
                            "dispatched": dispatched_at is not None, "accepted": accepted})
        def all_controls(action_name, *, epoch=0, identities=None):
            selected_roles = roles if identities is None else identities
            def invoke(identity):
                if timeline.recorder is not None:
                    with timeline.recorder.bind():
                        return control(action_name, identity, epoch=epoch)
                return control(action_name, identity, epoch=epoch)
            with ThreadPoolExecutor(max_workers=min(4, len(selected_roles))) as pool:
                futures = [pool.submit(invoke, identity) for identity in selected_roles]
                return [future.result() for future in futures]
        diagnostic = self._diagnostic_reporter(snapshot, jobs)
        failure = self._failure_reporter(snapshot)
        records = {}
        phase = "native_preflight"
        def checkpoint(checkpoint_phase):
            values = lambda name: [row[name] for row in records.values()
                                   if type(row.get(name)) in (int, float) and math.isfinite(row[name])]
            timing = {"observerNow": time.time(), "observerBoottime": detached_boottime()}
            for name in ("startedAt", "finishedAt", "receiverStartedAt", "receiverFinishedAt"):
                observed = values(name)
                timing[name + "Min"] = min(observed) if observed else None
                timing[name + "Max"] = max(observed) if observed else None
            checkpoint_started = time.monotonic()
            checkpoint_counter = time.perf_counter()
            accepted = False
            try:
                accepted = diagnostic(record["token"], checkpoint_phase, records, timing) is True
                if not accepted:
                    raise RoundRuntimeBlocked("Detached diagnostic checkpoint failed")
            except Exception as error:
                code = getattr(error, 'sqlite_errorcode', None) if isinstance(error, sqlite3.Error) else None
                name = getattr(error, 'sqlite_errorname', None) if isinstance(error, sqlite3.Error) else None
                code = code if type(code) is int and 0 <= code <= 2**31 - 1 else None
                if (type(name) is not str or not re.fullmatch(
                        r'SQLITE_(?:BUSY|LOCKED|READONLY|FULL|IOERR|CANTOPEN|CORRUPT|NOTADB|'
                        r'PROTOCOL|NOMEM|INTERRUPT|ERROR|INTERNAL|PERM|ABORT)(?:_[A-Z0-9]+)?', name)
                        or code is None or getattr(sqlite3, name, None) != code):
                    name = None
                category = type(error).__name__
                try:
                    transaction_phase = getattr(error, 'onprem_transaction_phase', None)
                    if (type(transaction_phase) is not str
                            or transaction_phase not in {'configure', 'begin', 'body', 'commit'}):
                        transaction_phase = 'unavailable'
                    transaction_elapsed = getattr(error, 'onprem_transaction_elapsed_ms', None)
                    if (type(transaction_elapsed) not in (int, float)
                            or not 0 <= transaction_elapsed <= 86400000
                            or not math.isfinite(transaction_elapsed)):
                        transaction_elapsed = None
                    logger.warning('Detached checkpoint failed generation=%s shard=%s phase=%s '
                        'errorType=%s sqliteErrorCode=%s sqliteErrorName=%s '
                        'transactionPhase=%s transactionElapsedMs=%s',
                        snapshot.get('generation'), record.get('shardIndex'), checkpoint_phase,
                        category if category in {'OperationalError', 'DatabaseError', 'IntegrityError',
                            'ProgrammingError', 'RoundRuntimeBlocked', 'RoundConflict', 'ValueError'} else 'other',
                        code, name, transaction_phase, transaction_elapsed)
                except Exception:
                    pass
                raise
            finally:
                if timeline.recorder is not None:
                    timeline.recorder.record('checkpoint.persist', checkpoint_counter,
                        status='ok' if accepted else 'error', shardIndex=record['shardIndex'],
                        checkpointPhase=checkpoint_phase)
                logger.warning('Detached postflight timing checkpoint generation=%s stage=%s shard=%s phase=%s '
                            'elapsedSeconds=%.6f accepted=%s',
                            snapshot.get("generation"), snapshot.get("stage"), record.get("shardIndex"), checkpoint_phase,
                            time.monotonic() - checkpoint_started, accepted)
        start_attempted = False
        primary_exc = None
        try:
            preparation_marks["reservationAgeAtPreflightSeconds"] = max(
                0., detached_boottime() - record["detachedReservation"]["createdMonotonic"])
            if (not self._owned(snapshot) or cancelled()
                    or record['detachedReservation']['clockBootId'] != detached_clock_boot_id()
                    or (native_preflight(jobs) if native_preflight is not None else
                        self._native_guard(jobs, snapshot, shard=True)) is not True):
                raise RoundRuntimeBlocked("Detached native preflight changed")
            phase = "receiver_prepare"
            timeline.advance('shard.start')
            preparation_marks["nativePreflightSeconds"] = time.monotonic() - preparation_started
            start_attempted = True
            started = all_controls("start")
            preparation_marks["allStartsAfterSeconds"] = time.monotonic() - preparation_started
            if any(row["bootId"] != selected["bootId"] or row["netns"] != selected["netns"]
                   for row, selected in zip(started, record["detachedReservation"]["roles"])):
                raise RoundRuntimeBlocked("Detached start acknowledgement identity changed")
            timeline.advance('shard.readiness')
            statuses = all_controls("status")
            preparation_marks["allStatusesAfterSeconds"] = time.monotonic() - preparation_started
            if any(row["state"] != ("LISTENING" if identity[0] == "server" else "ARMED")
                   or row["releaseEpoch"] is not None for identity, row in zip(roles, statuses)):
                raise RoundRuntimeBlocked("Detached shard did not arm exactly once")
            phase = "client_measurement"
            timeline.advance('shard.release_barrier')
            if release_schedule is None:
                raise RoundRuntimeBlocked("Detached common release barrier is unavailable")
            mono_before = detached_boottime()
            wall = time.time()
            mono_after = detached_boottime()
            release_epoch, release_deadline = release_schedule(
                wall=wall, mono_before=mono_before, mono_after=mono_after,
                child_deadline=record["detachedReservation"]["clientChildDeadline"],
                transport_deadline=record["detachedReservation"]["transportDeadline"],
                duration=SHARD_DURATION_SECONDS)
            if cancelled():
                raise RoundRuntimeBlocked("Detached shard cancelled before release")
            if detached_boottime() >= release_deadline:
                raise RoundRuntimeBlocked("Detached common release dispatch deadline expired")
            client_roles = [identity for identity in roles if identity[0] == "client"]
            timeline.advance('shard.release_dispatch')
            released = all_controls("release", epoch=release_epoch, identities=client_roles)
            if any(row["releaseEpoch"] != release_epoch for row in released):
                raise RoundRuntimeBlocked("Detached shard release epoch changed")
            deadline = record["detachedReservation"]["transportDeadline"]
            timeline.advance('shard.wait_sealed')
            while True:
                if cancelled() or detached_boottime() >= deadline:
                    raise RoundRuntimeBlocked("Detached shard completion deadline expired")
                statuses = all_controls("status")
                states = {row["state"] for row in statuses}
                if "FAILED" in states or "CANCELLED" in states:
                    raise RoundRuntimeBlocked("Detached shard supervisor failed")
                if states == {"SEALED"}:
                    break
                time.sleep(.1)
            timeline.advance('shard.collect')
            collected = all_controls("collect")
            timeline.advance('shard.decode')
            by_key = {}
            for identity, receipt in zip(roles, collected):
                role, _, uid = identity
                expected = {job.key for job in jobs
                            if (job.receiver_uid if role == "server" else job.ue_uid) == uid}
                if receipt["count"] != len(expected):
                    raise RoundRuntimeBlocked("Detached sealed record count changed")
                parsed = parse_worker_output(receipt["batchOutput"], expected)
                for key, row in parsed.items():
                    tuning = record["detachedReservation"].get("trafficTuning")
                    if record["detachedReservation"]["schemaVersion"] == TUNED_SCHEMA:
                        verify_report_tuning(row["report"], tuning)
                    item = by_key.setdefault(key, {"job": next(job for job in jobs if job.key == key)})
                    if role in item:
                        raise RoundRuntimeBlocked("Detached duplicate role result")
                    item[role] = row
            if set(by_key) != {job.key for job in jobs} or any(set(row) != {"job", "client", "server"}
                                                               for row in by_key.values()):
                raise RoundRuntimeBlocked("Detached shard result set is incomplete")
            records = {key: {"job": row["job"], "client": row["client"]["report"],
                             "server": row["server"]["report"],
                             "clientExit": row["client"]["exit"], "serverExit": row["server"]["exit"],
                             "startedAt": row["client"]["startedAt"],
                             "finishedAt": row["client"]["finishedAt"],
                             "receiverStartedAt": row["server"]["startedAt"],
                             "receiverFinishedAt": row["server"]["finishedAt"]}
                       for key, row in by_key.items()}
            # Validate the acquired records before native postflight/diagnostic
            # I/O consumes their acquisition-age budget. This is not publication:
            # ownership, native identity, durable raw receipts, closure, and the
            # existing final freshness check must still all succeed below.
            phase = "evidence_reduce"
            timeline.advance('shard.reduce_acquired')
            reduce_evidence(jobs, records, now=time.time(), profile=SHARD_PROFILE,
                             maximum_age=SHARD_EVIDENCE_MAX_AGE_SECONDS, receiver_contract=receiver_contract,
                             observer_boottime=detached_boottime())
            phase = "native_postflight"
            timeline.advance('shard.postflight_checkpoint')
            checkpoint(phase)
            timeline.advance('shard.native_postflight')
            native_started = time.monotonic()
            native_accepted = False
            try:
                native_accepted = (self._owned(snapshot)
                                   and self._native_guard(jobs, snapshot, shard=True) is True)
                if not native_accepted:
                    raise RoundRuntimeBlocked("Detached native postflight changed")
            finally:
                logger.warning('Detached postflight timing native generation=%s stage=%s shard=%s elapsedSeconds=%.6f accepted=%s',
                            snapshot.get("generation"), snapshot.get("stage"), record.get("shardIndex"),
                            time.monotonic() - native_started, native_accepted)
            phase = "evidence_reduce"
            timeline.advance('shard.closure_checkpoint')
            checkpoint(phase)
            timeline.advance('shard.cleanup')
            closures = all_controls("cancel")
            if len(closures) != len(roles) or any("closureDigest" not in row for row in closures):
                raise RoundRuntimeBlocked("Detached shard closure is incomplete")
            start_attempted = False
            phase = "final_freshness"
            timeline.advance('shard.freshness_checkpoint')
            checkpoint(phase)
            timeline.advance('shard.final_freshness')
            reduce_evidence(jobs, records, now=time.time(), profile=SHARD_PROFILE,
                            maximum_age=SHARD_POST_CLEANUP_MAX_AGE_SECONDS, receiver_contract=receiver_contract,
                            observer_boottime=detached_boottime())
            return {"rawRecords": records, "cleanupVerified": True}
        except Exception as exc:
            primary_exc = exc
            timeline.advance('shard.failure_diagnostics', status='error')
            logger.warning('Detached shard preparation failed roundId=%s generation=%s shard=%s phase=%s '
                           'errorType=%s elapsedSeconds=%.3f timing=%s controls=%s',
                           snapshot.get("roundId"), snapshot.get("generation"), record.get("shardIndex"), phase,
                           type(exc).__name__, time.monotonic()-preparation_started,
                           json.dumps(preparation_marks, sort_keys=True), json.dumps(preparation_controls, sort_keys=True))
            code = getattr(exc, "code", "batch.worker_failed")
            if code not in BATCH_ERROR_CODES:
                code = "batch.worker_failed"
            exits = [{"key": key, **{name: row[name] for name in ("clientExit", "serverExit")
                                      if type(row.get(name)) is int}}
                     for key, row in records.items()]
            detached = getattr(exc, "detached_diagnostic", None)
            if isinstance(detached, dict):
                exits.append(deepcopy(detached))
            budget = getattr(exc, "release_budget_diagnostic", None)
            if isinstance(budget, dict):
                exits.append(deepcopy(budget))
            exits.append({"kind": "detached_preparation_timing",
                **{key: preparation_marks.get(key) for key in
                   ("reservationAgeAtPreflightSeconds", "nativePreflightSeconds", "allStartsAfterSeconds", "allStatusesAfterSeconds")},
                "elapsedSeconds": time.monotonic()-preparation_started,
                "controlTimings": deepcopy(preparation_controls)})
            try:
                checkpoint(phase)
                failure(record["token"], phase, code, exits)
            except Exception:
                exc.diagnostic_persistence_failed = True
            raise
        finally:
            timeline.advance('shard.emergency_cleanup')
            if start_attempted:
                try:
                    closures = all_controls("cancel")
                    if len(closures) != len(roles):
                        raise RoundRuntimeBlocked("Detached shard closure is incomplete")
                except Exception as cleanup_exc:
                    if primary_exc is None:
                        raise
                    diagnostic = getattr(cleanup_exc, "detached_diagnostic", None)
                    secondary = [deepcopy(diagnostic)] if isinstance(diagnostic, dict) else []
                    try:
                        failure(record["token"], "cleanup", "batch.cleanup_unverified", secondary)
                    except Exception:
                        primary_exc.diagnostic_persistence_failed = True
            timeline.close('error' if primary_exc is not None else 'ok')

    def _diagnostic_reporter(self, snapshot, jobs):
        """Persist bounded unvalidated worker receipts, never promote evidence."""
        expected = {j.key: j for j in jobs}
        from backend.onprem.evidence.batch_receipts import BatchCheckpoint
        fields = {"job", "client", "server", "clientExit", "serverExit", "startedAt", "finishedAt",
                  "receiverStartedAt", "receiverFinishedAt"}
        raw_cache, raw_sha = None, None
        manifest = [asdict(j) for j in jobs]
        def persist(token, phase, records, diagnostics):
            nonlocal raw_cache, raw_sha
            if (not BatchCheckpoint.valid(phase, diagnostics) or not isinstance(records, dict) or len(records) > 100
                    or set(records) - expected.keys()):
                return False
            raw = {}
            for key, row in records.items():
                if not isinstance(row, dict) or set(row) - fields or row.get("job") != expected[key]:
                    return False
                item = {k: v for k, v in row.items() if k != "job"}
                if raw_cache is not None:
                    raw[key] = item
                    continue
                for name, value in item.items():
                    if name in {"client", "server"}:
                        if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 128 * 1024:
                            return False
                    elif name.endswith("Exit"):
                        if type(value) is not int or not -128 <= value <= 255:
                            return False
                    elif value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                        return False
                raw[key] = item
            from backend.onprem.evidence.batch_receipts import same_json
            if raw_cache is not None and not same_json(raw_cache, raw):
                return False  # Even nested bool/int substitutions are changes.
            if raw_cache is None and len(json.dumps(raw, allow_nan=False).encode()) > 8 * 1024 * 1024:
                return False
            try:
                receipt = self.ledger.batch_checkpoint(snapshot['roundId'], snapshot['generation'], token,
                    manifest, phase, diagnostics, raw=raw if raw_cache is None else None, raw_sha=raw_sha)
            except (RoundConflict, ValueError):
                return False
            if raw_cache is None:
                raw_cache, raw_sha = deepcopy(raw), receipt['rawSha256']
            return True
        return persist

    def _failure_reporter(self, snapshot):
        def persist(token, phase, code, exits):
            try:
                scope = self.ledger.batch_failure_scope(snapshot['roundId'], snapshot['generation'], token)
                self.ledger.record_batch_failure(snapshot['roundId'], snapshot['generation'], token,
                    scope, phase, code, exits)
                return True
            except (RoundConflict, ValueError):
                return False
        return persist

    def _sample(self, snapshot, reserve, cancelled):
        bindings = sorted(self._bindings(snapshot), key=lambda row: row["supi"])
        # Requested and applied loads are distinct, explicit and frozen before
        # any remote operation. No caller-selected profile or hidden scaling.
        workload = plan_for_stage(snapshot, current_only=True)
        rates = applied_rates(workload)
        budget = max(workload["appliedUplinkKbps"], workload["appliedDownlinkKbps"])
        jobs = build_jobs(bindings, aggregate_kbps=budget, offered_by_supi=rates, **protocol_options(workload))
        token = uuid4().hex
        def persist(actual_token, actual_jobs):
            if actual_token != token or actual_jobs != jobs:
                raise RoundRuntimeBlocked("Batch intent changed during reservation")
            return reserve(token, deepcopy(bindings), [asdict(j) for j in jobs], rates, budget,
                           workload=deepcopy(workload))
        batch = BatchTraffic(execute=self._transport(jobs, snapshot, cancelled), fence=lambda: self._owned(snapshot),
                             native_guard=lambda observed: not cancelled() and observed == jobs and self._native_guard(jobs, snapshot),
                             diagnostic_reporter=self._diagnostic_reporter(snapshot, jobs),
                             failure_reporter=self._failure_reporter(snapshot), cancelled=cancelled)
        result = batch.sample(bindings, token=token, reserve=persist, aggregate_kbps=budget, offered_by_supi=rates, **protocol_options(workload))
        samples = reduce_evidence(jobs, result["rawRecords"], now=time.time())
        owners = {r["supi"]: r for r in snapshot["perUe"]}
        reports = []
        for sample in samples:
            job = sample["job"]
            receiver = receiver_summary(sample["server"] if job.direction == "ul" else sample["client"])
            reports.append({**{k: v for k, v in sample.items() if k != "job"},
                "binding": {**owners[job.supi], "ip": job.ue_ip, "generation": job.generation},
                "direction": job.direction, "receiverPodUid": job.receiver_uid,
                "endpoint": job.endpoint, "port": job.port, "durationSeconds": job.duration,
                "offeredTargetMbps": job.offered_kbps / 1000, "receivedBytes": receiver["bytes"],
                "source": "native-iperf3-receiver"})
        if workload.get('serviceContractRevision'):
            from backend.onprem.evidence.sla import native_intervals
            for report in reports:
                report['slaIntervals'] = native_intervals(report['client'], report['server'], report['direction'],
                    started_at=report['effectiveStartedAt'], finished_at=report['effectiveFinishedAt'])
        return {"token": token, "reports": reports, "jobs": [asdict(j) for j in jobs],
                "workload": workload, "cleanupVerified": result["cleanupVerified"]}

    async def cleanup(self, snapshot):
        return await asyncio.to_thread(self._cleanup, deepcopy(snapshot))

    def _cleanup(self, snapshot):
        for record in snapshot.get("actions", []):
            if record.get("kind") != "traffic_window" or record.get("mode") != "batch" or record.get("cleanupVerified") is True:
                continue
            validate_record_workload(record, snapshot, allow_legacy=True)
            shard = record.get('shardIndex') is not None
            jobs = build_jobs(record["bindings"], aggregate_kbps=record["aggregateKbps"],
                              offered_by_supi=record["offeredBySupi"],
                              profile=SHARD_PROFILE if shard else LEGACY_PROFILE,
                              shard_index=record['shardIndex'] if shard else 0,
                              **protocol_options(record.get('workload', {})))
            if [asdict(j) for j in jobs] != record.get("jobs"):
                raise RoundRuntimeBlocked("Durable batch job identities are inconsistent")
            if record.get("executionMode") == "detached-shard-v1":
                if not shard or not validate_detached_reservation(
                        record.get("detachedReservation"), round_id=snapshot.get("roundId"),
                        generation=snapshot.get("generation"), shard_index=record.get("shardIndex"),
                        shard_count=record.get("shardCount"),
                        token=record.get("token"), jobs=jobs):
                    raise RoundRuntimeBlocked("Detached batch reservation identity changed")
                roles = [(row["role"], row["pod"], row["podUid"])
                         for row in record["detachedReservation"]["roles"]]
                # Recovery is observational plus exact closure. It never
                # recreates a supervisor or publishes a release epoch.
                for role, name, uid in roles:
                    try:
                        status = self._detached_control("status", record, jobs, snapshot, role=role,
                                                        name=name, uid=uid, recovery=True)
                        if status["state"] == "SEALED":
                            self._detached_control("collect", record, jobs, snapshot, role=role,
                                                   name=name, uid=uid, recovery=True)
                    except (RoundRuntimeBlocked, PolicyBlocked):
                        # A crash may leave only the durable outer reservation.
                        # Exact cancel creates/verifies its closed tombstone.
                        pass
                closures = [self._detached_control("cancel", record, jobs, snapshot, role=role,
                                                   name=name, uid=uid, recovery=True)
                            for role, name, uid in roles]
                if len(closures) != len(roles) or any("closureDigest" not in row for row in closures):
                    raise RoundRuntimeBlocked("Detached recovery closure is incomplete")
            elif "detachedReservation" in record or record.get("executionMode") is not None:
                raise RoundRuntimeBlocked("Unknown batch execution reservation")
            else:
                batch = BatchTraffic(execute=self._transport(jobs, snapshot, shard=shard),
                                     fence=lambda: self._owned(snapshot), native_guard=lambda _: False)
                batch.recover(record["bindings"], token=record["token"],
                              profile=SHARD_PROFILE if shard else LEGACY_PROFILE,
                              jobs=record["jobs"] if shard or record.get("workload", {}).get("trafficMode") == "qos" else None,
                              **protocol_options(record.get('workload', {})))
            # Persist recovery before a normal UE stop removes these exact Pods.
            current = read_cleanup(self.ledger)
            matches = [a for a in current['actions'] if a.get('id') == record['id'] and a.get('token') == record['token']]
            if len(matches) != 1:
                raise RoundRuntimeBlocked('Batch cleanup ownership changed')
            mutate_current(self.ledger, action_id=record['id'], action_patch={'cleanupVerified': True})
        return True
