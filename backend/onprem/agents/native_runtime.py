"""Run the adaptive agents against serial, durable native round measurements."""
from __future__ import annotations
from backend.onprem.storage.current_state import current_reader, read_current

import asyncio
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import re
import time

from backend.onprem.agents.contracts import AgentLoopBlocked, ScenarioContract
from backend.onprem.agents.executor import ExecutorAgent
from backend.onprem.agents.memory import ValidatedMemory
from backend.onprem.agents.native_observation import native_observation
from backend.onprem.agents.roles import ResponsesToolModel
from backend.onprem.agents.validator import ValidatorAgent


class _NativeObserver:
    """Worker-thread bridge; all coordinator and ledger writes stay on its event loop."""

    def __init__(self, coordinator, event_loop):
        self.coordinator, self.event_loop = coordinator, event_loop
        self.reads = 0
        self.publication = None

    def read(self):
        if self.reads and not self.scenario_active():
            return {"scenarioActive": False}
        if self.reads:
            window = self.coordinator._measure("measuring_result")
            future = asyncio.run_coroutine_threadsafe(
                window, self.event_loop)
            self.publication = future.result()
        self.reads += 1
        snapshot = current_reader(self.coordinator.ledger)()
        self.coordinator._check_cancel()
        return native_observation(snapshot)

    def scenario_active(self):
        """Pure budget check used before scheduling another native window."""
        return not self.coordinator._cancel.is_set()

    def can_start_policy(self):
        return self.scenario_active()


