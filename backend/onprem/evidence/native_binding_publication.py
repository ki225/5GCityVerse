"""Immutable internal publication, independently revalidated by the round ledger.

The private envelope contains held nonces only in memory. Persistent/public
output contains the existing bounded wrapper and digest-only cohort seal.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib,json,time

SCHEMA='cityverse.native-binding-publication/v1'
MAX_BYTES=4*1024*1024

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def owner_digest(snapshot):
    return digest({k:snapshot.get(k) for k in ('roundId','requestId','generation','schemaVersion','allocation','fleetLease')})

def need(value):
    if not value:raise ValueError('Native binding publication identity, ownership or source seal is invalid')

@dataclass(frozen=True)
class NativeBindingPublication:
    payload:bytes
    sha256:str

    @classmethod
    def create(cls,snapshot,generation,held,observed):
        value=dict(schemaVersion=SCHEMA,roundId=snapshot['roundId'],generation=snapshot['generation'],
            expectedSequence=snapshot['sequence'],ownerDigest=owner_digest(snapshot),held=held.to_dict(),
            generationRecord={k:deepcopy(generation[k]) for k in ('generation','desired','allocation','created')},
            observed=deepcopy(observed))
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        result=cls(raw,hashlib.sha256(raw).hexdigest())
        result.validate(snapshot,held)
        return result

    def validate(self,snapshot,held):
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation
        from backend.onprem.runtime.current_sessions import V2
        from backend.onprem.traffic.v2_batch import typed_sampling_view,native_session_bindings
        from backend.onprem.scenarios.native_session_presentation import project_native_session_bindings
        need(type(self.payload) is bytes and 0<len(self.payload)<=MAX_BYTES
            and type(self.sha256) is str and hashlib.sha256(self.payload).hexdigest()==self.sha256)
        value=json.loads(self.payload)
        need(set(value)=={'schemaVersion','roundId','generation','expectedSequence','ownerDigest',
            'held','generationRecord','observed'} and value['schemaVersion']==SCHEMA
            and snapshot.get('schemaVersion')==V2
            and value['roundId']==snapshot['roundId'] and value['generation']==snapshot['generation']
            and type(value['generation']) is int and type(value['expectedSequence']) is int
            and value['expectedSequence']==snapshot['sequence'] and value['ownerDigest']==owner_digest(snapshot)
            and type(held) is HeldUEAllocation and HeldUEAllocation.from_dict(value['held'])==held)
        observed=value['observed']
        need(observed.get('source')=='native-free-ran-ue-single-session/v2')
        view=typed_sampling_view(snapshot,value['generationRecord'],held,observed=observed)
        wrapper=native_session_bindings(snapshot,value['generationRecord'],held,observed)
        # Reuse the existing strict public contract: unknown nested fields,
        # original raw nonce keys, stale proofs or missing members cannot leak.
        public=project_native_session_bindings({**snapshot,'nativeSessionBindings':wrapper})
        need(public.get('available') is True)
        return {'nativeSessionBindings':wrapper,'typedCohortProof':view['typedCohortProof']}
