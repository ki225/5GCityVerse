"""Population commands have a delta journal and never enter global compensation."""
from __future__ import annotations
import asyncio
import hashlib
import json
import time

from .domain import ContractError, ServiceIntent
from .repository import IntentConflict

FORWARD_STATUSES=frozenset({'queued','running','recovery_required'})


class PopulationForwardStopped(ContractError):
    """Current durable full-release/terminal authority owns this command."""


def assert_population_open(repository,intent_id,operation_id,owner=None):
    """Current row + fleet fence in one bounded read; no history or native I/O."""
    with repository.connect() as db:
        repository._assert_fleet_open(db,intent_id,operation_id)
        if operation_id is not None:
            row=db.execute('SELECT kind,status,intent_id,owner FROM operations WHERE id=?',(operation_id,)).fetchone()
            if (not row or row['kind']!='population_change' or row['status'] not in FORWARD_STATUSES
                    or row['intent_id']!=intent_id or row['owner']!=owner):
                raise PopulationForwardStopped('population_forward_authority_closed')


def record_population(service,operation_id,owner,*,status=None,stage=None,**data):
    """CAS only current forward work; never resurrect full-release authority.

    BEGIN/SELECT/fence/update is one short transaction, with no model/native
    call. Quiescence can win before or after it; no stale worker overwrites it.
    """
    with service.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,owner)).fetchone()
        if not row or row['kind']!='population_change' or row['status'] not in FORWARD_STATUSES:
            return False
        try:service.repository._assert_fleet_open(db,row['intent_id'],operation_id)
        except IntentConflict:return False
        value=json.loads(row['value']);value.update(data)
        return db.execute('UPDATE operations SET status=?,stage=?,value=?,updated=? '
            'WHERE id=? AND owner=? AND kind=? AND status=? AND stage=? AND value=? AND updated=?',
            (status or row['status'],stage or row['stage'],json.dumps(value,allow_nan=False),time.time(),
             operation_id,owner,row['kind'],row['status'],row['stage'],row['value'],row['updated'])).rowcount==1


def commissioned(adapter,request):
    capabilities=getattr(adapter,'capabilities',{})
    return (getattr(adapter,'commissioned',False) is True and isinstance(capabilities,dict)
        and capabilities.get('configured') is True
        and (not any(request['add_counts'].values()) or capabilities.get('fleet_join') is True)
        and (not request['remove_device_ids'] or capabilities.get('fleet_leave') is True))


