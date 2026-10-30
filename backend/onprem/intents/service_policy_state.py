"""Current verified service policy definitions, independent of member epochs."""
from dataclasses import asdict,dataclass
import hashlib
import json

from .domain import ContractError,ServiceIntent,finite


OBSERVE_SOURCE='verified-current-service-policy/v2'
OBSERVE_EFFECTIVE_SOURCE='verified-current-service-policy/v3'
OBSERVE_SCOPE='session-ambr-mbr-and-pdr-cardinality'


@dataclass(frozen=True)
class ObserveNativePolicyDefinition:
    """A verified no-change policy, never a native rate allocation template."""
    intent_id:str
    revision:int
    semantic_hash:str
    baseline_profiles:tuple[dict,...]
    verified_observation_id:str
    effect_evidence_sha256:str

    def to_dict(self):
        return {**asdict(self),'baseline_profiles':list(self.baseline_profiles),
            'source':OBSERVE_SOURCE,'policy_kind':'observe_native','supported':True,
            'allocation_rule':'preserve_native_configuration','observation_scope':OBSERVE_SCOPE}


@dataclass(frozen=True)
class ObserveEffectiveConfigurationDefinition(ObserveNativePolicyDefinition):
    """Nonallocating constraints may observe a positively validated shared policy."""
    def to_dict(self):
        return {**super().to_dict(),'source':OBSERVE_EFFECTIVE_SOURCE,
            'allocation_rule':'observe_effective_configuration','allocating':False,
            'baseline_profiles_role':'unowned-fallback-and-original-restoration-provenance'}


def observe_native(definition):
    return isinstance(definition,dict) and definition.get('policy_kind')=='observe_native'


def validate_observe_native(intent,definition):
    v3=(isinstance(definition,dict) and definition.get('source')==OBSERVE_EFFECTIVE_SOURCE
        and definition.get('allocation_rule')=='observe_effective_configuration'
        and definition.get('allocating') is False
        and definition.get('baseline_profiles_role')=='unowned-fallback-and-original-restoration-provenance')
    v2=(isinstance(definition,dict) and definition.get('source')==OBSERVE_SOURCE
        and definition.get('allocation_rule')=='preserve_native_configuration')
    if (not observe_native(definition) or not (v2 or v3)
            or definition.get('intent_id')!=intent.intent_id or definition.get('semantic_hash')!=semantic_hash(intent)
            or type(definition.get('revision')) is not int or not 1<=definition['revision']<=intent.revision
            or definition.get('observation_scope')!=OBSERVE_SCOPE or definition.get('supported') is not True
            or not isinstance(definition.get('baseline_profiles'),list) or not definition['baseline_profiles']
            or not definition.get('verified_observation_id') or not definition.get('effect_evidence_sha256')
            or definition.get('template') is not None):
        raise ContractError('observe_native_policy_definition_missing_or_stale')
    return definition['baseline_profiles']


def observes_effective_configuration(intent,definition):
    validate_observe_native(intent,definition)
    return definition['source']==OBSERVE_EFFECTIVE_SOURCE


def validate_current_definition(intent,definition):
    return validate_observe_native(intent,definition) if observe_native(definition) else validate_definition(intent,definition)


