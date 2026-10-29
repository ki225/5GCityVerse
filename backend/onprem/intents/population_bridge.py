"""Native population admission and deterministic current-policy inheritance.

Capacity remains an actual pre-traffic plan, not receiver evidence. Inherited
QER installation is explicitly not an observed service effect. Uncertain native
writes retain exact command journals and are inspected rather than replayed.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict,dataclass,replace
import hashlib
import json
import time

from .actuators import ActionRequest,ActionReceipt
from .domain import ContractError,ServiceIntent,SessionBinding,SessionKey,finite
from .effective_policy import EffectivePolicyCoordinator,QoSPolicyClaim
from .pending_admission import AdmissionInstallation,PendingAdmission,SessionAssignment
from .planning import PlanAction,ResourcePlan
from .pretraffic_planning import reserved_identities,selected_reserved_cohort
from .service import invoke,document
from .service_policy_state import (encoded,semantic_hash,validate_definition,validate_current_definition,
    observe_native,validate_observe_native,native_baseline)
from .service_policy_state import (baseline_supports_minimums,observes_effective_configuration,
    native_effective_configuration,effective_supports_observer,observer_configuration_cost)
from backend.onprem.runtime.current_sessions import session_rows
from backend.onprem.runtime.ue_reservation import UEAdmissionLaunchProfile
from backend.onprem.scenarios.population import ScenarioPopulationAdapter,current_authority
from backend.onprem.storage.current_state import current_reader


def digest(value): return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class PopulationReservation:
    """The actual held delta, never a second whole-fleet reservation."""
    reservation_id:str
    version:str
    network_scope:str
    created_at:float
    valid_until:float
    launch_profile_id:str
    allocation:tuple
    devices:tuple
    counts:tuple

    def to_dict(self): return asdict(self)


class PopulationJournal:
    def __init__(self,repository):
        self.repository=repository
        with repository.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS population_native_state '
                '(operation_id TEXT PRIMARY KEY,lease_id TEXT NOT NULL,owner_intent_id TEXT NOT NULL,value TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS population_native_owner ON population_native_state(owner_intent_id,lease_id)')
            db.execute('CREATE TABLE IF NOT EXISTS population_current_admissions '
                '(nonce TEXT PRIMARY KEY,lease_id TEXT NOT NULL,session_key TEXT,value TEXT NOT NULL)')

    def load(self,op):
        with self.repository.connect() as db:
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            return json.loads(row[0]) if row else None

    def initialize(self,lease,delta,op):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if row:
                state=json.loads(row[0])
                if (state['lease_id'],state['delta_id'],state['delta_version'])!=(lease.lease_id,delta.delta_id,delta.version):
                    raise ContractError('population_native_journal_identity_changed')
                return state
            state=dict(operation_id=op,lease_id=lease.lease_id,owner_intent_id=lease.intent_id,
                delta_id=delta.delta_id,delta_version=delta.version,commands={},installations=[],plans=[],
                admissions_closed=False,claims_committed=False)
            db.execute('INSERT INTO population_native_state VALUES(?,?,?,?)',(op,lease.lease_id,lease.intent_id,encoded(state)))
            return state

    def save(self,op,**patch):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if row is None: raise ContractError('population_native_journal_missing')
            value=json.loads(row[0]); value.update(deepcopy(patch))
            if len(encoded(value).encode())>8*1024*1024:
                raise ContractError('population_native_journal_bound_exceeded')
            db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(value),op))
            return value

    def retain_handle(self,lease,handle,key=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT lease_id,value FROM population_current_admissions WHERE nonce=?',
                (handle.admission.identity.nonce,)).fetchone()
            if row and (row[0]!=lease.lease_id or json.loads(row[1])!=handle.to_dict()):
                raise ContractError('population_admission_current_owner_changed')
            db.execute('INSERT OR REPLACE INTO population_current_admissions VALUES(?,?,?,?)',
                (handle.admission.identity.nonce,lease.lease_id,key,encoded(handle.to_dict())))

    def handles(self,lease):
        with self.repository.connect() as db:
            rows=db.execute('SELECT value,session_key FROM population_current_admissions WHERE lease_id=? LIMIT 4097',
                (lease.lease_id,)).fetchall()
            if len(rows)>4096: raise ContractError('population_current_admission_bound_exceeded')
            return [(AdmissionInstallation.from_dict(json.loads(r[0])),r[1]) for r in rows]

    def forget_handle(self,nonce):
        with self.repository.connect() as db:
            db.execute('DELETE FROM population_current_admissions WHERE nonce=?',(nonce,))

    def drain_journal(self,lease,op):
        with self.repository.connect() as db:
            db.execute('INSERT OR IGNORE INTO population_native_state VALUES(?,?,?,?)',
                (op,lease.lease_id,lease.intent_id,encoded(dict(operation_id=op,lease_id=lease.lease_id,
                    owner_intent_id=lease.intent_id,delta_id='whole-lease-drain',delta_version='current',commands={}))))
            row=db.execute('SELECT lease_id,owner_intent_id FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if tuple(row)!=(lease.lease_id,lease.intent_id):
                raise ContractError('population_drain_journal_owner_changed')

    def _parent_delegation(self,db,op):
        row=db.execute('SELECT owner,intent_id,value FROM operations WHERE id=?',(op,)).fetchone()
        if not row:return None,()
        value=json.loads(row['value'])
        if not value.get('population_parent_authority'):return None,()
        from .session_constraints import PopulationParentAuthority
        authority=PopulationParentAuthority.from_dict(value['population_parent_authority'])
        parent=db.execute('SELECT owner,intent_id,status FROM operations WHERE id=?',(authority.parent_operation_id,)).fetchone()
        if (not parent or parent['owner']!=row['owner'] or parent['intent_id']!=row['intent_id']
                or row['intent_id']!=authority.intent_id or parent['status']!='running'):
            raise ContractError('population_delegated_parent_authority_changed')
        self.repository._assert_fleet_open(db,authority.intent_id,authority.parent_operation_id)
        current=db.execute('SELECT revision FROM intents WHERE id=? AND owner=?',(authority.intent_id,row['owner'])).fetchone()
        if current is None or current['revision']!=authority.intent_revision:
            raise ContractError('population_delegated_intent_revision_changed')
        return authority.parent_operation_id,tuple(value.get('population_delegated_scopes',()))

    def _assert_publication_scope(self,db,op,scope,state):
        expected=state.get('borrowed_publication_scopes',{}).get(scope,op)
        held=db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
        if held is None or held[0]!=expected:raise ContractError('population_publication_write_fence_lost')
        if expected!=op:
            parent,allowed=self._parent_delegation(db,op)
            if parent!=expected or scope not in allowed:
                raise ContractError('population_borrowed_scope_authority_changed')

    def lock(self,op,scopes):
        from .repository import IntentConflict
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            parent,allowed=self._parent_delegation(db,op)
            borrowed={}
            for scope in sorted(set(scopes)):
                claim=db.execute('SELECT intent_id FROM policy_claims WHERE scope=?',(scope,)).fetchone()
                held=db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
                own_intent_marker=scope.startswith('intent:') and claim and claim[0]==scope[7:]
                delegated=bool(held and parent and held[0]==parent and scope in allowed)
                if delegated:borrowed[scope]=parent
                if ((claim and claim[0]!='shared:qos' and not own_intent_marker) or (held and held[0]!=op and not delegated)):
                    raise IntentConflict('Population policy authority reserved by another operation')
            for scope in sorted(set(scopes)):
                if scope not in borrowed:
                    db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?)',(scope,op,time.time()+300))
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if row is None:raise ContractError('population_native_journal_missing')
            state=json.loads(row[0])
            state['publication_scopes']=sorted(set(state.get('publication_scopes',()))|set(scopes))
            previous=state.get('borrowed_publication_scopes',{})
            if any(k in previous and previous[k]!=v for k,v in borrowed.items()):
                raise ContractError('population_borrowed_scope_parent_changed')
            state['borrowed_publication_scopes']={**previous,**borrowed}
            db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(state),op))

    def publication_locks(self,op,*,release=False):
        """No TTL takeover: uncertain publication keeps its native write fence."""
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if row is None:raise ContractError('population_native_journal_missing')
            state=json.loads(row[0])
            if state.get('publication_confirmed'):return state
            scopes=state.get('publication_scopes',())
            if not scopes:raise ContractError('population_publication_write_fence_missing')
            for scope in scopes:
                self._assert_publication_scope(db,op,scope,state)
            if release:
                db.executemany('DELETE FROM leases WHERE scope=? AND operation_id=?',((s,op) for s in scopes))
                state['publication_confirmed']=True
                db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(state),op))
            return state

    def commit_claims(self,op,claims,effective,scopes,*,remove_keys=(),manifest=None,invalidate_memberships=None):
        """One current-state transaction: no per-intent wholesale replacement."""
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state_row=db.execute('SELECT owner_intent_id,value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            if state_row is None: raise ContractError('population_native_journal_missing')
            self.repository._assert_fleet_open(db,state_row[0],op)
            for scope in scopes:
                self._assert_publication_scope(db,op,scope,json.loads(state_row['value']))
            if manifest is not None:
                # Intent locks fence writers; recheck their actual current
                # semantic authority in the transaction that publishes claims.
                for intent_id,sem in manifest.items():
                    row=db.execute('SELECT value,state FROM intents WHERE id=?',(intent_id,)).fetchone()
                    contract=ServiceIntent.from_dict(json.loads(row[0])) if row else None
                    if (not row or row[1] not in {'active','satisfied','degraded'}
                            or not contract.is_active(time.time()) or semantic_hash(contract)!=sem):
                        raise ContractError('population_service_policy_changed_before_commit')
            for key in remove_keys:
                db.execute('DELETE FROM qos_claims WHERE session_key=?',(key,))
            for item in claims:
                claim=item['claim'];key=claim['member']['session_token']
                db.execute('INSERT OR REPLACE INTO qos_claims VALUES(?,?,?,?)',
                    (key,claim['intent_id'],item['target_id'],encoded(claim)))
            for key,item in effective.items():
                if item is None: db.execute('DELETE FROM qos_effective WHERE session_key=?',(key,))
                else:
                    db.execute('INSERT OR REPLACE INTO qos_effective VALUES(?,?,?,?,?)',
                        (key,item['target_id'],encoded(item['baseline']),encoded(item['receipt']),encoded(item['value'])))
            for scope in scopes:
                if scope.startswith(('session:','resource:')):
                    query=('SELECT 1 FROM qos_claims WHERE session_key=? LIMIT 1' if scope.startswith('session:')
                           else 'SELECT 1 FROM qos_claims WHERE target_id=? LIMIT 1')
                    if db.execute(query,(scope.split(':',1)[1],)).fetchone():
                        db.execute('INSERT OR REPLACE INTO policy_claims VALUES(?,?,?)',(scope,'shared:qos',op))
                    else: db.execute("DELETE FROM policy_claims WHERE scope=? AND intent_id='shared:qos'",(scope,))
            row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(op,)).fetchone()
            state=json.loads(row[0]);state['claims_committed']=True
            if invalidate_memberships and not state.get('inheritance_membership_invalidated'):
                from .service_policy_state import membership_with_pending
                for intent_id,keys in invalidate_memberships.items():
                    row=db.execute('SELECT revision,membership FROM intents WHERE id=?',(intent_id,)).fetchone()
                    installed=[r[0] for r in db.execute('SELECT session_key FROM qos_claims WHERE intent_id=?',(intent_id,))]
                    membership=membership_with_pending(json.loads(row['membership']),intent_id=intent_id,revision=row['revision'],
                        additions=keys,installed_keys=installed)
                    db.execute("UPDATE intents SET state='active',membership=?,issues=?,updated=? WHERE id=?",
                        (encoded(membership),encoded(['pending_members:receiver_effect_not_verified']),time.time(),intent_id))
                state['inheritance_membership_invalidated']=True
            db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(state),op))
            self.repository._advance_dependency_epoch(db)

    def unlock(self,op,scopes):
        with self.repository.connect() as db:
            db.executemany('DELETE FROM leases WHERE scope=? AND operation_id=?',((s,op) for s in scopes))


class NativePopulationCallbacks:
    def __init__(self,context,service):
        self.context,self.service=context,service
        self.repository,self.registry=service.repository,service.registry
        self.preparer=service.pre_traffic_admission
        self.coordinator=service.admission_coordinator
        self.validator=self.preparer.planner.validator
        self.journal=PopulationJournal(self.repository)
        self.ledger=context.round_coordinator.ledger
        self.commissioning=None

    async def _guard(self,lease,op):
        state=await invoke(self.journal.load,op)
        if state and state.get('closing_release_operation_id'):
            raise ContractError('population_frozen_for_owned_full_release')
        current=await invoke(current_authority,self.ledger,lease,op)
        await invoke(self.repository.assert_fleet_open,lease.intent_id,op)
        def parent_extension():
            with self.repository.connect() as db:
                row=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',(op,lease.owner_id)).fetchone()
            return json.loads(row['value']).get('population_parent_authority') if row else None
        extension=await invoke(parent_extension)
        if extension is not None:
            from .session_constraints import PopulationParentAuthority
            parent=PopulationParentAuthority.from_dict(extension)
            raw=await self._round()
            if (parent.intent_id!=lease.intent_id or parent.round_id!=raw['roundId']
                    or parent.generation!=raw['generation']):
                raise ContractError('population_parent_round_generation_changed')
        if self.commissioning is not None:
            operation=await invoke(self.repository.operation,op,lease.owner_id)
            request=operation.get('population_request')
            if request is not None:
                contract=ServiceIntent.from_dict(operation['population_intent'])
                await invoke(self.commissioning.check,contract,lease.owner_id,request,op)
        return current

    async def assert_population_dispatch(self,lease,operation_id):
        if self.commissioning is not None:
            operation=await invoke(self.repository.operation,operation_id,lease.owner_id)
            contract=ServiceIntent.from_dict(operation['population_intent'])
            await invoke(self.commissioning.check,contract,lease.owner_id,operation['population_request'],operation_id)
        def check():
            with self.repository.connect() as db:
                own=db.execute('SELECT status FROM operations WHERE id=?',(operation_id,)).fetchone()
                if own and own[0]=='quiesced_for_release':raise ContractError('population_frozen_for_owned_full_release')
                rows=db.execute('SELECT DISTINCT n.operation_id FROM population_native_state n JOIN leases l '
                    'ON l.operation_id=n.operation_id WHERE n.lease_id=? AND n.owner_intent_id=? AND n.operation_id!=? LIMIT 1',
                    (lease.lease_id,lease.intent_id,operation_id)).fetchall()
                if rows:raise ContractError('population_previous_publication_ack_required')
        await invoke(check)

    async def _round(self): return await invoke(current_reader(self.ledger))

    async def profile_reader(self,*,intent,lease):
        controller=self.context.ue_controller
        if getattr(controller.driver,'backend',None)=='free-ran-ue':
            from backend.onprem.runtime.current_launch import current_device_launch_contract
            return current_device_launch_contract(controller.driver)
        return await self.replacement_profile_reader(intent=intent,lease=lease)

    async def replacement_profile_reader(self,*,intent,lease):
        """Historical launch evidence remains part of exact replacement proof."""
        body=await self._round()
        profile=await invoke(self.preparer.profile_reader,body)
        if type(profile) is not UEAdmissionLaunchProfile or not profile.device_sessions:
            raise ContractError('population_requires_commissioned_device_profile')
        return profile

    async def prepare_session_replacement(self,*,intent,request,snapshot,session):
        """Freeze an exact owned geographic launch before removing its device."""
        from .session_replacement import ExactSessionReplacement,digest,spatial_revision,verify_vendor_geography
        from .session_constraints import PopulationParentAuthority
        from backend.onprem.traffic.session_workload import session_rates
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        resources=self.context.round_coordinator.resources
        controller=resources.controller
        state=await invoke(controller.store.current)
        if not state or state['generation']!=snapshot['generation']:
            raise ContractError('replacement_native_generation_changed')
        original=[r for r in state['allocation'] if r.get('device',{}).get('device_id')==session['deviceId']]
        if len(original)!=1:raise ContractError('replacement_original_allocation_missing')
        original=json.loads(json.dumps(original[0]))
        access=original.get('spatialAccess',{})
        if (not access or session.get('sourceLocation')!=original.get('sourceLocation')
                or session.get('demandGroupId')!=access.get('groupId') or session.get('servingCellId')!=access.get('cellId')):
            raise ContractError('replacement_original_geography_unproven')
        native_proof=session.get('proof',{})
        uid=native_proof.get('gnbPodUid')
        geo_proof=native_proof.get('spatialRan',{})
        driver_profile=getattr(controller.driver,'device_profile',None)
        if (not uid or driver_profile is None or getattr(controller.driver,'backend','ueransim')!=driver_profile.driver
                or native_proof.get('gnbSearchEndpoint')!=access['endpoint']
                or geo_proof.get('pdr_far_gnb_verified') is not True
                or geo_proof.get('gnbPodUid')!=uid or geo_proof.get('gnbId')!=access.get('gnbId')):
            raise ContractError('replacement_commissioned_gnb_profile_changed')
        verify_vendor_geography(geo_proof,dict(access,gnbPodUid=uid),driver_profile)
        from backend.onprem.runtime.ue import seconds
        if not -1<=time.time()-seconds(snapshot.get('observedAt'))<=15:
            raise ContractError('replacement_original_native_observation_expired')
        profile=await invoke(self.preparer.profile_reader,snapshot)
        if (type(profile) is not UEAdmissionLaunchProfile or profile.valid_until<=time.time()
                or profile.image_digest!=controller.driver.image.split('@')[-1]
                or profile.configuration_digest!=controller.driver.reservation_configuration_digest):
            raise ContractError('replacement_commissioned_launch_profile_changed')
        workload=await invoke(resources.session_workload,snapshot)
        rate=session_rates(workload).get(session['sessionKey'])
        if not rate:raise ContractError('replacement_offered_workload_missing')
        # request.owner_id is the intent ID; repository traffic is owned by the
        # original submitting user, resolved from the exact parent row.
        with self.repository.connect() as db:
            parent=db.execute('SELECT owner FROM operations WHERE id=? AND intent_id=?',(request.operation_id,intent.intent_id)).fetchone()
        if parent is None:raise ContractError('replacement_parent_missing')
        traffic=await invoke(self.repository.current_intent_traffic,intent.intent_id,parent['owner'])
        lease=ServiceIntentTrafficLease.from_dict(traffic['service_intent_traffic_lease'])
        nonces=[]
        for handle,key in await self._all_handles(lease):
            if [handle.admission.identity.network_scope,handle.admission.identity.supi,handle.admission.identity.pdu_session_id]!=json.loads(session['sessionKey'])[:3]:continue
            binding=await invoke(self.coordinator.read_binding,handle)
            if binding is not None and binding.session_key==session['sessionKey']:
                nonces.append(handle.admission.identity.nonce)
        if not nonces:raise ContractError('replacement_original_native_nonce_missing')
        original.update(demandGroupId=access['groupId'],servingCellId=access['cellId'])
        groups=[g for g in snapshot['spatialDemand'].get('requestedGroups',()) if g['groupId']==access['groupId']]
        if len(groups)!=1 or groups[0]['sourceLocation']!=original['sourceLocation']:
            raise ContractError('replacement_requested_demand_group_changed')
        original['scenario']=groups[0]['profileId']
        original['nativeSessionBaseline']=deepcopy(session)
        original['nativeDeviceProfile']=asdict(driver_profile)
        original['nativeDeviceBaseline']=deepcopy(next(d for d in snapshot['devices'] if d['deviceId']==session['deviceId']))
        original['spatialAccess']['gnbPodUid']=uid
        demand={k:session.get(k) for k in ('service_type','region','dnn','snssai','logicalSessionId','slice')}
        demand.update(offered_ul_kbps=rate['ul'],offered_dl_kbps=rate['dl'])
        constraint=ExactSessionReplacement(PopulationParentAuthority(request.operation_id,intent.intent_id,intent.revision,
            snapshot['roundId'],snapshot['generation']).to_dict(),session['sessionKey'],tuple(nonces),digest(profile.to_dict()),
            original,demand,snapshot['spatialDemand']['topologyRevision'],spatial_revision(snapshot))
        constraint.validate_profile(profile)
        return constraint

    async def replacement_reader(self,*,intent,lease,operation_id):
        await self._guard(lease,operation_id)
        with self.repository.connect() as db:
            row=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',(operation_id,lease.owner_id)).fetchone()
        extension=json.loads(row['value']).get('population_replacement_constraint') if row else None
        if extension is None:return None
        from .session_replacement import ExactSessionReplacement,spatial_revision
        value=ExactSessionReplacement.from_dict(extension)
        body=await self._round()
        if (value.parent['round_id']!=body['roundId'] or value.parent['generation']!=body['generation']
                or value.topology_revision!=body.get('spatialDemand',{}).get('topologyRevision')
                or value.spatial_revision!=spatial_revision(body)):
            raise ContractError('replacement_spatial_fence_changed')
        value.validate_profile(await self.replacement_profile_reader(intent=intent,lease=lease))
        value.preview_workload(body,self.context.round_coordinator.resources)
        return value

    async def _reservation(self,intent,lease,delta):
        profile=await self.profile_reader(intent=intent,lease=lease)
        with self.repository.connect() as db:
            operation=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',
                (delta.operation_id,lease.owner_id)).fetchone()
        if operation and json.loads(operation['value']).get('population_replacement_constraint') is not None:
            profile=await self.replacement_profile_reader(intent=intent,lease=lease)
        from backend.onprem.runtime.current_launch import CurrentDeviceLaunchContract
        current_contract=type(profile) is CurrentDeviceLaunchContract
        scopes={d.network_scope for d in delta.added_devices}
        if len(scopes)!=1: raise ContractError('population_single_native_network_scope_required')
        from backend.onprem.runtime.ue import SLICES
        return PopulationReservation(delta.delta_id,delta.version,next(iter(scopes)),delta.created_at,
            delta.valid_until if current_contract else min(delta.valid_until,profile.valid_until),
            profile.contract_id if current_contract else profile.profile_id,delta.reserved_sessions,delta.added_devices,
            tuple((name,sum(d.access_slice==name for d in delta.added_devices)) for name in SLICES))

    async def _active(self):
        def read():
            result=[]
            for owner,value in self.repository.iter_active():
                if len(result)>=1000: raise ContractError('active_service_policy_census_bound_exceeded')
                contract=ServiceIntent.from_dict({k:v for k,v in value.items() if k in ServiceIntent.__dataclass_fields__})
                if contract.is_active(self.service.clock()):
                    result.append((owner,contract,self.repository.current_qos_definition(contract.intent_id)))
            return result
        return await invoke(read)

    @staticmethod
    def _manifest(active):
        def authority(definition):
            if definition is None:return None
            fields=('semantic_hash','template','supported')
            if observe_native(definition):
                fields+=('policy_kind','baseline_profiles','allocation_rule','observation_scope')
                if definition.get('source')=='verified-current-service-policy/v3':
                    fields+=('source','allocating','baseline_profiles_role')
            return digest({k:definition.get(k) for k in fields})
        return digest(sorted((owner,c.intent_id,semantic_hash(c),
            authority(d))
            for owner,c,d in active))

    def _receipt(self,delta,op,flag,**extra):
        return dict(delta_id=delta.delta_id,delta_version=delta.version,operation_id=op,**{flag:True},**extra)

    async def capacity_admission(self,*,intent,lease,delta,operation_id,recover=False):
        await self._guard(lease,operation_id)
        state=await invoke(self.journal.initialize,lease,delta,operation_id)
        if not delta.added_devices:
            return self._receipt(delta,operation_id,'admitted',source='physical-removal-without-new-load')
        reservation=await self._reservation(intent,lease,delta)
        body=await self._round()
        active=await self._active(); manifest=self._manifest(active)
        if state['plans']:
            if state.get('policy_manifest')!=manifest: raise ContractError('population_current_service_policy_changed')
            await self._capacity_readback(state,reservation,body,active)
            return self._receipt(delta,operation_id,'admitted',plan_digest=digest(state['plans']))
        classification=await invoke(self.preparer.classification_reader,body,reservation)
        # Native dispatch authority has already been checked by _guard. Read
        # only this optional extension; legacy unextended capacity preparations
        # do not acquire a new dependency on a public operation projection.
        def route_extension():
            with self.service.repository.connect() as db:
                row=db.execute('SELECT value FROM operations WHERE id=? AND owner=?',(operation_id,lease.owner_id)).fetchone()
            return json.loads(row['value']).get('population_route_constraint') if row else None
        extension=await invoke(route_extension)
        route_constraint=None
        if extension is not None:
            from .session_constraints import NewSessionRouteConstraint
            route_constraint=NewSessionRouteConstraint.from_dict(extension)
            if (route_constraint.intent_id!=intent.intent_id or route_constraint.round_id!=body['roundId']
                    or route_constraint.generation!=body['generation']):
                raise ContractError('population_route_constraint_fence_changed')
        plans=[]; covered=set(); choices={}
        for owner,contract,definition in active:
            try: cohort=selected_reserved_cohort(contract,reservation,classification)
            except ContractError as error:
                if str(error)=='needs_definition:no_matching_reserved_service_sessions': continue
                raise
            if contract.selector.applications:
                raise ContractError('population_observed_application_policy_inheritance_not_supported')
            validate_current_definition(contract,definition)
            plan=await self.preparer.planner.plan(contract,body,reservation,cohort=cohort,
                **({'target_constraint':route_constraint} if route_constraint else {}))
            selected=reserved_identities(cohort)
            actual={p.identity.token for action in plan.actions for p in SessionAssignment.from_action(action).pending_admissions}
            if actual!=selected: raise ContractError('population_plan_admission_coverage_changed')
            for action in plan.actions:
                for pending in SessionAssignment.from_action(action).pending_admissions:
                    before=choices.get(pending.identity.token)
                    if before and before.target_upf_id!=pending.target_upf_id:
                        raise ContractError('population_matching_policy_route_conflict')
                    # Every matching policy independently validated capacity and
                    # authority. Equal routes use one deterministic native owner.
                    if before is None or pending.owner_id<before.owner_id: choices[pending.identity.token]=pending
            covered.update(actual)
            plans.append(dict(intent_id=contract.intent_id,owner=owner,semantic_hash=semantic_hash(contract),plan=plan.to_dict()))
        if covered!=reserved_identities(reservation):
            raise ContractError('population_future_session_has_no_validated_service_policy')
        if self._manifest(await self._active())!=manifest:
            raise ContractError('population_current_service_policy_changed')
        await self._guard(lease,operation_id)
        state=await invoke(self.journal.save,operation_id,plans=plans,policy_manifest=manifest,
            selected_admissions=[p.to_dict() for _,p in sorted(choices.items())],
            **({'route_constraint':route_constraint.to_dict()} if route_constraint else {}))
        return self._receipt(delta,operation_id,'admitted',plan_digest=digest(plans))

    async def _capacity_readback(self,state,reservation,body,active):
        contracts={c.intent_id:c for _,c,_ in active}
        requested=await invoke(self.preparer.planner.demand_reader,body,reservation)
        for item in state['plans']:
            contract=contracts.get(item['intent_id'])
            if contract is None or semantic_hash(contract)!=item['semantic_hash']:
                raise ContractError('population_plan_policy_authority_changed')
            plan=ResourcePlan.from_dict(item['plan'])
            if state.get('route_constraint') is not None:
                from .session_constraints import NewSessionRouteConstraint
                constraint=NewSessionRouteConstraint.from_dict(state['route_constraint'])
                if any(a.kind=='assign_sessions' and a.target_id!=constraint.target_id for a in plan.actions):
                    raise ContractError('population_route_constraint_plan_changed')
            if self.service.clock()>=plan.expires_at:
                raise ContractError('population_capacity_plan_expired')
            snapshot=await invoke(self.preparer.planner.observation_source.snapshot_reserved,contract,reservation)
            selected={p.identity.token for a in plan.actions for p in SessionAssignment.from_action(a).pending_admissions}
            self.preparer.planner._capacity(plan,snapshot,requested,getattr(requested,'traffic_profile',''),
                getattr(requested,'source',''),sum(v for k,v in requested.items() if k not in selected))

    async def install_pending_admissions(self,*,intent,lease,delta,operation_id,recover=False):
        await self._guard(lease,operation_id)
        state=await invoke(self.journal.initialize,lease,delta,operation_id)
        if not delta.added_devices: return self._receipt(delta,operation_id,'installed',installations_count=0)
        active=await self._active()
        if state.get('policy_manifest')!=self._manifest(active): raise ContractError('population_current_service_policy_changed')
        reservation=await self._reservation(intent,lease,delta)
        await self._capacity_readback(state,reservation,await self._round(),active)
        handles={h['admission']['identity']['nonce']:h for h in state['installations']}
        for raw in state.get('selected_admissions',()):
            pending=PendingAdmission.from_dict(raw); nonce=pending.identity.nonce
            command=state['commands'].get('admission:'+nonce)
            await self._guard(lease,operation_id)
            if command is None:
                state['commands']['admission:'+nonce]={'dispatched':True,'pending':raw}
                state=await invoke(self.journal.save,operation_id,commands=state['commands'])
                handle=await invoke(self.coordinator.install,pending,operation_id)
            else:
                handle=await invoke(self.coordinator.recover_installation,pending,operation_id,
                    handles.get(nonce,{}).get('native_boot_epoch'))
            handles[nonce]=handle.to_dict()
            state=await invoke(self.journal.save,operation_id,installations=list(handles.values()))
            await invoke(self.journal.retain_handle,lease,handle)
        expected={p.identity.token for p in map(PendingAdmission.from_dict,state.get('selected_admissions',()))}
        if {AdmissionInstallation.from_dict(h).admission.identity.token for h in handles.values()}!=expected or expected!=reserved_identities(reservation):
            raise ContractError('population_native_installation_coverage_missing')
        return self._receipt(delta,operation_id,'installed',installations_count=len(handles),installation_digest=digest(handles))

    async def _bindings(self,snapshot):
        reader=getattr(self.service.observer,'session_binding_reader',None)
        if not callable(getattr(reader,'current_bindings',None)):
            raise ContractError('population_current_native_classification_not_configured')
        batch=await invoke(reader.current_bindings,snapshot)
        result={}
        for proof in batch.values():
            raw=deepcopy(proof['binding']);raw['key']=SessionKey(**raw['key']);raw['flows']=()
            binding=SessionBinding(**raw)
            result[binding.key.token]=binding
        if set(result)!={r['sessionKey'] for r in session_rows(snapshot)}:
            raise ContractError('population_current_native_classification_incomplete')
        return result

    def _claim(self,contract,definition,key,target):
        """Reauthorize a deterministic template for this new exact native key.

        This is policy inheritance, not an AI bottleneck diagnosis. No receiver
        metrics or effect_verified receipt is manufactured here.
        """
        from .model_planner import _flatten
        template=validate_definition(contract,definition)
        if contract.definition_issues(self.validator.authority) or not contract.is_active(self.service.clock()):
            raise ContractError('population_service_policy_authority_invalid')
        rates={}
        for direction in ('ul','dl'):
            value=finite(template['desired_'+direction+'_bps'],'inherited native rate',1)
            if value%1000: raise ContractError('population_native_kbps_precision_unsupported')
            rates[direction]=int(value//1000)
        policy=self.validator.targets.get(('update_qos',target))
        budget=self.validator.budgets.get(contract.resource_budget_id)
        if not policy or not policy.preserves_guarantees or not budget or 'update_qos' not in budget.allowed_actions:
            raise ContractError('population_new_target_policy_not_authorized')
        params=_flatten({'rates_kbps':rates});fixed=_flatten(policy.fixed_parameters)
        if set(params)!=set(policy.numeric_bounds)|set(fixed):
            raise ContractError('population_inherited_parameter_allowlist_mismatch')
        if any(params[k]!=v for k,v in fixed.items()):
            raise ContractError('population_inherited_fixed_parameter_changed')
        if any(not lo<=finite(params[k],k)<=hi for k,(lo,hi) in policy.numeric_bounds.items()):
            raise ContractError('population_inherited_rate_outside_authority')
        cost=policy.fixed_cost+sum(params[k]*v for k,v in policy.unit_prices.items())
        if cost>budget.maximum_cost or policy.disruption_seconds>contract.continuity_policy.maximum_interruption_seconds:
            raise ContractError('population_inheritance_budget_or_continuity_exceeded')
        # Infrastructure allocation changes are outside this policy-only port.
        # The actual admission planner already proved payload/wire capacity.
        if policy.resource_requirements:
            raise ContractError('population_qos_allocation_requirements_not_supported')
        action=PlanAction('inherit','update_qos',target,{'rates_kbps':rates},(key,),expected_version='native-preflight')
        actual=self.validator.qos_claim(contract,action,key)
        for d in ('ul','dl'):
            if (template[d+'_min_bps']<getattr(actual,d+'_min_bps')
                    or template[d+'_max_bps']>getattr(actual,d+'_max_bps')):
                raise ContractError('population_template_bounds_exceed_current_target')
        if actual.allocation_authority!=template['allocation_authority']:
            raise ContractError('population_template_allocation_authority_changed')
        return QoSPolicyClaim.from_dict(dict(template,intent_id=contract.intent_id,revision=contract.revision,
            member={'session_token':key,'flow_id':''}))

    async def _install_qos(self,state,key,target,policy,lease,op):
        command_id='qos:'+key
        command=state['commands'].get(command_id)
        if command is None:
            request=ActionRequest('update_qos',target,policy.owner_id,op,'inspect',{'rates_kbps':policy.rates_kbps},(key,))
            native=await invoke(self.registry.get('update_qos').provider.inspect,request)
            request=replace(request,expected_version=native['version'])
            checked=await invoke(self.registry.preflight,request)
            command={'request':request.to_dict(),'before':checked['before'],'receipt':None,'dispatched':False}
            state['commands'][command_id]=command
            state=await invoke(self.journal.save,op,commands=state['commands'])
            await self._guard(lease,op)
            command['dispatched']=True
            state['commands'][command_id]=command
            state=await invoke(self.journal.save,op,commands=state['commands'])
            receipt=await invoke(self.registry.apply,request)
        else:
            request=ActionRequest.from_dict(command['request'])
            if (request.target_id!=target or request.parameters!={'rates_kbps':policy.rates_kbps}
                    or request.owner_id!=policy.owner_id or request.session_keys!=(key,)):
                raise ContractError('population_inherited_native_command_changed')
            receipt=(ActionReceipt.from_dict(command['receipt']) if command['receipt'] else
                ActionReceipt(request,'recovery_required',before=command['before'],started_at=self.service.clock()))
            receipt=await invoke(self.registry.readback,receipt)
        command['receipt']=receipt.to_dict();state['commands'][command_id]=command
        state=await invoke(self.journal.save,op,commands=state['commands'])
        if receipt.state not in {'installation_confirmed','effect_verified'}:
            raise ContractError('population_native_qos_installation_pending')
        return state,receipt

    async def inherit_member_policies(self,*,intent,lease,delta,operation_id,readback,session_keys,recover=False):
        await self._guard(lease,operation_id)
        state=await invoke(self.journal.initialize,lease,delta,operation_id)
        keys=sorted(session_keys)
        if state.get('publication_confirmed'):
            receipt=state.get('inherited_receipt')
            if not receipt or receipt.get('session_keys')!=keys:
                raise ContractError('population_published_inheritance_changed')
            return receipt
        if not keys:
            if delta.added_devices: raise ContractError('population_added_native_session_keys_missing')
            return self._receipt(delta,operation_id,'inherited',session_keys=[])
        active=await self._active()
        if state.get('policy_manifest')!=self._manifest(active): raise ContractError('population_current_service_policy_changed')
        snapshot=readback.addition_snapshot or readback.current_snapshot
        bindings=await self._bindings(snapshot)
        rows={r['sessionKey']:r for r in session_rows(snapshot)}
        handles=tuple(AdmissionInstallation.from_dict(h) for h in state['installations'])
        batch=await invoke(self.coordinator.read_bindings,handles)
        if not batch.complete or set(batch.session_keys)!=set(keys):
            raise ContractError('population_new_native_admission_bindings_pending')
        for bound in batch.bindings:
            if rows[bound.session_key]['nativeUpfId']!=bound.upf_id:
                raise ContractError('population_native_admission_upf_changed')
        from .membership import MembershipResolver
        claims=[];targets={};observed={};wanted={}
        costs={}
        for _,contract,definition in active:
            membership=MembershipResolver().resolve(contract,tuple(bindings[k] for k in keys))
            if membership.rejected or any(m.flow_id for m in membership.members):
                raise ContractError('population_observed_application_policy_inheritance_not_supported')
            for member in membership.members:
                key=member.session_token;target=bindings[key].upf_id
                wanted.setdefault(contract.intent_id,[]).append(key)
                targets[key]=target
                if observe_native(definition):
                    validate_observe_native(contract,definition)
                    if (contract.definition_issues(self.validator.authority)
                            or not contract.is_active(self.service.clock())
                            or contract.resource_budget_id not in self.validator.budgets):
                        raise ContractError('population_observe_native_authority_invalid')
                    observed.setdefault(key,[]).append((contract,definition))
                    continue
                claim=self._claim(contract,definition,key,target)
                claims.append({'target_id':target,'claim':claim.to_dict()});targets[key]=target
                native_policy=self.validator.targets['update_qos',target]
                from .model_planner import _flatten
                params=_flatten({'rates_kbps':{'ul':int(claim.desired_ul_bps//1000),'dl':int(claim.desired_dl_bps//1000)}})
                costs[contract.intent_id]=costs.get(contract.intent_id,0)+native_policy.fixed_cost+sum(
                    params[k]*v for k,v in native_policy.unit_prices.items())
                if costs[contract.intent_id]>self.validator.budgets[contract.resource_budget_id].maximum_cost:
                    raise ContractError('population_aggregate_inheritance_budget_exceeded')
        if set(targets)!=set(keys): raise ContractError('population_native_member_has_no_current_service_policy')
        scopes=sorted({'intent:'+c.intent_id for _,c,_ in active}|{'session:'+k for k in keys}|{'resource:'+v for v in targets.values()})
        await invoke(self.journal.lock,operation_id,scopes)
        if self._manifest(await self._active())!=state['policy_manifest']:
            raise ContractError('population_current_service_policy_changed')
        # Locks are retained on uncertainty. Recovery inspects exact journaled
        # native commands; it cannot publish an unprotected new member.
        prior_claims,previous=await invoke(self.repository.qos_state,keys)
        contracts={c.intent_id:(c,d) for _,c,d in active}
        for item in prior_claims:
            old=QoSPolicyClaim.from_dict(item['claim']);pair=contracts.get(old.intent_id)
            if pair is None or old.member.session_token not in wanted.get(old.intent_id,()):
                raise ContractError('population_existing_claim_not_current_matching')
            contract,definition=pair
            expected=self._claim(contract,definition,old.member.session_token,item['target_id']).to_dict()
            if (old.revision>contract.revision or item['target_id']!=targets[old.member.session_token]
                    or dict(expected,revision=old.revision)!=old.to_dict()):
                raise ContractError('population_existing_claim_authority_changed')
        proposed={(c['claim']['intent_id'],c['claim']['member']['session_token']):c for c in prior_claims}
        proposed.update({(c['claim']['intent_id'],c['claim']['member']['session_token']):c for c in claims})
        combined=list(proposed.values())
        effective={p.member.session_token:p for p in EffectivePolicyCoordinator().reconcile(tuple(QoSPolicyClaim.from_dict(c['claim']) for c in combined))}
        allocating=set(effective)
        observed_allocating=set(observed)&allocating
        observed_fallback=set(observed)-allocating
        for key in observed_allocating:
            for contract,definition in observed[key]:
                if not observes_effective_configuration(contract,definition):
                    raise ContractError('population_observe_native_conflicts_with_allocating_policy')
                budget=self.validator.budgets[contract.resource_budget_id]
                cost=observer_configuration_cost(self.validator,contract,effective[key],targets[key])
                costs[contract.intent_id]=costs.get(contract.intent_id,0)+cost
                if costs[contract.intent_id]>budget.maximum_cost:
                    raise ContractError('population_observe_effective_budget_exceeded')
        if observed_allocating:
            reservation=await self._reservation(intent,lease,delta)
            await self._capacity_readback(state,reservation,await self._round(),active)
        if observed:
            reader=getattr(self.registry.get('update_qos').provider,'current_policy',None)
            if not callable(reader):raise ContractError('population_native_baseline_reader_unavailable')
            baseline=await invoke(reader,tuple(sorted(observed)))
            for key,definitions in observed.items():
                # A retry may already have a positively journaled installation.
                # Otherwise new members must begin from the original unowned
                # fallback, or a proven currently committed effective policy.
                if key in observed_allocating and 'qos:'+key in state['commands']:continue
                if key in observed_allocating and key in previous:
                    old=EffectivePolicyCoordinator().reconcile(tuple(QoSPolicyClaim.from_dict(r['claim'])
                        for r in prior_claims if r['claim']['member']['session_token']==key))
                    if len(old)!=1:raise ContractError('population_existing_effective_policy_ambiguous')
                    if encoded(previous[key]['value'])!=encoded(old[0].to_dict()):
                        raise ContractError('population_existing_effective_claim_set_changed')
                    native_effective_configuration(baseline['sessions'][key],bindings[key],old[0],previous[key]['receipt'])
                    continue
                actual=native_baseline(baseline['sessions'][key],bindings[key])
                for contract,definition in definitions:
                    if actual not in validate_observe_native(contract,definition):
                        raise ContractError('population_observe_native_baseline_changed')
                    if key in observed_fallback:baseline_supports_minimums(contract,baseline['sessions'][key])
            state=await invoke(self.journal.save,operation_id,observed_baseline_digest=digest(
                {key:native_baseline(baseline['sessions'][key],bindings[key]) for key in observed_fallback}),
                observed_session_keys=sorted(observed_fallback),observed_effective_session_keys=sorted(observed_allocating))
        updates={}
        for key,policy in sorted(effective.items()):
            # The merged value must also satisfy every applicable native target
            # allowlist, rather than trusting individual claims independently.
            for _,contract,definition in active:
                if any(c['claim']['intent_id']==contract.intent_id and c['claim']['member']['session_token']==key for c in claims):
                    template=dict(validate_definition(contract,definition),
                        desired_ul_bps=policy.rates_kbps['ul']*1000,desired_dl_bps=policy.rates_kbps['dl']*1000)
                    self._claim(contract,dict(definition,template=template),key,targets[key])
            state,receipt=await self._install_qos(state,key,targets[key],policy,lease,operation_id)
            updates[key]=dict(target_id=targets[key],baseline=previous[key]['baseline'] if key in previous else receipt.to_dict(),
                receipt=receipt.to_dict(),value=policy.to_dict())
        if self._manifest(await self._active())!=state['policy_manifest']:
            raise ContractError('population_current_service_policy_changed')
        await self._guard(lease,operation_id)
        if observed:
            latest=await invoke(reader,tuple(sorted(observed)))
            latest_digest=digest({key:native_baseline(latest['sessions'][key],bindings[key]) for key in observed_fallback})
            if latest_digest!=state['observed_baseline_digest']:
                raise ContractError('population_observe_native_baseline_changed')
            configurations={key:native_effective_configuration(latest['sessions'][key],bindings[key],effective[key],updates[key]['receipt'])
                for key in observed_allocating}
            state=await invoke(self.journal.save,operation_id,observed_effective_configurations=configurations,
                observed_effective_policies={key:effective[key].to_dict() for key in observed_allocating})
        await invoke(self.journal.commit_claims,operation_id,combined,updates,scopes,
            manifest={c.intent_id:semantic_hash(c) for _,c,_ in active},
            invalidate_memberships={intent_id:sorted(set(member_keys)) for intent_id,member_keys in wanted.items()})
        for handle in handles:
            bound=next(b for b in batch.bindings if json.loads(b.session_key)[:3]==[
                handle.admission.identity.network_scope,handle.admission.identity.supi,handle.admission.identity.pdu_session_id])
            await invoke(self.journal.retain_handle,lease,handle,bound.session_key)
        # Keep the native write fence through the traffic ledger's publication.
        # A crash after its commit is recovered by exact publication readback.
        result=self._receipt(delta,operation_id,'inherited',session_keys=keys,
            source='validated-service-policy-inheritance/v1',service_effect_verified=False,
            observed_native_session_keys=sorted(observed),native_commands_count=len(updates))
        await invoke(self.journal.save,operation_id,inherited_receipt=result)
        return result

    async def _cancel(self,handle,op,*,guard=None):
        """One dispatched cancel, followed by exact native readback on restart."""
        state=await invoke(self.journal.load,op)
        nonce=handle.admission.identity.nonce;key='cancel:'+nonce
        native=await invoke(self.coordinator.api.inspect,nonce)
        if native is None: raise ContractError('population_admission_missing_during_cancel')
        # Coordinator release validates immutable owner/boot/operation even for
        # an already cancelled registration, and never recreates a selection.
        command=state['commands'].get(key)
        if command is not None and native.status!='cancelled':
            raise ContractError('population_admission_cancel_pending_readback')
        if command is None:
            state['commands'][key]={'dispatched':True,'native_version':native.version}
            await invoke(self.journal.save,op,commands=state['commands'])
        if guard is not None:await guard()
        proof=await invoke(self.coordinator.release,handle,native.version)
        if proof.status!='cancelled': raise ContractError('population_admission_cancel_unconfirmed')
        await invoke(self.journal.forget_handle,nonce)

    async def _all_handles(self,lease):
        handles=await invoke(self.journal.handles,lease)
        initial=await invoke(self.repository.session_admissions,lease.intent_id,lease.owner_id)
        if initial:
            from .pending_admission import PreparedSessionAdmissions
            for h in PreparedSessionAdmissions.from_dict(initial).installations:
                if h.operation_id!=lease.operation_id: raise ContractError('population_initial_admission_owner_changed')
                handles.append((h,None))
        unique={}
        for handle,key in handles:
            nonce=handle.admission.identity.nonce
            if nonce in unique and unique[nonce][0]!=handle:
                raise ContractError('population_admission_nonce_ambiguous')
            unique[nonce]=(handle,key)
        return tuple(unique.values())

    async def withdraw_member_policies(self,*,intent,lease,delta,operation_id,session_keys,recover=False):
        await self._guard(lease,operation_id)
        state=await invoke(self.journal.initialize,lease,delta,operation_id)
        keys=sorted(session_keys)
        if not keys:
            if delta.removed_device_ids: raise ContractError('population_departed_device_sessions_missing')
            return self._receipt(delta,operation_id,'withdrawn',session_keys=[])
        snapshot=await self._round()
        rows={r['sessionKey']:r for r in session_rows(snapshot)}
        exact_removed={k for k,r in rows.items() if r['deviceId'] in delta.removed_device_ids}
        if set(keys)!=exact_removed or any(k not in rows for k in keys):
            raise ContractError('population_withdrawal_not_exact_departed_device')
        bindings=await self._bindings(snapshot)
        active=await self._active()
        manifest=self._manifest(active)
        if state.get('policy_manifest') not in (None,manifest):
            raise ContractError('population_current_service_policy_changed')
        from .membership import MembershipResolver
        for _,contract,_ in active:
            if contract.intent_id==lease.intent_id: continue
            selected=MembershipResolver().resolve(contract,tuple(bindings[k] for k in keys))
            if selected.members or selected.rejected:
                raise ContractError('population_departing_device_has_foreign_service_dependency')
        claims,previous=await invoke(self.repository.qos_state,keys)
        if any(c['claim']['intent_id']!=lease.intent_id for c in claims):
            raise ContractError('population_departing_device_has_foreign_policy_claim')
        scopes=sorted({'intent:'+c.intent_id for _,c,_ in active}|{'session:'+k for k in keys}|
            {'resource:'+p['target_id'] for p in previous.values()})
        await invoke(self.journal.lock,operation_id,scopes)
        await invoke(self.journal.save,operation_id,policy_manifest=manifest)
        if self._manifest(await self._active())!=self._manifest(active):
            raise ContractError('population_protected_service_set_changed')
        latest_claims,latest_previous=await invoke(self.repository.qos_state,keys)
        if latest_claims!=claims or latest_previous!=previous:
            raise ContractError('population_member_claim_changed_before_withdrawal')
        # Native admission closure precedes device retirement, including the
        # original startup reservations. Never carry old selection into reuse.
        identities={(rows[k]['supi'],rows[k]['pduSessionId']) for k in keys}
        for handle,_ in await self._all_handles(lease):
            if (handle.admission.identity.supi,handle.admission.identity.pdu_session_id) in identities:
                await self._guard(lease,operation_id)
                await self._cancel(handle,operation_id)
        from .qos_runtime import SharedQoSRuntime
        runtime=SharedQoSRuntime(self.repository,self.registry,self.validator.qos_claim)
        for key,record in sorted(previous.items()):
            command_id='withdraw:'+key
            state=await invoke(self.journal.load,operation_id)
            command=state['commands'].get(command_id)
            if command is None:
                receipt=runtime.final_release_receipt(record)
                state['commands'][command_id]={'receipt':receipt.to_dict(),'dispatched':True}
                await invoke(self.journal.save,operation_id,commands=state['commands'])
                await self._guard(lease,operation_id)
                receipt=await invoke(self.registry.release,receipt)
            else:
                receipt=await invoke(self.registry.compensation_readback,ActionReceipt.from_dict(command['receipt']))
            state['commands'][command_id]={'receipt':receipt.to_dict(),'dispatched':True}
            await invoke(self.journal.save,operation_id,commands=state['commands'])
            if receipt.state not in {'released','compensated'}:
                raise ContractError('population_member_policy_restoration_pending')
        if self._manifest(await self._active())!=self._manifest(active):
            raise ContractError('population_protected_service_set_changed')
        await self._guard(lease,operation_id)
        await invoke(self.journal.commit_claims,operation_id,[],{k:None for k in keys},scopes,remove_keys=keys,
            manifest={c.intent_id:semantic_hash(c) for _,c,_ in active})
        # Inheritance and withdrawal share one operation's union write fence.
        return self._receipt(delta,operation_id,'withdrawn',session_keys=keys,
            source='exact-owned-member-native-restoration/v1',admissions_closed=True)

    async def before_population_publication(self,*,intent,lease,delta,operation_id,readback):
        """Validate current policy while leases remain held across actual commit."""
        await self._guard(lease,operation_id)
        replacement=await self.replacement_reader(intent=intent,lease=lease,operation_id=operation_id)
        if replacement is not None:
            replacement.validate_published(readback.current_snapshot,delta)
            from backend.onprem.traffic.session_workload import session_rates
            rates=session_rates(await invoke(self.context.round_coordinator.resources.session_workload,readback.current_snapshot))
            key=next(r['sessionKey'] for r in session_rows(readback.current_snapshot) if r['deviceId']==delta.added_devices[0].device_id)
            if rates.get(key)!={'ul':replacement.demand_profile['offered_ul_kbps'],'dl':replacement.demand_profile['offered_dl_kbps']}:
                raise ContractError('replacement_offered_workload_changed')
        state=await invoke(self.journal.publication_locks,operation_id)
        if state.get('policy_manifest')!=self._manifest(await self._active()):
            raise ContractError('population_current_service_policy_changed')
        keys=state.get('observed_session_keys',())
        effective_keys=state.get('observed_effective_session_keys',())
        if keys or effective_keys:
            bindings=await self._bindings(readback.current_snapshot)
            if not (set(keys)|set(effective_keys))<=set(bindings):raise ContractError('population_observed_native_member_missing')
            reader=self.registry.get('update_qos').provider.current_policy
            latest=await invoke(reader,tuple(sorted(set(keys)|set(effective_keys))))
            if digest({k:native_baseline(latest['sessions'][k],bindings[k]) for k in keys})!=state['observed_baseline_digest']:
                raise ContractError('population_observe_native_baseline_changed')
            if effective_keys:
                active=await self._active()
                reservation=await self._reservation(intent,lease,delta)
                await self._capacity_readback(state,reservation,await self._round(),active)
                claims,current=await invoke(self.repository.qos_state,effective_keys)
                policies={p.member.session_token:p for p in EffectivePolicyCoordinator().reconcile(
                    tuple(QoSPolicyClaim.from_dict(r['claim']) for r in claims))}
                for key in effective_keys:
                    policy=policies.get(key)
                    if policy is None or encoded(policy.to_dict())!=encoded(state['observed_effective_policies'][key]):
                        raise ContractError('population_observed_effective_claims_changed')
                    actual=native_effective_configuration(latest['sessions'][key],bindings[key],policy,current[key]['receipt'])
                    if actual!=state['observed_effective_configurations'][key]:
                        raise ContractError('population_observed_effective_configuration_changed')
        await invoke(self.journal.save,operation_id,
            publication_session_keys=sorted(r['sessionKey'] for r in session_rows(readback.current_snapshot)))

    async def confirm_population_publication(self,*,intent,lease,delta,operation_id):
        """Pure ledger readback authorizes unlocking; never replay native writes."""
        from backend.onprem.scenarios.population import read
        record=await invoke(read,self.ledger,operation_id,lease.owner_id)
        state=await invoke(self.journal.load,operation_id)
        if (not record or record.get('pending') is not False or record.get('phase')!='completed'
                or record.get('delta',{}).get('delta_id')!=delta.delta_id
                or record.get('delta',{}).get('version')!=delta.version
                or not state or (state['lease_id'],state['delta_id'],state['delta_version'])!=(lease.lease_id,delta.delta_id,delta.version)):
            raise ContractError('population_publication_not_confirmed')
        if state.get('publication_confirmed'):return self._receipt(delta,operation_id,'published')
        current=await self._round()
        if (current.get('generation')!=delta.generation or current.get('populationRevision')!=delta.next_revision
                or current.get('populationPending')
                or sorted(r['sessionKey'] for r in session_rows(current))!=state.get('publication_session_keys')):
            raise ContractError('population_publication_not_confirmed')
        await invoke(self.journal.publication_locks,operation_id,release=True)
        return self._receipt(delta,operation_id,'published')

    async def verify_parent_population_installation(self,service,operation,owner):
        """Private parent performs effects later; this child proves installation only."""
        from .session_constraints import PopulationParentAuthority
        from backend.onprem.scenarios.population import read
        from backend.onprem.runtime.population import HeldPopulationDelta
        authority=PopulationParentAuthority.from_dict(operation['population_parent_authority'])
        op=operation['operation_id']
        def parent_check():
            with self.repository.connect() as db:
                parent,allowed=self.journal._parent_delegation(db,op)
                if parent!=authority.parent_operation_id:raise ContractError('population_parent_delegation_missing')
                for scope in allowed:
                    held=db.execute('SELECT operation_id FROM leases WHERE scope=?',(scope,)).fetchone()
                    if held is None or held[0]!=parent:raise ContractError('population_parent_lease_lost')
        await invoke(parent_check)
        entry=await invoke(self.ledger.intent_traffic_lease)
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        lease=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
        if lease is None:raise ContractError('population_parent_owned_traffic_missing')
        await self._guard(lease,op)
        record=await invoke(read,self.ledger,op,owner)
        state=await invoke(self.journal.load,op)
        if (not record or record.get('phase')!='completed' or record.get('pending') is not False
                or not state or state.get('publication_confirmed') is not True or state.get('claims_committed') is not True):
            raise ContractError('population_parent_native_publication_incomplete')
        delta=HeldPopulationDelta.from_dict(record['delta'])
        raw=await self._round()
        rows=session_rows(raw)
        if (raw['roundId']!=authority.round_id or raw['generation']!=authority.generation
                or raw.get('populationRevision')!=delta.next_revision
                or set(delta.removed_device_ids)&{r['deviceId'] for r in rows}):
            raise ContractError('population_parent_native_cohort_changed')
        native=await invoke(service.observer.session_binding_reader.current_bindings,raw)
        if set(native)!={r['sessionKey'] for r in rows}:
            raise ContractError('population_parent_native_session_readback_incomplete')
        selected={r['sessionKey'] for r in rows if r['deviceId'] in {d.device_id for d in delta.added_devices}}
        handles=[handle for handle,key in await invoke(self.journal.handles,lease) if key in selected]
        bindings=await invoke(self.coordinator.read_bindings,handles)
        if not bindings.complete or set(bindings.session_keys)!=selected:
            raise ContractError('population_parent_new_nonce_readback_incomplete')
        if operation.get('population_route_constraint'):
            from .session_constraints import NewSessionRouteConstraint
            constraint=NewSessionRouteConstraint.from_dict(operation['population_route_constraint'])
            if any(r['upf']!=constraint.target_id for r in rows if r['sessionKey'] in selected):
                raise ContractError('population_parent_planned_upf_readback_changed')
        await invoke(parent_check)
        return {'installation_confirmed':True,'service_effect_verified':False,
            'parent_operation_id':authority.parent_operation_id,'new_session_keys':sorted(selected),
            'round_id':raw['roundId'],'generation':raw['generation'],'population_revision':raw['populationRevision']}

    async def prepare_drain(self,intent,owner,operation):
        """Close only current population selections before whole-fleet drain.

        Service owns full restoration authority; this port cannot release UEs,
        delete NFs, or assert traffic restoration.
        """
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        entry=await invoke(self.ledger.intent_traffic_lease)
        lease=ServiceIntentTrafficLease.from_dict(entry['lease']) if entry else None
        if lease is None or (lease.intent_id,lease.owner_id)!=(intent.intent_id,owner):
            raise ContractError('population_drain_lease_owner_changed')
        handles=await invoke(self.journal.handles,lease)
        # Full cleanup has its own service operation and exact current handle
        # set. It does not scan prior population operations.
        op=operation['operation_id'] if isinstance(operation,dict) else operation
        actual=await invoke(self.repository.operation,op,owner)
        owning_failure=(actual.get('failure_cleanup') is True and actual.get('release_purpose')=='full_release')
        if (actual['intent_id']!=intent.intent_id or (actual['kind']!='release' and not owning_failure)
                or actual.get('release_purpose')=='membership_withdrawal'
                or actual.get('revision',0)>lease.intent_revision
                or actual.get('expected_traffic_request_id',lease.request_id)!=lease.request_id):
            raise ContractError('population_drain_requires_actual_full_release')
        await invoke(self.journal.drain_journal,lease,op)
        for handle,_ in handles: await self._cancel(handle,op)
        return {'admissions_closed':True,'source':'native-population-admission-closure/v1'}

    async def finalize_member_departure(self,*,intent,lease,delta,operation_id,readback,session_keys):
        """Prune desired pending identities only after the actual native barrier."""
        from backend.onprem.runtime.population import PopulationReadback
        from backend.onprem.runtime.round_resources import fresh
        await self._guard(lease,operation_id)
        from backend.onprem.scenarios.population import read
        owned=await invoke(read,self.ledger,operation_id,lease.owner_id)
        if (owned.get('phase')!='native_ready'
                or sorted(session_keys)!=owned.get('receipts',{}).get('withdrawn',{}).get('session_keys')):
            raise ContractError('population_departure_membership_not_current_delta')
        target=readback.current_snapshot
        if (type(readback) is not PopulationReadback or readback.ready is not True
                or readback.removed_native_absent is not True or readback.generation!=delta.generation
                or readback.population_revision!=delta.next_revision or not isinstance(target,dict)
                or target.get('generation')!=delta.generation or target.get('populationRevision')!=delta.next_revision
                or not fresh(target.get('observedAt'))):
            raise ContractError('population_departure_native_absence_not_confirmed')
        rows=session_rows(target)
        if set(session_keys)&{r['sessionKey'] for r in rows} or set(delta.removed_device_ids)&{r['deviceId'] for r in rows}:
            raise ContractError('population_departing_native_session_still_present')
        def publish():
            from .service_policy_state import membership_with_pending
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self.repository._assert_fleet_open(db,lease.intent_id,operation_id)
                state_row=db.execute('SELECT value FROM population_native_state WHERE operation_id=?',(operation_id,)).fetchone()
                state=json.loads(state_row[0]) if state_row else None
                if not state or state['delta_id']!=delta.delta_id or state['delta_version']!=delta.version:
                    raise ContractError('population_departure_journal_changed')
                if state.get('departures_finalized'): return
                # Only exact keys from this delta's durably dispatched/restored
                # withdrawal may be removed from desired state.
                commands=state['commands']
                if any('withdraw:'+key not in commands and db.execute('SELECT 1 FROM qos_claims WHERE session_key=?',(key,)).fetchone()
                       for key in session_keys):
                    raise ContractError('population_departure_policy_withdrawal_missing')
                current_rows=db.execute("SELECT id,revision,membership FROM intents WHERE state IN ('active','satisfied','degraded') LIMIT 1001").fetchall()
                if len(current_rows)>1000: raise ContractError('active_service_policy_census_bound_exceeded')
                for row in current_rows:
                    current=json.loads(row['membership'])
                    if not any(m['session_token'] in set(session_keys) for m in current.get('members',())): continue
                    installed=[r[0] for r in db.execute('SELECT session_key FROM qos_claims WHERE intent_id=?',(row['id'],))]
                    membership=membership_with_pending(current,intent_id=row['id'],revision=row['revision'],
                        removals=session_keys,installed_keys=installed)
                    db.execute("UPDATE intents SET state='active',membership=?,issues=?,updated=? WHERE id=?",
                        (encoded(membership),encoded(['membership_changed:receiver_effect_not_verified'] if membership['members'] else ['waiting_members']),time.time(),row['id']))
                state['departures_finalized']=True
                db.execute('UPDATE population_native_state SET value=? WHERE operation_id=?',(encoded(state),operation_id))
                self.repository._advance_dependency_epoch(db)
        await invoke(publish)
        return self._receipt(delta,operation_id,'finalized',session_keys=sorted(session_keys))


def build_native_population_coordinator(context,config,service):
    """Compose existing trusted writers; enabling config is not commissioning."""
    policy=config.get('population')
    if not isinstance(policy,dict) or set(policy)-{'enabled','commission_receipt'} or type(policy.get('enabled')) is not bool:
        raise ContractError('invalid_pinned_population_configuration')
    if not policy['enabled']: return None
    preparer=getattr(service,'pre_traffic_admission',None)
    if (preparer is None or service.admission_coordinator is None
            or not callable(getattr(preparer,'classification_reader',None))
            or (not callable(getattr(preparer,'profile_reader',None))
                and getattr(getattr(context.ue_controller,'driver',None),'backend',None)!='free-ran-ue')
            or not hasattr(preparer.planner,'validator')
            or not callable(getattr(getattr(service.observer,'session_binding_reader',None),'current_bindings',None))):
        raise ContractError('population_native_capacity_policy_ports_not_configured')
    callbacks=NativePopulationCallbacks(context,service)
    from .population_commissioning import ExistingFourPopulationAdapter,NativePopulationCommissioning
    adapter=ExistingFourPopulationAdapter(context.round_coordinator,profile_reader=callbacks.profile_reader,
        capacity_admission=callbacks.capacity_admission,install_pending_admissions=callbacks.install_pending_admissions,
        inherit_member_policies=callbacks.inherit_member_policies,withdraw_member_policies=callbacks.withdraw_member_policies,
        commissioned=False)
    receipt=policy.get('commission_receipt')
    if receipt is not None:
        if not isinstance(receipt,dict) or set(receipt)!={'file','sha256'}:
            raise ContractError('invalid_population_commission_receipt_configuration')
        # Real typed ports are mandatory; config enabled never supplies proof.
        commission=NativePopulationCommissioning(context,service,receipt['file'],receipt['sha256'],clock=service.clock)
        adapter.commissioning=callbacks.commissioning=commission
    adapter.verify_parent_population_installation=callbacks.verify_parent_population_installation
    adapter.replacement_reader=callbacks.replacement_reader
    adapter.replacement_profile_reader=callbacks.replacement_profile_reader
    adapter.prepare_drain=callbacks.prepare_drain
    adapter.finalize_member_departure=callbacks.finalize_member_departure
    adapter.before_population_publication=callbacks.before_population_publication
    adapter.confirm_population_publication=callbacks.confirm_population_publication
    adapter.assert_population_dispatch=callbacks.assert_population_dispatch
    async def quiesce_for_release(intent,owner,operation):
        from .population_release import quiesce
        async with adapter._lock:
            return await quiesce(callbacks,intent,owner,operation)
    adapter.quiesce_for_release=quiesce_for_release
    adapter.native_callbacks=callbacks
    return adapter