async def queue_population(service,intent_id,body,owner,key,expected_revision,*,route_constraint=None,parent_authority=None,replacement_constraint=None):
    from .service import invoke
    from backend.onprem.scenarios.population import validate_request
    if service.closed:
        raise IntentConflict('Runtime is draining')
    if service.population_coordinator is None or not getattr(service.population_coordinator,'configured',False):
        raise ContractError('population_change_not_configured')
    if type(expected_revision) is not int or expected_revision<1:
        raise IntentConflict('If-Match intent revision is required')
    if not isinstance(key,str) or not 1<=len(key)<=256:
        raise ContractError('Idempotency-Key is required (1..256 characters)')
    try:
        request=validate_request(body)
    except (ValueError,TypeError) as error:
        raise ContractError('invalid_population_change') from error
    if not commissioned(service.population_coordinator,request) and any(request['add_counts'].values()):
        raise ContractError('population_change_not_commissioned')
    current=await invoke(service.repository.get,intent_id,owner)
    if current['revision']!=expected_revision:
        raise IntentConflict('Intent revision changed before population command')
    intent=ServiceIntent.from_dict({k:v for k,v in current.items() if k in ServiceIntent.__dataclass_fields__})
    if current.get('status')=='expired' or not intent.is_active(service.clock()):
        raise IntentConflict('Population command requires an active intent')
    delegated_scopes=[]
    if parent_authority is not None:
        from .session_constraints import PopulationParentAuthority
        if type(parent_authority) is not PopulationParentAuthority:
            raise ContractError('typed_population_parent_authority_required')
        if parent_authority.intent_id!=intent_id or parent_authority.intent_revision!=intent.revision:
            raise ContractError('population_parent_intent_changed')
        with service.repository.connect() as db:
            parent=db.execute('SELECT owner,intent_id,status FROM operations WHERE id=?',(parent_authority.parent_operation_id,)).fetchone()
            if (not parent or parent['owner']!=owner or parent['intent_id']!=intent_id or parent['status']!='running'):
                raise ContractError('population_parent_authority_changed')
            service.repository._assert_fleet_open(db,intent_id,parent_authority.parent_operation_id)
            delegated_scopes=[r[0] for r in db.execute('SELECT scope FROM leases WHERE operation_id=?',(parent_authority.parent_operation_id,))]
    if route_constraint is not None:
        from .session_constraints import NewSessionRouteConstraint
        if type(route_constraint) is not NewSessionRouteConstraint:
            raise ContractError('typed_server_route_constraint_required')
        if (route_constraint.intent_id!=intent_id or route_constraint.intent_revision!=intent.revision
                or not any(request['add_counts'].values()) or request['remove_device_ids']):
            raise ContractError('route_constraint_population_scope_changed')
        with service.repository.connect() as db:
            parent=db.execute('SELECT owner,intent_id,status FROM operations WHERE id=?',(route_constraint.parent_operation_id,)).fetchone()
        if (not parent or parent['owner']!=owner or parent['intent_id']!=intent_id or parent['status'] not in {'running','recovery_required'}):
            raise ContractError('route_constraint_parent_authority_changed')
    if replacement_constraint is not None:
        from .session_replacement import ExactSessionReplacement
        if (type(replacement_constraint) is not ExactSessionReplacement or parent_authority is None or route_constraint is None
                or replacement_constraint.parent!=parent_authority.to_dict()
                or sum(request['add_counts'].values())!=1 or request['remove_device_ids']):
            raise ContractError('typed_parent_exact_replacement_required')
    traffic=await invoke(service.repository.current_intent_traffic,intent_id,owner)
    if not traffic:
        raise ContractError('population_change_requires_owned_traffic')
    guard=getattr(service.population_coordinator,'validate_population',None)
    if not callable(guard):raise ContractError('population_receipt_authority_not_configured')
    # Resolve only the exact idempotency row, not operation history. A resumed
    # delta may already have added its devices to the current allocation.
    with service.repository.connect() as db:
        previous=db.execute('SELECT id FROM operations WHERE owner=? AND key=?',(owner,key)).fetchone()
    await invoke(guard,intent,owner,request,previous[0] if previous else None)
    # The current If-Match fence authorizes this call; an existing key owns its
    # original immutable revision across trusted traffic-lease renewal.
    fingerprint=hashlib.sha256(json.dumps(dict(intent_id=intent_id,
        kind='population_change',request=request,**({'route_constraint':route_constraint.to_dict()} if route_constraint else {}),
        **({'parent_authority':parent_authority.to_dict()} if parent_authority else {}),
        **({'replacement_constraint':replacement_constraint.to_dict()} if replacement_constraint else {})),sort_keys=True,allow_nan=False).encode()).hexdigest()
    operation=await invoke(service.repository.submit,intent.to_dict(),owner,key,fingerprint,
        kind='population_change',metadata={'population_request':request,'population_intent':intent.to_dict(),
            'population_idempotency_key':key,
            **({'population_route_constraint':route_constraint.to_dict()} if route_constraint else {}),
            **({'population_replacement_constraint':replacement_constraint.to_dict()} if replacement_constraint else {}),
            **({'population_parent_authority':parent_authority.to_dict(),'population_delegated_scopes':delegated_scopes} if parent_authority else {})})
    if operation['status']=='recovery_required':
        await invoke(record_population,service,operation['operation_id'],owner,status='queued',stage='population_recovering',error=None)
        operation=await invoke(service.repository.operation,operation['operation_id'],owner)
    service._schedule(operation,owner,recover=operation['status']=='running')
    return dict(intent_id=intent_id,operation_id=operation['operation_id'],status=operation['status'])


