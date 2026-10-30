"""LangGraph orchestration of three tool-using LLM roles and native services."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from backend.onprem.agents.contracts import AgentLoopBlocked, _digest, _seconds
from backend.onprem.agents.native_observation import commissioned_candidates
from backend.onprem.agents.observations import PlannerObservations
from backend.onprem.agents.persistence import GraphArtifacts
from backend.onprem.agents.roles import RoleAgent, StageTool, object_schema
from backend.onprem.agents.state import AgentGraphState


class _ScenarioEnded(Exception):
    """A supported observer's pure clock ended before new work could begin."""


class LangGraphAgentLoop:
    """All state transitions are code-owned; all three roles select real tools."""

    def __init__(self, contract, observer, executor, validator, memory, models, *,
                 checkpoint_path: Path, artifacts_path: Path, current_reader=None,
                 observations=None, check_cancel=None, stage_callback=None,
                 clock=time.time, sleep=time.sleep, interval_seconds=0,
                 maximum_cycles=5, maximum_turns=8, maximum_calls=24, retain_on_completion=False):
        if set(models) != {"planner", "executor", "validator"}:
            raise ValueError("Three configured LLM role models are required")
        if type(maximum_cycles) is not int or not 1 <= maximum_cycles <= 5:
            raise ValueError("Invalid graph cycle budget")
        for path in (checkpoint_path, artifacts_path):
            if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
                raise ValueError("Durable graph paths must be absolute and non-symlink")
        self.contract, self.observer, self.executor, self.validator, self.memory = contract, observer, executor, validator, memory
        self.execution_digest = _digest({"contract": contract.digest, "graph_version": 7,
            "maximum_policy_changes": contract.maximum_policy_changes,
            "minimum_cycles_between_changes": contract.minimum_cycles_between_changes,
            "retain_on_completion": bool(retain_on_completion),
            "maximum_cycles": maximum_cycles, "maximum_turns": maximum_turns, "maximum_calls": maximum_calls})
        self.models, self.checkpoint_path, self.artifacts_path = models, checkpoint_path, artifacts_path
        self.current_reader, self.check_cancel = current_reader, check_cancel or (lambda: None)
        self.stage_callback = stage_callback or (lambda _: None)
        self.clock, self.sleep, self.interval_seconds = clock, sleep, interval_seconds
        self.maximum_cycles, self.maximum_turns, self.maximum_calls = maximum_cycles, maximum_turns, maximum_calls
        self.retain_on_completion = bool(retain_on_completion)
        self.policy_retained = False
        self.observations = observations
        self.current = None
        self.artifacts = None
        self.cycle = 0
        self.cleanup_verified = False

    def _audit(self, **event):
        event["cycle"] = self.cycle
        if event.get("call_id"):
            event["call_id"] = _digest([self.cycle, event["call_id"]])
        stored = self.artifacts.audit(**event)
        self.stage_callback(deepcopy(stored if isinstance(stored, dict) else event))

    def _role(self, phase, view, tools, *, finish_accepted=None):
        from backend.onprem.agents.roles import PHASE_ROLES
        role = PHASE_ROLES[phase]
        self.check_cancel()
        model = self.models[role]
        if self.contract.service_requirements_revision:
            view = {**view, "service_requirements_revision": self.contract.service_requirements_revision,
                    "sla_verdict_authority": "deterministic-native-evidence-only"}
        marker = {"scenario_ended": True}
        if phase in {"plan", "precheck", "apply", "postcheck"} and callable(getattr(self.observer, "scenario_active", None)):
            def permitted():
                return (self._scenario_active() is not False
                        or phase == "postcheck" and callable(finish_accepted) and finish_accepted())
            def require_active():
                if not permitted():
                    raise _ScenarioEnded()
            require_active()
            def complete(**kwargs):
                require_active()
                # Schema/allowlist validation still sees the genuine response,
                # including a malformed response returned as time expires.
                return self.models[role].complete(**kwargs)
            model = SimpleNamespace(model=getattr(model, "model", "configured-model"), complete=complete)
            def guard(handler):
                def call(**kwargs):
                    if not permitted():
                        return marker
                    return handler(**kwargs)
                return call
            tools = [replace(tool, handler=guard(tool.handler)) for tool in tools]
        result = RoleAgent(role, model, maximum_turns=self.maximum_turns,
            maximum_calls=self.maximum_calls, audit=self._audit, evidence=self._tool_evidence,
            targets=self._tool_targets).run(phase, view, tools)
        if result["result"] is marker:
            raise _ScenarioEnded()
        return result

    def _tool_evidence(self, metadata, result, arguments):
        from backend.onprem.agents.action_observability import project_tool_evidence
        evidence = project_tool_evidence(metadata["tool"], result, self.current or {}, arguments)
        if evidence is None:
            return None
        evidence.update(roundId=self.round_id, generation=self.generation, cycle=self.cycle,
                        callId=_digest([self.cycle, metadata["call_id"]]), role=metadata["role"],
                        phase=metadata["phase"], tool=metadata["tool"])
        from backend.onprem.agents.topology_observability import evidence_targets
        evidence["targetRefs"] = evidence_targets(evidence, self.current or {}) or metadata.get("targetRefs", [])
        metadata["targetRefs"] = evidence["targetRefs"]
        captured = getattr(getattr(self, "observations", None), "log_evidence", {})
        if metadata["tool"] == "get_nf_log_excerpt":
            saved = captured.pop(arguments.get("target"), None)
            if saved:
                evidence["savedLogs"] = saved
        self._known_targets = list({json_key: row for row in
            getattr(self, "_known_targets", []) + evidence["targetRefs"]
            for json_key in [str(sorted(row.items()))]}.values())[-100:]
        return self.artifacts.put("tool_observability", evidence)

    def _tool_targets(self, metadata, arguments):
        from backend.onprem.agents.topology_observability import resolve_targets
        scope = dict(arguments)
        # A validated reference is code-owned and carries the selected cohort;
        # its profile name alone never determines a target NF.
        ref = arguments.get("validated_ref") or arguments.get("validated_plan_ref")
        if ref:
            try:
                selected = self.artifacts.get(ref).get("selected") or {}
                scope["group_id"] = selected.get("eventId")
            except AgentLoopBlocked:
                pass
        return resolve_targets(self.current or {}, scope, getattr(self, "_known_targets", []))

    def _scenario_active(self):
        predicate = getattr(self.observer, "scenario_active", None)
        if not callable(predicate):
            return None  # Generic observers retain their original read contract.
        value = predicate()
        if type(value) is not bool:
            raise AgentLoopBlocked("graph.scenario_clock_unavailable")
        return value

    def _tool(self, name, handler, schema=None, *, terminal=False, writes=False, parallel_safe=False):
        return StageTool(name, name.replace("_", " "), schema or object_schema(), handler,
                         terminal, writes, parallel_safe)

    def _guard(self, before):
        self.check_cancel()
        self.validator.observe(before)
        self._guard_scope(before)

    def _guard_scope(self, before):
        self.check_cancel()
        if self.current_reader:
            live = self.current_reader()
            expected = before.get("_actuatorSnapshot", before)
            if any(live.get(k) != expected.get(k) for k in ("roundId", "generation", "sequence", "policyRevision", "perUe")):
                raise AgentLoopBlocked("graph.native_fence_changed")

    def _before(self, state):
        return self.artifacts.get(state["before_ref"])

    def _capture_before(self, observed):
        validated_at = self.validator.clock()
        self.validator.observe(observed)
        self._guard_scope(observed)
        ref = self.artifacts.put('observation', observed)
        seal = dict(schema='cityverse.validated-traffic-before/v1', before_ref=ref,
            round_id=observed['roundId'], generation=observed['generation'], sequence=observed['sequence'],
            rows_digest=_digest(observed['perUe']), validated_at=validated_at)
        return dict(before_ref=ref, before_seal_ref=self.artifacts.put('observation_seal',seal))

    def _accepted_before(self, state):
        before = self._before(state)
        seal = self.artifacts.get(state['before_seal_ref'])
        rows = self._sealed_before_rows(before, state['before_ref'], seal)
        self._guard_scope(before)
        return rows, seal

    @staticmethod
    def _candidate_view(candidates):
        return [{"group_id": c["eventId"], "profile_id": c["profileId"],
                 "treatment_count": len(c["treatmentSupis"]), "holdout_count": len(c["holdoutSupis"])} for c in candidates]

    def _observe(self, state):
        self.cycle = state["cycle"]
        self.check_cancel()
        active = self._scenario_active()
        if active is False:
            return {"route": "cleanup", "stop_reason": "scenario_ended", "phase": "cleanup"}
        if active is True and state.get("after_ref"):
            observed = self.artifacts.get(state["after_ref"])
            self._guard(observed)
        else:
            observed = self.observer.read()
        if observed.get("scenarioActive") is not True:
            return {"route": "cleanup", "stop_reason": "scenario_ended", "phase": "cleanup"}
        self.validator.observe(observed)
        if (observed["roundId"], observed["generation"]) != (self.round_id, self.generation):
            raise AgentLoopBlocked("graph.round_changed")
        if self.current is not None and ((observed["roundId"], observed["generation"]) !=
                                          (self.current["roundId"], self.current["generation"])):
            raise AgentLoopBlocked("graph.round_changed")
        self.current = observed
        if self.observations is None:
            self.observations = PlannerObservations(lambda: self.current, clock=self.clock)
        self.observations.record(observed)
        if state.get("active_ref"):
            active = self.artifacts.get(state["active_ref"])
            self.validator.installation(active["receipt"], active["selected"], active["qer_before"],
                                        self.validator.qer_inventory(observed))
        return {**self._capture_before(observed), "phase": "plan", "route": "plan",
                "plan_ref": "", "validated_ref": "", "action_ref": "", "verification_ref": "", "outcome_ref": ""}

    def _planner_preload(self, name, reader):
        started, clock = self.clock(), time.monotonic()
        metadata = dict(kind="preload", role="planner", phase="plan", tool=name, started_at=started)
        self._audit(**metadata, status="entered")
        try:
            result = reader()
        except Exception:
            self._audit(**metadata, status="failed", ended_at=self.clock(),
                        duration_ms=round((time.monotonic() - clock) * 1000, 3))
            raise
        self._audit(**metadata, status="finished", ended_at=self.clock(),
                    duration_ms=round((time.monotonic() - clock) * 1000, 3))
        return result

    def _plan(self, state):
        before = self._before(state)
        self.current = before
        candidates = commissioned_candidates(before, tuple(self.contract.profiles))
        previous = self.artifacts.outcomes()
        active = self.artifacts.get(state["active_ref"])["selected"] if state.get("active_ref") else None
        active_view = {"group_id": active["eventId"], "profile_id": active["profileId"]} if active else None
        used = state.get("changes", 0)
        remaining = max(0, self.contract.maximum_policy_changes - used)
        cycles_until_change = max(0, self.contract.minimum_cycles_between_changes - (
            state["cycle"] - state.get("last_change", -self.contract.minimum_cycles_between_changes)))
        can_change = remaining > 0 and cycles_until_change == 0
        available = [candidate for candidate in candidates
                     if can_change or active and candidate["id"] == active["id"]]
        permitted = ["hold"] + (["apply_group_qer"] if can_change else [])
        if active:
            permitted.append("monitor_current_policy")
        budget = {"maximum_changes": self.contract.maximum_policy_changes,
                  "changes_used": used, "changes_remaining": remaining,
                  "cycles_until_next_change": cycles_until_change}
        tools = self.observations.tools()
        tools.extend([
            self._tool("observe_network", lambda: self.observations.read("get_environment_overview")),
            self._tool("observe_traffic", lambda: self.observations.read("get_traffic_metrics")),
            self._tool("get_traffic_demand", lambda: {"total_ues": self.contract.total_ues,
                "groups": self.contract.group_counts, "metric": self.contract.metric, "direction": self.contract.direction,
                "traffic_mode": before.get("trafficMode", "functional"),
                "demand_source": "user-service-requirements" if "serviceRequirements" in before else "declared-scenario-profile",
                "service_requirements": before.get("resolvedRequirements"),
                "sla": before.get("sla"),
                "load": next(({k: a["workload"].get(k) for k in
                    ("requestedUplinkKbps", "requestedDownlinkKbps", "appliedUplinkKbps", "appliedDownlinkKbps", "capReason")}
                    for a in reversed(before.get("actions", [])) if isinstance(a.get("workload"), dict)), None)}),
            self._tool("get_network_snapshot", lambda: self.observations.read("get_environment_overview")),
            self._tool("get_round_context", lambda: {"round_id": self.round_id,
                "generation": self.generation, "cycle": state["cycle"],
                "groups": self.contract.group_counts, "metric": self.contract.metric,
                "direction": self.contract.direction, "change_budget": budget,
                "remaining_seconds": before.get("remainingSeconds")}),
            self._tool("get_policy_capabilities", lambda: {"actions": permitted,
                "groups_and_profiles": self._candidate_view(available), "change_budget": budget,
                "rates_kbps": {key: dict(value) for key, value in self.contract.profiles.items()}}),
            self._tool("search_lessons", lambda: self.artifacts.get(state["lessons_ref"])),
            self._tool("get_previous_decision", lambda: previous[-1] if previous else None),
            self._tool("get_current_policy", lambda: active_view),
        ])
        schema = object_schema({"action": {"type": "string", "enum": ["apply", "hold"]},
            "group_id": {"anyOf": [{"type": "string", "enum": list(self.contract.group_counts)}, {"type": "null"}]},
            "profile_id": {"anyOf": [{"type": "string", "enum": list(self.contract.profiles)}, {"type": "null"}]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000}})
        schema["properties"]["action"]["description"] = "apply installs a commissioned profile; hold also means monitoring an already active policy without any write."
        schema["properties"]["group_id"]["description"] = "Must be null for hold, even when monitoring an active group. For apply choose a commissioned group."
        schema["properties"]["profile_id"]["description"] = "Must be null for hold, even when monitoring an active profile. For apply choose a commissioned profile."
        def propose(**plan):
            if (plan["action"] == "hold" and (plan["group_id"] is not None or plan["profile_id"] is not None)):
                raise AgentLoopBlocked("plan.hold_has_policy_arguments")
            if plan["action"] == "apply" and not any(c["eventId"] == plan["group_id"] and c["profileId"] == plan["profile_id"] for c in candidates):
                raise AgentLoopBlocked("plan.unsupported_capability")
            if plan["action"] == "apply" and not any(
                    c["eventId"] == plan["group_id"] and c["profileId"] == plan["profile_id"] for c in available):
                raise AgentLoopBlocked("plan.change_budget_exhausted")
            return plan
        tools.append(self._tool("propose_policy", propose, schema, terminal=True))
        resources = self._planner_preload("resource_snapshot", self.observations.resource_snapshot)
        environment_reader = getattr(self.observations, "environment_snapshot", None)
        if not callable(environment_reader):
            environment_reader = lambda: self.observations.read("get_environment_overview")
        environment = self._planner_preload("get_environment_overview", environment_reader)
        result = self._role("plan", {"total_ues": self.contract.total_ues, "groups": self.contract.group_counts,
            "capabilities": self._candidate_view(available), "last_decision": previous[-1] if previous else None,
            "authorized_actions": permitted, "policy_change_budget": budget,
            "active_policy": active_view, "active_policy_scope": "agent-owned-policy-only",
            "lessons_ref": state.get("lessons_ref"),
            "proposal_feedback": self.artifacts.get(state["proposal_feedback_ref"])
                if state.get("proposal_feedback_ref") else None,
            "optimization_objective": {"metric": self.contract.metric, "direction": self.contract.direction,
                "minimum_net_improvement_percent": self.contract.minimum_improvement_percent,
                "maximum_holdout_regression_percent": self.contract.holdout_tolerance_percent,
                "comparison": "same selected UEs and frozen offered load; treatment change minus holdout change",
                "actuator_scope": "commissioned group QER rates within existing slices"},
            **({"service_requirements": before.get("resolvedRequirements"), "sla": before.get("sla")}
               if self.contract.service_requirements_revision else {}),
            "resources": resources, "environment": environment}, tools)
        plan = {**result["result"], "response_id": result["response_id"], "before_ref": state["before_ref"]}
        plan_ref = self.artifacts.put("plan", plan)
        self._audit(kind="decision", role="planner", phase="plan", status="proposed",
                    action=plan["action"], plan_ref=plan_ref, before_ref=state["before_ref"])
        return {"plan_ref": plan_ref, "phase": "precheck"}

    def _precheck(self, state):
        before, plan = self._before(state), self.artifacts.get(state["plan_ref"])
        candidates = commissioned_candidates(before, tuple(self.contract.profiles))
        selected = next((c for c in candidates if c["eventId"] == plan["group_id"] and c["profileId"] == plan["profile_id"]), None)
        decision = {"candidateId": selected["id"] if plan["action"] == "apply" and selected else None}
        def validate():
            rows, seal = self._accepted_before(state)
            chosen = self.validator.proposal(decision, candidates, rows)
            active = self.artifacts.get(state["active_ref"]) if state.get("active_ref") else None
            replace = bool(active and chosen and chosen["id"] != active["selected"]["id"])
            already_installed = bool(active and chosen and chosen["id"] == active["selected"]["id"])
            if chosen is not None and not replace and not already_installed:
                if (state.get("changes", 0) >= self.contract.maximum_policy_changes
                        or state["cycle"] - state.get("last_change", -self.contract.minimum_cycles_between_changes)
                        < self.contract.minimum_cycles_between_changes):
                    raise AgentLoopBlocked("plan.change_budget_exhausted")
                can_apply = getattr(self.observer, "can_start_policy", None)
                if callable(can_apply) and not can_apply():
                    raise AgentLoopBlocked("graph.policy_window_ended")
            fresh_hold = plan["action"] == "hold" and active is None
            proof = {"plan_ref": state["plan_ref"], "before_ref": state["before_ref"], "selected": chosen,
                     "round_id": self.round_id, "generation": self.generation, "cycle": state["cycle"],
                     "replace": replace, "fresh_hold": fresh_hold, "already_installed": already_installed,
                     "qer_before": active["qer_before"] if already_installed else
                         self.validator.qer_inventory(before, sealed_rows=rows) if fresh_hold or chosen and not replace else None}
            if fresh_hold or chosen and not replace and not already_installed:
                proof['hold_seal'] = seal
            return {"validated_ref": self.artifacts.put("validated", proof), "replace": replace, "apply": chosen is not None}
        def freshness():
            rows, seal = self._accepted_before(state)
            return {'baseline_fresh_at_capture': bool(rows), 'validated_at': seal['validated_at'],
                    'scope': 'historical-baseline-with-live-owner-fence'}
        result = self._role("precheck", {"plan": {k: plan[k] for k in ("action", "group_id", "profile_id", "reason")}}, [
            self._tool("inspect_plan", lambda: {k: plan[k] for k in ("action", "group_id", "profile_id", "reason")}, parallel_safe=True),
            self._tool("check_round_scope", lambda: {"selected_ues": len(self._accepted_before(state)[0]), "matches": True}, parallel_safe=True),
            self._tool("check_observation_freshness", freshness, parallel_safe=True),
            self._tool("check_policy_capability", lambda: self._candidate_view(candidates), parallel_safe=True),
            self._tool("read_native_qer", lambda: {"verified_sessions": len(self.validator.qer_inventory(before, sealed_rows=self._accepted_before(state)[0]))}, parallel_safe=True),
            self._tool("check_proposal", validate, terminal=True)])
        proof = result["result"]
        validated = self.artifacts.get(proof["validated_ref"])
        if validated["fresh_hold"] or validated["selected"] is not None:
            proof["validated_ref"] = self.artifacts.put("validated", {
                **validated, "precheck_response_id": result["response_id"]})
        route = "restore_replace" if proof["replace"] else "apply"
        return {"validated_ref": proof["validated_ref"], "route": route, "phase": route}

    def _execute_hold(self, state):
        """Execute an approved no-write decision without changing policy ownership."""
        proof = self.artifacts.get(state["validated_ref"])
        plan, before = self.artifacts.get(state["plan_ref"]), self._before(state)
        if (proof["selected"] is not None
                or proof["plan_ref"] != state["plan_ref"] or proof["before_ref"] != state["before_ref"]
                or plan["action"] == "hold" and (plan["group_id"] is not None or plan["profile_id"] is not None)):
            raise AgentLoopBlocked("graph.no_validated_hold")
        op = "hold:" + _digest([state["thread_id"], state["cycle"], state["validated_ref"]])
        def execute(validated_ref):
            if validated_ref != state["validated_ref"]:
                raise AgentLoopBlocked("graph.validated_reference_denied")
            rows, _ = self._accepted_before(state)
            actual = self.validator.qer_inventory(before, sealed_rows=rows)
            if proof.get("fresh_hold") and actual != proof["qer_before"]:
                raise AgentLoopBlocked("graph.hold_native_changed")
            self._accepted_before(state)
            if state.get("active_ref"):
                owned = self.artifacts.get(state["active_ref"])
                self.validator.installation(owned["receipt"], owned["selected"], owned["qer_before"], actual)
            receipt = {"operation": "hold", "network_write": False, "validated_ref": validated_ref,
                       "round_id": self.round_id, "generation": self.generation, "cycle": state["cycle"]}
            recorded = self.artifacts.freeze(op, receipt)
            if recorded != receipt:
                raise AgentLoopBlocked("graph.hold_decision_changed")
            return {"action_ref": self.artifacts.put("decision_action", recorded)}
        result = self._role("apply", {"validated_ref": state["validated_ref"],
            "operation": "hold"}, [
            self._tool("get_validated_decision", lambda: {"validated_ref": state["validated_ref"], "action": "hold"}),
            self._tool("get_action_status", lambda: self.artifacts.freeze(op) or {"state": "not_started"}),
            self._tool("execute_decision", execute,
                object_schema({"validated_ref": {"type": "string", "maxLength": 256}}), terminal=True)])
        return {"validated_ref": self.artifacts.put("validated", {**proof, "hold_confirmation": {
                "validated_ref": state["validated_ref"], "executor_response_id": result["response_id"]}}),
            "action_ref": result["result"]["action_ref"],
            "phase": "postcheck"}

    def _apply(self, state):
        proof = self.artifacts.get(state["validated_ref"])
        if proof["plan_ref"] != state["plan_ref"] or proof["before_ref"] != state["before_ref"]:
            raise AgentLoopBlocked("graph.validated_plan_changed")
        selected, before = proof["selected"], self._before(state)
        if selected is None:
            return self._execute_hold(state)
        plan = self.artifacts.get(state["plan_ref"])
        if proof.get("already_installed"):
            return self._execute_existing(state, proof, plan)
        op = "apply:" + _digest([state["thread_id"], state["cycle"], state["validated_ref"]])
        schema = object_schema({"validated_ref": {"type": "string", "maxLength": 256}})
        def apply(validated_ref):
            if validated_ref != state["validated_ref"]:
                raise AgentLoopBlocked("graph.validated_reference_denied")
            rows, _ = self._accepted_before(state)
            prior = self.artifacts.operation(op)
            if prior is not None:
                if prior["state"] == "completed":
                    receipt = prior["receipt"]
                else:
                    records = getattr(self.executor.adapter, "_records", None)
                    matches = [r for r in records(before["roundId"]) if r.get("modelResponseId") == plan["response_id"]
                               and r.get("candidateId") == selected["id"]] if callable(records) else []
                    if len(matches) != 1 or matches[0].get("state") != "installed" or matches[0].get("uncertain"):
                        raise AgentLoopBlocked("graph.uncertain_operation_requires_reconciliation")
                    receipt = self.executor.adapter.view(matches[0])
                self.validator.installation(receipt, selected, proof["qer_before"], self.validator.qer_inventory(before, sealed_rows=rows))
            else:
                can_apply = getattr(self.observer, "can_start_policy", None)
                if callable(can_apply) and not can_apply():
                    raise AgentLoopBlocked("graph.policy_window_ended")
                self.artifacts.operation(op, {"state": "intent", "plan_ref": state["plan_ref"]})
                receipt = self.executor.apply(before, selected, model_response_id=plan["response_id"])
            self.artifacts.operation(op, {"state": "completed", "receipt": receipt})
            return {"action_ref": self.artifacts.put("action", {"receipt": receipt, "selected": selected,
                "qer_before": proof["qer_before"], "before_ref": state["before_ref"],
                "before_seal": proof.get("hold_seal"), "validated_ref": state["validated_ref"]})}
        result = self._role("apply", {"validated_ref": state["validated_ref"], "operation": "apply"}, [
            self._tool("get_validated_decision", lambda: {"validated_ref": state["validated_ref"], "group_id": plan["group_id"], "profile_id": plan["profile_id"]}),
            self._tool("get_action_status", lambda: {"state": (self.artifacts.operation(op) or {}).get("state", "not_started")}),
            self._tool("execute_decision", apply, schema, terminal=True, writes=True)])
        executed = self.artifacts.get(result["result"]["action_ref"])
        action_ref = self.artifacts.put("action", {**executed, "executor_response_id": result["response_id"]})
        self._audit(kind="decision", role="executor", phase="apply", status="installed",
                    action="apply", plan_ref=state["plan_ref"], action_ref=action_ref,
                    before_ref=state["before_ref"])
        return {"action_ref": action_ref, "changes": state.get("changes", 0) + 1,
                "last_change": state["cycle"], "phase": "postcheck"}

    def _execute_existing(self, state, proof, plan):
        """An idempotent apply stays an apply; only the Planner can choose hold."""
        ref = state.get("active_ref")
        if not ref:
            raise AgentLoopBlocked("graph.owned_action_missing")
        owned = self.artifacts.get(ref)
        if owned["selected"]["id"] != proof["selected"]["id"]:
            raise AgentLoopBlocked("graph.validated_plan_changed")
        def execute(validated_ref):
            if validated_ref != state["validated_ref"]:
                raise AgentLoopBlocked("graph.validated_reference_denied")
            rows, _ = self._accepted_before(state)
            self.validator.installation(owned["receipt"], owned["selected"], owned["qer_before"],
                self.validator.qer_inventory(self._before(state), sealed_rows=rows))
            return {"action_ref": ref}
        self._role("apply", {"validated_ref": state["validated_ref"], "operation": "apply",
            "already_installed": True}, [
            self._tool("get_validated_decision", lambda: {"validated_ref": state["validated_ref"],
                "action": "apply", "group_id": plan["group_id"], "profile_id": plan["profile_id"]}),
            self._tool("get_action_status", lambda: {"state": "installed", "action_ref": ref}),
            self._tool("execute_decision", execute,
                object_schema({"validated_ref": {"type": "string", "maxLength": 256}}), terminal=True)])
        self._audit(kind="decision", role="executor", phase="apply", status="installed",
            action="apply", plan_ref=state["plan_ref"], action_ref=ref, before_ref=state["before_ref"])
        return {"action_ref": ref, "phase": "postcheck"}

    def _sealed_before_rows(self, before, before_ref, seal):
        # The content-addressed before_ref binds the complete accepted measurement;
        # this seal records when its rows passed the real decision-time guards.
        if (not isinstance(seal, dict) or seal.get("schema") != "cityverse.validated-traffic-before/v1"
                or before_ref != "observation:" + _digest(before)
                or seal.get("before_ref") != before_ref or seal.get("round_id") != before["roundId"]
                or type(seal.get("generation")) is not int or seal["generation"] != before["generation"]
                or type(seal.get("sequence")) is not int or seal["sequence"] != before["sequence"]
                or seal.get("rows_digest") != _digest(before["perUe"])
                or type(seal.get("validated_at")) not in (int, float)
                or not -1 <= seal["validated_at"] - _seconds(before["observedAt"]) <= self.contract.maximum_evidence_age_seconds):
            raise AgentLoopBlocked("graph.hold_before_seal_invalid")
        return {row["supi"]: row for row in before["perUe"]}

    def _policy_action_ref(self, state):
        ref = state.get("action_ref")
        if ref and self.artifacts.get(ref).get("operation") != "hold":
            return ref
        return state.get("active_ref") or ""

    def _postcheck(self, state):
        window_key = "result-window:" + str(state["cycle"])
        window = self.artifacts.freeze(window_key)
        if self._scenario_active() is False and window is None:
            raise _ScenarioEnded()
        before = self._before(state)
        action_ref = self._policy_action_ref(state)
        installed = self.artifacts.get(action_ref) if action_ref else None
        validated = self.artifacts.get(state["validated_ref"])
        fresh_hold = validated.get("fresh_hold") is True
        hold_rows = None
        if fresh_hold:
            hold_rows = self._sealed_before_rows(before, state["before_ref"], validated.get("hold_seal"))
        result_holder = {"after": self.artifacts.get(window["after_ref"])} if window else {}
        def current_qer():
            after = result_holder.get('after')
            if after is not None:
                return self.validator.qer_inventory(after)
            if fresh_hold:
                # A delayed hold validates the captured traffic seal while
                # independently reading fresh QERs under the unchanged owner.
                self._guard_scope(before)
                return self.validator.qer_inventory(before, sealed_rows=hold_rows)
            if not installed:
                return self.validator.qer_inventory(before)
            # Installation baseline is immutable effect evidence, not current
            # execution authority. A later cycle has a newer accepted sequence.
            rows, _ = self._accepted_before(state)
            return self.validator.qer_inventory(before, sealed_rows=rows)
        def installation():
            if installed:
                self.validator.installation(installed["receipt"], installed["selected"], installed["qer_before"],
                                            current_qer())
            return {"installed": bool(installed), "policy_action": bool(installed)}
        def measure():
            if "after" not in result_holder:
                after = self.observer.read()
                if after.get("scenarioActive") is not True:
                    if installed or fresh_hold:
                        raise AgentLoopBlocked("graph.result_window_unavailable")
                    after = None
                if after:
                    self.validator.observe(after)
                    self.current = after
                    saved = self.artifacts.freeze(window_key, {"after_ref": self.artifacts.put("observation", after)})
                    after = self.artifacts.get(saved["after_ref"])
                result_holder["after"] = after
            return {"available": result_holder["after"] is not None, "scope": "selected-round-ues"}
        def compare():
            measure()
            after = result_holder["after"]
            if not installed:
                if fresh_hold:
                    if not validated.get("hold_confirmation") or after is None:
                        raise AgentLoopBlocked("graph.hold_confirmation_unavailable")
                    self._guard(after)
                    first, last = hold_rows, self.validator.observe(after)
                    if ((before["roundId"], before["generation"], before.get("policyRevision"))
                            != (after["roundId"], after["generation"], after.get("policyRevision"))
                            or after["sequence"] <= before["sequence"] or set(first) != set(last)
                            or any(any(first[s].get(k) != last[s].get(k) for k in
                                ("eventId", "slice", "requestedSlice", "offeredUplinkMbps", "offeredDownlinkMbps"))
                                for s in first)):
                        raise AgentLoopBlocked("graph.hold_result_scope_changed")
                    actual = self.validator.qer_inventory(after)
                    if actual != validated["qer_before"]:
                        raise AgentLoopBlocked("graph.hold_native_changed")
                    self._guard(after)
                    return {"outcome": "no_op", "improvement_percent": None,
                        "result_available": True, "metric": self.contract.metric,
                        "hold_verified": True, "native_digest": _digest(actual)}
                return {"outcome": "no_op", "improvement_percent": None,
                        "result_available": after is not None, "metric": self.contract.metric}
            self.validator.installation(installed["receipt"], installed["selected"], installed["qer_before"],
                                        self.validator.qer_inventory(after))
            policy_before = self.artifacts.get(installed["before_ref"])
            seal = installed.get("before_seal")
            if seal is not None:
                baseline = self._sealed_before_rows(policy_before, installed["before_ref"], seal)
                outcome, improvement = self.validator._effect_rows(policy_before, after, installed["selected"], baseline)
            else:
                outcome, improvement = self.validator.effect(policy_before, after, installed["selected"])
            return {"outcome": outcome, "improvement_percent": improvement, "metric": self.contract.metric,
                    "treatment_count": len(installed["selected"]["treatmentSupis"]),
                    "holdout_count": len(installed["selected"]["holdoutSupis"]),
                    "baseline_sequence": policy_before["sequence"], "result_sequence": after["sequence"]}
        def verify():
            installation()
            comparison = compare()
            after = result_holder["after"]
            return {"outcome": comparison["outcome"], "improvement_percent": comparison["improvement_percent"],
                    "after_ref": self.artifacts.put("observation", after) if after else "",
                    "restore": installed is not None and comparison["outcome"] != "effect_verified",
                    **({"hold_verified": True, "native_digest": comparison["native_digest"]}
                       if comparison.get("hold_verified") else {})}
        result = self._role("postcheck", {"installation_to_check": bool(installed), "metric": self.contract.metric}, [
            self._tool("read_native_qer", lambda: {"verified_sessions": len(current_qer())}),
            self._tool("check_execution", installation), self._tool("measure_result_window", measure, writes=True),
            self._tool("compare_traffic_windows", compare, writes=True),
            self._tool("check_effect", verify, terminal=True)],
            finish_accepted=lambda: result_holder.get("after") is not None)
        verdict = result["result"]
        if verdict.get("hold_verified"):
            verdict = {**verdict, "validator_response_id": result["response_id"]}
        return {"verification_ref": self.artifacts.put("verdict", verdict), "after_ref": verdict["after_ref"],
                "active_ref": action_ref if installed else "", "route": "restore" if verdict["restore"] else "learn",
                "phase": "restore" if verdict["restore"] else "learn"}

    def _restore(self, state):
        before = self._before(state)
        ref = self._policy_action_ref(state)
        if not ref:
            raise AgentLoopBlocked("graph.owned_action_missing")
        def action_status():
            action = self.artifacts.get(ref)
            records = getattr(self.executor.adapter, "_records", None)
            if not callable(records):
                return {"owned_action_ref": ref, "source": "graph-receipt", "state": action["receipt"].get("state"),
                        "native_journal_available": False}
            matching = [r for r in records(before["roundId"]) if r.get("id") == action["receipt"].get("id")]
            if len(matching) != 1:
                raise AgentLoopBlocked("graph.owned_action_missing")
            return {"owned_action_ref": ref, "source": "adapter-journal", "state": matching[0]["state"],
                    "native_journal_available": True, "uncertain": matching[0].get("uncertain", False)}
        result = self._role("restore", {"owned_action_ref": ref}, [
            self._tool("get_action_status", action_status),
            self._tool("restore_owned_policy", lambda: self._restore_owned(before["roundId"]), terminal=True, writes=True)])
        return {"restoration_ref": self.artifacts.put("restoration", {
            "owned_action_ref": ref, "restored": True, "executor_response_id": result["response_id"]}),
            "phase": "restoration"}

    def _restore_owned(self, round_id):
        self.executor.restore(round_id)
        return {"cleanup_verified": True}

    def _restoration(self, state):
        ref = self._policy_action_ref(state)
        action = self.artifacts.get(ref)
        def verify():
            # Independent native read; this role has no network write capability.
            actual = self.validator.qer_inventory(self._before(state))
            if actual != action["qer_before"]:
                raise AgentLoopBlocked("graph.native_restoration_differs")
            return {"restored": True}
        self._role("restoration", {"owned_action_ref": ref}, [
            self._tool("read_native_qer", lambda: {"verified_sessions": len(self.validator.qer_inventory(self._before(state)))}),
            self._tool("check_restoration", verify, terminal=True)])
        self._audit(kind="decision", role="validator", phase="restoration", status="restored",
                    action="restore", action_ref=ref, plan_ref=state["plan_ref"])
        patch = {"active_ref": "", "phase": "learn"}
        if not state.get("verification_ref"):
            patch["verification_ref"] = self.artifacts.put("verdict", {"outcome": "no_op", "improvement_percent": None,
                "after_ref": "", "restore": False, "reason": "replan_after_restoration"})
        return patch

    def _learn(self, state):
        plan, verdict, before = self.artifacts.get(state["plan_ref"]), self.artifacts.get(state["verification_ref"]), self._before(state)
        checked_ref = self._policy_action_ref(state)
        checked = self.artifacts.get(checked_ref) if checked_ref else None
        candidate_id = checked["selected"]["id"] if checked else None
        outcome = {"iteration": state["cycle"], "candidateId": candidate_id,
                   "outcome": verdict["outcome"], "improvementPercent": verdict["improvement_percent"], "responseId": plan["response_id"]}
        if checked_ref:
            outcome["actionRef"] = checked_ref
        if verdict.get("hold_verified"):
            outcome.update(verificationKind="policy_maintained", planRef=state["plan_ref"],
                validatedRef=state["validated_ref"], verificationRef=state["verification_ref"],
                beforeRef=state["before_ref"], afterRef=verdict["after_ref"])
        learned = self.artifacts.get(verdict["after_ref"]) if self.contract.service_requirements_revision and verdict.get("after_ref") else before
        def record():
            self.memory.record(self.contract, state["cycle"], learned, candidate_id, verdict["outcome"],
                               verdict["improvement_percent"], plan["response_id"], now=self.clock())
            self.artifacts.outcome(state["cycle"], outcome)
            return {"lesson_ref": self.artifacts.put("lesson_receipt", {
                "contract_digest": self.contract.digest, "observation_digest": _digest({
                    key: learned[key] for key in ("roundId", "generation", "sequence", "observedAt")}),
                "memory_context_key": state["memory_context_key"], **outcome})}
        recorded = self._role("learn", {"cycle": state["cycle"], "outcome": verdict["outcome"],
            "candidate_id": candidate_id, "improvement_percent": verdict["improvement_percent"]}, [
            self._tool("record_lesson", record, terminal=True, writes=True)])
        lesson_ref = recorded["result"]["lesson_ref"]
        # Keep the authoritative outcome byte-compatible with pre-observability
        # checkpoints. Display-only references live in a separate artifact so a
        # crash after outcome() can replay with the original strict equality.
        display = {**outcome, "cycle": state["cycle"], "action": plan["action"],
                   "planRef": state["plan_ref"], "validatedRef": state["validated_ref"],
                   "verificationRef": state["verification_ref"],
                   "beforeRef": checked["before_ref"] if checked else state["before_ref"],
                   "afterRef": verdict.get("after_ref", "")}
        display_ref = self.artifacts.put("cycle_observability", display)
        self._audit(kind="decision", role="validator", phase="learn", status=verdict["outcome"],
                    action=plan["action"], plan_ref=state["plan_ref"], before_ref=display["beforeRef"],
                    after_ref=display["afterRef"], verification_ref=state["verification_ref"], display_ref=display_ref,
                    **({"action_ref": checked_ref} if checked_ref else {}))
        if state["cycle"] + 1 >= self.maximum_cycles:
            return {"lesson_ref": lesson_ref, "cycle": state["cycle"] + 1, "phase": "cleanup", "route": "cleanup",
                    "stop_reason": "cycle_limit_reached"}
        self.sleep(self.interval_seconds)
        return {"lesson_ref": lesson_ref, "cycle": state["cycle"] + 1, "phase": "observe"}

    def _cleanup(self, state):
        retaining = (self.retain_on_completion and state.get('stop_reason') == 'cycle_limit_reached'
                     and not state.get('error_code'))
        # Deterministic: loss of an LLM must never suppress owned restoration.
        if not retaining:
            self.executor.restore(self.round_id)
        outcomes = self.artifacts.outcomes()
        if outcomes and all(o.get("verificationKind") == "policy_maintained" for o in outcomes):
            verified_ref = outcomes[-1]["afterRef"]
            latest_ref = state.get("after_ref") or verified_ref
            last = self.artifacts.get(latest_ref)
            verified = self.artifacts.get(verified_ref)
            if last["sequence"] < verified["sequence"]:
                raise AgentLoopBlocked("graph.hold_cleanup_scope_changed")
            validated = self.artifacts.get(outcomes[0]["validatedRef"])
            def owner_guard():
                if self.current_reader:
                    expected = last.get("_actuatorSnapshot", last)
                    live = self.current_reader()
                    if any(live.get(k) != expected.get(k) for k in
                           ("roundId", "generation", "sequence", "policyRevision", "perUe", "fleetLease")):
                        raise AgentLoopBlocked("graph.native_fence_changed")
            owner_guard()
            # Cleanup proves fresh native identities/QERs; it does not pretend
            # that a historical traffic observation is fresh again.
            proof = self.validator.qer_reader(last)
            if (proof.get("source") != "native-upf-qer"
                    or not -1 <= self.validator.clock() - _seconds(proof["observedAt"]) <= self.contract.maximum_evidence_age_seconds
                    or not isinstance(proof.get("sessions"), list) or len(proof["sessions"]) != self.contract.total_ues):
                raise AgentLoopBlocked("graph.hold_cleanup_native_unavailable")
            actual = {row["supi"]: row for row in proof["sessions"]}
            if actual != validated["qer_before"]:
                raise AgentLoopBlocked("graph.hold_native_changed")
            owner_guard()
            self.artifacts.put("hold_cleanup", {"after_ref": latest_ref, "verified_after_ref": verified_ref,
                "native_digest": _digest(actual), "cleanup_verified": True})
        if retaining:
            # A no-write hold still requires its final independent QER read.
            # Installed policies stay owned by the coordinator until stop.
            self.policy_retained = True
            return {'phase': 'completed', 'route': 'completed'}
        self.cleanup_verified = True
        return {"phase": "completed", "route": "completed"}

    @staticmethod
    def _state_patch(patch):
        """Publish canonical references while keeping existing evidence consumers compatible."""
        patch = dict(patch)
        for canonical, legacy in (("proposal_ref", "plan_ref"), ("verdict_ref", "verification_ref"),
                                  ("active_action_ref", "active_ref"), ("changes_used", "changes"),
                                  ("last_change_cycle", "last_change")):
            if canonical in patch and legacy in patch and patch[canonical] != patch[legacy]:
                raise AgentLoopBlocked("graph.state_alias_changed")
            if canonical in patch:
                patch[legacy] = patch[canonical]
            elif legacy in patch:
                patch[canonical] = patch[legacy]
        return patch

    def _planner(self, state):
        """Observation and frozen memory are part of the planning node."""
        state = self._state_patch(state)
        key = "cycle-inputs:" + str(state["cycle"])
        inputs = self.artifacts.freeze(key)
        if inputs is None:
            observed = self._observe(state)
            if observed.get("phase") == "cleanup":
                return {**observed, "route": "end"}
            inputs = self.artifacts.freeze(key, {**observed,
                "memory_context_key": self.memory.context_key(self.contract),
                "lessons_ref": self.artifacts.put("lessons", self.memory.search(self.contract))})
        state = {**state, **inputs}
        self.current = self._before(state)
        if self.observations is None:
            self.observations = PlannerObservations(lambda: self.current, clock=self.clock)
        self.observations.record(self.current)
        self._accepted_before(state)
        proposal_key = f"proposal:{state['cycle']}:{state.get('proposal_attempt', 0)}"
        proposal = self.artifacts.freeze(proposal_key)
        if proposal is None:
            proposal = self.artifacts.freeze(proposal_key, self._plan(state))
        return self._state_patch({**inputs, **proposal, "validation_phase": "precheck",
            "operation": "", "route": "validator", "restore_required": False,
            "restore_request_ref": "", "restoration_ref": "", "lesson_ref": ""})

    def _restore_request(self, state, reason):
        ref = self._policy_action_ref(state)
        if not ref:
            raise AgentLoopBlocked("graph.owned_action_missing")
        return self.artifacts.put("restore_request", {"round_id": self.round_id,
            "generation": self.generation, "cycle": state["cycle"], "owned_action_ref": ref,
            "proposal_ref": state["plan_ref"], "reason": reason})

    def _executor_node(self, state):
        state = self._state_patch(state)
        operation = state.get("operation")
        if operation not in {"apply", "hold", "restore"}:
            raise AgentLoopBlocked("graph.executor_operation_denied")
        if operation == "restore":
            request = self.artifacts.get(state["restore_request_ref"])
            if (request.get("round_id"), request.get("generation"), request.get("cycle"),
                    request.get("owned_action_ref"), request.get("proposal_ref")) != (
                    self.round_id, self.generation, state["cycle"], self._policy_action_ref(state), state["plan_ref"]):
                raise AgentLoopBlocked("graph.restore_request_changed")
        else:
            proof = self.artifacts.get(state["validated_ref"])
            if (proof.get("round_id"), proof.get("generation"), proof.get("cycle")) != (
                    self.round_id, self.generation, state["cycle"]):
                raise AgentLoopBlocked("graph.validated_decision_scope_changed")
            if operation != ("apply" if proof.get("selected") is not None else "hold"):
                raise AgentLoopBlocked("graph.executor_operation_denied")
        binding = state["restore_request_ref"] if operation == "restore" else state["validated_ref"]
        key = f"execution:{state['cycle']}:{state.get('proposal_attempt', 0)}:{operation}:" + _digest(binding)
        patch = self.artifacts.freeze(key)
        if patch is None:
            patch = self._restore(state) if operation == "restore" else self._apply(state)
            if operation == "apply":
                patch["active_ref"] = patch["action_ref"]
            patch = self.artifacts.freeze(key, patch)
        return self._state_patch({**patch, "route": "validator",
            "validation_phase": "restore_check" if operation == "restore" else "postcheck"})

    def _validator_node(self, state):
        state = self._state_patch(state)
        phase = state.get("validation_phase")
        if phase == "precheck":
            key = f"precheck:{state['cycle']}:{state.get('proposal_attempt', 0)}"
            patch = self.artifacts.freeze(key)
            if patch is None:
                patch = self.artifacts.freeze(key, self._precheck(state))
            checked = {**state, **patch}
            if patch["route"] == "restore_replace":
                return self._state_patch({**patch, "operation": "restore", "restore_required": True,
                    "restore_request_ref": self._restore_request(checked, "replace_policy"), "route": "executor"})
            selected = self.artifacts.get(patch["validated_ref"])["selected"]
            return self._state_patch({**patch, "operation": "apply" if selected is not None else "hold",
                "route": "executor"})
        if phase == "postcheck":
            key = "postcheck:" + str(state["cycle"])
            patch = self.artifacts.freeze(key)
            if patch is None:
                patch = self.artifacts.freeze(key, self._postcheck(state))
            state = {**state, **patch}
            if patch["route"] == "restore":
                return self._state_patch({**patch, "operation": "restore", "restore_required": True,
                    "restore_request_ref": self._restore_request(state, "effect_not_verified"), "route": "executor"})
        elif phase == "restore_check":
            key = "restore-check:" + str(state["cycle"])
            patch = self.artifacts.freeze(key)
            if patch is None:
                patch = self.artifacts.freeze(key, self._restoration(state))
            state = {**state, **patch}
        else:
            raise AgentLoopBlocked("graph.validation_phase_denied")
        lesson_key = "lesson:" + str(state["cycle"])
        learned = self.artifacts.freeze(lesson_key)
        if learned is None:
            learned = self.artifacts.freeze(lesson_key, self._learn(state))
        return self._state_patch({**patch, **learned, "restore_required": False,
            "route": "end" if learned.get("phase") == "cleanup" else "planner",
            "proposal_attempt": 0, "proposal_feedback_ref": ""})

    def _node(self, name, method):
        def node(state):
            self.cycle = state.get("cycle", 0)
            try:
                return method(state)
            except _ScenarioEnded:
                patch = {"route": "end", "phase": "cleanup", "stop_reason": "scenario_ended"}
                if self.current is not None:
                    patch["after_ref"] = self.artifacts.put("observation", self.current)
                return patch
            except Exception as exc:
                # Local fixed error codes only, never transport exception text.
                code = str(exc) if type(exc) is AgentLoopBlocked and len(str(exc)) < 160 and str(exc).startswith(("graph.", "tool.", "model.", "plan.", "observation.")) else "graph.stage_failed"
                audit_phase = ("plan" if name == "planner" else
                    "restore" if name == "executor" and state.get("operation") == "restore" else
                    "apply" if name == "executor" else
                    {"restore_check": "restoration"}.get(state.get("validation_phase"), state.get("validation_phase", "precheck")))
                self._audit(phase=audit_phase, role=name, status="failed", code=code)
                retryable = code in {"plan.unsupported_capability", "plan.hold_has_policy_arguments"}
                if retryable and name in {"planner", "validator"} and state.get("proposal_attempt", 0) < 1:
                    return {"route": "planner", "phase": "plan", "proposal_attempt": 1,
                        "proposal_feedback_ref": self.artifacts.put("proposal_feedback", {"code": code})}
                return {"error_code": code, "route": "end", "phase": "cleanup"}
        return node

    def build(self, saver):
        builder = StateGraph(AgentGraphState)
        methods = {"planner": self._planner, "executor": self._executor_node, "validator": self._validator_node}
        for name, method in methods.items():
            builder.add_node(name, self._node(name, method))
        builder.add_edge(START, "planner")
        for name in methods:
            builder.add_conditional_edges(name, lambda s: s["route"],
                {"planner": "planner", "executor": "executor", "validator": "validator", "end": END})
        return builder.compile(checkpointer=saver, name="cityverse-policy-agents")

    def run(self, *, round_id, generation):
        if round_id != self.contract.scenario_id or type(generation) is not int or generation < 0:
            raise AgentLoopBlocked("graph.round_contract_mismatch")
        self.round_id, self.generation = round_id, generation
        self.cleanup_verified = False
        self._known_targets = []
        self.policy_retained = False
        captured = getattr(getattr(self, "observations", None), "log_evidence", None)
        if isinstance(captured, dict):
            captured.clear()
        thread_id = f"{round_id}:{generation}"
        self.artifacts = GraphArtifacts(self.artifacts_path, thread_id)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": self.maximum_cycles * 12 + 20}
        connection = sqlite3.connect(self.checkpoint_path, check_same_thread=False)
        self.checkpoint_path.chmod(0o600)
        try:
            identity = {"round_id": round_id, "generation": generation,
                        "execution_digest": self.execution_digest}
            if self.artifacts.freeze("execution-context", identity) != identity:
                raise AgentLoopBlocked("graph.checkpoint_contract_changed")
            saver = SqliteSaver(connection)
            graph = self.build(saver)
            saved = graph.get_state(config)
            if saved.values and saved.values.get("contract_digest") != self.execution_digest:
                raise AgentLoopBlocked("graph.checkpoint_contract_changed")
            if (self.retain_on_completion and saved.values and not saved.next
                    and saved.values.get('stop_reason') == 'cycle_limit_reached'):
                # Restart recovery belongs to the coordinator. An old terminal
                # checkpoint cannot authorize retaining a current native policy.
                raise AgentLoopBlocked('graph.completed_checkpoint_not_resumable')
            initial = self._state_patch({"thread_id": thread_id, "round_id": round_id, "generation": generation,
                       "contract_digest": self.execution_digest,
                       "cycle": 0, "changes": 0, "last_change": -self.contract.minimum_cycles_between_changes,
                       "active_ref": "", "error_code": "", "phase": "plan", "proposal_attempt": 0,
                       "validation_phase": "", "operation": "", "restore_required": False})
            # No prompts/evidence are exported through ambient tracing settings.
            with tracing_context(enabled=False):
                if saved.values and not saved.next:
                    final = saved.values
                else:
                    final = graph.invoke(None if saved.values else initial, config)
            cleanup = self._cleanup(final)
            # Cleanup is outside the Agent nodes; checkpoint readers still need
            # its verified terminal state and ownership release.
            completed = {**cleanup, "route": "end", "restore_required": False}
            if not self.policy_retained:
                completed.update(active_ref="", active_action_ref="")
            graph.update_state(config, completed, as_node="validator")
            if final.get("error_code"):
                raise AgentLoopBlocked(final["error_code"])
            return self.artifacts.outcomes()
        finally:
            try:
                if not self.cleanup_verified and not self.policy_retained:
                    self.executor.restore(round_id)
            finally:
                connection.close()


