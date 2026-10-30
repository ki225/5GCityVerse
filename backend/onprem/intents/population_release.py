"""Exact-owner forward cleanup of an interrupted population transaction.

No timer grants authority and no uncertain native write is replayed. The caller
holds the adapter's async writer lock; SQLite transactions never span native I/O.
"""
import json

from .domain import ContractError
from .actuators import ActionReceipt,ActionRequest
from .service import invoke
from .service_policy_state import encoded
from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,lease_renewal_follows
from backend.onprem.scenarios.population import read
from backend.onprem.storage.current_state import current_reader


def owned_full_release(row):
    value=json.loads(row['value']) if row else {}
    return (row is not None and row['status'] in {'queued','running'}
        and (row['kind']=='release' or value.get('failure_cleanup') is True and value.get('release_purpose')=='full_release')
        and value.get('release_purpose')!='membership_withdrawal')


def authority(callbacks,intent,owner,release_id,pop_id,lease,delta,*,finish=False):
    """Freeze/finish only the exact current command; foreign operations stay busy."""
    record=delta_request(callbacks,pop_id,owner)
    with callbacks.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        release=db.execute('SELECT * FROM operations WHERE id=? AND intent_id=? AND owner=?',
            (release_id,intent.intent_id,owner)).fetchone()
        if not owned_full_release(release):
            raise ContractError('population_quiescence_requires_owned_full_release')
        traffic=db.execute('SELECT value FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
            (intent.intent_id,owner)).fetchone()
        traffic=json.loads(traffic[0]) if traffic else {}
        traffic_lease=traffic.get('receipt',{}).get('service_intent_traffic_lease')
        if (traffic.get('receipt',{}).get('requestId')!=lease.request_id
                or traffic.get('start_operation_id')!=lease.operation_id or not traffic_lease
                or not lease_renewal_follows(ServiceIntentTrafficLease.from_dict(traffic_lease),lease)):
            raise ContractError('population_quiescence_traffic_owner_changed')
        pop=db.execute('SELECT * FROM operations WHERE id=? AND intent_id=? AND owner=?',
            (pop_id,intent.intent_id,owner)).fetchone()
        if not pop or pop['kind']!='population_change' or pop['status'] not in {'queued','running','recovery_required','quiesced_for_release'}:
            raise ContractError('population_quiescence_operation_changed')
        operation_value=json.loads(pop['value'])
        if (operation_value.get('population_request')!=record['request']
                or operation_value.get('population_idempotency_key')!=record['idempotency_key']):
            raise ContractError('population_quiescence_operation_input_changed')
        row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(pop_id,)).fetchone()
        state=json.loads(row[0]) if row else None
        if (not state or (state['lease_id'],state['owner_intent_id'],state['delta_id'],state['delta_version'])
                !=(lease.lease_id,intent.intent_id,delta.delta_id,delta.version)
                or state.get('closing_release_operation_id') not in (None,release_id)):
            raise ContractError('population_quiescence_native_journal_changed')
        expected_owner=release_id if state.get('quiesced_for_release') else pop_id
        for scope in state.get('publication_scopes',()):
            held=db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
            if held is None or held[0]!=expected_owner:
                raise ContractError('population_quiescence_write_fence_lost')
        actual_scopes={r[0] for r in db.execute('SELECT scope FROM leases WHERE operation_id=?',(pop_id,))}
        if actual_scopes!=(set() if state.get('quiesced_for_release') else set(state.get('publication_scopes',()))):
            raise ContractError('population_quiescence_write_fence_changed')
        state['closing_release_operation_id']=release_id
        if finish:
            # Every native command was read back/restored while its original
            # exclusive lease remained held. Transfer, never TTL-steal/unlock.
            db.execute('UPDATE leases SET operation_id=? WHERE operation_id=?',(release_id,pop_id))
            state['quiesced_for_release']=True
            original=json.loads(pop['value'])
            original.update(population_cleanup_release_id=release_id)
            db.execute("UPDATE operations SET status='quiesced_for_release',stage='population_forward_cleanup',value=? WHERE id=?",
                (encoded(original),pop_id))
            callbacks.repository._advance_dependency_epoch(db)
        db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(state),pop_id))
        return state


def delta_request(callbacks,pop_id,owner):
    record=read(callbacks.ledger,pop_id,owner)
    if not record:raise ContractError('population_quiescence_delta_missing')
    return record


