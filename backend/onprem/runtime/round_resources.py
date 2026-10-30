"""Native UE, Pod, receiver and release evidence adapter for scenario execution."""
from __future__ import annotations
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import logging
import subprocess
import time
from uuid import uuid4
from backend.onprem.scenarios.contracts import slice_counts
from backend.onprem.runtime.ue import ENDPOINTS, SLICES, EvidenceUnavailable, seconds, timestamp
from backend.onprem.runtime.kubeadm import TRAFFIC_IMAGE
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows, session_identity
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.capability import ReceiverWindowBlocked
from backend.onprem.runtime.errors import RoundRuntimeBlocked, ReleaseProofBlocked, RequestGuardDiagnostic

def native_window_reason(exc):
    """Classify only exact local failures; never persist subprocess output."""
    if type(exc) in (PolicyBlocked, ReceiverWindowBlocked):
        return {
            "Scoped traffic command failed; no retry": "traffic.command_failed",
            "Traffic Pod UID changed or is terminating": "traffic.pod_uid_changed",
            "Pinned traffic sidecar is not available": "traffic.sidecar_missing",
            "Fresh native PDU/TUN binding required": "traffic.binding_stale",
            "Traffic sample lost native ownership or failed": "traffic.ownership_or_iperf_failed",
            "iperf did not use the exact UE/N6 binding": "traffic.binding_mismatch",
            "Unexpected traffic direction/protocol": "traffic.direction_mismatch",
            "Incomplete receiver measurement window": "traffic.receiver_window_incomplete",
            "iperf receiver reported an error": "traffic.receiver_report_error",
            "Missing actual receiver summary": "traffic.receiver_summary_missing",
            "Invalid receiver measurements": "traffic.receiver_measurements_invalid",
            "Empty or inconsistent receiver window": "traffic.receiver_empty_or_inconsistent",
            "Missing actual sender summary": "traffic.sender_summary_missing",
            "Invalid actual offered-load measurements": "traffic.sender_measurements_invalid",
        }.get(str(exc), "traffic.policy_blocked")
    if type(exc) is subprocess.TimeoutExpired:
        return "traffic.command_timeout"
    if type(exc) is json.JSONDecodeError:
        return "traffic.result_json_invalid"
    if type(exc) is RoundRuntimeBlocked:
        return {
            "Traffic job no longer owns the immutable round lease": "round.lease_changed",
            "Receiver window identity or request does not match": "traffic.receipt_identity_mismatch",
            "Receiver window requires finite nonnegative measurements": "traffic.receipt_measurements_invalid",
            "Receiver window is stale, future-dated or incomplete": "traffic.receipt_window_invalid",
        }.get(str(exc), "round.guard_rejected")
    if type(exc) is OSError:
        return "traffic.io_error"
    return "traffic.unexpected_failure"

RESIDENT_FRESHNESS_ERRORS = frozenset({
    "Existing UE fleet lacks fresh identity, registration or PDU proof",
    "Existing UE fleet lacks fresh identity, registration, PDU or traffic proof",
})

def native_receiver_pods(pods, namespace):
    """Resolve the actual renderer's N6 identity; never infer from Pod prefixes."""
    result = {}
    uids = set()
    for name in SLICES:
        matches = [pod for pod in pods
                   if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "n6-" + name
                   and pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}]
        if len(matches) != 1:
            raise RoundRuntimeBlocked("Every slice requires exactly one native N6 receiver")
        pod = matches[0]
        meta, labels = pod["metadata"], pod["metadata"].get("labels", {})
        uid = meta.get("uid")
        if (labels.get("cityverse.io/runtime") != "kubeadm" or labels.get("cityverse.io/role") != "upf"
                or meta.get("namespace") != namespace or not isinstance(uid, str) or not uid or uid in uids
                or not isinstance(meta.get("name"), str) or not meta["name"].startswith("n6-" + name + "-")
                or meta.get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running"
                or not any(c.get("name") == "traffic" and c.get("image") == TRAFFIC_IMAGE
                           for c in pod.get("spec", {}).get("containers", []))):
            raise RoundRuntimeBlocked("Native N6 receiver ownership, UID or pinned traffic image is unverified")
        result[name] = pod
        uids.add(uid)
    return result

def fresh(value, now=None):
    try:
        return -1 <= (time.time() if now is None else now) - seconds(value) <= 15
    except (TypeError, ValueError):
        return False

def resident_stable_window(status, created):
    """Use observed network time, not the renewing outage-watchdog checkpoint."""
    try:
        observed = seconds(status.get("observedAt"))
        stable = seconds(status.get("stableSince"))
        return (type(created) in (int, float) and math.isfinite(created)
                and created <= stable <= observed and observed - stable >= 60)
    except (TypeError, ValueError, OverflowError):
        return False

def action(kind, **details):
    return {"id": str(uuid4()), "kind": kind, "requested": True,
            "accepted": False, "installed": False, "effectVerified": False,
            "requestedAt": timestamp(), **details}

def failure_code(exc):
    # Fixed local validation strings only. Never publish arbitrary remote
    # exceptions, stderr, commands, callback URLs or credentials.
    if isinstance(exc, RoundRuntimeBlocked) and str(exc) == 'ue.registration_rejected':
        return 'ue.registration_rejected'
    batch_codes = {
        "Invalid batch native UPF epoch": "batch.native_upf_epoch_invalid",
        "Aggregate offered load must be bounded integer kbps": "batch.aggregate_budget_invalid",
        "Per-UE offered load must fit the explicit aggregate budget": "batch.per_ue_budget_invalid",
        "Incomplete or malformed batch binding": "batch.binding_malformed",
        "Native DNN/S-NSSAI/UPF identity mismatch": "batch.native_identity_mismatch",
        "Fresh complete native binding required": "batch.native_binding_unavailable",
        "Fresh native batch snapshot required": "batch.native_snapshot_unavailable",
        "Exact batch token/job intent must be durably reserved before execution": "batch.intent_not_reserved",
        "Receiver worker failed before readiness": "batch.receiver_readiness_failed",
        "Batch receiver readiness deadline exceeded": "batch.receiver_readiness_timeout",
        "Malformed, foreign or duplicate worker output": "batch.worker_output_invalid",
        "Invalid actual worker record": "batch.worker_record_invalid",
        "Incomplete worker result set": "batch.worker_result_incomplete",
        "Native binding changed during actual batch traffic": "batch.native_binding_changed",
        "Batch cleanup remains unverified": "batch.cleanup_unverified",
        "Batch windows did not overlap or remain jointly fresh": "batch.windows_not_jointly_fresh",
        "Incomplete, future or stale batch window": "batch.window_stale_or_incomplete",
    }
    source_codes = {
        "batch.receiver_socket_identity_mismatch", "batch.client_socket_identity_mismatch",
        "batch.iperf_cookie_mismatch", "batch.worker_failed", "batch.worker_result_invalid",
        "batch.worker_timestamp_invalid", "batch.receiver_timestamp_invalid",
        "batch.native_binding_changed", "batch.cleanup_unverified", "batch.window_unverified",
        "batch.offered_load_mismatch", "batch.receiver_readiness_failed", "batch.failure_persistence_failed",
        "batch.native_snapshot_unavailable",
    }
    if isinstance(exc, RoundRuntimeBlocked):
        source_code = getattr(exc, "code", None)
        if isinstance(source_code, str) and source_code in source_codes | set(batch_codes.values()):
            return source_code
    if isinstance(exc, PolicyBlocked):
        # A source may attach a structured code, but its content is still
        # untrusted until explicitly allowlisted here.
        source_code = getattr(exc, "code", None)
        if isinstance(source_code, str) and source_code in source_codes | set(batch_codes.values()):
            return source_code
        return batch_codes.get(str(exc), "policy.blocked")
    return "runtime.guard_rejected" if isinstance(exc, RoundRuntimeBlocked) else "runtime.unexpected_failure"