class FormalIntentGraph:
    """The formal three-role graph over IntentService's existing operation journal.

    Ports execute explicitly in their owning node. Checkpoints contain references;
    recovery and uncertain native writes remain owned by the operation journal.
    The legacy diagnostic graph above is never used as an all-member controller.
    """
    def __init__(self, service):
        self.service = service

    async def run(self, context):
        import asyncio
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self._run_sync, context, loop)

    def _run_sync(self, context, event_loop):
        import asyncio
        import json
        from pathlib import Path
        from langgraph.checkpoint.sqlite import SqliteSaver
        from backend.onprem.agents.persistence import GraphArtifacts
        from backend.onprem.agents.roles import RoleAgent, StageTool, object_schema
        service = self.service
        operation_id, owner = context['op_id'], context['owner']
        path = Path(service.repository.path).resolve().with_suffix('.formal-agents.sqlite3')
        artifacts = GraphArtifacts(path, operation_id)
        def call(function, *args, **kwargs):
            return asyncio.run_coroutine_threadsafe(function(*args, **kwargs), event_loop).result()
        def journal(**value):
            return call(service._record, operation_id, **value)
        model = getattr(getattr(service.planner, 'provider', None), 'model', None)
        models = getattr(service, 'role_models', None) or {r: model for r in ('planner', 'executor', 'validator')}
        def port(role, phase, name, method, *, writes=False):
            # Persist the role boundary before crossing into an existing native
            # operation port. A model cannot supply a different operation/plan.
            journal(graph_role=role, graph_phase=phase, graph_nodes=['planner', 'executor', 'validator'])
            started = False
            def handler():
                nonlocal started
                started = True
                call(method, context)
                return {'operation_id': operation_id, 'phase': phase}
            tool = StageTool(name, 'Run the exact bound service operation phase', object_schema(),
                             handler, terminal=True, writes=writes)
            selected = models.get(role)
            if callable(getattr(selected, 'complete', None)):
                try:
                    result = RoleAgent(role, selected, audit=artifacts.audit,
                        knowledge='Use the single allowed tool for the exact bound formal service operation. '
                        'Never invent receipts, change authorization or bypass a failed tool. '
                        'Installation, routing and receiver SLA are distinct checks.').run(
                        phase, {'operation_id': operation_id, 'intent_id': context['intent_id']}, [tool])
                except Exception:
                    if phase == 'restore' and not started:
                        # Model availability cannot remove the journal's existing
                        # cleanup obligation. This exact owned compensation is
                        # deterministic and still enters only the Executor port.
                        result = handler()
                        artifacts.audit(role=role, phase=phase, tool=name, status='completed', kind='deterministic_recovery')
                        return result
                    raise
                context.setdefault('role_response_ids', {})[phase] = result.get('response_id')
                return result
            # Custom deterministic planners are supported for offline adapters;
            # this path is explicitly labelled and never emits model provenance.
            try:
                result = handler()
            except Exception:
                artifacts.audit(role=role, phase=phase, tool=name, status='failed', kind='deterministic_port')
                raise
            artifacts.audit(role=role, phase=phase, tool=name, status='completed', kind='deterministic_port')
            return result
        snapshot = context['snapshot']
        intent = context['intent']
        spatial = getattr(snapshot, 'spatial_context', {})
        stable_demands = [{k: d.get(k) for k in ('group_id', 'parent_group_id', 'serving_cell_id',
            'weight', 'requested_ue_count', 'requested_ul_mbps', 'requested_dl_mbps',
            'upf_workload_id', 'application_workload_id')} for d in spatial.get('demands', ())]
        topology = spatial.get('topology', {})
        memory_key = _digest({'owner': _digest(owner), 'objectives': intent.to_dict().get('objectives'),
            'budget': intent.resource_budget_id, 'policy': [intent.priority_policy_id, intent.degradation_policy_id],
            'workloads': spatial.get('workloads', ()), 'demands': stable_demands,
            'requested_groups': spatial.get('spatial_demand', {}).get('requestedGroups', ()),
            'topology': {'revision': topology.get('revision'), 'sites': topology.get('sites'), 'cells': topology.get('cells')},
            'constraints': context['operation'].get('control_options', {}), 'mode': 'formal-all-members'})
        with artifacts.db() as db:
            lessons = [json.loads(r[0]) for r in db.execute(
                "SELECT value FROM graph_outcomes WHERE json_extract(value,'$.memory_context_key')=? ORDER BY rowid DESC LIMIT 5",
                (memory_key,))]
        context['lessons'] = artifacts.freeze('lessons', lessons)
        scope = {'operation_id': operation_id, 'intent_id': intent.intent_id,
            'intent_revision': intent.revision, 'contract_digest': _digest(intent.to_dict()), 'mode': 'formal-all-members'}
        if artifacts.freeze('scope', scope) != scope:
            raise ValueError('formal_graph_scope_changed')
        def planner(state):
            journal(graph_role='planner', graph_phase='plan', graph_nodes=['planner', 'executor', 'validator'])
            try:
                call(service._formal_plan, context)
            except Exception as error:
                context['error'] = error
                from backend.onprem.intents.model_planner import MeasurementRequired
                if isinstance(error, MeasurementRequired):
                    return {'validation_phase': 'needs_measurement', 'route': 'validator'}
                return {'operation': 'restore', 'route': 'executor'}
            candidates = context.get('candidates')
            plan = context.get('plan')
            if candidates is None and plan is None:
                return {'route': 'end'}
            proposals = [p.to_dict() for p in candidates] if candidates is not None else [plan.to_dict()]
            for candidate in (candidates if candidates is not None else (plan,)):
                if candidate.model_response_id:
                    artifacts.audit(role='planner', phase='plan', tool='propose_resource_plans', status='completed',
                        response_id=candidate.model_response_id, model=candidate.model_name, kind='tool')
            return {'before_ref': artifacts.put('formal_observation', context['snapshot'].to_dict()),
                'proposal_ref': artifacts.put('formal_proposals', proposals),
                'lessons_ref': artifacts.put('formal_lessons', context['lessons']),
                'validation_phase': 'precheck', 'route': 'validator'}
        def executor(state):
            if state.get('operation') == 'restore':
                port('executor', 'restore', 'restore_owned_resources', service._formal_failure, writes=True)
                return {'validation_phase': 'restore_check', 'route': 'validator'}
            try:
                port('executor', 'apply', 'execute_decision', service._formal_execute, writes=True)
            except Exception as error:
                context['error'] = error
                return {'operation': 'restore', 'validation_phase': 'execution_failed', 'route': 'validator'}
            return {'action_ref': artifacts.put('formal_receipts', context['receipts']),
                    'validation_phase': 'postcheck', 'route': 'validator'}
        def validator(state):
            if state.get('validation_phase') == 'needs_measurement':
                # This typed branch persists a missing-evidence request only;
                # it cannot cancel admission, compensate or dispatch resources.
                journal(graph_role='validator', graph_phase='precheck')
                call(service._formal_failure, context)
                return {'verdict_ref': artifacts.put('formal_measurement_request',
                    {'status': 'needs_measurement', 'service_effect_verified': False}), 'route': 'end'}
            if state.get('validation_phase') == 'execution_failed':
                return {'operation': 'restore', 'route': 'executor'}
            if state.get('validation_phase') == 'restore_check':
                port('validator', 'restoration', 'check_restoration', service._formal_restore_check)
                lesson = {'memory_context_key': memory_key, 'operation_id': operation_id,
                    'outcome': 'failed', 'service_effect_verified': False,
                    'restoration_verified': context.get('restoration_verified') is True,
                    'mode': 'formal-all-members'}
                artifacts.outcome(0, artifacts.freeze('lesson', lesson))
                journal(graph_lesson_ref=artifacts.put('formal_lesson', lesson))
                return {'restoration_ref': artifacts.put('formal_restoration', {'verified': lesson['restoration_verified']}),
                        'route': 'end'}
            if state['validation_phase'] == 'precheck':
                try:
                    port('validator', 'precheck', 'check_proposal', service._formal_precheck)
                except Exception as error:
                    context['error'] = error
                    from backend.onprem.intents.model_planner import MeasurementRequired
                    if isinstance(error, MeasurementRequired):
                        call(service._formal_failure, context)
                        return {'verdict_ref': artifacts.put('formal_measurement_request',
                            {'status': 'needs_measurement', 'service_effect_verified': False}), 'route': 'end'}
                    return {'operation': 'restore', 'route': 'executor'}
                return {'validated_ref': artifacts.put('formal_validated_plan', context['plan'].to_dict()), 'route': 'executor'}
            try:
                port('validator', 'postcheck', 'check_execution_and_service_effect', service._formal_postcheck)
            except Exception as error:
                context['error'] = error
                return {'operation': 'restore', 'route': 'executor'}
            effect = context.get('effect', {})
            lesson = {'memory_context_key': memory_key, 'operation_id': operation_id,
                'outcome': 'effect_verified', 'service_effect_verified': effect.get('met') is True,
                'protected_services_met': effect.get('protected_services_met') is True,
                'action_kinds': sorted({a.kind for a in context['plan'].actions}),
                'planner_response_id': context['plan'].model_response_id,
                'role_response_ids': context.get('role_response_ids', {}),
                'plan_digest': _digest(context['plan'].to_dict()), 'mode': 'formal-all-members'}
            lesson['effect_verdict'] = {k: ('passed' if effect.get(k) is True else 'failed' if effect.get(k) is False else 'unknown')
                for k in ('met', 'protected_services_met', 'comparison_eligible')}
            lesson['sla_summary'] = [{k: v.get(k) for k in ('metric', 'status', 'unit', 'statistic', 'scope') if k in v}
                                     for v in effect.get('objectives', ())[:16]]
            artifacts.outcome(0, artifacts.freeze('lesson', lesson))
            journal(graph_role='validator', graph_phase='learn', graph_lesson_ref=artifacts.put('formal_lesson', lesson))
            return {'verdict_ref': artifacts.put('formal_effect', effect), 'route': 'end'}
        builder = StateGraph(AgentGraphState)
        builder.add_node('planner', planner)
        builder.add_node('executor', executor)
        builder.add_node('validator', validator)
        builder.add_edge(START, 'planner')
        builder.add_conditional_edges('planner', lambda s: s['route'], {'validator': 'validator', 'executor': 'executor', 'end': END})
        builder.add_edge('executor', 'validator')
        builder.add_conditional_edges('validator', lambda s: s['route'], {'executor': 'executor', 'end': END})
        with SqliteSaver.from_conn_string(str(path)) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({'operation_id': operation_id, 'intent_id': intent.intent_id,
                'intent_revision': intent.revision, 'validation_phase': 'precheck', 'operation': 'apply'},
                {'configurable': {'thread_id': operation_id}})
        journal(graph_state={k: v for k, v in result.items() if k.endswith('_ref') or k in
                            {'operation_id', 'intent_id', 'intent_revision', 'validation_phase', 'route'}})
        return result
