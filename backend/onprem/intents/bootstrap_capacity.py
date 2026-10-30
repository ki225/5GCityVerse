"""Measured template envelopes for admission, never maximum NF capacity.

Only a server-pinned calibration file can construct a profile. A demonstrated
duplex receiver lower bound is transferred only across an identical measured
software/template/hardware/resource/flow envelope, then sealed to a fresh native
incarnation. No model-supplied certificate or PodReady inference is accepted.
"""
from dataclasses import asdict, dataclass
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path

from .domain import ContractError
from .native_path_capacity import PathCalibration


class BootstrapBlocked(ContractError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def need(condition, reason):
    if not condition:
        raise BootstrapBlocked(reason)


def number(value, minimum=0):
    need(type(value) in (int,float) and math.isfinite(value) and value>=minimum,
         'invalid_bootstrap_numeric_proof')
    return value


def fresh(value, now, maximum=15):
    number(value)
    need(0<=now-value<=maximum,'bootstrap_source_expired_or_future')


ENVELOPE = ('template_id','template_sha256','software_sha256','hardware_fingerprint',
            'traffic_profile_id','cpu_cores','memory_bytes','max_sessions')
IDENTITY = ('target_id','pod_uid','native_upf_id','native_node_ip','smf_boot_epoch',
            'kernel_epoch','owner_id','operation_id','version')


@dataclass(frozen=True)
class PhaseAuthorityProof:
    """Immutable ORIGINAL evidence; validate once after the last blocking IO.

    Creation and cleanup deliberately have distinct policies. Cleanup does not
    renew expired admission authority, and expiration cannot prevent cleanup.
    All nested validators are pure and retain their original source clocks.
    """
    authority_until: float
    lease_until: float
    profile_until: float | None = None
    node_json: str | None = None
    envelope_json: str | None = None
    absence_json: str | None = None
    node_id: str | None = None
    native_proofs: tuple = ()
    certificate_checks: tuple = ()
    cleanup: bool = False

    def validate_at(self, now):
        number(now)
        if not self.cleanup:
            need(now < min(number(self.authority_until), number(self.lease_until)),
                 'bootstrap_original_dispatch_authority_expired')
            if self.profile_until is not None:
                need(now < number(self.profile_until), 'bootstrap_original_profile_expired')
            if self.node_json is not None:
                validate_node(json.loads(self.node_json), self.node_id,
                              json.loads(self.envelope_json), now, deployed=False)
        if self.absence_json is not None:
            absence=json.loads(self.absence_json)
            need(absence.get('exists') is False and absence.get('version'),
                 'bootstrap_original_absence_changed')
            fresh(absence.get('observed_at'), now)
        for proof in self.native_proofs:
            proof.validate_at(now)
        for certificate,binding in self.certificate_checks:
            BootstrapCapacityCertificate(**json.loads(certificate)).validate(binding,now)


@dataclass(frozen=True)
class BootstrapProfile:
    profile_id: str
    profile_sha256: str
    evidence_file: str
    evidence_sha256: str
    valid_until: float
    safety_factor: float = .9

    def load(self, now):
        calibration=PathCalibration(self.profile_id,self.evidence_file,self.evidence_sha256,
                                    self.valid_until,self.safety_factor)
        evidence,capacities=calibration.load(now)
        envelope=evidence.get('bootstrap_envelope')
        need(isinstance(envelope,dict) and set(envelope)==set(ENVELOPE),
             'not_configured:measured_bootstrap_envelope_missing')
        for key in ENVELOPE[:5]:
            need(isinstance(envelope[key],str) and 0<len(envelope[key])<=256,
                 'invalid_bootstrap_envelope_identity')
        for key in ('template_sha256','software_sha256','hardware_fingerprint'):
            need(len(envelope[key])==64 and all(c in '0123456789abcdef' for c in envelope[key]),
                 'invalid_bootstrap_envelope_digest')
        number(envelope['cpu_cores'],.001)
        need(type(envelope['memory_bytes']) is int and envelope['memory_bytes']>0,
             'invalid_bootstrap_memory')
        # Existing PathCalibration proves exactly one isolated native PDU.
        # Do not multiply a single-session receipt into concurrency capacity.
        need(type(envelope['max_sessions']) is int and envelope['max_sessions']==1,
             'not_configured:bootstrap_concurrency_not_calibrated')
        samples=evidence['nf_samples']
        for sample in samples:
            measured=sample.get('measured_bootstrap_envelope')
            need(sample.get('envelope_source')=='native-template-hardware-envelope/v1'
                 and measured==envelope,'not_configured:measured_hardware_resource_envelope_missing')
        need(number(samples[0].get('envelope_observed_at'))<=min(w['startedAt'] for w in evidence['receiver_windows'])
             and max(w['finishedAt'] for w in evidence['receiver_windows'])<=number(samples[1].get('envelope_observed_at'))<=evidence['calibrated_at'],
             'bootstrap_hardware_envelope_does_not_bracket_receiver_window')
        return envelope,capacities,evidence['calibrated_wire_load_bps']


class BootstrapProfiles:
    """Factory-only pinned files; an empty catalog is explicitly unconfigured."""
    def __init__(self, entries=()):
        self.entries=dict(entries)
        need(len(self.entries)<=64,'unbounded_bootstrap_profiles')

    @classmethod
    def pinned(cls, entries):
        values={}
        for profile_id,path,sha256 in entries:
            path=Path(path)
            need(path.is_absolute() and not path.is_symlink() and path.is_file()
                 and path.stat().st_size<=65536,'invalid_bootstrap_profile_file')
            raw=path.read_bytes()
            need(hashlib.sha256(raw).hexdigest()==sha256,'bootstrap_profile_digest_changed')
            spec=json.loads(raw)
            need(set(spec)=={'profile_id','evidence_file','evidence_sha256','valid_until','safety_factor'}
                 and spec['profile_id']==profile_id and profile_id not in values,
                 'invalid_bootstrap_profile_specification')
            values[profile_id]=BootstrapProfile(profile_sha256=sha256,**spec)
        return cls(values.items())

    def get(self, profile_id, now):
        need(profile_id in self.entries,'not_configured:commissioned_bootstrap_profile_missing')
        profile=self.entries[profile_id]
        need(type(profile) is BootstrapProfile,'untrusted_bootstrap_profile')
        profile.load(now)
        return profile


def validate_node(proof, node_id, envelope, now, *, deployed):
    need(isinstance(proof,dict) and proof.get('source')=='native-node-aggregate-allocation/v1'
         and proof.get('node_id')==node_id and proof.get('complete') is True
         and proof.get('version') and proof.get('hardware_fingerprint')==envelope['hardware_fingerprint'],
         'not_configured:complete_node_aggregate_proof_required')
    fresh(proof.get('observed_at'),now)
    for resource,key in (('cpu','cpu_cores'),('memory','memory_bytes')):
        free=number(proof.get('allocatable_'+resource))-number(proof.get('allocated_'+resource))-number(proof.get('reserved_'+resource))
        need(free>=0 and (deployed or free>=envelope[key]),'bootstrap_node_aggregate_overcommit')
    if deployed:
        need(proof.get('target_allocation')=={k:envelope[k] for k in ('cpu_cores','memory_bytes')},
             'bootstrap_current_target_allocation_mismatch')


@dataclass(frozen=True)
class BootstrapCapacityCertificate:
    profile_id: str
    profile_sha256: str
    native_identity: dict
    requested_bps: dict
    supported_lower_bound_bps: dict
    source_observed_at: float
    valid_until: float
    node_version: str
    load_proof_sha256: str
    service_effect_verified: bool = False

    def to_dict(self):
        return asdict(self)

    def validate(self, binding, now):
        need(self.service_effect_verified is False and now<self.valid_until,
             'bootstrap_certificate_expired_or_invalid')
        need(self.native_identity=={k:getattr(binding,k) for k in IDENTITY},
             'bootstrap_incarnation_or_version_changed')
        fresh(self.source_observed_at,now)


def certify(profile,binding,load,node,demand,session_count,now):
    """Trusted executor only: fresh native proofs, no caller certificate parser."""
    envelope,lower,wire_lower=profile.load(now)
    need(type(session_count) is int and 0<session_count<=envelope['max_sessions'],
         'bootstrap_session_concurrency_outside_measured_envelope')
    need(set(demand)=={'ul','dl'},'exact_duplex_demand_required')
    native={k:getattr(binding,k) for k in IDENTITY}
    need(isinstance(load,dict) and load.get('source')=='native-upf-bootstrap-load/v1'
         and load.get('complete') is True and load.get('native_identity')==native
         and load.get('envelope')==envelope,'not_configured:current_bootstrap_native_load_proof_missing')
    fresh(load.get('observed_at'),now)
    fresh(binding.observed_at,now)
    validate_node(node,load.get('node_id'),envelope,now,deployed=True)
    need(load.get('node_id')==node.get('node_id'),'bootstrap_node_changed')
    existing=load.get('payload_bps',{})
    need(set(existing)=={'ul','dl'},'bootstrap_current_duplex_load_missing')
    count=load.get('active_session_count')
    need(type(count) is int and count>=0 and count+session_count<=envelope['max_sessions'],
         'bootstrap_total_concurrency_outside_measured_envelope')
    for direction in ('ul','dl'):
        need(number(existing[direction])+number(demand[direction])<=lower[direction],
             'bootstrap_load_exceeds_demonstrated_lower_bound')
    # Packet overhead is measured for this exact flow shape, never assumed1.
    overhead=wire_lower*profile.safety_factor/sum(lower.values())
    need(overhead>=1,'bootstrap_measured_wire_ratio_invalid')
    need(load.get('traffic_profile_id')==envelope['traffic_profile_id']
         and number(load.get('wire_bps'))+sum(demand.values())*overhead<=wire_lower*profile.safety_factor,
         'bootstrap_wire_load_outside_measured_envelope')
    at=min(binding.observed_at,load['observed_at'],node['observed_at'])
    return BootstrapCapacityCertificate(profile.profile_id,profile.profile_sha256,native,dict(demand),lower,
        at,min(profile.valid_until,at+15),node['version'],digest(load))


class BootstrapCalibrationRunner:
    """Actual receiver runner plus mandatory exact native envelope-reader port.

    This calibrates an already owned established reference PDU; it does not
    launch a benchmark UE as part of a pending service operation. No built-in
    envelope source is invented. Missing producer leaves commissioning off.
    """
    def __init__(self,resources,native_api,nf_reader,envelope_reader=None,*,clock,enabled=False):
        from .native_path_calibration import NativePathCalibrationRunner
        self.clock,self.enabled,self.envelope_reader=clock,enabled,envelope_reader
        outer=self

        class MeasuredReader:
            def sample(self,target):
                need(outer.enabled and callable(outer.envelope_reader),
                     'not_configured:bootstrap_native_envelope_reader_missing')
                sample=deepcopy(nf_reader.sample(target))
                proof=outer.envelope_reader(target)
                need(proof.get('source')=='native-template-hardware-envelope/v1'
                     and proof.get('complete') is True
                     and proof.get('native_identity')=={k:sample.get(k) for k in
                         ('target_id','pod_uid','container_id','upf_epoch','image_id')},
                     'bootstrap_reference_envelope_identity_changed')
                fresh(proof.get('observed_at'),outer.clock())
                envelope=deepcopy(proof.get('envelope',{}))
                need(set(envelope)==set(ENVELOPE),'bootstrap_measured_envelope_incomplete')
                from .resource_adapters import _cpu_millis,_memory_bytes
                limits=sample.get('resources',{}).get('limits',{})
                need(_cpu_millis(limits.get('cpu'))/1000==envelope['cpu_cores']
                     and _memory_bytes(limits.get('memory'))==envelope['memory_bytes']
                     and sample.get('image_id','').endswith('sha256:'+envelope['software_sha256']),
                     'bootstrap_reference_resource_or_software_changed')
                sample['observed_at']=min(sample['observed_at'],proof['observed_at'])
                sample['envelope_source']=proof['source']
                sample['envelope_observed_at']=proof['observed_at']
                sample['measured_bootstrap_envelope']=envelope
                return sample

        self.runner=NativePathCalibrationRunner(resources,native_api,MeasuredReader(),clock=clock)

    async def run(self,snapshot,**kwargs):
        need(self.enabled and callable(self.envelope_reader),
             'not_configured:bootstrap_native_envelope_reader_missing')
        evidence=await self.runner.run(snapshot,**kwargs)
        first,last=evidence['nf_samples']
        envelope=first['measured_bootstrap_envelope']
        need(last['measured_bootstrap_envelope']==envelope,
             'bootstrap_reference_envelope_changed_during_receiver_window')
        evidence['bootstrap_envelope']=deepcopy(envelope)
        return evidence

    def pin_profile(self,evidence,evidence_file,profile_file,*,valid_seconds=900,safety_factor=.9):
        profile_file=Path(profile_file)
        need(profile_file.is_absolute() and not profile_file.exists() and not profile_file.is_symlink(),
             'unique_bootstrap_profile_path_required')
        calibration=self.runner.pin_receipt(evidence,evidence_file,
                    valid_seconds=valid_seconds,safety_factor=safety_factor)
        spec=asdict(calibration)
        encoded=json.dumps(spec,sort_keys=True,allow_nan=False).encode()
        result=BootstrapProfile(profile_sha256=hashlib.sha256(encoded).hexdigest(),**spec)
        result.load(self.clock())  # Do not advertise an unvalidated profile.
        fd=os.open(profile_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        return result