def safe_reason(exc):
    if isinstance(exc, PolicyBlocked):
        code = failure_code(exc)
        return "PolicyBlocked" + (" [" + code + "]" if code != "policy.blocked" else "")
    return str(exc) if isinstance(exc, RoundRuntimeBlocked) else type(exc).__name__

def validate_window(job, report, *, now=None, maximum_age=15):
    """Validate the production traffic receipt, including its exact owner/window."""
    now = time.time() if now is None else now
    if (not isinstance(report, dict) or report.get("source") != "native-iperf3-receiver"
            or report.get("binding") != job["binding"]
            or any(report.get(field) != job[key] for field, key in (
                ("direction", "direction"), ("receiverPodUid", "receiver_uid"),
                ("endpoint", "endpoint"), ("port", "port"),
                ("durationSeconds", "duration"), ("offeredTargetMbps", "offered_mbps")))):
        raise RoundRuntimeBlocked("Receiver window identity or request does not match")
    fields = ("startedAt", "finishedAt", "receivedMbps", "offeredObservedMbps",
              "receivedBytes", "packets", "lostPackets")
    if any(type(report.get(k)) not in (int, float) or not math.isfinite(report[k])
           or report[k] < 0 for k in fields):
        raise RoundRuntimeBlocked("Receiver window requires finite nonnegative measurements")
    elapsed = report["finishedAt"] - report["startedAt"]
    if (maximum_age not in (15, 35)
            or not -1 <= now - report["finishedAt"] <= maximum_age
            or not job["duration"] - 2 <= elapsed <= job["duration"] + 30
            or report["packets"] <= report["lostPackets"]
            or report["receivedBytes"] <= 0 or report["receivedMbps"] <= 0
            or report["offeredObservedMbps"] <= 0):
        raise RoundRuntimeBlocked("Receiver window is stale, future-dated or incomplete")

