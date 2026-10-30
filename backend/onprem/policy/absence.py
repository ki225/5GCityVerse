"""GET-only, exact-Pod PCF absence for durable known round bindings.

No global SM inventory, no policy mutation, and no inferred default session ID.
The ledger reader and Kubernetes runner are trusted startup dependencies, not
HTTP input. Unknown allocation outcomes deliberately remain unavailable.
"""
from copy import deepcopy
import ipaddress
import json
import logging
import math
import re
import time

from backend.onprem.runtime.ue import seconds, timestamp
from backend.onprem.policy.recovery_scope import recovered_allocation_scope

logger = logging.getLogger(__name__)









def scope_for_round(snapshot, *, released=False, allocation_record=None, held_allocation=None):
    if not isinstance(snapshot, dict):
        raise ValueError('Missing durable round')
    rows, actions = snapshot.get('perUe'), snapshot.get('actions')
    if not isinstance(rows, list) or not isinstance(actions, list):
        raise ValueError('Missing durable ownership')
    if 'generation' not in snapshot or 'roundId' not in snapshot:
        raise ValueError('Missing durable scope identity')
    generation = snapshot['generation']
    if snapshot.get('schemaVersion')=='cityverse.current-state/v2':
        from backend.onprem.runtime.current_sessions import session_rows
        native_rows=session_rows(snapshot)
        proof=snapshot.get('connectivityEvidence')
        allocations=[a for a in actions if a.get('kind')=='ue_allocation']
        identity_matches = isinstance(proof, dict) and proof.get('identities') == snapshot.get('perSession')
        if isinstance(proof, dict) and not identity_matches:
            from backend.onprem.policy.device_scope import verify_spatial_device_scope
            identity_matches = verify_spatial_device_scope(snapshot, native_rows, allocation_record, held_allocation)
        if (not isinstance(proof,dict) or proof.get('source')!='native-ue-controller'
                or proof.get('roundId')!=snapshot.get('roundId') or proof.get('generation')!=generation
                or not identity_matches or not native_rows
                or len(allocations)!=1 or allocations[0].get('accepted') is not True
                or allocations[0].get('generation')!=generation):
            raise ValueError('Incomplete durable native device/session binding')
        return {'kind':'known_native_bindings','roundId':snapshot['roundId'],'generation':generation,
                'bindings':[{'supi':r['supi'],'sessionId':str(r['pduSessionId']),
                    'podUid':r['podUid'],'smContextRef':r['smContextRef']} for r in native_rows]}
    recovered = recovered_allocation_scope(snapshot, released=released)
    if recovered is not None:
        return recovered
    if any(key in snapshot for key in ("gen17PartialRelease", "gen25PartialRelease", "gen34PartialRecovery")) or (
            isinstance(snapshot.get("releaseProof"), dict) and (
                "operatorAbandon" in snapshot["releaseProof"] or
                str(snapshot["releaseProof"].get("schema", "")).startswith("cityverse.gen25-"))):
        from archive.legacy.policy.pcf_history import terminal_scope
        terminal = terminal_scope(snapshot, released=released)
        if terminal is not None:
            return terminal
    if generation is None and rows == []:
        # This is only a statement about this round, never global PCF absence.
        counts = snapshot.get('counts')
        total = snapshot.get('allocation', {}).get('total')
        if (type(total) is not int or not 0 <= total <= 50
                or not isinstance(counts, dict)
                or set(counts) != {'allocated','registered','pduEstablished','trafficVerified'}
                or any(v is not None and (type(v) is not int or v != 0) for v in counts.values())
                or snapshot.get('connectivityEvidence') is not None):
            raise ValueError('Possible prior native resources')
        allocations = [a for a in actions if isinstance(a, dict) and a.get('kind') == 'ue_allocation']
        reconciled = allocations[0] if len(allocations) == 1 else None
        try:
            requested_at = seconds(reconciled.get('requestedAt')) if reconciled else None
            reconciled_at = seconds(reconciled.get('reconciledAt')) if reconciled else None
        except (TypeError, ValueError, OverflowError):
            requested_at = reconciled_at = None
        unwritten = (reconciled is not None
            and total > 0
            and reconciled.get('requested') is True
            and reconciled.get('accepted') is False
            and reconciled.get('installed') is False
            and reconciled.get('effectVerified') is False
            and reconciled.get('generation') is None
            and reconciled.get('reconciliation') == 'native_write_not_observed'
            and type(requested_at) in (int, float) and math.isfinite(requested_at)
            and type(reconciled_at) in (int, float) and math.isfinite(reconciled_at)
            and requested_at <= reconciled_at)
        allowed = ({'zero_workload', 'execution_failure'} if not allocations
                   else {'ue_allocation', 'execution_failure'} if unwritten else set())
        cleanup = [a for a in actions if isinstance(a, dict) and a.get('kind')=='adaptive_qer_cleanup']
        if (allowed and snapshot.get('strategy')=='ai' and len(cleanup)==1
                and cleanup[0].get('cleanupVerified') is True
                and cleanup[0].get('reconciliationRequired') is False
                and cleanup[0].get('policyActionIds')==[]
                and cleanup[0].get('installed') is False
                and cleanup[0].get('effectVerified') is False):
            allowed.add('adaptive_qer_cleanup')
        if (not allowed or any(not isinstance(a, dict) or a.get('kind') not in allowed
                or (a.get('kind') == 'execution_failure' and a.get('requested') is not False)
                or (a.get('kind') == 'zero_workload' and a.get('requested') is not False)
                for a in actions)):
            raise ValueError('Possible native or policy side effects')
        return {'kind': 'not_created', 'roundId': snapshot.get('roundId'), 'generation':None, 'bindings': []}
    total = snapshot.get('allocation', {}).get('total')
    proof = snapshot.get('connectivityEvidence', {})
    allocation = snapshot.get('allocation', {})
    events = allocation.get('events')
    adaptive100 = (type(total) is int and 1 <= total <= 100 and snapshot.get('strategy') == 'ai'
                   and type(allocation.get('baselineUes')) is int
                   and allocation['baselineUes'] >= 0 and isinstance(events, dict)
                   and all(type(count) is int and count >= 0 for count in events.values())
                   and allocation['baselineUes'] + sum(events.values()) == total)
    if (type(generation) is not int or generation <= 0 or type(total) is not int
            or not (1 <= total <= 50 or adaptive100
                    or (total == 100 and _verified_historical_lineage(snapshot, released=released)))
            or len(rows) != total or not snapshot.get('roundId')
            or not isinstance(proof, dict) or proof.get('source') != 'native-ue-controller'
            or proof.get('roundId') != snapshot['roundId'] or type(proof.get('generation')) is not int
            or proof['generation'] != generation or proof.get('identities') != rows):
        raise ValueError('Incomplete durable native binding')
    allocations = [a for a in actions if a.get('kind') == 'ue_allocation']
    if (len(allocations) != 1 or allocations[0].get('accepted') is not True
            or type(allocations[0].get('generation')) is not int
            or allocations[0]['generation'] != generation):
        raise ValueError('Missing native allocation receipt')
    result, seen = [], set()
    for row in rows:
        supi, sid = row.get('supi'), row.get('sessionId')
        if (not isinstance(supi, str) or not re.fullmatch(r'imsi-[0-9]{5,15}', supi)
                or supi in seen or not isinstance(sid, str) or not re.fullmatch(r'[1-9][0-9]{0,2}', sid)
                or int(sid) > 255 or any(not isinstance(row.get(k), str) or not row[k]
                                       for k in ('podUid', 'smContextRef'))):
            raise ValueError('Missing or ambiguous actual PDU identity')
        seen.add(supi)
        result.append({'supi': supi, 'sessionId': sid, 'podUid': row['podUid'],
                       'smContextRef': row['smContextRef']})
    return {'kind': 'known_native_bindings', 'roundId': snapshot['roundId'],
            'generation': generation, 'bindings': result}


