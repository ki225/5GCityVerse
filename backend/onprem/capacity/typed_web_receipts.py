"""Separate typed100 evidence: private full cohort, original clocks, exact scope."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import time
import sqlite3
import math

from .typed_web import (CONFIG, MANIFEST, MAX_BYTES, ScopedVerifiedTrafficCapacityGrant,
    ScopedCapacityBlocked, digest, encoded, need, read_pinned, request_scope, runtime_scope, validate_protocol)
from .performance import scoped_result_capacity_windows, _flow
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, session_rows
from backend.onprem.runtime.ue import seconds
from backend.onprem.runtime.ue_reservation import HeldUEAllocation
from backend.onprem.traffic.v2_batch import typed_sampling_view, wire_binding
from backend.onprem.evidence.scenario_receipts import released, RELEASE

CAPTURE='cityverse.typed-web100-capacity-capture/v1'
WINDOW='cityverse.scoped-capacity-window-origin/v1'

def window_origin(snapshot,protocol_sha):
    records=[a for a in snapshot.get('actions',[]) if a.get('kind')=='scoped_capacity_window']
    need(len(records)==1 and records[0].get('accepted') is True,'scoped_capacity_window_missing_or_duplicate')
    origin=records[0].get('windowOrigin',{})
    need(type(origin) is dict and set(origin)=={'schemaVersion','protocolSha256','roundId','generation',
        'cycle','phase','windowId','originSequence','declaredAt'}
        and origin.get('schemaVersion')==WINDOW and origin.get('protocolSha256')==protocol_sha
        and origin.get('roundId')==snapshot['roundId'] and origin.get('generation')==snapshot['generation']
        and origin.get('cycle')==1 and type(origin['cycle']) is int
        and origin.get('phase')=='measuring_result' and type(origin.get('originSequence')) is int
        and origin['originSequence']>=0 and type(origin.get('declaredAt')) in (int,float)
        and math.isfinite(origin['declaredAt']), 'scoped_capacity_window_origin')
    import uuid
    uuid.UUID(origin['windowId'])
    previous=snapshot['actions'][:snapshot['actions'].index(records[0])]
    need(not any(a.get('kind') in {'traffic_window','traffic_aggregate'} and a.get('stage')=='measuring_result'
        for a in previous),'scoped_capacity_window_declared_late')
    return origin

def archived_actions(path,round_id,ids):
    """Read only explicitly named first-window receipts, never choose a later case."""
    need(type(ids) is list and len(ids)<=22 and len(set(ids))==len(ids),'scoped_capacity_archive_scope')
    if not ids:return []
    with sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,timeout=10) as db:
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_traffic_window_archive'").fetchone():return []
        owner=db.execute("SELECT json_extract(snapshot,'$.generation') FROM rounds WHERE round_id=?",(round_id,)).fetchone()
        need(owner is not None,'scoped_capacity_archive_owner')
        rows=[json.loads(row[0]) for identifier in ids for row in db.execute(
            'SELECT receipt FROM intent_traffic_window_archive WHERE round_id=? AND action_id=?',(round_id,identifier))]
        from backend.onprem.evidence import batch_receipts as b
        from backend.onprem.evidence.report_receipts import ReportReceiptRepository
        scope=dict(roundId=round_id,generation=owner[0])
        # Current-state hydration deliberately rejects archived owners. This
        # explicit offline reader verifies only the named immutable archive
        # index and its original raw/canonical bytes; never grant live ownership.
        for item in rows:
            diagnostics=item.get('workerDiagnostics') or {}
            if 'rawRecordsRef' not in diagnostics:
                need('reportsRef' not in item,'scoped_capacity_archive_raw_missing')
                continue
            raw=b.receipt(db,scope,item)
            index=db.execute('SELECT binding_sha,raw_sha,reports_sha FROM batch_archived_receipts '
                'WHERE round_id=? AND action_id=?',(round_id,item['id'])).fetchone()
            need(index is not None and index[:2]==raw[:2]
                and diagnostics.pop('rawRecordsRef')=={'schema':'cityverse.batch-raw/v1','sha256':raw[1]}
                and 'rawRecords' not in diagnostics,'scoped_capacity_archive_binding')
            diagnostics['rawRecords']=json.loads(raw[2])
            if 'reportsRef' in item:
                canonical=db.execute('SELECT binding_sha,reports_sha,reports_json FROM canonical_batch_reports '
                    'WHERE round_id=? AND action_id=?',(round_id,item['id'])).fetchone()
                need(canonical is not None and canonical[:2]==(index[0],index[2])
                    and item.pop('reportsRef')==ReportReceiptRepository._reference(canonical[1])
                    and 'reports' not in item,'scoped_capacity_archive_reports')
                item['reports']=ReportReceiptRepository(db)._payload(item,canonical)
        return rows

def released_snapshot(path,round_id):
    """Released scoped traffic is independent of a model's policy outcome."""
    from backend.onprem.evidence import batch_receipts
    path=Path(path).resolve(strict=True)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10) as db:
        db.execute('PRAGMA query_only=ON')
        row=db.execute('SELECT snapshot,released FROM rounds WHERE round_id=?',(round_id,)).fetchone()
        need(row is not None and row[1]==1,'scoped_capacity_local_round_not_released')
        value=batch_receipts.hydrate(db,json.loads(row[0]),copy_snapshot=False)
    need(value.get('roundId')==round_id and released(value),'scoped_capacity_local_cleanup_incomplete')
    return value