def _native_profile(row,binding,*,require_unowned):
    """Only the native API's actual exposed fields are compared across members."""
    from uuid import UUID
    if require_unowned and (row.get('owner_id') not in (None,'') or row.get('operation_id') not in (None,'')):
        raise ContractError('observe_native_requires_unowned_current_baseline')
    try:
        if str(UUID(row['upf_id']))!=row['upf_id']:raise ValueError
    except (KeyError,ValueError,TypeError):
        raise ContractError('observe_native_upf_identity_missing') from None
    expected=f"{row.get('smf_boot_epoch')}:{row['upf_id']}:{row.get('upfEpoch')}:{row.get('pfcpSeid')}"
    snssai=f"{row.get('sst')}-{row.get('sd')}"
    if (binding.path_revision!=expected or snssai!=binding.s_nssai or row.get('dnn')!=binding.dnn
            or row.get('session_key')!=binding.key.token or not row.get('upfEpoch')):
        raise ContractError('observe_native_session_path_changed')
    qers=row.get('qers')
    if not isinstance(qers,list) or not 1<=len(qers)<=64:
        raise ContractError('observe_native_current_qer_policy_missing')
    shapes=[]
    for qer in qers:
        pdrs=qer.get('pdrIds')
        if not isinstance(pdrs,list) or not pdrs:raise ContractError('observe_native_pdr_policy_missing')
        shapes.append({**{k:v for k,v in qer.items() if k not in {'id','pdrIds'}},'pdr_count':len(pdrs)})
    return dict(target_id=binding.upf_id,network_scope=binding.key.network_scope,
        native_upf_id=row['upf_id'],upf_epoch=row['upfEpoch'],s_nssai=snssai,dnn=binding.dnn,
        qers=sorted(shapes,key=encoded))


def native_baseline(row,binding):
    return _native_profile(row,binding,require_unowned=True)


def native_effective_configuration(row,binding,policy,receipt):
    """Positive exact installation proof, never recognition by owner prefix."""
    from backend.onprem.policy.group_qer import _ambr_qer
    from .resource_adapters import SessionQerProvider
    request=receipt.get('request',{})
    installed=receipt.get('installed',{}).get('sessions',{}).get(binding.key.token,{})
    if (receipt.get('state') not in {'installation_confirmed','effect_verified'}
            or request.get('action')!='update_qos' or request.get('session_keys') not in ([binding.key.token],(binding.key.token,))
            or request.get('target_id')!=binding.upf_id
            or request.get('owner_id')!=policy.owner_id or row.get('owner_id')!=policy.owner_id
            or not request.get('operation_id') or row.get('operation_id')!=request['operation_id']
            or request.get('parameters')!={'rates_kbps':policy.rates_kbps}
            or row.get('version')!=installed.get('version') or row.get('qers')!=installed.get('qers')
            or any(row.get(f)!=installed.get(f) for f in SessionQerProvider.identity_fields)
            or _ambr_qer(row['qers'])['mbrKbps']!=policy.rates_kbps):
        raise ContractError('population_observed_effective_native_installation_unproven')
    return dict(profile=_native_profile(row,binding,require_unowned=False),session_key=binding.key.token,
        path_revision=binding.path_revision,owner_id=row['owner_id'],operation_id=row['operation_id'],version=row['version'])


def effective_supports_observer(intent,policy):
    """Check enforceable rate constraints; receiver attainment remains pending."""
    for objective in intent.objectives:
        if objective.metric not in {'ul_throughput','dl_throughput'}:continue
        if objective.scope!='per_session':
            raise ContractError('population_observe_effective_group_constraint_not_configured')
        rate=policy.rates_kbps[objective.metric.split('_',1)[0]]*1000
        if ((objective.direction=='at_least' and rate<objective.target)
                or (objective.direction=='at_most' and rate>objective.target)):
            raise ContractError('population_observed_effective_policy_violates_objective')


def observer_configuration_cost(validator,intent,policy,target):
    """All observers constrain a shared configuration; none obtains a rate claim."""
    if validator is None or intent.definition_issues(validator.authority):
        raise ContractError('population_observe_effective_authority_unproven')
    native=validator.targets.get(('update_qos',target));budget=validator.budgets.get(intent.resource_budget_id)
    if (native is None or budget is None or not native.preserves_guarantees
            or native.disruption_seconds>intent.continuity_policy.maximum_interruption_seconds):
        raise ContractError('population_observe_effective_continuity_unproven')
    effective_supports_observer(intent,policy)
    params={'rates_kbps.ul':policy.rates_kbps['ul'],'rates_kbps.dl':policy.rates_kbps['dl']}
    return native.fixed_cost+sum(params[k]*v for k,v in native.unit_prices.items())


