"""Bounded, time-qualified observations and calibrated capacity evidence."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Any, Mapping

from .domain import ContractError, Objective, SessionBinding, SessionKey, FlowBinding, finite, required, timestamp


@dataclass(frozen=True)
class MeasurementRequest:
    """Missing evidence only; never permission to start a native measurement."""
    kind: str
    reason: str
    resource_id: str
    required_metrics: tuple[str, ...]

    def __post_init__(self):
        schemas = {
            'native_path_calibration': ('path_capacity_missing', {'path_ul_capacity_bps', 'path_dl_capacity_bps'}),
            'native_qer_inventory': ('qer_inventory_missing', {'qer_ul_cap_bps', 'qer_dl_cap_bps'}),
            'native_nf_load': ('nf_load_missing', {'nf_ingress_wire_bps'}),
            'native_resource_inventory': ('resource_inventory_missing', {'cpu_limits', 'memory_limits', 'node_free_cpu', 'node_free_memory',
                'cpu_throttled_percent','memory_working_set_bytes'}),
        }
        schema = schemas.get(self.kind)
        values = tuple(self.required_metrics)
        if (not schema or self.reason != schema[0] or not values or len(set(values)) != len(values)
                or not set(values) <= schema[1] or not isinstance(self.resource_id, str)
                or not 1 <= len(self.resource_id) <= 4096):
            raise ContractError('invalid_measurement_request')
        object.__setattr__(self, 'required_metrics', tuple(sorted(values)))

    @property
    def request_id(self):
        import hashlib, json
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def to_dict(self):
        return dict(asdict(self), request_id=self.request_id, automatic_dispatch=False)

    @classmethod
    def from_dict(cls, value):
        if set(value) != {'kind', 'reason', 'resource_id', 'required_metrics', 'request_id', 'automatic_dispatch'}:
            raise ContractError('invalid_measurement_request')
        item = cls(**{k: value[k] for k in ('kind', 'reason', 'resource_id', 'required_metrics')})
        if value['request_id'] != item.request_id or value['automatic_dispatch'] is not False:
            raise ContractError('invalid_measurement_request')
        return item


def missing_measurements(sessions, metrics, missing=()):
    """Convert bounded collector omissions to a safe closed vocabulary."""
    result = {}
    for session in sessions:
        names = {m.name for m in metrics if m.resource_id == session.key.token and m.validity == 'valid'}
        for kind, reason, required_names in (
                ('native_path_calibration', 'path_capacity_missing', ('path_ul_capacity_bps', 'path_dl_capacity_bps')),
                ('native_qer_inventory', 'qer_inventory_missing', ('qer_ul_cap_bps', 'qer_dl_cap_bps'))):
            if not set(required_names) <= names:
                item = MeasurementRequest(kind, reason, session.key.token, required_names)
                result[item.request_id] = item
    # Collector strings never become model instructions or public diagnostics.
    for code in tuple(missing)[:256]:
        if not isinstance(code, str): continue
        if ':native_nf:' in code:
            target = code.split(':native_nf:', 1)[0]
            item = MeasurementRequest('native_resource_inventory', 'resource_inventory_missing', target,
                ('cpu_limits', 'memory_limits', 'cpu_throttled_percent', 'memory_working_set_bytes'))
            result[item.request_id] = item
        if ':native_nf_load:' in code:
            target = code.split(':native_nf_load:', 1)[0]
            item = MeasurementRequest('native_nf_load', 'nf_load_missing', target, ('nf_ingress_wire_bps',))
            result[item.request_id] = item
        for metric in ('cpu_throttled_percent','memory_working_set_bytes','node_capacity'):
            token=':'+metric+':'
            if token in code:
                target=code.split(token,1)[0]
                names=('node_free_cpu','node_free_memory') if metric=='node_capacity' else (metric,)
                item=MeasurementRequest('native_resource_inventory','resource_inventory_missing',target,names)
                result[item.request_id]=item
    if len(result) > 64:
        raise ContractError('measurement_request_scope_too_large')
    return tuple(result[k] for k in sorted(result))


@dataclass(frozen=True)
class Metric:
    name: str
    value: float | None
    unit: str
    source: str
    observed_at: float
    window_seconds: float
    resource_id: str
    validity: str = "valid"
    statistic: str = "mean"
    scope: str = "per_session"

    def __post_init__(self):
        for name in ("name", "unit", "source", "resource_id"):
            required(getattr(self, name), name)
        finite(self.window_seconds, "window_seconds", 0.001)
        object.__setattr__(self, "observed_at", timestamp(self.observed_at))
        if self.validity not in {"valid", "stale", "missing", "invalid"}:
            raise ContractError("invalid metric validity")
        if self.value is not None:
            finite(self.value, "metric value")
        elif self.validity == "valid":
            raise ContractError("valid metric requires a value")
        if self.statistic not in {"mean", "p95", "max", "min", "sum"}:
            raise ContractError("unsupported metric statistic")
        if self.scope not in {"per_session", "per_flow", "group", "resource"}:
            raise ContractError("unsupported metric scope")

    def usable(self, now: float, maximum_age_seconds: float) -> bool:
        return self.validity == "valid" and self.value is not None and 0 <= now - self.observed_at <= maximum_age_seconds


@dataclass(frozen=True)
class ObservationSnapshot:
    observation_id: str
    observed_at: float
    configuration_revision: str
    sessions: tuple[SessionBinding, ...] = ()
    metrics: tuple[Metric, ...] = ()
    available_resources: tuple[tuple[str, float], ...] = ()
    maximum_age_seconds: float = 30
    target_versions: tuple[tuple[str, str], ...] = ()
    traffic_request_id: str = ''
    target_measurement_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    native_target_session_keys: tuple[tuple[str,tuple[str,...]],...] = ()
    measurement_requests: tuple[MeasurementRequest, ...] = ()
    spatial_context: dict = field(default_factory=dict)
    spatial_context_ref: str = ''
    topology_revision: int | None = None
    demand_revision: str = ''
    cohort_digest: str = ''
    path_bindings_ref: str = ''
    offered_load_ref: str = ''
    path_evidence: tuple[dict, ...] = ()

    def __post_init__(self):
        required(self.observation_id, "observation_id")
        required(self.configuration_revision, "configuration_revision")
        if not isinstance(self.traffic_request_id,str):
            raise ContractError('traffic_request_id must be a string')
        object.__setattr__(self, "observed_at", timestamp(self.observed_at))
        finite(self.maximum_age_seconds, "maximum_age_seconds", 0.001)
        for name in ("sessions", "metrics", "available_resources", "target_versions"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        import json
        from copy import deepcopy
        for name in ('spatial_context_ref', 'demand_revision', 'cohort_digest', 'path_bindings_ref', 'offered_load_ref'):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > 1024:
                raise ContractError('invalid observation evidence reference')
        if self.topology_revision is not None and (type(self.topology_revision) is not int or self.topology_revision < 1):
            raise ContractError('invalid observation topology revision')
        if type(self.spatial_context) is not dict or len(json.dumps(self.spatial_context, allow_nan=False).encode()) > 262144:
            raise ContractError('invalid bounded spatial observation')
        if not isinstance(self.path_evidence, (tuple, list)) or len(self.path_evidence) > 256 or any(type(v) is not dict for v in self.path_evidence):
            raise ContractError('invalid path evidence collection')
        if len(json.dumps(self.path_evidence, allow_nan=False).encode()) > 262144:
            raise ContractError('path evidence collection too large')
        object.__setattr__(self, 'spatial_context', deepcopy(self.spatial_context))
        object.__setattr__(self, 'path_evidence', tuple(deepcopy(v) for v in self.path_evidence))
        requests = tuple(self.measurement_requests)
        if (len(requests) > 64 or any(type(r) is not MeasurementRequest for r in requests)
                or len({r.request_id for r in requests}) != len(requests)):
            raise ContractError('invalid_measurement_requests')
        object.__setattr__(self, 'measurement_requests', requests)
        if len({s.key for s in self.sessions}) != len(self.sessions):
            raise ContractError("duplicate session in snapshot")
        if len({k for k, _ in self.available_resources}) != len(self.available_resources):
            raise ContractError("duplicate resource capacity")
        for name, value in self.available_resources:
            required(name, "capacity name")
            finite(value, name)
        if len(dict(self.target_versions)) != len(self.target_versions):
            raise ContractError("duplicate native target version")
        for target,version in self.target_versions:
            required(target,"native target")
            required(version,"native version")
        memberships = tuple((target,tuple(ids)) for target,ids in self.target_measurement_ids)
        object.__setattr__(self,'target_measurement_ids',memberships)
        if len(dict(memberships)) != len(memberships):
            raise ContractError('duplicate target measurement membership')
        for target,ids in memberships:
            required(target,'measurement target')
            if not ids or len(set(ids)) != len(ids):
                raise ContractError('target measurement members must be nonempty and unique')
            for identity in ids:
                required(identity,'measurement member')
        cohorts=tuple((target,tuple(keys)) for target,keys in self.native_target_session_keys)
        object.__setattr__(self,'native_target_session_keys',cohorts)
        if len(dict(cohorts))!=len(cohorts):
            raise ContractError('duplicate native target cohort')
        for target,keys in cohorts:
            required(target,'native cohort target')
            if len(set(keys))!=len(keys):
                raise ContractError('duplicate native cohort session')
            for key in keys:
                required(key,'native cohort exact session key')
        identities = [(m.name, m.resource_id, m.statistic, m.scope) for m in self.metrics]
        if len(set(identities)) != len(identities):
            raise ContractError("ambiguous metric identity")

    def fresh_metrics(self, now: float) -> tuple[Metric, ...]:
        if not 0 <= now - self.observed_at <= self.maximum_age_seconds:
            return ()
        return tuple(m for m in self.metrics if m.usable(now, self.maximum_age_seconds))

    def to_dict(self) -> dict:
        return dict(asdict(self), measurement_requests=[r.to_dict() for r in self.measurement_requests])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObservationSnapshot:
        from .domain import strict_fields
        strict_fields(data, set(cls.__dataclass_fields__))
        values = dict(data)
        values['measurement_requests'] = tuple(MeasurementRequest.from_dict(r) for r in values.get('measurement_requests', ()))
        sessions = []
        for raw in values.get("sessions", ()):
            session = dict(raw)
            session["key"] = SessionKey(**session["key"])
            session["flows"] = tuple(FlowBinding(**f) for f in session.get("flows", ()))
            sessions.append(SessionBinding(**session))
        values["sessions"] = tuple(sessions)
        values["metrics"] = tuple(Metric(**m) for m in values.get("metrics", ()))
        resources = values.get("available_resources", ())
        values["available_resources"] = tuple(resources.items()) if isinstance(resources, dict) else tuple(tuple(v) for v in resources)
        versions = values.get('target_versions',())
        values['target_versions'] = tuple(versions.items()) if isinstance(versions,dict) else tuple(tuple(v) for v in versions)
        memberships = values.get('target_measurement_ids',())
        values['target_measurement_ids'] = tuple(memberships.items()) if isinstance(memberships,dict) else tuple((target,tuple(ids)) for target,ids in memberships)
        cohorts=values.get('native_target_session_keys',())
        values['native_target_session_keys']=tuple(cohorts.items()) if isinstance(cohorts,dict) else tuple((target,tuple(keys)) for target,keys in cohorts)
        try:
            return cls(**values)
        except TypeError as exc:
            raise ContractError(str(exc)) from exc


@dataclass(frozen=True)
class ObjectiveVerdict:
    metric: str
    status: str
    targets: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]


def evaluate_objective(objective: Objective, snapshot: ObservationSnapshot, resource_ids: tuple[str, ...], now: float) -> ObjectiveVerdict:
    metrics = {m.resource_id: m for m in snapshot.fresh_metrics(now) if m.name == objective.metric and m.unit == objective.unit and m.statistic == objective.statistic and m.scope == objective.scope and m.window_seconds >= objective.window_seconds}
    targets = tuple(sorted(set(resource_ids)))
    missing = tuple(r for r in targets if r not in metrics)
    failed = tuple(r for r in targets if r in metrics and ((metrics[r].value < objective.target) if objective.direction == "at_least" else (metrics[r].value > objective.target)))
    status = "unknown" if not targets or missing else "failed" if failed else "passed"
    return ObjectiveVerdict(objective.metric, status, targets, missing, failed)


class BoundedObservationStore:
    def __init__(self, maximum_snapshots: int = 8, retention_seconds: float = 300):
        if type(maximum_snapshots) is not int or not 1 <= maximum_snapshots <= 1024:
            raise ContractError("maximum_snapshots must be 1..1024")
        finite(retention_seconds, "retention_seconds", 0.001)
        self._items = deque(maxlen=maximum_snapshots)
        self.retention_seconds = retention_seconds
        self._lock = RLock()

    def append(self, snapshot: ObservationSnapshot, now: float) -> None:
        if not 0 <= now - snapshot.observed_at <= self.retention_seconds:
            raise ContractError("snapshot outside retention window")
        with self._lock:
            if self._items and snapshot.observed_at < self._items[-1].observed_at:
                raise ContractError("out-of-order observation")
            self._items.append(snapshot)
            self._expire(now)

    def _expire(self, now: float) -> None:
        while self._items and now - self._items[0].observed_at > self.retention_seconds:
            self._items.popleft()

    def current(self, now: float) -> ObservationSnapshot | None:
        with self._lock:
            self._expire(now)
            return self._items[-1] if self._items else None

    def recent(self, now: float) -> tuple[ObservationSnapshot, ...]:
        with self._lock:
            self._expire(now)
            return tuple(self._items)


@dataclass(frozen=True)
class CapacityProfile:
    profile_id: str
    nf_type: str
    hardware_id: str
    software_version: str
    traffic_profile: str
    cpu_cores: float
    memory_bytes: float
    minimum_load_bps: float
    maximum_load_bps: float
    error_fraction: float
    calibration_evidence: str
    valid_until: float
    load_metric_name: str = 'offered_bps'
    load_metric_source: str = ''
    load_metric_scope: str = ''

    def __post_init__(self):
        for name in ("profile_id", "nf_type", "hardware_id", "software_version", "traffic_profile", "calibration_evidence"):
            required(getattr(self, name), name)
        for name in ("cpu_cores", "memory_bytes", "minimum_load_bps", "maximum_load_bps", "error_fraction"):
            finite(getattr(self, name), name)
        if self.cpu_cores == 0 or self.memory_bytes == 0 or self.maximum_load_bps < self.minimum_load_bps or self.error_fraction >= 1:
            raise ContractError("invalid calibration range")
        object.__setattr__(self, "valid_until", timestamp(self.valid_until))
        if self.load_metric_name!='offered_bps' and (not self.load_metric_source or self.load_metric_scope!='resource'):
            raise ContractError('non_payload_calibration_requires_explicit_metric_source_scope')

    def supports(self, *, nf_type: str, hardware_id: str, software_version: str, traffic_profile: str, cpu_cores: float, memory_bytes: float, load_bps: float, now: float) -> bool:
        finite(load_bps, "load_bps")
        return now < self.valid_until and (nf_type, hardware_id, software_version, traffic_profile, cpu_cores, memory_bytes) == (self.nf_type, self.hardware_id, self.software_version, self.traffic_profile, self.cpu_cores, self.memory_bytes) and self.minimum_load_bps <= load_bps <= self.maximum_load_bps * (1 - self.error_fraction)
