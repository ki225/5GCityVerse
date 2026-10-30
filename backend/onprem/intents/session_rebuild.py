"""Interrupted, bounded rebuild of complete owned single-PDU devices.

Uses the commissioned population remove/add controller and native admissions;
does not migrate an established session or restart an unrelated UE fleet.
"""
import asyncio
import hashlib
import json
import time

from .actuators import ActuationBlocked, Capability
from .domain import ServiceIntent
from .pending_admission import AdmissionInstallation
from .service_routes import _json
from .native_service_routes import _stamp
from backend.onprem.runtime.current_sessions import V2, session_rows
from backend.onprem.runtime.topology import SLICES


class SessionRebuildProvider:
    actions = ('reestablish_sessions',)
    def __init__(self, context, registry, *, clock=time.time):
        self.context, self.registry, self.clock = context, registry, clock
        with registry.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS service_session_rebuilds (id TEXT PRIMARY KEY,value TEXT NOT NULL)')

    def capabilities(self):
        service = getattr(self.context, 'intent_service', None)
        adapter = getattr(service, 'population_coordinator', None)
        loop = getattr(self.context, 'intent_event_loop', None) or getattr(service, 'event_loop', None)
        ready = (getattr(adapter, 'commissioned', False) is True and loop is not None
            and not loop.is_closed() and callable(getattr(service, 'change_population', None))
            and getattr(service, 'admission_coordinator', None) is not None)
        return (Capability('reestablish_sessions', 'configured' if ready else 'not_configured',
            'Commissioned V2 complete-owned single-device/single-PDU rebuild only; same service profile required; '
            'formal FreeRAN geographic rebuild requires a matching commissioned single-PDU vendor profile, '
            'operator gNB endpoint allowlist and current reciprocal native attachment; same geography/offered demand preserved; '
            'explicit interruption budget required; no transparent migration'),)

    def _key(self, request):
        return request.operation_id + ':' + request.target_id

    def _service_owner(self, request):
        service = self.context.intent_service
        with service.repository.connect() as db:
            row = db.execute('SELECT owner,intent_id FROM operations WHERE id=?', (request.operation_id,)).fetchone()
        if row is None or row['intent_id'] != request.owner_id:
            raise ActuationBlocked('Rebuild requires the current exact intent operation')
        return service, row['owner']

    def _snapshot(self):
        return self.context.round_ledger.live_snapshot()

    def _contract(self, request):
        service, owner = self._service_owner(request)
        current = service.repository.get(request.owner_id, owner)
        intent = ServiceIntent.from_dict({k:v for k,v in current.items() if k in ServiceIntent.__dataclass_fields__})
        if not intent.is_active(self.clock()) or not intent.continuity_policy.allow_session_rebuild:
            raise ActuationBlocked('Intent forbids interrupted session rebuild')
        budget = intent.continuity_policy.maximum_interruption_seconds
        if not 0 < budget <= 300:
            raise ActuationBlocked('Rebuild requires a positive interruption budget of at most 300 seconds')
        return service, owner, intent, budget

    def inspect(self, request):
        with self.registry.db() as db:
            row = db.execute('SELECT value FROM service_session_rebuilds WHERE id=?', (self._key(request),)).fetchone()
        snapshot = self._snapshot()
        return {'source': 'native-owned-session-rebuild/v1', 'version': str(snapshot.get('populationRevision', 0)) if row is None else json.loads(row[0])['version'],
            'execution': None if row is None else json.loads(row[0])}

    def preflight(self, request, before):
        _, _, _, budget = self._contract(request)
        if set(request.parameters) != {'round_id', 'generation', 'expected_population_revision'}:
            raise ActuationBlocked('Exact round/generation/population revision required for rebuild')
        snapshot = self._snapshot()
        if (snapshot.get('schemaVersion') != V2 or snapshot.get('roundId') != request.parameters['round_id']
                or snapshot.get('generation') != request.parameters['generation']
                or str(snapshot.get('populationRevision', 0)) != request.expected_version
                or snapshot.get('populationRevision', 0) != request.parameters['expected_population_revision']):
            raise ActuationBlocked('Rebuild native population fence changed')
        rows = session_rows(snapshot)
        selected = [r for r in rows if r['sessionKey'] in request.session_keys]
        devices = {r['deviceId'] for r in selected}
        if not selected or len(selected) != len(request.session_keys) or len(devices) != 1:
            raise ActuationBlocked('Exact existing session cohort required')
        if any(sum(r['deviceId'] == device for r in rows) != 1 for device in devices):
            raise ActuationBlocked('Partial or multi-PDU device rebuild is unsupported')
        service, owner, intent, _ = self._contract(request)
        geographic=bool(snapshot.get('spatialDemand') or snapshot.get('allocation',{}).get('demandGroups'))
        if geographic and not callable(getattr(getattr(service.population_coordinator,'native_callbacks',None),'prepare_session_replacement',None)):
            raise ActuationBlocked('Geographic rebuild requires the commissioned exact replacement bridge')
        # The current native classifications must match this intent; owning a
        # generation is not authority to interrupt its other services.
        types = set(intent.selector.service_types)
        if any(getattr(intent.selector, name) for name in ('regions', 'applications', 'supis', 'slice_ids')):
            raise ActuationBlocked('Rebuild subset requires an exact service-type-only intent selector')
        from .service_classification import matches_service_types
        reader=getattr(self.context,'intent_session_binding_reader',None)
        # Reuse the independently authenticated native census/launch join used
        # by normal and protected-service membership. Raw perSession profile
        # labels are not sufficient authority to interrupt a device.
        try:
            proofs=reader.current_bindings(snapshot) if reader is not None else {}
            classifications=[proofs[(r['supi'],str(r['sessionId']))]['binding'] for r in selected]
        except Exception as error:
            raise ActuationBlocked('Rebuild requires current authenticated service classification') from error
        if not types or len(classifications)!=len(selected) or any(
                not matches_service_types(types,c['service_type'],c.get('service_profile_id',''))
                for c in classifications):
            raise ActuationBlocked('Rebuild device falls outside the intent service selector')
        with self.registry.db() as db:
            flows = [json.loads(r[0]) for r in db.execute('SELECT value FROM service_route_flows')]
        if any(f['session_key'] in request.session_keys for f in flows):
            raise ActuationBlocked('Rebuild requires independently drained owned service flows')
        return {'version': before['version'], 'old_session_keys': list(request.session_keys),
            'removed_device_ids': sorted(devices), 'budget_seconds': budget,
            'geographic':geographic,
            'demand_profile': {k:selected[0].get(k) for k in ('service_type','region','dnn','snssai','logicalSessionId','slice')},
            'add_counts': {name:sum(r['slice']==name for r in selected) for name in SLICES}}

    def _save(self, key, value):
        with self.registry.db() as db:
            db.execute('INSERT OR REPLACE INTO service_session_rebuilds VALUES (?,?)', (key, _json(value)))

    async def _wait(self, service, operation, owner, deadline):
        while self.clock() < deadline:
            value = await asyncio.to_thread(service.repository.operation, operation['operation_id'], owner)
            if value['status'] == 'completed':
                return value
            if value['status'] in {'failed','recovery_required','released','cancelled'}:
                raise ActuationBlocked('Population child operation requires recovery')
            await asyncio.sleep(min(.25, max(0, deadline-self.clock())))
        raise ActuationBlocked('Session rebuild interruption budget exhausted')

    async def _run(self, request, value, checks):
        service, owner, intent, _ = self._contract(request)
        deadline = value['started_at'] + checks['budget_seconds']
        before = self._snapshot()
        replacement=None
        if checks['geographic']:
            try:
                replacement=await service.population_coordinator.native_callbacks.prepare_session_replacement(
                    intent=intent,request=request,snapshot=before,
                    session=next(r for r in session_rows(before) if r['sessionKey'] in request.session_keys))
            except Exception as error:
                raise ActuationBlocked('Exact geographic replacement could not be validated before removal') from error
            value['replacement_constraint']=replacement.to_dict();self._save(self._key(request),value)
        removal = {'expected_population_revision':before['populationRevision'],
            'add_counts':dict.fromkeys(SLICES,0),'remove_device_ids':checks['removed_device_ids']}
        # change_population keeps the authoritative typed reservation, native
        # nonce, admission handles and identity-preserving delta journal.
        value.update(phase='removal_dispatch');self._save(self._key(request),value)
        from .session_constraints import PopulationParentAuthority
        parent_authority=PopulationParentAuthority(request.operation_id,request.owner_id,intent.revision,
            request.parameters['round_id'],request.parameters['generation'])
        removed = await service.change_population(request.owner_id,removal,owner,self._key(request)+':remove',intent.revision,parent_authority=parent_authority)
        value['removal_operation_id']=removed['operation_id'];self._save(self._key(request),value)
        await self._wait(service,removed,owner,deadline)
        census = await asyncio.to_thread(service.registry.get('update_qos').provider.api.active_inventory)
        if (census.get('complete') is not True or census.get('source') != 'native-smf-active-sessions'
                or not 0<=self.clock()-_stamp(census.get('observed_at'))<=15
                or set(request.session_keys)&{s.get('session_key') for s in census.get('sessions',())}):
            raise ActuationBlocked('Fresh complete native absence proof required before replacement')
        middle=self._snapshot()
        if set(request.session_keys)&{s['sessionKey'] for s in session_rows(middle)}:
            raise ActuationBlocked('Old sessions remain in current native population')
        survivors={s['sessionKey'] for s in session_rows(middle)}
        addition={'expected_population_revision':middle['populationRevision'],'add_counts':checks['add_counts'],'remove_device_ids':[]}
        if self.clock()>=deadline:
            raise ActuationBlocked('Session rebuild interruption budget exhausted before replacement')
        value.update(phase='replacement_dispatch');self._save(self._key(request),value)
        from .session_constraints import NewSessionRouteConstraint
        constraint=NewSessionRouteConstraint(request.operation_id,request.owner_id,intent.revision,
            request.parameters['round_id'],request.parameters['generation'],request.target_id)
        added=await service.change_population(request.owner_id,addition,owner,self._key(request)+':add',intent.revision,route_constraint=constraint,parent_authority=parent_authority,
            **({'replacement_constraint':replacement} if replacement is not None else {}))
        value['addition_operation_id']=added['operation_id'];self._save(self._key(request),value)
        await self._wait(service,added,owner,deadline)
        after=self._snapshot()
        new=sorted({s['sessionKey'] for s in session_rows(after)}-survivors)
        if len(new)!=len(request.session_keys) or set(new)&set(request.session_keys):
            raise ActuationBlocked('Replacement native session lineage incomplete')
        lineage=[]
        for old,key in zip(sorted(request.session_keys),new):
            path=self.registry.session(key)
            if path['upf_target_id']!=request.target_id:
                raise ActuationBlocked('Replacement session did not select the planned UPF')
            with service.repository.connect() as db:
                handle=db.execute('SELECT value FROM population_current_admissions WHERE session_key=?',(key,)).fetchone()
            if not handle:
                raise ActuationBlocked('Replacement session lacks a retained native admission nonce')
            installation=AdmissionInstallation.from_dict(json.loads(handle[0]))
            binding=await asyncio.to_thread(service.admission_coordinator.read_binding,installation)
            if binding is None or binding.session_key!=key or installation.admission.owner_id!=request.owner_id:
                raise ActuationBlocked('Replacement session native nonce binding differs')
            if replacement is not None and installation.admission.identity.nonce in replacement.old_nonces:
                raise ActuationBlocked('Replacement reused an old native admission nonce')
            native_row=next(r for r in session_rows(after) if r['sessionKey']==key)
            if {k:native_row.get(k) for k in checks['demand_profile']}!=checks['demand_profile']:
                raise ActuationBlocked('Replacement service/region/session launch profile differs from the authorized demand')
            if replacement is not None:
                from backend.onprem.runtime.population import HeldPopulationDelta
                from backend.onprem.scenarios.population import read
                record=await asyncio.to_thread(read,service.population_coordinator.ledger,added['operation_id'],owner)
                replacement.validate_published(after,HeldPopulationDelta.from_dict(record['delta']))
            lineage.append({'old_session_key':old,'new_session_key':key,'nonce':installation.admission.identity.nonce,
                'installation_version':installation.native_version,'path':path,
                'native_session':{k:native_row[k] for k in ('sessionKey','deviceId','supi','podUid','containerId','processEpoch',
                    'generation','establishedEpoch','smContextRef','upfEpoch','nativeUpfId')},
                'round_id':after['roundId'],'generation':after['generation'],'population_revision':after['populationRevision']})
            if replacement is not None:lineage[-1]['preserved_demand']=replacement.demand_profile | replacement.added_metadata()
        value.update(phase='complete',state='complete',version=str(after['populationRevision']),new_session_keys=new,
            native_admission_bindings=lineage,round_id=after['roundId'],generation=after['generation'],
            population_revision=after['populationRevision'],finished_at=self.clock(),interruption_seconds=self.clock()-value['started_at'],
            receiver_effect_verified=False)
        if self.clock()>deadline:
            raise ActuationBlocked('Replacement completed outside authorized interruption budget')
        self._save(self._key(request),value)
        return value

    def mutate(self, request, before):
        checks=self.preflight(request,before)
        loop=getattr(self.context,'intent_event_loop',None) or getattr(self.context.intent_service,'event_loop',None)
        if loop is None or loop.is_closed():
            raise ActuationBlocked('Owning intent event loop is unavailable')
        digest=hashlib.sha256(_json(request.to_dict()).encode()).hexdigest()
        with self.registry.db() as db:
            prior=db.execute('SELECT value FROM service_session_rebuilds WHERE id=?',(self._key(request),)).fetchone()
            if prior:
                old=json.loads(prior[0])
                if old['request_digest']!=digest:raise ActuationBlocked('Rebuild replay differs')
                if old.get('state')=='complete':return old
                raise ActuationBlocked('Interrupted rebuild requires child-operation readback, not redispatch')
            value={'state':'dispatching','phase':'prepared','version':before['version'],'request_digest':digest,
                'owner_id':request.owner_id,'operation_id':request.operation_id,'old_session_keys':list(request.session_keys),
                'started_at':self.clock()}
            db.execute('INSERT INTO service_session_rebuilds VALUES (?,?)',(self._key(request),_json(value)))
        future=asyncio.run_coroutine_threadsafe(self._run(request,value,checks),loop)
        # A timeout never cancels/forgets in-flight native child operations.
        return future.result(timeout=checks['budget_seconds']+5)

    def observe(self, request, mutation):return self.inspect(request)
    def confirmed(self, request, observed):
        v=observed.get('execution') or {}
        return v.get('state')=='complete' and v.get('owner_id')==request.owner_id and v.get('operation_id')==request.operation_id
    def resolve_effect_session_keys(self, receipt):
        v=receipt.installed.get('execution') or {}
        if not self.confirmed(receipt.request,receipt.installed):return None
        return tuple(v['new_session_keys'])
    def restore(self, request, before, installed):
        raise ActuationBlocked('An interrupted rebuild needs a new authorized compensating admission; original sessions cannot be restored')