class NativeRoundResources:
    """Production bridge to native collector, exact Pod UIDs and iperf sidecars.

    policy_quiet must check the durable AF journal for unknown/live ownership.
    It is mandatory even for NONE: an earlier uncertain external policy may not
    be hidden by selecting a strategy that creates no additional AF policy.
    """
    subset_leasing_enabled = False
    owned_native_teardown_factory = None  # Optional private composition; legacy read-only fixtures keep the default.

    def __init__(self, controller, traffic, *, policy_quiet, pcf_absence=None, duration=5,
                 offered_mbps=None, concurrency=4,owned_native_teardown_factory=None,
                 session_workload_compiler=None):
        if controller.driver is None or not callable(policy_quiet):
            raise ValueError("Native mutation driver and durable policy guard required")
        if type(duration) is not int or not 5 <= duration <= 30:
            raise ValueError("Measurement duration must be 5–30 seconds")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("Traffic concurrency must be 1–8")
        self.controller, self.traffic, self.policy_quiet = controller, traffic, policy_quiet
        if owned_native_teardown_factory is not None and not callable(owned_native_teardown_factory):
            raise ValueError('Trusted native teardown factory must be callable')
        self.owned_native_teardown_factory=owned_native_teardown_factory
        if owned_native_teardown_factory is not None:
            controller._owned_native_factory=owned_native_teardown_factory
            controller._owned_native_snapshot_reader=lambda:self.round_reader()
        # Internal exact-round exception is installed only for an owned fixture.
        # The public zero-argument admission guard remains unchanged.
        self.fixture_policy_quiet = None
        self.fixture_cleanup_quiet = None
        self.round_reader = None
        self.control_reader = None
        self.request_cancel_check = None
        self.pcf_absence = pcf_absence
        self.batch = None  # Separate opt-in provider; legacy sequential default.
        from backend.onprem.traffic.session_workload import SessionWorkloadCompiler
        if session_workload_compiler is not None and not isinstance(session_workload_compiler,SessionWorkloadCompiler):
            raise ValueError('Trusted session workload compiler required')
        self.session_workload_compiler = session_workload_compiler
        self.service_routes = None  # Set only by the server-owned native route factory.
        self.duration, self.concurrency = duration, concurrency
        from backend.onprem.scenarios.profiles import DEFAULT_TRAFFIC
        self.offered = offered_mbps or {name:max(row['offeredUplinkMbpsPerUe'],row['offeredDownlinkMbpsPerUe']) for name,row in DEFAULT_TRAFFIC.items()}
        if set(self.offered) != set(DEFAULT_TRAFFIC) or any(
                type(n) not in (int, float) or not .001 <= n <= 20 for n in self.offered.values()):
            raise ValueError("A bounded offered-load profile is required for every group")

    def _pods(self):
        reader = getattr(self.controller.driver, 'read_pods', None)
        return (reader() if callable(reader) else
                self.controller.driver.kubectl("get", "pods", "-o", "json"))["items"]

    def _traffic_absent(self, pods):
        # Observed process absence, not merely a completed kubectl connection.
        check = "for p in /proc/[0-9]*/comm; do [ -r \"$p\" ] || continue; read -r n < \"$p\"; [ \"$n\" != iperf3 ] || exit 1; done; printf absent"
        receivers = native_receiver_pods(pods, self.controller.driver.namespace)
        managed = [pod for pod in pods if pod["metadata"].get("labels", {}).get("cityverse.io/managed-by") == "onprem-ue-controller"]
        def check_one(pod):
            name, uid = pod["metadata"]["name"], pod["metadata"].get("uid")
            if not uid or not any(c.get("name") == "traffic" and c.get("image") == TRAFFIC_IMAGE for c in pod["spec"]["containers"]):
                raise RoundRuntimeBlocked("Traffic process owner or pinned sidecar is unavailable")
            before = self.traffic.pod(name, uid)
            batch = getattr(self, 'batch', None)
            manager = getattr(batch, 'manager_transport', None)
            if manager is not None:
                raw = manager.absence(before)
            else:
                raw = self.traffic._run(["exec", name, "-c", "traffic", "--", "sh", "-c", check])
            after = self.traffic.pod(name, uid)
            if manager is not None and batch._traffic_epoch(before) != batch._traffic_epoch(after):
                raise RoundRuntimeBlocked('Traffic manager container epoch changed during absence check')
            if raw.strip() != "absent":
                return False
            return True
        # Independent fixed-scope reads; each keeps both exact-UID fences.
        # Waiting for all futures also prevents an early false result from
        # leaving unobserved background commands after cleanup returns.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(check_one, [*receivers.values(), *managed]))
        return all(results)

    def _ran_context_proof(self):
        observe = getattr(self.controller.driver, "observe_gnb_contexts", None)
        if not callable(observe):
            raise ReleaseProofBlocked("release.ran_context_unavailable")
        profile=getattr(self.controller.driver,'device_profile',None)
        if profile is not None and profile.driver == 'ueransim':
            from backend.onprem.evidence.device_ran import NativeDeviceRanReader,verified_isolated_contexts
            reader=getattr(self.controller.driver,'device_ran_reader',None)
            if not isinstance(reader,NativeDeviceRanReader) or reader.driver is not self.controller.driver:
                raise ReleaseProofBlocked('release.ran_context_unavailable')
            try:
                proof=observe()
                verified=verified_isolated_contexts(proof,profile,reader.groups)
            except (EvidenceUnavailable,ValueError,KeyError,TypeError):
                raise ReleaseProofBlocked('release.ran_context_unavailable') from None
            if verified['ueCount']!=0:
                raise ReleaseProofBlocked('release.ran_contexts_remain')
            return {'metadata':{'source':proof['source'],'scope':proof['scope'],
                        'observedAt':timestamp(min(verified['observed'])),
                        'uniqueGnBCount':verified['uniqueGnBCount'],'ueCount':verified['ueCount'],
                        'sharedGnBs':verified['sharedGnBs'],'readouts':deepcopy(proof['readouts'])},
                    'observed':verified['observed']}
        try:
            # Free-RAN provides independently UID-fenced reads for all four
            # gNBs. Run them concurrently so the oldest observation remains
            # within the 15-second release-proof freshness budget on WSL.
            proof = observe(parallel=True) if getattr(self.controller.driver, "backend", None) == "free-ran-ue" else observe()
        except EvidenceUnavailable:
            raise ReleaseProofBlocked("release.ran_context_unavailable") from None
        expected_source = ("free-ran-ue-gnb-api-ue-count"
                           if getattr(self.controller.driver, "backend", None) == "free-ran-ue"
                           else "native-gnb-nr-cli-ue-count")
        if (not isinstance(proof, dict) or proof.get("source") != expected_source
                or proof.get("scope") != "four-kubeadm-gnb-pods"
                or not isinstance(proof.get("readouts"), dict)
                or set(proof.get("readouts", {})) != set(SLICES)):
            raise ReleaseProofBlocked("release.ran_context_unavailable")
        readouts, observed = proof["readouts"], []
        counts = {}
        for name in SLICES:
            row = readouts.get(name, {})
            count = row.get("ueCount")
            if type(count) is not int or count < 0 or not fresh(row.get("observedAt")):
                raise ReleaseProofBlocked("release.ran_context_unavailable")
            counts[name] = count
            observed.append(seconds(row["observedAt"]))
        if any(counts.values()):
            raise ReleaseProofBlocked("release.ran_contexts_remain")
        return {"metadata": {"source": proof["source"], "scope": proof["scope"],
                             "observedAt": timestamp(min(observed)),
                             "counts": counts, "readouts": deepcopy(readouts)},
                "observed": observed}

    async def release_proof(self, snapshot=None):
        return await asyncio.to_thread(self._release_proof, snapshot)

    def _release_proof(self, snapshot):
        current = self.controller.store.current()
        generation = current["generation"] if current else 0
        if current and any(current["desired"].values()):
            raise RoundRuntimeBlocked("Native generation still requests UEs")
        if not callable(self.pcf_absence):
            raise ReleaseProofBlocked("release.pcf_observation_unavailable")
        # These independent bounded readers share one freshness budget. Preserve
        # their source clocks and drain every future, including failure exits.
        # A serial RAN/process scan after native reads can expire valid evidence.
        pods_observed = time.time()
        pods = self._pods()
        def timed(read):
            started = time.time()
            return read(), started
        with ThreadPoolExecutor(max_workers=5) as pool:
            native_future = pool.submit(self.controller.adapter.observe_release, generation)
            pcf_future = pool.submit(self.pcf_absence, snapshot)
            ran_future = pool.submit(self._ran_context_proof)
            quiet_future = pool.submit(timed, self.policy_quiet)
            traffic_future = pool.submit(timed, lambda: self._traffic_absent(pods))
            evidence, pcf = native_future.result(), pcf_future.result()
            ran = ran_future.result()
            (quiet, quiet_observed), (traffic_absent, traffic_observed) = quiet_future.result(), traffic_future.result()
        if (not isinstance(pcf, dict) or pcf.get('source') != 'native-pcf-known-round'
                or pcf.get('available') is not True or not fresh(pcf.get('observedAt'))
                or pcf.get('globalSmInventoryComplete') is not False):
            logging.getLogger(__name__).warning(
                'PCF baseline evidence rejected shapeValid=%s sourceValid=%s available=%s '
                'fresh=%s knownScopeValid=%s', isinstance(pcf, dict),
                isinstance(pcf, dict) and pcf.get('source') == 'native-pcf-known-round',
                isinstance(pcf, dict) and pcf.get('available') is True,
                isinstance(pcf, dict) and fresh(pcf.get('observedAt')),
                isinstance(pcf, dict) and pcf.get('globalSmInventoryComplete') is False)
            raise ReleaseProofBlocked("release.pcf_observation_unavailable")
        if snapshot is not None:
            scope = pcf.get('scope')
            expected_generation = snapshot.get('generation')
            from backend.onprem.policy.recovery_scope import recovered_allocation_scope
            recovered_scope = recovered_allocation_scope(snapshot, released=True)
            if recovered_scope is not None and (
                    scope != recovered_scope or pcf.get('nativeAbsenceObserved') is not True
                    or pcf.get('amQueries') != 50 or pcf.get('knownSmQueries') != 750):
                raise ReleaseProofBlocked('release.pcf_scope_mismatch')
            terminal_proof = snapshot.get('releaseProof')
            exceptional = (isinstance(terminal_proof, dict) and
                (terminal_proof.get('schema') == 'cityverse.gen17-partial-failed-round-release/v1'
                 or terminal_proof.get('operatorAbandon', {}).get('schema')
                    in ('cityverse.gen34-cancelled-operator-release/v1',
                        'cityverse.gen36-cancelled-ai-operator-release/v1')))
            device_inventory=False
            if (recovered_scope is None and isinstance(scope,dict)
                    and scope.get('kind')=='allocated_supi_pdu_inventory'
                    and (getattr(getattr(self.controller,'driver',None),'device_profile',None) is not None
                         or bool(snapshot.get('spatialDemand')))):
                allocations=[a for a in snapshot.get('actions',[]) if a.get('kind')=='ue_allocation']
                try:
                    from backend.onprem.policy.absence import PcfRoundAbsence
                    expected=PcfRoundAbsence(self.controller.driver,None,
                        generation_reader=self.controller.store.read_generation)._allocation_scope(snapshot)
                    device_inventory=(snapshot.get('perUe')==[] and snapshot.get('connectivityEvidence') is None
                        and snapshot.get('strategy')=='none' and len(allocations)==1
                        and allocations[0].get('accepted') is True and allocations[0].get('reused') is False
                        and all(a.get('kind') in {'ue_allocation','execution_failure','ue_stop'} for a in snapshot.get('actions',[]))
                        and scope==expected and pcf.get('nativeAbsenceObserved') is True
                        and type(pcf.get('amQueries')) is int and pcf['amQueries']==len({r['supi'] for r in expected['bindings']})
                        and type(pcf.get('knownSmQueries')) is int and pcf['knownSmQueries']==len(expected['bindings']))
                except Exception:
                    device_inventory=False
                if not device_inventory:
                    raise ReleaseProofBlocked('release.pcf_scope_mismatch')
            if (not isinstance(scope, dict) or 'roundId' not in scope or 'generation' not in scope
                    or scope['roundId'] != snapshot.get('roundId')
                    or type(scope['generation']) is not type(expected_generation)
                    or scope['generation'] != expected_generation
                    or scope.get('kind') != ('not_created' if expected_generation is None else
                        'allocated_supi_pdu_inventory' if recovered_scope is not None or device_inventory else
                        'verified_exceptional_release' if exceptional else 'known_native_bindings')):
                raise ReleaseProofBlocked("release.pcf_scope_mismatch")
        if pcf.get('knownRoundAbsent') is not True:
            raise ReleaseProofBlocked("release.pcf_policies_remain")
        if evidence.get("generation") != generation or not fresh(evidence.get("observedAt")):
            raise ReleaseProofBlocked("release.snapshot_invalid")
        observed = [seconds(evidence['observedAt']), seconds(pcf['observedAt'])]
        for key in ("ue", "amf", "smf", "upf"):
            source = evidence.get("sources", {}).get(key, {})
            if source.get("available") is not True or not fresh(source.get("observedAt")):
                raise ReleaseProofBlocked("release.source_" + key + "_unavailable")
            observed.append(seconds(source['observedAt']))
        # Native collector retains unresolved known SMF refs and unattributed
        # kernel sessions as rows; even an unregistered row blocks admission.
        if evidence.get("ues") != []:
            raise ReleaseProofBlocked("release.known_resources_remain")
        observed.extend(ran["observed"])
        observed.extend([pods_observed, quiet_observed, traffic_observed])
        owned = [p for p in pods if p["metadata"].get("labels", {}).get("cityverse.io/managed-by") == "onprem-ue-controller"]
        quiet = quiet is True
        latest = self.controller.store.current()
        if (latest['generation'] if latest else 0) != generation or (latest and any(latest['desired'].values())):
            raise ReleaseProofBlocked("release.generation_changed")
        oldest = min(observed)
        proof_age = time.time() - oldest
        if not -1 <= proof_age <= 15:
            raise ReleaseProofBlocked("release.proof_expired", round(proof_age, 3))
        proof={"trafficStopped": traffic_absent, "policiesAbsent": quiet,
                "rulesRestored": quiet and not evidence["ues"], "uesReleased": True,
                "podsAbsent": not owned, "observedAt": timestamp(oldest),
                "source": "native-round-release/v1", "generation": generation,
                "proofAgeSeconds": round(proof_age, 3),
                "scope": "known-round-and-managed-runtime",
                "ranContextProof": ran["metadata"],
                "pcfPolicyCoverage": deepcopy(pcf),
                "globalSmfInventoryComplete": evidence["sources"]["smf"].get("inventoryComplete") is True}
        if snapshot is not None and self.owned_native_teardown_factory is not None:
            from .owned_drain import OwnedNormalStopRepository
            journal=OwnedNormalStopRepository(self.controller.store)
            receipt=journal.read()
            if (receipt and receipt.baseline.round_id==snapshot.get('roundId')
                    and all(proof.get(k) is True for k in ('trafficStopped','policiesAbsent','rulesRestored','uesReleased','podsAbsent','globalSmfInventoryComplete'))):
                journal.complete(receipt)
        return proof

    async def baseline(self):
        control = self.control_reader() if callable(self.control_reader) else None
        if control and control.get("roundId") and control.get("stage") != "completed":
            raise RoundRuntimeBlocked("Previous round still owns native resources")
        current = self.controller.store.current()
        if current and any(current.get("desired", {}).values()):
            # A persistent fleet is a valid baseline when all round-scoped
            # traffic/policy is absent and the complete resident generation has
            # fresh, internally consistent registration/PDU identity evidence.
            if not await self.prepare_cleanup({"actions": []}):
                return False
            self.lease_generation(current["desired"])
            latest = self.controller.store.current()
            if (not latest or latest.get("generation") != current.get("generation")
                    or latest.get("desired") != current.get("desired")):
                raise RoundRuntimeBlocked("Resident UE generation changed during baseline verification")
            return True
        if current:
            status = self.status()
            zero = dict.fromkeys(SLICES, 0)
            if (current.get("desired") != zero
                    or status.get("generation") != current.get("generation")
                    or status.get("status") != "stopped"
                    or not fresh(status.get("observedAt"))
                    or not resident_stable_window(status, current.get("created"))
                    or status.get("errors")
                    or status.get("perUe") != []
                    or any(status.get(key) != 0 for key in
                           ("allocated", "registered", "pduEstablished", "trafficVerified"))):
                raise RoundRuntimeBlocked("Zero-UE generation lacks fresh stable stopped status")
        proof = await self.release_proof()
        return all(proof.get(k) is True for k in ("trafficStopped", "policiesAbsent", "rulesRestored", "uesReleased", "podsAbsent"))

    async def prepare_cleanup(self, snapshot):
        # Restart may leave timeout-owned iperf processes running even though
        # the Python future no longer exists. Observe their exit before NAS
        # deregistration; do not kill unrelated processes or assume exit.
        if any(a.get("kind") == "traffic_window" and a.get("mode") == "batch" for a in snapshot.get("actions", [])):
            if self.batch is None:
                raise RoundRuntimeBlocked("Durable batch work requires exact-token recovery")
            await self.batch.cleanup(snapshot)
        def observe():
            quiet = self.policy_quiet() is True
            if not quiet:
                scoped = self.fixture_cleanup_quiet
                quiet = callable(scoped) and scoped(snapshot) is True
            return quiet and self._traffic_absent(self._pods())
        return await asyncio.to_thread(observe)

    async def request_owned_drain(self,snapshot,lease,permit,*,admission_guard,recovery=False):
        """Normal stop, or the sole writer's exact owner-safe native-zero path."""
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficDrainPermit
        if type(lease) is not ServiceIntentTrafficLease or type(permit) is not IntentTrafficDrainPermit:
            raise RoundRuntimeBlocked('Typed formal native drain authority required')
        permit.validate(lease,time.time())
        expected=snapshot.get('generation')
        allocation=self.controller.store.read_generation(expected)
        if allocation is None or allocation['generation']!=expected or snapshot.get('fleetLease'):
            raise RoundRuntimeBlocked('Owned native drain allocation is unavailable')
        admission_guard()
        if self.owned_native_teardown_factory is not None:
            # Accepted-V2 is checked by the adapter before any zero/NAS write.
            # An uncertain previous stop is read back, never dispatched again.
            return await self.controller.request_owned_native_stop(snapshot,lease,permit,
                admission_guard=admission_guard,factory=self.owned_native_teardown_factory)
        if not recovery:
            try:
                return await self.controller.request(dict.fromkeys(SLICES,0),expected_generation=expected,
                    admission_guard=admission_guard)
            except EvidenceUnavailable:
                # This is not a retry of an uncertain native write. The private
                # controller independently requires exact native absence and
                # original-generation CAS before journalling a zero request.
                pass
        return await self.controller.request_owned_drain(lease,permit,expected_generation=expected,
            original_allocation=deepcopy(allocation['allocation']),owned_snapshot=deepcopy(snapshot),
            admission_guard=admission_guard)

    async def request_owned_round_stop(self, snapshot):
        """Stop an exact ordinary spatial allocation, including pre-write retry.

        GenerationStore commits a zero generation before the native driver can
        retire anything. The unchanged original generation therefore proves a
        previous rejected request did not dispatch a stop. A changed generation
        remains unknown here and must use its existing reconciliation protocol.
        """
        expected = snapshot.get('generation')
        def guard():
            live = self.round_reader() if callable(self.round_reader) else None
            current = self.controller.store.current()
            allocations = [a for a in (live or {}).get('actions', []) if a.get('kind') == 'ue_allocation']
            if (not live or not live.get('spatialDemand') or live.get('fleetLease')
                    or live.get('stage') != 'cleaning'
                    or any(live.get(k) != snapshot.get(k) for k in ('roundId','requestId','generation'))
                    or not current or current['generation'] != expected
                    or len(allocations) != 1 or allocations[0].get('accepted') is not True
                    or allocations[0].get('reused') is not False
                    or allocations[0].get('generation') != expected
                    or current['desired'] != allocations[0].get('counts')
                    or current['desired'] != slice_counts(live['allocation'])):
                raise EvidenceUnavailable('Exact unchanged spatial allocation required for round stop')
        guard()
        return await self.controller.request(dict.fromkeys(SLICES, 0),
            expected_generation=expected, admission_guard=guard)

    def prepare_spatial(self, body):
        from .spatial_ran import configured_registry, native_readouts, resolve_groups
        registry = configured_registry()
        resolution = resolve_groups(body, registry, native_readouts(self.controller.driver, registry))
        import hashlib
        resolution['spatialRevision'] = hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()
        return resolution

    def verify_spatial_bindings(self, snapshot):
        from .spatial_ran import configured_registry, native_readouts, verify_bindings
        current = self.controller.store.current()
        if not current or current['generation'] != snapshot['generation']:
            raise EvidenceUnavailable('Spatial UE generation changed')
        registry = configured_registry()
        if registry['topologyRevision'] != snapshot['spatialDemand']['topologyRevision']:
            raise EvidenceUnavailable('Spatial topology revision changed')
        return dict(verify_bindings(current['allocation'], native_readouts(self.controller.driver, registry)), generation=current['generation'], topologyRevision=registry['topologyRevision'])

    async def request(self, counts,*,reservation=None,spatial_request=None):
        if spatial_request is not None:
            resolved = await asyncio.to_thread(self.prepare_spatial, spatial_request)
            owner = self.round_reader() if callable(self.round_reader) else None
            frozen = (owner or {}).get('spatialDemand', {})
            if frozen and frozen.get('resolution', {}).get('spatialRevision') != resolved['spatialRevision']:
                raise EvidenceUnavailable('Commissioned RAN registry changed after round admission')
        if reservation is not None:
            self.validate_held_workload(reservation)
            # The same owner-checked held allocation becomes the runnable native
            # generation atomically; never fall through to ordinary allocation.
            return await self.controller.request(counts,reservation=reservation,**({"spatial_request":spatial_request} if spatial_request is not None else {}))
        expired = getattr(self.controller, 'expired_stopped_generation', None)
        current = expired(counts) if callable(expired) else None
        if current is None:
            return await self.controller.request(counts,**({"spatial_request":spatial_request} if spatial_request is not None else {}))
        expected = current['generation']
        owner = self.round_reader() if callable(self.round_reader) else None
        fence_keys = ('roundId', 'requestId', 'generation', 'stage')
        owner_fence = {key: owner.get(key) for key in fence_keys} if owner else None

        def admission_guard():
            if callable(self.request_cancel_check):
                self.request_cancel_check()
            if owner_fence is not None:
                latest = self.round_reader()
                if (owner_fence.get('stage') != 'allocating'
                        or latest.get('cancelRequested') is True
                        or {key: latest.get(key) for key in fence_keys} != owner_fence):
                    raise RoundRuntimeBlocked('Round ownership changed during UE allocation')

        diagnostic = self.controller.request_guard_diagnostic(current, self.controller.status())
        started = time.monotonic()
        deadline = started + 120
        diagnostic.update(refreshAttempts=0, refreshOutcome='native_error')

        def same_empty_generation():
            latest = self.controller.store.current()
            return (latest is not None and latest.get('generation') == expected
                and latest.get('created') == current.get('created')
                and latest.get('desired') == dict.fromkeys(SLICES, 0) and latest.get('allocation') == []
                and not any(key in latest['result'] for key in ('recoveryMode', 'hostStateLoss', 'exceptionalRecovery')))

        def reject(outcome):
            diagnostic['refreshOutcome'] = outcome
            message = ('UE allocation generation changed during native refresh' if outcome == 'generation_changed'
                else 'Current UE generation requires operator reconciliation before resize or retirement')
            raise EvidenceUnavailable(message)

        try:
            while True:
                admission_guard()
                if not same_empty_generation():
                    reject('generation_changed')
                if time.monotonic() >= deadline:
                    reject('deadline_exhausted')
                status = self.controller.status()
                try:
                    observed_fresh = (-1 <= time.time() - seconds(status.get('observedAt'))
                        <= self.controller.verifier.max_age)
                except (ValueError, TypeError, OverflowError):
                    observed_fresh = False
                if (status.get('generation') == expected and observed_fresh
                        and status.get('status') == 'stopped' and status.get('errors') == []):
                    diagnostic['refreshOutcome'] = 'refreshed'
                    break
                background_fresh = (observed_fresh and status.get('generation') == expected
                    and status.get('status') == 'verifying_traffic' and not status.get('errors'))
                if (not diagnostic['refreshAttempts'] and not background_fresh
                        and not self.controller._lock.locked()):
                    diagnostic['refreshAttempts'] = 1
                    active = asyncio.create_task(self.controller.reconcile())
                    try:
                        await asyncio.shield(active)
                    except asyncio.CancelledError as cancelled:
                        # A cancelled await cannot stop native to_thread work.
                        # Drain it before allowing the controller/request to move on.
                        while not active.done():
                            try:
                                await asyncio.shield(active)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if active.done() and not active.cancelled():
                            try:
                                active.result()
                            except Exception:
                                pass
                        raise cancelled
                    if not same_empty_generation():
                        reject('generation_changed')
                    if time.monotonic() >= deadline:
                        reject('deadline_exhausted')
                    status = self.controller.status()
                    if status.get('errors') or status.get('status') not in {'stopped', 'verifying_traffic'}:
                        reject('not_ready')
                    continue
                if status.get('errors') and status.get('errors') != ['Last observation expired']:
                    reject('not_ready')
                # Only read status here. The existing controller.run supplies
                # native captures and retains the original 15s/60s stability rules.
                await asyncio.sleep(min(max(.001, self.controller.poll_seconds),
                    max(0., deadline - time.monotonic())))
            admission_guard()
            result = await self.controller.request(counts, expected_generation=expected, admission_guard=admission_guard,**({"spatial_request":spatial_request} if spatial_request is not None else {}))
        except BaseException as exc:
            if (isinstance(exc, asyncio.CancelledError)
                    or type(exc) is RoundRuntimeBlocked and str(exc) == 'Round cancelled'):
                diagnostic['refreshOutcome'] = 'cancelled'
            elif isinstance(exc, EvidenceUnavailable) and str(exc) == 'UE allocation generation changed during native refresh':
                diagnostic['refreshOutcome'] = 'generation_changed'
            elif diagnostic.get('refreshOutcome') == 'refreshed':
                diagnostic['refreshOutcome'] = 'not_ready'
            diagnostic['refreshDurationMs'] = (time.monotonic() - started) * 1000
            # Keep the original source code/age that caused this refresh.
            exc.nativeRequestDiagnostic = RequestGuardDiagnostic.project(diagnostic)
            raise
        diagnostic['refreshDurationMs'] = (time.monotonic() - started) * 1000
        return {**result, 'nativeRequestDiagnostic': RequestGuardDiagnostic.project(diagnostic)}

    def reusable_generation(self, counts):
        """Return the current healthy fleet generation, or fail closed.

        Scenario rounds may borrow an already-established fleet but never
        resize it.  Resizing (including zero) remains an explicit UE API
        operation so a round cannot accidentally retire resident UEs.
        """
        current = self.controller.store.current()
        if not current or not any(current.get("desired", {}).values()):
            return None
        if current.get("desired") != counts:
            raise RoundRuntimeBlocked("Existing UE fleet allocation is incompatible with the requested slices")
        status = self.status()
        if status.get('schemaVersion') == V2 or any(item.get('device') for item in current.get('allocation', [])):
            raise RoundRuntimeBlocked('v2 resident fleet reuse is not commissioned')
        expected = {item["supi"]: item["slice"] for item in current.get("allocation", [])}
        rows = status.get("perUe", [])
        actual = {row.get("supi"): row.get("slice") for row in rows}
        counters = ("allocated", "registered", "pduEstablished")
        established_at = current.get("lastEstablishedAt")
        if (status.get("generation") != current.get("generation")
                or not fresh(status.get("observedAt"))
                or type(established_at) not in (int, float)
                or not resident_stable_window(status, current.get("created"))
                or status.get("errors")
                or len(actual) != len(rows) or actual != expected
                or any(status.get(key) != len(expected) for key in counters)
                or any(row.get("generation") != current["generation"]
                       or row.get("registered") is not True
                       or row.get("pduEstablished") is not True
                       or not all(row.get(key) for key in ("podUid", "ip", "sessionId", "interface", "upf"))
                       for row in rows)):
            raise RoundRuntimeBlocked("Existing UE fleet lacks fresh identity, registration, PDU or traffic proof")
        return current

    @staticmethod
    def _fleet_identity(row):
        return {key: row.get(key) for key in ("supi", "slice", "generation", "podUid", "ip",
                                               "sessionId", "interface", "upf", "smContextRef",
                                               "pfcpSeid", "dnn", "sst", "sd", "upfEpoch")}

    @classmethod
    def _fleet_digest(cls, generation, rows):
        payload = {"generation": generation,
                   "identities": sorted((cls._fleet_identity(row) for row in rows), key=lambda row: row["supi"])}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def lease_generation(self, counts):
        """Select an immutable per-slice subset from a fully healthy fleet."""
        current = self.controller.store.current()
        if not current or not any(current.get("desired", {}).values()):
            return None
        if counts != current.get("desired") and not self.subset_leasing_enabled:
            raise RoundRuntimeBlocked("Subset fleet leasing is capability-gated until every traffic and policy guard is lease-aware")
        if any(current["desired"].get(name, 0) < counts.get(name, 0) for name in SLICES):
            raise RoundRuntimeBlocked("Existing UE fleet has insufficient capacity in a requested slice")
        status = self.status()
        if status.get('schemaVersion') == V2 or any(item.get('device') for item in current.get('allocation', [])):
            raise RoundRuntimeBlocked('v2 resident fleet reuse is not commissioned')
        expected = {item["supi"]: item["slice"] for item in current.get("allocation", [])}
        rows = status.get("perUe", [])
        actual = {row.get("supi"): row.get("slice") for row in rows}
        established_at = current.get("lastEstablishedAt")
        if (status.get("generation") != current.get("generation") or not fresh(status.get("observedAt"))
                or type(established_at) not in (int, float)
                or not resident_stable_window(status, current.get("created"))
                or status.get("errors") or len(actual) != len(rows) or actual != expected
                or any(status.get(key) != len(expected) for key in ("allocated", "registered", "pduEstablished"))
                or any(row.get("generation") != current["generation"]
                       or row.get("registered") is not True or row.get("pduEstablished") is not True
                       or not all(row.get(key) for key in ("podUid", "ip", "sessionId", "interface", "upf"))
                       for row in rows)):
            raise RoundRuntimeBlocked("Existing UE fleet lacks fresh identity, registration or PDU proof")
        selected = []
        for name in SLICES:
            selected.extend(sorted((row for row in rows if row["slice"] == name), key=lambda row: row["supi"])
                            [:counts.get(name, 0)])
        return {"generation": current["generation"], "fleetDigest": self._fleet_digest(current["generation"], rows),
                "counts": deepcopy(counts), "supis": [row["supi"] for row in selected],
                "identities": [self._fleet_identity(row) for row in selected]}

    def leased_status(self, lease, status):
        if status.get('schemaVersion') == V2:
            raise RoundRuntimeBlocked('v2 resident fleet reuse is not commissioned')
        if status.get("generation") != lease.get("generation") or not fresh(status.get("observedAt")):
            raise RoundRuntimeBlocked("Leased fleet generation is stale or changed")
        rows = status.get("perUe", [])
        if self._fleet_digest(status["generation"], rows) != lease.get("fleetDigest"):
            raise RoundRuntimeBlocked("Leased fleet identity digest changed")
        by_supi = {row.get("supi"): row for row in rows}
        if len(by_supi) != len(rows) or set(lease.get("supis", [])) - set(by_supi):
            raise RoundRuntimeBlocked("Leased SUPI set is incomplete or duplicated")
        selected = [by_supi[supi] for supi in lease["supis"]]
        if [self._fleet_identity(row) for row in selected] != lease.get("identities"):
            raise RoundRuntimeBlocked("Leased SUPI/PDU identity changed")
        return selected

    async def round_release_proof(self, snapshot):
        """Prove round resources are gone while the borrowed fleet remains."""
        if snapshot.get('schemaVersion') == V2:
            raise ReleaseProofBlocked('release.v2_resident_reuse_not_commissioned')
        if not await self.prepare_cleanup(snapshot):
            raise ReleaseProofBlocked("release.round_resources_remain")
        current = self.controller.store.current()
        status = self.status()
        lease = snapshot.get("fleetLease")
        zero_workload = snapshot.get("allocation", {}).get("total") == 0
        if zero_workload and not lease:
            if current and sum(current.get("desired", {}).values()):
                resident = self.lease_generation(current["desired"])
                self.leased_status(resident, status)
                return {"trafficStopped": True, "policiesAbsent": True, "rulesRestored": True,
                        "ueLeaseReleased": True, "fleetPreserved": True,
                        "uesReleased": False, "podsAbsent": False,
                        "generation": current["generation"], "observedAt": status["observedAt"],
                        "scope": "zero-workload-round-resources-only",
                        "residentFleetSize": len(resident["supis"])}
            return await self.release_proof(snapshot)
        effective_generation = snapshot.get("generation") or (lease or {}).get("generation")
        rows = self.leased_status(lease, status) if lease else status.get("perUe", [])
        expected_rows = snapshot.get("perUe") or (lease or {}).get("identities", [])
        if not expected_rows and not lease:
            # A failed registration never publishes established PDU identities.
            # Release only this round's resources, retaining its newly allocated
            # fleet for a separate, natively verified zero-UE request.
            allocations = [a for a in snapshot.get("actions", []) if a.get("kind") == "ue_allocation"]
            expected_allocation = {(row["supi"], row["slice"])
                                   for row in (current or {}).get("allocation", [])}
            actual_allocation = {(row.get("supi"), row.get("slice")) for row in rows}
            if (not current or current.get("generation") != effective_generation
                    or status.get("generation") != effective_generation
                    or not fresh(status.get("observedAt")) or status.get("errors")
                    or len(allocations) != 1 or allocations[0].get("accepted") is not True
                    or allocations[0].get("reused") is not False
                    or allocations[0].get("generation") != effective_generation
                    or allocations[0].get("counts") != slice_counts(snapshot["allocation"])
                    or current.get("desired") != allocations[0].get("counts")
                    or len(expected_allocation) != snapshot["allocation"]["total"]
                    or len(rows) != len(actual_allocation) or actual_allocation != expected_allocation
                    or any(row.get("generation") != effective_generation for row in rows)
                    or any(a.get("kind") not in {"ue_allocation", "execution_failure", "adaptive_qer_cleanup"}
                           for a in snapshot.get("actions", []))):
                raise ReleaseProofBlocked("release.fleet_identity_changed")
            return {"trafficStopped": True, "policiesAbsent": True, "rulesRestored": True,
                    "ueLeaseReleased": True, "fleetPreserved": True,
                    "uesReleased": False, "podsAbsent": False,
                    "generation": effective_generation, "observedAt": status["observedAt"],
                    "scope": "pre-established-round-resources-only"}
        expected = {(row["supi"], row["slice"], row["podUid"], row["sessionId"])
                    for row in expected_rows}
        actual = {(row.get("supi"), row.get("slice"), row.get("podUid"), row.get("sessionId"))
                  for row in rows}
        if (not current or current.get("generation") != effective_generation
                or status.get("generation") != effective_generation
                or not fresh(status.get("observedAt"))
                or status.get("errors") or actual != expected):
            raise ReleaseProofBlocked("release.fleet_identity_changed")
        return {"trafficStopped": True, "policiesAbsent": True, "rulesRestored": True,
                "ueLeaseReleased": True, "fleetPreserved": True,
                "uesReleased": False, "podsAbsent": False,
                "generation": effective_generation, "observedAt": status["observedAt"],
                "scope": "round-resources-only"}

    def status(self):
        return self.controller.status()

    def current_generation(self):
        return self.controller.store.current()

    @staticmethod
    def _round_fence(snapshot):
        if snapshot.get('schemaVersion') == V2:
            identities = sorted(({key: row.get(key) for key in IDENTITY_FIELDS}
                                 for row in session_rows(snapshot)), key=lambda r: r['sessionKey'])
            return {'roundId': snapshot.get('roundId'), 'generation': snapshot.get('generation'),
                    'schemaVersion': V2, 'fleetDigest': None,
                    'populationRevision': snapshot.get('populationRevision', 0),
                    'sessionKeys': [r['sessionKey'] for r in identities], 'identities': identities}
        identities = [{key: row.get(key) for key in ("supi", "slice", "podUid", "sessionId", "ueIp", "tun", "upf")}
                      for row in snapshot.get("perUe", [])]
        return {"roundId": snapshot.get("roundId"), "generation": snapshot.get("generation"),
                "fleetDigest": (snapshot.get("fleetLease") or {}).get("fleetDigest"),
                "supis": sorted(row["supi"] for row in identities), "identities": identities}

    def _owns_round_fence(self, fence):
        if not callable(self.round_reader) or not isinstance(fence, dict):
            return False
        return self._round_fence_matches(self.round_reader(), fence)

    def _round_fence_matches(self, current, fence):
        """Match one supplied full read; this helper does not grant freshness."""
        if (current.get("roundId") != fence.get("roundId")
                or current.get("generation") != fence.get("generation")
                or current.get("stage") in {"cleaning", "completed"}
                or (current.get("fleetLease") or {}).get("fleetDigest") != fence.get("fleetDigest")):
            return False
        try:
            return self._round_fence(current) == fence
        except (ValueError, TypeError, KeyError):
            return False

    def _guard(self, binding, fence=None):
        if fence is not None and not self._owns_round_fence(fence):
            return False
        reader = getattr(self, 'population_status_reader', None)
        population = reader() if callable(reader) else None
        status = population if population is not None else self.status()
        if status.get("generation") != binding["generation"] or not fresh(status.get("observedAt")):
            return False
        if binding.get('sessionKey'):
            try:
                if status.get('schemaVersion') != V2:
                    return False
                rows = [r for r in session_rows(status) if r['sessionKey'] == binding['sessionKey']]
                return (len(rows) == 1 and rows[0].get('trafficVerified') is True
                        and all(rows[0].get(k) == binding.get(k) for k in IDENTITY_FIELDS))
            except (ValueError, TypeError, KeyError):
                return False
        rows = [r for r in status.get("perUe", []) if r.get("supi") == binding["supi"]]
        return len(rows) == 1 and rows[0].get("trafficVerified") is True and all(
            rows[0].get(k) == binding[v] for k, v in (("ip", "ip"), ("sessionId", "sessionId"),
                                                     ("podUid", "podUid"), ("interface", "tun"), ("upf", "upf")))

    def population_sampling_view(self, snapshot):
        """Private survivor jobs while public cohort awaits native absence."""
        excluded = set(snapshot.get('populationSamplingExcluded', ()))
        if not excluded:
            return snapshot
        if snapshot.get('schemaVersion') != V2 or not snapshot.get('populationPending'):
            raise RoundRuntimeBlocked('Population sampling exclusion lacks authority')
        view = deepcopy(snapshot)
        view['_populationRoundFence'] = self._round_fence(snapshot)
        view['devices'] = [d for d in view['devices'] if d['deviceId'] not in excluded]
        view['perSession'] = [s for s in view['perSession'] if s['deviceId'] not in excluded]
        view['perUe'] = [d for d in view.get('perUe', ()) if d.get('deviceId') not in excluded]
        session_rows(view)
        return view

    def session_workload_capabilities(self):
        from backend.onprem.traffic.session_workload import SessionWorkloadCompiler,SCHEMA
        ready=isinstance(getattr(self,'session_workload_compiler',None),SessionWorkloadCompiler)
        return {'configured':ready,'schemaVersion':SCHEMA,'multiplePduSessions':ready,
                'source':'server-pinned-service-catalog' if ready else None,'serviceEffectVerified':False}

    def validate_held_workload(self,reservation,body=None):
        """Check the same pinned forecast before consuming a typed device hold."""
        from .ue_reservation import HeldUEAllocation
        if type(reservation) is not HeldUEAllocation or not reservation.devices:return
        from backend.onprem.intents.pretraffic_planning import requested_session_workload,PlanningError
        try:
            return requested_session_workload(body or {},reservation,self.session_workload_compiler)
        except PlanningError:
            raise RoundRuntimeBlocked('v2 session workload catalog is not commissioned for the held sessions') from None

    def session_workload(self, snapshot):
        if snapshot.get('schemaVersion') != V2:
            return None
        if self.session_workload_compiler is None:
            raise RoundRuntimeBlocked('v2 session workload catalog is not commissioned')
        return self.session_workload_compiler.compile([
            dict(identity=r['sessionKey'],service_type=r.get('service_type'),region=r.get('region'),slice=r['slice'],
                 ue_id=r['supi'])
            for r in session_rows(snapshot)],traffic_mode=snapshot.get('trafficMode','functional'),
            service_requirements=snapshot.get('serviceRequirements'))

    def service_route(self, snapshot, binding):
        registry = self.service_routes
        if registry is None or not registry.has_session(snapshot, binding):
            if snapshot.get('requireServiceRoutes'):
                raise RoundRuntimeBlocked('Commissioned application service route is required')
            return None
        try:
            route = registry.for_session(snapshot, binding)
        except Exception:
            raise RoundRuntimeBlocked('Independent application/session route evidence unavailable') from None
        if (route['upf_target_id'] != binding['upf'] or route['upf_epoch'] != binding['upfEpoch']
                or str(route['pfcp_seid']) != str(binding['pfcpSeid'])):
            raise RoundRuntimeBlocked('Application route differs from native PDU identity')
        return route

    def service_receiver(self, snapshot, binding, pods):
        route = self.service_route(snapshot, binding)
        if route is None:
            receiver = native_receiver_pods(pods, self.controller.driver.namespace)[binding['slice']]
            return receiver, ENDPOINTS[binding['slice']], None
        matches = [pod for pod in pods if pod.get('metadata', {}).get('uid') == route['application_pod_uid']
            and pod['metadata'].get('name') == route['receiver_pod']]
        if len(matches) != 1 or matches[0].get('metadata', {}).get('deletionTimestamp'):
            raise RoundRuntimeBlocked('Exact application receiver Pod unavailable')
        return matches[0], route['address'], route

    async def traffic_jobs(self, snapshot):
        from backend.onprem.traffic.session_workload import session_rates
        workload = self.session_workload(snapshot)
        rates = session_rates(workload) if workload is not None else None
        pods = await asyncio.to_thread(self._pods)
        result = []
        fence = snapshot.get('_populationRoundFence') or self._round_fence(snapshot)
        for index, binding in enumerate(session_rows(snapshot)):
            ue = [p for p in pods if p["metadata"].get("uid") == binding["podUid"]]
            if len(ue) != 1:
                raise RoundRuntimeBlocked("Unique UE and slice receiver Pod required")
            receiver, endpoint, route = self.service_receiver(snapshot, binding, pods)
            for direction in ("ul", "dl"):
                result.append({"binding": {**binding, "ip": binding["ueIp"], "generation": snapshot["generation"]},
                               "ue_pod": ue[0]["metadata"]["name"], "receiver_pod": receiver["metadata"]["name"],
                               "receiver_uid": receiver["metadata"]["uid"], "endpoint": endpoint,
                               "port": route["port"] if route else 20000 + index * 2 + int(direction == "dl"), "direction": direction,
                                "offered_mbps": (rates[binding['sessionKey']][direction]/1000 if rates is not None
                                                 else self.offered[binding["scenario"]]), "duration": self.duration,
                               "roundFence": deepcopy(fence), **({"serviceRoute": route} if route else {})})
        return result

    async def sample(self, job):
        fence = job.get("roundFence")
        if not self._owns_round_fence(fence):
            raise RoundRuntimeBlocked("Traffic job no longer owns the immutable round lease")
        route = job.get('serviceRoute')
        args = {key: value for key, value in job.items() if key not in {"roundFence", "serviceRoute", "flowOperationId"}}
        flow_id = None
        if route:
            flow_operation = job.get('flowOperationId', route['operation_id'])
            flow_id = ':'.join((flow_operation, route['binding_id'], route['version'], str(job['port']), job['direction']))
            lease_route = {**route, 'flow_operation_id': flow_operation}
            await asyncio.to_thread(self.service_routes.acquire_flow, flow_id, lease_route)
        def guard(binding):
            if not self._guard(binding, fence):
                return False
            if route:
                try:
                    current = self.service_routes.for_session(self.round_reader(), binding)
                    return all(current.get(k) == route.get(k) for k in ('binding_id', 'version', 'endpoint_revision',
                        'application_pod_uid', 'upf_epoch', 'path_revision', 'pfcp_seid'))
                except Exception:
                    return False
            return True
        # Uncertain native completion keeps its lease, preventing application removal.
        result = await asyncio.to_thread(self.traffic.sample, **args, guard=guard, **({'service_route': route} if route else {}))
        if flow_id:
            await asyncio.to_thread(self.service_routes.release_flow, flow_id, lease_route)
        return result

    async def measure_rtt(self, snapshot, *, cancelled=lambda: False, deadline=None):
        """Observe an adjacent RTT window under the existing immutable lease."""
        from backend.onprem.evidence.rtt import collect_rtt, cohort_fingerprint
        from backend.onprem.evidence.free_ran_read_transport import configured_ue_read_transport
        import os
        fence = self._round_fence(snapshot)
        expected_cohort = cohort_fingerprint(snapshot)
        def bound():
            if not callable(self.round_reader) or not isinstance(fence, dict):
                return False
            current = self.round_reader()
            if not self._round_fence_matches(current, fence) or cohort_fingerprint(current) != expected_cohort:
                return False
            native = self.status()
            if native.get("generation") != snapshot.get("generation") or not fresh(native.get("observedAt")):
                return False
            try:
                native_rows = session_rows(native)
                owners = session_rows(snapshot)
            except (ValueError, TypeError, KeyError):
                return False
            rows = {session_identity(r): r for r in native_rows}
            if len(rows) != len(native_rows):
                return False
            for owner in owners:
                row = rows.get(session_identity(owner), {})
                if snapshot.get('schemaVersion') == V2 and any(row.get(k) != owner.get(k) for k in IDENTITY_FIELDS):
                    return False
                if row.get("trafficVerified") is not True or any(row.get(key) != owner.get(source) for key, source in
                    (("sessionId", "sessionId"), ("podUid", "podUid"), ("ip", "ueIp"), ("interface", "tun"), ("upf", "upf"))):
                    return False
                if any(owner.get(k) is not None and row.get(k) != owner[k] for k in ("upfEpoch", "smContextRef", "pfcpSeid", "dnn", "sst", "sd")):
                    return False
            return True
        if cancelled() or not await asyncio.to_thread(bound):
            return None
        driver = self.controller.driver
        transport = configured_ue_read_transport(os.environ, driver)
        observed = await asyncio.to_thread(collect_rtt, driver, snapshot, cancelled=cancelled,
                                          deadline=deadline, read_transport=transport)
        if cancelled() or not await asyncio.to_thread(bound):
            return None
        return observed
