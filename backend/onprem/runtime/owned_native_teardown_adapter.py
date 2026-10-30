"""Private current-state composition for owned-core retirement.

Prepare after policy restoration, before NAS retirement. Drain only after the UE
container is terminal. The caller supplies the runtime's shared mutation guard;
this module never creates a substitute lock or treats a session header as authority.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

from .current_sessions import session_rows, V2
from .owned_native_teardown import (
    CommissionedUpfBinding, CurrentTeardownEvidence, KernelView, OwnedAmfContext,
    OwnedAssociation, OwnedSession, OwnedTeardownTarget, SbiEndpoint, TeardownBlocked,
    _NoRedirect, _DeadlineHTTPHandler, _DeadlineHTTPSHandler, configured_owned_native_teardown,
)
from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease


def _time(value):
    return float(value) if type(value) in {int,float} else datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def _fresh(value,now,age=10):
    result=_time(value)
    if not -0.000001<=now-result<=age:
        raise TeardownBlocked(f'Current native adapter source is stale (age_seconds={now-result:.6f})')
    return result


@dataclass(frozen=True)
class UpfReadBinding:
    logical_target: str
    census_target_id: str
    native_upf_id: str
    node_ip: str
    evidence_origin: str
    current_session_target_id: str

    def __post_init__(self):
        CommissionedUpfBinding(self.logical_target,self.native_upf_id,self.node_ip,'configuration-check')
        SbiEndpoint(self.evidence_origin,'none')
        if not self.census_target_id or not self.current_session_target_id:
            raise ValueError('Explicit native census target identity required')


@dataclass(frozen=True)
class ProtectedNf:
    role: str
    pod_name: str
    container_name: str
    process_pid: int
    process_identity: str='container_proc'

    def __post_init__(self):
        if not all(isinstance(x,str) and x for x in (self.role,self.pod_name,self.container_name)) or type(self.process_pid) is not int or not 1<=self.process_pid<=4194304:
            raise ValueError('Commissioned native process selector required')
        if (self.process_identity not in {'container_proc','host_cri'}
                or (self.process_identity=='host_cri' and (self.role!='smf' or self.container_name!='smf' or self.process_pid!=1))):
            raise ValueError('Only explicitly selected SMF PID1 can use the commissioned host CRI reader')


class FixedOriginOamRead:
    """Bounded-size, explicit-origin native OAM reads; no public URL arguments."""
    def __init__(self,amf:SbiEndpoint,pcf:SbiEndpoint):
        self.amf,self.pcf=amf,pcf
        self.opener=build_opener(ProxyHandler({}),_NoRedirect(),_DeadlineHTTPHandler(),_DeadlineHTTPSHandler())

    def _get(self,endpoint,path):
        headers={}
        if endpoint.auth_mode=='bearer':
            token=endpoint.token_reader()
            if not isinstance(token,str) or not token.strip() or any(c in token for c in '\r\n'):
                raise TeardownBlocked('Native read authentication unavailable')
            headers['Authorization']='Bearer '+token.strip()
        try:
            with self.opener.open(Request(endpoint.origin.rstrip('/')+path,headers=headers),timeout=min(5,endpoint.timeout_seconds)) as response:
                raw=response.read(2*1024*1024+1)
                if response.status!=200 or len(raw)>2*1024*1024:
                    raise ValueError()
            return json.loads(raw)
        except Exception:
            raise TeardownBlocked('Current native OAM read unavailable') from None

    def amf_contexts(self):
        value=self._get(self.amf,'/namf-oam/v1/registered-ue-context')
        if value is None:
            return []
        if not isinstance(value,list) or len(value)>1000:
            raise TeardownBlocked('Complete AMF native census unavailable')
        return value

    def pcf_associations(self,supi):
        value=self._get(self.pcf,'/npcf-oam/v1/am-policy/'+quote(supi,safe=''))
        if not isinstance(value,list) or len(value)>30:
            raise TeardownBlocked('Exact native PCF association census unavailable')
        return value


class CurrentDrainAuthority:
    """Re-read only the current lease, release operation and generation."""
    def __init__(self,ledger,repository,generation_store,*,clock=time.time):
        self.ledger,self.repository,self.generation_store=ledger,repository,generation_store
        self.clock=clock
        self.journal=None

    def check(self,round_id):
        authority=self.ledger.intent_traffic_drain_authority(round_id)
        if not authority or type(authority.get('lease')) is not ServiceIntentTrafficLease:
            raise TeardownBlocked('Current typed owned drain authority is unavailable')
        lease,permit=authority['lease'],authority['permit']
        permit.validate(lease,self.clock())
        traffic=self.repository.current_intent_traffic(lease.intent_id,lease.owner_id)
        if not traffic or traffic.get('receipt',{}).get('service_intent_traffic_lease')!=lease.to_dict():
            raise TeardownBlocked('Current traffic lease changed')
        operation=self.repository.operation(permit.release_operation_id,lease.owner_id)
        if (operation.get('intent_id')!=lease.intent_id or operation.get('revision')!=lease.intent_revision
                or operation.get('kind')!='release' or operation.get('status') not in {'running','recovery_required'}
                or operation.get('policy_restoration_complete') is not True or operation.get('admissions_closed') is not True
                or operation.get('receipts') or operation.get('release_purpose','full_release')!='full_release'):
            raise TeardownBlocked('Policy restoration or release operation authority is incomplete')
        # Read-only direct lease check; no transaction survives into native IO.
        uri=self.repository.path.resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True,timeout=3)) as db:
            row=db.execute('SELECT operation_id FROM leases WHERE scope=?',('intent:'+lease.intent_id,)).fetchone()
        if row!=(permit.release_operation_id,):
            raise TeardownBlocked('Release operation lost its intent resource lease')
        current=self.ledger.current_service_scope()
        if current.get('roundId')!=round_id or current.get('schemaVersion')!=V2:
            raise TeardownBlocked('Only the current typed device/session round can own fallback cleanup')
        fleet=self.generation_store.current()
        generation=fleet['generation']
        if generation==current.get('generation'):
            from .ue import population_revision,EvidenceUnavailable
            try:
                if population_revision(current)!=population_revision(fleet):
                    raise TeardownBlocked('Current population revision changed before owned drain')
            except EvidenceUnavailable:
                raise TeardownBlocked('Invalid current population revision before owned drain') from None
        bound=self.journal.drain_binding(permit.release_operation_id) if self.journal else None
        if bound and bound[1] is not None and generation==current.get('generation'):
            raise TeardownBlocked('Committed owned zero generation was rewound')
        if generation!=current.get('generation'):
            from .owned_drain import OwnedNormalStopRepository
            if not bound or bound[1] is None:
                raise TeardownBlocked('Current UE generation changed without an exact owned stop receipt')
            baseline,receipt=bound
            if (baseline.round_id!=round_id or baseline.original_generation!=current.get('generation')
                    or baseline.lease!=lease.to_dict()
                    or baseline.permit['release_operation_id']!=permit.release_operation_id):
                raise TeardownBlocked('Owned zero receipt authority changed')
            OwnedNormalStopRepository(self.generation_store).validate(receipt)
            if OwnedNativeTeardownAdapter._snapshot_digest(current)!=baseline.snapshot_sha256:
                raise TeardownBlocked('Immutable original device/session scope changed after stop')
        session_rows(current)
        if self.repository.other_current_policy_sessions(lease.intent_id):
            raise TeardownBlocked('Other services still own native session policies')
        return lease,permit,current


class NativeTeardownSource:
    """Actual SMF/OAM/kernel/Kubernetes readers; all selectors are private configuration."""
    def __init__(self,*,smf_api,oam,collector,driver,upfs,protected_nfs,configuration_seal,
                 configuration_seal_reader,clock=time.time,host_process_reader=None,current_upfs=None):
        self.smf_api,self.oam,self.collector,self.driver=smf_api,oam,collector,driver
        self.upfs,self.protected_nfs=tuple(upfs),tuple(protected_nfs)
        self.configuration_seal,self.configuration_seal_reader=configuration_seal,configuration_seal_reader
        self.clock=clock
        self.host_process_reader=host_process_reader
        self.current_upfs=current_upfs
        if current_upfs is not None:
            from backend.onprem.intents.current_upf_scope import CurrentUpfSource
            if type(current_upfs) is not CurrentUpfSource:raise ValueError('Trusted current registry topology source required')
        if len(self.upfs)!=4 or {u.logical_target for u in self.upfs}!={'embb','urllc','mmtc','v2x'}:
            raise ValueError('Four commissioned native UPF read bindings required')
        if any(collector.upf_urls.get(u.logical_target,'').rstrip('/')!=u.evidence_origin.rstrip('/') for u in self.upfs):
            raise ValueError('Collector UPF origins must match the commissioned crosswalk')
        if not {'amf','smf','pcf','embb','urllc','mmtc','v2x'}<={n.role for n in self.protected_nfs} or len({n.role for n in self.protected_nfs})!=len(self.protected_nfs):
            raise ValueError('Complete exact protected NF process selectors required')
        if (sum(n.process_identity=='host_cri' for n in self.protected_nfs)>1
                or any(n.process_identity=='host_cri' for n in self.protected_nfs)!=(host_process_reader is not None)):
            raise ValueError('Host process reader must match one explicit protected SMF selector')

    def pods(self,*,deadline=None):
        if deadline is None:
            result=self.driver.read_pods()
        else:
            reader=getattr(self.driver,'_metadata_pod_reader',None)
            if (type(deadline) not in {int,float} or not math.isfinite(deadline)
                    or time.monotonic()>=deadline or reader is None
                    or not callable(getattr(reader,'pods',None))):
                raise TeardownBlocked('Deadline-aware native Pod census reader required')
            # Never call the driver's convenience method here: it issues a new
            # five-second budget, which can extend this original census window.
            result=reader.pods(deadline=deadline)
            if time.monotonic()>=deadline:
                raise TeardownBlocked('Original native Pod census deadline expired')
        if (not isinstance(result,dict) or not isinstance(result.get('items'),list)
                or len(result['items'])>4096 or result.get('metadata',{}).get('continue')):
            raise TeardownBlocked('Current Kubernetes Pod census unavailable')
        return result['items']

    def validate_inventory(self,pods,devices,topology=None):
        owned={d['podUid'] for d in devices}
        protected={s.pod_name for s in self.protected_nfs}
        if topology is not None:
            for row in topology.rows.values():
                matches=[p for p in pods if p.get('metadata',{}).get('name')==row.slot.pod_name]
                if len(matches)!=1 or matches[0]['metadata'].get('uid')!=row.binding.pod_uid:
                    raise TeardownBlocked('Owned topology Pod incarnation missing from complete census')
                protected.add(row.slot.pod_name)
        if (len({p.get('metadata',{}).get('name') for p in pods})!=len(pods)
                or any(p.get('metadata',{}).get('namespace')!=self.driver.namespace for p in pods)
                or {p['metadata']['name'] for p in pods if p.get('metadata',{}).get('uid') not in owned}!=protected):
            raise TeardownBlocked('Protected NF and exact owned UE selectors do not cover complete namespace census')

    def nf_identities(self,*,selectors=None):
        selectors=self.protected_nfs if selectors is None else tuple(selectors)
        if not selectors or len(selectors)>64:raise TeardownBlocked('Bounded explicit native process scope required')
        observed_at=self.clock()
        deadline=time.monotonic()+8
        opening_pods=self.pods(deadline=deadline)
        pods={p['metadata']['name']:p for p in opening_pods}
        if len(pods)!=len(opening_pods) or any(not p.get('metadata',{}).get('uid') for p in opening_pods):
            raise TeardownBlocked('Complete opening Pod identities are unavailable')
        def read(selector):
            if time.monotonic()>=deadline:raise TeardownBlocked('Protected NF process budget expired')
            pod=pods.get(selector.pod_name,{})
            meta=pod.get('metadata',{})
            statuses=[c for c in pod.get('status',{}).get('containerStatuses',()) if c.get('name')==selector.container_name]
            if meta.get('namespace')!=self.driver.namespace or not meta.get('uid') or meta.get('deletionTimestamp') or len(statuses)!=1:
                raise TeardownBlocked('Protected native NF Pod identity unavailable')
            status=statuses[0]
            if not status.get('containerID') or not status.get('state',{}).get('running') or status.get('ready') is not True:
                raise TeardownBlocked('Protected native NF process is not running')
            # PID is commissioned explicitly; neither nfInstanceId nor Pod age is a process epoch.
            if selector.process_identity=='host_cri':
                from .host_cri_process_identity import HostCriProcessProof
                proof=self.host_process_reader.read(pod,selector,deadline=deadline)
                if type(proof) is not HostCriProcessProof:
                    raise TeardownBlocked('Typed original host process observation proof required')
                if (proof.opening_observed_at<observed_at-.000001
                        or not -.000001<=self.clock()-proof.closing_observed_at<=10):
                    raise TeardownBlocked('Host process source clock is stale or predates this census')
                epoch=proof.identity
            else:
                script=f"set -eu; cat /proc/sys/kernel/random/boot_id; awk '{{print $22}}' /proc/{selector.process_pid}/stat; readlink /proc/{selector.process_pid}/exe"
                result=self.driver.runner(['kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,
                    'exec',selector.pod_name,'-c',selector.container_name,'--','sh','-ec',script],
                    text=True,capture_output=True,timeout=max(.001,min(5,deadline-time.monotonic())),check=False)
                values=result.stdout.strip().splitlines()
                if result.returncode or len(values)!=3 or not values[1].isdigit() or not values[0] or not values[2].startswith('/'):
                    raise TeardownBlocked('Protected native NF process epoch unavailable')
                epoch=json.dumps(values,separators=(',',':'))
            if time.monotonic()>deadline:raise TeardownBlocked('Protected NF process budget expired')
            return selector.role,epoch,json.dumps([meta['uid'],status['containerID'],epoch],separators=(',',':'))
        # Bound parallel reads, drain every future, preserve the original window
        # start. The complete closing Pod census below supplies every selected
        # process's post-read UID/full-container-status fence, after ALL process
        # futures have drained; do not issue a redundant per-role Pod GET.
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows=list(pool.map(read,selectors))
        closing_pods=self.pods(deadline=deadline)
        def membership(values):
            return sorted((p.get('metadata',{}).get('namespace'),p.get('metadata',{}).get('name'),
                p.get('metadata',{}).get('uid')) for p in values)
        if membership(closing_pods)!=membership(list(pods.values())) or time.monotonic()>deadline:
            raise TeardownBlocked('Complete namespace Pod membership changed during NF process census')
        closing_by_name={p['metadata']['name']:p for p in closing_pods}
        for selector in selectors:
            def container(pod):return [s for s in pod.get('status',{}).get('containerStatuses',[]) if s.get('name')==selector.container_name]
            if container(closing_by_name[selector.pod_name])!=container(pods[selector.pod_name]):
                raise TeardownBlocked('Protected native container changed at complete census closing fence')
        epochs={role:epoch for role,epoch,identity in rows}
        identities={role:identity for role,epoch,identity in rows}
        if self.configuration_seal_reader()!=self.configuration_seal:
            raise TeardownBlocked('Private native cleanup configuration seal changed')
        return tuple(sorted(identities.items())),epochs,observed_at

    def topology_read(self,expected=None):
        if self.current_upfs is None:
            if expected is not None:raise TeardownBlocked('Pinned topology reader is no longer configured')
            return None
        from .owned_upf_topology import CurrentOwnedTopology
        return CurrentOwnedTopology(self,expected)

    def census(self,topology=None):
        value=self.smf_api.active_inventory()
        if (not isinstance(value,dict) or value.get('source')!='native-smf-active-sessions' or value.get('complete') is not True
                or not isinstance(value.get('sessions'),list) or len(value['sessions'])>3000 or not isinstance(value.get('upfs'),list)):
            raise TeardownBlocked('Complete current native SMF census unavailable')
        at=_fresh(value['observed_at'],self.clock())
        bindings=[]
        if topology is not None:
            from .owned_native_teardown import PinnedCommissionedUpfBinding,PinnedOwnedSession
            topology.scope.validate_census(value)
            if topology.pin is None:raise TeardownBlocked('Topology process identity must be pinned before census')
            for member in topology.pin.members:
                bindings.append(PinnedCommissionedUpfBinding(member.logical_target,member.native_upf_id,member.node_ip,member.kernel_epoch))
            active={m.native_upf_id for m in topology.pin.registered}
            if any(row.get('upf_id') not in active for row in value['sessions']):
                raise TeardownBlocked('Session is outside registered physical topology')
            session_type=PinnedOwnedSession
        else:session_type=OwnedSession
        for configured in (() if topology is not None else self.upfs):
            rows=[u for u in value['upfs'] if (u.get('target_id'),u.get('upf_id'),u.get('node_ip'))==
                (configured.census_target_id,configured.native_upf_id,configured.node_ip)]
            if len(rows)!=1:
                raise TeardownBlocked('Current native UPF crosswalk differs from commissioning')
            at=min(at,_fresh(rows[0]['observed_at'],self.clock()))
            bindings.append(CommissionedUpfBinding(configured.logical_target,configured.native_upf_id,configured.node_ip,rows[0]['upf_epoch']))
        sessions=tuple(session_type.from_native_row(row,smf_epoch=value['smf_boot_epoch'],upf_bindings=bindings) for row in value['sessions'])
        for row in value['sessions']:
            at=min(at,_fresh(row['observed_at'],self.clock()))
        return value,sessions,tuple(bindings),at

    def verify_running_device(self,pod,device):
        name=pod['metadata']['name']
        script="readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id; /ueransim/nr-cli --dump"
        result=self.driver.runner(['kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,
            'exec',name,'-c','ue','--','sh','-ec',script],text=True,capture_output=True,timeout=5,check=False)
        values=result.stdout.strip().splitlines()
        if (result.returncode or len(values)!=4 or values[0]!='/ueransim/nr-ue' or not values[1].isdigit()
                or values[2]+':'+values[1]!=device['processEpoch'] or values[3]!=device['supi']):
            raise TeardownBlocked('Current UE process incarnation or native SUPI differs from the round')
        after=self.driver.read_pod(name)
        statuses=[c for c in after.get('status',{}).get('containerStatuses',()) if c.get('name')=='ue']
        if (after.get('metadata',{}).get('uid')!=device['podUid'] or len(statuses)!=1
                or statuses[0].get('containerID')!=device['containerId'] or not statuses[0].get('state',{}).get('running')):
            raise TeardownBlocked('Owned UE changed during baseline process observation')

    def kernels(self,bindings,topology=None):
        views=[]
        for binding in bindings:
            # Reuse the commissioned collector validation and native gtp5g source.
            if topology is None:row=self.collector._upf(binding.logical_target)
            else:
                from backend.onprem.intents.resource_adapters import KubectlResourceClient
                target=topology.current_target(binding.logical_target)
                row=topology.scope.kernel(target,KubectlResourceClient(context=self.driver.context,
                    namespace=self.driver.namespace,runner=self.driver.runner,timeout=5),
                    cleanup=not topology.rows[target].native_registered)
            at=_fresh(row['observedAt'],self.clock())
            views.append(KernelView(binding.logical_target,row['epoch'],at,len(row['sessions']),len(row['residuals']),
                available=row['available'],source=row['source'],errors=tuple(row['errors']),
                native_upf_id=binding.native_upf_id,node_ip=binding.node_ip))
        return tuple(views)


class RuntimeOwnedTeardownReader:
    def __init__(self,source,authority,round_id):
        self.source,self.authority,self.round_id=source,authority,round_id

    def snapshot(self,target,*,pre_nas=False):
        lease,permit,current=self.authority.check(self.round_id)
        if (target.lease_id,target.owner_id,target.operation_id,target.generation)!=(lease.lease_id,lease.owner_id,permit.release_operation_id,current['generation']):
            raise TeardownBlocked('Target no longer owns current runtime drain')
        devices=[d for d in current['devices'] if d.get('supi')==target.supi and d.get('podUid')==target.pod_uid]
        if len(devices)!=1 or (devices[0].get('containerId'),devices[0].get('processEpoch'))!=(target.container_id,target.process_epoch):
            raise TeardownBlocked('Current round device process identity changed')
        authority_at=self.source.clock()
        topology=self.source.topology_read(target.upf_topology)
        identities,epochs,identity_at=self.source.nf_identities()
        if topology is not None:topology.capture_processes(identities,identity_at)
        native,sessions,bindings,smf_at=self.source.census(topology) if topology is not None else self.source.census()
        amf_rows=self.source.oam.amf_contexts()
        amf_at=self.source.clock()
        if len({r['Supi'] for r in amf_rows})!=len(amf_rows):
            raise TeardownBlocked('Duplicate complete AMF context census')
        matches=[r for r in amf_rows if r['Supi']==target.supi]
        amf=OwnedAmfContext.from_native_row(matches[0],amf_epoch=epochs['amf']) if matches else None
        policies=self.source.oam.pcf_associations(target.supi)
        pcf_at=self.source.clock()
        associations=tuple(OwnedAssociation.from_native_oam_row(row,pcf_epoch=epochs['pcf'],
            queried_supi=target.supi,expected_supi=target.supi) for row in policies)
        pods=self.source.pods()
        self.source.validate_inventory(pods,current['devices'],topology) if topology is not None else self.source.validate_inventory(pods,current['devices'])
        selected=[p for p in pods if p.get('metadata',{}).get('uid')==target.pod_uid]
        journal=self.authority.journal
        if pre_nas:
            if len(selected)!=1:
                raise TeardownBlocked('Pre-NAS owned UE Pod is not exactly present')
            self.source.verify_running_device(selected[0],devices[0])
        elif not selected:
            # An exact terminal process was already observed under the current
            # release fence. Fresh complete Pod census proves it is still gone.
            if not journal or journal.terminal(target) is None:
                raise TeardownBlocked('Owned UE Pod absence has no persisted exact terminal proof')
            device_name=devices[0].get('podName')
            if device_name and any(p.get('metadata',{}).get('name')==device_name for p in pods):
                raise TeardownBlocked('Owned device Pod name was reused')
        elif len(selected)==1:
            pod=selected[0]
            statuses=[c for c in pod.get('status',{}).get('containerStatuses',()) if c.get('name')=='ue']
            if (pod.get('metadata',{}).get('namespace')!=target.namespace or len(statuses)!=1
                    or statuses[0].get('containerID')!=target.container_id or not statuses[0].get('state',{}).get('terminated')):
                raise TeardownBlocked('Exact owned UE container terminal quiescence is not confirmed')
            quiet=getattr(self.source.driver,'_quiescent_device_sidecar',None)
            if callable(quiet) and quiet(pod) is not True:
                raise TeardownBlocked('Owned UE traffic sidecar is not independently quiescent')
            if journal:journal.terminal(target,self.source.clock())
        else:raise TeardownBlocked('Owned UE Pod census has duplicate identities')
        terminal_at=self.source.clock()
        kernels=self.source.kernels(bindings,topology) if topology is not None else self.source.kernels(bindings)
        if pre_nas:
            closing_identities,closing_epochs,closing_at=self.source.nf_identities()
            closing_native,closing_sessions,closing_bindings,closing_smf_at=(self.source.census(topology) if topology is not None else self.source.census())
            if (closing_identities!=identities or closing_epochs!=epochs or closing_bindings!=bindings
                    or closing_native['smf_boot_epoch']!=native['smf_boot_epoch']
                    or {s.session_key:s for s in closing_sessions}!={s.session_key:s for s in sessions}):
                raise TeardownBlocked('Native NF or session configuration changed during pre-NAS proof')
            # Keep opening clocks; closing reads cannot make earlier proof fresh.
            _fresh(closing_at,self.source.clock())
            _fresh(closing_smf_at,self.source.clock())
        final_lease,final_permit,final_current=self.authority.check(self.round_id)
        if (lease,permit.release_operation_id,current['generation'])!=(final_lease,final_permit.release_operation_id,final_current['generation']):
            raise TeardownBlocked('Current native release authority changed during observation')
        if topology is not None:topology.finish()
        return CurrentTeardownEvidence(observed_at=min(authority_at,identity_at,smf_at,amf_at,pcf_at,terminal_at),
            operation_id=target.operation_id,lease_id=target.lease_id,owner_id=target.owner_id,generation=target.generation,
            supi=target.supi,pod_uid=target.pod_uid,namespace=target.namespace,container_id=target.container_id,
            process_epoch=target.process_epoch,configuration_seal=self.source.configuration_seal,protected_nf_identities=identities,
            admissions_closed=True,policy_restoration_complete=True,amf_absent=amf is None,amf_observed_at=amf_at,
            ue_terminal=not pre_nas,quiescence_observed_at=terminal_at,smf_complete=True,smf_epoch=native['smf_boot_epoch'],sessions=sessions,
            pcf_epoch=epochs['pcf'],associations=tuple(a.association_id for a in associations),kernels=kernels,
            smf_observed_at=smf_at,pcf_observed_at=pcf_at,authority_observed_at=authority_at,protected_nf_observed_at=identity_at,
            upf_bindings=bindings,amf_context=amf,amf_epoch=epochs['amf'],amf_complete=True,amf_context_count=len(amf_rows),
            pcf_queried_supi=target.supi,pcf_access_types=tuple((a.association_id,'3GPP_ACCESS') for a in associations),
            upf_topology=topology.pin if topology is not None else None)


class CurrentDrainGuard:
    """Borrow the runtime-wide mutation guard; never a new unrelated local lock."""
    def __init__(self,authority,round_id,mutation_guard):
        if not callable(mutation_guard):
            raise ValueError('Shared runtime mutation guard is required')
        self.authority,self.round_id,self.mutation_guard=authority,round_id,mutation_guard

    @contextmanager
    def __call__(self,target):
        with self.mutation_guard():
            lease,permit,current=self.authority.check(self.round_id)
            if (target.lease_id,target.owner_id,target.operation_id,target.generation)!=(lease.lease_id,lease.owner_id,permit.release_operation_id,current['generation']):
                raise TeardownBlocked('Mutation guard does not own this native teardown target')
            yield
            self.authority.check(self.round_id)


class OwnedNativeTeardownAdapter:
    """Two-phase private cleanup adapter; no route, restart or automatic NAS dispatch."""
    def __init__(self,source,authority,round_id,*,native_config,mutation_guard):
        self.source,self.authority,self.round_id=source,authority,round_id
        self.reader=RuntimeOwnedTeardownReader(source,authority,round_id)
        self.guard=CurrentDrainGuard(authority,round_id,mutation_guard)
        self.port=configured_owned_native_teardown(native_config,reader=self.reader,authority_guard=self.guard)
        self.authority.journal=self.port.journal

    def prepare_drain_baseline(self,snapshot=None):
        """Durable pre-stop capture or exact readback; never recapture after stop."""
        from .owned_native_teardown import OwnedDrainBaseline
        from .owned_drain import authority,sha,OwnedNormalStopRepository
        # Hydrate an already committed zero receipt BEFORE checking generation.
        raw=self.authority.ledger.intent_traffic_drain_authority(self.round_id)
        if not raw:raise TeardownBlocked('Current drain authority unavailable')
        operation_id=raw['permit'].release_operation_id
        bound=self.port.journal.drain_binding(operation_id)
        if bound:
            baseline,receipt=bound
            persisted=OwnedNormalStopRepository(self.authority.generation_store).replay(baseline)
            if persisted is not None:self.bind_stop_receipt(persisted)
            self.authority.check(self.round_id)
            if self._snapshot_digest(self.authority.ledger.current_service_scope())!=baseline.snapshot_sha256:
                raise TeardownBlocked('Accepted original drain snapshot changed')
            return baseline
        lease,permit,current=self.authority.check(self.round_id)
        if snapshot is None:raise TeardownBlocked('Accepted pre-stop V2 snapshot is required')
        reader=getattr(self.authority.ledger,'control_snapshot',None)
        accepted=reader() if callable(reader) else snapshot
        if (accepted.get('roundId')!=self.round_id or accepted.get('requestId')!=lease.request_id
                or self._snapshot_digest(accepted)!=self._snapshot_digest(current)
                or self._snapshot_digest(snapshot)!=self._snapshot_digest(current)):
            raise TeardownBlocked('Accepted current V2 allocation snapshot changed')
        original=self.authority.generation_store.read_generation(current['generation'])
        if not original:raise TeardownBlocked('Original owned allocation unavailable')
        allocation_sha=authority(lease,permit,current['generation'],original['allocation'],accepted,self.source.clock())
        if not callable(getattr(self.source.driver,'_quiescent_device_sidecar',None)):
            raise TeardownBlocked('Native driver terminal/idle-sidecar proof is not configured')
        if self.source.current_upfs is None:self.source.validate_inventory(self.source.pods(),current['devices'])
        targets=self.pin_baseline()
        baseline=OwnedDrainBaseline(self.round_id,lease.to_dict(),permit.to_dict(),current['generation'],allocation_sha,
            self._snapshot_digest(current),sha(sorted(t.digest for t in targets)),self.source.configuration_seal,
            upf_topology=targets[0].upf_topology)
        self.port.journal.bind_drain(baseline)
        return baseline

    @staticmethod
    def _snapshot_digest(current):
        from .owned_drain import sha
        from .current_sessions import IDENTITY_FIELDS
        fields=('deviceId','supi','podUid','generation','containerId','processEpoch','sessionKeys')
        population_revision=current.get('populationRevision',0)
        if type(population_revision) is not int or population_revision<0:
            raise TeardownBlocked('Invalid current population revision')
        return sha({k:current.get(k) for k in ('roundId','schemaVersion','generation')}|{
            'populationRevision':population_revision,
            'devices':[{k:d.get(k) for k in fields} for d in current.get('devices',[])],
            'perSession':[{k:s.get(k) for k in IDENTITY_FIELDS} for s in current.get('perSession',[])]})

    def bind_stop_receipt(self,receipt):
        from .owned_drain import OwnedNormalStopRepository,sha
        bound=self.port.journal.drain_binding(receipt.baseline.permit['release_operation_id'])
        if not bound or bound[0]!=receipt.baseline:
            raise TeardownBlocked('Zero receipt has no exact pre-stop baseline')
        if sha(sorted(t.digest for t in self.port.journal.targets(receipt.baseline.permit['release_operation_id'])))!=receipt.baseline.targets_sha256:
            raise TeardownBlocked('Immutable pre-stop targets changed')
        if any(t.upf_topology!=receipt.baseline.upf_topology for t in self.port.journal.targets(receipt.baseline.permit['release_operation_id'])):
            raise TeardownBlocked('Owned zero receipt topology differs from immutable target cohort')
        OwnedNormalStopRepository(self.authority.generation_store).validate(receipt)
        self.port.journal.bind_drain(receipt.baseline,receipt)

    def _prepared_nas(self,target,pod,dispatch):
        from .kubeadm import KubeadmDriver,KubeadmMutation,KubeadmMutationKind
        import types,sys
        code=next(c for c in KubeadmDriver.retire_owned_pod.__code__.co_consts
            if isinstance(c,types.CodeType) and c.co_name=='dispatch')
        if (type(dispatch) is not types.FunctionType or dispatch.__code__ is not code
                or dispatch.__globals__ is not vars(sys.modules[KubeadmDriver.__module__])):
            raise TeardownBlocked('Prepared NAS producer is not the exact driver port')
        captured=dict(zip(dispatch.__code__.co_freevars,(c.cell_contents for c in dispatch.__closure__)))
        prepared=getattr(dispatch,'native_dispatch_request',None)
        if (type(prepared) is not KubeadmMutation or prepared.kind is not KubeadmMutationKind.NAS_DEREGISTER
                or prepared.context!=self.source.driver.context or prepared.namespace!=target.namespace
                or json.loads(prepared.target_json)!={'pod':pod,'supis':[target.supi]}
                or prepared.input_text is not None):
            raise TeardownBlocked('Exact prepared normal-owned NAS request required')
        actual_driver=getattr(self.source.driver,'_driver',getattr(self.source.driver,'driver',self.source.driver))
        if captured.get('prepared') is not prepared or captured.get('self') is not actual_driver:
            raise TeardownBlocked('Prepared NAS closure or exact driver changed')
        expected=('kubectl','--context',prepared.context,'--namespace',prepared.namespace,'exec',
            pod['metadata']['name'],'-c','ue','--','sh','-c',
            KubeadmDriver._nas_script([target.supi],'deregister switch-off'))
        if prepared.argv!=expected:
            raise TeardownBlocked('Prepared NAS command differs from exact normal-owned switch-off')
        return prepared

    def _nas_previous_request(self,target):
        # One bounded request-identity row in the EXISTING native journal DB.
        # NAS status/dispatch authority stays solely in the original NAS table.
        # Never adopt a preexisting uncertain dispatch as a new attempt.
        with self.port.journal.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS owned_teardown_nas_request_v1 '
                '(target_digest TEXT PRIMARY KEY,request_binding TEXT NOT NULL)')
            row=db.execute('SELECT request_binding FROM owned_teardown_nas_request_v1 WHERE target_digest=?',(target.digest,)).fetchone()
            return row[0] if row else None

    def _nas_request_binding(self,target,prepared,binding):
        return json.dumps({'schema':'owned-nas-request/v1','request_id':prepared.request_id,
            'request_sha256':prepared.request_sha256,'body_sha256':prepared.body_sha256,
            'target_digest':target.digest,'pod_uid':target.pod_uid,'baseline_digest':binding[0].digest,
            'release_operation_id':target.operation_id},sort_keys=True,separators=(',',':'))

    def _check_nas_binding(self,db,target,binding):
        baseline=db.execute('SELECT baseline,stop_receipt FROM owned_teardown_drain_bindings_v1 WHERE operation_id=?',(target.operation_id,)).fetchone()
        pinned=db.execute('SELECT digest FROM owned_teardown_targets_v2 WHERE operation_id=? AND target_id=?',
            (target.operation_id,target.identity)).fetchone()
        if (not baseline or json.loads(baseline[0])!=binding[0].to_dict()
                or not baseline[1] or json.loads(baseline[1])!=binding[1].to_dict()
                or not pinned or pinned[0]!=target.digest):
            raise TeardownBlocked('Original NAS target or normal-stop nonce changed')

    def _reserve_nas_request(self,target,prepared,binding,previous):
        encoded=self._nas_request_binding(target,prepared,binding)
        with self.port.journal.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._check_nas_binding(db,target,binding)
            state=db.execute('SELECT status FROM owned_teardown_nas_v1 WHERE target_digest=?',(target.digest,)).fetchone()
            row=db.execute('SELECT request_binding FROM owned_teardown_nas_request_v1 WHERE target_digest=?',(target.digest,)).fetchone()
            if (row[0] if row else None)!=previous:
                raise TeardownBlocked('NAS request attempt CAS changed')
            if state is None:
                # A surviving request identity is evidence of a prior attempt.
                # Missing canonical authority is corruption, never fresh scope.
                if row is not None:raise TeardownBlocked('Original NAS dispatch authority missing; readback only')
                db.execute("INSERT INTO owned_teardown_nas_v1 VALUES(?,'dispatch_reserved')",(target.digest,))
            elif state==('blocked_not_sent',):
                changed=db.execute("UPDATE owned_teardown_nas_v1 SET status='dispatch_reserved' WHERE target_digest=? AND status='blocked_not_sent'",(target.digest,)).rowcount
                if changed!=1:raise TeardownBlocked('NAS request attempt CAS changed')
            else:return None
            if row:
                changed=db.execute('UPDATE owned_teardown_nas_request_v1 SET request_binding=? WHERE target_digest=? AND request_binding=?',
                    (encoded,target.digest,previous)).rowcount
                if changed!=1:raise TeardownBlocked('NAS request attempt CAS changed')
            else:db.execute('INSERT INTO owned_teardown_nas_request_v1 VALUES(?,?)',(target.digest,encoded))
        return encoded

    def _block_nas_request_not_sent(self,target,prepared,binding,encoded):
        from .owned_drain import OwnedNormalStopRepository
        OwnedNormalStopRepository(self.authority.generation_store).validate(binding[1])
        if encoded!=self._nas_request_binding(target,prepared,binding):
            raise TeardownBlocked('Original NAS request proof changed')
        with self.port.journal.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._check_nas_binding(db,target,binding)
            changed=db.execute("UPDATE owned_teardown_nas_v1 SET status='blocked_not_sent' WHERE target_digest=? "
                "AND status='dispatch_reserved' AND EXISTS(SELECT 1 FROM owned_teardown_nas_request_v1 r "
                "WHERE r.target_digest=owned_teardown_nas_v1.target_digest AND r.request_binding=?)",(target.digest,encoded)).rowcount
            if changed!=1:raise TeardownBlocked('Original not-sent NAS request CAS changed')
        # The GenerationStore and native journal are separate databases. A
        # post-CAS drift is reported; this never claims a cross-DB atomic commit.
        OwnedNormalStopRepository(self.authority.generation_store).validate(binding[1])

    def nas_shutdown(self,pod,dispatch):
        """One exact switch-off dispatch; uncertain ACK is readback-only forever."""
        with self.guard.mutation_guard():
            lease,permit,current=self.authority.check(self.round_id)
            binding=self.port.journal.drain_binding(permit.release_operation_id)
            if not binding or binding[1] is None:
                raise TeardownBlocked('NAS shutdown requires committed typed owned zero receipt')
            from .owned_drain import OwnedNormalStopRepository
            OwnedNormalStopRepository(self.authority.generation_store).validate(binding[1])
            targets=[t for t in self.port.journal.targets(permit.release_operation_id)
                if t.pod_uid==pod.get('metadata',{}).get('uid')]
            if len(targets)!=1:raise TeardownBlocked('NAS shutdown Pod is not exactly one pinned physical UE')
            target=targets[0]
            prepared=self._prepared_nas(target,pod,dispatch)
            previous_request=self._nas_previous_request(target)
            if (self.source.configuration_seal!=target.configuration_seal
                    or self.source.configuration_seal_reader()!=target.configuration_seal):
                raise TeardownBlocked('Current native teardown configuration changed before NAS')
            devices=[d for d in current['devices'] if d['podUid']==target.pod_uid]
            if len(devices)!=1:raise TeardownBlocked('Current owned NAS device changed')
            pinned=self.port.journal.targets(permit.release_operation_id)
            if {(t.supi,t.pod_uid) for t in pinned}!={(d['supi'],d['podUid']) for d in current['devices']}:
                raise TeardownBlocked('Pre-NAS baseline does not cover the current owned cohort')
            evidence=self.reader.snapshot(target,pre_nas=True)
            allowed=tuple(s for t in pinned for s in t.sessions)
            self.port.validate_before_nas(target,evidence,allowed)
            if any(t.protected_nf_identities!=evidence.protected_nf_identities for t in pinned):
                raise TeardownBlocked('Pre-NAS protected NF incarnation differs from pinned cohort')
            if self.source.configuration_seal_reader()!=target.configuration_seal:
                raise TeardownBlocked('Current native teardown configuration changed during NAS identity proof')
            final_lease,final_permit,final_scope=self.authority.check(self.round_id)
            if (final_lease!=lease or final_permit.release_operation_id!=permit.release_operation_id
                    or self._snapshot_digest(final_scope)!=self._snapshot_digest(current)
                    or self.port.journal.drain_binding(permit.release_operation_id)!=binding
                    or self.port.journal.targets(permit.release_operation_id)!=pinned):
                raise TeardownBlocked('Current pre-NAS ownership or immutable baseline changed during proof')
            # Recheck original source ages after final durable/config reads.
            self.port.validate_before_nas(target,evidence,allowed)
            request_binding=self._reserve_nas_request(target,prepared,binding,previous_request)
            if request_binding is None:
                raise TeardownBlocked('Owned NAS already dispatched; await exact terminal readback without replay')
            try:
                final_lease,final_permit,final_scope=self.authority.check(self.round_id)
                if (final_lease!=lease or final_permit.release_operation_id!=permit.release_operation_id
                        or self._snapshot_digest(final_scope)!=self._snapshot_digest(current)
                        or self.port.journal.drain_binding(permit.release_operation_id)!=binding
                        or self.port.journal.targets(permit.release_operation_id)!=pinned):
                    raise TeardownBlocked('Owned NAS reservation lost its exact current authority')
                if (self.source.configuration_seal!=target.configuration_seal
                        or self.source.configuration_seal_reader()!=target.configuration_seal):
                    raise TeardownBlocked('Current native teardown configuration changed before NAS dispatch')
                self.port.validate_before_nas(target,evidence,allowed)
                final_permit.validate(final_lease,self.authority.clock())
            except BaseException:
                # Dispatch has not been entered. Persist this fact rather than
                # confusing a locally blocked reservation with an unknown send.
                self._block_nas_request_not_sent(target,prepared,binding,request_binding)
                raise
            from .kubeadm import NativeDispatchNotSent
            try:
                value=dispatch()
            except NativeDispatchNotSent as not_sent:
                if type(not_sent) is not NativeDispatchNotSent or not not_sent.matches(prepared):
                    raise TeardownBlocked('Untrusted or foreign NAS no-send proof') from None
                self._block_nas_request_not_sent(target,prepared,binding,request_binding)
                raise
            self.authority.check(self.round_id)
            return target,value

    def acknowledge_nas(self,target):self.port.journal.acknowledge_nas(target)

    def pin_baseline(self):
        """Called under the shared runtime guard after policy restore, BEFORE NAS retirement."""
        with self.guard.mutation_guard():
            lease,permit,current=self.authority.check(self.round_id)
            topology=self.source.topology_read()
            identities,epochs,identity_at=self.source.nf_identities()
            if topology is not None:topology.capture_processes(identities,identity_at)
            native,sessions,bindings,_=(self.source.census(topology) if topology is not None else self.source.census())
            if topology is not None:
                kernels=self.source.kernels(bindings,topology)
                if any(k.residual_count or k.session_count!=sum(
                        s.native_upf_id==k.native_upf_id and s.upf_node_ip==k.node_ip for s in sessions) for k in kernels):
                    raise TeardownBlocked('Pre-stop full physical topology has unaccounted kernel state')
            rows=session_rows(current)
            rows_by_key={r['sessionKey']:r for r in rows}
            if {s.session_key for s in sessions}!=set(rows_by_key):
                raise TeardownBlocked('Native baseline does not exactly cover the current owned device sessions')
            for session in sessions:
                row=rows_by_key[session.session_key]
                configured=next((u for u in self.source.upfs if u.logical_target==session.upf_id),None)
                current_target=configured.current_session_target_id if configured is not None else topology.current_target(session.upf_id)
                if (session.sm_context_ref,current_target,session.native_upf_id,session.upf_epoch)!=(
                        row['smContextRef'],row['upf'],row['nativeUpfId'],row['upfEpoch']):
                    raise TeardownBlocked('Current round and native baseline session identity differ')
            amf_rows=self.source.oam.amf_contexts()
            if len({r['Supi'] for r in amf_rows})!=len(amf_rows) or not {r['Supi'] for r in amf_rows}<={d['supi'] for d in current['devices']}:
                raise TeardownBlocked('Native AMF baseline contains unowned contexts')
            pods=self.source.pods()
            if topology is not None:self.source.validate_inventory(pods,current['devices'],topology)
            targets=[]
            for device in current['devices']:
                matches=[p for p in pods if p.get('metadata',{}).get('uid')==device['podUid']]
                if len(matches)!=1:
                    raise TeardownBlocked('Current owned device Pod is missing during baseline pin')
                pod=matches[0]
                statuses=[c for c in pod.get('status',{}).get('containerStatuses',()) if c.get('name')=='ue']
                if (len(statuses)!=1 or statuses[0].get('containerID')!=device['containerId']
                        or not statuses[0].get('state',{}).get('running')):
                    raise TeardownBlocked('Current owned device container changed during baseline pin')
                self.source.verify_running_device(pod,device)
                amf=[r for r in amf_rows if r['Supi']==device['supi']]
                policies=self.source.oam.pcf_associations(device['supi'])
                target=OwnedTeardownTarget(permit.release_operation_id,lease.lease_id,lease.owner_id,current['generation'],
                    device['supi'],device['podUid'],self.source.driver.namespace,device['containerId'],device['processEpoch'],
                    self.source.configuration_seal,identities,tuple(s for s in sessions if json.loads(s.session_key)[1]==device['supi']),
                    tuple(OwnedAssociation.from_native_oam_row(row,pcf_epoch=epochs['pcf'],queried_supi=device['supi'],expected_supi=device['supi']) for row in policies),
                    OwnedAmfContext.from_native_row(amf[0],amf_epoch=epochs['amf']) if amf else None,
                    upf_topology=topology.pin if topology is not None else None)
                targets.append(target)
            self.authority.check(self.round_id)
            if topology is not None:topology.finish()
            for target in targets:self.port.journal.bind(target)
            return tuple(targets)

    def retire(self,targets=None):
        """Returns owned-core-contexts-only; normal Pod/RAN/full release proof must follow."""
        lease,permit,current=self.authority.check(self.round_id)
        pinned=self.port.journal.targets(permit.release_operation_id)
        if not pinned:
            raise TeardownBlocked('Native baseline must be pinned before NAS retirement')
        if {(t.supi,t.pod_uid) for t in pinned}!={(d['supi'],d['podUid']) for d in current['devices']}:
            raise TeardownBlocked('Pinned baseline does not cover the complete current owned device population')
        if targets is None:
            targets=pinned
        elif {t.digest for t in targets}!={t.digest for t in pinned}:
            raise TeardownBlocked('Supplied targets differ from the immutable pre-NAS baseline')
        return self.port.retire_many(tuple(targets))


def configured_owned_native_teardown_adapter(context,*,round_id,source,native_config,mutation_guard):
    """Private DI entry point; the owning cleanup path supplies its existing shared guard."""
    authority=CurrentDrainAuthority(context.round_ledger,context.intent_service.repository,context.ue_controller.store)
    return OwnedNativeTeardownAdapter(source,authority,round_id,native_config=native_config,mutation_guard=mutation_guard)


def configured_owned_native_teardown_factory(context,environment):
    """Server-pinned lazy composition; absent configuration enables no native writes.

    All source URLs, NF selectors, credentials references and four-UPF mappings
    come from one SHA256-pinned private startup file, never intent/API input.
    Context services are resolved at drain time after application composition.
    """
    filename=environment.get('CITYVERSE_ONPREM_NATIVE_TEARDOWN_CONFIG_FILE')
    digest=environment.get('CITYVERSE_ONPREM_NATIVE_TEARDOWN_CONFIG_SHA256')
    if filename is None and digest is None:return None
    if not isinstance(filename,str) or not isinstance(digest,str) or len(digest)!=64:
        raise ValueError('Pinned private native teardown file and SHA256 required')
    path=Path(filename)
    def read_private(path,limit):
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or (os.name!='nt' and path.stat().st_mode&0o077)):
            raise TeardownBlocked('Private native teardown configuration/credential reference is unsafe')
        with path.open('rb') as stream:raw=stream.read(limit+1)
        if len(raw)>limit:raise TeardownBlocked('Private native teardown file exceeds bound')
        return raw
    def seal():return hashlib.sha256(read_private(path,1024*1024)).hexdigest()
    raw=read_private(path,1024*1024)
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Pinned native teardown configuration SHA256 mismatch')
    config=json.loads(raw)
    required={'schema','native','smf_read','amf_read','pcf_read','upfs','protected_nfs'}
    if not required<=set(config) or set(config)-required-{'host_cri','registry_read'} or config['schema']!='owned-native-teardown-configuration/v1':
        raise ValueError('Complete pinned native teardown composition required')
    def token_reader(filename):
        token_path=Path(filename)
        # Resolve reference permissions at startup without printing its content.
        if not token_path.is_absolute() or token_path.is_symlink() or not token_path.is_file():
            raise ValueError('Private native credential reference required')
        return lambda:read_private(token_path,16384).decode('utf-8').strip()
    def endpoint(value):
        if not isinstance(value,dict) or set(value)-{'origin','auth_mode','token_file','timeout_seconds'}:
            raise ValueError('Exact native read endpoint configuration required')
        return SbiEndpoint(value['origin'],value['auth_mode'],
            token_reader(value['token_file']) if value['auth_mode']=='bearer' else None,value.get('timeout_seconds',5))
    amf,pcf=endpoint(config['amf_read']),endpoint(config['pcf_read'])
    smf_config=config['smf_read']
    if set(smf_config)!={'origin','token_file'}:raise ValueError('Exact native SMF QER read reference required')
    from backend.onprem.intents.resource_adapters import FixedOriginSessionQerApi
    smf=FixedOriginSessionQerApi(smf_config['origin'],token_reader=token_reader(smf_config['token_file']))
    upfs=tuple(UpfReadBinding(**row) for row in config['upfs'])
    protected=tuple(ProtectedNf(**row) for row in config['protected_nfs'])
    current_upfs=None
    if 'registry_read' in config:
        registry=config['registry_read']
        if not isinstance(registry,dict) or set(registry)!={'origin','token_file'}:
            raise ValueError('Exact private registry read authority required')
        from backend.onprem.intents.current_upf_scope import CurrentUpfSource
        from backend.onprem.intents.commissioned_upfs import CommissionedUpfRegistry
        from backend.onprem.intents.resource_adapters import NativeControllerClient,FixedOriginUpfControlApi
        control=FixedOriginUpfControlApi(NativeControllerClient(registry['origin'],token_reader=token_reader(registry['token_file'])))
        current_upfs=CurrentUpfSource(CommissionedUpfRegistry(control.current_registry),require_service_descriptors=False)
    host_binding=None
    if 'host_cri' in config:
        from .host_cri_process_identity import HostCriBinding
        host_binding=HostCriBinding(**config['host_cri'])
    if any(n.process_identity=='host_cri' for n in protected)!=(host_binding is not None):
        raise ValueError('Explicit protected SMF selector and host CRI binding must both be commissioned')
    def factory(snapshot,mutation_guard):
        if seal()!=digest:raise TeardownBlocked('Pinned native teardown configuration changed')
        from backend.onprem.evidence.collector import NativeCollector
        collector=context.ue_controller.adapter
        if not isinstance(collector,NativeCollector) or collector.driver is not context.ue_controller.driver:
            raise TeardownBlocked('Owned teardown requires the existing native collector and sole UE driver')
        host_reader=None
        if host_binding is not None:
            from .host_cri_process_identity import HostCriProcessIdentityReader,FixedNodeMetadataRead
            host_reader=HostCriProcessIdentityReader(host_binding,FixedNodeMetadataRead(
                getattr(context.ue_controller.driver,'_metadata_pod_reader',None),host_binding))
        source=NativeTeardownSource(smf_api=smf,oam=FixedOriginOamRead(amf,pcf),collector=collector,
            driver=context.ue_controller.driver,upfs=upfs,protected_nfs=protected,
            configuration_seal=digest,configuration_seal_reader=seal,host_process_reader=host_reader,current_upfs=current_upfs)
        return configured_owned_native_teardown_adapter(context,round_id=snapshot['roundId'],source=source,
            native_config=config['native'],mutation_guard=mutation_guard)
    return factory
