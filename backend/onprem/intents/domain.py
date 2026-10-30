"""Validated contracts and identities for intent-driven network control."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping


class ContractError(ValueError):
    pass


OBJECTIVE_UNITS={'ul_throughput':'bps','dl_throughput':'bps','rtt':'ms','loss':'percent',
    'availability':'percent','demand_satisfaction':'percent'}
OBJECTIVE_STATISTICS=('mean','p95','max','min','sum')
OBJECTIVE_SCOPES=('per_session','per_flow','group')
OBJECTIVE_DIRECTIONS=('at_least','at_most')


def timestamp(value: str | float | int | datetime) -> float:
    if isinstance(value, bool):
        raise ContractError("invalid timestamp")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ContractError("timestamp requires timezone")
        return value.timestamp()
    if isinstance(value, str):
        try:
            return timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as error:
            raise ContractError('invalid timestamp') from error
    try:
        result = float(value)
    except (TypeError,ValueError,OverflowError) as error:
        raise ContractError('invalid timestamp') from error
    if not isfinite(result):
        raise ContractError("timestamp must be finite")
    return result


def finite(value: Any, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < minimum:
        raise ContractError(f"{name} must be finite and >= {minimum}")
    return float(value)


def required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} is required")
    return value


def strict_fields(data: Mapping, allowed: set[str]) -> None:
    if not isinstance(data, Mapping):
        raise ContractError("expected object")
    extra = set(data) - allowed
    if extra:
        raise ContractError(f"unknown fields: {sorted(extra)}")


def construct(cls, values):
    try:
        return cls(**values)
    except TypeError as error:
        raise ContractError('missing or malformed '+cls.__name__) from error


@dataclass(frozen=True)
class Objective:
    metric: str
    target: float
    unit: str
    statistic: str
    scope: str
    window_seconds: float
    provenance: str
    direction: str = "at_least"

    def __post_init__(self):
        if OBJECTIVE_UNITS.get(self.metric) != self.unit:
            raise ContractError("unsupported metric/unit")
        finite(self.target, "target")
        if self.unit == "percent" and self.target > 100:
            raise ContractError("percent exceeds 100")
        if self.statistic not in OBJECTIVE_STATISTICS:
            raise ContractError("unsupported statistic")
        if self.scope not in OBJECTIVE_SCOPES:
            raise ContractError("unsupported objective scope")
        if self.direction not in OBJECTIVE_DIRECTIONS:
            raise ContractError("unsupported direction")
        finite(self.window_seconds, "window_seconds", 0.001)
        required(self.provenance, "objective provenance")


@dataclass(frozen=True)
class Selector:
    regions: tuple[str, ...] = ()
    service_types: tuple[str, ...] = ()
    applications: tuple[str, ...] = ()
    supis: tuple[str, ...] = ()
    slice_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            values = getattr(self, name)
            if isinstance(values, str) or not isinstance(values, (list, tuple)):
                raise ContractError(f"{name} requires an array")
            object.__setattr__(self, name, tuple(required(v, name) for v in values))
        if not any(getattr(self, n) for n in self.__dataclass_fields__):
            raise ContractError("selector cannot match the entire network implicitly")


@dataclass(frozen=True)
class ContinuityPolicy:
    allow_session_rebuild: bool = False
    maximum_interruption_seconds: float = 0

    def __post_init__(self):
        if type(self.allow_session_rebuild) is not bool:
            raise ContractError("allow_session_rebuild must be boolean")
        finite(self.maximum_interruption_seconds, "maximum_interruption_seconds")


@dataclass(frozen=True)
class PolicyAuthority:
    priority_policy_ids: frozenset[str]
    degradation_policy_ids: frozenset[str]
    resource_budget_ids: frozenset[str]
    allowed_metrics: frozenset[str] = frozenset()

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, frozenset(getattr(self, name)))


@dataclass(frozen=True)
class ServiceIntent:
    intent_id: str
    revision: int
    source_event_id: str
    selector: Selector
    valid_from: float
    valid_until: float
    objectives: tuple[Objective, ...]
    priority_policy_id: str
    degradation_policy_id: str
    resource_budget_id: str
    continuity_policy: ContinuityPolicy = field(default_factory=ContinuityPolicy)
    provenance: str = ""

    def __post_init__(self):
        for name in ("intent_id", "source_event_id", "priority_policy_id", "degradation_policy_id", "resource_budget_id", "provenance"):
            required(getattr(self, name), name)
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("revision must be a positive integer")
        object.__setattr__(self, "valid_from", timestamp(self.valid_from))
        object.__setattr__(self, "valid_until", timestamp(self.valid_until))
        if self.valid_until <= self.valid_from:
            raise ContractError("valid_until must follow valid_from")
        object.__setattr__(self, "objectives", tuple(self.objectives))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ServiceIntent:
        strict_fields(data, set(cls.__dataclass_fields__))
        values = dict(data)
        selector = values.get("selector", {})
        strict_fields(selector, set(Selector.__dataclass_fields__))
        values["selector"] = construct(Selector,selector)
        objectives = []
        for obj in values.get("objectives", []):
            strict_fields(obj, set(Objective.__dataclass_fields__))
            objectives.append(construct(Objective,obj))
        values["objectives"] = tuple(objectives)
        continuity = values.get("continuity_policy", {})
        strict_fields(continuity, set(ContinuityPolicy.__dataclass_fields__))
        values["continuity_policy"] = construct(ContinuityPolicy,continuity)
        try:
            return cls(**values)
        except TypeError as exc:
            raise ContractError(str(exc)) from exc

    def to_dict(self) -> dict:
        return asdict(self)

    def is_active(self, now: float) -> bool:
        return self.valid_from <= now < self.valid_until

    def definition_issues(self, authority: PolicyAuthority, metrics: frozenset[str] | None = None) -> tuple[str, ...]:
        issues = []
        for name, allowed in (("priority_policy_id", authority.priority_policy_ids), ("degradation_policy_id", authority.degradation_policy_ids), ("resource_budget_id", authority.resource_budget_ids)):
            if getattr(self, name) not in allowed:
                issues.append(f"unauthorized:{name}")
        if not self.objectives:
            issues.append("objectives_missing")
        available = authority.allowed_metrics if metrics is None else metrics
        issues.extend(f"unobservable:{o.metric}" for o in self.objectives if o.metric not in available)
        return tuple(issues)


@dataclass(frozen=True, order=True)
class SessionKey:
    network_scope: str
    supi: str
    pdu_session_id: int
    established_epoch: str

    def __post_init__(self):
        for name in ("network_scope", "supi", "established_epoch"):
            required(getattr(self, name), name)
        if type(self.pdu_session_id) is not int or not 1 <= self.pdu_session_id <= 15:
            raise ContractError("PDU session ID must be 1..15")

    @property
    def token(self) -> str:
        # JSON prevents delimiter collisions in opaque session identities.
        import json
        return json.dumps([self.network_scope, self.supi, self.pdu_session_id, self.established_epoch], separators=(",", ":"))


@dataclass(frozen=True)
class FlowBinding:
    flow_id: str
    application_id: str
    qfi: int
    classification_source: str
    separable: bool = False

    def __post_init__(self):
        for name in ("flow_id", "application_id", "classification_source"):
            required(getattr(self, name), name)
        if type(self.qfi) is not int or not 1 <= self.qfi <= 63:
            raise ContractError("QFI must be 1..63")
        if type(self.separable) is not bool:
            raise ContractError("separable must be boolean")


@dataclass(frozen=True)
class SessionBinding:
    key: SessionKey
    s_nssai: str
    dnn: str
    smf_id: str
    upf_id: str
    slice_instance_id: str
    region: str
    service_type: str
    path_revision: str
    flows: tuple[FlowBinding, ...] = ()
    service_profile_id: str = ""

    def __post_init__(self):
        if self.service_profile_id:
            from .service_classification import matches_service_types
            matches_service_types((), self.service_type, self.service_profile_id)
        for name in ("s_nssai", "dnn", "smf_id", "upf_id", "slice_instance_id", "path_revision"):
            required(getattr(self, name), name)
        object.__setattr__(self, "flows", tuple(self.flows))
        if len({f.flow_id for f in self.flows}) != len(self.flows):
            raise ContractError("duplicate flow identity")


@dataclass(frozen=True)
class SliceInstanceBinding:
    instance_id: str
    s_nssai: str
    nf_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    resource_revision: str

    def __post_init__(self):
        for name in ("instance_id", "s_nssai", "resource_revision"):
            required(getattr(self, name), name)
        object.__setattr__(self, "nf_ids", tuple(self.nf_ids))
        object.__setattr__(self, "node_ids", tuple(self.node_ids))
