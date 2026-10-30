"""Durable shared QoS ownership; claims commit after effect verification."""
from dataclasses import replace

from .actuators import ActionReceipt, ActionRequest
from .domain import ContractError
from .effective_policy import EffectivePolicyCoordinator, QoSPolicyClaim


class SharedQoSRuntime:
    def __init__(self, repository, registry, claim_authorizer):
        self.repository, self.registry, self.claim_authorizer = repository, registry, claim_authorizer
        self.coordinator = EffectivePolicyCoordinator()

    def prepare(self, intent, actions, *, snapshot=None):
        claims = []
        keys = {key for action in actions for key in action.session_keys}
        rows, previous = self.repository.qos_state(keys,intent_id=intent.intent_id)
        if self.claim_authorizer is None:
            raise ContractError('qos_allocation_authority_not_configured')
        for action in actions:
            for key in action.session_keys:
                claim = self.claim_authorizer(intent,action,key)
                claims.append({'target_id':action.target_id,'claim':claim.to_dict()})
        retained = [r for r in rows if r['claim']['intent_id'] != intent.intent_id]
        combined = retained + claims
        target_by_key = {}
        for row in rows + claims:
            key = row['claim']['member']['session_token']
            if key in target_by_key and target_by_key[key] != row['target_id']:
                raise ContractError('shared_session_target_changed')
            target_by_key[key] = row['target_id']
        effective = {p.member.session_token:p for p in self.coordinator.reconcile(tuple(QoSPolicyClaim.from_dict(r['claim']) for r in combined))}
        for row in retained:
            claim=QoSPolicyClaim.from_dict(row['claim']);policy=effective[claim.member.session_token]
            if any(policy.rates_kbps[d]*1000<getattr(claim,'desired_'+d+'_bps') for d in ('ul','dl')):
                raise ContractError('qos_existing_claim_would_be_reduced')
        checker=getattr(getattr(self.claim_authorizer,'__self__',None),'validate_effective_qos',None)
        if callable(checker):
            if snapshot is None:raise ContractError('qos_current_capacity_snapshot_missing')
            checker(intent,snapshot,{k:p.rates_kbps for k,p in effective.items()})
        elif snapshot is not None and (snapshot.measurement_requests or any(m.name.startswith('path_') for m in snapshot.metrics)):
            raise ContractError('qos_merged_capacity_authority_missing')
        return {'claims':combined,'previous':previous,'effective':effective,'targets':target_by_key,'updates':{},'receipts':[],'actions':actions,
                'definition_intent':intent,'capacity_snapshot':snapshot,'capacity_checker':checker}

    def scopes(self, prepared):
        return [*('session:'+k for k in prepared['targets']),*('resource:'+v for v in prepared['targets'].values())]

    def requests(self, intent, operation_id, prepared):
        requests = []
        for action in prepared.get('actions',()):
            original = ActionRequest('update_qos',action.target_id,intent.intent_id,operation_id,action.expected_version,action.parameters,action.session_keys)
            current = self.registry.get('update_qos').provider.inspect(original)
            if current['version'] != action.expected_version:
                raise ContractError('native_qos_cohort_changed')
        for key, policy in sorted(prepared['effective'].items()):
            request = ActionRequest('update_qos',prepared['targets'][key],policy.owner_id,operation_id,'inspect',{'rates_kbps':policy.rates_kbps},(key,))
            native = self.registry.get('update_qos').provider.inspect(request)
            requests.append(replace(request,expected_version=native['version']))
        checker=prepared.get('capacity_checker')
        if callable(checker):
            checker(intent,prepared['capacity_snapshot'],{k:p.rates_kbps for k,p in prepared['effective'].items()})
        return tuple(requests)

    def installed(self, prepared, receipt):
        key = receipt.request.session_keys[0]
        prior = prepared['previous'].get(key)
        prepared['updates'][key] = {'target_id':receipt.request.target_id,'baseline':prior['baseline'] if prior else receipt.to_dict(),'receipt':receipt.to_dict(),'value':prepared['effective'][key].to_dict()}

    def commit(self, intent_id, operation_id, prepared, scopes):
        from .service_policy_state import from_verified_commit
        definition=(from_verified_commit(prepared['definition_intent'],prepared['claims'],prepared['updates'])
                    if 'definition_intent' in prepared else None)
        self.repository.commit_qos(intent_id,operation_id,prepared['claims'],prepared['updates'],scopes,
            **({'desired_definition':definition} if definition is not None else {}))

    def release_plan(self, intent_id):
        rows, previous = self.repository.qos_state(intent_id=intent_id)
        remaining = [r for r in rows if r['claim']['intent_id'] != intent_id]
        effective = {p.member.session_token:p for p in self.coordinator.reconcile(tuple(QoSPolicyClaim.from_dict(r['claim']) for r in remaining))}
        return {'claims':remaining,'previous':previous,'effective':effective,'targets':{k:r['target_id'] for k,r in previous.items()},'updates':{}}

    def final_release_receipt(self, record):
        """Latest native owner/operation fence plus the very first original baseline."""
        latest = ActionReceipt.from_dict(record['receipt'])
        first = ActionReceipt.from_dict(record['baseline'])
        latest.before = first.before
        return latest

    def retirements(self, prepared):
        return tuple((key,self.final_release_receipt(record)) for key,record in prepared['previous'].items() if key not in prepared['effective'])
