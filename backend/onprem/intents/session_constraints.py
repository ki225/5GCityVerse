"""Private authority references for a planned new-session UPF selection."""
from dataclasses import asdict, dataclass
from .domain import ContractError, required


@dataclass(frozen=True)
class NewSessionRouteConstraint:
    parent_operation_id: str
    intent_id: str
    intent_revision: int
    round_id: str
    generation: int
    target_id: str

    def __post_init__(self):
        for key in ('parent_operation_id','intent_id','round_id','target_id'):
            if len(required(getattr(self,key),key))>256:raise ContractError('route constraint identity too large')
        if type(self.intent_revision) is not int or self.intent_revision<1 or type(self.generation) is not int or self.generation<1:
            raise ContractError('invalid route constraint revision/generation')
    def to_dict(self):return asdict(self)
    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__):raise ContractError('invalid route constraint fields')
        return cls(**value)


@dataclass(frozen=True)
class PopulationParentAuthority:
    parent_operation_id: str
    intent_id: str
    intent_revision: int
    round_id: str
    generation: int

    def __post_init__(self):
        for key in ('parent_operation_id','intent_id','round_id'):
            if len(required(getattr(self,key),key))>256:raise ContractError('parent authority identity too large')
        if type(self.intent_revision) is not int or self.intent_revision<1 or type(self.generation) is not int or self.generation<1:
            raise ContractError('invalid parent authority revision/generation')
    def to_dict(self):return asdict(self)
    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__):raise ContractError('invalid parent authority fields')
        return cls(**value)
