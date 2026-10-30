"""Pure cross-intent QoS resolution; callers persist claims transactionally."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math

from .domain import ContractError, finite, required
from .membership import Member, PolicyMerger, ServicePolicy


def effective_owner(member: Member) -> str:
    identity = json.dumps(asdict(member), sort_keys=True, separators=(",", ":"))
    return "policy:" + hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class QoSPolicyClaim:
    intent_id: str
    revision: int
    member: Member
    ul_min_bps: float
    ul_max_bps: float
    dl_min_bps: float
    dl_max_bps: float
    desired_ul_bps: float
    desired_dl_bps: float
    allocation_authority: str

    def __post_init__(self):
        required(self.intent_id, "intent_id")
        required(self.allocation_authority, "allocation_authority")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("invalid claim revision")
        if self.member.flow_id:
            raise ContractError("native QER flow discrimination not supported")
        for direction in ("ul", "dl"):
            minimum, maximum, desired = (getattr(self, f"{direction}_{name}_bps") if name in {"min", "max"} else getattr(self, f"desired_{direction}_bps") for name in ("min", "max", "desired"))
            for name, value in (("minimum", minimum), ("maximum", maximum), ("desired", desired)):
                finite(value, name)
            if not minimum <= desired <= maximum:
                raise ContractError("desired allocation outside authorized bounds")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["member"] = Member(**values["member"])
        return cls(**values)

    @classmethod
    def from_validated_action(cls, *, intent_id, revision, session_key, action, allocation_authority, ul_min_bps, ul_max_bps, dl_min_bps, dl_max_bps):
        if action.kind != "update_qos" or session_key not in action.session_keys:
            raise ContractError("claim requires explicit validated QoS action membership")
        if action.parameters.get("flow_ids"):
            raise ContractError("native QER flow discrimination not supported")
        rates = action.parameters.get("rates_by_session", {}).get(session_key) or action.parameters.get("rates_kbps")
        if not isinstance(rates, dict) or set(rates) != {"ul", "dl"}:
            raise ContractError("claim requires explicit native rates, not inferred objectives")
        for rate in rates.values():
            if type(rate) is not int or rate < 1:
                raise ContractError("native rate must be positive integer Kbps")
        return cls(intent_id, revision, Member(session_key), ul_min_bps, ul_max_bps, dl_min_bps, dl_max_bps, rates["ul"] * 1000, rates["dl"] * 1000, allocation_authority)


@dataclass(frozen=True)
class EffectiveResourcePolicy:
    member: Member
    owner_id: str
    desired_revision: str
    owners: tuple[tuple[str, int], ...]
    ul_min_bps: float
    ul_max_bps: float
    dl_min_bps: float
    dl_max_bps: float
    rates_kbps: dict

    def to_dict(self):
        return asdict(self)


class EffectivePolicyCoordinator:
    def reconcile(self, claims: tuple[QoSPolicyClaim, ...]) -> tuple[EffectiveResourcePolicy, ...]:
        grouped, unique = {}, set()
        for claim in claims:
            identity = (claim.intent_id, claim.member)
            if identity in unique:
                raise ContractError("duplicate active claim or intent revision")
            unique.add(identity)
            grouped.setdefault(claim.member, []).append(claim)
        result = []
        for member, active in sorted(grouped.items()):
            merged = {}
            rates = {}
            for direction in ("ul", "dl"):
                policies = tuple(ServicePolicy(c.intent_id, c.revision, (member,), getattr(c, direction + "_min_bps"), getattr(c, direction + "_max_bps")) for c in active)
                effective = PolicyMerger().merge(policies)[0]
                requested = max(getattr(c, "desired_" + direction + "_bps") for c in active)
                chosen = min(effective.maximum_bps, max(effective.minimum_bps, requested))
                kbps = math.floor(chosen / 1000)
                if kbps < 1 or kbps * 1000 < effective.minimum_bps:
                    raise ContractError("native Kbps precision cannot satisfy guarantee")
                merged[direction] = effective
                rates[direction] = kbps
            owners = tuple(sorted((c.intent_id, c.revision) for c in active))
            owner_id = effective_owner(member)
            body = {"owners": owners, "rates_kbps": rates, "bounds": {d: (merged[d].minimum_bps, merged[d].maximum_bps) for d in merged}}
            revision = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            result.append(EffectiveResourcePolicy(member, owner_id, revision, owners, merged["ul"].minimum_bps, merged["ul"].maximum_bps, merged["dl"].minimum_bps, merged["dl"].maximum_bps, rates))
        return tuple(result)

    def release(self, claims: tuple[QoSPolicyClaim, ...], intent_id: str):
        remaining = tuple(c for c in claims if c.intent_id != intent_id)
        return remaining, self.reconcile(remaining)