@dataclass
class NativeAdaptiveRuntime:
    """Trusted startup dependencies for 1..100 selected traffic UEs."""

    planner: object
    qer_provider: object
    profiles: dict[str, dict[str, int]]
    journal_path: Path
    memory_path: Path
    fixture_config_sha256: str | None = None
    fixture_quarantine_registry: Path | None = None
    metric: str = "receivedUplinkMbps"
    direction: str = "increase"
    minimum_improvement_percent: float = 5
    holdout_tolerance_percent: float = 5
    commissioning_wait_seconds: int = 120
    role_models: dict | None = None
    maximum_cycles: int = 5
    persistent: bool = True
    maximum_turns: int = 8
    maximum_tool_calls: int = 24
    interval_seconds: float = 1
    maximum_policy_changes: int = 3
    minimum_cycles_between_changes: int = 1

    def __post_init__(self):
        if (not isinstance(self.journal_path, Path) or not self.journal_path.is_absolute()
                or not isinstance(self.memory_path, Path) or not self.memory_path.is_absolute()
                or (self.fixture_config_sha256 is not None and
                    (type(self.fixture_config_sha256) is not str or
                     re.fullmatch(r"[0-9a-f]{64}", self.fixture_config_sha256) is None))
                or (self.fixture_quarantine_registry is not None and
                    (not isinstance(self.fixture_quarantine_registry, Path)
                     or not self.fixture_quarantine_registry.is_absolute()))
                or type(self.maximum_cycles) is not int or not 1 <= self.maximum_cycles <= 5
                or type(self.persistent) is not bool
                or type(self.maximum_turns) is not int or not 1 <= self.maximum_turns <= 30
                or type(self.maximum_tool_calls) is not int or not 1 <= self.maximum_tool_calls <= 100
                or type(self.interval_seconds) not in (int, float) or not 0 <= self.interval_seconds <= 60
                or type(self.maximum_policy_changes) is not int or not 0 <= self.maximum_policy_changes <= 1000
                or type(self.minimum_cycles_between_changes) is not int or not 1 <= self.minimum_cycles_between_changes <= 1000
                or type(self.commissioning_wait_seconds) is not int
                or not 30 <= self.commissioning_wait_seconds <= 300
                or not isinstance(self.profiles, dict) or not 1 <= len(self.profiles) <= 3
                or (self.role_models is None and not callable(getattr(self.planner, "plan", None)))
                or not callable(getattr(self.qer_provider, "native", None))
                or not callable(getattr(self.qer_provider, "adapter", None))
                or not callable(getattr(self.qer_provider, "commissioning_scope", None))
                or not callable(getattr(self.qer_provider, "capability", None))):
            raise ValueError("Adaptive runtime requires bounded native providers and durable paths")
        self.adapter = None
        self._round_db = None
        self._graph_status = {}
        if self.role_models is None and getattr(self.planner, "client", None) is not None:
            self.role_models = {role: ResponsesToolModel(self.planner.client)
                                for role in ("planner", "executor", "validator")}
        if self.role_models is not None and (set(self.role_models) != {"planner", "executor", "validator"}
                or any(not callable(getattr(model, "complete", None)) for model in self.role_models.values())):
            raise ValueError("Three tool-using role models are required")

    def status(self):
        return deepcopy(self._graph_status)

    def bind(self, round_reader):
        if self.adapter is not None:
            raise ValueError("Adaptive QER journal is already bound")
        self.adapter = self.qer_provider.adapter(self.journal_path, round_reader, self.profiles)
        self._round_db = getattr(getattr(round_reader, '__self__', None), 'path', None)

    def fixture_owned_quiet(self, snapshot, registry):
        """Internal exact-round exception; external admission never calls this."""
        if (self.fixture_config_sha256 is None or self.fixture_quarantine_registry is None
                or registry != self.fixture_quarantine_registry or type(snapshot) is not dict):
            return False
        from backend.onprem.validation.constrained import ConstrainedFixtureBlocked, fixture_path, owned_fixture_quarantine_clear
        if not fixture_path(self.journal_path).exists():
            return False
        try:
            return owned_fixture_quarantine_clear(
                self.journal_path, snapshot,
                expected_config_sha256=self.fixture_config_sha256,
                expected_quarantine_registry=registry)
        except (ConstrainedFixtureBlocked, OSError, ValueError, TypeError):
            return False

    def fixture_restored_quiet(self, snapshot, registry):
        """Internal cleanup exception only after all original QER values replay."""
        if (self.fixture_config_sha256 is None or self.fixture_quarantine_registry is None
                or registry != self.fixture_quarantine_registry or type(snapshot) is not dict):
            return False
        from backend.onprem.validation.constrained import ConstrainedFixtureBlocked, fixture_path
        from backend.onprem.validation.restore import restored_fixture_cleanup_clear
        if not fixture_path(self.journal_path).exists():
            return False
        try:
            return restored_fixture_cleanup_clear(
                self.journal_path, snapshot,
                expected_config_sha256=self.fixture_config_sha256,
                expected_quarantine_registry=registry)
        except (ConstrainedFixtureBlocked, OSError, ValueError, TypeError):
            return False

    def claim_constrained(self, snapshot):
        """Claim only the separately armed controlled fixture for this round."""
        if self.fixture_config_sha256 is None or self.fixture_quarantine_registry is None:
            return None
        from backend.onprem.validation.constrained import claim_constrained
        return claim_constrained(
            self.journal_path, snapshot,
            expected_config_sha256=self.fixture_config_sha256,
            expected_quarantine_registry=self.fixture_quarantine_registry, round_db=self._round_db)

    def install_constrained(self, coordinator, claim):
        from backend.onprem.validation.controller import install_constrained_fixture
        return install_constrained_fixture(
            self.qer_provider, self.journal_path, current_reader(coordinator.ledger), claim,
            expected_config_sha256=self.fixture_config_sha256,
            expected_quarantine_registry=self.fixture_quarantine_registry,
            check_cancel=coordinator._check_cancel)

    def cleanup_group_policy(self, snapshot):
        """Resume outer-restored cleanup without demanding obsolete cap rates."""
        from backend.onprem.validation.constrained import read_constrained_receipt, fixture_path
        from backend.onprem.validation.native import seal_native_receipt
        from backend.onprem.validation.restore import reconcile_original_restored_model
        record = (read_constrained_receipt(self.journal_path, snapshot.get('requestId'))
                  if fixture_path(self.journal_path).exists() else None)
        if record is None or record['state'] != 'original_restored':
            return self.adapter.cleanup(snapshot['roundId'])
        dumps = {}
        proof = self.qer_provider.native(snapshot, _fixture_raw_sink=dumps)
        native = seal_native_receipt(snapshot, record['claim'], proof, dumps, 'original_restored')
        return reconcile_original_restored_model(self.journal_path, snapshot, native,
            expected_config_sha256=self.fixture_config_sha256,
            expected_quarantine_registry=self.fixture_quarantine_registry)

    def restore_constrained(self, coordinator, claim):
        """Model cleanup must be durable before the outer original-rate restore."""
        from backend.onprem.validation.constrained import read_constrained_receipt, constrained_claim_for_snapshot
        from backend.onprem.validation.native import seal_native_receipt
        from backend.onprem.validation.restore import seal_inner_restored
        from backend.onprem.validation.controller import restore_constrained_fixture
        snapshot = read_current(coordinator.ledger)
        if constrained_claim_for_snapshot(
                self.journal_path, snapshot,
                expected_quarantine_registry=self.fixture_quarantine_registry) != claim:
            raise AgentLoopBlocked("Controlled fixture changed before cleanup")
        record = read_constrained_receipt(self.journal_path, snapshot["requestId"])
        if record['state'] == 'unwritten':
            return {'claim': claim, 'unwritten': True}
        cleanup = [item for item in snapshot.get("actions", [])
                   if type(item) is dict and item.get("kind") == "adaptive_qer_cleanup"]
        if (len(cleanup) != 1 or cleanup[0].get("cleanupVerified") is not True
                or cleanup[0].get("reconciliationRequired") is not False
                or (record["state"] in {"inner_restored", "original_restored", "reconciled"}
                    and cleanup[0].get("policyActionIds") != record["modelPolicyActionIds"])):
            raise AgentLoopBlocked("Controlled model cleanup differs from durable fixture")
        if record['state'] == 'active':
            from contextlib import closing
            import sqlite3
            from backend.onprem.validation.constrained import fixture_path
            with closing(sqlite3.connect(fixture_path(self.journal_path).as_uri()+'?mode=ro',uri=True)) as db:
                partial = db.execute('SELECT 1 FROM fixture_attempts WHERE request_id=? LIMIT 1',
                                     (snapshot['requestId'],)).fetchone()
            if partial:
                from backend.onprem.operations.recovery.partial_fixture import PartialFixtureRecovery
                return PartialFixtureRecovery(self.qer_provider,self.journal_path,current_reader(coordinator.ledger),
                    config_sha256=self.fixture_config_sha256,registry=self.fixture_quarantine_registry).restore()
            from backend.onprem.validation.restore import retire_unwritten_fixture
            dumps = {}
            proof = self.qer_provider.native(snapshot, _fixture_raw_sink=dumps)
            unchanged = seal_native_receipt(snapshot, claim, proof, dumps, 'original_restored')
            return retire_unwritten_fixture(self.journal_path, snapshot, unchanged,
                expected_config_sha256=self.fixture_config_sha256,
                expected_quarantine_registry=self.fixture_quarantine_registry)
        if record["state"] in {"original_restored", "reconciled"}:
            return {"claim": claim, "originalRestoredNative":
                    record["originalRestoredNative"]}
        if record["state"] == "fixture_installed":
            dumps = {}
            proof = self.qer_provider.native(snapshot, _fixture_raw_sink=dumps)
            inner = seal_native_receipt(snapshot, claim, proof, dumps, "inner_restored")
            seal_inner_restored(
                self.journal_path, snapshot, inner,
                expected_quarantine_registry=self.fixture_quarantine_registry)
        elif record["state"] != "inner_restored":
            raise AgentLoopBlocked("Controlled fixture cleanup requires reconciliation")
        def check_restore_owner():
            # Cleanup remains authorized after forward simulation is cancelled.
            # The provider independently fences every exact original-rate write.
            current = read_current(coordinator.ledger)
            if (not isinstance(current, dict)
                    or any(current.get(key) != claim[key]
                           for key in ('requestId', 'roundId', 'generation'))):
                raise AgentLoopBlocked('Controlled fixture owner changed during restoration')
        return restore_constrained_fixture(
            self.qer_provider, self.journal_path, current_reader(coordinator.ledger), claim,
            expected_config_sha256=self.fixture_config_sha256,
            expected_quarantine_registry=self.fixture_quarantine_registry,
            check_cancel=check_restore_owner)

    async def _await_commissioning(self, coordinator):
        from backend.onprem.runtime.round_resources import action
        snapshot = read_current(coordinator.ledger)
        scope = self.qer_provider.commissioning_scope(snapshot, tuple(self.profiles))
        intent = action("adaptive_qer_commissioning", scope=scope, accepted=False)
        coordinator._action(intent)
        deadline = time.monotonic() + self.commissioning_wait_seconds
        while True:
            coordinator._check_cancel()
            current = read_current(coordinator.ledger)
            if ((current.get("roundId"), current.get("generation"))
                    != (scope["roundId"], scope["generation"])):
                raise AgentLoopBlocked("Adaptive round changed during QER commissioning")
            receipt = await asyncio.to_thread(self.qer_provider.capability, current, tuple(self.profiles))
            if isinstance(receipt, dict) and receipt.get("verified") is True:
                coordinator._finish_action(intent["id"], accepted=True, proofId=receipt["proofId"])
                return
            if time.monotonic() >= deadline:
                coordinator._finish_action(intent["id"], accepted=False, timedOut=True)
                raise AgentLoopBlocked("Round-scoped QER commissioning was not verified")
            await asyncio.sleep(min(2, deadline - time.monotonic()))

    async def run(self, coordinator):
        from backend.onprem.agents.graph import LangGraphAgentLoop
        from backend.onprem.agents.observations import PlannerObservations
        snapshot = read_current(coordinator.ledger)
        if (snapshot.get("strategy") != "ai"
                or not 1 <= snapshot.get("allocation", {}).get("total", 0) <= 100
                or self.adapter is None):
            raise AgentLoopBlocked("Adaptive round requires 1..100 selected traffic UEs")
        if self.role_models is None:
            raise AgentLoopBlocked("Three tool-using role models are not configured")
        await self._await_commissioning(coordinator)
        snapshot = read_current(coordinator.ledger)
        from backend.onprem.validation.normal import normal_claim_for_snapshot
        try:
            normal_claim = normal_claim_for_snapshot(self.journal_path, snapshot)
        except Exception as exc:
            raise AgentLoopBlocked("Normal fixture authority unavailable") from exc
        if normal_claim is not None:
            published = [item for item in snapshot.get("actions", [])
                         if isinstance(item, dict) and item.get("kind") == "scenario_validation_track"]
            if (len(published) != 1 or published[0].get("accepted") is not True
                    or published[0].get("fixtureClaim") != normal_claim
                    or normal_claim.get("configSha256") != self.fixture_config_sha256):
                raise AgentLoopBlocked("Normal fixture claim was not published by this runtime")
        from backend.onprem.validation.constrained import SCHEMA as CONSTRAINED_SCHEMA, constrained_claim_for_snapshot, read_constrained_receipt
        controlled = [item for item in snapshot.get("actions", [])
                      if isinstance(item, dict) and item.get("kind") == "scenario_validation_track"
                      and isinstance(item.get("fixtureClaim"), dict)
                      and item["fixtureClaim"].get("schema") == CONSTRAINED_SCHEMA]
        if controlled:
            if (normal_claim is not None or len(controlled) != 1
                    or controlled[0].get("accepted") is not True
                    or self.fixture_quarantine_registry is None
                    or controlled[0]["fixtureClaim"] != constrained_claim_for_snapshot(
                        self.journal_path, snapshot,
                        expected_quarantine_registry=self.fixture_quarantine_registry)
                    or controlled[0]["fixtureClaim"].get("configSha256") != self.fixture_config_sha256
                    or read_constrained_receipt(self.journal_path, snapshot["requestId"])["state"]
                    != "fixture_installed"):
                raise AgentLoopBlocked("Controlled fixture is not installed for this round")
        contract = ScenarioContract(
            scenario_id=snapshot["roundId"], event_counts=snapshot["allocation"]["events"],
            metric=self.metric, direction=self.direction,
            minimum_improvement_percent=self.minimum_improvement_percent,
            holdout_tolerance_percent=self.holdout_tolerance_percent,
            profiles=self.profiles, baseline_ues=snapshot["allocation"]["baselineUes"],
            traffic_mode=snapshot.get("trafficMode", "functional"),
            service_requirements_revision=snapshot.get("resolvedRequirements", {}).get("revision"),
            maximum_policy_changes=0 if normal_claim is not None else self.maximum_policy_changes,
            minimum_cycles_between_changes=self.minimum_cycles_between_changes)
        observer = _NativeObserver(coordinator, asyncio.get_running_loop())
        progress_scope = {key: snapshot[key] for key in ('roundId', 'generation')}
        progress_counts = {}
        def progress(event):
            self._graph_status = {**progress_scope,
                                  **event, "updatedAt": time.time()}
            cycle = event.get('cycle')
            if type(cycle) is int and 0 <= cycle < 5:
                patch = {'cyclesAttempted': cycle + 1}
                if (event.get('kind') == 'decision' and event.get('role') == 'validator'
                        and event.get('phase') == 'learn'):
                    patch['cyclesCompleted'] = cycle + 1
                changed = {k: v for k, v in patch.items() if progress_counts.get(k) != v}
                if changed:
                    coordinator.schedule.patch_lifecycle(progress_scope['roundId'], **changed)
                    progress_counts.update(changed)
        loop = LangGraphAgentLoop(contract, observer, ExecutorAgent(self.adapter),
            ValidatorAgent(contract, self.qer_provider.native), ValidatedMemory(self.memory_path),
            self.role_models,
            checkpoint_path=self.journal_path.with_name(self.journal_path.stem + ".graph-checkpoints.db"),
            artifacts_path=self.journal_path.with_name(self.journal_path.stem + ".graph-evidence.db"),
            current_reader=current_reader(coordinator.ledger), check_cancel=coordinator._check_cancel,
            stage_callback=progress, interval_seconds=self.interval_seconds,
            maximum_cycles=self.maximum_cycles, maximum_turns=self.maximum_turns,
            maximum_calls=self.maximum_tool_calls, retain_on_completion=self.persistent)
        loop.observations = PlannerObservations(lambda: loop.current,
            driver=getattr(self.qer_provider, "driver", None), qer_reader=self.qer_provider.native)
        del snapshot  # The progress callback retains only scalar ownership.
        try:
            outcomes = await asyncio.to_thread(loop.run, round_id=progress_scope["roundId"], generation=progress_scope["generation"])
        except Exception:
            progress({"phase": "failed", "status": "failed"})
            raise
        finally:
            # Durable graph artifacts own audit evidence. Drop transient trees
            # even if a model exception traceback retains the loop object.
            loop.current = None
            loop.observations = None
            loop.stage_callback = lambda _event: None
        progress({"phase": "completed", "status": "completed"})
        if observer.publication is None:
            raise AgentLoopBlocked("No post-decision native receiver window was published")
        return outcomes, observer.publication