def body_from_snapshot(snapshot):
    return dict(snapshot['allocation'],requestId=snapshot['requestId'],strategy=snapshot['strategy'],
        trafficMode=snapshot['trafficMode'],serviceRequirements=snapshot['serviceRequirements']) | {
        k:snapshot['allocation'][k] for k in ('topologyRevision','demandGroups') if k in snapshot['allocation']}

def scope_from_snapshot(snapshot):
    body=body_from_snapshot(snapshot);body.pop('total',None)
    return request_scope(body)

def trial_record(snapshot, protocol_sha):
    rows=[a for a in snapshot.get('actions',[]) if a.get('kind')=='scoped_capacity_trial']
    need(len(rows)==1 and rows[0].get('protocolSha256')==protocol_sha and rows[0].get('accepted') is True,
         'scoped_capacity_predeclaration_missing')
    allocations=[a for a in snapshot.get('actions',[]) if a.get('kind')=='ue_allocation']
    need(allocations and snapshot['actions'].index(rows[0])<snapshot['actions'].index(allocations[0]),
         'scoped_capacity_predeclaration_late')
    return rows[0]

def validate_native_capture(capture, snapshot, protocol, protocol_sha):
    need(capture.get('schemaVersion')==CAPTURE and capture.get('protocolSha256')==protocol_sha
        and capture.get('roundId')==snapshot['roundId'],'scoped_capacity_native_capture_scope')
    generation=capture['payload']['generation'];held=HeldUEAllocation.from_dict(capture['payload']['held'])
    native=capture['payload']['native']
    need(-1<=capture['capturedAt']-seconds(native['observedAt'])<=15,'scoped_capacity_native_clock')
    view=typed_sampling_view(snapshot,generation,held,observed=native)
    need(view['typedCohortProof']['deviceCount']==100 and held.runtime_policy_digest==protocol['runtime']['runtimePolicySha256'],
         'scoped_capacity_native_runtime')
    fence=native.get('observationFence',{})
    need(fence.get('source')=='native-free-ran-ue-cohort-fence/v1' and fence.get('generation')==snapshot['generation']
        and fence.get('allocationDigest')==digest(generation['allocation'])
        and fence.get('openingCensusDigest')==fence.get('closingCensusDigest')
        and type(fence.get('openingCensusDigest')) is str and len(fence['openingCensusDigest'])==64
        and fence.get('deviceCount')==fence.get('sessionCount')==100,'scoped_capacity_native_fence')
    need(seconds(native['observedAt'])-15<=fence['openedAt']<=fence['closedAt']<=seconds(native['observedAt'])+1,
         'scoped_capacity_native_fence_clock')
    for row in session_rows(native):
        proof=row.get('proof',{})
        need(proof.get('source')=='native-free-ran-ue-single-session/v2'
            and all(proof.get(k)==row.get(k) for k in ('sessionKey','podUid','containerId','processEpoch','upfEpoch'))
            and bool(proof.get('smfBootEpoch')),'scoped_capacity_native_proof')
        catalog=protocol['runtime'].get('serviceCatalog',{}).get('web_browsing',{})
        need(catalog and all(row.get(key)==catalog.get(key) for key in ('service_type','region')),
             'scoped_capacity_native_service_classification')
    return view,held

