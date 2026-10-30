"""Evidence-led diagnostics and constrained selection of typed AI proposals."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import inspect
import time
from typing import Any, Mapping, Protocol

from .domain import ContractError, PolicyAuthority, ServiceIntent, finite, required, timestamp
from .membership import ServiceMembership
from .observations import ObservationSnapshot, evaluate_objective

EXECUTION_MODES=('new_sessions_only','reestablish_sessions')


class ActionKind(str, Enum):
    UPDATE_QOS = "update_qos"
    RESIZE_NF = "resize_nf"
    ADD_UPF = "add_upf"
    ASSIGN_SESSIONS = "assign_sessions"
    PROVISION_NODE = "provision_node"
    PLACE_NF = "place_nf"
    DRAIN_AND_REMOVE = "drain_and_remove"
    PLACE_APPLICATION = "place_application"
    REMOVE_APPLICATION = "remove_application"
    DRAIN_APPLICATION = "drain_application"
    BIND_APPLICATION_ENDPOINT = "bind_application_endpoint"
    DRAIN_APPLICATION_ENDPOINT = "drain_application_endpoint"
    REESTABLISH_SESSIONS = "reestablish_sessions"
    START_SERVICE_FLOWS = "start_service_flows"
    STOP_SERVICE_FLOWS = "stop_service_flows"


@dataclass(frozen=True)
class PlanAction:
    action_id: str
    kind: str
    target_id: str
    parameters: dict[str, Any]
    session_keys: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    expected_version: str = ""
    timeout_seconds: float = 60
    compensation: dict[str, Any] = field(default_factory=dict)
    cohort_from_action_id: str = ""

    def __post_init__(self):
        for name in ("action_id", "target_id", "expected_version"):
            required(getattr(self, name), name)
        try:
            object.__setattr__(self, "kind", ActionKind(self.kind).value)
        except ValueError as exc:
            raise ContractError("unsupported action") from exc
        if not isinstance(self.cohort_from_action_id, str) or len(self.cohort_from_action_id) > 128:
            raise ContractError("invalid cohort dependency reference")
        finite(self.timeout_seconds, "timeout_seconds", 0.001)
        object.__setattr__(self, "session_keys", tuple(self.session_keys))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        if not isinstance(self.parameters, dict) or not isinstance(self.compensation, dict):
            raise ContractError("action parameters/compensation require objects")


@dataclass(frozen=True)
class ResourcePlan:
    plan_id: str
    intent_id: str
    intent_revision: int
    observation_id: str
    actions: tuple[PlanAction, ...]
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    expected_attainment: float
    cost: float
    disruption: float
    change_size: float
    guarantees_preserved: bool
    expires_at: float
    model_response_id: str = ''
    model_name: str = ''
    spatial_context_ref: str = ''
    topology_revision: int | None = None
    demand_revision: str = ''
    cohort_digest: str = ''
    path_bindings_ref: str = ''
    offered_load_ref: str = ''
    expected_path_bindings_ref: str = ''
    execution_mode: str = 'new_sessions_only'

    def __post_init__(self):
        for name in ("plan_id", "intent_id", "observation_id"):
            required(getattr(self, name), name)
        if type(self.intent_revision) is not int or self.intent_revision < 1:
            raise ContractError("intent_revision must be positive")
        if type(self.guarantees_preserved) is not bool:
            raise ContractError("guarantees_preserved must be boolean")
        for name in ("expected_attainment", "cost", "disruption", "change_size"):
            finite(getattr(self, name), name)
        if self.expected_attainment > 1:
            raise ContractError("expected_attainment must be 0..1")
        for name in ("actions", "evidence", "uncertainty"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "expires_at", timestamp(self.expires_at))
        for name in ('spatial_context_ref', 'demand_revision', 'cohort_digest', 'path_bindings_ref',
                     'offered_load_ref', 'expected_path_bindings_ref'):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > 1024:
                raise ContractError('invalid plan evidence reference')
        if self.topology_revision is not None and (type(self.topology_revision) is not int or self.topology_revision < 1):
            raise ContractError('invalid plan topology revision')
        if self.execution_mode not in EXECUTION_MODES:
            raise ContractError('unsupported session execution mode')
        seen = set()
        for action in self.actions:
            if action.action_id in seen or not set(action.depends_on) <= seen:
                raise ContractError("duplicate action or unordered/cyclic dependency")
            if action.cohort_from_action_id:
                source = next((a for a in self.actions if a.action_id == action.cohort_from_action_id), None)
                if (action.kind not in {'bind_application_endpoint', 'drain_application_endpoint', 'start_service_flows', 'stop_service_flows'}
                        or source is None or source.action_id not in action.depends_on
                        or source.kind not in {'assign_sessions', 'reestablish_sessions'}):
                    raise ContractError('invalid staged session cohort dependency')
                if source.kind == 'reestablish_sessions' and (not action.session_keys
                        or not set(action.session_keys) <= set(source.session_keys)):
                    raise ContractError('staged rebuild cohort must select exact authorized old sessions')
                if source.kind == 'assign_sessions' and (action.session_keys
                        or source.parameters.get('mode', 'new_sessions') != 'new_sessions'):
                    raise ContractError('staged admission cohort must use the complete new-session binding')
            seen.add(action.action_id)
        if not self.evidence:
            raise ContractError("plan requires observation evidence")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourcePlan:
        from .domain import strict_fields
        strict_fields(data, set(cls.__dataclass_fields__))
        values = dict(data)
        actions = []
        for raw in values.get("actions", ()):
            strict_fields(raw, set(PlanAction.__dataclass_fields__))
            actions.append(PlanAction(**raw))
        values["actions"] = tuple(actions)
        try:
            return cls(**values)
        except TypeError as exc:
            raise ContractError(str(exc)) from exc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Diagnosis:
    bottleneck: str
    resource_id: str
    evidence: tuple[str, ...]
    suggested_actions: tuple[str, ...]


class DiagnosticEngine:
    """Diagnostic hints are conditional evidence, never installation authority."""
    def diagnose(self, snapshot: ObservationSnapshot, now: float | None = None) -> tuple[Diagnosis, ...]:
        now = time.time() if now is None else now
        grouped: dict[str, dict[str, float]] = {}
        windows = {}
        for metric in snapshot.fresh_metrics(now):
            # Input unit contracts prevent comparing percentages with core counts.
            units = {"offered_bps": "bps", "received_bps": "bps", "qer_cap_bps": "bps", "path_capacity_bps": "bps", "cpu_throttled_percent": "percent", "node_free_cpu": "cores", "nf_pending_cpu": "cores", "link_utilization_percent": "percent", "rtt": "ms", "rtt_target": "ms"}
            for direction in ('ul','dl'):
                units.update({name:'bps' for name in (direction+'_offered_bps',direction+'_received_bps',
                    'qer_'+direction+'_cap_bps','path_'+direction+'_capacity_bps')})
            if metric.unit == units.get(metric.name):
                grouped.setdefault(metric.resource_id, {})[metric.name] = metric.value
                windows[(metric.resource_id,metric.name)]=(metric.observed_at,metric.window_seconds)
        result = []
        for resource, m in sorted(grouped.items()):
            offered, received, cap, capacity = (m.get(k) for k in ("offered_bps", "received_bps", "qer_cap_bps", "path_capacity_bps"))
            if None not in (offered, received, cap, capacity) and received < offered and cap < min(offered,capacity):
                result.append(Diagnosis("policy_limited", resource, ("offered_bps", "received_bps", "qer_cap_bps", "path_capacity_bps"), ("update_qos",)))
            for direction in ('ul','dl'):
                names=(direction+'_offered_bps',direction+'_received_bps',
                       'qer_'+direction+'_cap_bps','path_'+direction+'_capacity_bps')
                values=tuple(m.get(k) for k in names)
                coherent=windows.get((resource,names[0]))==windows.get((resource,names[1]))
                if (coherent and None not in values and values[1]<values[0] and values[2]<min(values[0],values[3])):
                    # One direction's native window is sufficient; no invented
                    # combined bidirectional measurement is needed.
                    result.append(Diagnosis('policy_limited',resource,names,('update_qos',)))
                if (coherent and m.get('cpu_throttled_percent',0)>0
                        and values[0] is not None and values[1] is not None and values[1]<values[0]):
                    result.append(Diagnosis('cpu_quota_limited',resource,
                        ('cpu_throttled_percent',names[0],names[1]),('resize_nf','add_upf')))
            if m.get("cpu_throttled_percent", 0) > 0 and offered is not None and received is not None and received < offered:
                result.append(Diagnosis("cpu_quota_limited", resource, ("cpu_throttled_percent", "offered_bps", "received_bps"), ("resize_nf", "add_upf")))
            if "nf_pending_cpu" in m and "node_free_cpu" in m and m["nf_pending_cpu"] > m["node_free_cpu"]:
                result.append(Diagnosis("node_capacity_limited", resource, ("nf_pending_cpu", "node_free_cpu"), ("provision_node", "place_nf")))
            if m.get("link_utilization_percent", 0) >= 95 and "rtt" in m and "rtt_target" in m and m["rtt"] > m["rtt_target"]:
                result.append(Diagnosis("path_limited", resource, ("link_utilization_percent", "rtt", "rtt_target"), ("place_nf", "assign_sessions")))
        if not result:
            result.append(Diagnosis("insufficient_evidence", "", ("collect_missing_service_and_resource_metrics",), ()))
        return tuple(result)


@dataclass(frozen=True)
class PlanContext:
    intent: ServiceIntent
    snapshot: ObservationSnapshot
    membership: ServiceMembership
    authority: PolicyAuthority
    configured_actions: frozenset[str]
    allowed_targets: frozenset[str]
    maximum_cost: float
    now: float
    maximum_actions: int = 16
    allow_session_migration: bool = False
    # Trusted calculator/validator supplies IDs; a model boolean is not proof.
    validated_plan_ids: frozenset[str] = frozenset()
    reserved_identities: frozenset[str] = frozenset()

    def __post_init__(self):
        finite(self.maximum_cost, "maximum_cost")
        timestamp(self.now)
        if type(self.maximum_actions) is not int or not 1 <= self.maximum_actions <= 256:
            raise ContractError("maximum_actions must be 1..256")
        if type(self.allow_session_migration) is not bool:
            raise ContractError("allow_session_migration must be boolean")


class ProposalProvider(Protocol):
    async def propose(self, intent: ServiceIntent, snapshot: ObservationSnapshot, membership: ServiceMembership) -> tuple[ResourcePlan, ...]: ...


class PlanningError(ContractError):
    pass


def staged_precondition(plan: ResourcePlan, action: PlanAction) -> tuple[str, str]:
    """Only explicit same-resource version bindings or exact node placement defer IO."""
    actions = {a.action_id:a for a in plan.actions}
    version_source = ''
    node_source = ''
    if action.expected_version.startswith('dependency:'):
        version_source = action.expected_version.removeprefix('dependency:')
        source = actions.get(version_source)
        if (source is None or source.action_id not in action.depends_on or source.target_id != action.target_id
                or source.kind not in {'add_upf','place_nf','assign_sessions','resize_nf','place_application','bind_application_endpoint','drain_application_endpoint','drain_application'}):
            raise PlanningError('invalid_dependency_version_binding')
    if action.kind in {'add_upf','place_nf','place_application'}:
        for dependency in action.depends_on:
            source = actions[dependency]
            if source.kind == 'provision_node':
                if action.parameters.get('node_id') != source.target_id:
                    raise PlanningError('placement_dependency_node_mismatch')
                node_source = dependency
    return version_source,node_source


class ConstrainedPlanner:
    def __init__(self, proposal_provider: ProposalProvider | None = None, context_factory=None, clock=time.time):
        self.proposal_provider = proposal_provider
        self.context_factory = context_factory
        self.clock = clock

    def rejection_reasons(self, plan: ResourcePlan, context: PlanContext) -> tuple[str, ...]:
        reasons = list(context.intent.definition_issues(context.authority))
        if not context.intent.is_active(context.now):
            reasons.append("intent_inactive")
        if plan.intent_id != context.intent.intent_id or plan.intent_revision != context.intent.revision or plan.observation_id != context.snapshot.observation_id:
            reasons.append("stale_plan")
        if not 0 <= context.now - context.snapshot.observed_at <= context.snapshot.maximum_age_seconds or context.now >= plan.expires_at:
            reasons.append("stale_observation_or_plan")
        if context.membership.intent_id != context.intent.intent_id or context.membership.intent_revision != context.intent.revision:
            reasons.append("stale_membership")
        if context.membership.rejected:
            reasons.append("unresolved_members")
        if not context.membership.members and not context.reserved_identities:
            reasons.append("no_members")
        if plan.cost > context.maximum_cost:
            reasons.append("budget_exceeded")
        if not plan.guarantees_preserved:
            reasons.append("guarantees_violated")
        if plan.actions and plan.plan_id not in context.validated_plan_ids:
            reasons.append("resource_policy_validation_missing")
        if len(plan.actions) > context.maximum_actions:
            reasons.append("too_many_actions")
        if (plan.execution_mode == 'reestablish_sessions' or any(a.kind == 'reestablish_sessions' for a in plan.actions)) and not context.intent.continuity_policy.allow_session_rebuild:
            reasons.append('session_rebuild_forbidden')
        for name in ('spatial_context_ref', 'topology_revision', 'demand_revision', 'cohort_digest', 'path_bindings_ref', 'offered_load_ref'):
            observed = getattr(context.snapshot, name, None)
            proposed = getattr(plan, name, None)
            if observed not in (None, '') and proposed != observed:
                reasons.append('stale_' + name)
        if plan.disruption > context.intent.continuity_policy.maximum_interruption_seconds:
            reasons.append("continuity_violated")
        session_keys = {m.session_token for m in context.membership.members}
        observed_keys = {s.key.token for s in context.snapshot.sessions}
        if not session_keys <= observed_keys:
            reasons.append("membership_session_missing")
        for action in plan.actions:
            if context.reserved_identities and action.kind!='assign_sessions':
                reasons.append('pretraffic_action_not_supported')
            if action.kind not in context.configured_actions:
                reasons.append(f"not_configured:{action.kind}")
            if action.target_id not in context.allowed_targets:
                reasons.append(f"unauthorized_target:{action.target_id}")
            if not set(action.session_keys) <= session_keys:
                reasons.append("foreign_session")
            if action.kind == "assign_sessions" and (action.parameters.get("migrate_existing") or action.parameters.get('mode') == 'migrate') and not context.allow_session_migration:
                reasons.append("migration_unsupported")
            if action.kind == "update_qos" and not action.session_keys:
                reasons.append("qos_sessions_missing")
            if action.parameters.get("rebuild_sessions") and not context.intent.continuity_policy.allow_session_rebuild:
                reasons.append("session_rebuild_forbidden")
        if context.reserved_identities:
            reserved=[]
            try:
                from .pending_admission import SessionAssignment
                for action in plan.actions:
                    assignment=SessionAssignment.from_action(action)
                    if assignment.mode!='new_sessions' or action.session_keys:
                        raise ContractError('Only new-session pending selections are supported')
                    for admission in assignment.pending_admissions:
                        if (admission.owner_id!=context.intent.intent_id or not admission.active(context.now)
                                or admission.valid_from<context.intent.valid_from or admission.valid_until>context.intent.valid_until):
                            raise ContractError('Reserved selection owner or validity changed')
                        reserved.append(admission.identity.token)
                if len(reserved)!=len(set(reserved)) or set(reserved)!=context.reserved_identities:
                    reasons.append('reserved_session_coverage_changed')
            except (ContractError,TypeError,ValueError,KeyError):
                reasons.append('reserved_session_selection_invalid')
        qos_actions = tuple(a for a in plan.actions if a.kind == "update_qos")
        if qos_actions:
            covered = {key for a in qos_actions for key in a.session_keys}
            if covered != session_keys:
                reasons.append("partial_group_qos_coverage")
            for session in context.snapshot.sessions:
                if session.key.token not in covered:
                    continue
                selected = {m.flow_id for m in context.membership.members if m.session_token == session.key.token}
                if "" not in selected and selected != {f.flow_id for f in session.flows}:
                    reasons.append("unsupported_qos_flow_granularity")
        if not plan.actions:
            for objective in context.intent.objectives:
                targets = (context.intent.intent_id,) if objective.scope == "group" else tuple(sorted(session_keys)) if objective.scope == "per_session" else tuple(sorted(m.session_token + ":" + m.flow_id for m in context.membership.members if m.flow_id))
                if evaluate_objective(objective, context.snapshot, targets, context.now).status != "passed":
                    reasons.append("hold_without_verified_attainment")
        return tuple(sorted(set(reasons)))

    def select(self, candidates: tuple[ResourcePlan, ...], context: PlanContext) -> ResourcePlan:
        feasible = [p for p in candidates if not self.rejection_reasons(p, context)]
        if not feasible:
            errors = sorted({r for p in candidates for r in self.rejection_reasons(p, context)})
            raise PlanningError("no_feasible_plan:" + ",".join(errors or ["no_candidates"]))
        return min(feasible, key=lambda p: (-p.expected_attainment, p.disruption, p.cost, p.change_size, p.plan_id))

    async def plan(self, intent: ServiceIntent, snapshot: ObservationSnapshot, membership: ServiceMembership) -> ResourcePlan:
        if self.proposal_provider is None or self.context_factory is None:
            raise PlanningError("not_configured:proposal_provider_or_policy_context")
        candidates = self.proposal_provider.propose(intent, snapshot, membership)
        if inspect.isawaitable(candidates):
            candidates = await candidates
        context = self.context_factory(intent, snapshot, membership)
        if inspect.isawaitable(context):
            context = await context
        return self.select(tuple(candidates), context)


class ResourceCalculator:
    """Weighted max-min allocation with hard minima and a shared capacity bound."""
    def allocate(self, demands: Mapping[str, float], capacity: float, minimums: Mapping[str, float] | None = None, weights: Mapping[str, float] | None = None) -> dict[str, float]:
        finite(capacity, "capacity")
        minimums, weights = dict(minimums or {}), dict(weights or {})
        if (set(minimums) | set(weights)) - set(demands):
            raise ContractError("allocation contains foreign members")
        for key, demand in demands.items():
            required(key, "member")
            finite(demand, "demand")
            finite(minimums.get(key, 0), "minimum")
            finite(weights.get(key, 1), "weight", 0.001)
            if minimums.get(key, 0) > demand:
                raise ContractError("minimum exceeds demand")
        allocated = {key: minimums.get(key, 0.0) for key in sorted(demands)}
        remaining = capacity - sum(allocated.values())
        if remaining < 0:
            raise PlanningError("capacity_below_guarantees")
        active = {key for key in allocated if allocated[key] < demands[key]}
        while active and remaining > 1e-9:
            total_weight = sum(weights.get(key, 1) for key in active)
            finite(total_weight, "total allocation weight", 0.001)
            increments = {key: min(demands[key] - allocated[key], remaining * weights.get(key, 1) / total_weight) for key in active}
            for key, increment in increments.items():
                allocated[key] += increment
            remaining -= sum(increments.values())
            active = {key for key in active if demands[key] - allocated[key] > 1e-9}
        return allocated
