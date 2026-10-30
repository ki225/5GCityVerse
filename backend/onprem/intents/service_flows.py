"""Execute bounded native receiver windows through current service-route bindings."""
import asyncio
from copy import deepcopy
import hashlib
import json

from .actuators import ActuationBlocked, Capability
from .service_routes import _json
from backend.onprem.runtime.current_sessions import session_rows


class ServiceFlowProvider:
    actions = ('start_service_flows', 'stop_service_flows')

    def __init__(self, resources, registry, snapshot_reader, *, loop_getter=None):
        self.resources, self.registry, self.snapshot_reader = resources, registry, snapshot_reader
        self.loop_getter = loop_getter
        with registry.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS service_flow_operations (id TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS service_flow_restorations (id TEXT PRIMARY KEY,value TEXT NOT NULL)')

    def capabilities(self):
        ready = (callable(self.snapshot_reader) and self.resources.service_routes is self.registry
            and callable(self.registry.native_reader) and callable(self.registry.session_reader))
        return tuple(Capability(action, 'configured' if ready else 'not_configured') for action in self.actions)

    def _key(self, request):
        return request.owner_id + ':' + request.operation_id + ':' + request.target_id + ':' + request.action

    def _scope(self, request):
        if set(request.parameters) != {'round_id', 'generation'} or not request.session_keys:
            raise ActuationBlocked('Exact round, generation and session flow scope required')
        snapshot = self.snapshot_reader()
        if snapshot.get('roundId') != request.parameters['round_id'] or snapshot.get('generation') != request.parameters['generation']:
            raise ActuationBlocked('Service flow round/generation changed')
        rows = session_rows(snapshot)
        from .domain import SessionKey
        def key(row):
            if row.get('sessionKey'):
                return row['sessionKey']
            # Match already commissioned route keys, never guess network scope.
            if not self.registry.has_session(snapshot, row):
                return None
            route = self.registry.for_session(snapshot, row)
            return route['session_key']
        selected = [r for r in rows if key(r) in request.session_keys]
        if len(selected) != len(request.session_keys):
            raise ActuationBlocked('Service flow session membership changed')
        for row in selected:
            route = self.registry.for_session(snapshot, row)
            if route['owner_id'] != request.owner_id:
                raise ActuationBlocked('Service flow route belongs to another intent')
        return snapshot, selected

    def inspect(self, request):
        with self.registry.db() as db:
            row = db.execute('SELECT value FROM service_flow_operations WHERE id=?', (self._key(request),)).fetchone()
        return {'source': 'native-service-flow-journal/v1', 'version': 'absent' if row is None else json.loads(row[0])['version'],
            'execution': None if row is None else json.loads(row[0])}

    def preflight(self, request, before):
        if before['version'] != request.expected_version:
            raise ActuationBlocked('Service flow execution version changed')
        self._scope(request)
        return {'version': before['version']}

    def mutate(self, request, before):
        self.preflight(request, before)
        snapshot, selected = self._scope(request)
        digest = hashlib.sha256(_json(request.to_dict()).encode()).hexdigest()
        with self.registry.db() as db:
            existing = db.execute('SELECT value FROM service_flow_operations WHERE id=?', (self._key(request),)).fetchone()
            if existing:
                value = json.loads(existing[0])
                if value['request_digest'] != digest:
                    raise ActuationBlocked('Service flow operation replay differs')
                if value['state'] == 'complete':
                    return value
                raise ActuationBlocked('Native service flow outcome requires readback; automatic replay forbidden')
            value = {'version': '1', 'request_digest': digest, 'state': 'dispatching', 'owner_id': request.owner_id,
                'operation_id': request.operation_id, 'request': request.to_dict()}
            db.execute('INSERT INTO service_flow_operations VALUES (?,?)', (self._key(request), _json(value)))

        async def run():
            jobs = await self.resources.traffic_jobs(snapshot)
            jobs = [j for j in jobs if (j.get('serviceRoute') or {}).get('session_key') in request.session_keys]
            if len(jobs) != 2 * len(selected) or any(not j.get('serviceRoute') for j in jobs):
                raise ActuationBlocked('Both directions must use exact commissioned application endpoints')
            # Sequential windows avoid rebinding an application's server port
            # while another one-shot native receiver still owns it.
            results = []
            for job in jobs:
                if self.snapshot_reader().get('generation') != snapshot['generation']:
                    raise ActuationBlocked('Service flow generation changed before dispatch')
                job['flowOperationId'] = request.operation_id
                window = await self.resources.sample(job)
                route = job['serviceRoute']
                if (window.get('source') != 'native-iperf3-receiver' or not window.get('flow_id')
                        or not window.get('client') or not window.get('server')):
                    raise ActuationBlocked('Native application receiver flow identity and raw reports required')
                from .evidence_contracts import PathEvidence
                forwarding = hashlib.sha256(_json(route).encode()).hexdigest()
                receiver = hashlib.sha256(_json({'client':window['client'],'server':window['server']}).encode()).hexdigest()
                path = PathEvidence(evidence_id=hashlib.sha256((forwarding+receiver+window['flow_id']).encode()).hexdigest(),
                    intent_id=request.owner_id, operation_id=request.operation_id, round_id=snapshot['roundId'],
                    generation=snapshot['generation'], session_key=route['session_key'], upf_target_id=route['upf_target_id'],
                    upf_epoch=route['upf_epoch'], pfcp_seid=str(route['pfcp_seid']), pdr_ids=tuple(route['pdr_ids']),
                    far_ids=tuple(route['far_ids']), application_instance_id=route['application_instance_id'],
                    application_pod_uid=route['application_pod_uid'], endpoint_revision=route['endpoint_revision'],
                    flow_id=window['flow_id'], receiver_id=window['receiverPodUid'],
                    native_evidence_refs=('forwarding:'+forwarding,'receiver:'+receiver),
                    window_started_at=window['startedAt'], observed_at=window['finishedAt'])
                window.update(serviceRoute=route,pathEvidence=path.to_dict(),flow_operation_id=request.operation_id)
                results.append(window)
            return results

        def prove_stopped_and_release():
            if not self.resources._traffic_absent(self.resources._pods()):
                raise ActuationBlocked('Native service flow processes are not independently absent')
            with self.registry.db() as db:
                leases = [(r[0], json.loads(r[1])) for r in db.execute('SELECT id,value FROM service_route_flows')]
            selected_leases = [(flow_id, route) for flow_id, route in leases if route['owner_id'] == request.owner_id
                and route['round_id'] == snapshot['roundId'] and route['generation'] == snapshot['generation']
                and route['session_key'] in request.session_keys
                and route.get('flow_operation_id',route['operation_id']) == request.operation_id]
            activity = getattr(self.registry, 'application_activity_reader', None)
            if selected_leases and not callable(activity):
                raise ActuationBlocked('Independent application flow activity reader unavailable')
            for target in {r['application_instance_id'] for _,r in selected_leases}:
                proof = activity(target)
                self.registry._fresh(proof)
                if proof.get('active_connections') != 0:
                    raise ActuationBlocked('Application has active or unknown native flow activity')
            for flow_id, route in selected_leases:
                self.registry.release_flow(flow_id, route)

        if request.action == 'start_service_flows':
            if self.loop_getter is not None:
                loop = self.loop_getter()
                if loop is None or loop.is_closed():
                    raise ActuationBlocked('Owning service flow event loop is unavailable')
                results = asyncio.run_coroutine_threadsafe(run(), loop).result(timeout=2*len(selected)*(self.resources.duration+35))
            else:
                results = asyncio.run(run())
            value.update(state='complete', version='2', windows=results, receiver_effect_verified=False)
        else:
            # Bounded windows stop through the existing worker cleanup protocol.
            # Unknown or still-live processes keep leases; never infer absence
            # from elapsed time or terminate other services to clear a fence.
            prove_stopped_and_release()
            value.update(state='complete', version='2', native_processes_absent=True)
        with self.registry.db() as db:
            db.execute('UPDATE service_flow_operations SET value=? WHERE id=?', (_json(value), self._key(request)))
        return value

    def observe(self, request, mutation):
        return self.inspect(request)

    def confirmed(self, request, observed):
        execution = observed.get('execution') or {}
        return execution.get('state') == 'complete' and execution.get('owner_id') == request.owner_id and execution.get('operation_id') == request.operation_id

    def verify_effect_window(self, receipt, effect):
        """Join evaluated receiver proofs to this operation's durable native windows."""
        from .evidence_contracts import PathEvidence
        try:
            request = receipt.request
            execution = receipt.installed['execution']
            if (request.action != 'start_service_flows' or execution['state'] != 'complete'
                    or execution['owner_id'] != request.owner_id or execution['operation_id'] != request.operation_id
                    or effect['owner_id'] != request.owner_id or effect['operation_id'] != request.operation_id
                    or not set(request.session_keys).issubset(effect['session_keys'])):
                return False
            start, end = effect['window_started_at'], effect['observed_at']
            import math
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for v in (start, end, receipt.started_at)) or not start < end:
                return False
            evaluated = {_json(PathEvidence.from_dict(p).to_dict()) for p in effect['receiver_path_evidence']}
            windows = execution['windows']
            if len(windows) != 2 * len(request.session_keys):
                return False
            scope = set()
            for window in windows:
                proof = PathEvidence.from_dict(window['pathEvidence'])
                if (proof.intent_id != request.owner_id or proof.operation_id != request.operation_id
                        or proof.round_id != request.parameters['round_id'] or proof.generation != request.parameters['generation']
                        or proof.session_key not in request.session_keys or proof.flow_id != window['flow_id']
                        or proof.window_started_at != window['startedAt'] or proof.observed_at != window['finishedAt']
                        or proof.window_started_at < receipt.started_at
                        or not start <= proof.window_started_at < proof.observed_at <= end
                        or _json(proof.to_dict()) not in evaluated or window['direction'] not in ('ul', 'dl')):
                    return False
                scope.add((proof.session_key, window['direction']))
            return scope == {(key, direction) for key in request.session_keys for direction in ('ul', 'dl')}
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def restore(self, request, before, installed):
        if not self.resources._traffic_absent(self.resources._pods()):
            raise ActuationBlocked('Native service flow compensation requires independently proved absence')
        snapshot, _ = self._scope(request)
        with self.registry.db() as db:
            leases = [(r[0], json.loads(r[1])) for r in db.execute('SELECT id,value FROM service_route_flows')]
        selected = [(flow_id, route) for flow_id, route in leases if route['owner_id']==request.owner_id
            and route['round_id']==snapshot['roundId'] and route['generation']==snapshot['generation']
            and route['session_key'] in request.session_keys
            and route.get('flow_operation_id',route['operation_id']) == request.operation_id]
        activity = getattr(self.registry,'application_activity_reader',None)
        for target in {route['application_instance_id'] for _,route in selected}:
            if not callable(activity):raise ActuationBlocked('Independent application activity reader unavailable')
            proof=activity(target);self.registry._fresh(proof)
            if proof.get('active_connections')!=0:
                raise ActuationBlocked('Native application flow compensation cannot prove absence')
        for flow_id,route in selected:self.registry.release_flow(flow_id,route)
        targets={route['application_instance_id'] for _,route in selected}
        targets.update(w['serviceRoute']['application_instance_id'] for w in (installed.get('execution') or {}).get('windows',()))
        # Even an empty lease set needs independent receiver quiescence proof.
        for row in session_rows(snapshot):
            if self.registry.has_session(snapshot,row):
                route=self.registry.for_session(snapshot,row)
                if route['session_key'] in request.session_keys:targets.add(route['application_instance_id'])
        for target in targets:
            if not callable(activity):raise ActuationBlocked('Independent application activity reader unavailable')
            proof=activity(target);self.registry._fresh(proof)
            if proof.get('active_connections')!=0:raise ActuationBlocked('Native application flow absence unknown')
        record={'digest':hashlib.sha256(_json({'request':request.to_dict(),'before':before,'installed':installed}).encode()).hexdigest(),
            'targets':sorted(targets),'owner_id':request.owner_id,'operation_id':request.operation_id}
        with self.registry.db() as db:
            prior=db.execute('SELECT value FROM service_flow_restorations WHERE id=?',(self._key(request),)).fetchone()
            if prior and json.loads(prior[0])!=record:
                raise ActuationBlocked('Service flow compensation replay differs')
            db.execute('INSERT OR IGNORE INTO service_flow_restorations VALUES (?,?)',(self._key(request),_json(record)))
        return {'restored': True, 'source': 'native-owned-service-flow-absence', 'receiver_effect_verified': False}

    def restoration_state(self, request, before, installed):
        """Independently read exact-flow absence after a recorded compensation."""
        try:
            snapshot=self.snapshot_reader()
            if snapshot.get('roundId')!=request.parameters['round_id'] or snapshot.get('generation')!=request.parameters['generation']:
                return {'restored':False}
            with self.registry.db() as db:
                row=db.execute('SELECT value FROM service_flow_restorations WHERE id=?',(self._key(request),)).fetchone()
            if not row:return {'restored':False}
            record=json.loads(row[0])
            digest=hashlib.sha256(_json({'request':request.to_dict(),'before':before,'installed':installed}).encode()).hexdigest()
            if record['digest']!=digest or record['owner_id']!=request.owner_id or record['operation_id']!=request.operation_id:
                return {'restored':False}
            if not self.resources._traffic_absent(self.resources._pods()):return {'restored':False}
            activity=getattr(self.registry,'application_activity_reader',None)
            for target in record['targets']:
                if not callable(activity):return {'restored':False}
                proof=activity(target);self.registry._fresh(proof)
                if proof.get('active_connections')!=0:return {'restored':False}
            with self.registry.db() as db:
                leases=[json.loads(r[0]) for r in db.execute('SELECT value FROM service_route_flows')]
            owned=[r for r in leases if r['owner_id']==request.owner_id
                and r['round_id']==request.parameters['round_id'] and r['generation']==request.parameters['generation']
                and r['session_key'] in request.session_keys
                and r.get('flow_operation_id',r['operation_id'])==request.operation_id]
            return {'restored':not owned,'source':'native-owned-service-flow-absence-read/v1','receiver_effect_verified':False}
        except (ActuationBlocked,KeyError,TypeError,ValueError):
            return {'restored':False}