async def run_population(service,operation,owner,*,recover=False):
    from .service import invoke
    op_id=operation['operation_id']
    async def record(**fields):
        if not await invoke(record_population,service,op_id,owner,**fields):
            raise PopulationForwardStopped('population_forward_authority_closed')
    try:
        operation=await invoke(service.repository.operation,op_id,owner)
        if operation['status'] not in FORWARD_STATUSES:return
        await invoke(assert_population_open,service.repository,operation['intent_id'],op_id,owner)
        adapter=service.population_coordinator
        if adapter is None or not getattr(adapter,'configured',False):
            raise ContractError('population_change_not_configured')
        if not commissioned(adapter,operation['population_request']) and any(operation['population_request']['add_counts'].values()):
            raise ContractError('population_change_not_commissioned')
        # The original contract and request are immutable. Adapter current_authority
        # resolves renewal lineage without issuing a new delta after a crash.
        intent=ServiceIntent.from_dict(operation['population_intent'])
        await record(status='running',stage='population_changing')
        while not service.stop.is_set():
            await invoke(assert_population_open,service.repository,intent.intent_id,op_id,owner)
            guard=getattr(adapter,'validate_population',None)
            if not callable(guard):raise ContractError('population_receipt_authority_not_configured')
            await invoke(guard,intent,owner,operation['population_request'],op_id)
            operation=await invoke(service.repository.operation,op_id,owner)
            if operation.get('population_topology_complete') is True and operation.get('population_parent_authority'):
                verify=getattr(adapter,'verify_parent_population_installation',None)
                if not callable(verify):raise ContractError('parent_population_installation_verifier_unavailable')
                proof=await invoke(verify,service,operation,owner)
                if not isinstance(proof,dict) or proof.get('installation_confirmed') is not True or proof.get('service_effect_verified') is not False:
                    raise ContractError('parent_population_installation_unconfirmed')
                await record(status='completed',stage='population_installed_for_parent_verification',
                    installation_confirmed=True,service_effect_verified=False,parent_installation=proof,
                    requires_parent_verification=proof['parent_operation_id'],
                    population={'pending':False,'phase':'installation_only'})
                return
            if operation.get('population_topology_complete') is True:
                verify=getattr(adapter,'verify_population',None)
                if not callable(verify):raise ContractError('population_post_topology_verifier_not_configured')
                verified=await invoke(verify,service,operation,owner)
                if verified is True:
                    await record(status='completed',stage='population_completed',
                        population={'pending':False,'phase':'verified'})
                    return
                await record(status='running',stage='population_verifying',population={'pending':True,'phase':'verifying'})
                try:await asyncio.wait_for(service.stop.wait(),timeout=service.readback_poll_seconds)
                except asyncio.TimeoutError:pass
                continue
            proof=await invoke(adapter.change,intent,owner,op_id,operation['population_request'],
                idempotency_key=operation['population_idempotency_key'])
            if (not isinstance(proof,dict) or proof.get('operation_id')!=op_id
                    or type(proof.get('pending')) is not bool or not isinstance(proof.get('phase'),str)):
                raise ContractError('population_change_readback_unconfirmed')
            # Never copy private leases, admission handles, devices or policy
            # receipts out of the authoritative adapter's journal.
            await record(status='running',
                stage='population_pending' if proof['pending'] else 'population_needs_measurement',
                population_topology_complete=not proof['pending'],
                population={'phase':proof['phase'],'pending':True})
            if not proof['pending']:
                continue
            try:
                await asyncio.wait_for(service.stop.wait(),timeout=service.readback_poll_seconds)
            except asyncio.TimeoutError:
                pass
    except PopulationForwardStopped:
        return
    except Exception as error:
        # The delta may already be installed or retired. Only adapter readback /
        # forward recovery can decide; whole-fleet release is never compensation.
        await invoke(record_population,service,op_id,owner,status='recovery_required',stage='population_recovery_required',
            error={'code':'population_change_recovery_required','category':type(error).__name__},
            population={'pending':True,'recovery_required':True})
