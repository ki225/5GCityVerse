"""Private, bounded native path and service-effect evidence contracts."""
from dataclasses import asdict, dataclass
import json

from .domain import ContractError, SessionKey, finite, required, strict_fields


@dataclass(frozen=True)
class PathEvidence:
    evidence_id: str
    intent_id: str
    operation_id: str
    round_id: str
    generation: int
    session_key: str
    upf_target_id: str
    upf_epoch: str
    pfcp_seid: str
    pdr_ids: tuple[int, ...]
    far_ids: tuple[int, ...]
    application_instance_id: str
    application_pod_uid: str
    endpoint_revision: str
    flow_id: str
    receiver_id: str
    native_evidence_refs: tuple[str, ...]
    window_started_at: float
    observed_at: float
    source: str = 'native-service-path/v1'

    def __post_init__(self):
        for name in ('evidence_id', 'intent_id', 'operation_id', 'round_id', 'session_key',
                     'upf_target_id', 'upf_epoch', 'pfcp_seid', 'application_instance_id',
                     'application_pod_uid', 'endpoint_revision', 'flow_id', 'receiver_id'):
            if len(required(getattr(self, name), name)) > 4096:
                raise ContractError('path evidence identity too large')
        try:
            identity = json.loads(self.session_key)
            if not isinstance(identity, list) or len(identity) != 4:
                raise ValueError
            SessionKey(*identity)
        except (ValueError, TypeError):
            raise ContractError('path evidence requires an established exact session') from None
        if type(self.generation) is not int or self.generation < 1 or self.source != 'native-service-path/v1':
            raise ContractError('invalid native path scope')
        for name in ('pdr_ids', 'far_ids'):
            values = tuple(getattr(self, name))
            if not 1 <= len(values) <= 256 or len(values) != len(set(values)) or any(type(v) is not int or v < 0 for v in values):
                raise ContractError('native forwarding rules are missing or malformed')
            object.__setattr__(self, name, values)
        refs = tuple(self.native_evidence_refs)
        if not 2 <= len(refs) <= 16 or any(not isinstance(v, str) or not 1 <= len(v) <= 1024 for v in refs):
            raise ContractError('independent forwarding and receiver evidence required')
        object.__setattr__(self, 'native_evidence_refs', refs)
        finite(self.window_started_at, 'path window start')
        finite(self.observed_at, 'path observed_at')
        if self.window_started_at >= self.observed_at:
            raise ContractError('path evidence requires a nonempty receiver window')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        strict_fields(value, set(cls.__dataclass_fields__))
        try:
            return cls(**value)
        except TypeError:
            raise ContractError('missing native path evidence fields') from None


@dataclass(frozen=True)
class EffectVerdict:
    installation: str
    routing: str
    sla: str
    protected_services: str
    comparability: str
    restoration: str = 'not_applicable'

    def __post_init__(self):
        if any(getattr(self, name) not in {'passed', 'failed', 'unknown', 'not_applicable'}
               for name in self.__dataclass_fields__):
            raise ContractError('invalid effect verdict')

    @property
    def service_effect_verified(self):
        return all(getattr(self, key) == 'passed' for key in ('installation', 'routing', 'sla', 'protected_services'))

    @property
    def improvement_comparable(self):
        return self.comparability == 'passed'

    def to_dict(self):
        return dict(asdict(self), service_effect_verified=self.service_effect_verified,
                    improvement_comparable=self.improvement_comparable)
