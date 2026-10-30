"""Readback-only recovery of a baseline preparation that never held or dispatched."""
from dataclasses import dataclass,asdict
import hashlib,json,sqlite3,time
from pathlib import Path
from .domain import ContractError
from .preparation_recovery import preparation_recovery_fingerprint as fingerprint

SOURCE='baseline-reservation-no-hold-no-dispatch/v1'
PENDING=('receipts','pending_action','pending_traffic_start','traffic_started','traffic_admission_receipt',
 'prepared_session_admissions','pending_restoration','pending_release','pending_traffic_release',
 'native_restoration_complete','traffic_round_id','round_id')

def require(condition,code):
    if not condition:raise ContractError(code)

def validate_operation(operation):
    require(operation.get('kind')=='reconcile' and operation.get('status')=='recovery_required'
        and operation.get('pending_session_preparation') is True
        and bool(operation.get('expected_traffic_request_id'))
        and not any(operation.get(k) for k in PENDING),'baseline_recovery_dispatch_or_operation_changed')

def reservation_fence(path):
    """One readonly SQLite snapshot, no GenerationStore constructor or DDL."""
    from backend.onprem.storage.readonly import decode_generation
    with sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        row=db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone()
        generation=decode_generation(row)
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require('ue_held_reservation' in tables,'baseline_recovery_held_inventory_unavailable')
        row=db.execute('SELECT value FROM ue_held_reservation WHERE slot=1').fetchone()
        held=json.loads(row[0]) if row else None
    require(generation is not None,'baseline_recovery_generation_unavailable')
    return {'generation':generation['generation'],'desired':generation['desired'],
        'allocation':generation['allocation'],'created':generation['created'],'held':held}

def validate_fence(value,operation):
    require(value.get('allocation')==[] and isinstance(value.get('desired'),dict)
        and set(value['desired'])=={'embb','urllc','mmtc','v2x'}
        and all(type(v) is int and v==0 for v in value['desired'].values())
        and type(value.get('generation')) is int
        and type(value.get('created')) in (int,float) and value['created']<=operation['created_at'],
        'baseline_recovery_generation_not_original_empty')
    held=value.get('held')
    if held is not None:
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation
        held=HeldUEAllocation.from_dict(held)
        require(held.operation_id!=operation['operation_id'] and held.state=='released'
            and held.created_at<=operation['created_at'], 'baseline_recovery_held_or_consumed_outcome_uncertain')

@dataclass(frozen=True)
class BaselineNoHoldRecoveryProof:
    operation_id:str
    intent_id:str
    owner:str
    revision:int
    operation_fingerprint:str
    reservation_fingerprint:str
    generation:int
    observed_at:float
    native_evidence_sha256:str
    source:str=SOURCE

    def validate(self,operation,owner,now):
        validate_operation(operation)
        require(self.source==SOURCE and self.operation_id==operation['operation_id']
            and self.intent_id==operation['intent_id'] and self.owner==owner
            and self.revision==operation['revision']
            and self.operation_fingerprint==fingerprint(operation)
            and -0.000001<=now-self.observed_at<=15,
            'baseline_recovery_proof_scope_or_clock_changed')
        import re
        require(all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in
            (self.operation_fingerprint,self.reservation_fingerprint,self.native_evidence_sha256))
            and type(self.generation) is int and self.generation>=0,'baseline_recovery_proof_invalid')

    def summary(self):
        return dict(source=self.source,observed_at=self.observed_at,generation=self.generation,
            reservation_fingerprint=self.reservation_fingerprint,native_evidence_sha256=self.native_evidence_sha256,
            proof_sha256=fingerprint(asdict(self)),dispatch_started=False,external_effects_absent=True)

async def recover(service,intent_id,operation_id,owner,revision):
    from .baseline_device_admission import BaselineDeviceAdmissionPreparer
    from .service import invoke
    operation=await invoke(service.repository.operation,operation_id,owner)
    require(operation['intent_id']==intent_id and operation['revision']==revision,'baseline_recovery_owner_or_revision_changed')
    if (operation.get('status')=='traffic_admission_failed'
            and (operation.get('baseline_preparation_recovery_proof') or {}).get('source')==SOURCE):
        return operation
    validate_operation(operation)
    preparer=service.pre_traffic_admission
    require(type(preparer) is BaselineDeviceAdmissionPreparer,'baseline_recovery_trusted_preparer_required')
    controller=preparer.controller
    # Existing process/controller ownership remains intact. This lock prevents
    # another local reservation from racing the native read and journal CAS.
    async with controller._lock:
        controller._assert_startup_not_quarantined()
        require(not await invoke(service.repository.current_intent_traffic,intent_id,owner),
                'baseline_recovery_existing_traffic')
        before=await invoke(reservation_fence,controller.store.path)
        validate_fence(before,operation)
        raw=await invoke(controller.adapter.observe,before['generation'])
        from backend.onprem.runtime.execution_admission import empty_native_snapshot
        now=service.clock()
        require(empty_native_snapshot(raw,before['generation'],now)
            and all(raw.get('sources',{}).get(k,{}).get('available') is True
                for k in ('ue','amf','smf','upf')),'baseline_recovery_fresh_global_native_zero_unproven')
        after=await invoke(reservation_fence,controller.store.path)
        require(before==after,'baseline_recovery_reservation_fence_changed')
        validate_fence(after,operation)
        proof=BaselineNoHoldRecoveryProof(operation_id,intent_id,owner,revision,fingerprint(operation),
            fingerprint(after),before['generation'],now,fingerprint(raw))
        proof.validate(operation,owner,service.clock())
        return await invoke(service.repository.recover_baseline_no_hold,operation_id,owner,
            proof.operation_fingerprint,proof.summary())