def quiesce_unreserved(callbacks,intent,owner,release_id,pop_id,lease,record):
    """A durable False marker proves the runtime reservation port never ran."""
    if (record.get('delta') is not None or record.get('reservation_dispatch_started') is not False
            or record.get('phase')!='reserved' or record.get('receipts')
            or delta_request(callbacks,pop_id,owner)!=record):
        raise ContractError('population_runtime_reservation_dispatch_unknown')
    with callbacks.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        release=db.execute('SELECT kind,status,value FROM operations WHERE id=? AND intent_id=? AND owner=?',
            (release_id,intent.intent_id,owner)).fetchone()
        if not owned_full_release(release):
            raise ContractError('population_quiescence_requires_owned_full_release')
        pop=db.execute('SELECT kind,status,value FROM operations WHERE id=? AND intent_id=? AND owner=?',
            (pop_id,intent.intent_id,owner)).fetchone()
        original=json.loads(pop[2]) if pop else {}
        if (not pop or pop[0]!='population_change' or pop[1] not in {'queued','running','recovery_required','quiesced_for_release'}
                or original.get('population_request')!=record['request']
                or original.get('population_idempotency_key')!=record['idempotency_key']
                or original.get('population_cleanup_release_id') not in (None,release_id)):
            raise ContractError('population_quiescence_operation_input_changed')
        traffic=db.execute('SELECT value FROM intent_traffic_resources WHERE intent_id=? AND owner=?',
            (intent.intent_id,owner)).fetchone()
        traffic=json.loads(traffic[0]) if traffic else {}
        raw=traffic.get('receipt',{}).get('service_intent_traffic_lease')
        if (traffic.get('start_operation_id')!=lease.operation_id or not raw
                or not lease_renewal_follows(ServiceIntentTrafficLease.from_dict(raw),lease)
                or db.execute('SELECT 1 FROM population_native_state WHERE operation_id=?',(pop_id,)).fetchone()
                or db.execute('SELECT 1 FROM leases WHERE operation_id=?',(pop_id,)).fetchone()):
            raise ContractError('population_pre_dispatch_absence_not_proven')
        original.update(population_cleanup_release_id=release_id,
            population_quiescence_source='durable-population-no-runtime-dispatch/v1')
        db.execute("UPDATE operations SET status='quiesced_for_release',stage='population_no_runtime_dispatch',value=? WHERE id=?",
            (encoded(original),pop_id))
        callbacks.repository._advance_dependency_epoch(db)
    return {'source':'owned-population-forward-cleanup/v1','quiesced':True,'operation_id':pop_id,
        'release_operation_id':release_id,'runtime_dispatched':False}


