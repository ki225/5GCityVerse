"""Durable exact-session application routes. Configuration is not path evidence."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .actuators import ActuationBlocked, Capability


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ActuationBlocked('Bounded route identity required')
    return value


@dataclass(frozen=True)
class ServiceRouteBinding:
    binding_id: str
    intent_id: str
    round_id: str
    generation: int
    service_type: str
    demand_group_id: str
    session_key: str
    upf_target_id: str
    upf_epoch: str
    application_instance_id: str
    application_pod_uid: str
    endpoint_id: str
    endpoint_revision: str
    address: str
    port: int
    protocol: str
    receiver_pod: str
    receiver_peer: str
    receiver_address: str
    owner_id: str
    operation_id: str
    version: str
    accepting: bool = True

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name not in {'generation', 'port', 'accepting'}:
                _text(value)
        from .domain import SessionKey
        try:
            if SessionKey(*json.loads(self.session_key)).token != self.session_key:
                raise ValueError()
            ipaddress.IPv4Address(self.address)
            ipaddress.IPv4Address(self.receiver_peer)
            ipaddress.IPv4Address(self.receiver_address)
        except (ValueError, TypeError):
            raise ActuationBlocked('Exact session and IPv4 receiver route required') from None
        if (type(self.generation) is not int or self.generation < 1
                or type(self.port) is not int or not 1 <= self.port <= 65535
                or self.protocol != 'udp' or type(self.accepting) is not bool
                or re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', self.receiver_pod) is None):
            raise ActuationBlocked('Invalid route generation, port or protocol')

    def to_dict(self):
        return asdict(self)


class ServiceRouteRegistry:
    """Native readers establish identity; SQLite fences routes and active flow leases."""
    def __init__(self, path, *, native_reader=None, session_reader=None, clock=time.time, maximum_age=15):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.native_reader, self.session_reader = native_reader, session_reader
        self.clock, self.maximum_age = clock, maximum_age
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS service_route_bindings
                (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_route_apps
                (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_route_operations
                (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_route_flows
                (id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, value TEXT NOT NULL);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _fresh(self, value):
        at = value.get('observed_at')
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= self.clock() - at <= self.maximum_age:
            raise ActuationBlocked('Fresh native route evidence required')

    def endpoint(self, target):
        if not callable(self.native_reader):
            raise ActuationBlocked('Native application endpoint reader is not configured')
        value = self.native_reader(target)
        self._fresh(value)
        if (value.get('target_id') != target or value.get('ready') is not True
                or any(not value.get(k) for k in ('pod_uid', 'endpoint_id', 'endpoint_revision', 'address', 'receiver_pod'))):
            raise ActuationBlocked('Independent ready application endpoint required')
        return value

    def session(self, key, endpoint=None):
        if not callable(self.session_reader):
            raise ActuationBlocked('Independent exact-session path reader is not configured')
        route_read = getattr(self.session_reader, 'for_endpoint', None)
        value = route_read(key, endpoint) if endpoint is not None and callable(route_read) else self.session_reader(key)
        self._fresh(value)
        if (value.get('session_key') != key or value.get('pdr_far_verified') is not True
                or any(not value.get(k) for k in ('upf_target_id', 'upf_epoch', 'pfcp_seid', 'path_revision', 'receiver_peer'))):
            raise ActuationBlocked('Installed exact-session PDR/FAR evidence required')
        return value

    def _app(self, db, target, pod_uid):
        row = db.execute('SELECT value FROM service_route_apps WHERE id=?', (target,)).fetchone()
        value = json.loads(row[0]) if row else {'version': '0', 'accepting': True, 'pod_uid': pod_uid,
            'owner_id': None, 'operation_id': None}
        if value['pod_uid'] != pod_uid:
            if self._references(db, target):
                raise ActuationBlocked('Application Pod incarnation changed with route references')
            value = {'version': str(int(value['version']) + 1), 'accepting': True, 'pod_uid': pod_uid, 'owner_id': None, 'operation_id': None}
            db.execute('INSERT OR REPLACE INTO service_route_apps VALUES (?,?)', (target, _json(value)))
        return value

    def _references(self, db, target):
        bindings = [json.loads(row[0]) for row in db.execute('SELECT value FROM service_route_bindings')]
        routes = sum(b['application_instance_id'] == target and b['accepting'] for b in bindings)
        flows = sum(json.loads(row[0])['application_instance_id'] == target for row in db.execute('SELECT value FROM service_route_flows'))
        return routes + flows

    def inspect_application(self, target_id):
        if not callable(self.native_reader):
            raise ActuationBlocked('Native application endpoint reader is not configured')
        endpoint = self.native_reader(target_id)
        self._fresh(endpoint)
        if endpoint.get('target_id') != target_id:
            raise ActuationBlocked('Native application target differs')
        with self.db() as db:
            value = self._app(db, target_id, endpoint.get('pod_uid'))
            return {**value, 'references': self._references(db, target_id), 'observed_at': endpoint['observed_at']}

    def begin_drain_application(self, target_id, owner_id, operation_id, expected_version):
        return self.set_accepting(target_id, False, owner_id, operation_id, expected_version)

    def set_accepting(self, target_id, accepting, owner_id, operation_id, expected_version):
        if type(accepting) is not bool:
            raise ActuationBlocked('Explicit endpoint admission state required')
        if accepting:
            endpoint = self.endpoint(target_id)
        else:
            endpoint = self.native_reader(target_id)
            self._fresh(endpoint)
            if endpoint.get('target_id') != target_id:
                raise ActuationBlocked('Native drain application identity differs')
        digest = hashlib.sha256(_json([target_id, accepting, owner_id, operation_id, expected_version]).encode()).hexdigest()
        operation_key = 'application:' + target_id + ':' + operation_id + ':' + str(accepting) + ':' + expected_version
        with self.db() as db:
            prior = db.execute('SELECT digest,result FROM service_route_operations WHERE id=?', (operation_key,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ActuationBlocked('Application drain operation replay differs')
                return json.loads(prior[1])
            value = self._app(db, target_id, endpoint.get('pod_uid'))
            if value['version'] != expected_version or value.get('owner_id') not in (None, owner_id):
                raise ActuationBlocked('Application endpoint fence owner/version changed')
            if accepting and value.get('operation_id') not in (None, operation_id):
                raise ActuationBlocked('Only the drain operation may reopen endpoint admission')
            value.update(accepting=accepting, owner_id=owner_id, operation_id=operation_id,
                version=str(int(value['version']) + 1))
            db.execute('INSERT OR REPLACE INTO service_route_apps VALUES (?,?)', (target_id, _json(value)))
            result = {**value, 'references': self._references(db, target_id), 'observed_at': endpoint['observed_at']}
            db.execute('INSERT INTO service_route_operations VALUES (?,?,?)', (operation_key, digest, _json(result)))
            return result

    def read(self, binding_id):
        with self.db() as db:
            row = db.execute('SELECT value FROM service_route_bindings WHERE id=?', (binding_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def bind(self, request):
        p = request.parameters
        if len(request.session_keys) != 1:
            raise ActuationBlocked('One exact existing session is required for an application route')
        endpoint = self.endpoint(p['application_instance_id'])
        path = self.session(request.session_keys[0], endpoint['address'])
        if endpoint.get('owner_id') != request.owner_id or path['upf_target_id'] != p['upf_target_id'] or path['upf_epoch'] != p['upf_epoch']:
            raise ActuationBlocked('Application/session ownership or UPF path differs')
        operation_key = 'binding:' + request.target_id + ':' + request.operation_id
        digest = hashlib.sha256(_json(request.to_dict()).encode()).hexdigest()
        with self.db() as db:
            replay = db.execute('SELECT digest,result FROM service_route_operations WHERE id=?', (operation_key,)).fetchone()
            if replay:
                if replay[0] != digest:
                    raise ActuationBlocked('Route replay differs from durable operation')
                return json.loads(replay[1])
            app = self._app(db, p['application_instance_id'], endpoint['pod_uid'])
            if not app['accepting'] or app['version'] != p['expected_app_version']:
                raise ActuationBlocked('Application endpoint is draining or version changed')
            old = db.execute('SELECT value FROM service_route_bindings WHERE id=?', (request.target_id,)).fetchone()
            old = None if old is None else json.loads(old[0])
            if request.expected_version != (old['version'] if old else 'absent') or old and old['owner_id'] != request.owner_id:
                raise ActuationBlocked('Route owner/version changed')
            value = ServiceRouteBinding(binding_id=request.target_id, intent_id=request.owner_id,
                round_id=p['round_id'], generation=p['generation'], service_type=p['service_type'],
                demand_group_id=p['demand_group_id'], session_key=request.session_keys[0],
                upf_target_id=p['upf_target_id'], upf_epoch=p['upf_epoch'], application_instance_id=p['application_instance_id'],
                application_pod_uid=endpoint['pod_uid'], endpoint_id=endpoint['endpoint_id'], endpoint_revision=endpoint['endpoint_revision'],
                address=endpoint['address'], port=endpoint['port'], protocol=endpoint['protocol'], receiver_pod=endpoint['receiver_pod'],
                receiver_peer=path['receiver_peer'], receiver_address=endpoint['receiver_address'], owner_id=request.owner_id, operation_id=request.operation_id,
                version=str(int(old['version']) + 1) if old else '1').to_dict()
            # A single service/session gets one current route. Existing flow leases
            # retain their old immutable endpoint when a route is replaced.
            conflicts = [json.loads(r[0]) for r in db.execute('SELECT value FROM service_route_bindings WHERE id!=?', (request.target_id,))]
            if any(b['accepting'] and (b['round_id'], b['generation'], b['session_key'], b['service_type']) ==
                   (value['round_id'], value['generation'], value['session_key'], value['service_type']) for b in conflicts):
                raise ActuationBlocked('Ambiguous active application route')
            db.execute('INSERT OR REPLACE INTO service_route_bindings VALUES (?,?)', (request.target_id, _json(value)))
            db.execute('INSERT INTO service_route_operations VALUES (?,?,?)', (operation_key, digest, _json(value)))
            return value

    def drain(self, binding_id, owner_id, operation_id, expected_version):
        with self.db() as db:
            row = db.execute('SELECT value FROM service_route_bindings WHERE id=?', (binding_id,)).fetchone()
            if not row:
                raise ActuationBlocked('Owned application route is missing')
            value = json.loads(row[0])
            if value['owner_id'] != owner_id or value['version'] != expected_version:
                raise ActuationBlocked('Route drain owner/version changed')
            value.update(accepting=False, operation_id=operation_id, version=str(int(value['version']) + 1))
            db.execute('UPDATE service_route_bindings SET value=? WHERE id=?', (_json(value), binding_id))
            return value

    def restore_binding(self, request, before):
        """Restore a frozen native baseline only after exact owned flows drain."""
        current=self.read(request.target_id)
        if not current or current['owner_id']!=request.owner_id:
            raise ActuationBlocked('Endpoint compensation ownership changed')
        baseline=before.get('binding')
        if baseline is not None:
            endpoint=self.endpoint(baseline['application_instance_id'])
            path=self.session(baseline['session_key'],endpoint['address'])
            prior_path=before.get('native_path')
            if (not prior_path or endpoint.get('owner_id')!=request.owner_id or path['pfcp_seid']!=prior_path['pfcp_seid']
                    or path['upf_target_id']!=baseline['upf_target_id'] or path['upf_epoch']!=baseline['upf_epoch']
                    or any(endpoint.get(k)!=baseline.get(v) for k,v in (
                        ('pod_uid','application_pod_uid'),('endpoint_id','endpoint_id'),('endpoint_revision','endpoint_revision'),
                        ('address','address'),('receiver_address','receiver_address'),('receiver_pod','receiver_pod'),('port','port'),('protocol','protocol')))):
                raise ActuationBlocked('Original native application/session baseline changed')
        activity=getattr(self,'application_activity_reader',None)
        if not callable(activity):raise ActuationBlocked('Independent application flow absence reader unavailable')
        proof=activity(current['application_instance_id']);self._fresh(proof)
        if proof.get('active_connections')!=0:
            raise ActuationBlocked('Endpoint compensation requires native application flow absence')
        key='restore:'+request.target_id+':'+request.operation_id
        digest=hashlib.sha256(_json(before).encode()).hexdigest()
        with self.db() as db:
            replay=db.execute('SELECT digest,result FROM service_route_operations WHERE id=?',(key,)).fetchone()
            if replay:
                if replay[0]!=digest:raise ActuationBlocked('Endpoint restoration replay differs')
                return json.loads(replay[1])
            actual=db.execute('SELECT value FROM service_route_bindings WHERE id=?',(request.target_id,)).fetchone()
            actual=json.loads(actual[0]) if actual else None
            if (actual!=current or actual['operation_id']!=request.operation_id):
                raise ActuationBlocked('Endpoint compensation owner/CAS changed')
            if db.execute('SELECT 1 FROM service_route_flows WHERE binding_id=? LIMIT 1',(request.target_id,)).fetchone():
                raise ActuationBlocked('Endpoint compensation still has active or uncertain flow references')
            value=dict(baseline if baseline is not None else current)
            value.update(version=str(int(current['version'])+1),operation_id=request.operation_id+':restore',
                accepting=baseline['accepting'] if baseline is not None else False)
            db.execute('UPDATE service_route_bindings SET value=? WHERE id=?',(_json(value),request.target_id))
            db.execute('INSERT INTO service_route_operations VALUES(?,?,?)',(key,digest,_json(value)))
        return value

    def for_session(self, snapshot, session):
        key = session.get('sessionKey')
        with self.db() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT value FROM service_route_bindings')]
        matches = [b for b in rows if b['accepting'] and b['round_id'] == snapshot['roundId']
            and b['generation'] == snapshot['generation'] and (b['session_key'] == key if key else
                json.loads(b['session_key'])[1:] == [session['supi'], int(session['sessionId']), session['smContextRef']])]
        if len(matches) != 1:
            raise ActuationBlocked('Exactly one current application route is required')
        route = matches[0]
        key = route['session_key']
        endpoint = self.endpoint(route['application_instance_id'])
        path = self.session(key, endpoint['address'])
        if (endpoint.get('owner_id') != route['owner_id'] or endpoint['pod_uid'] != route['application_pod_uid'] or endpoint['endpoint_revision'] != route['endpoint_revision']
                or endpoint['address'] != route['address'] or path['receiver_peer'] != route['receiver_peer']
                or path['upf_target_id'] != route['upf_target_id'] or path['upf_epoch'] != route['upf_epoch']):
            raise ActuationBlocked('Native application/session route changed')
        return {**route, 'path_revision': path['path_revision'], 'pfcp_seid': path['pfcp_seid'],
            'observed_at': min(endpoint['observed_at'], path['observed_at']),
            'native_path_verified': True, 'path_evidence_source': path.get('source', 'native-exact-session-pdr-far'),
            'receiver_effect_verified': False, 'pdr_ids': path.get('pdr_ids', ()), 'far_ids': path.get('far_ids', ())}

    def observe_bindings(self, snapshot, sessions):
        bindings = [self.for_session(snapshot, session) for session in sessions]
        if not bindings:
            raise ActuationBlocked('No service routes to observe')
        return {'source': 'native-service-route-path/v1', 'observed_at': min(b['observed_at'] for b in bindings),
            'bindings': bindings, 'revision': hashlib.sha256(_json([{k:v for k,v in b.items() if k != 'observed_at'} for b in bindings]).encode()).hexdigest(),
            'receiver_effect_verified': False}

    def has_session(self, snapshot, session):
        key = session.get('sessionKey')
        with self.db() as db:
            bindings = [json.loads(r[0]) for r in db.execute('SELECT value FROM service_route_bindings')]
        return any(b['accepting'] and b['round_id'] == snapshot['roundId'] and b['generation'] == snapshot['generation']
            and (b['session_key'] == key if key else json.loads(b['session_key'])[1:] ==
                [session['supi'], int(session['sessionId']), session['smContextRef']]) for b in bindings)

    def receiver_windows(self, snapshot, sessions):
        """Only current exact-route native receiver records, retaining original clocks."""
        expected = {self.for_session(snapshot, row)['session_key']: self.for_session(snapshot, row) for row in sessions
            if self.has_session(snapshot, row)}
        with self.db() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_flow_operations'").fetchone()
            operations = [] if not exists else [json.loads(r[0]) for r in db.execute('SELECT value FROM service_flow_operations')]
        result = []
        for operation in operations:
            if operation.get('state') != 'complete':
                continue
            for window in operation.get('windows', ()):
                route = window.get('serviceRoute') or {}
                key = route.get('session_key')
                current = expected.get(key)
                if current is None or window.get('source') != 'native-iperf3-receiver':
                    continue
                if any(route.get(k) != current.get(k) for k in ('binding_id', 'version', 'endpoint_revision',
                        'application_pod_uid', 'upf_epoch', 'path_revision', 'pfcp_seid')):
                    continue
                if (window.get('receiverPodUid') != current['application_pod_uid'] or window.get('endpoint') != current['address']
                        or window.get('port') != current['port'] or window.get('direction') not in {'ul','dl'}):
                    continue
                from .evidence_contracts import PathEvidence
                try:
                    proof = PathEvidence.from_dict(window.get('pathEvidence'))
                except Exception:
                    continue
                if (proof.session_key != key or proof.flow_id != window.get('flow_id')
                        or proof.operation_id != window.get('flow_operation_id')
                        or proof.round_id != snapshot['roundId'] or proof.generation != snapshot['generation']
                        or proof.application_pod_uid != current['application_pod_uid']
                        or proof.endpoint_revision != current['endpoint_revision']):
                    continue
                start, end = window.get('startedAt'), window.get('finishedAt')
                if (type(start) not in (int,float) or type(end) not in (int,float) or not start < end <= self.clock()
                        or not 0 <= self.clock()-end <= self.maximum_age):
                    continue
                if any(type(window.get(k)) not in (int,float) or not math.isfinite(window[k]) or window[k] < 0
                        for k in ('receivedMbps','offeredObservedMbps','receivedBytes','packets','lostPackets')):
                    continue
                result.append({k:v for k,v in window.items() if k not in {'client','server'}})
        # For each exact session/direction only the newest accepted window is evidence.
        latest = {}
        for window in result:
            key = (window['serviceRoute']['session_key'], window['direction'])
            if key not in latest or window['finishedAt'] > latest[key]['finishedAt']:
                latest[key] = window
        return list(latest.values())

    def acquire_flow(self, flow_id, route):
        """Fence every new flow; leases survive restarts and never expire by guess."""
        _text(flow_id)
        with self.db() as db:
            row = db.execute('SELECT value FROM service_route_bindings WHERE id=?', (route['binding_id'],)).fetchone()
            if row is None or json.loads(row[0]) != {k: route[k] for k in ServiceRouteBinding.__dataclass_fields__}:
                raise ActuationBlocked('Route changed before flow admission')
            app = self._app(db, route['application_instance_id'], route['application_pod_uid'])
            if not route['accepting'] or not app['accepting']:
                raise ActuationBlocked('Application route is draining')
            value = _json(route)
            old = db.execute('SELECT value FROM service_route_flows WHERE id=?', (flow_id,)).fetchone()
            if old and old[0] != value:
                raise ActuationBlocked('Flow replay changed route')
            db.execute('INSERT OR IGNORE INTO service_route_flows VALUES (?,?,?)', (flow_id, route['binding_id'], value))
        return route

    def release_flow(self, flow_id, route):
        with self.db() as db:
            row = db.execute('SELECT value FROM service_route_flows WHERE id=?', (flow_id,)).fetchone()
            if row and row[0] != _json(route):
                raise ActuationBlocked('Flow release route differs')
            db.execute('DELETE FROM service_route_flows WHERE id=?', (flow_id,))


class ApplicationEndpointProvider:
    actions = ('bind_application_endpoint', 'drain_application_endpoint')
    def __init__(self, registry):
        self.registry = registry
    def capabilities(self):
        ready = callable(self.registry.native_reader) and callable(self.registry.session_reader)
        return tuple(Capability(a, 'configured' if ready else 'not_configured') for a in self.actions)
    def inspect(self, request):
        value = self.registry.read(request.target_id)
        result={'source': 'server-service-route-registry/v1', 'version': value['version'] if value else 'absent', 'binding': value}
        if value and value['accepting']:
            result['native_path']=self.registry.for_session({'roundId':value['round_id'],'generation':value['generation']},
                {'sessionKey':value['session_key']})
        return result
    def preflight(self, request, before):
        if before['version'] != request.expected_version:
            raise ActuationBlocked('Application route version changed')
        if before['binding'] and before['binding']['owner_id'] != request.owner_id:
            raise ActuationBlocked('Application route belongs to another intent')
        return {'version': before['version']}
    def mutate(self, request, before):
        self.preflight(request, before)
        return self.registry.bind(request) if request.action == 'bind_application_endpoint' else self.registry.drain(
            request.target_id, request.owner_id, request.operation_id, request.expected_version)
    def observe(self, request, mutation):
        return self.inspect(request)
    def confirmed(self, request, observed):
        b = observed.get('binding') or {}
        return b.get('owner_id') == request.owner_id and b.get('operation_id') == request.operation_id and b.get('accepting') == (request.action == 'bind_application_endpoint')
    def restore(self, request, before, installed):
        restored=self.registry.restore_binding(request,before)
        baseline=before.get('binding')
        fields=('session_key','upf_target_id','upf_epoch','application_instance_id','application_pod_uid',
            'endpoint_revision','address','receiver_address','port','protocol','accepting')
        return {'restored':all(restored[k]==baseline[k] for k in fields) if baseline is not None else restored['accepting'] is False,
            'source':'server-service-route-registry/v1','binding':restored}

    def restoration_state(self, request, before, installed):
        """Read the compensation CAS and native baseline; never redispatch it."""
        try:
            key='restore:'+request.target_id+':'+request.operation_id
            digest=hashlib.sha256(_json(before).encode()).hexdigest()
            with self.registry.db() as db:
                record=db.execute('SELECT digest,result FROM service_route_operations WHERE id=?',(key,)).fetchone()
                refs=db.execute('SELECT 1 FROM service_route_flows WHERE binding_id=? LIMIT 1',(request.target_id,)).fetchone()
            current=self.registry.read(request.target_id)
            if (not record or record[0]!=digest or refs or current!=json.loads(record[1])
                    or current['owner_id']!=request.owner_id or current['operation_id']!=request.operation_id+':restore'):
                return {'restored':False}
            baseline=before.get('binding')
            expected=baseline if baseline is not None else current
            if current['accepting']!=(baseline['accepting'] if baseline is not None else False):
                return {'restored':False}
            endpoint=self.registry.endpoint(expected['application_instance_id'])
            path=self.registry.session(expected['session_key'],endpoint['address'])
            if (endpoint.get('owner_id')!=request.owner_id or any(endpoint.get(k)!=expected.get(v) for k,v in (
                    ('pod_uid','application_pod_uid'),('endpoint_id','endpoint_id'),('endpoint_revision','endpoint_revision'),
                    ('address','address'),('receiver_address','receiver_address'),('receiver_pod','receiver_pod'),('port','port'),('protocol','protocol')))
                    or any(path.get(k)!=expected.get(k) for k in ('upf_target_id','upf_epoch','receiver_peer'))):
                return {'restored':False}
            prior=before.get('native_path')
            if prior and any(_json(path.get(k))!=_json(prior.get(k)) for k in ('pfcp_seid','path_revision','pdr_ids','far_ids')):
                return {'restored':False}
            activity=getattr(self.registry,'application_activity_reader',None)
            if not callable(activity):return {'restored':False}
            proof=activity(expected['application_instance_id']);self.registry._fresh(proof)
            if proof.get('active_connections')!=0:return {'restored':False}
            # Fence the read across independent native probes as well.
            with self.registry.db() as db:
                latest=db.execute('SELECT value FROM service_route_bindings WHERE id=?',(request.target_id,)).fetchone()
                refs=db.execute('SELECT 1 FROM service_route_flows WHERE binding_id=? LIMIT 1',(request.target_id,)).fetchone()
            return {'restored':bool(latest and json.loads(latest[0])==current and not refs),
                'source':'native-service-route-restoration-read/v1','binding':current}
        except (ActuationBlocked,KeyError,TypeError,ValueError):
            return {'restored':False}
