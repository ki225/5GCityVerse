"""A traffic creator cannot retire physical devices still serving other intents."""
import json
import sqlite3
from .domain import ContractError, ServiceIntent, SessionKey


class FleetDependencyBlocked(ContractError):
    pass


async def assert_independent_fleet_lifetime(service,intent,owner):
    from .service import invoke
    traffic=await invoke(service.repository.current_intent_traffic,intent.intent_id,owner)
    if not traffic:
        return  # A policy-only intent does not own a native fleet lifetime.
    if not all(callable(getattr(service.repository,name,None)) for name in
            ('dependency_version','begin_fleet_closing','assert_fleet_open')):
        raise FleetDependencyBlocked('fleet_closing_repository_upgrade_required')
    try:
        epoch=await invoke(service.repository.dependency_version)
    except sqlite3.OperationalError:
        raise FleetDependencyBlocked('fleet_closing_repository_state_unavailable') from None
    others=[]
    for other_owner,raw in await invoke(lambda:list(service.repository.iter_active())):
        if raw['intent_id']!=intent.intent_id:
            candidate=ServiceIntent.from_dict({k:v for k,v in raw.items() if k in ServiceIntent.__dataclass_fields__})
            if candidate.is_active(service.clock()):others.append(candidate)
    claims=await invoke(service.repository.other_current_policy_sessions,intent.intent_id)
    if not others and not claims:
        return epoch
    snapshot=await service._snapshot(intent)
    request_id=traffic.get('receipt',{}).get('requestId')
    if not request_id or snapshot.traffic_request_id!=request_id:
        raise FleetDependencyBlocked('owned_fleet_scope_unconfirmed')
    # The native current snapshot is the whole owned traffic request, never the
    # releasing intent's selector. Every co-resident PDU belongs to the device.
    devices={(s.key.network_scope,s.key.supi) for s in snapshot.sessions}
    for key in claims:
        try:
            fields=json.loads(key)
            if not isinstance(fields,list) or len(fields)!=4:
                raise ValueError
            native=SessionKey(*fields)
            if native.token!=key:
                raise ValueError
            scope,supi=native.network_scope,native.supi
        except (TypeError,ValueError,ContractError):
            raise FleetDependencyBlocked('dependent_policy_identity_unconfirmed') from None
        if (scope,supi) in devices:
            raise FleetDependencyBlocked('owned_fleet_has_dependent_intents')
    for candidate in others:
        membership=service.membership.resolve(candidate,snapshot.sessions)
        if membership.rejected:
            raise FleetDependencyBlocked('dependent_membership_unconfirmed')
        if membership.members:
            raise FleetDependencyBlocked('owned_fleet_has_dependent_intents')
    return epoch


async def close_owned_fleet(service,intent,owner,operation_id):
    from .service import invoke
    from .repository import IntentConflict
    epoch=await assert_independent_fleet_lifetime(service,intent,owner)
    if epoch is None:return
    # A pending owned delta must first stop future dispatch and account for its
    # unknown native commands. Its leases transfer to this SAME full release;
    # unrelated outstanding operations still block the closing transaction.
    quiesce=getattr(getattr(service,'population_coordinator',None),'quiesce_for_release',None)
    if callable(quiesce):
        await invoke(quiesce,intent,owner,operation_id)
        epoch=await assert_independent_fleet_lifetime(service,intent,owner)
    try:
        await invoke(service.repository.begin_fleet_closing,intent.intent_id,owner,operation_id,epoch)
    except IntentConflict as error:
        reasons={
            'Fleet dependency epoch changed before closing':'fleet_closing_waiting_for_dependency_epoch',
            'Fleet dependencies have outstanding operations':'fleet_closing_waiting_for_outstanding_operations',
            'Owned release operation required for fleet closing':'fleet_closing_authority_unconfirmed',
            'Native city fleet closing owner changed':'fleet_closing_owner_changed',
        }
        raise FleetDependencyBlocked(reasons.get(str(error),'fleet_closing_unconfirmed')) from None