# Remote reads use a fixed path list, no shell, proxy environment or redirects.
# Every task has the same absolute budget; queued tasks cannot gain extra time.
READ_SCRIPT = '''import concurrent.futures,json,time,urllib.request,urllib.error,sys,signal
# Process-wide hard limit also terminates worker threads if a peer trickles
# data indefinitely. No child process is created by these HTTP reads.
signal.alarm(8)
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
deadline=time.monotonic()+7
def read(path):
 started=time.time(); remaining=deadline-time.monotonic()
 if remaining<=0: raise TimeoutError('expired')
 try:
  with opener.open(BASE+path,timeout=min(3,remaining)) as r: status,raw=r.status,r.read(65537)
 except urllib.error.HTTPError as e: status,raw=e.code,e.read(65537)
 if len(raw)>65536 or time.monotonic()>=deadline: raise ValueError('unbounded or expired')
 return {'path':path,'status':status,'body':json.loads(raw or b'null'),'observedAt':started}
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: rows=list(pool.map(read,PATHS))
print(json.dumps(rows,allow_nan=False))
'''


class PcfRoundAbsence:
    def __init__(self, driver, ledger_reader, *, release_reader=None,
                 generation_reader=None, held_reader=None, query_reader=None, clock=time.time, monotonic=time.monotonic):
        self.driver, self.ledger_reader = driver, ledger_reader
        self.release_reader = release_reader
        self.generation_reader = generation_reader
        self.held_reader = held_reader
        self.query_reader = query_reader
        self.clock, self.monotonic = clock, monotonic

    def _pre_established_scope(self, durable, released):
        """Query every valid PDU ID; never infer an unobserved session ID."""
        proof = durable.get('releaseProof', {})
        actions = durable.get('actions', [])
        total = durable.get('allocation', {}).get('total')
        allocations = [a for a in actions if a.get('kind') == 'ue_allocation']
        if (not released or not callable(self.generation_reader)
                or durable.get('stage') != 'completed' or durable.get('cleanupStatus') != 'verified'
                or durable.get('perUe') != [] or durable.get('fleetLease') is not None
                or durable.get('connectivityEvidence') is not None
                or proof.get('scope') != 'pre-established-round-resources-only'
                or any(proof.get(k) is not True for k in ('trafficStopped', 'policiesAbsent',
                       'rulesRestored', 'ueLeaseReleased', 'fleetPreserved'))
                or type(total) is not int or not 1 <= total <= 100
                or len(allocations) != 1 or allocations[0].get('accepted') is not True
                or allocations[0].get('reused') is not False
                or allocations[0].get('generation') != durable.get('generation')
                or any(a.get('kind') not in {'ue_allocation', 'execution_failure', 'adaptive_qer_cleanup'}
                       for a in actions)):
            raise ValueError('Missing pre-established allocation ownership')
        return self._allocation_scope(durable, released=released)

    def _allocation_scope(self, durable, *, released=False):
        from backend.onprem.runtime.ue import allocate
        from backend.onprem.scenarios.contracts import slice_counts
        if not callable(self.generation_reader):
            raise ValueError('Historical generation reader is unavailable')
        allocations = [a for a in durable['actions'] if a.get('kind') == 'ue_allocation']
        total = durable['allocation']['total']
        record = self.generation_reader(durable['generation'])
        allocation = durable['allocation']
        from backend.onprem.scenarios.contracts import EVENT_SLICES
        if set(allocation.get('events', {})) == set(EVENT_SLICES):
            counts = slice_counts(allocation)
        else:
            # Historical baseline-only contracts predate profile IDs. Do not
            # remap old event workloads or mutate their durable journals.
            counts = self._released_baseline_counts(durable, record, released=released)
        actual=record.get('allocation',[]) if record else []
        commissioned_devices=bool(actual) and all('device' in row for row in actual)
        allocation_matches=actual==allocate(counts)
        if durable.get('spatialDemand') and not commissioned_devices:
            from backend.onprem.runtime.spatial_ran import assign_allocation
            resolution = durable['spatialDemand'].get('resolution')
            if not isinstance(resolution, dict) or not resolution.get('spatialRevision'):
                raise ValueError('Frozen spatial allocation resolution is unavailable')
            body = dict(durable['allocation'], requestId=durable['requestId'], strategy=durable['strategy'])
            allocation_matches = actual == assign_allocation(allocate(counts), body, resolution)
        if commissioned_devices:
            from backend.onprem.runtime.device_sessions import DeviceAllocation
            from backend.onprem.runtime.ue_reservation import ReservedUE
            from backend.onprem.runtime.ue import BASE_IMSI
            devices=[DeviceAllocation.from_dict(row['device']) for row in actual]
            allocation_matches=(len({d.device_id for d in devices})==len(actual)
                and len({d.supi for d in devices})==len(actual)
                and {name:sum(d.access_slice==name for d in devices) for name in counts}==counts)
            for d,row in zip(devices,actual):
                sessions=[ReservedUE(**s) for s in row.get('reservedSessions',[])]
                offset=int(d.supi.removeprefix('imsi-'))-BASE_IMSI
                allocation_matches=allocation_matches and (0<=offset<1000 and offset//250==d.credential_group
                    and (d.supi,d.access_slice,d.credential_group)==(row['supi'],row['slice'],row['credentialGroup'])
                    and len(sessions)==len(d.sessions)
                    and {s.session_id for s in sessions}=={s.session_id for s in d.sessions}
                    and len({s.pdu_session_id for s in sessions})==len(sessions)
                    and all((s.supi,s.slice,s.credential_group)==(d.supi,d.access_slice,d.credential_group) for s in sessions))
        if (not record or record.get('generation') != durable['generation']
                or record.get('desired') != counts or allocations[0].get('counts') != counts
                or not allocation_matches or sum(counts.values()) != total):
            raise ValueError('Generation allocation changed')
        return {'kind': 'allocated_supi_pdu_inventory', 'roundId': durable['roundId'],
                'generation': durable['generation'], 'bindings': [
                    {'supi': row['supi'], 'sessionId': str(sid)}
                    for row in record['allocation'] for sid in range(1, 16)]}

    def _released_baseline_counts(self, durable, record, *, released):
        from backend.onprem.runtime.current_sessions import V2, session_rows
        from backend.onprem.runtime.device_sessions import DeviceAllocation
        from backend.onprem.runtime.ue_reservation import HeldUEAllocation, ReservedUE
        allocation = durable.get('allocation', {})
        events, total = allocation.get('events'), allocation.get('total')
        proof = durable.get('connectivityEvidence') or {}
        if (released is not True or durable.get('stage') != 'completed'
                or durable.get('cleanupStatus') != 'verified' or durable.get('schemaVersion') != V2
                or not isinstance(events, dict)
                or set(events) != {'concert','typhoon','accident','medical','iot_surge'}
                or any(type(v) is not int or v != 0 for v in events.values())
                or type(total) is not int or not 1 <= total <= 100
                or type(allocation.get('baselineUes')) is not int or allocation['baselineUes'] != total
                or proof.get('identities') != durable.get('perSession')
                or not callable(self.held_reader) or not isinstance(record, dict)):
            raise ValueError('Unsupported historical allocation contract')
        held = self.held_reader(durable['roundId'])
        counts = {'embb':total,'urllc':0,'mmtc':0,'v2x':0}
        rows = record.get('allocation', [])
        if (type(held) is not HeldUEAllocation or not held.devices
                or held.request_id != durable.get('requestId') or dict(held.counts) != counts
                or not held.baseline_generation < record.get('generation', -1)
                or len(rows) != total or len(held.devices) != total):
            raise ValueError('Historical baseline reservation is unavailable')
        expected = {d.device_id:d for d in held.devices}
        reserved = {(r.supi,r.session_id):r for r in held.allocation}
        actual_devices = [DeviceAllocation.from_dict(row['device']) for row in rows]
        if (len(rows) != total or len(expected) != total
                or any(expected.get(d.device_id) != d for d in actual_devices)
                or {(r.supi,r.session_id):r for row in rows
                    for r in (ReservedUE(**s) for s in row['reservedSessions'])} != reserved):
            raise ValueError('Historical baseline reservation changed')
        sessions = session_rows(durable)
        if (len(sessions) != len(reserved) or len(sessions) != total
                or {(r['deviceId'],r['supi'],r['pduSessionId']) for r in sessions}
                    != {(d.device_id,d.supi,r.pdu_session_id) for d in held.devices
                        for r in held.allocation if r.supi == d.supi}):
            raise ValueError('Historical baseline native membership changed')
        return counts

    def _identity(self, pod, name, container, app):
        meta, status = pod['metadata'], pod['status']
        labels = meta.get('labels', {})
        if (meta['name'] != name or meta['namespace'] != self.driver.namespace
                or labels.get('app.kubernetes.io/name') != app
                or (container == 'pcf' and labels.get('nf') != 'pcf')
                or meta.get('deletionTimestamp') or status.get('phase') != 'Running'):
            raise ValueError('Untrusted Pod')
        rows = [c for c in status['containerStatuses'] if c.get('name') == container]
        if len(rows) != 1 or rows[0].get('ready') is not True:
            raise ValueError('Not ready')
        c = rows[0]
        values = {'pod':name, 'uid':meta['uid'], 'ip':str(ipaddress.IPv4Address(status['podIP'])),
                  'containerID':c['containerID'], 'imageID':c['imageID'],
                  'restartCount':c['restartCount'], 'startedAt':c['state']['running']['startedAt']}
        if (type(values['restartCount']) is not int or values['restartCount'] < 0
                or any(not isinstance(values[k], str) or not values[k] for k in
                       ('uid', 'containerID', 'imageID', 'startedAt'))
                or not re.search(r'@sha256:[0-9a-f]{64}$', values['imageID'])
                or seconds(values['startedAt']) > self.clock()+1):
            raise ValueError('Incomplete incarnation')
        return values

    def __call__(self, snapshot=None):
        unavailable = {'source':'native-pcf-known-round', 'available':False,
                       'knownRoundAbsent':False, 'globalSmInventoryComplete':False,
                       'reason':'pcf_scope_or_observation_unavailable'}
        phase, code, return_code = 'scope', 'validation', None
        began = self.monotonic()
        try:
            durable = self.ledger_reader() if snapshot is None else snapshot
            released = (callable(self.release_reader)
                        and durable.get('stage') == 'completed'
                        and self.release_reader(durable) is True)
            pre_established = ((durable.get('releaseProof') or {}).get('scope')
                               == 'pre-established-round-resources-only')
            allocations=[a for a in durable.get('actions',[]) if a.get('kind')=='ue_allocation']
            device_pre_established=((getattr(self.driver,'device_profile',None) is not None
                or bool(durable.get('spatialDemand')))
                and durable.get('perUe')==[] and durable.get('connectivityEvidence') is None
                and len(allocations)==1 and allocations[0].get('accepted') is True
                and allocations[0].get('reused') is False and allocations[0].get('generation')==durable.get('generation')
                and durable.get('strategy')=='none'
                and all(a.get('kind') in {'ue_allocation','execution_failure','ue_stop'} for a in durable.get('actions',[])))
            legacy_device_proof = (durable.get('schemaVersion') == 'cityverse.current-state/v2'
                and (durable.get('connectivityEvidence') or {}).get('identities') != durable.get('perSession'))
            scope = (self._pre_established_scope(durable, released) if pre_established else
                     self._allocation_scope(durable, released=released) if device_pre_established else
                     scope_for_round(durable, released=released,
                        allocation_record=self.generation_reader(durable['generation'])
                            if legacy_device_proof and callable(self.generation_reader) else None,
                        held_allocation=self.held_reader(durable['roundId'])
                            if legacy_device_proof and callable(self.held_reader) else None))
            if durable.get('schemaVersion')=='cityverse.current-state/v2' and scope['kind']=='known_native_bindings':
                # Durable native keys must still belong to the exact server
                # commissioned device/PDU allocation, not merely share a SUPI.
                self._allocation_scope(durable, released=released)
                from backend.onprem.runtime.device_sessions import DeviceAllocation
                from backend.onprem.runtime.ue_reservation import ReservedUE
                record=self.generation_reader(durable['generation'])
                expected=set()
                for row in record['allocation']:
                    device=DeviceAllocation.from_dict(row['device'])
                    expected.update((device.supi,session.pdu_session_id)
                        for session in (ReservedUE(**s) for s in row['reservedSessions']))
                if {(r['supi'],int(r['sessionId'])) for r in scope['bindings']}!=expected:
                    raise ValueError('Native PDU binding differs from commissioned reservation')
            if (not pre_established and scope['kind'] == 'allocated_supi_pdu_inventory'
                    and scope != self._allocation_scope(durable, released=released)):
                raise ValueError('Recovered allocation scope changed')
            if scope['kind'] == 'not_created':
                return {**unavailable, 'available':True, 'knownRoundAbsent':True, 'reason':None,
                        'scope':scope, 'observedAt':timestamp(self.clock()),
                        'amQueries':0, 'knownSmQueries':0, 'nativeAbsenceObserved':False}
            if scope['kind'] == 'verified_exceptional_release':
                return {**unavailable, 'available':True, 'knownRoundAbsent':True, 'reason':None,
                        'scope':scope, 'observedAt':timestamp(self.clock()),
                        'amQueries':0, 'knownSmQueries':0, 'nativeAbsenceObserved':False,
                        'terminalEvidenceAccepted':True}
            deadline = self.monotonic()+10
            def run(args):
                nonlocal code, return_code
                remaining = deadline-self.monotonic()
                if remaining <= 0:
                    code = 'deadline'
                    raise ValueError('Deadline expired')
                command = ['kubectl', '--context', self.driver.context, '--namespace', self.driver.namespace, *args]
                result = self.driver.runner(command, text=True, capture_output=True,
                                            timeout=remaining, check=False)
                return_code = result.returncode
                if (result.returncode != 0 or self.monotonic() >= deadline
                        or len(result.stdout.encode()) > 8*1024*1024):
                    code = ('command_nonzero' if result.returncode else
                            'output_bound' if len(result.stdout.encode()) > 8*1024*1024 else 'deadline')
                    raise ValueError('Read unavailable')
                code = 'decode_json'
                return json.loads(result.stdout)
            phase = 'list_pods'
            metadata_reader = getattr(self.driver, '_metadata_pod_reader', None)
            pods = (metadata_reader.pods(deadline=deadline) if metadata_reader is not None
                    else run(['get','pods','-o','json']))['items']
            phase, code = 'owner_identity', 'validation'
            owners = {}
            for container, app in (('pcf','free5gc-pcf'), ('callback','cityverse-af-callback')):
                matches = [p for p in pods if p.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/name') == app]
                if len(matches) != 1:
                    raise ValueError('Singleton owner required')
                owners[container] = self._identity(matches[0], matches[0]['metadata']['name'], container, app)
            paths, am_supis = [], set()
            for row in scope['bindings']:
                if row['supi'] not in am_supis:
                    paths.append('/npcf-oam/v1/am-policy/'+row['supi'])
                    am_supis.add(row['supi'])
                paths.append('/npcf-smpolicycontrol/v1/sm-policies/'+row['supi']+'-'+row['sessionId'])
            started = self.clock()
            script = 'BASE='+repr('http://'+owners['pcf']['ip']+':8080')+'\nPATHS='+repr(paths)+'\n'+READ_SCRIPT
            phase, code = 'remote_query', 'transport'
            rows = (self.query_reader(owners['pcf'], paths, deadline=deadline)
                    if self.query_reader is not None else
                    run(['exec',owners['callback']['pod'],'-c','callback','--','python','-c',script]))
            if self.monotonic() >= deadline:
                raise TimeoutError('Read expired')
            phase, code = 'recheck_pods', 'transport'
            after_pods = (metadata_reader.pods(deadline=deadline) if metadata_reader is not None
                          else run(['get','pods','-o','json']))['items']
            phase, code = 'recheck_owner_identity', 'validation'
            for container, app in (('pcf','free5gc-pcf'), ('callback','cityverse-af-callback')):
                matches = [p for p in after_pods if p.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/name') == app]
                if len(matches) != 1 or self._identity(matches[0], owners[container]['pod'],container,app) != owners[container]:
                    raise ValueError('Owner changed')
            phase = 'coverage'
            if not isinstance(rows,list) or len(rows) != len(paths) or [r.get('path') for r in rows] != paths:
                raise ValueError('Incomplete response coverage')
            observed = [started]
            absent = True
            phase = 'endpoint_evidence'
            for row in rows:
                stamp = row.get('observedAt')
                if (type(stamp) not in (int,float) or not math.isfinite(stamp)
                        or not -1 <= self.clock()-stamp <= 15 or stamp < started-1
                        or type(row.get('status')) is not int):
                    raise ValueError('Invalid source time or status')
                observed.append(stamp)
                is_am = row['path'].startswith('/npcf-oam/')
                missing = row['status']==404 and isinstance(row.get('body'),dict) and row['body'].get('cause')=='CONTEXT_NOT_FOUND'
                if is_am and row['status']==200 and isinstance(row.get('body'),list):
                    absent = absent and row['body']==[]
                elif missing:
                    pass
                elif not is_am and row['status']==200 and isinstance(row.get('body'),dict):
                    absent = False
                else:
                    raise ValueError('Unknown endpoint response')
            return {**unavailable, 'available':True, 'knownRoundAbsent':absent, 'reason':None if absent else 'pcf_policies_remain',
                    'scope':scope, 'observedAt':timestamp(min(observed)), 'nativeAbsenceObserved':True,
                    'amQueries':len(am_supis), 'knownSmQueries':len(scope['bindings']),
                    'pcfIncarnation':owners['pcf'], 'callbackIncarnation':owners['callback'],
                    'observations':deepcopy(rows)}
        except Exception as exc:
            # Never emit exception text, HTTP bodies, SUPIs, scripts or tokens.
            logger.warning('PCF absence read failed phase=%s reasonCode=%s errorType=%s '
                           'elapsedSeconds=%.3f returnCode=%s',
                           phase, code if code in {'validation','deadline','command_nonzero',
                           'output_bound','decode_json','transport'} else 'validation',
                           type(exc).__name__, self.monotonic()-began, return_code)
            return unavailable


def _verified_historical_lineage(snapshot, *, released=False):
    from archive.legacy.policy.pcf_history import _verified_gen32_100_lineage
    return _verified_gen32_100_lineage(snapshot, released=released)
