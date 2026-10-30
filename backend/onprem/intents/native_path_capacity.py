"""Commissioned path capacity derived from actual receiver calibration receipts.

A calibration is a conservative achieved-rate budget under a measured envelope,
not a measurement of maximum path capacity. Current native census and NF
configuration must still match; old receiver observations never become current traffic.
"""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import math
import sqlite3
import re
from pathlib import Path
import time
from .domain import ContractError,timestamp,SessionKey


def hardware_identity(proof):
    fields = ('source', 'target_id', 'pod_uid', 'node_name', 'node_uid', 'system_uuid', 'machine_id',
              'architecture', 'operating_system', 'kernel_version', 'os_image', 'container_runtime', 'capacity')
    if (not isinstance(proof, dict) or proof.get('source') != 'native-kubernetes-node-hardware/v1'
            or any(not isinstance(proof.get(k),str) or not proof[k] or proof[k].lower() in {'unknown','none','null'} for k in fields if k!='capacity')
            or not isinstance(proof.get('capacity'),dict) or not all(proof['capacity'].get(k) for k in ('cpu','memory'))):
        raise ContractError('native_path_hardware_proof_missing')
    return {k:deepcopy(proof[k]) for k in fields}


class NativePathHardwareReader:
    """Current node/Pod identities from the fixed native NF client's own scope."""
    def __init__(self, client, targets, *, clock=time.time, maximum_read_seconds=8):
        self.client,self.targets,self.clock=client,deepcopy(dict(targets)),clock
        if not 0 < maximum_read_seconds <= 8 or not 1 <= len(self.targets) <= 64:
            raise ContractError('bounded_native_hardware_scope_required')
        self.maximum_read_seconds=maximum_read_seconds

    def __call__(self,target):
        started=self.clock();began=time.monotonic();spec=self.targets[target]
        def get(kind,name):
            if kind not in {'pod','node'} or not isinstance(name,str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',name):
                raise ContractError('native_path_hardware_name_invalid')
            remaining=self.maximum_read_seconds-(time.monotonic()-began)
            if remaining<=0:raise ContractError('native_path_hardware_deadline_exceeded')
            argv=['kubectl','--context',self.client.context,'--namespace',self.client.namespace,
                  '--request-timeout='+str(min(self.client.timeout,remaining))+'s','get',kind,name,'-o','json']
            result=self.client.runner(argv,capture_output=True,text=True,timeout=min(self.client.timeout+2,remaining),check=False)
            if result.returncode or len(result.stdout.encode())>8*1024*1024:
                raise ContractError('native_path_hardware_read_unavailable')
            return json.loads(result.stdout)
        pod=get('pod',spec['pod'])
        node_name=pod.get('spec',{}).get('nodeName')
        if (pod.get('metadata',{}).get('namespace')!=self.client.namespace or not node_name
                or pod['metadata'].get('deletionTimestamp')):
            raise ContractError('native_path_hardware_pod_unavailable')
        node=get('node',node_name)
        closing_pod=get('pod',spec['pod'])
        closing_node=get('node',node_name)
        def pin(p):
            return (p.get('metadata',{}).get('uid'),p.get('metadata',{}).get('resourceVersion'),
                    p.get('metadata',{}).get('deletionTimestamp'),p.get('spec'),p.get('status',{}).get('nodeInfo'),
                    p.get('status',{}).get('capacity'))
        end=self.clock()
        if (pin(pod)!=pin(closing_pod) or pin(node)!=pin(closing_node)
                or not 0<=end-started<=self.maximum_read_seconds or time.monotonic()-began>self.maximum_read_seconds
                or node.get('metadata',{}).get('name')!=node_name or node['metadata'].get('deletionTimestamp')):
            raise ContractError('native_path_hardware_changed_or_stale')
        info=node.get('status',{}).get('nodeInfo',{})
        proof=dict(source='native-kubernetes-node-hardware/v1',target_id=target,pod_uid=pod['metadata'].get('uid'),
            node_name=node_name,node_uid=node['metadata'].get('uid'),system_uuid=info.get('systemUUID'),
            machine_id=info.get('machineID'),architecture=info.get('architecture'),operating_system=info.get('operatingSystem'),
            kernel_version=info.get('kernelVersion'),os_image=info.get('osImage'),container_runtime=info.get('containerRuntimeVersion'),
            capacity=node.get('status',{}).get('capacity'),observed_at=started,finished_at=end)
        hardware_identity(proof)
        return proof


def nf_fingerprint(row):
    value={k:row.get(k) for k in ('pod_uid','container_id','restart_count','resources','image','image_id',
                                'upf_epoch','netns','evidence_service_uid')}
    value['interfaces']={role:{k:v for k,v in entry.items() if k!='rx_bytes'}
                         for role,entry in row.get('interfaces',{}).items()}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def positive(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ContractError('invalid_native_path_calibration_rate')
    return value


def native_session_id(value):
    # Existing accepted round receipts serialize this identifier as decimal text;
    # the native SMF JSON uses an integer. Accept only canonical identities.
    if type(value) is str and value.isascii() and value.isdecimal() and str(int(value)) == value:
        value = int(value)
    if type(value) is not int or not 1 <= value <= 15:
        raise ContractError('native_path_pdu_session_identity_invalid')
    return value


def native_identity(row, census):
    key = SessionKey(census['network_scope'], row['supi'], row['sessionId'], row['smContextRef'])
    if (row.get('session_key') != key.token or row.get('establishedEpoch') != key.established_epoch
        or row.get('networkScope') != key.network_scope
        or row.get('smf_boot_epoch') != census['smf_boot_epoch']
        or not 0 < timestamp(row['created_at']) <= timestamp(census['observed_at'])):
        raise ContractError('native_path_current_session_identity_invalid')
    return key


def native_snssai(row):
    if type(row.get('sst')) is not int or not 0 <= row['sst'] <= 255 or not isinstance(row.get('sd'), str):
        raise ContractError('native_path_native_slice_missing')
    return f"{row['sst']}-{row['sd']}"


def calibration_cohort(census, bindings):
    """Exact full measured sender cohort, including non-target realtime_control PDUs."""
    if (census.get('source')!='native-smf-active-sessions' or census.get('complete') is not True
            or not census.get('smf_boot_epoch') or not isinstance(census.get('sessions'),list)):
        raise ContractError('native_calibration_full_census_missing')
    rows={r.get('session_key'):r for r in census['sessions']}
    if len(rows)!=len(census['sessions']) or set(rows)!=set(bindings):
        raise ContractError('native_calibration_full_cohort_changed')
    identities={}
    for key,row in rows.items():
        native_identity(row,census)
        binding=bindings[key]
        if (binding.get('sessionKey')!=key or binding.get('supi')!=row.get('supi')
                or native_session_id(binding.get('sessionId'))!=native_session_id(row.get('sessionId'))
                or binding.get('smContextRef')!=row.get('smContextRef')):
            raise ContractError('native_calibration_full_binding_changed')
        identities[key]={f:row.get(f) for f in ('session_key','networkScope','supi','sessionId','smContextRef',
            'establishedEpoch','created_at','smf_boot_epoch','upf_node_ip','upf_id','upfEpoch','dnn','sst','sd','pfcpSeid')}
    return identities


def calibration_envelope(evidence):
    """Validate raw receivers for every claimed simultaneous sender, never descriptors."""
    shape=evidence['load_shape'];rates=shape['all_rates_kbps'];bindings=evidence.get('all_job_bindings',{})
    windows=evidence.get('all_receiver_windows',())
    if set(bindings)!=set(rates) or len(windows)!=shape['all_jobs_count']:
        raise ContractError('native_calibration_full_receiver_coverage_missing')
    opening=calibration_cohort(evidence['native_census_before'],bindings)
    closing=calibration_cohort(evidence['native_census_after'],bindings)
    if opening!=closing:raise ContractError('native_calibration_full_incarnation_changed')
    seen=set();starts=[];ends=[]
    for window in windows:
        binding=window.get('binding',{});key=binding.get('sessionKey');direction=window.get('direction')
        pair=(key,direction)
        if key not in bindings or direction not in ('ul','dl') or pair in seen:
            raise ContractError('native_calibration_full_receiver_identity_changed')
        expected=bindings[key]
        if any(binding.get(f)!=v for f,v in expected.items()):
            raise ContractError('native_calibration_full_receiver_identity_changed')
        start,end=timestamp(window['startedAt']),timestamp(window['finishedAt'])
        offered=positive(window['offeredObservedMbps'])*1000
        received=positive(window['receivedMbps'])*1000
        if (window.get('source')!='native-iperf3-receiver' or end-start<5
                or not .98*rates[key][direction]<=offered<=1.02*rates[key][direction]
                or received>offered*1.02 or start<opening[key]['created_at']):
            raise ContractError('native_calibration_full_sender_window_unproven')
        seen.add(pair);starts.append(start);ends.append(end)
    if seen!={(key,d) for key in rates for d in ('ul','dl')} or min(ends)-max(starts)<5:
        raise ContractError('native_calibration_full_joint_window_missing')
    if (not timestamp(evidence['native_census_before']['observed_at'])<=min(starts)
            or not max(ends)<=timestamp(evidence['native_census_after']['observed_at'])<=timestamp(evidence['calibrated_at'])):
        raise ContractError('native_calibration_full_census_clock_changed')
    if (not evidence['nf_samples'][0]['observed_at']<=min(starts)
            or not max(ends)<=evidence['nf_samples'][1]['observed_at']
            or not evidence['hardware_proofs'][0]['observed_at']<=min(starts)
            or not max(ends)<=evidence['hardware_proofs'][1]['observed_at']):
        raise ContractError('native_calibration_full_envelope_clock_changed')
    selected=[w for w in windows if w['binding']['sessionKey'] in shape['session_keys']]
    if selected!=evidence['receiver_windows']:
        raise ContractError('native_calibration_selected_receivers_changed')


def current_envelope_clocks(census,bindings):
    """Original current clocks for ALL paths in the measured sender envelope."""
    clocks=[timestamp(census['observed_at'])]
    for row in census['sessions']:
        if row['session_key'] not in bindings:continue
        paths=[p for p in census.get('upfs',()) if p.get('node_ip')==row.get('upf_node_ip')]
        if (len(paths)!=1 or paths[0].get('upf_id')!=row.get('upf_id')
                or paths[0].get('upf_epoch')!=row.get('upfEpoch')):
            raise ContractError('native_path_current_full_envelope_path_changed')
        clocks.append(timestamp(paths[0]['observed_at']))
        if 'observed_at' in row:clocks.append(timestamp(row['observed_at']))
    return clocks


@dataclass(frozen=True)
class PathCalibration:
    profile_id:str
    evidence_file:str
    evidence_sha256:str
    valid_until:float
    safety_factor:float=.9

    def load(self,now):
        path=Path(self.evidence_file)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size>8*1024*1024 or not .1<=self.safety_factor<=.95):
            raise ContractError('invalid_native_path_calibration_evidence')
        encoded=path.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=self.evidence_sha256:
            raise ContractError('native_path_calibration_digest_changed')
        evidence=json.loads(encoded)
        at=timestamp(evidence['calibrated_at']);until=timestamp(self.valid_until)
        if (evidence.get('schema') not in {'cityverse.path-calibration/v1','cityverse.path-calibration/v2'} or not at<=now<until
            or until-at>3600 or evidence.get('profile_id')!=self.profile_id):
            raise ContractError('native_path_calibration_expired_or_invalid')
        for key in ('target_id','native_node_ip','native_upf_id','upf_epoch','dnn','s_nssai','nf_fingerprint'):
            if not evidence.get(key):raise ContractError('native_path_calibration_scope_missing')
        capacities={}
        v2=evidence['schema']=='cityverse.path-calibration/v2'
        if v2:
            hardware=evidence['hardware_proofs']
            if (len(hardware)!=2 or hardware_identity(hardware[0])!=hardware_identity(hardware[1])
                    or hardware[0]['target_id']!=evidence['target_id']
                    or any(h['pod_uid']!=evidence['nf_samples'][0]['pod_uid'] for h in hardware)):
                raise ContractError('native_path_calibration_hardware_changed')
            shape=evidence['load_shape']
            keys=shape.get('session_keys')
            rates=shape.get('rates_kbps')
            all_rates=shape.get('all_rates_kbps')
            if (not isinstance(keys,list) or not 1<=len(keys)<=32 or len(set(keys))!=len(keys)
                    or set(rates)!=set(keys) or type(shape.get('all_jobs_combined_kbps')) is not int
                    or type(shape.get('combined_budget_kbps')) is not int
                    or type(shape.get('all_jobs_count')) is not int or not 2<=shape['all_jobs_count']<=1000
                    or not 1<=shape['all_jobs_combined_kbps']<=shape['combined_budget_kbps']<=8000
                    or not isinstance(all_rates,dict) or len(all_rates)*2!=shape['all_jobs_count']
                    or not set(keys)<=set(all_rates)
                    or any(set(r)!= {'ul','dl'} or any(type(v) is not int or v<=0 for v in r.values()) for r in all_rates.values())
                    or sum(v for r in all_rates.values() for v in r.values())!=shape['all_jobs_combined_kbps']
                    or any(set(r)!= {'ul','dl'} or any(type(v) is not int or v<=0 for v in r.values()) for r in rates.values())):
                raise ContractError('native_path_calibration_load_shape_invalid')
            calibration_envelope(evidence)
            by_session={key:{} for key in keys}
        windows=evidence.get('receiver_windows',())
        if not 2<=len(windows)<=100:
            raise ContractError('native_path_calibration_receiver_windows_missing')
        for window in windows:
            binding=window.get('binding',{})
            native_session_id(binding.get('sessionId'))
            start,end=timestamp(window['startedAt']),timestamp(window['finishedAt'])
            received=positive(window['receivedMbps'])*1_000_000
            offered=positive(window['offeredObservedMbps'])*1_000_000
            direction=window.get('direction')
            if (window.get('source')!='native-iperf3-receiver' or direction not in ('ul','dl')
                or end-start<5 or not end<=at or at-end>120 or received>offered*1.02
                or not binding.get('supi') or not binding.get('smContextRef')):
                raise ContractError('native_path_calibration_window_not_verified')
            capacities.setdefault(direction,[]).append(received*self.safety_factor)
            if v2:
                key=window.get('binding',{}).get('sessionKey')
                if key not in by_session or offered>rates[key][direction]*1000*1.02:
                    raise ContractError('native_path_calibration_receiver_shape_changed')
                by_session[key].setdefault(direction,[]).append(received*self.safety_factor)
        if set(capacities)!={'ul','dl'}:
            raise ContractError('native_path_calibration_both_directions_required')
        # Separate directional runs do not establish that their sum is achievable.
        if not any(min(timestamp(u['finishedAt']), timestamp(d['finishedAt'])) -
                   max(timestamp(u['startedAt']), timestamp(d['startedAt'])) >= 5
                   for u in windows if u['direction'] == 'ul'
                   for d in windows if d['direction'] == 'dl'):
            raise ContractError('native_path_calibration_duplex_window_missing')
        before,after=evidence['native_census_before'],evidence['native_census_after']
        for census in (before,after):
            if (census.get('source')!='native-smf-active-sessions' or census.get('complete') is not True
                or not census.get('smf_boot_epoch') or census['smf_boot_epoch']!=before['smf_boot_epoch']):
                raise ContractError('native_path_calibration_census_not_verified')
            upfs = [u for u in census.get('upfs', ()) if u.get('node_ip') == evidence['native_node_ip']]
            if (len(upfs) != 1 or upfs[0].get('upf_id') != evidence['native_upf_id']
                or upfs[0].get('upf_epoch') != evidence['upf_epoch']
                or not timestamp(census['observed_at']) <= timestamp(upfs[0]['observed_at']) <= at
                or timestamp(upfs[0]['observed_at']) - timestamp(census['observed_at']) > 15):
                raise ContractError('native_path_calibration_upf_scope_mismatch')
        if not timestamp(before['observed_at'])<=min(timestamp(w['startedAt']) for w in windows):
            raise ContractError('native_path_calibration_census_order_invalid')
        if not max(timestamp(w['finishedAt']) for w in windows)<=timestamp(after['observed_at'])<=at:
            raise ContractError('native_path_calibration_census_order_invalid')
        for census in (before,after):
            cohort=[r for r in census['sessions'] if r.get('upf_node_ip')==evidence['native_node_ip']]
            if (v2 and {r.get('session_key') for r in cohort}!=set(keys)
                    or not v2 and len(cohort)!=1 or any(r.get('upfEpoch')!=evidence['upf_epoch'] for r in cohort)):
                raise ContractError('native_path_calibration_cohort_not_isolated')
            for row in cohort:
                native_identity(row, census)
                if (row.get('upf_id') != evidence['native_upf_id'] or row.get('dnn') != evidence['dnn']
                    or native_snssai(row) != evidence['s_nssai']):
                    raise ContractError('native_path_calibration_service_scope_mismatch')
            for window in windows:
                binding=window['binding'];matches=[r for r in cohort if r.get('supi')==binding.get('supi') and native_session_id(r.get('sessionId'))==native_session_id(binding.get('sessionId'))]
                if len(matches)!=1:raise ContractError('native_path_calibration_receiver_session_mismatch')
                row=matches[0]
                if (native_session_id(row.get('sessionId')) != native_session_id(binding.get('sessionId'))
                    or any(row.get(key)!=binding.get(key) for key in ('supi','smContextRef'))):
                    raise ContractError('native_path_calibration_receiver_session_mismatch')
                if timestamp(window['startedAt']) < timestamp(row['created_at']):
                    raise ContractError('native_path_calibration_receiver_predates_session')
        samples=evidence['nf_samples']
        if len(samples) != 2 or any(
            type(s.get('interfaces', {}).get(role, {}).get('rx_bytes')) is not int
            or not 0 <= s['interfaces'][role]['rx_bytes'] < 2**64
            for s in samples for role in ('n3', 'n6')):
            raise ContractError('native_path_calibration_nf_counter_invalid')
        if (len(samples)!=2 or any(nf_fingerprint(s)!=evidence['nf_fingerprint']
                                  or s.get('target_id') != evidence['target_id']
                                  or s.get('upf_epoch') != evidence['upf_epoch'] for s in samples)
            or not samples[0]['observed_at']<=min(timestamp(w['startedAt']) for w in windows)
            or not max(timestamp(w['finishedAt']) for w in windows)<=samples[1]['observed_at']<=at
            or any(samples[1]['interfaces'][role]['rx_bytes']<=samples[0]['interfaces'][role]['rx_bytes']
                   for role in ('n3','n6'))):
            raise ContractError('native_path_calibration_nf_scope_not_verified')
        if v2:
            if (not hardware[0]['observed_at']<=min(timestamp(w['startedAt']) for w in windows)
                    or not max(timestamp(w['finishedAt']) for w in windows)<=hardware[1]['observed_at']<=hardware[1]['finished_at']<=at
                    or not 0<=hardware[0]['finished_at']-hardware[0]['observed_at']<=8
                    or not 0<=hardware[1]['finished_at']-hardware[1]['observed_at']<=8
                    or any(set(d)!= {'ul','dl'} for d in by_session.values())):
                raise ContractError('native_path_calibration_hardware_clock_or_receiver_coverage')
            evidence['per_session_capacity_bps']={k:{d:min(values) for d,values in dirs.items()} for k,dirs in by_session.items()}
            capacities={d:[sum(row[d] for row in evidence['per_session_capacity_bps'].values())] for d in ('ul','dl')}
        wire_window=positive(samples[1]['observed_at']-samples[0]['observed_at'])
        evidence['calibrated_wire_load_bps']=sum(samples[1]['interfaces'][role]['rx_bytes']-
            samples[0]['interfaces'][role]['rx_bytes'] for role in ('n3','n6'))*8/wire_window
        evidence['common_duplex_window'] = next((
            {'startedAt':u['startedAt'],'finishedAt':u['finishedAt']}
            for u in windows if u['direction']=='ul' for d in windows if d['direction']=='dl'
            if timestamp(u['startedAt'])==timestamp(d['startedAt'])
            and timestamp(u['finishedAt'])==timestamp(d['finishedAt'])), None)
        return evidence,{direction:min(values) for direction,values in capacities.items()}


class NativeCalibratedPathCapacityReader:
    def __init__(self,api,nf_load_reader,profiles,*,hardware_reader=None,offered_reader=None,publication_store=None,clock=time.time):
        self.api,self.nf_load_reader,self.clock=api,nf_load_reader,clock
        self.hardware_reader=hardware_reader
        self.offered_reader=offered_reader
        self.publication_store=Path(publication_store) if publication_store is not None else None
        if self.publication_store is not None:
            if not self.publication_store.is_absolute() or self.publication_store.is_symlink():
                raise ContractError('fixed_path_capacity_store_required')
            self.publication_store.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(self.publication_store) as db:
                db.execute('CREATE TABLE IF NOT EXISTS native_path_current_profiles (target_id TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.profiles=tuple(PathCalibration(**p) for p in profiles)
        if (not self.profiles and self.publication_store is None) or len(self.profiles)>64:
            raise ContractError('bounded_native_path_calibration_required')
        # Validate pinned evidence on construction; time and hash are checked again on use.
        for profile in self.profiles:profile.load(clock())

    @staticmethod
    def _catalogue(db):
        rows=list(db.execute('SELECT target_id,value FROM native_path_current_profiles ORDER BY target_id LIMIT 65'))
        if len(rows)>64:raise ContractError('path_capacity_current_store_unbounded')
        return rows

    @staticmethod
    def _revision(rows):
        return hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()

    def registry_revision(self):
        if self.publication_store is None:raise ContractError('path_capacity_publication_not_configured')
        with sqlite3.connect(self.publication_store) as db:return self._revision(self._catalogue(db))

    def _current_profiles(self):
        profiles={p.profile_id:p for p in self.profiles}
        if self.publication_store is not None:
            with sqlite3.connect(self.publication_store) as db:rows=self._catalogue(db)
            for key,value in rows:
                profile=PathCalibration(**json.loads(value))
                if self.clock()>=profile.valid_until:continue
                evidence,_=profile.load(self.clock())
                if key!=evidence['target_id']:raise ContractError('path_capacity_current_identity_changed')
                profiles[profile.profile_id]=profile
        if len(profiles)>64:raise ContractError('path_capacity_current_store_unbounded')
        return tuple(profiles[k] for k in sorted(profiles))

    def publish_calibration(self,profile,sessions,*,expected_registry_revision):
        """Trusted local operator port. No public/model upload and no native write.

        Native reads complete before the short current-row CAS. Publication does
        not extend the original receipt or grant authority to a future cohort.
        Every normal capacity read repeats the current native evidence checks.
        """
        if type(profile) is not PathCalibration or self.publication_store is None:
            raise ContractError('typed_path_capacity_publication_required')
        evidence,_=profile.load(self.clock())
        if evidence['schema']!='cityverse.path-calibration/v2':
            raise ContractError('path_capacity_historical_profile_not_authority')
        encoded=json.dumps(vars(profile),sort_keys=True)
        target=evidence['target_id']
        with sqlite3.connect(self.publication_store) as db:
            opening=self._catalogue(db)
        if self._revision(opening)!=expected_registry_revision:
            if dict(opening).get(target)==encoded:return self._revision(opening)
            raise ContractError('path_capacity_publication_revision_changed')
        proofs=self._capacities(tuple(sessions),(profile,))
        if set(proofs)!=set(evidence['load_shape']['session_keys']):
            raise ContractError('path_capacity_publication_current_proof_missing')
        with sqlite3.connect(self.publication_store,timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            rows=self._catalogue(db)
            if self._revision(rows)!=expected_registry_revision:
                # Exact response-lost replay is read-only and never refreshes expiry.
                if dict(rows).get(target)==encoded:
                    return self._revision(rows)
                raise ContractError('path_capacity_publication_revision_changed')
            now=self.clock()  # After last lock/SQL read, before the sole current write.
            if (now>=profile.valid_until or any(not 0<=now-stamp<=15
                    for p in proofs.values() for stamp in p['original_source_clocks'])):
                raise ContractError('path_capacity_publication_proof_expired')
            old=dict(rows).get(target)
            if old and old!=encoded and json.loads(old)['evidence_sha256']==profile.evidence_sha256:
                raise ContractError('path_capacity_original_receipt_authority_changed')
            if target not in dict(rows) and len(rows)>=64:
                raise ContractError('path_capacity_current_store_unbounded')
            db.execute('INSERT OR REPLACE INTO native_path_current_profiles VALUES(?,?)',(target,encoded))
            return self._revision(self._catalogue(db))

    def current_capacities(self,sessions):
        return self._capacities(sessions,self._current_profiles())

    def _capacities(self,sessions,profiles):
        if not profiles:return {}
        inventory=self.api.active_inventory();now=self.clock()
        at=inventory.get('observed_at')
        if (inventory.get('source')!='native-smf-active-sessions' or inventory.get('complete') is not True
            or not inventory.get('smf_boot_epoch') or type(inventory.get('census_revision')) is not int
            or type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=15):
            raise ContractError('native_path_current_census_missing')
        known={s.key.token:s for s in sessions};native=inventory['sessions'];result={}
        if len(known) != len(sessions):raise ContractError('native_path_known_session_duplicate')
        if len(native)>4096:raise ContractError('native_path_current_census_unbounded')
        for row in native:
            native_identity(row, inventory)
        for profile in profiles:
            evidence,capacity=profile.load(now);target=evidence['target_id']
            # v1 remains readable evidence, never newly hardware-authorized capacity.
            if (evidence['schema']!='cityverse.path-calibration/v2' or not callable(self.hardware_reader)
                    or not callable(self.offered_reader)):continue
            # The lower bound was measured with ALL concurrent sender paths.
            # Retain legitimate QER owner/cap/version changes; immutable native
            # PDU/PFCP/UPF incarnation changes require a new measurement instead.
            bindings=evidence['all_job_bindings']
            try:
                if calibration_cohort(inventory,bindings)!=calibration_cohort(evidence['native_census_before'],bindings):continue
                envelope_clocks=current_envelope_clocks(inventory,bindings)
            except ContractError:continue
            hardware=self.hardware_reader(target)
            offered=self.offered_reader(sessions)
            now=self.clock()
            if (hardware_identity(hardware)!=hardware_identity(evidence['hardware_proofs'][0])
                    or not 0<=now-hardware['observed_at']<=15 or not 0<=now-at<=15):continue
            shape=evidence['load_shape'];rates=offered.get('rates_bps',{})
            if (offered.get('source')!='native-current-sender-load/v1' or not 0<=now-offered.get('observed_at',float('-inf'))<=15
                    or len(rates)*2!=shape['all_jobs_count']
                    or set(rates)!=set(shape['all_rates_kbps'])
                    or any(set(r)!= {'ul','dl'} or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in r.values()) for r in rates.values())
                    or sum(v for r in rates.values() for v in r.values())>shape['combined_budget_kbps']*1000
                    or any(rates[k][d]>shape['all_rates_kbps'][k][d]*1020 for k in rates for d in ('ul','dl'))
                    or any(k not in rates or set(rates[k])!= {'ul','dl'} or any(type(v) not in (int,float)
                        or not math.isfinite(v) or not 0<v<=shape['rates_kbps'][k][d]*1020 for d,v in rates[k].items())
                        for k in shape['session_keys'])):continue
            if inventory['smf_boot_epoch'] != evidence['native_census_before']['smf_boot_epoch']:continue
            cohort=[r for r in native if r.get('upf_node_ip')==evidence['native_node_ip']]
            if not cohort:continue
            if {r.get('session_key') for r in cohort}!=set(evidence['load_shape']['session_keys']):continue
            if (len({r['session_key'] for r in cohort})!=len(cohort)
                or any(r['session_key'] not in known or r.get('upfEpoch')!=evidence['upf_epoch']
                       for r in cohort)):
                continue
            if any(known[r['session_key']].dnn != r.get('dnn')
                   or known[r['session_key']].s_nssai != native_snssai(r)
                   or known[r['session_key']].upf_id != target for r in cohort):continue
            with self.nf_load_reader.lock:
                scope=self.nf_load_reader.previous.get(target)
                if not scope:continue
                scope=deepcopy(scope)
            if (scope.get('status')!='measured' or not 0<=now-scope['observed_at']<=15
                or nf_fingerprint(scope)!=evidence['nf_fingerprint']
                or type(scope.get('ingress_wire_bps')) not in (int,float)
                or not 0<=scope['ingress_wire_bps']<=evidence['calibrated_wire_load_bps']):
                continue
            upfs=[u for u in inventory.get('upfs',()) if u.get('node_ip')==evidence['native_node_ip']]
            if (len(upfs)!=1 or upfs[0].get('upf_epoch')!=evidence['upf_epoch']
                or upfs[0].get('upf_id')!=evidence['native_upf_id']
                or not 0<=now-upfs[0].get('observed_at',float('-inf'))<=15):continue
            for row in cohort:
                session=known[row['session_key']]
                if (session.upf_id!=target or session.dnn!=evidence['dnn']
                    or session.s_nssai!=evidence['s_nssai']
                    or row.get('smContextRef')!=session.key.established_epoch
                    or row.get('upf_id')!=upfs[0]['upf_id']):continue
                if session.key.token in result:
                    raise ContractError('ambiguous_native_path_calibration')
                # Conservative equal capacity budget for every actual NF user.
                # This is explicitly a model share, not a new receiver observation.
                proof={'source':'commissioned-path-capacity','verified':True,
                    'session_key':session.key.token,'path_revision':session.path_revision,
                    'observed_at':min(*envelope_clocks,scope['observed_at'],hardware['observed_at'],offered['observed_at']),
                    'original_source_clocks':tuple([*envelope_clocks,scope['observed_at'],hardware['observed_at'],offered['observed_at']]),
                    'ul_capacity_bps':evidence['per_session_capacity_bps'][session.key.token]['ul'],
                    'dl_capacity_bps':evidence['per_session_capacity_bps'][session.key.token]['dl'],
                    'calibration_profile_id':profile.profile_id,'calibrated_at':evidence['calibrated_at'],
                    'calibration_evidence_sha256':profile.evidence_sha256,'native_cohort_size':len(cohort),
                    'calibration_receiver_windows':deepcopy(evidence['receiver_windows']),
                    'common_duplex_window':deepcopy(evidence['common_duplex_window']),
                    'hardware_identity':hardware_identity(hardware),
                    'capacity_definition':'conservative achieved receiver lower-bound for exact measured cohort and load shape; not maximum capacity'}
                if evidence['common_duplex_window'] is not None:
                    proof['capacity_bps']=proof['ul_capacity_bps']+proof['dl_capacity_bps']
                result[session.key.token]=proof
        decision=self.clock()
        return {key:proof for key,proof in result.items()
                if all(0<=decision-stamp<=15 for stamp in proof['original_source_clocks'])
                and all(decision<p.valid_until for p in profiles)}
