"""Trusted readback of a fixed reservation-only preparation that never dispatched."""
from dataclasses import dataclass,asdict
import hashlib
import json
from .domain import ContractError,finite


def preparation_recovery_fingerprint(operation):
    return hashlib.sha256(json.dumps(operation,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PreparationRecoveryProof:
    operation_id:str
    intent_id:str
    owner:str
    intent_revision:int
    request_id:str
    operation_fingerprint:str
    observed_at:float
    current_held_start_operation_id:str
    generation:int
    native_zero_evidence:dict
    evidence_references:tuple[str,...]
    source:str='fixed-reservation-only-no-dispatch/v1'
    dispatch_started:bool=False
    external_effects_absent:bool=True

    def validate(self,operation,owner,current_traffic,now):
        if (self.source!='fixed-reservation-only-no-dispatch/v1'
                or self.dispatch_started is not False or self.external_effects_absent is not True
                or self.owner!=owner or self.operation_id!=operation['operation_id']
                or self.intent_id!=operation['intent_id'] or self.intent_revision!=operation['revision']
                or self.request_id!=operation.get('expected_traffic_request_id')
                or self.operation_fingerprint!=preparation_recovery_fingerprint(operation)
                or type(self.generation) is not int or self.generation<0
                or not isinstance(self.evidence_references,tuple) or not self.evidence_references
                or any(not isinstance(r,str) or not r for r in self.evidence_references)):
            raise ContractError('preparation_recovery_authority_or_no_dispatch_proof_invalid')
        if (operation.get('kind')!='reconcile' or operation.get('status')!='recovery_required'
                or operation.get('pending_session_preparation') is not True
                or any(operation.get(k) for k in ('receipts','pending_action','pending_traffic_start','traffic_started',
                    'traffic_admission_receipt','prepared_session_admissions','pending_restoration'))):
            raise ContractError('preparation_recovery_original_operation_has_external_effects_or_changed')
        if not -0.000001<=now-finite(self.observed_at,'preparation readback clock',0)<=30:
            raise ContractError('preparation_recovery_readback_stale')
        traffic=current_traffic or {}
        lease=traffic.get('receipt',{}).get('service_intent_traffic_lease',{})
        if (not self.current_held_start_operation_id or self.current_held_start_operation_id==self.operation_id
                or traffic.get('start_operation_id')!=self.current_held_start_operation_id
                or lease.get('operation_id')!=self.current_held_start_operation_id
                or lease.get('intent_id')!=self.intent_id or lease.get('owner_id')!=owner
                or lease.get('request_id')!=self.request_id):
            raise ContractError('preparation_recovery_current_held_owner_not_proven')
        evidence=self.native_zero_evidence
        if (not isinstance(evidence,dict) or evidence.get('amf_complete') is not True
                or evidence.get('smf_complete') is not True
                or type(evidence.get('amf_context_count')) is not int or evidence['amf_context_count']!=0
                or type(evidence.get('smf_session_count')) is not int or evidence['smf_session_count']!=0):
            raise ContractError('preparation_recovery_native_global_zero_unproven')
        upfs=evidence.get('upfs')
        if (not isinstance(upfs,list) or len(upfs)!=4
                or {r.get('target_id') for r in upfs if isinstance(r,dict)}!=
                    {'upf-embb','upf-urllc','upf-mmtc','upf-v2x'}):
            raise ContractError('preparation_recovery_all_native_upfs_required')
        for row in upfs:
            if (row.get('complete') is not True or not isinstance(row.get('epoch'),str) or not row['epoch']
                    or any(type(row.get(k)) is not int or row[k]!=0 for k in ('session_count','residual_count'))
                    or not -0.000001<=now-finite(row.get('observed_at'),'UPF readback clock',0)<=30):
                raise ContractError('preparation_recovery_native_upf_zero_unproven')

    def summary(self):
        return {'source':self.source,'observed_at':self.observed_at,
            'external_effects_absent':True,'dispatch_started':False,
            'proof_sha256':hashlib.sha256(json.dumps(asdict(self),sort_keys=True,allow_nan=False).encode()).hexdigest()}


async def recover_failed_preparation(service,operation_id,owner):
    from .service import invoke
    reader=getattr(service,'preparation_recovery_reader',None)
    if not callable(reader):raise ContractError('trusted_preparation_recovery_not_configured')
    operation=await invoke(service.repository.operation,operation_id,owner)
    proof=await invoke(reader,operation,owner)
    if type(proof) is not PreparationRecoveryProof:
        raise ContractError('typed_preparation_recovery_proof_required')
    traffic=await invoke(service.repository.current_intent_traffic,operation['intent_id'],owner)
    proof.validate(operation,owner,traffic,service.clock())
    return await invoke(service.repository.recover_failed_preparation,operation_id,owner,
        proof.operation_fingerprint,proof.summary(),expected_traffic_fingerprint=preparation_recovery_fingerprint(traffic))