def verify_manifest(manifest, read_artifact, protocol, *, protocol_sha, runtime, completed_reader, archived_reader=None, now=None):
    now=time.time() if now is None else now
    validate_protocol(protocol)
    need(type(manifest) is dict and set(manifest)=={'schemaVersion','protocolSha256','artifacts'}
        and manifest['schemaVersion']==MANIFEST and manifest['protocolSha256']==protocol_sha,'scoped_capacity_manifest_schema')
    need(digest(runtime)==protocol['runtimeFingerprint'],'scoped_capacity_runtime_drift')
    artifacts=manifest['artifacts']
    need(set(artifacts)=={'opening-native','closing-native','result','cleanup','rendered'},'scoped_capacity_receipts_incomplete')
    values={}
    for phase,descriptor in artifacts.items():
        need(type(descriptor) is dict and set(descriptor)=={'ref','sha256'},'scoped_capacity_descriptor')
        raw=read_artifact(descriptor['ref'])
        need(type(raw) is bytes and 0<len(raw)<=MAX_BYTES and hashlib.sha256(raw).hexdigest()==descriptor['sha256'],
             'scoped_capacity_receipt_digest')
        value=json.loads(raw)
        need(value.get('schemaVersion')==CAPTURE and value.get('phase')==phase
            and value.get('protocolSha256')==protocol_sha,'scoped_capacity_capture_schema')
        need(type(value.get('capturedAt')) in (int,float) and math.isfinite(value['capturedAt']),
             'scoped_capacity_capture_clock')
        values[phase]=value
    result=values['result']['payload']['snapshot'];cleanup=values['cleanup']['payload']['snapshot']
    need(result.get('schemaVersion')==V2 and scope_from_snapshot(result)==protocol['requestScope'],
         'scoped_capacity_result_scope')
    record=trial_record(result,protocol_sha)
    origin=window_origin(result,protocol_sha)
    need(result.get('stage')=='measuring_result'
        and protocol['declaredAt']<=record['declaredAt']<=origin['declaredAt']<=protocol['trialValidUntil'],
        'scoped_capacity_window_phase_or_clock')
    outcomes=[a for a in result['actions'] if a.get('kind')=='scoped_capacity_window_outcome']
    need(len(outcomes)==1 and outcomes[0].get('state')=='completed'
        and outcomes[0].get('windowOrigin')==origin,'scoped_capacity_window_not_completed_once')
    for phase in ('opening-native','closing-native','result'):
        need(values[phase].get('windowOrigin')==origin,'scoped_capacity_capture_window_mismatch')
    windows=[a for a in result['actions'] if a.get('kind')=='traffic_window' and a.get('stage')=='measuring_result']
    need(windows and all(a.get('windowOrigin')==origin for a in windows),
         'scoped_capacity_native_window_origin_missing')
    need(origin['declaredAt']<=values['opening-native']['capturedAt']<=values['closing-native']['capturedAt']
        <=values['result']['capturedAt'],'scoped_capacity_window_capture_order')
    need(seconds(values['opening-native']['payload']['native']['observedAt'])>=origin['declaredAt']-1,
         'scoped_capacity_opening_before_window_origin')
    for phase in ('opening-native','closing-native'):
        descriptor=artifacts[phase]
        need(any(a.get('kind')=='scoped_capacity_evidence' and a.get('accepted') is True
            and a.get('phase')==phase and a.get('ref')==descriptor['ref']
            and a.get('sha256')==descriptor['sha256'] for a in result['actions']),
            'scoped_capacity_native_capture_ledger_origin')
    need(record.get('requestScopeSha256')==digest(protocol['requestScope'])
        and record.get('runtimeFingerprint')==protocol['runtimeFingerprint']
        and protocol['declaredAt']<=record['declaredAt']<=protocol['trialValidUntil'],'scoped_capacity_trial_record')
    for phase in ('opening-native','closing-native'):
        validate_native_capture(values[phase],result,protocol,protocol_sha)
    opening=values['opening-native'];closing=values['closing-native']
    need(all(opening['payload']['generation'].get(k)==closing['payload']['generation'].get(k)
             for k in ('generation','desired','allocation','created'))
        and opening['payload']['held']==closing['payload']['held'],'scoped_capacity_nonce_generation_drift')
    live=session_rows(result)
    need(len(live)==len(result.get('devices',[]))==100,'scoped_capacity_denominator')
    rendered=values['rendered']['payload']['rendered']
    need(rendered.get('source')=='chrome-native-round-observation/v1'
        and rendered.get('roundId')==result['roundId'] and rendered.get('generation')==result['generation']
        and all(rendered.get(k)==100 for k in ('expectedDevices','measuredUplinkSessions','measuredDownlinkSessions','duplexSessions'))
        and rendered.get('missingSessionKeys')==[] and rendered.get('unknownSessionKeys')==[]
        and rendered.get('sessionKeys')==sorted(r['sessionKey'] for r in live),
        'scoped_capacity_rendered_denominator')
    seen=rendered.get('nativeSessionBindings',{})
    need(seen.get('available') is True and seen.get('schemaVersion')=='cityverse.native-session-bindings/v1'
        and seen.get('generation')==result['generation'] and seen.get('roundId')==result['roundId']
        and -1<=values['rendered']['capturedAt']-seconds(seen.get('observedAt'))<=15,
        'scoped_capacity_rendered_native_scope')
    seen_rows=seen.get('bindings',[])
    need(len(seen_rows)==100 and sorted([{k:r.get(k) for k in IDENTITY_FIELDS} for r in seen_rows],key=lambda r:r['sessionKey'])
        ==sorted([{k:r.get(k) for k in IDENTITY_FIELDS} for r in live],key=lambda r:r['sessionKey']),
        'scoped_capacity_rendered_native_identity')
    # Lossless worker text codec only. Source native rows stay untouched.
    view=deepcopy(result)
    owners={r['supi']:dict(r,ip=r['ueIp'],interface=r['tun']) for r in live}
    view['perUe']=[wire_binding(row) for row in owners.values()]
    for action in view['actions']:
        for report in action.get('reports',[]):
            owner=owners.get(report.get('binding',{}).get('supi'))
            need(owner is not None and report['binding']==dict(owner,generation=result['generation']),
                 'scoped_capacity_receiver_native_binding')
            report['binding']=wire_binding(report['binding'])
    stages=scoped_result_capacity_windows(view)
    _,aggregate,reports=stages['measuring_result']
    need(len(reports)==200,'scoped_capacity_direction_denominator')
    for job,report in reports.values():
        need(job.offered_kbps==100 and _flow(job,report,'measuring_result').passed,'scoped_capacity_delivery_failed')
    finished=aggregate['effectiveFinishedAt']
    need(seconds(closing['payload']['native']['observedAt'])>=finished,
         'scoped_capacity_closing_before_result_finished')
    need(opening['capturedAt']<=aggregate['effectiveStartedAt'] and finished<=closing['capturedAt']
        and 0<=values['result']['capturedAt']-finished<35,'scoped_capacity_result_clock')
    need(finished<=values['rendered']['capturedAt']<=values['cleanup']['capturedAt'],
         'scoped_capacity_rendered_clock')
    need(released(cleanup) and cleanup['roundId']==result['roundId'] and cleanup['generation']==result['generation']
        and cleanup['sequence']>result['sequence'],'scoped_capacity_cleanup')
    proof=cleanup['releaseProof']
    need(proof.get('scope')=='known-round-and-managed-runtime'
        and all(proof.get(k) is True for k in RELEASE)
        and 0<=values['cleanup']['capturedAt']-seconds(proof['observedAt'])<=15
        and seconds(proof['observedAt'])>=finished,'scoped_capacity_cleanup_clock')
    zero=values['cleanup']['payload']['generation']
    status=values['cleanup']['payload']['native']
    need(zero and not any(zero['desired'].values()) and not zero['allocation']
        and status.get('generation')==zero['generation'] and status.get('status')=='stopped' and not status.get('errors')
        and all(status.get(k)==0 for k in ('requested','allocated','registered','pduEstablished','trafficVerified'))
        and -1<=values['cleanup']['capturedAt']-seconds(status['observedAt'])<=15,'scoped_capacity_final_native_zero')
    durable=completed_reader(result['roundId'])
    need(durable==cleanup,'scoped_capacity_completed_ledger_mismatch')
    need(window_origin(durable,protocol_sha)==origin
        and [a for a in durable['actions'] if a.get('kind')=='scoped_capacity_window_outcome']==outcomes,
        'scoped_capacity_durable_window_outcome_changed')
    durable_actions={a['id']:a for a in durable['actions']}
    missing=[a for a in result['actions'] if a.get('id') not in durable_actions]
    need(all(a.get('kind') in {'traffic_window','traffic_aggregate'} for a in missing)
        and len(missing)<=22,'scoped_capacity_result_ledger_origin')
    archived=archived_reader(result['roundId'],[a['id'] for a in missing]) if missing and callable(archived_reader) else []
    need(all(durable_actions.get(a.get('id'))==a or a in archived for a in result['actions']),
         'scoped_capacity_result_ledger_origin')
    until=finished+protocol['grantMaximumAgeSeconds']
    need(values['cleanup']['capturedAt']<=now<until,'scoped_capacity_expired')
    return ScopedVerifiedTrafficCapacityGrant(protocol_sha,digest(manifest),protocol['runtimeFingerprint'],
        digest(protocol['requestScope']),until)


