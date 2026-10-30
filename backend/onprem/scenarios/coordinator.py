"""Scenario lifecycle: admission, execution, adaptive AI, results and cleanup."""
from __future__ import annotations
from backend.onprem.storage.current_state import current_reader, read_cleanup, mutate_current, complete_current, replay_current, reserve_current
import asyncio
from backend.onprem.api.timing import admission_span, timed_admission_lock
from copy import deepcopy
import math
import logging
import time
from backend.onprem.scenarios.contracts import RoundConflict, RoundUnavailable, bind_scenarios, slice_counts, validate_round
from backend.onprem.runtime.ue import SLICES, EvidenceUnavailable, seconds, timestamp
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows, session_identity
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.capability import ReceiverWindowBlocked
from backend.onprem.capacity.admission import CalibrationAdmission, CalibrationBlocked, PROFILE_ID
from backend.onprem.runtime.errors import RoundRuntimeBlocked, NativeWindowFailure, ReleaseProofBlocked, RequestGuardDiagnostic
from backend.onprem.runtime.round_resources import native_window_reason, fresh, action, failure_code, safe_reason, validate_window, RESIDENT_FRESHNESS_ERRORS

class OwnedRoundCancelled(RoundRuntimeBlocked):
    """A cancellation with current durable formal-drain lineage, not a guard failure."""
    def __init__(self, round_id, lease, permit):
        super().__init__('Owned traffic round cancelled')
        self.cancellation={'source':'owned-formal-round-cancel/v1','round_id':round_id,
            'lease_id':lease.lease_id,'owner_id':lease.owner_id,
            'release_operation_id':permit.release_operation_id}

class _ShardReservations:
    """One cohort transaction, retaining the callable single-shard port."""
    def __init__(self, coordinator, snapshot, stage):
        self.coordinator, self.snapshot, self.stage = coordinator, snapshot, stage
        self.journals = []

    def _journal(self, token, bindings, jobs, rates, budget, *, workload, shard_index,
                 shard_count, detached_reservation=None):
        journal = action('traffic_window', stage=self.stage, mode='batch', token=token,
            bindings=bindings, jobs=jobs, offeredBySupi=rates, aggregateKbps=budget,
            workload=deepcopy(workload), cleanupVerified=False, shardIndex=shard_index,
            shardCount=shard_count)
        if detached_reservation is not None:
            journal.update(executionMode='detached-shard-v1', detachedReservation=deepcopy(detached_reservation))
        if self.snapshot.get('schemaVersion') == V2:
            proof = self.snapshot.get('typedCohortProof')
            if not isinstance(proof, dict) or proof.get('bindingSchema') != V2:
                raise RoundRuntimeBlocked('Typed shard journal requires exact native cohort seal')
            journal.update(bindingSchema=V2, typedCohortProof=deepcopy(proof))
        if self.snapshot.get('_scopedCapacityWindowOrigin') is not None:
            journal['windowOrigin']=deepcopy(self.snapshot['_scopedCapacityWindowOrigin'])
        return journal

    def __call__(self, *args, **kwargs):
        self.coordinator._check_cancel()
        journal = self._journal(*args, **kwargs)
        if (journal['shardIndex'] != len(self.journals) or not 2 <= journal['shardCount'] <= 10
                or any(j['shardCount'] != journal['shardCount'] for j in self.journals)):
            raise RoundRuntimeBlocked('Batch shard reservation order or count changed')
        self.coordinator.ledger.reserve_batch_action(self.snapshot['roundId'], self.snapshot['generation'], journal)
        self.journals.append(journal)
        return True

    def reserve_many(self, requests):
        self.coordinator._check_cancel()
        if self.journals or not isinstance(requests, list) or not 2 <= len(requests) <= 10:
            raise RoundRuntimeBlocked('Batch cohort reservation changed')
        journals = [self._journal(**request) for request in requests]
        if any(j['shardIndex'] != i or j['shardCount'] != len(journals) for i,j in enumerate(journals)):
            raise RoundRuntimeBlocked('Batch cohort reservation is incomplete')
        self.coordinator.ledger.reserve_batch_actions(self.snapshot['roundId'], self.snapshot['generation'], journals)
        self.journals.extend(journals)
        return True


