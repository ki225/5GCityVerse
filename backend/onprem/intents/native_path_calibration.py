"""Bounded native receiver calibration of an already owned, established session.

This runner never creates UEs, changes QERs, or invents a common traffic window.
The caller supplies the live NativeRoundResources instance and its current
durable round snapshot. Ordinary round fences remain authoritative throughout.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from .domain import ContractError
from .native_path_capacity import (PathCalibration, nf_fingerprint, native_session_id, hardware_identity,
                                   calibration_cohort, calibration_envelope)


class NativePathCalibrationRunner:
    def __init__(self, resources, native_api, nf_load_reader, *, hardware_reader=None, clock=time.time):
        self.resources, self.native_api, self.nf_load_reader = resources, native_api, nf_load_reader
        self.clock = clock
        self.hardware_reader=hardware_reader
        self.last_observation = None

    async def run(self, snapshot, *, profile_id, target_id, offered_mbps=None, duration_seconds=10,
                  target_session_key=None, target_session_keys=None, rates_kbps=None, functional_combined_budget_kbps=8000):
        began=time.monotonic();timings={}
        self.last_observation={'state':'validating','profile_id':profile_id,'target_id':target_id,'timings_seconds':timings}
        from backend.onprem.runtime.current_sessions import session_rows
        rows=session_rows(snapshot)
        v2=rates_kbps is not None
        if not v2 or offered_mbps is not None:
            raise ContractError('explicit_combined_duplex_calibration_rates_required')
        if target_session_keys is not None:
            if (target_session_key is not None or not isinstance(target_session_keys,(list,tuple))
                    or not 1<=len(target_session_keys)<=32 or len(set(target_session_keys))!=len(target_session_keys)):
                raise ContractError('native_calibration_exact_cohort_required')
            selected=[r for r in rows if r.get('sessionKey') in target_session_keys]
            if len(selected)!=len(target_session_keys):raise ContractError('native_calibration_exact_cohort_required')
        elif target_session_key is None:
            selected=rows if len(rows)==1 else []
        else:
            if not isinstance(target_session_key,str) or not target_session_key:
                raise ContractError('native_calibration_exact_session_key_required')
            selected=[r for r in rows if r.get('sessionKey')==target_session_key]
        if ((not v2 and (type(offered_mbps) is not int or not 8 <= offered_mbps <= 10))
                or type(duration_seconds) is not int or not 8 <= duration_seconds <= 15
                or not isinstance(profile_id, str) or not profile_id or len(profile_id) > 128
                or not selected or not v2 and len(selected)!=1 or not snapshot.get('roundId')):
            raise ContractError('bounded_owned_single_session_calibration_required')
        if any(r.get('upf') and r['upf']!=target_id for r in selected):
            raise ContractError('native_calibration_target_upf_mismatch')
        # Real jobs retain exact Pod/receiver UIDs, native TUN binding and the
        # existing immutable roundFence. No stand-alone synthetic lease exists.
        phase=time.monotonic();jobs = await self.resources.traffic_jobs(snapshot)
        all_jobs_count=len(jobs)
        keys={r.get('sessionKey') for r in selected}
        selected_jobs=[j for j in jobs if j.get('binding',{}).get('sessionKey') in keys] if target_session_key is not None or target_session_keys is not None else jobs
        if v2:
            if (type(functional_combined_budget_kbps) is not int or not 2<=functional_combined_budget_kbps<=8000
                    or type(rates_kbps) is not dict or set(rates_kbps)!= {'ul','dl'}
                    or any(type(r) is not int or r<=0 for r in rates_kbps.values())):
                raise ContractError('native_calibration_combined_budget_invalid')
            combined=0;all_rates={}
            for job in jobs:
                if job in selected_jobs:rate=rates_kbps.get(job.get('direction'))
                else:
                    raw=job.get('offered_mbps');rate=raw*1000 if type(raw) in (int,float) else None
                if type(rate) not in (int,float) or rate<=0 or int(rate)!=rate:
                    raise ContractError('native_calibration_full_job_envelope_missing')
                combined+=int(rate)
                key=job.get('binding',{}).get('sessionKey');direction=job.get('direction')
                if not isinstance(key,str) or not key or direction not in ('ul','dl') or direction in all_rates.setdefault(key,{}):
                    raise ContractError('native_calibration_full_job_cohort_missing')
                all_rates[key][direction]=int(rate)
            if any(set(r)!= {'ul','dl'} for r in all_rates.values()):
                raise ContractError('native_calibration_full_job_cohort_missing')
            if combined>functional_combined_budget_kbps:raise ContractError('native_calibration_full_job_budget_exceeded')
            if not callable(self.hardware_reader):raise ContractError('native_path_hardware_reader_missing')
            first_hardware=await asyncio.to_thread(self.hardware_reader,target_id)
            hardware_identity(first_hardware)
            if first_hardware['target_id']!=target_id or not 0<=self.clock()-first_hardware['observed_at']<=15:
                raise ContractError('native_path_hardware_proof_stale')
        timings['native_job_preflight']=time.monotonic()-phase
        if len(selected_jobs)!=2*len(selected) or {j.get('direction') for j in selected_jobs} != {'ul', 'dl'}:
            raise ContractError('native_calibration_exact_duplex_jobs_required')
        current_bindings={r.get('sessionKey'):r for r in rows}
        if len(current_bindings)!=len(rows) or set(current_bindings)!=set(all_rates):
            raise ContractError('native_calibration_full_owned_cohort_missing')
        proof_fields=('sessionKey','supi','sessionId','smContextRef','upfEpoch','nativeUpfId',
                      'dnn','sst','sd','tun','ueIp','podUid','containerId','processEpoch')
        all_bindings={k:{f:v for f,v in row.items() if f in proof_fields} for k,row in current_bindings.items()}
        for job in jobs:
            key=job['binding']['sessionKey']
            if any(job['binding'].get(f)!=v for f,v in all_bindings[key].items()):
                raise ContractError('native_calibration_full_job_binding_changed')
            job['offered_mbps'], job['duration'] = all_rates[key][job['direction']]/1000, duration_seconds
        phase=time.monotonic();before = await asyncio.to_thread(self.native_api.active_inventory)
        timings['native_census_before']=time.monotonic()-phase
        self.last_observation['native_census_before']=before
        calibration_cohort(before,all_bindings)  # BEFORE any sender dispatch.
        binding = selected[0]
        matches = [r for r in before.get('sessions', ())
                   if r.get('supi') == binding.get('supi')
                   and native_session_id(r.get('sessionId')) == native_session_id(binding.get('sessionId'))
                   and r.get('smContextRef') == binding.get('smContextRef')]
        if before.get('complete') is not True or len(matches) != 1:
            raise ContractError('native_calibration_current_session_missing')
        row = matches[0]
        cohort=[r for r in before['sessions'] if r.get('upf_node_ip')==row.get('upf_node_ip')]
        if (v2 and {r.get('session_key') for r in cohort}!=keys or not v2 and len(cohort)!=1):
            raise ContractError('native_calibration_shared_nf_not_isolated')
        phase=time.monotonic();first = await asyncio.to_thread(self.nf_load_reader.sample, target_id)
        timings['native_nf_before']=time.monotonic()-phase
        self.last_observation['nf_samples']=[first]
        if v2 and (first_hardware['pod_uid']!=first['pod_uid']
                or not 0<=self.clock()-first_hardware['observed_at']<=15
                or not 0<=self.clock()-before['observed_at']<=15):
            raise ContractError('native_path_hardware_or_census_stale_before_measurement')
        # Shield and drain: cancelling a Python await cannot stop native work
        # already running in to_thread. Never return while a listener is owned.
        active = asyncio.gather(*(self.resources.sample(j) for j in jobs), return_exceptions=True)
        phase=time.monotonic()
        try:
            results = await asyncio.shield(active)
        except asyncio.CancelledError:
            while not active.done():
                try:
                    await asyncio.shield(active)
                except asyncio.CancelledError:
                    continue
            active.result()
            raise
        timings['receiver_parallel']=time.monotonic()-phase
        self.last_observation['receiver_windows']=[r for r in results if not isinstance(r,BaseException)]
        phase=time.monotonic();second = await asyncio.to_thread(self.nf_load_reader.sample, target_id)
        timings['native_nf_after']=time.monotonic()-phase
        self.last_observation['nf_samples'].append(second)
        phase=time.monotonic();after = await asyncio.to_thread(self.native_api.active_inventory)
        timings['native_census_after']=time.monotonic()-phase
        self.last_observation['native_census_after']=after
        if v2:
            second_hardware=await asyncio.to_thread(self.hardware_reader,target_id)
            if hardware_identity(first_hardware)!=hardware_identity(second_hardware):
                raise ContractError('native_path_hardware_changed')
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            raise ContractError('native_calibration_receiver_measurement_failed') from failures[0]
        evidence = {
            'schema':'cityverse.path-calibration/v1', 'profile_id':profile_id,
            'calibrated_at':self.clock(), 'target_id':target_id, 'native_node_ip':row['upf_node_ip'],
            'native_upf_id':row['upf_id'], 'upf_epoch':row['upfEpoch'],
            'dnn':row['dnn'], 's_nssai':f"{row['sst']}-{row['sd']}",
            'nf_fingerprint':nf_fingerprint(first), 'nf_samples':[first, second],
            'native_census_before':before, 'native_census_after':after,
            'receiver_windows':deepcopy([r for j,r in zip(jobs,results) if j in selected_jobs]),
            'runner_timings_seconds':{**timings,'total':time.monotonic()-began},
            'measurement_definition':'simultaneous independent UDP direction jobs; separate actual windows; achieved lower-bound budgets, not maximum capacity',
        }
        if v2:
            evidence.update(schema='cityverse.path-calibration/v2',hardware_proofs=[first_hardware,second_hardware],
                all_job_bindings=all_bindings,all_receiver_windows=deepcopy(results),
                load_shape={'session_keys':sorted(keys),'rates_kbps':{k:dict(rates_kbps) for k in keys},
                    'all_rates_kbps':all_rates,
                    'all_jobs_combined_kbps':combined,'combined_budget_kbps':functional_combined_budget_kbps,
                    'all_jobs_count':all_jobs_count})
            calibration_envelope(evidence)
        self.last_observation={'state':'collected_uncommissioned','evidence':evidence}
        return evidence

    def pin_receipt(self, evidence, evidence_file, *, valid_seconds=900, safety_factor=.9):
        """Write a concrete operator-chosen receipt, then validate before returning a profile.

        The caller must not configure a profile until this method succeeds.
        Output contains network measurement metadata and raw iperf reports, never
        AKA material or credentials. Existing evidence is never overwritten.
        """
        path = Path(evidence_file)
        if (not path.is_absolute() or path.exists() or path.is_symlink()
                or type(valid_seconds) is not int or not 1 <= valid_seconds <= 3600):
            raise ContractError('native_calibration_unique_absolute_receipt_required')
        raw = json.dumps(evidence, sort_keys=True, allow_nan=False).encode()
        if len(raw) > 8 * 1024 * 1024:
            raise ContractError('native_calibration_receipt_unbounded')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        profile = PathCalibration(evidence['profile_id'], str(path), hashlib.sha256(raw).hexdigest(),
                                  evidence['calibrated_at'] + valid_seconds, safety_factor)
        profile.load(self.clock())
        return profile