class ScopedWebCapacity:
    """Optional operator policy; capture is evidence only and never promotion."""
    def __init__(self, descriptor, *, runtime_reader, ledger, clock=time.time):
        need(type(descriptor) is dict and set(descriptor)<= {'schemaVersion','protocolFile','protocolSha256',
            'captureDirectory','manifestFile','manifestSha256'} and descriptor.get('schemaVersion')==CONFIG,
            'scoped_capacity_configuration')
        need({'protocolFile','protocolSha256','captureDirectory'}<=set(descriptor),'scoped_capacity_configuration')
        need(bool(descriptor.get('manifestFile'))==bool(descriptor.get('manifestSha256')),'scoped_capacity_manifest_pin')
        self.descriptor=deepcopy(descriptor);self.runtime_reader=runtime_reader;self.ledger=ledger;self.clock=clock
        self.root=Path(descriptor['captureDirectory']).resolve(strict=True)
        need(Path(descriptor['captureDirectory']).is_absolute() and not Path(descriptor['captureDirectory']).is_symlink()
            and self.root.is_dir(),'scoped_capacity_capture_directory')
        self.protocol=self.read_protocol()

    def read_protocol(self):
        return validate_protocol(read_pinned(self.descriptor['protocolFile'],self.descriptor['protocolSha256']))

    def read_grant(self):
        if not self.descriptor.get('manifestFile'):return None
        protocol=self.read_protocol()
        manifest=read_pinned(self.descriptor['manifestFile'],self.descriptor['manifestSha256'])
        def read(ref):
            path=(self.root/ref).resolve(strict=True)
            need(path.is_relative_to(self.root) and not Path(self.root/ref).is_symlink(),'scoped_capacity_receipt_path')
            return path.read_bytes()
        return verify_manifest(manifest,read,protocol,protocol_sha=self.descriptor['protocolSha256'],
            runtime=self.runtime_reader(),completed_reader=lambda rid:released_snapshot(self.ledger.path,rid),
            archived_reader=lambda rid,ids:archived_actions(self.ledger.path,rid,ids),now=self.clock())

    def prepare(self,body):
        protocol=self.read_protocol()
        try:match=request_scope(body)==protocol['requestScope']
        except (ValueError,KeyError,TypeError):return None,False
        if not match:return None,False
        runtime=self.runtime_reader()
        need(digest(runtime)==protocol['runtimeFingerprint'],'scoped_capacity_runtime_drift')
        grant=self.read_grant()
        if grant is not None:
            need(grant.matches(body,runtime,self.clock()),'scoped_capacity_request_outside_grant')
            return None,True
        validate_protocol(protocol,now=self.clock())
        return dict(protocolSha256=self.descriptor['protocolSha256'],runtimeFingerprint=digest(runtime),
            requestScopeSha256=digest(protocol['requestScope']),declaredAt=self.clock(),accepted=True),False

    def select_window(self,snapshot,phase,window_id):
        if phase!='measuring_result':return None
        trials=[a for a in snapshot.get('actions',[]) if a.get('kind')=='scoped_capacity_trial']
        if not trials:return None
        trial_record(snapshot,self.descriptor['protocolSha256'])
        if any(a.get('kind')=='scoped_capacity_window' for a in snapshot['actions']):return None
        need(not any(a.get('kind') in {'traffic_window','traffic_aggregate'} and a.get('stage')=='measuring_result'
            for a in snapshot['actions']),'scoped_capacity_window_declared_late')
        need(snapshot.get('stage')==phase and snapshot.get('schemaVersion')==V2,
             'scoped_capacity_window_phase')
        return dict(schemaVersion=WINDOW,protocolSha256=self.descriptor['protocolSha256'],
            roundId=snapshot['roundId'],generation=snapshot['generation'],cycle=1,phase=phase,
            windowId=window_id,originSequence=snapshot['sequence'],declaredAt=self.clock())

    def record(self,snapshot,phase,payload):
        records=[a for a in snapshot.get('actions',[]) if a.get('kind')=='scoped_capacity_trial']
        if not records:return None
        need(len(records)==1 and records[0]['protocolSha256']==self.descriptor['protocolSha256'],
             'scoped_capacity_capture_owner')
        need(phase in {'opening-native','closing-native','result','cleanup','failure'},'scoped_capacity_capture_phase')
        if not any(a.get('kind')=='scoped_capacity_window' for a in snapshot.get('actions',[])):return None
        origin=window_origin(snapshot,self.descriptor['protocolSha256'])
        if phase!='cleanup':
            if snapshot.get('_scopedCapacityWindowOrigin') is None:return None
            need(snapshot.get('stage')=='measuring_result'
                and snapshot['_scopedCapacityWindowOrigin']==origin,'scoped_capacity_capture_wrong_window')
        import uuid
        uuid.UUID(snapshot['roundId'])
        folder=self.root/snapshot['roundId'];folder.mkdir(mode=0o700,exist_ok=True)
        value=dict(schemaVersion=CAPTURE,phase=phase,protocolSha256=self.descriptor['protocolSha256'],
            roundId=snapshot['roundId'],capturedAt=self.clock(),windowOrigin=origin,payload=payload)
        existing=list(folder.glob(phase+'-*.json'))
        if existing:
            need(len(existing)==1,'scoped_capacity_duplicate_capture')
            prior=json.loads(existing[0].read_bytes())
            need(prior.get('windowOrigin')==origin and (phase=='cleanup' or prior.get('payload')==payload),
                'scoped_capacity_duplicate_capture')
            return dict(ref=str(existing[0].relative_to(self.root)),
                sha256=hashlib.sha256(existing[0].read_bytes()).hexdigest(),phase=phase)
        raw=encoded(value);need(len(raw)<=MAX_BYTES,'scoped_capacity_capture_size')
        name=phase+'-'+hashlib.sha256(raw).hexdigest()+'.json';path=folder/name
        if not path.exists():
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as out:out.write(raw)
        return dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest(),phase=phase)

    def capabilities(self):
        grant=self.read_grant()
        return dict(schemaVersion='cityverse.scoped-traffic-capabilities/v1',profileId='web_browsing',
            verifiedDevices=100 if grant else 0,scope=deepcopy(self.protocol['requestScope']),
            validUntil=grant.valid_until if grant else None,policyWritesAllowed=False,uePduReady=False,
            evidenceId=grant.manifest_sha256 if grant else None)

def configured_scoped_capacity(config,driver,ledger):
    descriptor=config.get('scoped_traffic_capacity')
    def read_runtime():
        from backend.onprem.intents.integration import load_configuration
        current=load_configuration()
        need(current.get('scoped_traffic_capacity')==descriptor,'scoped_capacity_configuration_changed')
        value=runtime_scope(driver)
        value['writeAuthorizationSha256']=digest({key:current.get(key) for key in
            ('targets','capacity_profiles','budgets','authority')})
        return value
    return None if descriptor is None else ScopedWebCapacity(descriptor,runtime_reader=read_runtime,ledger=ledger)