def baseline_supports_minimums(intent,row):
    from backend.onprem.policy.group_qer import _ambr_qer
    rates=_ambr_qer(row['qers'])['mbrKbps']
    for objective in intent.objectives:
        if (objective.scope=='per_session' and objective.direction=='at_least'
                and objective.metric in {'ul_throughput','dl_throughput'}):
            direction=objective.metric.split('_',1)[0]
            if finite(rates.get(direction),'native baseline MBR',0)*1000<objective.target:
                raise ContractError('observe_native_baseline_below_service_minimum')


def shared_observation(service,intent,snapshot,membership):
    """Authorize only a read lock on positively known current shared policies."""
    from .effective_policy import EffectivePolicyCoordinator,QoSPolicyClaim
    from .planning import PlanAction
    from .planning_trace import ReobserveBeforeWrite
    keys={m.session_token for m in membership.members}
    claims,records=service.repository.qos_state(keys)
    if not records:return {'scopes':[],'records':{},'policies':{}}
    if service.qos_claim_authorizer is None:raise ContractError('shared_observation_authority_missing')
    active={}
    for owner,value in service.repository.iter_active():
        if len(active)>=1000:raise ContractError('active_service_policy_census_bound_exceeded')
        contract=ServiceIntent.from_dict({k:v for k,v in value.items() if k in ServiceIntent.__dataclass_fields__})
        if contract.is_active(service.clock()):active[contract.intent_id]=contract
    for row in claims:
        claim=QoSPolicyClaim.from_dict(row['claim']);contract=active.get(claim.intent_id)
        if contract is None or contract.definition_issues(service.authority):
            raise ContractError('shared_observation_current_claim_authority_missing')
        selected=service.membership.resolve(contract,snapshot.sessions)
        if selected.rejected or claim.member not in selected.members:
            raise ContractError('shared_observation_current_claim_membership_changed')
        template=validate_definition(contract,service.repository.current_qos_definition(contract.intent_id))
        if {k:v for k,v in claim.to_dict().items() if k not in {'intent_id','revision','member'}}!=template:
            raise ContractError('shared_observation_current_definition_changed')
        action=PlanAction('observe','update_qos',row['target_id'],{'rates_kbps':{
            'ul':int(claim.desired_ul_bps//1000),'dl':int(claim.desired_dl_bps//1000)}},(claim.member.session_token,),
            expected_version='read-only-current-shared-policy')
        authorized=service.qos_claim_authorizer(contract,action,claim.member.session_token)
        if (claim.revision>contract.revision or claim.allocation_authority!=authorized.allocation_authority
                or any(getattr(claim,d+'_min_bps')<getattr(authorized,d+'_min_bps')
                    or getattr(claim,d+'_max_bps')>getattr(authorized,d+'_max_bps') for d in ('ul','dl'))):
            raise ContractError('shared_observation_current_claim_bounds_changed')
    policies={p.member.session_token:p for p in EffectivePolicyCoordinator().reconcile(tuple(QoSPolicyClaim.from_dict(r['claim']) for r in claims))}
    if set(policies)!=set(records):raise ContractError('shared_observation_current_claims_incomplete')
    provider=service.registry.get('update_qos').provider
    inventory=provider.current_policy(tuple(sorted(records)))
    bindings={b.key.token:b for b in snapshot.sessions}
    versions=dict(snapshot.target_versions)
    validator=getattr(getattr(getattr(service,'pre_traffic_admission',None),'planner',None),'validator',None)
    validator=validator or getattr(service.qos_claim_authorizer,'__self__',None)
    observed_cost=0
    for key,record in records.items():
        if inventory['session_versions'].get(key)!=versions.get(key):
            raise ReobserveBeforeWrite('shared_observation_native_configuration_changed')
        native_effective_configuration(inventory['sessions'][key],bindings[key],policies[key],record['receipt'])
        if encoded(record['value'])!=encoded(policies[key].to_dict()):
            raise ContractError('shared_observation_effective_claim_set_changed')
        observed_cost+=observer_configuration_cost(validator,intent,policies[key],record['target_id'])
    if observed_cost>validator.budgets[intent.resource_budget_id].maximum_cost:
        raise ContractError('population_observe_effective_budget_exceeded')
    return {'scopes':sorted({'session:'+k for k in records}|{'resource:'+r['target_id'] for r in records.values()}),
        'records':records,'policies':policies}


def from_verified_hold(intent,plan,effect,before,after,membership,inventory,now,shared=None):
    keys={m.session_token for m in membership.members}
    rows=inventory.get('sessions',{})
    if (plan.actions or not keys or any(m.flow_id for m in membership.members)
            or effect.get('met') is not True or effect.get('protected_services_met') is not True
            or effect.get('owner_id')!=intent.intent_id or set(effect.get('session_keys',()))!=keys
            or inventory.get('source')!='native-upf-qer' or set(rows)!=keys
            or not 0<=now-finite(inventory.get('observed_at'),'native baseline clock',0)<=15
            or before.configuration_revision!=after.configuration_revision):
        raise ContractError('verified_whole_cohort_hold_required_for_definition')
    bindings={b.key.token:b for b in after.sessions}
    old_versions,new_versions=dict(before.target_versions),dict(after.target_versions)
    profiles={}
    for key in sorted(keys):
        version=inventory.get('session_versions',{}).get(key)
        if not version or old_versions.get(key)!=version or new_versions.get(key)!=version:
            raise ContractError('observe_native_baseline_changed_during_effect')
        if shared and key in shared['records']:
            record=shared['records'][key]
            native_effective_configuration(rows[key],bindings[key],shared['policies'][key],record['receipt'])
            original=record['baseline']['before']['sessions'][key]
            from .resource_adapters import SessionQerProvider
            if any(original.get(f)!=rows[key].get(f) for f in SessionQerProvider.identity_fields):
                raise ContractError('observe_effective_original_baseline_path_changed')
            # Original preflight receipt supplies restoration provenance; only
            # current independently pinned NF metadata fills its absent fields.
            profile=native_baseline({**rows[key],**original},bindings[key])
        else:profile=native_baseline(rows[key],bindings[key])
        baseline_supports_minimums(intent,rows[key])
        profile_key=encoded({k:profile[k] for k in ('target_id','network_scope','s_nssai','dnn')})
        if profile_key in profiles and profiles[profile_key]!=profile:
            raise ContractError('observe_native_heterogeneous_baseline')
        profiles[profile_key]=profile
    return ObserveEffectiveConfigurationDefinition(intent.intent_id,intent.revision,semantic_hash(intent),
        tuple(profiles[k] for k in sorted(profiles)),after.observation_id,
        hashlib.sha256(encoded(effect).encode()).hexdigest()).to_dict()


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def semantic_hash(intent):
    if type(intent) is not ServiceIntent:
        raise ContractError('typed_service_policy_definition_required')
    value=asdict(intent)
    for field in ('revision','valid_from','valid_until'):
        value.pop(field)
    # All selectors/classification, objectives, priority, degradation, continuity,
    # budget and provenance remain part of the authorization identity.
    value['allocation_rule']='demand'
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def from_verified_commit(intent,claims,updates):
    own=[r['claim'] for r in claims if r['claim']['intent_id']==intent.intent_id]
    if not own:
        return None  # Logical empty-group withdrawal must retain desired policy.
    for claim in own:
        key=claim['member']['session_token']
        receipt=updates.get(key,{}).get('receipt',{})
        if receipt.get('state')!='effect_verified' or receipt.get('request',{}).get('session_keys') not in ([key],(key,)):
            return None  # Proposal/installation alone cannot mint a definition.
    templates={encoded({k:v for k,v in claim.items() if k not in {'member','intent_id','revision'}}) for claim in own}
    supported=len(templates)==1
    return dict(source='verified-current-service-policy/v1',intent_id=intent.intent_id,
        revision=intent.revision,semantic_hash=semantic_hash(intent),allocation_rule='demand',
        supported=supported,reason='' if supported else 'heterogeneous_current_service_policy',
        template=json.loads(next(iter(templates))) if supported else None)


def validate_definition(intent,definition):
    if observe_native(definition):
        raise ContractError('observe_native_policy_has_no_rate_template')
    if (not isinstance(definition,dict) or definition.get('source')!='verified-current-service-policy/v1'
            or definition.get('intent_id')!=intent.intent_id or definition.get('semantic_hash')!=semantic_hash(intent)
            or definition.get('allocation_rule')!='demand' or definition.get('revision',0)>intent.revision):
        raise ContractError('current_verified_service_policy_definition_missing_or_stale')
    if definition.get('supported') is not True or not isinstance(definition.get('template'),dict):
        raise ContractError('heterogeneous_current_service_policy')
    return definition['template']


def membership_with_pending(current,*,intent_id,revision,additions=(),removals=(),installed_keys=()):
    """Desired membership is separate from a newly installed receiver effect."""
    def identity(member): return member['session_token'],member.get('flow_id','')
    removed=set(removals)
    wanted={identity(m):dict(m) for m in current.get('members',()) if m['session_token'] not in removed}
    pending={identity(m):dict(m) for m in current.get('pending_members',()) if m['session_token'] not in removed}
    for key in additions:
        member={'session_token':key,'flow_id':''};wanted[identity(member)]=member;pending[identity(member)]=member
    members=[wanted[k] for k in sorted(wanted)]
    waiting=[pending[k] for k in sorted(pending) if k in wanted]
    return dict(intent_id=intent_id,intent_revision=revision,members=members,rejected=current.get('rejected',[]),
        pending_members=waiting,desired_count=len(members),pending_count=len(waiting),
        installed_count=sum(m['session_token'] in set(installed_keys) for m in members),effect_invalidated=bool(members))


def preserve_pending_membership(current,state,incoming,issues):
    """An old/partial observation cannot certify or erase desired new members."""
    if not current.get('effect_invalidated') or state not in {'active','satisfied','degraded'}:
        return state,incoming,issues
    def identities(value):
        return {(m['session_token'],m.get('flow_id','')) for m in (value or {}).get('members',())}
    wanted=identities(current)
    # Only the normal successful whole-cohort verification path clears the
    # marker. Mere active/planning publication, even with all keys, cannot.
    if state=='satisfied' and incoming is not None and identities(incoming)==wanted:
        return state,incoming,issues
    if incoming is not None and identities(incoming)==wanted:
        incoming=dict(incoming,**{k:current[k] for k in ('pending_members','desired_count','pending_count','installed_count','effect_invalidated')})
    else:
        incoming=current
    return ('active' if state=='satisfied' else state),incoming,tuple(dict.fromkeys((*issues,'pending_members:receiver_effect_not_verified')))


def persist(db,intent_id,operation_id,definition):
    if definition is None:
        return
    row=db.execute('SELECT revision,value,state FROM intents WHERE id=?',(intent_id,)).fetchone()
    if row is None or row['state'] not in {'active','satisfied','degraded'}:
        raise ContractError('active_intent_required_for_service_policy_definition')
    actual=ServiceIntent.from_dict(json.loads(row['value']))
    if (definition['intent_id']!=intent_id or definition['revision']>row['revision']
            or definition['semantic_hash']!=semantic_hash(actual)):
        raise ContractError('service_policy_definition_authority_changed')
    db.execute('INSERT OR REPLACE INTO qos_policy_definitions VALUES(?,?,?)',
        (intent_id,operation_id,encoded(definition)))
