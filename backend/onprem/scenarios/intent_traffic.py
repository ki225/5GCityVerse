"""Private, durable ownership contract for formal NONE scenario traffic.

Neither a public request field nor an elapsed timer is cleanup authority. Native
intent restoration must finish before the owning UE generation can be retired.
"""
from dataclasses import asdict, dataclass
import json
import time
from uuid import uuid5, NAMESPACE_URL

from backend.onprem.intents.domain import ContractError, required, timestamp


def lease_identity(lease):
    """Immutable ownership; a renewal is not a new launch or request."""
    if type(lease) is not ServiceIntentTrafficLease:
        raise ContractError('Trusted typed service traffic lease required')
    return {key: getattr(lease, key) for key in
        ('lease_id', 'intent_id', 'owner_id', 'operation_id', 'request_id', 'valid_from')}


def lease_renewal_follows(before, after):
    """Pure lineage check, never authority to renew or reactivate a lease."""
    return (lease_identity(before) == lease_identity(after)
        and after.intent_revision >= before.intent_revision and after.valid_until >= before.valid_until)


@dataclass(frozen=True)
class ServiceIntentTrafficLease:
    lease_id: str
    intent_id: str
    intent_revision: int
    owner_id: str
    operation_id: str
    request_id: str
    valid_from: float
    valid_until: float

    def __post_init__(self):
        for field in ('lease_id','intent_id','owner_id','operation_id','request_id'):
            required(getattr(self,field),field)
        if type(self.intent_revision) is not int or self.intent_revision<1:
            raise ContractError('Invalid traffic lease intent revision')
        object.__setattr__(self,'valid_from',timestamp(self.valid_from))
        object.__setattr__(self,'valid_until',timestamp(self.valid_until))
        if self.valid_until<=self.valid_from:
            raise ContractError('Invalid traffic lease validity')

    @classmethod
    def issue(cls,intent,owner,operation_id,request_id):
        identity=json.dumps([intent.intent_id,intent.revision,owner,operation_id,request_id])
        return cls(str(uuid5(NAMESPACE_URL,identity)),intent.intent_id,intent.revision,owner,operation_id,
                   request_id,intent.valid_from,intent.valid_until)

    def active(self,now):
        return self.valid_from<=now<self.valid_until

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__):
            raise ContractError('Invalid durable service traffic lease')
        return cls(**value)


@dataclass(frozen=True)
class IntentTrafficRestoration:
    lease_id: str
    intent_id: str
    intent_revision: int
    owner_id: str
    operation_id: str
    request_id: str
    release_operation_id: str
    observed_at: float
    restored: bool = True
    source: str = 'intent-service-owner-safe-release/v1'

    @classmethod
    def for_lease(cls,lease,release_operation_id):
        """Called privately only after IntentService proves full native restoration."""
        return cls(**{k:lease.to_dict()[k] for k in ('lease_id','intent_id','intent_revision',
            'owner_id','operation_id','request_id')},release_operation_id=release_operation_id,
            observed_at=time.time())

    def validate(self,lease,now):
        if self.source!='intent-service-owner-safe-release/v1' or self.restored is not True:
            raise ContractError('Native intent restoration has not completed')
        required(self.release_operation_id,'release_operation_id')
        for field in ('lease_id','intent_id','intent_revision','owner_id','operation_id','request_id'):
            if getattr(self,field)!=getattr(lease,field):
                raise ContractError('Restoration does not own this traffic lease')
        if not 0<=now-timestamp(self.observed_at)<=30:
            raise ContractError('Intent restoration proof is stale')

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class IntentTrafficDrainPermit:
    """Quiesced admission/policy authority, explicitly not final restoration."""
    lease_id: str
    intent_id: str
    intent_revision: int
    owner_id: str
    operation_id: str
    request_id: str
    release_operation_id: str
    observed_at: float
    source: str = 'intent-service-quiesced/v1'
    policies_restored: bool = True
    admissions_closed: bool = True

    def validate(self,lease,now):
        if (self.source!='intent-service-quiesced/v1' or self.policies_restored is not True
                or self.admissions_closed is not True):
            raise ContractError('Native policy/admission quiescence is not confirmed')
        # Reuse lineage/freshness checks with the distinct final-proof source;
        # the typed drain permit itself is never accepted by final release.
        final=IntentTrafficRestoration(**{k:v for k,v in self.to_dict().items()
            if k in IntentTrafficRestoration.__dataclass_fields__ and k!='source'})
        final.validate(lease,now)

    @classmethod
    def for_lease(cls,lease,release_operation_id):
        values=IntentTrafficRestoration.for_lease(lease,release_operation_id).to_dict()
        return cls(**{k:v for k,v in values.items() if k not in {'restored','source'}})

    def to_dict(self):
        return asdict(self)


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS intent_traffic_leases '
        '(round_id TEXT PRIMARY KEY, lease TEXT NOT NULL, state TEXT NOT NULL, restoration TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS intent_traffic_lease_state_round '
        'ON intent_traffic_leases(state,round_id)')


def persist_lease(db,round_id,lease):
    if type(lease) is not ServiceIntentTrafficLease:
        raise ContractError('Trusted typed service traffic lease required')
    ensure_schema(db)
    db.execute('INSERT INTO intent_traffic_leases VALUES (?,?,?,NULL)',
        (round_id,json.dumps(lease.to_dict(),sort_keys=True),'active'))


def read_lease(db,round_id):
    # Existing old ledgers have no formal leases. Reading them creates no schema.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_traffic_leases'").fetchone():
        return None
    row=db.execute('SELECT lease,state,restoration FROM intent_traffic_leases WHERE round_id=?',(round_id,)).fetchone()
    return None if row is None else dict(lease=json.loads(row[0]),state=row[1],
                                        restoration=json.loads(row[2]) if row[2] else None)
