"""Composition root and process lifecycle; one context per ASGI application."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from backend.onprem.settings import ApiSettings, PROJECT_ROOT
from backend.onprem.storage.executions import ExecutionRepository
from backend.onprem.application.city import CityService
from backend.onprem.application.providers import (CapacityStartupBlocked, configured_daily_native_observer,
    configured_round_coordinator, policy_quarantine_registry_from_environment, scenario_runtime_enabled)
from backend.onprem.policy.safety import policy_inventory_clear, policy_quarantine_clear
from backend.onprem.runtime.ue import UEController, GenerationStore, EvidenceUnavailable
from backend.onprem.runtime.kubeadm import configured_driver
from backend.onprem.runtime.bootstrap import CityBootstrap
from backend.onprem.runtime.errors import LocalKubernetesError
from backend.onprem.storage.rounds import RoundLedger, maintenance_pending, maintenance_digest
from backend.onprem.runtime.errors import RoundRuntimeBlocked


def _log_manager_capture_close(result=None, *, failed=False):
    """Bounded shutdown diagnostics; never stringify an exception or result."""
    try:
        result = result if type(result) is dict else {}
        def flag(name):
            value = result.get(name)
            return value if type(value) is bool else None
        def count(name):
            value = result.get(name)
            return value if type(value) is int and 0 <= value <= (1 << 63) - 1 else None
        logging.getLogger('uvicorn.error').info(
            'manager.failure_capture_close category=%s drained=%s pending=%s workerAlive=%s failed=%s',
            'generic_failed' if failed else 'result', flag('drained'), count('pending'),
            flag('workerAlive'), count('failed'))
    except Exception:
        pass


class ApplicationContext:
    """Own stores, services, admission lock and lifecycle without module globals."""

    def __init__(self, settings: ApiSettings, *, ue_controller=None, city_service=None,
                 round_ledger=None, coordinator=None):
        self.settings = settings
        self.ue_controller = ue_controller if ue_controller is not None else UEController(
            GenerationStore(settings.ue_state_file), driver=configured_driver())
        self.service = city_service if city_service is not None else CityService(
            ExecutionRepository(settings.state_file), ue_controller=self.ue_controller)
        self.round_ledger = round_ledger if round_ledger is not None else RoundLedger(
            settings.round_state_file, canonical_reports=settings.canonical_reports)
        from backend.onprem.storage.measurement_timings import MeasurementTimingRepository
        self.measurement_timings = MeasurementTimingRepository(self.round_ledger.path)
        from backend.onprem.storage.scenario_queue import ScenarioScheduleRepository
        from backend.onprem.application.scenario_queue import ScenarioScheduler
        self.scenario_schedule = ScenarioScheduleRepository(self.round_ledger)
        self.scenario_scheduler = ScenarioScheduler(self)
        self.round_coordinator = coordinator
        self.application_admission_lock = asyncio.Lock()
        self.application_shutting_down = False
        self.application_maintenance = False
        self.manager_failure_capture = None
        self.round_unavailable_reason = 'Native scenario traffic/policy capability verification is incomplete'
        self.policy_quarantine_registry = policy_quarantine_registry_from_environment(os.environ)
        self.city_bootstrap = CityBootstrap(self.ue_controller, submit_baseline=self.submit_city_baseline,
            guard=self.bootstrap_blocker)

    def bootstrap_blocker(self):
        if self.application_shutting_down or self.maintenance_blocked():
            return 'Runtime is draining or maintenance is unresolved'
        current = self.round_ledger.batch_control()
        if current.get('roundId') and current.get('stage') != 'completed':
            return 'An active scenario owns UE resources'
        if not self.runtime_policy_clear():
            return 'Policy ownership is unresolved'
        return None

    def configured_daily_native_observer(self):
        return configured_daily_native_observer(self)

    async def configured_round_coordinator(self):
        return await configured_round_coordinator(self)

    async def submit_city_baseline(self, counts):
        async with self.application_admission_lock:
            if self.application_shutting_down or self.maintenance_blocked() or not self.runtime_policy_clear():
                raise EvidenceUnavailable("City bootstrap admission is blocked")
            current_round = self.round_ledger.batch_control()
            if current_round.get("roundId") and current_round.get("stage") != "completed":
                raise EvidenceUnavailable("An active scenario owns UE resources")
            await self.ue_controller.request(counts)

    def maintenance_blocked(self):
        return self.application_maintenance or self.round_ledger.has_pending_maintenance()

    def load_maintenance_startup(self):
        """Trusted startup-only reservation. No physical dispatch or acknowledgement.

        OS-fence acquisition precedes this function. A separately audited graceful
        drain receipt is mandatory: lock availability is NOT evidence of graceful drain.
        Existing unresolved reservations quarantine startup even with flags removed.
        """
        names = ("CITYVERSE_ONPREM_MAINTENANCE_PLAN_FILE", "CITYVERSE_ONPREM_MAINTENANCE_PLAN_SHA256",
                 "CITYVERSE_ONPREM_MAINTENANCE_DRAIN_FILE", "CITYVERSE_ONPREM_MAINTENANCE_DRAIN_SHA256")
        values = [os.environ.get(name, "") for name in names]
        if not any(values) and os.environ.get("CITYVERSE_ONPREM_MAINTENANCE_ENABLED") != "true":
            return self.round_ledger.has_pending_maintenance()
        if not all(values) or os.environ.get("CITYVERSE_ONPREM_MAINTENANCE_ENABLED") != "true":
            raise RoundRuntimeBlocked("Complete trusted maintenance startup configuration required")
        def read(path, digest):
            try:
                target = Path(path)
                if not target.is_absolute() or target.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError()
                with target.open("rb") as stream:
                    raw = stream.read(131073)
                if len(raw) > 131072 or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError()
                value = json.loads(raw)
                if maintenance_digest(value) != digest:
                    raise ValueError()
                return value
            except (OSError, ValueError, TypeError):
                raise RoundRuntimeBlocked("Invalid pinned canonical maintenance artifact") from None
        plan, drain = read(values[0], values[1]), read(values[2], values[3])
        if plan.get("drainDigest") != values[3]:
            raise RoundRuntimeBlocked("Maintenance drain artifact is not bound to plan")
        self.round_ledger.reserve_maintenance(plan, plan_sha256=values[1], drain=drain, now=time.time())
        return True

    def runtime_policy_clear(self):
        if not policy_quarantine_clear(self.policy_quarantine_registry):
            return False
        if not scenario_runtime_enabled():
            return True  # Preserve the older optional quarantine contract.
        inventory = Path(os.environ.get("CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY",
                                       PROJECT_ROOT / ".tmp" / "onprem-policy-journal-inventory.json"))
        return inventory.is_absolute() and policy_inventory_clear(inventory)

    @asynccontextmanager
    async def lifespan(self, _app):
        await self.service.state.load()
        # Read the durable retirement marker only after acquire obtains the UE
        # process lock, before acquire can rewrite its raw status bytes.
        self.ue_controller.startup_quarantine_check = self.round_ledger.has_pending_maintenance
        self.ue_controller.acquire()
        try:
            self.service.daily_observer = await asyncio.to_thread(self.configured_daily_native_observer)
        except (LocalKubernetesError, ValueError, TypeError, OSError, json.JSONDecodeError):
            self.service.daily_observer = None
        self.service._daily_observation_cache = None
        self.service._daily_observation_cached_at = float("-inf")
        if self.ue_controller.driver is not None:
            self.ue_controller.driver.terminal_authorizer = None
        self.application_shutting_down = False
        self.application_admission_lock = asyncio.Lock()
        stop = asyncio.Event()
        ue_task = None
        self.round_coordinator = None
        self.round_unavailable_reason = "Native scenario runtime is disabled unless explicitly configured"
        try:
            # No autonomous controller, transport, or round recovery may start until
            # the startup reservation gate has run under the exclusive OS fence.
            startup_maintenance = self.load_maintenance_startup()
            self.application_maintenance = bool(getattr(self.ue_controller, "_startup_quarantined", False)) or startup_maintenance
            if self.application_maintenance:
                self.round_unavailable_reason = "Startup maintenance quarantine; physical recovery is not wired"
            else:
                ue_task = asyncio.create_task(self.ue_controller.run(self.service.broadcast, stop=stop))
                try:
                    self.round_coordinator = await self.configured_round_coordinator()
                    if self.round_coordinator is not None:
                        self.round_coordinator.resources.service_routes = getattr(self, 'intent_service_routes', None)
                except CapacityStartupBlocked:
                    raise
                except RoundRuntimeBlocked as exc:
                    self.round_unavailable_reason = str(exc)
                from archive.legacy.operations.recovery.gen13_round_owned_ue_stop import load_startup_intent, run_startup_intent
                gen13_stop = load_startup_intent(os.environ)
                if gen13_stop is not None and self.round_coordinator is None:
                    raise RoundRuntimeBlocked("Gen13 UE-stop startup requires the native round coordinator")
                if self.round_coordinator is not None:
                    if gen13_stop is not None:
                        await run_startup_intent(
                            intent=gen13_stop, ledger=self.round_ledger,
                            resources=self.round_coordinator.resources,
                            application_lock=self.application_admission_lock,
                            coordinator_lock=self.round_coordinator._lock, now=time.time)
                    await self.round_coordinator.recover()
                    await self.scenario_scheduler.start()
            yield
        finally:
            async with self.application_admission_lock:
                self.application_shutting_down = True
            try:
                await self.scenario_scheduler.close()
                if self.round_coordinator is not None and not self.application_maintenance:
                    await drain_native_task(asyncio.create_task(self.round_coordinator.close()))
                    traffic = getattr(getattr(self.round_coordinator, "resources", None), "traffic", None)
                    close_traffic = getattr(traffic, "close", None)
                    if callable(close_traffic):
                        await drain_native_task(asyncio.create_task(asyncio.to_thread(close_traffic)))
            finally:
                try:
                    stop.set()
                    try:
                        if ue_task is not None:
                            await drain_native_task(ue_task)
                    finally:
                        await asyncio.to_thread(self.measurement_timings.close, 5.)
                        self.ue_controller.release()
                        self.round_coordinator = None
                        self.application_shutting_down = False
                        self.application_maintenance = False
                finally:
                    capture, self.manager_failure_capture = self.manager_failure_capture, None
                    if capture is not None:
                        try:
                            close_result = await asyncio.to_thread(capture.close, 2.)
                        except Exception:
                            _log_manager_capture_close(failed=True)
                        else:
                            _log_manager_capture_close(close_result)
            for task in list(self.service.tasks):
                task.cancel()
            if self.service.tasks:
                await asyncio.gather(*self.service.tasks, return_exceptions=True)

async def drain_native_task(task):
    """Keep the process fence until thread-backed work has actually returned."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()

