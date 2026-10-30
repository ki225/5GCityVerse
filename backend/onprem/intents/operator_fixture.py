"""Private, explicitly commissioned QoS reductions; never an AI proposal source.

No HTTP route or environment command enables this authority. A trusted local
composition must inject an exact approved directive into the service. Execution
uses the ordinary durable shared-policy, installation and effect pipeline.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import time
from uuid import UUID

from .domain import ContractError, finite, required
from .planning import PlanAction, ResourcePlan


@dataclass(frozen=True)
class OperatorQoSFixture:
    nonce: str
    intent_id: str
    intent_revision: int
    owner_id: str
    lease_id: str
    start_operation_id: str
    traffic_request_id: str
    configuration_revision: str
    target_id: str
    target_version: str
    session_keys: tuple[str, ...]
    ul_kbps: int
    dl_kbps: int
    issued_at: float
    expires_at: float
    operator_reference: str

    def __post_init__(self):
        try:
            if str(UUID(self.nonce)) != self.nonce:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ContractError('fixture_nonce_invalid') from None
        for name in ('intent_id', 'owner_id', 'lease_id', 'start_operation_id',
                     'traffic_request_id', 'configuration_revision', 'target_id',
                     'target_version', 'operator_reference'):
            required(getattr(self, name), name)
        if type(self.intent_revision) is not int or self.intent_revision < 1:
            raise ContractError('fixture_revision_invalid')
        keys = tuple(self.session_keys)
        if not keys or len(set(keys)) != len(keys):
            raise ContractError('fixture_cohort_must_be_nonempty_and_unique')
        from .domain import SessionKey
        for key in keys:
            # Canonical parsing prevents opaque/ambiguous identity approvals.
            if not isinstance(key, str):
                raise ContractError('fixture_session_key_invalid')
            try:
                value = json.loads(key)
                if not isinstance(value, list) or len(value) != 4 or SessionKey(*value).token != key:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ContractError('fixture_session_key_invalid') from None
        object.__setattr__(self, 'session_keys', tuple(sorted(keys)))
        for value in (self.ul_kbps, self.dl_kbps):
            if type(value) is not int or value < 1:
                raise ContractError('fixture_rate_invalid')
        finite(self.issued_at, 'fixture issued_at')
        finite(self.expires_at, 'fixture expires_at')
        if not 0 < self.expires_at - self.issued_at <= 180:
            raise ContractError('fixture_validity_must_be_bounded_to_180_seconds')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError('fixture_directive_schema_invalid')
        return cls(**value)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class OperatorFixtureCoordinator:
    """Allow only exact local approvals, retaining deployed policy constraints."""
    def __init__(self, approved, validator, *, clock=time.time):
        self.approved = {d.nonce: d.fingerprint for d in approved}
        if len(self.approved) != len(approved):
            raise ContractError('duplicate_fixture_approval')
        self.validator, self.clock = validator, clock

    def validate_authority(self, directive, intent, owner, traffic):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        if not isinstance(directive, OperatorQoSFixture):
            raise ContractError('typed_fixture_directive_required')
        if self.approved.get(directive.nonce) != directive.fingerprint:
            raise ContractError('fixture_not_explicitly_approved')
        now = self.clock()
        if not directive.issued_at <= now < directive.expires_at:
            raise ContractError('fixture_approval_expired_or_future')
        if (owner != directive.owner_id or intent.intent_id != directive.intent_id
                or intent.revision != directive.intent_revision or not intent.is_active(now)):
            raise ContractError('fixture_intent_authority_changed')
        if not isinstance(traffic, dict) or traffic.get('native_restoration_complete') is not False:
            raise ContractError('fixture_requires_active_owned_traffic')
        receipt = traffic.get('receipt', {})
        lease = ServiceIntentTrafficLease.from_dict(receipt.get('service_intent_traffic_lease'))
        if (not lease.active(now) or lease.lease_id != directive.lease_id
                or lease.operation_id != directive.start_operation_id
                or traffic.get('start_operation_id') != directive.start_operation_id
                or lease.request_id != directive.traffic_request_id
                or lease.intent_id != intent.intent_id or lease.intent_revision != intent.revision
                or lease.owner_id != owner or receipt.get('accepted') is not True
                or receipt.get('requestId') != lease.request_id):
            raise ContractError('fixture_owned_lease_changed')
        return lease

    def plan(self, directive, intent, snapshot, membership, owner, traffic):
        self.validate_authority(directive, intent, owner, traffic)
        now = self.clock()
        keys = tuple(sorted(m.session_token for m in membership.members))
        sessions = {s.key.token: s for s in snapshot.sessions}
        if (membership.rejected or keys != directive.session_keys
                or snapshot.traffic_request_id != directive.traffic_request_id
                or snapshot.configuration_revision != directive.configuration_revision
                or dict(snapshot.target_versions).get(directive.target_id) != directive.target_version
                or any(k not in sessions or sessions[k].upf_id != directive.target_id for k in keys)):
            raise ContractError('fixture_current_cohort_or_native_version_changed')
        cohort = dict(snapshot.native_target_session_keys).get(directive.target_id)
        if cohort is None or set(cohort) != set(keys):
            raise ContractError('fixture_requires_complete_isolated_native_cohort')
        metrics = {(m.resource_id, m.name): m for m in snapshot.fresh_metrics(now)
                   if m.unit == 'bps' and m.statistic == 'mean' and m.scope == 'per_session'}
        for objective in intent.objectives:
            if (objective.metric not in {'ul_throughput', 'dl_throughput'} or objective.unit != 'bps'
                    or objective.statistic != 'mean' or objective.scope != 'per_session'
                    or objective.direction != 'at_least'):
                raise ContractError('fixture_requires_explicit_per_session_throughput_sla')
            rate = directive.ul_kbps if objective.metric == 'ul_throughput' else directive.dl_kbps
            if rate * 1000 < objective.target:
                raise ContractError('fixture_rate_below_protected_sla')
        for key in keys:
            for direction, rate in (('ul', directive.ul_kbps), ('dl', directive.dl_kbps)):
                cap = metrics.get((key, 'qer_' + direction + '_cap_bps'))
                capacity = metrics.get((key, 'path_' + direction + '_capacity_bps'))
                received = metrics.get((key, direction + '_throughput'))
                if cap is None or capacity is None or received is None:
                    raise ContractError('fixture_current_native_receiver_and_calibration_required')
                if (cap.source != 'native-upf-qer' or capacity.source != 'commissioned-path-capacity'
                        or received.source not in {'native-receiver', 'native-iperf3-receiver'}):
                    raise ContractError('fixture_native_measurement_provenance_invalid')
                if rate * 1000 > cap.value or rate * 1000 > capacity.value:
                    raise ContractError('fixture_must_only_reduce_within_measured_capacity')
                if any(o.metric == direction + '_throughput' and received.value < o.target for o in intent.objectives):
                    raise ContractError('fixture_current_service_not_satisfied')
        action = PlanAction('operator-fixture-' + directive.nonce, 'update_qos', directive.target_id,
            {'rates_kbps': {'ul': directive.ul_kbps, 'dl': directive.dl_kbps}}, keys,
            expected_version=directive.target_version, timeout_seconds=60)
        candidate = ResourcePlan('operator-fixture-' + directive.nonce, intent.intent_id,
            intent.revision, snapshot.observation_id, (action,),
            (snapshot.observation_id, 'operator_fixture:' + directive.operator_reference),
            ('Deliberate operator fixture; no AI resource decision is claimed.',),
            1, 0, 0, 1, True, min(directive.expires_at, intent.valid_until))
        validate = getattr(self.validator, 'validate_operator_fixture', None)
        if not callable(validate):
            raise ContractError('operator_fixture_policy_validator_not_configured')
        return validate(candidate, intent, snapshot, membership)

    def validate_shared(self, directive, prepared):
        if any(row['claim']['intent_id'] != directive.intent_id for row in prepared['claims']):
            raise ContractError('fixture_foreign_protected_claim_present')
        expected = {'ul': directive.ul_kbps, 'dl': directive.dl_kbps}
        if (set(prepared['effective']) != set(directive.session_keys)
                or any(p.rates_kbps != expected for p in prepared['effective'].values())):
            raise ContractError('fixture_shared_policy_does_not_match_approved_reduction')


class OperatorFixturePolicyValidator:
    """Same deployed policy gates, with operator causality instead of diagnosis.

Only the typed coordinator above may construct this single-action reduction.
Normal AI validation never receives this mode or an operator override flag.
"""
    def __init__(self, deployed_validator):
        self.deployed = deployed_validator

    def validate_operator_fixture(self, candidate, intent, snapshot, membership):
        if (len(candidate.actions) != 1 or candidate.actions[0].kind != 'update_qos'
                or not candidate.plan_id.startswith('operator-fixture-')):
            raise ContractError('operator_fixture_plan_shape_invalid')
        return self.deployed._validate(candidate,intent,snapshot,membership,operator_reduction=True)