async def quiesce(callbacks,intent,owner,release):
    """Called before fleet closing, under the sole population dispatch lock."""
    release_id=release['operation_id'] if isinstance(release,dict) else release
    current=await invoke(current_reader(callbacks.ledger))
    entry=await invoke(callbacks.ledger.intent_traffic_lease)
    lease=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
    if lease is None or (lease.intent_id,lease.owner_id)!=(intent.intent_id,owner):
        raise ContractError('population_quiescence_lease_owner_changed')
    pop_id=current.get('populationPending',{}).get('operationId')
    if not pop_id:
        # Only presently retained write leases are inspected, never history.
        def retained():
            with callbacks.repository.connect() as db:
                return [r[0] for r in db.execute('SELECT DISTINCT n.operation_id FROM population_native_state n '
                    'JOIN leases l ON l.operation_id=n.operation_id WHERE n.owner_intent_id=? AND n.lease_id=? LIMIT 2',
                    (intent.intent_id,lease.lease_id))]
        ids=await invoke(retained)
        if not ids:return None
        if len(ids)!=1:raise ContractError('population_quiescence_current_operation_ambiguous')
        pop_id=ids[0]
    record=await invoke(read,callbacks.ledger,pop_id,owner)
    if not record:raise ContractError('population_quiescence_delta_missing')
    from backend.onprem.runtime.population import HeldPopulationDelta
    original=ServiceIntentTrafficLease.from_dict(record['lease'])
    watermark=ServiceIntentTrafficLease.from_dict(record.get('authority_lease',record['lease']))
    if (not lease_renewal_follows(original,lease) or not lease_renewal_follows(watermark,lease)
            or record['round_id']!=current.get('roundId') or current.get('generation')!=record['generation']
            or record['operation_id']!=pop_id):
        raise ContractError('population_quiescence_delta_lineage_changed')
    if record.get('delta') is None:
        return await invoke(quiesce_unreserved,callbacks,intent,owner,release_id,pop_id,lease,record)
    delta=HeldPopulationDelta.from_dict(record['delta'])
    if current.get('generation')!=delta.generation or delta.operation_id!=pop_id:
        raise ContractError('population_quiescence_delta_lineage_changed')
    state=await invoke(authority,callbacks,intent,owner,release_id,pop_id,lease,delta)
    async def guard():
        return await invoke(authority,callbacks,intent,owner,release_id,pop_id,lease,delta)
    if not state.get('quiesced_for_release'):
        # The sole runtime writer freezes its full old+added allocation; these
        # devices remain owned and MUST be included in the later whole drain.
        await invoke(callbacks.context.round_coordinator.resources.controller.quiesce_population,lease)
        await guard()
        restorations=dict(state.get('quiescence_commands',{}))
        for key,command in state['commands'].items():
            if not key.startswith('qos:'):continue
            request=ActionRequest.from_dict(command['request'])
            session=request.session_keys[0]
            _,effective=await invoke(callbacks.repository.qos_state,[session])
            committed=effective.get(session,{}).get('receipt',{}).get('request',{}).get('operation_id')==pop_id
            if committed:continue  # Normal shared-policy release owns restoration.
            previous=restorations.get(key)
            if previous:
                receipt=await invoke(callbacks.registry.compensation_readback,ActionReceipt.from_dict(previous))
                if receipt.state not in {'compensated','released'}:
                    # A fresh positive installation proof authorizes an exact
                    # owner/version CAS retry, not replay from a timer or error.
                    receipt=await invoke(callbacks.registry.readback,receipt)
                    if receipt.state not in {'installation_confirmed','effect_verified'}:
                        raise ContractError('population_quiescence_unknown_restoration')
                    await guard()
                    receipt=await invoke(callbacks.registry.compensate,receipt)
            else:
                native=await invoke(callbacks.registry.get('update_qos').provider.inspect,request)
                if native['version']==command['before']['version']:
                    if command.get('dispatched') is False:
                        continue  # Exact durable pre-dispatch failure, no external write.
                    # A baseline GET cannot exclude a request still in transit.
                    # Never guess absence for a dispatched/legacy unknown write.
                    raise ContractError('population_quiescence_unknown_native_dispatch')
                receipt=ActionReceipt.from_dict(command['receipt']) if command.get('receipt') else ActionReceipt(
                    request,'recovery_required',before=command['before'],started_at=callbacks.service.clock())
                receipt=await invoke(callbacks.registry.readback,receipt)
                if receipt.state not in {'installation_confirmed','effect_verified'}:
                    raise ContractError('population_quiescence_unknown_native_write')
                # Journal the compensation BEFORE dispatch; retries only read back.
                restorations[key]=receipt.to_dict()
                await invoke(callbacks.journal.save,pop_id,quiescence_commands=restorations)
                await guard()
                receipt=await invoke(callbacks.registry.compensate,receipt)
            restorations[key]=receipt.to_dict()
            await invoke(callbacks.journal.save,pop_id,quiescence_commands=restorations)
            if receipt.state not in {'compensated','released'}:
                raise ContractError('population_quiescence_native_restoration_pending')
        await invoke(callbacks.journal.drain_journal,lease,release_id)
        # A POST may have reached SMF before its installation handle was saved.
        # Recover that exact nonce by native readback; absence/foreign lineage
        # stays uncertain and blocks transfer. Never repeat registration.
        from .pending_admission import PendingAdmission
        known={h['admission']['identity']['nonce']:h for h in state['installations']}
        for command_id,command in state['commands'].items():
            if not command_id.startswith('admission:'):continue
            pending=PendingAdmission.from_dict(command['pending'])
            expected=known.get(pending.identity.nonce,{}).get('native_boot_epoch')
            handle=await invoke(callbacks.coordinator.recover_for_release,pending,pop_id,expected)
            await invoke(callbacks.journal.retain_handle,lease,handle)
        for handle,_ in await callbacks._all_handles(lease):
            await guard()
            await callbacks._cancel(handle,release_id,guard=guard)
        await invoke(authority,callbacks,intent,owner,release_id,pop_id,lease,delta,finish=True)
    return {'source':'owned-population-forward-cleanup/v1','quiesced':True,
        'operation_id':pop_id,'release_operation_id':release_id,'delta_id':delta.delta_id,'delta_version':delta.version}
