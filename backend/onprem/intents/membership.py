"""All-member policy resolution; no experimental holdout allocation."""
from dataclasses import dataclass
from .domain import ContractError, ServiceIntent, SessionBinding, finite
from .service_classification import matches_service_types


@dataclass(frozen=True, order=True)
class Member:
    session_token: str
    flow_id: str = ""


@dataclass(frozen=True)
class ServiceMembership:
    intent_id: str
    intent_revision: int
    members: tuple[Member, ...]
    rejected: tuple[tuple[str, str], ...] = ()


class MembershipResolver:
    def resolve(self, intent: ServiceIntent, sessions: tuple[SessionBinding, ...]) -> ServiceMembership:
        selector = intent.selector
        members, rejected, seen = [], [], set()
        for session in sessions:
            token = session.key.token
            if token in seen:
                raise ContractError("duplicate session identity")
            seen.add(token)
            if not matches_service_types(selector.service_types, session.service_type, session.service_profile_id):
                continue
            if any(values and value not in values for values, value in ((selector.regions, session.region), (selector.supis, session.key.supi), (selector.slice_ids, session.slice_instance_id))):
                continue
            if not selector.applications:
                members.append(Member(token))
                continue
            matching = [f for f in session.flows if f.application_id in selector.applications]
            if not matching:
                rejected.append((token, "application_classification_missing"))
            elif len(session.flows) > len(matching) and not all(f.separable for f in matching):
                rejected.append((token, "unsupported_granularity"))
            else:
                members.extend(Member(token, f.flow_id) for f in matching)
        return ServiceMembership(intent.intent_id, intent.revision, tuple(sorted(members)), tuple(sorted(rejected)))


@dataclass(frozen=True)
class ServicePolicy:
    intent_id: str
    policy_revision: int
    members: tuple[Member, ...]
    minimum_bps: float
    maximum_bps: float
    allocation_rule: str = "demand"

    def __post_init__(self):
        finite(self.minimum_bps, "minimum_bps")
        finite(self.maximum_bps, "maximum_bps")
        if self.minimum_bps > self.maximum_bps:
            raise ContractError("minimum exceeds maximum")
        if self.allocation_rule not in {"equal", "demand", "weighted"}:
            raise ContractError("unsupported allocation rule")
        if type(self.policy_revision) is not int or self.policy_revision < 1:
            raise ContractError("policy revision must be positive")
        object.__setattr__(self, "members", tuple(self.members))


@dataclass(frozen=True)
class EffectivePolicy:
    member: Member
    owners: tuple[tuple[str, int], ...]
    minimum_bps: float
    maximum_bps: float


class PolicyMerger:
    def merge(self, policies: tuple[ServicePolicy, ...]) -> tuple[EffectivePolicy, ...]:
        grouped = {}
        identities = set()
        for policy in policies:
            if policy.intent_id in identities:
                raise ContractError("multiple revisions of same intent")
            identities.add(policy.intent_id)
            for member in set(policy.members):
                grouped.setdefault(member, []).append(policy)
        # A session-wide rule overlaps every flow rule: reject until narrowed explicitly.
        for member in grouped:
            if member.flow_id and Member(member.session_token) in grouped:
                raise ContractError("session and flow policy overlap")
        result = []
        for member, matches in sorted(grouped.items()):
            minimum = max(p.minimum_bps for p in matches)
            maximum = min(p.maximum_bps for p in matches)
            if minimum > maximum:
                raise ContractError("incompatible guarantees")
            if len({p.allocation_rule for p in matches}) > 1:
                raise ContractError("incompatible allocation policies")
            result.append(EffectivePolicy(member, tuple(sorted((p.intent_id, p.policy_revision) for p in matches)), minimum, maximum))
        return tuple(result)


@dataclass(frozen=True)
class MembershipDelta:
    add: tuple[Member, ...]
    remove: tuple[Member, ...]
    pending: tuple[Member, ...]


def reconcile_members(desired: tuple[Member, ...], effective: tuple[Member, ...], verified: tuple[Member, ...] = ()) -> MembershipDelta:
    wanted, current = set(desired), set(effective)
    accepted = set(verified) & wanted
    return MembershipDelta(tuple(sorted(wanted - current)), tuple(sorted(current - wanted)), tuple(sorted(wanted - accepted)))