class NativeRoundCoordinator:
    """Serial scenario lifecycle and adaptive AI; UEController owns the process fence.

    snapshot() never refreshes observation timestamps. start/cancel/recover are
    asynchronous and must run in the same application event loop.
    """
    capabilities = {"start": True, "staticPolicy": False, "aiPolicy": False,
                    "reason": "NONE uses real native traffic; official policy strategies remain capability-gated"}

    def __init__(self, ledger, resources, *, poll_seconds=2, setup_timeout=900,
                 cleanup_timeout=180, result_seconds=2, verified_max_ues=1,
                 monotonic=time.monotonic, calibration=None, policy_bridge=None, ai_planner=None,
                 group_adapter=None, group_oracle=None, group_ai_planner=None, group_ai_reason=None,
                 capacity_grant=None, capacity_grant_reader=None, adaptive_runtime=None,
                 measurement_timings=None, intent_traffic_restorer=None, intent_traffic_renewer=None, wall_clock=time.time,
                 scoped_capacity=None):
        from backend.onprem.capacity.promotion import PROMOTION_SCOPE, VerifiedCapacityGrant
        from backend.onprem.agents.startup import UnverifiedAiTrial
        ai_trial = type(capacity_grant) is UnverifiedAiTrial
        promoted = (type(capacity_grant) is VerifiedCapacityGrant
                    and capacity_grant.verified_max_ues == 100
                    and capacity_grant.profile_id == PROFILE_ID
                    and capacity_grant.scope == PROMOTION_SCOPE
                    and all(isinstance(getattr(capacity_grant, key), str)
                            and getattr(capacity_grant, key)
                            for key in ("manifest_sha256", "runtime_fingerprint", "evidence_id")))
        if (type(verified_max_ues) is not int
                or verified_max_ues not in (0, 1, 100)
                or (verified_max_ues == 0 and not (
                    (calibration is not None and isinstance(calibration, CalibrationAdmission)
                     and calibration.enabled and capacity_grant is None
                     and capacity_grant_reader is None and adaptive_runtime is None)
                    or (ai_trial and callable(capacity_grant_reader)
                        and adaptive_runtime is not None
                        and isinstance(calibration, CalibrationAdmission)
                        and not calibration.enabled)))
                or (verified_max_ues == 100 and not promoted)
                or (verified_max_ues == 100 and not callable(capacity_grant_reader))
                or (verified_max_ues == 1 and capacity_grant is not None)):
            raise ValueError("Verified traffic admission requires an exact receipt-backed capacity grant")
        self.capacity_grant = capacity_grant
        self.scoped_capacity = scoped_capacity
        if scoped_capacity is not None:
            from backend.onprem.capacity.typed_web_receipts import ScopedWebCapacity
            if type(scoped_capacity) is not ScopedWebCapacity:
                raise ValueError('Exact scoped capacity provider required')
        self.capacity_grant_reader = capacity_grant_reader
        self.ledger, self.resources = ledger, resources
        if scoped_capacity is not None:
            self.resources.scoped_capacity = scoped_capacity
        from backend.onprem.storage.measurement_timings import MeasurementTimingRepository
        self.measurement_timings = (measurement_timings if measurement_timings is not None
                                    else MeasurementTimingRepository(ledger.path))
        self._owns_measurement_timings = measurement_timings is None
        if hasattr(self.resources, "round_reader"):
            self.resources.round_reader = current_reader(self.ledger)
        if hasattr(self.resources, "control_reader"):
            self.resources.control_reader = self.ledger.batch_control
        if hasattr(self.resources, "request_cancel_check"):
            self.resources.request_cancel_check = self._check_cancel
        self.policy_bridge, self.ai_planner = policy_bridge, ai_planner
        self.group_adapter, self.group_oracle = group_adapter, group_oracle
        self.group_ai_planner = group_ai_planner
        if adaptive_runtime is not None and (verified_max_ues == 100 or ai_trial):
            adaptive_runtime.bind(current_reader(self.ledger))
        self.adaptive_runtime = adaptive_runtime
        self.adaptive_ai_ready = ((verified_max_ues == 100 or ai_trial) and adaptive_runtime is not None
                                  and getattr(adaptive_runtime, "adapter", None) is not None)
        # Dependencies alone do not authorize 500-UE traffic. Keep the public
        # capability closed until admission has an independently verified limit.
        self.group_ai_ready = (verified_max_ues == 500 and all(value is not None for value in
                               (group_adapter, group_oracle, group_ai_planner)))
        self.verified_max_ues = verified_max_ues
        self.calibration = calibration if calibration is not None else CalibrationAdmission()
        if not isinstance(self.calibration, CalibrationAdmission):
            raise ValueError("Trusted operator calibration configuration required")
        self.capabilities = {**type(self).capabilities, **self.calibration.capabilities(),
                              "reason": ("Unverified commissioning: candidate load is not verified capacity; policies remain gated"
                                         if self.calibration.enabled else "NONE verified native profile remains at most 1 UE; policy strategies remain gated")}
        if scoped_capacity is not None:
            self.capabilities['scopedVerifiedTrafficProfiles'] = [scoped_capacity.capabilities()]
        session_capabilities=getattr(resources,'session_workload_capabilities',None)
        if callable(session_capabilities):
            self.capabilities['sessionWorkload']=session_capabilities()
        if self.group_ai_ready:
            self.capabilities.update(aiPolicy=True, groupAiPolicy=True,
                                     reason="Frozen-oracle 500-UE group AI providers are commissioned")
        else:
            self.capabilities.update(groupAiPolicy=False,
                                     reason=group_ai_reason or self.capabilities["reason"])
        if self.adaptive_ai_ready:
            self.capabilities.update(aiPolicy=True, verifiedMaxUes=0 if ai_trial else 100,
                                     admissionMaxUes=100,
                                     candidateMaxUes=100 if ai_trial else None,
                                     capacityStatus="unverified_ai_trial" if ai_trial else "receipt_verified",
                                     reason=("Unverified 100-UE AI trial with native QER providers"
                                             if ai_trial else "100-UE adaptive AI and native QER providers commissioned"))
        from backend.onprem.scenarios.intent_bridge import strategy_capabilities, diagnostics_enabled
        self.capabilities.update(strategy_capabilities())
        if not diagnostics_enabled():
            self.capabilities.update(aiPolicy=False,
                reason='Formal AI requires /api/intents; legacy treatment/holdout diagnostics are disabled')
        self.poll_seconds, self.setup_timeout = poll_seconds, setup_timeout
        self.cleanup_timeout, self.result_seconds = cleanup_timeout, result_seconds
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._task = None
        self._cancel = asyncio.Event()
        self._intent_lease_wake = asyncio.Event()
        # Population publication waits for the bounded sampler/RTT window;
        # ordinary measurement never owns the native population writer.
        self._population_window_lock = asyncio.Lock()
        self.population_refresh = None
        self.intent_traffic_restorer,self._wall_clock=intent_traffic_restorer,wall_clock
        self.intent_traffic_renewer=intent_traffic_renewer
        from backend.onprem.storage.scenario_queue import ScenarioScheduleRepository
        self.schedule = ScenarioScheduleRepository(ledger)

    def _update(self, **patch):
        return mutate_current(self.ledger, patch)

    def _action(self, item):
        return mutate_current(self.ledger, append=item)

    def _finish_action(self, identifier, *, snapshot_patch=None, **patch):
        return mutate_current(self.ledger, snapshot_patch, action_id=identifier, action_patch=patch)

    async def snapshot(self):
        snapshot = await asyncio.to_thread(current_reader(self.ledger))
        snapshot["capabilities"] = deepcopy(self.capabilities)
        runtime = getattr(self, "adaptive_runtime", None)
        status = getattr(runtime, "status", None)
        if callable(status):
            progress = status()
            if (isinstance(progress, dict) and snapshot.get("roundId") is not None
                    and (progress.get("roundId"), progress.get("generation"))
                    == (snapshot.get("roundId"), snapshot.get("generation"))):
                snapshot["agentRuntime"] = progress
        from backend.onprem.storage.rounds import maintenance_pending
        if maintenance_pending(snapshot):
            snapshot["capabilities"].update(start=False, staticPolicy=False, aiPolicy=False,
                                            reason="Maintenance reservation is unresolved")
        return snapshot

    async def summary_read(self):
        """Display projection without raw hydration or native observation."""
        def read():
            from backend.onprem.scenarios.presentation import project_round_summary
            from backend.onprem.scenarios.presentation import with_traffic_counts
            from backend.onprem.scenarios.presentation import project_agent_runtime, project_capabilities
            projected_reader = getattr(self.ledger, 'live_summary_read', None) or getattr(self.ledger, 'projected_summary_read', None)
            projected = callable(projected_reader)
            reading = projected_reader() if projected else self.ledger.summary_read()
            snapshot = reading.checked_snapshot()
            snapshot["capabilities"] = (project_capabilities(self.capabilities) if projected
                                        else deepcopy(self.capabilities))
            status = getattr(getattr(self, "adaptive_runtime", None), "status", None)
            if callable(status):
                progress = status()
                if (isinstance(progress, dict) and snapshot.get("roundId") is not None
                        and (progress.get("roundId"), progress.get("generation"))
                        == (snapshot.get("roundId"), snapshot.get("generation"))):
                    if projected:
                        progress = project_agent_runtime(progress)
                        if progress is None:
                            snapshot.pop('agentRuntime', None)
                        else:
                            snapshot['agentRuntime'] = progress
                    else:
                        snapshot["agentRuntime"] = progress
            if reading.maintenance_pending:
                snapshot["capabilities"].update(start=False, staticPolicy=False, aiPolicy=False,
                    reason="Admission stopped or policy ownership is unresolved")
            if projected:
                return reading.with_snapshot(snapshot)
            # Project before the traffic-count helper's deepcopy, so duplicated
            # private report trees never enter that display-only copy.
            return reading.with_snapshot(with_traffic_counts(project_round_summary(snapshot)))
        return await asyncio.to_thread(read)

    async def summary_snapshot(self):
        """Keep the existing display dictionary contract."""
        return (await self.summary_read()).checked_snapshot()

    def _maintenance_guard(self):
        from backend.onprem.storage.rounds import maintenance_pending
        if maintenance_pending(read_cleanup(self.ledger)):
            raise RoundConflict("Maintenance reservation is unresolved")

    def _baseline_fences_match(self, round_fence, resident_fence):
        latest_round = read_cleanup(self.ledger)
        current = getattr(self.resources, "current_generation", lambda: None)()
        latest_resident = None if current is None else (current.get("generation"), current.get("desired"))
        return (tuple(latest_round.get(key) for key in ("roundId", "stage", "sequence")) == round_fence
                and latest_resident == resident_fence)

    async def _pre_reserve_baseline(self, body, total_ues, previous_round):
        """Retry only a stale resident observation before any round is reserved."""
        requested_counts = slice_counts({"baselineUes": body["baselineUes"],
                                         "events": body["events"], "total": total_ues})
        round_fence = tuple(previous_round.get(key) for key in ("roundId", "stage", "sequence"))
        current = getattr(self.resources, "current_generation", lambda: None)()
        resident_fence = None if current is None else (current.get("generation"), deepcopy(current.get("desired")))
        deadline = self._monotonic() + 60
        while True:
            fleet_lease = None
            try:
                lease_generation = getattr(self.resources, "lease_generation", None)
                if total_ues and callable(lease_generation):
                    self._baseline_phase = "resident_lease"
                    resident = self.resources.current_generation()
                    if (resident and any(resident.get("desired", {}).values())
                            and requested_counts != resident.get("desired")
                            and body["strategy"] != "none"
                            and not (body["strategy"] == "ai" and self.adaptive_ai_ready)):
                        raise RoundRuntimeBlocked("Resident subset policy requires the adaptive runtime")
                    fleet_lease = lease_generation(requested_counts)
                elif total_ues:
                    self._baseline_phase = "resident_reuse"
                    reusable = getattr(self.resources, "reusable_generation", None)
                    if callable(reusable):
                        reusable(requested_counts)
                self._baseline_phase = "native_scope"
                with admission_span('admission.native_baseline'):
                    clean = await self.resources.baseline()
                if not self._baseline_fences_match(round_fence, resident_fence):
                    raise RoundRuntimeBlocked("Resident admission fence changed during freshness retry")
                if self._monotonic() >= deadline:
                    raise RoundRuntimeBlocked("Resident evidence freshness retry exhausted")
                return fleet_lease, clean, round_fence, resident_fence, deadline
            except RoundRuntimeBlocked as exc:
                if (type(exc) is not RoundRuntimeBlocked or str(exc) not in RESIDENT_FRESHNESS_ERRORS
                        or resident_fence is None):
                    raise
                self._baseline_phase = "resident_freshness_retry"
                if not self._baseline_fences_match(round_fence, resident_fence):
                    raise RoundRuntimeBlocked("Resident admission fence changed during freshness retry") from None
                if self._monotonic() >= deadline:
                    raise RoundRuntimeBlocked("Resident evidence freshness retry exhausted") from None
                await asyncio.sleep(min(self.poll_seconds, 2))

    async def start(self, body, *, trusted_lease=None,held_reservation=None):
        body = validate_round(body)
        if trusted_lease is not None:
            from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
            if (type(trusted_lease) is not ServiceIntentTrafficLease or body['strategy']!='none'
                    or trusted_lease.request_id!=body['requestId'] or not trusted_lease.active(self._wall_clock())):
                raise RoundConflict('Active exact trusted service traffic lease required')
            if not callable(self.intent_traffic_restorer):
                raise RoundUnavailable('Owner-safe intent traffic restoration is not configured')
        from backend.onprem.scenarios.intent_bridge import require_diagnostic_strategy
        require_diagnostic_strategy(body)
        async with timed_admission_lock(self._lock):
            self._maintenance_guard()
            replay = replay_current(self.ledger, body)
            if replay is not None:
                if trusted_lease is not None:
                    entry=self.ledger.intent_traffic_lease(replay['roundId'])
                    if entry is None or entry['lease']!=trusted_lease.to_dict():
                        raise RoundConflict('Request belongs to a different traffic lease')
                return replay
            from backend.onprem.traffic.admission import admit_traffic
            if held_reservation is not None:
                validate_held=getattr(self.resources,'validate_held_workload',None)
                if callable(validate_held):validate_held(held_reservation,body)
            admit_traffic(body)
            current = read_cleanup(self.ledger)
            # The ledger checks duplicate fingerprints before baseline gating.
            if current.get("requestId") == body["requestId"]:
                with admission_span('admission.db_reserve'):
                    return reserve_current(self.ledger, body, baseline_verified=False, capabilities=self.capabilities)
            if current["roundId"] and current["stage"] != "completed":
                raise RoundConflict("Previous round still owns reserved resources")
            spatial_resolution = None
            if body.get("demandGroups"):
                prepare = getattr(self.resources, "prepare_spatial", None)
                if not callable(prepare):
                    raise RoundUnavailable("Native spatial RAN admission is not configured")
                spatial_resolution = await asyncio.to_thread(prepare, body)
            total_ues = body["baselineUes"] + sum(body["events"].values())
            scoped_trial, scoped_verified = (None, False)
            if self.scoped_capacity is not None:
                from backend.onprem.capacity.typed_web import ScopedCapacityBlocked
                try:
                    scoped_trial, scoped_verified = await asyncio.to_thread(self.scoped_capacity.prepare, body)
                except ScopedCapacityBlocked:
                    raise RoundUnavailable('Exact scoped capacity evidence is unavailable or changed') from None
            group_ai = body["strategy"] == "ai" and total_ues == 500
            adaptive_ai = body["strategy"] == "ai" and 1 <= total_ues <= 100
            # Reuse only a real verified traffic grant, never an AI trial candidate ceiling.
            verified_none = (body['strategy'] == 'none' and self.verified_max_ues == 100
                             and 1 <= total_ues <= self.verified_max_ues)
            if (adaptive_ai and self.adaptive_ai_ready) or verified_none:
                try:
                    with admission_span('admission.capacity_grant'):
                        current_grant = await asyncio.to_thread(self.capacity_grant_reader)
                except Exception:
                    raise RoundUnavailable("AI startup evidence is unavailable") from None
                if current_grant != self.capacity_grant:
                    raise RoundUnavailable("AI startup evidence changed")
            if body["strategy"] != "none" and ((group_ai and not self.group_ai_ready)
                    or (adaptive_ai and not self.adaptive_ai_ready)
                    or (not group_ai and not adaptive_ai and (self.policy_bridge is None
                        or self.capabilities.get(body["strategy"] + "Policy") is not True
                        or (body["strategy"] == "ai" and self.ai_planner is None)))):
                raise RoundUnavailable("Official policy strategy capability is not enabled")
            try:
                if body["strategy"] == "none":
                    if not verified_none and not scoped_verified:
                        self.calibration.admit(total_ues=total_ues, strategy="none", profile_id=PROFILE_ID)
                elif not group_ai and not adaptive_ai and total_ues != 2:
                    raise CalibrationBlocked("Commissioned policy scope requires exactly two UEs")
                elif group_ai and (self.verified_max_ues != 500 or body["baselineUes"] != 0):
                    raise CalibrationBlocked("Commissioned group AI requires the exact 500-UE event profile")
            except CalibrationBlocked as exc:
                raise RoundConflict("Requested traffic concurrency exceeds the verified native profile or operator commissioning limit") from exc
            self._baseline_phase = "admission"
            try:
                fleet_lease, clean, round_fence, resident_fence, deadline = await self._pre_reserve_baseline(body, total_ues, current)
                if not self._baseline_fences_match(round_fence, resident_fence):
                    raise RoundRuntimeBlocked("Resident admission fence changed during freshness retry")
                if self._monotonic() >= deadline:
                    raise RoundRuntimeBlocked("Resident evidence freshness retry exhausted")
            except Exception as exc:
                # Exact trusted classes only: subclass names/messages may contain
                # remote data. Never log traceback, request body or exception text.
                error_type = {
                    TimeoutError: "TimeoutError", OSError: "OSError",
                    ValueError: "ValueError", RuntimeError: "RuntimeError",
                    EvidenceUnavailable: "EvidenceUnavailable",
                    RoundRuntimeBlocked: "RoundRuntimeBlocked",
                    ReleaseProofBlocked: "ReleaseProofBlocked",
                    PolicyBlocked: "PolicyBlocked",
                }.get(type(exc), "UnknownError")
                logging.getLogger(__name__).warning(
                    "Native admission blocked phase=resource_baseline subphase=%s reasonCode=%s errorType=%s",
                    self._baseline_phase,
                    (exc.code if type(exc) is ReleaseProofBlocked and
                     isinstance(exc.code, str) and exc.code.startswith("release.") else
                     "resident_freshness_timeout" if type(exc) is RoundRuntimeBlocked and
                     str(exc) == "Resident evidence freshness retry exhausted" else
                     "resident_fence_changed" if type(exc) is RoundRuntimeBlocked and
                     str(exc) == "Resident admission fence changed during freshness retry" else "blocked"),
                    error_type)
                raise RoundUnavailable("Fresh known-scope resource baseline is unavailable") from None
            if body.get("demandGroups") and fleet_lease is not None:
                raise RoundUnavailable("Spatial traffic requires a new location-bound UE allocation")
            with admission_span('admission.db_reserve'):
                snapshot = reserve_current(self.ledger, body, baseline_verified=clean, capabilities=self.capabilities,
                                               fleet_lease=fleet_lease,
                                               **({'held_reservation':held_reservation} if held_reservation is not None else {}),
                                               **({'service_intent_lease':trusted_lease} if trusted_lease is not None else {}))
            if scoped_trial is not None:
                # Durable protocol binding precedes _execute and its UE write.
                self._action(action('scoped_capacity_trial', **scoped_trial))
                snapshot = read_cleanup(self.ledger)
            if spatial_resolution is not None:
                spatial = deepcopy(snapshot["spatialDemand"])
                spatial["resolution"] = deepcopy(spatial_resolution)
                if isinstance(spatial_resolution, dict):
                    spatial["resolvedGroups"] = deepcopy(spatial_resolution.get("resolvedGroups", []))
                self._update(spatialDemand=spatial)
                snapshot = read_cleanup(self.ledger)
            if snapshot["stage"] == "completed" or snapshot["roundId"] != read_cleanup(self.ledger)["roundId"]:
                return snapshot
            if group_ai:
                mix = self._oracle_mix(body["events"])
                self._action(action("frozen_group_oracle", sha256=self.group_oracle.sha256,
                                    mixId=mix["mixId"], artifactId=self.group_oracle.artifact["artifactId"],
                                    accepted=True))
                snapshot = read_cleanup(self.ledger)
            self._cancel.clear()
            self._task = asyncio.create_task(self._execute(snapshot["roundId"]))
            return snapshot

    async def cancel(self, round_id, request_id=None):
        async with self._lock:
            self._maintenance_guard()
            current = read_cleanup(self.ledger)
            if current["roundId"] != round_id:
                raise RoundConflict("Cancellation does not own current round")
            if self._intent_lease(round_id) is not None:
                raise RoundConflict('Formal intent traffic must be released through its owning intent')
            if current["stage"] == "completed":
                return current
            self._cancel.set()
            if not self._task or self._task.done():
                self._task = asyncio.create_task(self._recover())
            return read_cleanup(self.ledger)

    async def recover(self):
        """Invoke once after native controller acquires its process fence."""
        async with self._lock:
            self._maintenance_guard()
            current = read_cleanup(self.ledger)
            if current.get("roundId") and current.get("stage") == "completed":
                self._task = asyncio.create_task(self._release_constrained_fixture(current))
                return
            if current["roundId"] and current["stage"] != "completed" and (not self._task or self._task.done()):
                self._task = asyncio.create_task(self._recover())

    def execution_drained(self):
        """Release publication can precede the final fixture journal closeout."""
        return self._task is None or self._task.done()

    async def wait(self):
        if self._task:
            await asyncio.shield(self._task)

    async def close(self):
        # Do not cancel asyncio.to_thread workers: the bounded native commands
        # must return before normal owned cleanup can be claimed.
        self._cancel.set()
        await self.wait()
        if self._owns_measurement_timings:
            await asyncio.to_thread(self.measurement_timings.close, 5.)

    def _check_cancel(self):
        if self._cancel.is_set():
            owned=self._owned_cancellation()
            if owned is not None:
                raise owned
            raise RoundRuntimeBlocked("Round cancelled")

    def _owned_cancellation(self):
        reader=getattr(self.ledger,'intent_traffic_drain_authority',None)
        if not callable(reader):return None
        try:
            current=self.ledger.ownership_control()
            authority=reader(current['roundId'])
            if authority is None:return None
            lease,permit=authority['lease'],authority['permit']
            permit.validate(lease,self._wall_clock())
            return OwnedRoundCancelled(current['roundId'],lease,permit)
        except Exception:
            return None  # Missing/foreign/stale authority stays a real guard failure.

    async def _execute(self, round_id):
        try:
            snapshot = read_cleanup(self.ledger)
            if snapshot["roundId"] != round_id:
                raise RoundRuntimeBlocked("Round ownership changed")
            self._check_cancel()
            if snapshot["allocation"]["total"]:
                self._update(stage="allocating")
                intent = action("ue_allocation", counts=slice_counts(snapshot["allocation"]))
                self._action(intent)  # Journal precedes the controller write.
                existing = ({"generation": snapshot["fleetLease"]["generation"]}
                            if snapshot.get("fleetLease") else None)
                reusable = getattr(self.resources, "reusable_generation", None)
                if existing is None and callable(reusable) and not snapshot.get("spatialDemand"):
                    existing = reusable(intent["counts"])
                held_reader=getattr(self.ledger,'intent_traffic_held',None)
                held=await asyncio.to_thread(held_reader,round_id) if callable(held_reader) else None
                if held is not None and existing is not None:
                    raise RoundRuntimeBlocked('Held new allocation cannot reuse an unrelated UE generation')
                request_options = {'reservation': held} if held is not None else {}
                if snapshot.get("spatialDemand"):
                    spatial_request = {key: deepcopy(value) for key, value in snapshot["allocation"].items() if key != "total"}
                    spatial_request.update(requestId=snapshot["requestId"], strategy=snapshot["strategy"])
                    request_options["spatial_request"] = spatial_request
                result = existing or await self.resources.request(intent["counts"], **request_options)
                self._finish_action(intent["id"], accepted=True, generation=result["generation"],
                                    reused=existing is not None,
                                    nativeRequestDiagnostic=RequestGuardDiagnostic.project(result.get('nativeRequestDiagnostic')),
                                    snapshot_patch={"generation": result["generation"], "stage": "registering"})
                await self._established()
                await asyncio.to_thread(self.schedule.patch_lifecycle, round_id, evaluationState='running')
                if (snapshot.get("strategy") == "ai" and 1 <= snapshot["allocation"]["total"] <= 100
                        and self.adaptive_runtime is not None):
                    from backend.onprem.validation.normal import claim_normal
                    from backend.onprem.validation.constrained import SCHEMA as CONSTRAINED_SCHEMA
                    fixture_claim = await asyncio.to_thread(
                        claim_normal, self.adaptive_runtime.journal_path, read_cleanup(self.ledger),
                        expected_config_sha256=self.adaptive_runtime.fixture_config_sha256)
                    if fixture_claim is not None:
                        self._action(action("scenario_validation_track", accepted=True,
                                            fixtureClaim=fixture_claim))
                    controlled_claim = await asyncio.to_thread(
                        self.adaptive_runtime.claim_constrained, read_cleanup(self.ledger))
                    if snapshot.get("trafficMode") == "qos" and (fixture_claim is not None or controlled_claim is not None):
                        raise RoundRuntimeBlocked("QoS experiment conflicts with a diagnostic validation track")
                    if fixture_claim is not None and controlled_claim is not None:
                        raise RoundRuntimeBlocked("Conflicting validation tracks")
                    if controlled_claim is not None:
                        if controlled_claim.get("schema") != CONSTRAINED_SCHEMA:
                            raise RoundRuntimeBlocked("Controlled validation track is invalid")
                        self._action(action("scenario_validation_track", accepted=True,
                                            fixtureClaim=controlled_claim))
                        await asyncio.to_thread(
                            self.adaptive_runtime.install_constrained, self, controlled_claim)
                await self._measure("measuring_baseline")
                if (snapshot.get("strategy") == "ai" and 1 <= snapshot["allocation"]["total"] <= 100):
                    outcomes, publication = await self.adaptive_runtime.run(self)
                elif snapshot.get("strategy") == "none":
                    self._action({**action("no_additional_policy"), "requested": False,
                                  "reason": "Existing four slices and baseline core QoS are unchanged"})
                else:
                    await self._apply_policy()
                if not (snapshot.get("strategy") == "ai" and 1 <= snapshot["allocation"]["total"] <= 100):
                    publication = await self._measure("measuring_result")
                if snapshot.get("strategy") == "ai" and snapshot["allocation"]["total"] == 500:
                    self._verify_group_effect()
            else:
                self._action({**action("zero_workload"), "requested": False,
                              "reason": "No UE, traffic worker or AI action started"})
            self._check_cancel()
            if snapshot["allocation"]["total"]:
                persistent = (snapshot.get('strategy') == 'ai'
                              and getattr(self.adaptive_runtime, 'persistent', False))
                managed=self._intent_lease(round_id) is not None
                if persistent or managed:
                    # Keep the native execution stage at measuring_result. The
                    # separate evaluation lifecycle announces completion;
                    # results_ready is a terminal publication, not a loop state.
                    pass
                elif publication is not None:
                    await asyncio.to_thread(self.ledger.publish_batch_results, publication)
                else:
                    await asyncio.to_thread(self._update, stage="results_ready", executionOutcome="passed")
                if (snapshot.get("strategy") == "ai" and 1 <= snapshot["allocation"]["total"] <= 100):
                    await asyncio.to_thread(self._action, action("adaptive_ai_outcomes", outcomes=outcomes,
                                        accepted=True, cleanupVerified=not persistent))
                    await asyncio.to_thread(self.schedule.patch_lifecycle, round_id,
                        evaluationState='completed', cyclesCompleted=min(5, len(outcomes)),
                        stopReason='cycle_limit_reached')
                    if persistent:
                        await self._hold_configuration(round_id)
                        await asyncio.to_thread(self._update, executionOutcome='passed')
                if managed:
                    await self._hold_configuration(round_id)
                    current=await asyncio.to_thread(read_cleanup,self.ledger)
                    if current['executionOutcome'] is None:
                        await asyncio.to_thread(self._update,executionOutcome='passed')
                else:
                    await asyncio.sleep(self.result_seconds)
            else:
                # There is no measurement result or UE generation for zero work.
                self._update(executionOutcome="passed")
        except Exception as exc:
            await asyncio.to_thread(self._record_execution_failure, exc)
        finally:
            await self._cleanup()
            if self.adaptive_runtime is not None and self._intent_lease(round_id) is None:
                current = await asyncio.to_thread(current_reader(self.ledger))
                if current.get("stage") == "completed" and current.get("cleanupStatus") == "verified":
                    from backend.onprem.validation.normal import complete_normal
                    await asyncio.to_thread(complete_normal, self.adaptive_runtime.journal_path, current)
                    await self._release_constrained_fixture(current)

    async def _hold_configuration(self, round_id):
        """Keep the declared traffic/owned policy running after LLM evaluation.

        Native windows retain their individual bounds and exact cleanup. The
        scenario has no elapsed-time deadline; cancel/handoff ends this loop.
        """
        if self._intent_lease(round_id) is not None:
            return await self._hold_intent_traffic(round_id)
        while not self._cancel.is_set():
            current = await asyncio.to_thread(getattr(self.ledger, 'ownership_control', current_reader(self.ledger)))
            if current['roundId'] != round_id:
                raise RoundRuntimeBlocked('Persistent scenario ownership changed')
            del current  # Owner check must not retain a report tree across native I/O.
            await self._measure('measuring_result')
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=max(.05, self.poll_seconds))
            except TimeoutError:
                pass

    def _intent_lease(self,round_id=None):
        reader=getattr(self.ledger,'intent_traffic_lease',None)
        return reader(round_id) if callable(reader) else None

    async def _renew_intent_traffic(self,lease):
        if not callable(self.intent_traffic_renewer):
            return lease
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        from backend.onprem.intents.service import invoke
        renewed=await invoke(self.intent_traffic_renewer,lease)
        if type(renewed) is not ServiceIntentTrafficLease:
            raise RoundRuntimeBlocked('Trusted current intent lease readback required')
        if renewed!=lease:
            await asyncio.to_thread(self.ledger.renew_intent_traffic_lease,lease,renewed)
        return renewed

    async def _ensure_intent_restored(self,round_id):
        entry=await asyncio.to_thread(self._intent_lease,round_id)
        if entry is None or entry['state'] in {'released','draining','drained'}:
            return
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficRestoration
        lease=ServiceIntentTrafficLease.from_dict(entry['lease'])
        await asyncio.to_thread(self.ledger.request_intent_traffic_release,lease)
        if not callable(self.intent_traffic_restorer):
            raise RoundRuntimeBlocked('Owner-safe intent restoration is unavailable; UE resources retained')
        from backend.onprem.intents.service import invoke
        result=await invoke(self.intent_traffic_restorer,lease)
        if type(result) is IntentTrafficRestoration:
            await asyncio.to_thread(self.ledger.finish_intent_traffic,lease,result)
        deadline=self._monotonic()+self.cleanup_timeout
        while True:
            entry=await asyncio.to_thread(self._intent_lease,round_id)
            if entry is None or entry['lease']!=lease.to_dict():
                raise RoundRuntimeBlocked('Intent traffic ownership changed during restoration')
            if entry['state'] in {'released','draining','drained'}:
                return
            if self._monotonic()>=deadline:
                raise RoundRuntimeBlocked('Native intent restoration is not confirmed; UE resources retained')
            self._intent_lease_wake.clear()
            try:
                await asyncio.wait_for(self._intent_lease_wake.wait(),timeout=max(.01,self.poll_seconds))
            except TimeoutError:
                pass

    async def _hold_intent_traffic(self,round_id):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        while True:
            entry=await asyncio.to_thread(self._intent_lease,round_id)
            current=await asyncio.to_thread(getattr(self.ledger,'ownership_control',current_reader(self.ledger)))
            if entry is None or current['roundId']!=round_id:
                raise RoundRuntimeBlocked('Formal intent traffic ownership changed')
            lease=ServiceIntentTrafficLease.from_dict(entry['lease'])
            if entry['state'] in {'released','draining','drained'}:
                return
            if entry['state']=='active' and not self._cancel.is_set():
                lease=await self._renew_intent_traffic(lease)
            if self._cancel.is_set() or entry['state']=='restoring' or not lease.active(self._wall_clock()):
                await self._ensure_intent_restored(round_id)
                return
            await self._measure('measuring_result')
            self._intent_lease_wake.clear()
            try:
                await asyncio.wait_for(self._intent_lease_wake.wait(),timeout=max(.01,self.poll_seconds))
            except TimeoutError:
                pass

    async def finish_intent_traffic(self,lease,proof):
        """Private owner-safe permit, followed by actual verified UE cleanup."""
        await asyncio.to_thread(self.ledger.finish_intent_traffic,lease,proof)
        self._intent_lease_wake.set()
        current=await asyncio.to_thread(read_cleanup,self.ledger)
        already_clean=(current['stage']=='completed' and current['cleanupStatus']=='verified')
        if not already_clean and (not self._task or self._task.done()):
            self._task=asyncio.create_task(self._recover())
        if not already_clean:
            await self.wait()
        current=await asyncio.to_thread(read_cleanup,self.ledger)
        entry=await asyncio.to_thread(self._intent_lease,current['roundId'])
        released=(entry is not None and entry['lease']==lease.to_dict()
            and current['stage']=='completed' and current['cleanupStatus']=='verified'
            and current.get('releaseProof',{}).get('uesReleased') is True
            and current.get('releaseProof',{}).get('podsAbsent') is True)
        if released:
            native=await self.resources.release_proof(current)
            required=('trafficStopped','policiesAbsent','rulesRestored','uesReleased','podsAbsent')
            released=all(native.get(k) is True for k in required) and fresh(native.get('observedAt'))
            if released and self.scoped_capacity is not None:
                await asyncio.to_thread(self._capture_scoped_cleanup, current)
        return {'released':released,'pending':not released,'roundId':current['roundId'],
                'service_intent_traffic_lease':lease.to_dict()}

    async def drain_intent_traffic(self,lease,permit):
        # Serialize authority validation and signalling with start/cancel. A
        # valid formal drain must interrupt setup as well as the lease hold
        # loop; signalling only _intent_lease_wake leaves setup waiting until
        # its full deadline. Do not cancel an in-flight bounded native worker.
        async with self._lock:
            current=await asyncio.to_thread(read_cleanup,self.ledger)
            if current.get('stage')=='completed':
                generation=self.resources.current_generation()
                stops=[a for a in current.get('actions',()) if a.get('kind')=='ue_stop' and a.get('accepted') is True]
                expected=stops[-1]['generation'] if stops else current.get('generation')
                if generation is None or generation.get('generation')!=expected:
                    raise RoundRuntimeBlocked('Terminal traffic round no longer owns current UE generation')
            await asyncio.to_thread(self.ledger.drain_intent_traffic,lease,permit)
            current=await asyncio.to_thread(read_cleanup,self.ledger)
            if current['stage']!='completed':
                self._cancel.set()
            self._intent_lease_wake.set()
        if current['stage']=='completed':
            proof=await self.resources.release_proof(current)
            await asyncio.to_thread(self.ledger.confirm_intent_traffic_drained,current['roundId'],proof)
        elif not self._task or self._task.done():
            self._task=asyncio.create_task(self._recover())
        if current['stage']!='completed':
            await self.wait()
        entry=await asyncio.to_thread(self._intent_lease)
        drained=entry is not None and entry['lease']==lease.to_dict() and entry['state']=='drained'
        if drained:
            current=await asyncio.to_thread(read_cleanup,self.ledger)
            stop=next((a for a in current['actions'] if a['kind']=='ue_stop' and a.get('accepted') is True),None)
            generation=self.resources.current_generation()
            if stop is not None and (generation is None or generation.get('generation')!=stop['generation']):
                raise RoundRuntimeBlocked('UE generation changed after intent drain')
            proof=await self.resources.release_proof(current)
            await asyncio.to_thread(self.ledger.confirm_intent_traffic_drained,current['roundId'],proof)
        return dict(drained=drained,native_ues_gone=drained,pending=not drained,
            service_intent_traffic_lease=lease.to_dict())

    async def _complete_or_drain(self,current,proof):
        entry=await asyncio.to_thread(self._intent_lease,current['roundId'])
        confirm = getattr(getattr(self.resources, 'controller', None), 'confirm_population_stopped', None)
        if current.get('populationPending') or (entry is not None and callable(confirm)):
            from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
            if (entry is None or not callable(confirm) or not fresh(proof.get('observedAt'))
                    or any(proof.get(k) is not True for k in ('trafficStopped', 'policiesAbsent',
                        'rulesRestored', 'uesReleased', 'podsAbsent'))):
                raise RoundRuntimeBlocked('Population whole-fleet stop lacks native completion authority')
            await confirm(ServiceIntentTrafficLease.from_dict(entry['lease']), proof)
        if entry is not None and entry['state']=='draining':
            await asyncio.to_thread(self.ledger.confirm_intent_traffic_drained,current['roundId'],proof)
            return
        await asyncio.to_thread(complete_current,self.ledger,current['roundId'],current['sequence'],proof)
        if self.scoped_capacity is not None:
            completed = await asyncio.to_thread(read_cleanup,self.ledger)
            await asyncio.to_thread(self._capture_scoped_cleanup, completed)

    def _capture_scoped_cleanup(self, current):
        """Private evidence only; completion/absence authority remains native."""
        if self.scoped_capacity is None:
            return
        from backend.onprem.capacity.typed_web_receipts import released_snapshot
        completed = released_snapshot(self.ledger.path,current['roundId'])
        self.scoped_capacity.record(completed, 'cleanup',
            dict(snapshot=completed,
                 generation=self.resources.current_generation(), native=self.resources.controller.status()))

    async def _release_constrained_fixture(self, current):
        from backend.onprem.validation.constrained import SCHEMA as CONSTRAINED_SCHEMA
        from backend.onprem.validation.restore import release_restored_fixture
        controlled = [item for item in current.get("actions", [])
                      if type(item) is dict and item.get("kind") == "scenario_validation_track"
                      and type(item.get("fixtureClaim")) is dict
                      and item["fixtureClaim"].get("schema") == CONSTRAINED_SCHEMA]
        if not controlled:
            runtime=self.adaptive_runtime
            from backend.onprem.validation.constrained import fixture_path, read_constrained_receipt, retire_unclaimed_constrained
            if runtime is not None and fixture_path(runtime.journal_path).exists():
                record=await asyncio.to_thread(read_constrained_receipt,runtime.journal_path,current['requestId'])
                if record is not None and record['state']=='armed':
                    if not await asyncio.to_thread(self.ledger.released_snapshot_matches,current):
                        raise RoundRuntimeBlocked('Unclaimed fixture round is not durably released')
                    await asyncio.to_thread(retire_unclaimed_constrained,runtime.journal_path,current,
                        expected_config_sha256=runtime.fixture_config_sha256,
                        expected_quarantine_registry=runtime.fixture_quarantine_registry)
            return
        if (len(controlled) != 1 or self.adaptive_runtime is None
                or current.get("stage") != "completed"
                or current.get("cleanupStatus") != "verified"):
            raise RoundRuntimeBlocked("Controlled fixture release has no verified owner")
        await asyncio.to_thread(
            release_restored_fixture, self.adaptive_runtime.journal_path, current,
            expected_config_sha256=self.adaptive_runtime.fixture_config_sha256,
            expected_quarantine_registry=self.adaptive_runtime.fixture_quarantine_registry)


    def _record_execution_failure(self, exc):
        """Persist sanitized causal stage atomically before cleanup changes it."""
        current = read_cleanup(self.ledger)
        if type(exc) is OwnedRoundCancelled:
            owned=self._owned_cancellation()
            if owned is not None and owned.cancellation==exc.cancellation:
                self._update(executionOutcome='cancelled',actions=[*current['actions'],
                    action('owned_round_cancelled',requested=False,accepted=False,
                           cancellation=deepcopy(exc.cancellation))])
                return
        if any(item.get("kind") == "execution_failure" for item in current["actions"]):
            return  # Retry/recovery must not replace the original cause.
        stage = current.get("stage")
        stages = {"preflight", "allocating", "registering", "establishing_pdu",
                  "verifying_connectivity", "measuring_baseline", "measuring_result", "results_ready"}
        receipt = {**action("execution_failure"), "requested": False,
                   "stage": stage if stage in stages else "unknown",
                   "code": failure_code(exc), "observedAt": timestamp(),
                   "errorType": "EvidenceUnavailable" if type(exc) is EvidenceUnavailable else
                       "PolicyBlocked" if isinstance(exc, PolicyBlocked) else
                       "RoundRuntimeBlocked" if isinstance(exc, RoundRuntimeBlocked) else "UnexpectedError"}
        if type(exc) is EvidenceUnavailable:
            blocker = {
                'Current UE generation requires operator reconciliation before resize or retirement': 'ue.generation_not_ready',
                'Durable UE retirement quarantine blocks controller mutation': 'ue.retirement_quarantine',
                'UE allocation generation changed during native refresh': 'ue.request_generation_changed',
            }.get(str(exc))
            if blocker:
                receipt['nativeRequestBlocker'] = blocker
        diagnostic = RequestGuardDiagnostic.project(getattr(exc, 'nativeRequestDiagnostic', None))
        if diagnostic:
            receipt['nativeRequestDiagnostic'] = diagnostic
        if isinstance(getattr(exc, "nativeSnapshotDiagnostic", None), dict):
            receipt["nativeSnapshotDiagnostic"] = deepcopy(exc.nativeSnapshotDiagnostic)
        self._update(executionOutcome="cancelled" if self._cancel.is_set() else "failed",
                     actions=[*current["actions"], receipt],
                     errors=[*current["errors"], "Execution blocked: " + safe_reason(exc)])

    async def _established(self):
        deadline = self._monotonic() + self.setup_timeout
        ranks = {"registering": 0, "establishing_pdu": 1, "verifying_connectivity": 2}
        while self._monotonic() < deadline:
            self._check_cancel()
            snapshot, status = read_cleanup(self.ledger), self.resources.status()
            if status.get("generation") != snapshot["generation"]:
                raise RoundRuntimeBlocked("Native generation was superseded")
            if fresh(status.get("observedAt")):
                stage = {"registering": "registering", "establishing_pdu": "establishing_pdu"}.get(status.get("status"), "verifying_connectivity")
                stage = max((snapshot["stage"], stage), key=lambda s: ranks[s])
                leased = (self.resources.leased_status(snapshot["fleetLease"], status)
                          if snapshot.get("fleetLease") and hasattr(self.resources, "leased_status") else status.get("perUe", []))
                counts = ({key: sum(row.get(key) is True for row in leased)
                           for key in ("registered", "pduEstablished", "trafficVerified")}
                          | {"allocated": len(leased)})
                self._update(stage=stage, counts=counts, observedAt=status["observedAt"])
                rejected = [row for row in status.get('perUe', [])
                            if row.get('failureCode') == 'ue.registration_rejected'
                            and row.get('generation') == snapshot['generation']
                            and row.get('nas', {}).get('podUid') == row.get('podUid')
                            and row.get('nas', {}).get('supi') == row.get('supi')
                            and row.get('nas', {}).get('generation') == snapshot['generation']
                            and row.get('nas', {}).get('source') == 'owned-ueransim-nr-cli-status'
                            and row.get('nas', {}).get('rmState') == 'RM-DEREGISTERED'
                            and row.get('nas', {}).get('updateState') == '5U3-ROAMING-NOT-ALLOWED'
                            and str(row.get('nas', {}).get('mmState', '')).startswith('MM-DEREGISTERED/')
                            and fresh(row.get('nas', {}).get('observedAt'))]
                if rejected:
                    self._update(perUe=deepcopy(rejected))
                    raise RoundRuntimeBlocked('ue.registration_rejected')
                if status.get("status") == "established" or snapshot.get("fleetLease"):
                    required_counts = ("allocated", "registered", "pduEstablished") if snapshot.get("fleetLease") else tuple(counts)
                    if (not fresh(status.get("observedAt"))
                            or (not snapshot.get("fleetLease") and (not status.get("stableSince")
                                or seconds(status["observedAt"]) - seconds(status["stableSince"]) < 60))
                            or any(counts[key] != snapshot["allocation"]["total"] for key in required_counts)):
                        raise RoundRuntimeBlocked("Native 60-second identity gate is incomplete")
                    bindings = bind_scenarios(snapshot["allocation"], leased)
                    if status.get('schemaVersion') == V2:
                        if snapshot.get('fleetLease'):
                            raise RoundRuntimeBlocked('v2 resident fleet reuse is not commissioned')
                        scenarios = {r['supi']: r['scenario'] for r in bindings}
                        sessions = session_rows(status)
                        if any(r.get('trafficVerified') is not True or r['supi'] not in scenarios for r in sessions):
                            raise RoundRuntimeBlocked('Native v2 session connectivity gate is incomplete')
                        identities = [{k: r[k] for k in IDENTITY_FIELDS}
                                      | {'scenario': scenarios[r['supi']]}
                                      | {k: deepcopy(r[k]) for k in ('sst', 'sd', 'pfcpSeid', 'logicalSessionId', 'service_type', 'region',
                                                                   'proof', 'sourceLocation', 'demandGroupId', 'servingCellId') if k in r}
                                      for r in sessions]
                        self._update(schemaVersion=V2, devices=deepcopy(status['devices']),
                            perSession=identities, perUe=bindings, connectivityEvidence={
                                'source': 'native-ue-controller', 'roundId': snapshot['roundId'],
                                'generation': status['generation'], 'stableSince': status['stableSince'],
                                'observedAt': status['observedAt'], 'verifiedAt': timestamp(),
                                'identities': deepcopy(identities),
                                'evidenceSources': deepcopy(status.get('evidenceSources', []))})
                        await self._verify_spatial_connection()
                        held_reader = getattr(self.ledger, 'intent_traffic_held', None)
                        held = await asyncio.to_thread(held_reader, snapshot['roundId']) if callable(held_reader) else None
                        if held is not None and status.get('source') == 'native-free-ran-ue-single-session/v2':
                            from backend.onprem.evidence.native_binding_publication import NativeBindingPublication
                            current_snapshot = await asyncio.to_thread(self.ledger.live_snapshot, snapshot['roundId'])
                            publication = NativeBindingPublication.create(current_snapshot,
                                self.resources.current_generation(), held, status)
                            await asyncio.to_thread(self.ledger.publish_native_evidence,publication)
                        return
                    if any((not snapshot.get("fleetLease") and row.get("trafficVerified") is not True)
                           or not all(row.get(k) for k in ("podUid", "ip", "sessionId", "interface", "upf"))
                           for row in bindings):
                        raise RoundRuntimeBlocked("Native UE binding is incomplete")
                    identities = [{k: row[k] for k in ("scenario", "supi", "slice", "podUid", "sessionId", "upf")}
                                  | {"ueIp": row["ip"], "tun": row["interface"]}
                                  | {k: row[k] for k in ("smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch", "demandGroupId", "sourceLocation", "servingGnb", "servingCellId")
                                     if row.get(k) is not None} for row in bindings]
                    self._update(perUe=identities, connectivityEvidence={
                        "source": "native-ue-controller", "roundId": snapshot["roundId"],
                        "generation": status["generation"], "stableSince": status["stableSince"],
                        "observedAt": status["observedAt"], "verifiedAt": timestamp(),
                        "identities": deepcopy(identities),
                        "evidenceSources": deepcopy(status.get("evidenceSources", []))})
                    await self._verify_spatial_connection()
                    return
            await asyncio.sleep(self.poll_seconds)
        raise RoundRuntimeBlocked("Native connectivity setup deadline exceeded")

    async def _verify_spatial_connection(self):
        snapshot = read_cleanup(self.ledger)
        if not snapshot.get("spatialDemand"):
            return
        verify = getattr(self.resources, "verify_spatial_bindings", None)
        if not callable(verify):
            raise RoundRuntimeBlocked("Native spatial attachment verification is not configured")
        evidence = await asyncio.to_thread(verify, snapshot)
        if not isinstance(evidence, dict) or not evidence.get("bindings"):
            raise RoundRuntimeBlocked("Native spatial attachment evidence is incomplete")
        actual = {row.get("supi"): row for row in evidence["bindings"]}
        expected = {row["supi"]: row for row in snapshot["perUe"]}
        if (len(actual) != len(evidence["bindings"]) or set(actual) != set(expected)
                or evidence.get("generation") != snapshot["generation"]
                or evidence.get("topologyRevision") != snapshot["spatialDemand"]["topologyRevision"]
                or any(not row.get("servingCellId") or not row.get("gnbId") or not row.get("gnbPodUid")
                       or row.get("demandGroupId") != expected[supi].get("demandGroupId")
                       for supi, row in actual.items())):
            raise RoundRuntimeBlocked("Native spatial attachment scope differs from requested UE allocation")
        spatial = deepcopy(snapshot["spatialDemand"])
        spatial["actualBindings"] = deepcopy(evidence["bindings"])
        spatial["generation"] = snapshot["generation"]
        spatial["observedAt"] = evidence.get("observedAt")
        per_ue = deepcopy(snapshot["perUe"])
        for row in per_ue:
            native = actual.get(row["supi"], {})
            row["ranBinding"] = deepcopy(native)
            for key in ("servingCellId", "servingGnb", "demandGroupId", "sourceLocation", "ranBinding"):
                if key in native:
                    row[key] = deepcopy(native[key])
        connectivity = deepcopy(snapshot["connectivityEvidence"])
        connectivity["spatialRan"] = deepcopy(evidence)
        # This is the same verified identity set, enriched with independently
        # checked radio membership. Keep the durable PCF scope join coherent.
        connectivity["identities"] = deepcopy(snapshot['perSession'] if snapshot.get('schemaVersion') == V2 else per_ue)
        self._update(spatialDemand=spatial, connectivityEvidence=connectivity, perUe=per_ue)

    async def _measure_with_deadline(self, stage, deadline):
        return await self._measure(stage, deadline=deadline)

    async def _measure(self, stage, *, deadline=None):
        async with self._population_window_lock:
            if callable(self.population_refresh):
                await self.population_refresh()
            return await self._measure_locked(stage, deadline=deadline)

    async def _measure_locked(self, stage, *, deadline=None):
        publication = await self._measure_traffic(stage)
        observe = getattr(self.resources, "measure_rtt", None)
        if not callable(observe) or self._cancel.is_set() or (deadline is not None and self._monotonic() >= deadline):
            return publication
        try:
            snapshot = await asyncio.to_thread(current_reader(self.ledger))
            if snapshot.get('populationPending'):
                return publication
        except Exception as exc:
            logging.getLogger(__name__).warning("rtt.sidecar_scope_unavailable errorType=%s", type(exc).__name__)
            return publication
        windows = [a for a in snapshot.get("actions", []) if a.get("accepted") is True
                   and a.get("kind") in {"traffic_window", "traffic_aggregate"} and a.get("stage") == stage]
        if not windows:
            return publication
        traffic_id = windows[-1]["id"]
        # A failed/unsupported diagnostic cannot overwrite receiver evidence or
        # turn a real traffic result into a synthetic success/failure.
        try:
            observed = await observe(snapshot, cancelled=self._cancel.is_set,
                                     deadline=min(time.monotonic()+12, deadline) if deadline is not None else time.monotonic()+12)
        except Exception:
            from backend.onprem.evidence.rtt import cohort_fingerprint
            observed = {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                        "cohortSha256": cohort_fingerprint(snapshot),
                        "status": "missing", "source": "native-ue-tun-icmp", "statistic": "mean",
                        "rows": [], "reason": "rtt.probe_unavailable", "startedAt": None, "finishedAt": None,
                        "relation": "adjacent-probe-window", "sameTrafficWindow": False}
        if observed is not None and not self._cancel.is_set():
            try:
                await asyncio.to_thread(self.ledger.save_rtt_observation, snapshot["roundId"], snapshot["generation"], traffic_id, observed)
            except Exception as exc:
                logging.getLogger(__name__).warning("rtt.sidecar_persistence_failed roundId=%s trafficActionId=%s errorType=%s",
                    snapshot["roundId"], traffic_id, type(exc).__name__)
        return publication

    async def _measure_traffic(self, stage):
        self._check_cancel()
        begin = getattr(self.ledger, 'begin_measurement', None)
        if callable(begin):
            scope = await asyncio.to_thread(self.ledger.ownership_control)
            snapshot = await asyncio.to_thread(begin, scope['roundId'], scope['generation'], stage)
        else:
            snapshot = await asyncio.to_thread(self._update, stage=stage, measurements=[])
        self._check_cancel()
        sampling_view = getattr(self.resources, 'population_sampling_view', None)
        if callable(sampling_view):
            snapshot = sampling_view(snapshot)
        batch = getattr(self.resources, 'batch', None)
        if (snapshot.get('schemaVersion') == V2 and getattr(batch, 'typed_enabled', False)
                and batch.enabled and len(session_rows(snapshot)) >= 10):
            snapshot = batch.sampling_view(snapshot)
            return await self._measure_batch_shards(stage, snapshot)
        if snapshot.get('schemaVersion') != V2 and getattr(self.resources, "batch", None) is not None and self.resources.batch.enabled:
            return await self._measure_batch(stage, snapshot)
        owned_rows = session_rows(snapshot)
        if snapshot.get('schemaVersion') == V2 and not owned_rows:
            # Last-member withdrawal keeps a live owned lease without making
            # an empty receiver window look like measured service success.
            return None
        jobs = await self.resources.traffic_jobs(snapshot)
        if len(jobs) != len(owned_rows) * 2:
            raise RoundRuntimeBlocked("Each UE requires independent UL and DL receiver windows")
        expected = {(session_identity(row), direction) for row in owned_rows for direction in ("ul", "dl")}
        actual = [(session_identity(job["binding"]), job["direction"]) for job in jobs]
        owners = {session_identity(row): row for row in owned_rows}
        if (len(expected) != len(jobs) or len(set(actual)) != len(actual) or set(actual) != expected
                or any(job["binding"] != {**owners[session_identity(job["binding"])],
                                         "ip": owners[session_identity(job["binding"])]["ueIp"],
                                         "generation": snapshot["generation"]} for job in jobs)):
            raise RoundRuntimeBlocked("Traffic jobs must cover each exact owned UE once per direction")
        journal = action("traffic_window", stage=stage, jobs=jobs)
        if snapshot.get('schemaVersion') == V2:
            workload = self.resources.session_workload(snapshot)
            from backend.onprem.traffic.session_workload import session_rates
            journal.update(workload=workload,offeredBySession=session_rates(workload))
        self._action(journal)  # Exact identities/ports are durable before exec.
        semaphore = asyncio.Semaphore(self.resources.concurrency)
        rows = {}
        async def sample(job):
            async with semaphore:
                try:
                    self._check_cancel()
                    report = await self.resources.sample(job)
                    self._check_cancel()
                except OwnedRoundCancelled:
                    raise
                except Exception as exc:
                    from backend.onprem.traffic.capability import native_command_diagnostic
                    raise NativeWindowFailure("native_sample", native_window_reason(exc),
                                              exc.diagnostic if type(exc) is ReceiverWindowBlocked else native_command_diagnostic(exc)) from None
                try:
                    validate_window(job, report)
                except Exception as exc:
                    raise NativeWindowFailure("receipt_validation", native_window_reason(exc)) from None
                binding = job["binding"]
                row = rows.setdefault(session_identity(binding), {k: binding[k] for k in ("supi", "scenario", "sessionId", "podUid", "ueIp", "tun", "upf")}
                                      | {k: binding[k] for k in IDENTITY_FIELDS + ('sst', 'sd', 'pfcpSeid', 'upfEpoch') if k in binding}
                                      | {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                                         "policyRevision": snapshot["policyRevision"], "source": "receiver",
                                         "observedAt": None, "receivedUplinkMbps": None, "receivedDownlinkMbps": None,
                                         "rttMs": None, "windows": []})
                row["receivedUplinkMbps" if job["direction"] == "ul" else "receivedDownlinkMbps"] = report["receivedMbps"]
                row["windows"].append(report)
                # Oldest constituent window controls freshness; never re-date UL
                # merely because the later reverse-DL command has completed.
                row["observedAt"] = timestamp(min(w["finishedAt"] for w in row["windows"]))
                try:
                    self._update(measurements=list(rows.values()),
                                 observedAt=timestamp(min(w["finishedAt"] for row in rows.values() for w in row["windows"])))
                except Exception as exc:
                    raise NativeWindowFailure("receipt_persistence", native_window_reason(exc)) from None
                return report
        results = await asyncio.gather(*(sample(job) for job in jobs), return_exceptions=True)
        # gather drains every bounded process before cleanup, even after cancel.
        failures=[r for r in results if isinstance(r,BaseException) and type(r) is not OwnedRoundCancelled]
        cancellations=[r for r in results if type(r) is OwnedRoundCancelled]
        if cancellations and not failures:
            owned=self._owned_cancellation()
            if owned is not None and all(r.cancellation==owned.cancellation for r in cancellations):
                self._finish_action(journal['id'],cancelled=True,accepted=False,
                    cancellation=deepcopy(owned.cancellation),finishedAt=timestamp())
                raise owned
            failures=[RoundRuntimeBlocked('Owned drain authority changed during cancellation')]
        if failures:
            diagnostics = []
            for job, result in zip(jobs, results):
                if not isinstance(result, BaseException) or type(result) is OwnedRoundCancelled:
                    continue
                entry = {"direction": job["direction"], "port": job["port"],
                         "phase": result.phase if type(result) is NativeWindowFailure else "worker",
                         "reasonCode": result.reason_code if type(result) is NativeWindowFailure else "traffic.unexpected_failure"}
                if type(result) is NativeWindowFailure and result.diagnostic is not None:
                    entry["receiverWindow"] = result.diagnostic
                diagnostics.append(entry)
            self._finish_action(journal["id"], failureDiagnostics=diagnostics, finishedAt=timestamp())
            raise RoundRuntimeBlocked("One or more actual receiver windows failed")
        # A later DL/queued UE can outlive an earlier UL's freshness budget.
        now = time.time()
        for job, report in zip(jobs, results):
            try:
                validate_window(job, report, now=now)
            except Exception as exc:
                self._finish_action(journal["id"], failureDiagnostics=[{
                    "direction": job["direction"], "port": job["port"],
                    "phase": "joint_freshness", "reasonCode": native_window_reason(exc)}],
                    finishedAt=timestamp())
                raise RoundRuntimeBlocked("One or more actual receiver windows failed") from None
        self._finish_action(journal["id"], accepted=True, installed=True, effectVerified=True,
                            finishedAt=timestamp(), reports=results,
                            **({'cleanupVerified': True} if snapshot.get('schemaVersion') == V2 else {}))

    async def _measure_batch(self, stage, snapshot):
        if snapshot.get('schemaVersion') == V2:
            raise RoundRuntimeBlocked('v2 SUPI batch sampler is not commissioned')
        if snapshot["allocation"]["total"] >= 50:
            return await self._measure_batch_shards(stage, snapshot)
        journal = None
        def reserve(token, bindings, jobs, rates, budget, *, workload=None):
            nonlocal journal
            if journal is not None:
                raise RoundRuntimeBlocked("Batch intent cannot be replaced")
            self._check_cancel()
            journal = action("traffic_window", stage=stage, mode="batch", token=token,
                             bindings=bindings, jobs=jobs, offeredBySupi=rates,
                             aggregateKbps=budget, cleanupVerified=False)
            if workload is not None:
                journal["workload"] = deepcopy(workload)
            self._action(journal)
            return True
        try:
            result = await self.resources.batch.sample(snapshot, reserve, cancelled=self._cancel.is_set)
        except Exception:
            self._check_cancel()
            raise
        self._check_cancel()  # Cooperative drain: never cancel an in-flight exec.
        if (journal is None or result.get("token") != journal["token"]
                or result.get("workload") != journal.get("workload")
                or result.get("jobs") != journal["jobs"] or result.get("cleanupVerified") is not True):
            raise RoundRuntimeBlocked("Batch receipt lacks exact durable ownership and cleanup")
        reports = result.get("reports", [])
        owners = {row["supi"]: row for row in snapshot["perUe"]}
        expected = {(supi, direction) for supi in owners for direction in ("ul", "dl")}
        jobs = journal["jobs"]
        if (len(jobs) != len(expected) or len(reports) != len(jobs)
                or {(j["supi"], j["direction"]) for j in jobs} != expected):
            raise RoundRuntimeBlocked("Batch must measure every owned UE in both directions")
        rows, now = {}, time.time()
        for job, report in zip(jobs, reports):
            owner = owners[job["supi"]]
            binding = {**owner, "ip": owner["ueIp"], "generation": snapshot["generation"]}
            if (job["generation"] != snapshot["generation"] or job["ue_ip"] != owner["ueIp"]
                    or job["session_id"] != owner["sessionId"] or job["ue_uid"] != owner["podUid"]
                    or job["tun"] != owner["tun"] or job["upf_name"] != owner["upf"]
                    or any(job.get(k) != owner.get(v) for k, v in (("sm_context_ref", "smContextRef"),
                        ("pfcp_seid", "pfcpSeid"), ("upf_epoch", "upfEpoch"), ("dnn", "dnn"), ("sst", "sst"), ("sd", "sd")))):
                raise RoundRuntimeBlocked("Batch receipt belongs to another native PDU")
            validate_window({"binding": binding, "direction": job["direction"],
                             "receiver_uid": job["receiver_uid"], "endpoint": job["endpoint"],
                             "port": job["port"], "duration": job["duration"],
                             "offered_mbps": job["offered_kbps"] / 1000}, report, now=now)
            row = rows.setdefault(job["supi"], {k: owner[k] for k in ("supi", "scenario", "sessionId", "podUid", "ueIp", "tun", "upf")}
                                  # Retain the exact native path checked against both
                                  # the durable job and its receiver report above.
                                  | {k: owner[k] for k in ("smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch", "demandGroupId", "sourceLocation", "servingGnb", "servingCellId")
                                     if k in owner}
                                  | {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                                     "policyRevision": snapshot["policyRevision"], "source": "receiver",
                                     "observedAt": None, "receivedUplinkMbps": None, "receivedDownlinkMbps": None,
                                     "rttMs": None, "windows": []})
            row["receivedUplinkMbps" if job["direction"] == "ul" else "receivedDownlinkMbps"] = report["receivedMbps"]
            row["windows"].append(report)
            row["observedAt"] = timestamp(min(w["finishedAt"] for w in row["windows"]))
        return await asyncio.to_thread(self.ledger.finish_batch_action, snapshot['roundId'], snapshot['generation'], journal['id'],
            token=journal['token'], jobs=jobs, stage=stage, reports=reports,
            finished_at=timestamp(), measurements=list(rows.values()),
            observed_at=timestamp(min(r['finishedAt'] for r in reports)))

    async def _measure_batch_shards(self, stage, snapshot):
        from backend.onprem.traffic.timing import MeasurementTiming
        timing, outcome = MeasurementTiming(snapshot, stage), 'error'
        origin=None
        try:
            if self.scoped_capacity is not None:
                origin=self.scoped_capacity.select_window(snapshot,stage,timing.metadata['windowId'])
                if origin is not None:
                    snapshot=self._action(action('scoped_capacity_window',accepted=True,windowOrigin=origin))
                    snapshot['_scopedCapacityWindowOrigin']=deepcopy(origin)
            with timing.bind():
                result = await self._measure_batch_shards_recorded(stage, snapshot, timing)
            outcome = 'ok'
            return result
        except Exception as exc:
            timing.metadata.update(errorType=type(exc).__name__, failureCode=native_window_reason(exc))
            if origin is not None:
                # Keep the first designated failure. A later successful cycle
                # never acquires this origin and cannot replace its artifacts.
                try:
                    failed=self._action(action('scoped_capacity_window_outcome',accepted=True,
                        state='failed',windowOrigin=origin,reasonCode=native_window_reason(exc)))
                    failed['_scopedCapacityWindowOrigin']=deepcopy(origin)
                    self.scoped_capacity.record(failed,'failure',dict(reasonCode=native_window_reason(exc)))
                except Exception as capture_error:
                    # An incomplete first-window artifact set fails promotion;
                    # do not replace the original native exception or retry it.
                    logging.getLogger(__name__).error('Scoped first-window failure capture blocked errorType=%s',
                        type(capture_error).__name__)
            raise
        finally:
            # Preserve the native result/exception. No additional persistence is
            # allowed to consume its freshness budget before parent validation.
            record = timing.snapshot(outcome)
            try:
                if not self.measurement_timings.submit(record):
                    logging.getLogger(__name__).warning('Measurement timing submission rejected window=%s', record['windowId'])
            except Exception as exc:
                logging.getLogger(__name__).warning('Measurement timing persistence failed window=%s errorType=%s',
                    record['windowId'], type(exc).__name__)

    async def _measure_batch_shards_recorded(self, stage, snapshot, timing):
        reserve = _ShardReservations(self, snapshot, stage)
        journals = reserve.journals
        try:
            with timing.span('provider.sample_shards'):
                result = await self.resources.batch.sample_shards(
                    snapshot, reserve, cancelled=self._cancel.is_set)
        except Exception:
            self._check_cancel()
            raise
        self._check_cancel()  # sample_shards drains every submitted worker before returning.
        with timing.span('parent.validate'):
            action_ids = result.get("shardActionIds")
            receipts = result.get("shardReceipts")
            jobs, reports, aggregate = result.get("jobs"), result.get("reports"), result.get("aggregate")
            if (not journals or not isinstance(action_ids, list) or action_ids != [row["id"] for row in journals]
                    or not isinstance(receipts, list) or len(receipts) != len(journals)
                    or result.get("cleanupVerified") is not True or not isinstance(jobs, list)
                    or not isinstance(reports, list) or len(jobs) != len(reports)
                    or result.get("workload") != journals[0].get("workload")
                    or not isinstance(aggregate, dict)):
                raise RoundRuntimeBlocked("Sharded batch receipt lacks exact durable ownership and cleanup")
            for index, (journal, receipt) in enumerate(zip(journals, receipts)):
                if (receipt != {"actionId": journal["id"], "token": journal["token"],
                                "shardIndex": index, "cleanupVerified": True}
                        or journal["jobs"] != jobs[sum(len(row["jobs"]) for row in journals[:index]):
                                                       sum(len(row["jobs"]) for row in journals[:index + 1])]):
                    raise RoundRuntimeBlocked("Sharded batch action receipt changed")
            owners = {row["supi"]: row for row in snapshot["perUe"]}
            expected = {(supi, direction) for supi in owners for direction in ("ul", "dl")}
            if (len(jobs) != len(expected) or {(job.get("supi"), job.get("direction")) for job in jobs} != expected
                    or aggregate.get("flowCount") != len(expected)
                    or aggregate.get("source") != "native-iperf3-receiver"):
                raise RoundRuntimeBlocked("Sharded batch must cover every owned UE in both directions")
            rows, now = {}, time.time()
            timing.metrics['parentValidationNowUnix'] = now
            finished = [r.get('finishedAt') for r in reports if isinstance(r, dict)]
            ages = [now-stamp for stamp in finished if type(stamp) in (int, float) and math.isfinite(stamp)]
            if ages:
                timing.metrics['parentMaximumEvidenceAgeSeconds'] = max(ages)
            for job, report in zip(jobs, reports):
                owner = owners.get(job.get("supi"))
                if owner is None:
                    raise RoundRuntimeBlocked("Sharded batch contains an unowned UE")
                wire_owner = owner
                if snapshot.get('schemaVersion') == V2:
                    from backend.onprem.traffic.v2_batch import wire_binding
                    wire_owner = wire_binding(owner,preserve_upf=bool(job.get('route_binding_id')))
                binding = {**owner, "ip": owner["ueIp"], "generation": snapshot["generation"]}
                if (job.get("generation") != snapshot["generation"] or job.get("ue_ip") != owner["ueIp"]
                        or job.get("session_id") != wire_owner["sessionId"] or job.get("ue_uid") != owner["podUid"]
                        or job.get("tun") != owner["tun"] or job.get("upf_name") != wire_owner["upf"]
                        or any(job.get(k) != wire_owner.get(v) for k, v in (("sm_context_ref", "smContextRef"),
                            ("pfcp_seid", "pfcpSeid"), ("upf_epoch", "upfEpoch"), ("dnn", "dnn"),
                            ("sst", "sst"), ("sd", "sd")))):
                    raise RoundRuntimeBlocked("Sharded batch receipt belongs to another native PDU")
                validate_window({"binding": binding, "direction": job["direction"],
                                 "receiver_uid": job["receiver_uid"], "endpoint": job["endpoint"],
                                 "port": job["port"], "duration": job["duration"],
                                 "offered_mbps": job["offered_kbps"] / 1000}, report,
                                now=now, maximum_age=35)
                row = rows.setdefault(job["supi"], {k: owner[k] for k in
                                      ("supi", "scenario", "sessionId", "podUid", "ueIp", "tun", "upf")}
                                      | ({k:owner[k] for k in IDENTITY_FIELDS if k in owner}
                                         if snapshot.get('schemaVersion') == V2 else {})
                                      | {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                                         "policyRevision": snapshot["policyRevision"], "source": "receiver",
                                         "upfEpoch": owner.get("upfEpoch"), "observedAt": None,
                                         "receivedUplinkMbps": None, "receivedDownlinkMbps": None,
                                         "rttMs": None, "lossPercent": None, "windows": []})
                row["receivedUplinkMbps" if job["direction"] == "ul" else "receivedDownlinkMbps"] = report["receivedMbps"]
                row["windows"].append(report)
                row["observedAt"] = timestamp(min(window["finishedAt"] for window in row["windows"]))
        with timing.span('parent.publish'):
            if snapshot.get('schemaVersion') == V2:
                from backend.onprem.evidence.native_binding_publication import NativeBindingPublication
                native_publication=result.get('_nativeBindingPublication')
                if type(native_publication) is not NativeBindingPublication:
                    raise RoundRuntimeBlocked('V2 shard result requires sealed native publication')
                persisted=await asyncio.to_thread(self.ledger.publish_native_evidence,native_publication)
                if persisted.get('nativeSessionBindings')!=result.get('nativeSessionBindings'):
                    raise RoundRuntimeBlocked('V2 shard native wrapper differs from sealed publication')
            publication = await asyncio.to_thread(self.ledger.finish_batch_shards,
                snapshot["roundId"], snapshot["generation"], action_ids, stage=stage, jobs=jobs,
                reports=reports, shard_receipts=receipts, aggregate=aggregate, finished_at=timestamp(),
                measurements=list(rows.values()),
                observed_at=timestamp(aggregate["effectiveFinishedAt"]))
            if snapshot.get('schemaVersion') == V2:
                native_bindings = result.get('nativeSessionBindings')
                if not isinstance(native_bindings, dict) or native_bindings.get('generation') != snapshot['generation']:
                    raise RoundRuntimeBlocked('V2 shard result requires final complete native binding projection')
                if self.scoped_capacity is not None and snapshot.get('_scopedCapacityWindowOrigin') is not None:
                    self._capture_scoped_result(snapshot,result)
            return publication

    def _capture_scoped_result(self,snapshot,result):
        """Persist this designated result's references and immutable outcome."""
        origin=snapshot.get('_scopedCapacityWindowOrigin')
        if self.scoped_capacity is None or origin is None:return
        for key in ('_scopedOpeningCaptureDescriptor','_scopedNativeCaptureDescriptor'):
            descriptor=result.get(key)
            if descriptor is None:
                raise RoundRuntimeBlocked('Designated result requires both native captures')
            self._action(action('scoped_capacity_evidence',**descriptor,accepted=True))
        self._action(action('scoped_capacity_window_outcome',accepted=True,state='completed',windowOrigin=origin))
        full=self.ledger.live_snapshot(snapshot['roundId'])
        payload=deepcopy(full)
        full['_scopedCapacityWindowOrigin']=deepcopy(origin)
        self.scoped_capacity.record(full,'result',dict(snapshot=payload))

    async def _recover(self):
        current = read_cleanup(self.ledger)
        entry=self._intent_lease(current['roundId'])
        if entry is not None:
            from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
            lease=ServiceIntentTrafficLease.from_dict(entry['lease'])
            if entry['state']=='active' and not self._cancel.is_set():
                lease=await self._renew_intent_traffic(lease)
            windows=[a for a in current.get('actions',()) if a.get('kind') in {'traffic_window','traffic_aggregate'}]
            def completed_window(item):
                if item.get('accepted') is not True:
                    return False
                if item.get('mode')=='batch' or item.get('kind')=='traffic_aggregate':
                    return item.get('cleanupVerified') is True
                # The ordinary sampler drains every bounded worker before its
                # final accepted receipt; it has no batch cleanup flag.
                return (item.get('installed') is True and item.get('effectVerified') is True
                    and bool(item.get('finishedAt')) and isinstance(item.get('reports'),list)
                    and len(item['reports'])==len(item.get('jobs',())) and not item.get('failureDiagnostics'))
            resumable=(entry['state']=='active' and lease.active(self._wall_clock())
                and current.get('generation') is not None and current['stage'] in {'measuring_baseline','measuring_result'}
                and windows and all(completed_window(a) for a in windows))
            if resumable:
                self._cancel.clear()
                try:
                    await self._hold_configuration(current['roundId'])
                except Exception as exc:
                    await asyncio.to_thread(self._record_execution_failure,exc)
            current=read_cleanup(self.ledger)
            if current['executionOutcome'] is None:
                self._update(executionOutcome='passed' if self._intent_lease(current['roundId'])['state']=='released' else 'failed')
            await self._cleanup()
            return
        if current["executionOutcome"] is None:
            self._update(executionOutcome="cancelled" if self._cancel.is_set() else "failed",
                         errors=[*current["errors"], "Backend restart interrupted execution; cleanup reconciliation required"])
        await self._cleanup()
        await self._release_constrained_fixture(read_cleanup(self.ledger))

    async def _cleanup_group_policy(self, snapshot):
        if (snapshot.get("strategy") == "ai"
                and 1 <= snapshot.get("allocation", {}).get("total", 0) <= 100):
            cleanup = next((a for a in snapshot["actions"]
                            if a.get("kind") == "adaptive_qer_cleanup"), None)
            if cleanup is None:
                cleanup = action("adaptive_qer_cleanup", cleanupVerified=False,
                                 reconciliationRequired=True)
                await asyncio.to_thread(self._action, cleanup)
            adapter = getattr(self.adaptive_runtime, "adapter", None)
            if adapter is None:
                raise RoundRuntimeBlocked("Adaptive QER journal is unavailable")
            try:
                resume = getattr(self.adaptive_runtime, 'cleanup_group_policy', None)
                result = await asyncio.to_thread(resume, snapshot) if callable(resume) else await asyncio.to_thread(adapter.cleanup, snapshot["roundId"])
                records = result.get("actions") if isinstance(result, dict) else None
                if (result.get("roundId") != snapshot["roundId"]
                        or result.get("cleanupVerified") is not True
                        or not isinstance(records, list)
                        or any(r.get("state") != "restored" for r in records)):
                    raise RoundRuntimeBlocked("Adaptive QER restoration is unverified")
            except Exception:
                await asyncio.to_thread(self._finish_action, cleanup["id"], cleanupVerified=False,
                                    reconciliationRequired=True)
                raise RoundRuntimeBlocked("Adaptive QER restoration requires reconciliation") from None
            await asyncio.to_thread(self._finish_action, cleanup["id"], cleanupVerified=True,
                                reconciliationRequired=False,
                                policyActionIds=sorted(r["id"] for r in records))
            return
        from archive.legacy.scenarios.policy import cleanup_group_policy
        return await cleanup_group_policy(self, snapshot)

    async def _owned_stop(self,snapshot,*,recovery=False):
        reader=getattr(self.ledger,'intent_traffic_drain_authority',None)
        authority=await asyncio.to_thread(reader,snapshot['roundId']) if callable(reader) else None
        request=getattr(self.resources,'request_owned_drain',None)
        if authority is None or not callable(request):
            spatial_stop = getattr(self.resources, 'request_owned_round_stop', None)
            if snapshot.get('spatialDemand') and not snapshot.get('fleetLease') and callable(spatial_stop):
                return await spatial_stop(snapshot)
            if recovery:
                raise RoundRuntimeBlocked('Stop request outcome unknown; reconciliation required')
            return await self.resources.request(dict.fromkeys(SLICES,0))
        lease,permit=authority['lease'],authority['permit']
        def guard():
            latest=reader(snapshot['roundId'])
            current=read_cleanup(self.ledger)
            if (latest is None or latest['lease']!=lease or latest['permit']!=permit
                    or any(current.get(k)!=snapshot.get(k) for k in ('roundId','requestId','generation'))
                    or current.get('stage')!='cleaning'):
                raise RoundRuntimeBlocked('Current formal drain ownership changed')
        return await request(snapshot,lease,permit,admission_guard=guard,recovery=recovery)

    async def _cleanup(self):
        current=await asyncio.to_thread(read_cleanup,self.ledger)
        entry=self._intent_lease(current['roundId'])
        if entry is not None and entry['state']=='drained':
            return
        if self._intent_lease(current['roundId']) is not None:
            try:
                await self._ensure_intent_restored(current['roundId'])
            except Exception:
                await asyncio.to_thread(self._update,cleanupStatus='blocked',
                    errors=[*current['errors'],'Intent traffic cleanup blocked until owner-safe policy restoration'])
                return
        await asyncio.to_thread(self._update, stage="cleaning", cleanupStatus="pending")
        snapshot = await asyncio.to_thread(read_cleanup, self.ledger)
        try:
            await self._cleanup_group_policy(snapshot)
            from backend.onprem.validation.constrained import SCHEMA as CONSTRAINED_SCHEMA
            current = await asyncio.to_thread(read_cleanup, self.ledger)
            controlled = [item for item in current.get("actions", [])
                          if type(item) is dict and item.get("kind") == "scenario_validation_track"
                          and type(item.get("fixtureClaim")) is dict
                          and item["fixtureClaim"].get("schema") == CONSTRAINED_SCHEMA]
            if controlled:
                if len(controlled) != 1 or self.adaptive_runtime is None:
                    raise RoundRuntimeBlocked("Controlled fixture cleanup has no unique owner")
                await asyncio.to_thread(
                    self.adaptive_runtime.restore_constrained, self,
                    controlled[0]["fixtureClaim"])
            policy_actions = [a for a in snapshot["actions"] if a.get("kind") == "policy_plan" and a.get("applied")]
            if policy_actions:
                cleanup = next((a for a in snapshot["actions"] if a.get("kind") == "policy_cleanup"), None)
                if cleanup is None:
                    cleanup = action("policy_cleanup", policyActionIds=[a["policyActionId"] for a in policy_actions])
                    await asyncio.to_thread(self._action, cleanup)
                    results = await asyncio.to_thread(self.policy_bridge.delete_owned, snapshot["roundId"])
                    if any(row.get("cleanupState") != "released" for row in results):
                        await asyncio.to_thread(self._finish_action, cleanup["id"], accepted=False, outcomeUnknown=True)
                        raise RoundRuntimeBlocked("Owned policy deletion requires reconciliation")
                    await asyncio.to_thread(self._finish_action, cleanup["id"], accepted=True, cleanupVerified=True,
                                        policies=deepcopy(results))
                elif cleanup.get("accepted") is not True:
                    raise RoundRuntimeBlocked("Policy cleanup outcome unknown; never retry blindly")
            # Restorations retain their own exact-once/write timeout protocols.
            # Their already-permitted work must not consume the later native
            # release observation budget before its first probe can run.
            deadline = self._monotonic() + self.cleanup_timeout
            while not await self.resources.prepare_cleanup(snapshot):
                if self._monotonic() >= deadline:
                    raise RoundRuntimeBlocked("Owned worker or policy release is not yet proven")
                await asyncio.sleep(self.poll_seconds)
            generation = self.resources.current_generation()
            allocations = [a for a in snapshot["actions"] if a["kind"] == "ue_allocation"]
            stop = next((a for a in snapshot["actions"] if a["kind"] == "ue_stop"), None)
            if allocations and snapshot["generation"] is None and not snapshot.get("fleetLease"):
                zero = dict.fromkeys(SLICES, 0)
                allocation = allocations[0] if len(allocations) == 1 else None
                try:
                    generation_created = generation.get("created") if isinstance(generation, dict) else None
                    requested_at = seconds(allocation.get("requestedAt")) if allocation else None
                except (TypeError, ValueError, OverflowError):
                    generation_created = requested_at = None
                unwritten = (allocation is not None
                    and allocation.get("requested") is True
                    and allocation.get("accepted") is False
                    and allocation.get("installed") is False
                    and allocation.get("effectVerified") is False
                    and allocation.get("generation") is None
                    and allocation.get("reconciliation") in (None, "native_write_not_observed")
                    and isinstance(generation, dict)
                    and type(generation.get("generation")) is int
                    and generation["generation"] > 0
                    and generation.get("desired") == zero
                    and type(generation_created) in (int, float)
                    and math.isfinite(generation_created)
                    and type(requested_at) in (int, float)
                    and math.isfinite(requested_at)
                    and generation_created < requested_at)
                if not unwritten:
                    raise RoundRuntimeBlocked("Allocation write outcome unknown; do not infer ownership after restart")
                if allocation.get("reconciliation") is None:
                    await asyncio.to_thread(self._finish_action, allocation["id"], reconciliation="native_write_not_observed",
                                        reconciledAt=timestamp())
                proof = await self.resources.release_proof(await asyncio.to_thread(read_cleanup, self.ledger))
                required = ("trafficStopped", "policiesAbsent", "rulesRestored", "uesReleased", "podsAbsent")
                latest = self.resources.current_generation()
                if (not isinstance(latest, dict)
                        or any(latest.get(key) != generation.get(key)
                               for key in ("generation", "desired", "created"))
                        or any(proof.get(key) is not True for key in required)
                        or not fresh(proof.get("observedAt"))):
                    raise RoundRuntimeBlocked("Unwritten allocation requires fresh full release proof")
                current = await asyncio.to_thread(read_cleanup, self.ledger)
                await self._complete_or_drain(current,proof)
                return
            persistent_release = getattr(self.resources, "round_release_proof", None)
            if (snapshot.get('schemaVersion') == V2 or self._intent_lease(snapshot['roundId']) is not None
                    or (snapshot.get('spatialDemand') and not snapshot.get('fleetLease'))):
                # Spatial rounds require a fresh location-bound allocation.
                # They own that fleet: cancellation/completion must stop it,
                # rather than retaining it as a borrowed resident generation.
                persistent_release = None
            if snapshot["generation"] is not None and not callable(persistent_release):
                expected = stop.get("generation") if stop and stop.get("accepted") else snapshot["generation"]
                recoverable_stop=(stop is not None and stop.get('accepted') is not True
                    and callable(getattr(self.resources,'request_owned_drain',None))
                    and callable(getattr(self.ledger,'intent_traffic_drain_authority',None)))
                if (not generation or generation["generation"] != expected) and not recoverable_stop:
                    raise RoundRuntimeBlocked("Native generation changed without a durable round receipt")
                if not stop:
                    formal = self._intent_lease(snapshot['roundId'])
                    if formal is not None:
                        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
                        quiesce = getattr(getattr(self.resources, 'controller', None), 'quiesce_population', None)
                        if snapshot.get('populationPending') and not callable(quiesce):
                            raise RoundRuntimeBlocked('Pending population must quiesce in the owned UE writer before full drain')
                        if callable(quiesce):
                            await quiesce(ServiceIntentTrafficLease.from_dict(formal['lease']))
                    stop = action("ue_stop", generation=None)
                    await asyncio.to_thread(self._action, stop)
                    result = await self._owned_stop(snapshot)
                    await asyncio.to_thread(self._finish_action, stop["id"], accepted=True, generation=result["generation"])
                elif not stop.get("accepted"):
                    # An interrupted normal stop may not have written anything.
                    # Only the private writer can prove native zero + original
                    # ownership and CAS/read back its exact durable stop intent.
                    result=await self._owned_stop(snapshot,recovery=True)
                    await asyncio.to_thread(self._finish_action,stop['id'],accepted=True,generation=result['generation'])
            while self._monotonic() < deadline:
                try:
                    current = await asyncio.to_thread(read_cleanup, self.ledger)
                    proof = (await persistent_release(current) if callable(persistent_release)
                             else await self.resources.release_proof(current))
                    if not fresh(proof.get("observedAt")):
                        raise RoundRuntimeBlocked("Release proof expired")
                    current = await asyncio.to_thread(read_cleanup, self.ledger)
                    await self._complete_or_drain(current,proof)
                    return
                except Exception as exc:
                    code = exc.code if isinstance(exc, ReleaseProofBlocked) else (
                        "release.incomplete_proof" if isinstance(exc, RoundConflict) else "release.observation_failed")
                    age = exc.proof_age if isinstance(exc, ReleaseProofBlocked) else None
                    # Preserve execution failures; retain only latest fixed
                    # release diagnostic within the existing ledger schema.
                    current = await asyncio.to_thread(read_cleanup, self.ledger)
                    errors = [item for item in current['errors']
                              if not item.startswith('Cleanup probe: ')]
                    await asyncio.to_thread(self._update, errors=[*errors, 'Cleanup probe: ' + code
                        + ('; proofAgeSeconds=' + str(age) if type(age) in (int, float) and math.isfinite(age) else '')])
                    await asyncio.sleep(self.poll_seconds)
            raise RoundRuntimeBlocked("Known owned resource release deadline exceeded")
        except Exception as exc:
            current = await asyncio.to_thread(read_cleanup, self.ledger)
            await asyncio.to_thread(self._update, cleanupStatus="blocked", errors=[*current["errors"], "Cleanup blocked: " + safe_reason(exc)])

    async def _apply_policy(self):
        from archive.legacy.scenarios.policy import _apply_policy
        return await _apply_policy(self)

    def _oracle_mix(self, event_counts):
        from archive.legacy.scenarios.policy import _oracle_mix
        return _oracle_mix(self, event_counts)

    def _group_ai_snapshot(self, snapshot):
        from archive.legacy.scenarios.policy import _group_ai_snapshot
        return _group_ai_snapshot(self, snapshot)

    async def _apply_group_policy(self, snapshot):
        from archive.legacy.scenarios.policy import _apply_group_policy
        return await _apply_group_policy(self, snapshot)

    def _verify_group_effect(self):
        from archive.legacy.scenarios.policy import _verify_group_effect
        return _verify_group_effect(self)
