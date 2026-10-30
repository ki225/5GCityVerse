"""Scenario and UE use cases sharing one admission lock and durable ledger."""
from backend.onprem.storage.current_state import read_cleanup, replay_current
import asyncio
import re
import time

from backend.onprem.application.errors import ApplicationError
from backend.onprem.api.timing import admission_span, timed_admission_lock
from backend.onprem.scenarios.contracts import MAX_ROUND_UES, RequestIdConflict, RoundConflict, RoundUnavailable, validate_scenario_request
from backend.onprem.runtime.ue import validate_counts
from backend.onprem.runtime.errors import EvidenceUnavailable
from backend.onprem.runtime.errors import RoundRuntimeBlocked
from backend.onprem.scenarios.presentation import with_traffic_counts
from backend.onprem.traffic.admission import admit_traffic, TrafficAdmissionBlocked


class ScenarioService:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def _legacy_snapshot(snapshot):
        # These new internal contracts are exposed only by opt-in observation
        # endpoints; strict legacy full/start/cancel clients keep their schema.
        return {k: v for k, v in snapshot.items() if k not in {"qosObjectives", "rttObservations"}}

    def _observation_scope(self, round_id, *, hydrate=True, display=False, event_scope=False, projected=False, current_only=True):
        """Read an exact durable round without refreshing its network evidence."""
        try:
            narrow = getattr(self.context.round_ledger, 'observation_event_scope', None)
            display_reader = getattr(self.context.round_ledger, 'projected_observation_read', None)
            if event_scope and callable(narrow):
                snapshot = narrow(round_id)
            elif not hydrate:
                snapshot = self.context.round_ledger.observation_metadata(round_id)
            elif current_only:
                snapshot = (self.context.round_ledger.live_observation_read(round_id) if projected
                            else self.context.round_ledger.live_snapshot(round_id))
            else:
                raise ValueError('Runtime observation supports current state only')
        except KeyError as exc:
            raise ApplicationError(404, "ROUND_NOT_FOUND", "Scenario round was not found") from exc
        runtime = getattr(self.context.round_coordinator, "adaptive_runtime", None)
        journal = getattr(runtime, "journal_path", None)
        path = journal.with_name(journal.stem + ".graph-evidence.db") if journal is not None else None
        return snapshot, runtime, path

    async def observability(self, round_id):
        def read():
            reading, runtime, path = self._observation_scope(round_id, display=True, projected=True)
            return self._observability_view(reading, runtime, path)
        return await asyncio.to_thread(read)

    def _observability_view(self, reading, runtime, path):
        from backend.onprem.scenarios.observability import observability, _ObservationProjection
        snapshot = reading.snapshot if isinstance(reading, _ObservationProjection) else reading
        status = getattr(runtime, "status", None)
        result = observability(reading, graph_path=path, runtime_status=status() if callable(status) else None,
            journal_path=getattr(runtime, "journal_path", None),
            capabilities=getattr(self.context.round_coordinator, "capabilities", None))
        # Round history is anchored to the original receiver observation.
        # Current runtime health has its own /api/onprem/topology endpoint.
        from datetime import datetime
        import math
        from backend.onprem.evidence.topology import unknown_nodes
        monitor = getattr(self.context, 'topology_monitor', None)
        stamp = snapshot.get('observedAt')
        health = None
        if monitor is not None and stamp:
            try:
                moment = datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp() if isinstance(stamp, str) else float(stamp)
                if not math.isfinite(moment):
                    raise ValueError('Invalid observation time')
                health = monitor.snapshot(at=moment,
                    round_id=snapshot['roundId'], generation=snapshot['generation'])
            except (ValueError, TypeError):
                pass
        if health is not None and health['fresh']:
            result['nodeStates'] = health['nodeStates']
        else:
            result.setdefault('nodeStates', unknown_nodes('no_saved_evidence'))
        if snapshot.get("spatialDemand") is not None:
            from copy import deepcopy
            result["spatialDemand"] = deepcopy(snapshot["spatialDemand"])
        return result

    async def observation_view(self, round_id, *, after=0, limit=100, scenario=None, view="current"):
        """One fresh durable read for both display projections, no evidence renewal.

        Scope/sequence describe that persisted source. Runtime, graph and saved
        topology retain their independent original observation clocks.
        """
        from backend.onprem.scenarios.contracts import SCENARIO_SLICES
        from backend.onprem.scenarios.observability import page_arguments
        # Only query validation becomes a client error. Evidence/decoder and
        # dynamic overlay failures retain the original sanitized server error.
        try:
            if view != "current":
                raise ValueError("Runtime observation supports current state only")
            after, limit = page_arguments(after, limit)
            if scenario is not None and scenario not in SCENARIO_SLICES:
                raise ValueError("Unknown scenario")
        except ValueError as exc:
            raise ApplicationError(400, "INVALID_OBSERVABILITY_QUERY", str(exc)) from exc
        def read():
            from backend.onprem.scenarios.observability import measurement_page, _ObservationProjection
            reading, runtime, path = self._observation_scope(round_id, display=True, projected=True, current_only=view == "current")
            samples = measurement_page(reading, after=after, limit=limit, scenario=scenario)
            snapshot = reading.snapshot if isinstance(reading, _ObservationProjection) else reading
            return {'schemaVersion': 'cityverse.round-observation-view/v1',
                'roundId': snapshot.get('roundId'), 'generation': snapshot.get('generation'),
                'sequence': snapshot.get('sequence'),
                'observability': self._observability_view(reading, runtime, path),
                'measurements': samples}
        return await asyncio.to_thread(read)

    @staticmethod
    def _action_query(cycle=None, action_id=None, call_id=None):
        from backend.onprem.scenarios.observability import page_arguments
        if cycle is not None:
            cycle = page_arguments(cycle, 1)[0]
            if cycle > 10000:
                raise ValueError("Action cycle must be between 0 and 10000")
        if any(value is not None and (not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is None)
               for value in (action_id, call_id)):
            raise ValueError("Invalid action or call id")
        return cycle, action_id, call_id

    async def observation_events(self, round_id, *, after=0, limit=100, cycle=None, action_id=None, call_id=None, role=None):
        def read():
            from backend.onprem.scenarios.observability import event_page, page_arguments
            page_arguments(after, limit)
            if role is not None and role not in {"planner", "executor", "validator"}:
                raise ValueError("Invalid agent role")
            selected_cycle, selected_action, selected_call = self._action_query(cycle, action_id, call_id)
            snapshot, runtime, path = self._observation_scope(round_id, hydrate=False,
                event_scope=selected_action is None)
            return event_page(path, snapshot, after=after, limit=limit, cycle=selected_cycle,
                action_id=selected_action, call_id=selected_call, role=role, journal_path=getattr(runtime, "journal_path", None))
        try:
            return await asyncio.to_thread(read)
        except ValueError as exc:
            raise ApplicationError(400, "INVALID_OBSERVABILITY_QUERY", str(exc)) from exc

    async def action_evidence(self, round_id, *, cycle=None, action_id=None, call_id=None):
        def read():
            from backend.onprem.agents.action_observability import action_evidence
            selected_cycle, selected_action, selected_call = self._action_query(cycle, action_id, call_id)
            if selected_cycle is None and selected_action is None and selected_call is None:
                raise ValueError("Select an action cycle, actionId or callId")
            snapshot, runtime, path = self._observation_scope(round_id, hydrate=False)
            return action_evidence(snapshot, graph_path=path, journal_path=getattr(runtime, "journal_path", None),
                cycle=selected_cycle, action_id=selected_action, call_id=selected_call)
        try:
            return await asyncio.to_thread(read)
        except ValueError as exc:
            raise ApplicationError(400, "INVALID_OBSERVABILITY_QUERY", str(exc)) from exc

    async def action_logs(self, round_id, *, cycle=None, action_id=None, call_id=None, role=None, after=0, limit=100):
        def read():
            from backend.onprem.agents.topology_observability import action_logs
            from backend.onprem.scenarios.observability import page_arguments
            cursor, count = page_arguments(after, limit)
            if count > 100 or role is not None and role not in {"planner", "executor", "validator"}:
                raise ValueError("Invalid log limit or agent role")
            selected_cycle, selected_action, selected_call = self._action_query(cycle, action_id, call_id)
            if selected_cycle is None and selected_action is None and selected_call is None:
                raise ValueError("Select an action cycle, actionId or callId")
            snapshot, runtime, path = self._observation_scope(round_id, hydrate=False)
            return action_logs(snapshot, graph_path=path, journal_path=getattr(runtime, "journal_path", None),
                cycle=selected_cycle, action_id=selected_action, call_id=selected_call, role=role, after=cursor, limit=count)
        try:
            return await asyncio.to_thread(read)
        except ValueError as exc:
            raise ApplicationError(400, "INVALID_OBSERVABILITY_QUERY", str(exc)) from exc

    async def observation_measurements(self, round_id, *, after=0, limit=100, scenario=None):
        def read():
            from backend.onprem.scenarios.observability import measurement_page, page_arguments
            page_arguments(after, limit)
            snapshot, _runtime, _path = self._observation_scope(round_id, display=True, projected=True)
            return measurement_page(snapshot, after=after, limit=limit, scenario=scenario)
        try:
            return await asyncio.to_thread(read)
        except ValueError as exc:
            raise ApplicationError(400, "INVALID_OBSERVABILITY_QUERY", str(exc)) from exc

    async def measurement_timings(self, round_id, *, after=0, limit=10, legacy_after=0):
        def read():
            from backend.onprem.scenarios.observability import page_arguments
            cursor, count = page_arguments(after, limit)
            legacy_cursor, _ = page_arguments(legacy_after, limit)
            if count > 20:
                raise ValueError('Timing page limit must not exceed 20')
            snapshot, _, _ = self._observation_scope(round_id, hydrate=False)
            return self.context.measurement_timings.page(
                round_id, snapshot['generation'], after=cursor, limit=count, legacy_after=legacy_cursor)
        try:
            return await asyncio.to_thread(read)
        except ValueError as exc:
            raise ApplicationError(400, 'INVALID_TIMING_QUERY', str(exc)) from exc

    async def resources(self, round_id):
        from backend.onprem.agents.observations import PlannerObservations
        current = await asyncio.to_thread(self.context.round_ledger.live_snapshot)
        if current.get('roundId') != round_id:
            raise ApplicationError(409, 'RESOURCE_SCOPE_NOT_CURRENT', 'Live resources require the current scenario')
        runtime = getattr(self.context.round_coordinator, 'adaptive_runtime', None)
        driver = getattr(getattr(runtime, 'qer_provider', None), 'driver', None)
        if driver is None:
            driver = getattr(self.context.ue_controller, 'driver', None)
        observer = PlannerObservations(lambda: current, driver=driver)
        from backend.onprem.runtime.city_catalog import placement_input
        spatial = current.get('spatialDemand') or {}
        body = {**current.get('allocation', {}), 'trafficMode': current.get('trafficMode')}
        if current.get('serviceRequirements') is not None:
            body['serviceRequirements'] = current['serviceRequirements']
        return {'roundId': round_id, 'generation': current['generation'],
                'spatialDemand': current.get('spatialDemand'),
                'placementDemand': placement_input(body, spatial.get('resolution'), spatial.get('actualBindings')),
                'resources': await asyncio.to_thread(observer.resource_snapshot)}

    async def current(self, view="full"):
        ctx = self.context
        if view not in {"full", "summary"}:
            raise ApplicationError(400, "INVALID_SCENARIO_VIEW", "view must be full or summary")
        from os import environ
        from backend.onprem.capacity.admission import calibration_from_environment
        from backend.onprem.scenarios.presentation import project_round_summary
        from backend.onprem.storage.rounds import RoundDisplayRead
        display_read = None
        if ctx.round_coordinator is None:
            def unavailable():
                summary_reader = getattr(ctx.round_ledger, 'live_summary_read', None) or getattr(ctx.round_ledger, 'summary_read', None)
                reading = summary_reader() if view == 'summary' and callable(summary_reader) else None
                if reading is not None and not isinstance(reading, RoundDisplayRead):
                    raise RoundConflict('Invalid round display read')
                value = (reading.checked_snapshot() if reading is not None else
                         ctx.round_ledger.summary_snapshot() if view == "summary" else ctx.round_ledger.live_snapshot())
                value["capabilities"] = {
                    **calibration_from_environment(environ).capabilities(), "start": False,
                    "staticPolicy": False, "aiPolicy": False, "reason": ctx.round_unavailable_reason,
                }
                return value, reading
            snapshot, display_read = await asyncio.to_thread(unavailable)
        elif view == "summary":
            summary_reader = getattr(ctx.round_coordinator, 'summary_read', None)
            if callable(summary_reader):
                display_read = await summary_reader()
                if not isinstance(display_read, RoundDisplayRead):
                    raise RoundConflict('Invalid round display read')
                snapshot = display_read.checked_snapshot()
            else:
                snapshot = await ctx.round_coordinator.summary_snapshot()
        else:
            snapshot = await ctx.round_coordinator.snapshot()
        def present():
            # Ledger/control reads and large display projections must not run
            # on the event loop used by cancellation and lightweight requests.
            if display_read is not None:
                display_read.checked_snapshot()
            maintenance = (ctx.application_maintenance or
                           (display_read.maintenance_pending if display_read is not None
                            else ctx.round_ledger.has_pending_maintenance()))
            if (ctx.application_shutting_down or ctx.application_maintenance
                    or maintenance
                    or (ctx.round_coordinator is not None and not ctx.runtime_policy_clear())):
                snapshot["capabilities"] = {
                    **snapshot["capabilities"], "start": False, "staticPolicy": False, "aiPolicy": False,
                    "reason": "Admission stopped or policy ownership is unresolved",
                }
            from backend.onprem.scenarios.presentation import VIEW_SCHEMA, project_agent_runtime
            projected = view == "summary" and snapshot.get("viewSchema") == VIEW_SCHEMA
            # Coordinator summary already derived counts, SLA and performance.
            # Full snapshots are independent per-read objects; retain their
            # evidence tree rather than recursively copying it a second time.
            result = (dict(snapshot) if projected and isinstance(snapshot.get('trafficCounts'), dict)
                      else with_traffic_counts(snapshot, copy_snapshot=False))
            result['lifecycle'] = ctx.scenario_schedule.lifecycle(result)
            if ctx.application_shutting_down or maintenance or ctx.round_coordinator is None:
                result['lifecycle'].update(canSubmitEvaluation=False, submissionMode='blocked')
            if view == "summary":
                return result if projected else project_round_summary(result)
            if "agentRuntime" in result:
                progress = project_agent_runtime(result["agentRuntime"])
                if progress is None:
                    result.pop("agentRuntime")
                else:
                    result["agentRuntime"] = progress
            return self._legacy_snapshot(result)
        return await asyncio.to_thread(present)

    async def prepare_spatial(self, body):
        """Read native RAN eligibility before accepting location-bound demand."""
        if not body.get("demandGroups"):
            return None
        coordinator = getattr(self.context, "round_coordinator", None)
        prepare = getattr(getattr(coordinator, "resources", None), "prepare_spatial", None)
        if not callable(prepare):
            raise ApplicationError(503, "SPATIAL_RAN_UNAVAILABLE", "Native location-bound RAN admission is not configured")
        try:
            return await asyncio.to_thread(prepare, body)
        except ApplicationError:
            raise
        except Exception as exc:
            if str(exc) in {"topology_revision_changed", "Spatial topology revision changed"}:
                raise ApplicationError(409, "STALE_CITY_TOPOLOGY", "City topology changed; refresh and retry") from None
            raise ApplicationError(503, "SPATIAL_RAN_UNAVAILABLE", "No verified eligible native gNB is available for this demand") from None

    async def preview(self, payload):
        """Validate and compile demand without touching UE, policy or round state."""
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        body = validate_scenario_request(payload, allow_none=True)
        contract = (ServiceRequirementCompiler.compile(body["serviceRequirements"], body, traffic_mode=body.get("trafficMode"))
                    if "serviceRequirements" in body else None)
        spatial = await self.prepare_spatial(body)
        warnings = list(contract["warnings"]) if contract else []
        admitted = True
        try:
            admit_traffic(body)
        except TrafficAdmissionBlocked as exc:
            admitted = False
            warnings.append({"code": exc.code, "detail": str(exc)})
        from backend.onprem.runtime.city_catalog import placement_input
        return {"schemaVersion": "cityverse.service-preview/v1", "admissible": admitted,
                "placementDemand": placement_input(body, spatial),
                "networkCapacityVerified": False, "resolvedRequirements": contract, "spatialResolution": spatial,
                "capabilities": ServiceRequirementCompiler.capabilities(), "warnings": warnings}

    async def service_capabilities(self):
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        from backend.onprem.scenarios.profiles import DEFAULT_TRAFFIC, public_catalog
        from backend.onprem.scenarios.intent_bridge import strategy_capabilities
        from backend.onprem.runtime.city_catalog import public_city_catalog
        try:
            city = await asyncio.to_thread(public_city_catalog)
        except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError):
            city = {'status': 'not_configured', 'nativeConnectivityVerified': False}
        return {**ServiceRequirementCompiler.capabilities(), **strategy_capabilities(),
                "cityAccessCatalog": city, "profileCatalog": public_catalog(),
                "spatialDemand": {"supported": callable(getattr(getattr(getattr(self.context, "round_coordinator", None), "resources", None), "prepare_spatial", None)),
                    "locationTypes": ["point"], "optionalFields": ["sourceLocation.buildingId", "serviceId"],
                    "selection": "nearest_commissioned_eligible_native_gnb",
                    "requiresNativeAttachmentProof": True, "coordinateUnit": "km", "configuredDoesNotProveConnectivity": True}, "trafficDefaults": {name:dict(row) for name,row in DEFAULT_TRAFFIC.items()}}

    async def sla(self, round_id):
        from backend.onprem.scenarios.objectives import round_sla_assessment
        def read():
            snapshot, _runtime, _path = self._observation_scope(round_id, display=True)
            return {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
                    "assessment": round_sla_assessment(snapshot)}
        return await asyncio.to_thread(read)

    async def start(self, payload):
        ctx = self.context
        body = validate_scenario_request(payload, allow_none=True)
        try:
            replay = await asyncio.to_thread(replay_current, ctx.round_ledger, body)
            if replay is not None:
                return self._legacy_snapshot(with_traffic_counts(replay))
            queued = await asyncio.to_thread(ctx.scenario_schedule.replay, body)
            if queued is not None:
                return queued
            admit_traffic(body)
            await self.prepare_spatial(body)
            if ctx.round_coordinator is None:
                raise ApplicationError(503, "RUNTIME_UNAVAILABLE", "Native scenario traffic/policy capability verification is incomplete")
            async with timed_admission_lock(ctx.application_admission_lock):
                if ctx.maintenance_blocked():
                    raise ApplicationError(409, "MAINTENANCE_UNRESOLVED", "Native maintenance remains quarantined")
                if ctx.application_shutting_down:
                    raise ApplicationError(503, "RUNTIME_STOPPING", "Native runtime is draining owned work")
                replay = await asyncio.to_thread(replay_current, ctx.round_ledger, body)
                if replay is not None:
                    return self._legacy_snapshot(with_traffic_counts(replay))
                queued = await asyncio.to_thread(ctx.scenario_schedule.replay, body)
                if queued is not None:
                    return queued
                current = await asyncio.to_thread(read_cleanup, ctx.round_ledger)
                state = await asyncio.to_thread(ctx.scenario_schedule.lifecycle, current)
                entries = await asyncio.to_thread(ctx.scenario_schedule.entries)
                if not state['canSubmitEvaluation'] or any(e['status'] == 'dispatching' for e in entries):
                    raise ApplicationError(409, 'ENVIRONMENT_PREPARING', 'Scenario environment is being prepared or released')
                if (current.get('roundId') and current['stage'] != 'completed') or entries:
                    receipt = await asyncio.to_thread(ctx.scenario_schedule.enqueue, body)
                    ctx.scenario_scheduler.wake()
                    return receipt
                with admission_span('admission.guards'):
                    if not await asyncio.to_thread(ctx.runtime_policy_clear):
                        raise ApplicationError(409, "POLICY_OUTCOME_UNKNOWN", "Native policy ownership is unresolved")
                receipt = await asyncio.to_thread(ctx.scenario_schedule.enqueue, body)
                dispatched = await ctx.scenario_scheduler.dispatch(receipt)
                if dispatched.get('schemaVersion') == 'cityverse.scenario-submission/v1':
                    return dispatched
                return self._legacy_snapshot(with_traffic_counts(dispatched))
        except TrafficAdmissionBlocked as exc:
            raise ApplicationError(503, exc.code, str(exc)) from exc
        except RequestIdConflict as exc:
            raise ApplicationError(409, "REQUEST_ID_CONFLICT", str(exc)) from exc
        except (RoundRuntimeBlocked, RoundUnavailable) as exc:
            raise ApplicationError(503, "RUNTIME_UNAVAILABLE", str(exc)) from exc
        except RoundConflict as exc:
            raise ApplicationError(409, "ROUND_CONFLICT", str(exc)) from exc

    async def start_intent_traffic(self,payload,lease,*,held_reservation=None):
        """Private trusted start: formal traffic is never a legacy finite NONE run."""
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        if type(lease) is not ServiceIntentTrafficLease or not lease.active(time.time()):
            raise ApplicationError(409,'INTENT_TRAFFIC_LEASE_INVALID','Active typed intent traffic lease required')
        body=validate_scenario_request(payload,allow_none=True)
        if body['strategy']!='none' or body['requestId']!=lease.request_id:
            raise ApplicationError(409,'INTENT_TRAFFIC_SCOPE_CHANGED','Lease requires exact NONE traffic request')
        ctx=self.context
        if ctx.round_coordinator is None:
            raise ApplicationError(503,'RUNTIME_UNAVAILABLE','Native scenario coordinator is unavailable')
        async with timed_admission_lock(ctx.application_admission_lock):
            if ctx.application_shutting_down or ctx.maintenance_blocked():
                raise ApplicationError(409,'RUNTIME_UNAVAILABLE','Native scenario admission is blocked')
            if not callable(ctx.round_coordinator.intent_traffic_restorer):
                ctx.round_coordinator.intent_traffic_restorer=getattr(ctx,'intent_traffic_restorer',None)
            if not callable(ctx.round_coordinator.intent_traffic_renewer):
                ctx.round_coordinator.intent_traffic_renewer=getattr(ctx,'intent_traffic_renewer',None)
            admit_traffic(body)
            await self.prepare_spatial(body)
            result=await ctx.round_coordinator.start(body,trusted_lease=lease,held_reservation=held_reservation)
        return {'requestId':lease.request_id,'accepted':True,'roundId':result['roundId'],
            'generation': result.get('generation'),
            'topologyRevision': result.get('spatialDemand', {}).get('topologyRevision'),
            'spatialRevision': result.get('spatialDemand', {}).get('resolution', {}).get('spatialRevision'),
            'status':'running','service_intent_traffic_lease':lease.to_dict(),'service_effect_verified':False}

    async def finish_intent_traffic(self,lease,proof):
        """After native intent restoration, wait for independently verified UE cleanup."""
        ctx=self.context
        if ctx.round_coordinator is None:
            await asyncio.to_thread(ctx.round_ledger.finish_intent_traffic,lease,proof)
            return {'released':False,'pending':True,'service_intent_traffic_lease':lease.to_dict()}
        return await ctx.round_coordinator.finish_intent_traffic(lease,proof)

    async def drain_intent_traffic(self,lease,permit):
        ctx=self.context
        if ctx.round_coordinator is None:
            await asyncio.to_thread(ctx.round_ledger.drain_intent_traffic,lease,permit)
            return {'drained':False,'native_ues_gone':False,'pending':True}
        return await ctx.round_coordinator.drain_intent_traffic(lease,permit)

    async def queue(self, request_id=None):
        if request_id is not None:
            receipt = await asyncio.to_thread(self.context.scenario_schedule.find, request_id=request_id)
            if receipt is None:
                raise ApplicationError(404, 'SUBMISSION_NOT_FOUND', 'Scenario submission was not found')
            return receipt
        return {'schemaVersion': 'cityverse.scenario-queue/v1',
                'entries': await asyncio.to_thread(self.context.scenario_schedule.entries)}

    async def cancel_queued(self, queue_id):
        ctx = self.context
        async with ctx.application_admission_lock:
            try:
                receipt = await asyncio.to_thread(ctx.scenario_schedule.find, queue_id=queue_id)
                if receipt is None:
                    raise KeyError(queue_id)
                if receipt['status'] == 'cancelled':
                    return receipt
                result = await asyncio.to_thread(ctx.scenario_schedule.transition, queue_id, 'cancelled', expected={'queued','blocked'})
                ctx.scenario_scheduler.wake()
                return result
            except KeyError as exc:
                raise ApplicationError(404, 'SUBMISSION_NOT_FOUND', 'Scenario submission was not found') from exc
            except RoundConflict as exc:
                raise ApplicationError(409, 'SUBMISSION_ALREADY_STARTED', 'Cancel the owning scenario round instead') from exc

    async def cancel(self, round_id):
        ctx = self.context
        if ctx.round_coordinator is None:
            raise ApplicationError(503, "SCENARIO_CAPABILITY_UNAVAILABLE", "Native coordinator is unavailable")
        async with ctx.application_admission_lock:
            if ctx.application_shutting_down or ctx.maintenance_blocked():
                raise ApplicationError(409, "MAINTENANCE_UNRESOLVED", "Native admission is stopped")
            try:
                return self._legacy_snapshot(await ctx.round_coordinator.cancel(round_id))
            except RoundConflict as exc:
                raise ApplicationError(409, "ROUND_CONFLICT", str(exc)) from exc

    async def set_ues(self, payload):
        ctx = self.context
        counts = validate_counts(payload)
        if sum(counts.values()) > MAX_ROUND_UES:
            raise ValueError(f"On-prem application total UE count must not exceed {MAX_ROUND_UES}")
        async with ctx.application_admission_lock:
            if ctx.maintenance_blocked():
                raise ApplicationError(409, "MAINTENANCE_UNRESOLVED", "Native maintenance blocks all UE writes")
            if ctx.application_shutting_down:
                raise ApplicationError(503, "RUNTIME_STOPPING", "Native runtime is draining owned work")
            if sum(counts.values()) and not await asyncio.to_thread(ctx.runtime_policy_clear):
                raise ApplicationError(409, "POLICY_OUTCOME_UNKNOWN", "Native policy ownership is unresolved; only zero-UE cleanup is allowed")
            current = read_cleanup(ctx.round_ledger)
            if current["roundId"] and current["stage"] != "completed":
                raise ApplicationError(409, "ROUND_OWNS_UE_RESOURCES", "Use scenario-round cancellation; compatibility UE requests cannot replace active round resources")
            try:
                blocker = ctx.ue_controller.mutation_blocker(counts)
                if blocker:
                    raise ApplicationError(409, "UE_GENERATION_RECONCILIATION_REQUIRED", blocker)
                result = await ctx.ue_controller.request(counts)
            except EvidenceUnavailable as exc:
                raise ApplicationError(409, "UE_GENERATION_RECONCILIATION_REQUIRED", str(exc)) from exc
        await ctx.service.broadcast({"type": "ue_status", "payload": result})
        return result
