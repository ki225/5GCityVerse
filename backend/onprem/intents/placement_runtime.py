"""Placement facade backed by the existing intent operation journal and actuators.

No controller is enabled by geography alone. A commissioned context reader supplies
native instance census (including absent slots), and a ResourceActuator registry
performs bounded native operations. Unknown dispatch outcomes are read back only.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import inspect
import json
import time

from .actuators import ActionReceipt, ActionRequest, ActuationBlocked, build_registry
from .domain import ContractError, timestamp
from .repository import IntentConflict, IntentNotFound


class PlacementUnavailable(NotImplementedError):
    pass


async def _invoke(function, *args, **kwargs):
    result = (function(*args, **kwargs) if inspect.iscoroutinefunction(function)
              else await asyncio.to_thread(function, *args, **kwargs))
    return await result if inspect.isawaitable(result) else result


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _validate_fresh_plan(plan, context, now):
    from .placement import PlacementPlanner
    try:
        PlacementPlanner().validate_context(plan, context, now=now)
    except ContractError as error:
        if str(error) == 'stale_placement_context':
            raise IntentConflict('Placement evidence changed or expired; replan required') from error
        raise


class NativePlacementContextReader:
    """Read exact commissioned slots and retain unchanged evidence snapshots.

    Configuration maps intent IDs to workloads/slots/demands. Every absent slot
    needs a controller readback; controller errors never become a zero census.
    """
    def __init__(self, config, registry, repository, *, clock=time.time, scenario_reader=None):
        self.config, self.registry, self.repository, self.clock = config, registry, repository, clock
        self.scenario_reader = scenario_reader
        self._snapshots = {}

    def assert_spatial_scope(self, intent_id, owner, topology, expected):
        if expected is None:
            return
        if not callable(self.scenario_reader):
            raise PlacementUnavailable('Current spatial scenario reader is unavailable')
        traffic = self.repository.current_intent_traffic(intent_id, owner)
        if traffic is None:
            raise IntentConflict('Owned spatial scenario traffic was released')
        from .spatial_demands import placement_demands
        _, actual = placement_demands(self.scenario_reader(), traffic, self.config[intent_id], topology)
        if actual != expected:
            raise IntentConflict('Spatial scenario scope changed before placement dispatch')

    def __call__(self, intent_id, owner, topology):
        intent = self.repository.get(intent_id, owner)
        config = self.config.get(intent_id)
        if not isinstance(config, dict):
            raise PlacementUnavailable('No commissioned placement catalog for this intent')
        workloads = {w['workload_id']: w for w in config['workloads']}
        instances, now = [], self.clock()
        for slot in config['slots']:
            workload = workloads[slot['workload_id']]
            action = 'place_nf' if workload['kind'] == 'nf' else 'place_application'
            adapter = self.registry.get(action)
            if adapter.provider is None:
                raise PlacementUnavailable('Native placement controller is not configured')
            request = ActionRequest(action, slot['instance_id'], intent_id, 'placement-census', 'inspect-only',
                                    {'node_id': slot['node_id'], 'template_id': workload['template_id']})
            value = adapter.provider.inspect(request)
            if type(value.get('exists')) is not bool or type(value.get('ready')) is not bool:
                raise PlacementUnavailable('Native slot existence/readiness is unknown')
            if value['exists'] and value.get('node_id') != slot['node_id']:
                raise IntentConflict('Native instance moved outside commissioned slot')
            if action == 'place_nf':
                active = value.get('active_session_keys')
                count = len(active) if isinstance(active, list) else None
                ready = (value['ready'] is True and all(value.get(k) is True for k in
                         ('pfcp_associated', 'path_verified', 'smf_selected', 'accepting'))
                         and bool(value.get('pod_uid')))
            else:
                connections, references = value.get('active_connections'), value.get('endpoint_references')
                count = connections + references if type(connections) is int and type(references) is int else None
                ready = value['ready'] is True and value.get('endpoint_verified') is True and bool(value.get('pod_uid'))
            instances.append(dict(instance_id=slot['instance_id'], workload_id=slot['workload_id'],
                node_id=slot['node_id'], version=value['version'], exists=value['exists'], ready=ready,
                owner_id=value.get('owner_id'), active_sessions=count, observed_at=now,
                pod_uid=value.get('pod_uid'), native_id=value.get('native_upf_id'),
                **({'site_id': slot['site_id']} if 'site_id' in slot else {})))
        demands, spatial_evidence = config.get('demands', []), None
        traffic = self.repository.current_intent_traffic(intent_id, owner)
        if traffic is not None:
            if not callable(self.scenario_reader):
                raise PlacementUnavailable('Current scenario reader is required for owned city traffic')
            from .spatial_demands import placement_demands
            demands, spatial_evidence = placement_demands(self.scenario_reader(), traffic, config, topology)
        data = dict(intent_id=intent_id, intent_revision=intent['revision'], topology=topology,
                    workloads=config['workloads'], instances=instances, demands=demands,
                    configured_actions=sorted(c.action for c in self.registry.capabilities() if c.status == 'configured'),
                    census_complete=True, maximum_age_seconds=60)
        if spatial_evidence is not None:
            data['spatial_evidence'] = spatial_evidence
        identity = dict(data, instances=[{k: v for k, v in row.items() if k != 'observed_at'} for row in instances])
        digest = hashlib.sha256(_json(identity).encode()).hexdigest()
        old = self._snapshots.get((intent_id, owner))
        if old and old['observation_id'] == digest and now - old['observed_at'] < 60:
            return json.loads(_json(old))
        data.update(observation_id=digest, observed_at=now)
        self._snapshots[(intent_id, owner)] = data
        return json.loads(_json(data))


class PlacementRuntime:
    def __init__(self, repository, registry=None, context_reader=None, *, clock=time.time, role_models=None, formal_service=None, topology_reader=None):
        self.repository = repository
        self.registry = registry or build_registry()
        self.context_reader = context_reader
        self.clock = clock
        self.role_models = role_models
        self.formal_service = formal_service
        self.topology_reader = topology_reader
        self._lock = asyncio.Lock()
        repository.initialize()
        with repository.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS city_placement_topology
                    (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS city_placement_plans
                    (plan_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, owner TEXT NOT NULL,
                     context TEXT NOT NULL, value TEXT NOT NULL);
            ''')

    async def topology(self):
        native = await _invoke(self.topology_reader) if self.topology_reader is not None else None
        return await asyncio.to_thread(self.read_topology, native)

    def read_topology(self, native=None):
        from .placement import CityTopology
        if self.topology_reader is not None:
            value = native if native is not None else self.topology_reader()
            if inspect.isawaitable(value):
                raise PlacementUnavailable('Synchronous native city inventory reader is required by the observer')
            normalized = CityTopology.from_dict(value).to_dict()
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision,value FROM city_placement_topology WHERE singleton=1').fetchone()
                if row and row['revision'] > normalized['revision']:
                    raise IntentConflict('Commissioned city inventory revision moved backwards')
                if row and row['revision'] == normalized['revision'] and json.loads(row['value']) != normalized:
                    raise IntentConflict('City inventory content changed without a revision')
                db.execute('INSERT OR REPLACE INTO city_placement_topology VALUES(1,?,?)',
                           (normalized['revision'], _json(normalized)))
            return normalized
        with self.repository.connect() as db:
            row = db.execute('SELECT revision,value FROM city_placement_topology WHERE singleton=1').fetchone()
        if row:
            return dict(json.loads(row['value']), revision=row['revision'])
        return CityTopology.default().to_dict()

    async def update_topology(self, body, expected_revision):
        from .placement import CityTopology
        if self.topology_reader is not None:
            raise IntentConflict('City geography is managed by the commissioned infra inventory')
        current = await self.topology()
        revision = current.get('revision', 1)
        if expected_revision != revision:
            raise IntentConflict('City topology revision changed')
        value = CityTopology.from_dict(dict(body, revision=revision + 1)).to_dict()
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM city_placement_topology WHERE singleton=1').fetchone()
            if (row['revision'] if row else 1) != expected_revision:
                raise IntentConflict('City topology revision changed')
            db.execute('INSERT OR REPLACE INTO city_placement_topology VALUES (1,?,?)', (revision + 1, _json(value)))
        return value

    async def context(self, intent_id, owner):
        from .placement import PlacementContext
        intent = self.repository.get(intent_id, owner)
        if self.context_reader is None:
            raise PlacementUnavailable('Native placement census and node commissioning are not configured')
        topology = await self.topology()
        raw = await _invoke(self.context_reader, intent_id, owner, topology)
        value = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw)
        if value.get('intent_id') != intent_id or value.get('intent_revision') != intent['revision']:
            raise IntentConflict('Placement context belongs to another intent revision')
        if value.get('topology') != topology:
            raise IntentConflict('Native placement context uses an obsolete topology')
        # Configured actions come from the actual adapter, never the model/context input.
        configured = {c.action for c in self.registry.capabilities() if c.status == 'configured'}
        value['configured_actions'] = sorted(set(value.get('configured_actions', ())) & configured)
        value.pop('context_ref', None)
        return PlacementContext.from_dict(value).to_dict()

    async def plan(self, intent_id, body, owner):
        from .placement import PlacementContext, PlacementPlanner
        context = PlacementContext.from_dict(await self.context(intent_id, owner))
        result = PlacementPlanner().build(context, body, now=self.clock())
        value = result.to_dict()
        with self.repository.connect() as db:
            prior = db.execute('SELECT * FROM city_placement_plans WHERE plan_id=?', (result.resource_plan.plan_id,)).fetchone()
            if prior:
                if prior['owner'] != owner or prior['intent_id'] != intent_id or prior['value'] != _json(value):
                    raise IntentConflict('Placement plan identity already exists with different content')
            else:
                db.execute('INSERT INTO city_placement_plans VALUES (?,?,?,?,?)',
                           (result.resource_plan.plan_id, intent_id, owner, _json(context.to_dict()), _json(value)))
        return value

    async def get_plan(self, intent_id, plan_id, owner):
        self.repository.get(intent_id, owner)
        with self.repository.connect() as db:
            row = db.execute('SELECT value FROM city_placement_plans WHERE plan_id=? AND intent_id=? AND owner=?',
                             (plan_id, intent_id, owner)).fetchone()
        if row is None:
            raise IntentNotFound('Placement plan not found')
        return json.loads(row['value'])

    async def execute(self, intent_id, plan_id, owner, key):
        from .placement import PlacementContext, PlacementPlan, PlacementPlanner
        from .planning import ResourcePlan
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ContractError('Idempotency-Key is required')
        async with self._lock:
            value = await self.get_plan(intent_id, plan_id, owner)
            resource = ResourcePlan.from_dict(value['resource_plan'])
            fingerprint = hashlib.sha256(_json({'placement_plan': value}).encode()).hexdigest()
            # Idempotent replay precedes observation validation: actions may have changed it.
            with self.repository.connect() as db:
                previous = db.execute('SELECT id,fingerprint FROM operations WHERE owner=? AND key=?', (owner, key)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise IntentConflict('Idempotency key reused with different placement plan')
                return await self._resume(self.repository.operation(previous['id'], owner), owner)
            context = PlacementContext.from_dict(await self.context(intent_id, owner))
            _validate_fresh_plan(PlacementPlan.from_dict(value), context, self.clock())
            intent = self.repository.get(intent_id, owner)
            operation = self.repository.submit(intent, owner, key, fingerprint, kind='placement',
                metadata={'placement_plan': value, 'placement_receipts': [], 'service_effect_verified': False})
            return await self._resume(operation, owner)

    async def validate_plan(self, intent_id, plan_id, owner):
        from .placement import PlacementContext, PlacementPlan, PlacementPlanner
        value = await self.get_plan(intent_id, plan_id, owner)
        plan = PlacementPlan.from_dict(value)
        context = PlacementContext.from_dict(await self.context(intent_id, owner))
        _validate_fresh_plan(plan, context, self.clock())
        for action in plan.resource_plan.actions:
            if action.expected_version.startswith('dependency:'):
                from .planning import staged_precondition
                staged_precondition(plan.resource_plan, action)
                continue
            request = ActionRequest(action.kind, action.target_id, intent_id,
                'precheck:' + plan_id + ':' + action.action_id, action.expected_version,
                dict(action.parameters), action.session_keys)
            await _invoke(self.registry.preflight, request)
        return {'plan_id': plan_id, 'validated': True, 'observation_id': context.data['observation_id']}

    async def reconcile_ai(self, intent_id, body, owner, key):
        if self.formal_service is not None:
            if not isinstance(body, dict) or set(body) - {'constraints'}:
                raise ContractError('placement_reconcile_requires_constraints_only')
            return await self.formal_service.reconcile(intent_id, owner, key, control_options={
                'allowed_actions': ['place_nf', 'place_application', 'drain_and_remove', 'remove_application',
                    'bind_application_endpoint', 'drain_application_endpoint', 'assign_sessions'],
                'constraints': body.get('constraints', {})})
        # An explicitly constructed standalone runtime remains available to
        # offline compatibility tests; the application composition below always
        # binds the one formal service and never enters this diagnostic graph.
        from pathlib import Path
        from backend.onprem.agents.placement import PlacementAgentLoop
        if self.role_models is None:
            raise PlacementUnavailable('Placement role models are not configured')
        path = Path(self.repository.path).resolve().with_suffix('.placement-agents.sqlite3')
        return await PlacementAgentLoop(self, self.role_models, path=path).run(intent_id, body, owner, key)

    async def check_execution(self, intent_id, plan_id, owner, operation_id):
        operation = self.repository.operation(operation_id, owner)
        if (operation['intent_id'] != intent_id or operation.get('placement_plan', {}).get('plan_id') != plan_id):
            raise IntentConflict('Placement operation scope changed')
        if operation.get('placement_pending') or operation['status'] != 'completed':
            return {'pending': True, 'execution_verified': False, 'service_effect_verified': False}
        plan = operation['placement_plan']['resource_plan']
        receipts = operation.get('placement_receipts', ())
        if len(receipts) != len(plan['actions']):
            raise IntentConflict('Placement receipt coverage incomplete')
        for envelope in receipts:
            receipt = ActionReceipt.from_dict({k: v for k, v in envelope.items() if k != 'action_id'})
            actual = await _invoke(self.registry.readback, receipt)
            if actual.state != 'installation_confirmed':
                return {'pending': False, 'execution_verified': False, 'service_effect_verified': False,
                        'replan_required': True}
        current = await self.context(intent_id, owner)
        now = self.clock()
        if (current.get('census_complete') is not True
                or current['intent_revision'] != plan['intent_revision']
                or current['topology']['revision'] != operation['placement_plan']['topology_revision']
                or not 0 <= now - current['observed_at'] <= current['maximum_age_seconds']
                or any(not 0 <= now - row['observed_at'] <= current['maximum_age_seconds'] for row in current['instances'])):
            return {'pending': True, 'execution_verified': False, 'service_effect_verified': False}
        node_site = {n: s['site_id'] for s in current['topology']['sites'] for n in s['node_ids']}
        actual_counts, ready_counts = {}, {}
        for instance in current['instances']:
            if instance['exists']:
                target = (instance['workload_id'], instance['site_id'])
                actual_counts[target] = actual_counts.get(target, 0) + 1
                ready_counts[target] = ready_counts.get(target, 0) + int(instance['ready'])
        desired = {(p['workload_id'], p['site_id']): p['desired_count']
                   for p in operation['placement_plan']['desired_placements']}
        if any(actual_counts.get(k, 0) != desired.get(k, 0) or ready_counts.get(k, 0) != desired.get(k, 0)
               for k in set(actual_counts) | set(desired)):
            return {'pending': False, 'execution_verified': False, 'service_effect_verified': False,
                    'replan_required': True}
        return {'pending': False, 'execution_verified': True, 'service_effect_verified': False,
                'operation_id': operation_id}

    async def _resume(self, operation, owner):
        from .planning import ResourcePlan
        if operation['kind'] != 'placement':
            raise IntentConflict('Operation is not a placement operation')
        if operation['status'] == 'completed':
            return operation
        resource = ResourcePlan.from_dict(operation['placement_plan']['resource_plan'])
        op_id = operation['operation_id']
        scopes = ['intent:' + resource.intent_id] + ['resource:' + a.target_id for a in resource.actions]
        self.repository.acquire(op_id, scopes)
        self.repository.assert_fleet_open(resource.intent_id, op_id)
        receipts = list(operation.get('placement_receipts', ()))
        try:
            for action in resource.actions:
                # Action identity is kept in the journal envelope; never injected into native parameters.
                if any(r.get('action_id') == action.action_id for r in receipts):
                    continue
                pending = operation.get('placement_pending')
                if pending:
                    if pending['action_id'] != action.action_id:
                        raise IntentConflict('Placement journal action order changed')
                    receipt = await _invoke(self.registry.readback, ActionReceipt.from_dict(pending['receipt']))
                else:
                    current = self.repository.get(resource.intent_id, owner)
                    topology = await self.topology()
                    now = self.clock()
                    if (current['revision'] != resource.intent_revision
                            or current.get('status') == 'expired'
                            or now >= resource.expires_at
                            or (current.get('valid_until') is not None and now >= timestamp(current['valid_until']))
                            or (current.get('valid_from') is not None and now < timestamp(current['valid_from']))
                            or topology['revision'] != operation['placement_plan']['topology_revision']):
                        raise IntentConflict('Placement authority changed or expired before native dispatch')
                    self.repository.assert_fleet_open(resource.intent_id, op_id)
                    await self._spatial_fence(resource, owner, topology)
                    for dependency in action.depends_on:
                        envelope = next((r for r in receipts if r.get('action_id') == dependency), None)
                        if envelope is None:
                            raise IntentConflict('Placement dependency has no confirmed receipt')
                        observed = await _invoke(self.registry.readback, ActionReceipt.from_dict(
                            {k: v for k, v in envelope.items() if k != 'action_id'}))
                        if observed.state != 'installation_confirmed':
                            raise IntentConflict('Placement dependency is no longer installed; keeping old instance')
                    version = action.expected_version
                    if version.startswith('dependency:'):
                        source = next((r for r in receipts if r.get('action_id') == version.removeprefix('dependency:')), None)
                        if (source is None or source['request']['target_id'] != action.target_id
                                or source.get('state') != 'installation_confirmed'
                                or version.removeprefix('dependency:') not in action.depends_on):
                            raise IntentConflict('Placement dependency version is not confirmed')
                        version = source['installed']['version']
                    request = ActionRequest(action.kind, action.target_id, resource.intent_id,
                        op_id + ':' + action.action_id, version, dict(action.parameters), action.session_keys)
                    checked = await _invoke(self.registry.preflight, request)
                    receipt = ActionReceipt(request, 'applying', checked['before'], started_at=self.clock())
                    # Durable dispatch barrier. No replay path invokes mutate again.
                    envelope = {'action_id': action.action_id, 'receipt': receipt.to_dict()}
                    with self.repository.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        self._assert_dispatch_authority(db, operation, resource, owner)
                        row = db.execute('SELECT value,status FROM operations WHERE id=?', (op_id,)).fetchone()
                        saved = json.loads(row['value'])
                        if saved.get('placement_pending') or len(saved.get('placement_receipts', ())) != len(receipts):
                            raise IntentConflict('Placement dispatch already claimed; readback required')
                        saved['placement_pending'] = envelope
                        saved['pending_action'] = request.to_dict()
                        saved['pending_before'] = receipt.before
                        db.execute('UPDATE operations SET value=?,status=?,stage=?,updated=? WHERE id=?',
                                   (_json(saved), 'running', 'placement_dispatch', self.clock(), op_id))
                    operation['placement_pending'] = envelope
                    adapter = self.registry.get(request.action)
                    deploy = getattr(adapter.provider, 'deploy_for_placement', None)
                    mutate = deploy if callable(deploy) and request.action in {'place_nf', 'add_upf'} else adapter.provider.mutate
                    mutation = await _invoke(mutate, request, receipt.before)
                    receipt.evidence['mutation'] = mutation
                    receipt = await _invoke(self.registry.readback, receipt)
                if receipt.state != 'installation_confirmed':
                    receipt = await self._continue_activation(operation, owner, resource, action, receipt)
                envelope = dict(receipt.to_dict(), action_id=action.action_id)
                if receipt.state != 'installation_confirmed':
                    self.repository.update_operation(op_id, status='recovery_required', stage='placement_readback',
                        placement_pending={'action_id': action.action_id, 'receipt': receipt.to_dict()})
                    return self.repository.operation(op_id, owner)
                receipts.append(envelope)
                operation['placement_pending'] = None
                self._commit_receipt(op_id, owner, resource.intent_id, envelope, receipts)
            self.repository.update_operation(op_id, status='completed', stage='placement_installation_confirmed',
                                             installation_confirmed=True, service_effect_verified=False)
            self.repository.release_leases(op_id)
        except Exception:
            self.repository.update_operation(op_id, status='recovery_required', stage='placement_recovery_required')
            raise
        return self.repository.operation(op_id, owner)

    async def _spatial_fence(self, resource, owner, topology):
        with self.repository.connect() as db:
            row = db.execute('SELECT context FROM city_placement_plans WHERE plan_id=? AND owner=?',
                             (resource.plan_id, owner)).fetchone()
        expected = json.loads(row['context']).get('spatial_evidence') if row else None
        if expected is not None:
            check = getattr(self.context_reader, 'assert_spatial_scope', None)
            if not callable(check):
                raise PlacementUnavailable('Spatial placement dispatch scope checker is unavailable')
            await _invoke(check, resource.intent_id, owner, topology, expected)

    async def _continue_activation(self, operation, owner, resource, action, receipt):
        """Executor-only durable second phase; a deployed UPF is never redeployed."""
        provider = self.registry.get(receipt.request.action).provider
        prepare = getattr(provider, 'activation_preflight', None)
        activate = getattr(provider, 'activate_deployed', None)
        if not callable(prepare) or not callable(activate) or receipt.evidence.get('activation_dispatch'):
            return receipt
        proof = prepare(receipt.request, receipt.installed)
        if proof is None:
            return receipt
        current = self.repository.get(resource.intent_id, owner)
        topology = await self.topology()
        now = self.clock()
        if (current['revision'] != resource.intent_revision or current.get('status') == 'expired'
                or now >= resource.expires_at
                or current.get('valid_until') is not None and now >= timestamp(current['valid_until'])
                or current.get('valid_from') is not None and now < timestamp(current['valid_from'])
                or topology['revision'] != operation['placement_plan']['topology_revision']):
            raise IntentConflict('Placement authority changed before UPF activation')
        self.repository.assert_fleet_open(resource.intent_id, operation['operation_id'])
        await self._spatial_fence(resource, owner, topology)
        receipt.evidence['activation_dispatch'] = proof
        envelope = {'action_id': action.action_id, 'receipt': receipt.to_dict()}
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_dispatch_authority(db, operation, resource, owner)
            row = db.execute('SELECT value FROM operations WHERE id=? AND owner=?',
                             (operation['operation_id'], owner)).fetchone()
            value = json.loads(row['value'])
            pending = value.get('placement_pending')
            if (not pending or pending['action_id'] != action.action_id
                    or pending['receipt']['evidence'].get('activation_dispatch')):
                raise IntentConflict('UPF activation dispatch already claimed; readback required')
            value['placement_pending'] = envelope
            db.execute('UPDATE operations SET value=?,stage=?,updated=? WHERE id=?',
                       (_json(value), 'placement_upf_activation_dispatch', now, operation['operation_id']))
        await _invoke(activate, receipt.request, proof)
        return await _invoke(self.registry.readback, receipt)

    def _assert_dispatch_authority(self, db, operation, resource, owner):
        """Fence mutable authority in the same transaction as dispatch intent."""
        row = db.execute('SELECT revision,state,value FROM intents WHERE id=? AND owner=?',
                         (resource.intent_id, owner)).fetchone()
        if row is None:
            raise IntentConflict('Placement intent authority is no longer owned')
        current = json.loads(row['value'])
        topology = db.execute('SELECT revision FROM city_placement_topology WHERE singleton=1').fetchone()
        now = self.clock()
        if (row['revision'] != resource.intent_revision or row['state'] == 'expired'
                or now >= resource.expires_at
                or current.get('valid_until') is not None and now >= timestamp(current['valid_until'])
                or current.get('valid_from') is not None and now < timestamp(current['valid_from'])
                or (topology['revision'] if topology else 1) != operation['placement_plan']['topology_revision']):
            raise IntentConflict('Placement authority changed before durable dispatch claim')
        self.repository._assert_fleet_open(db, resource.intent_id, operation['operation_id'])

    def _commit_receipt(self, operation_id, owner, intent_id, envelope, receipts):
        """Keep generic release ownership; intentional deletion retires creations.

        A confirmed absence is the terminal state of an owned creation. It must
        not become a compensation instruction to recreate a deliberately removed
        NF. Preserve the full deletion receipt in the placement journal instead.
        """
        removing = envelope['request']['action'] in {'drain_and_remove', 'remove_application'}
        target = envelope['request']['target_id']
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if removing:
                rows = db.execute('SELECT * FROM operations WHERE owner=? AND intent_id=? AND kind=?',
                                  (owner, intent_id, 'placement')).fetchall()
                for row in rows:
                    value = json.loads(row['value'])
                    released = list(value.get('released', ()))
                    changed = False
                    for creation in value.get('receipts', ()):
                        request = creation['request']
                        if (request['target_id'] != target or request['owner_id'] != intent_id
                                or request['action'] not in {'place_nf', 'place_application'}
                                or creation.get('before', {}).get('exists') is not False):
                            continue
                        if any(r.get('request') == request and r.get('state') == 'released' for r in released):
                            continue
                        retired = deepcopy(creation)
                        retired['state'] = 'released'
                        retired.setdefault('evidence', {})['intentional_removal'] = {
                            'operation_id': envelope['request']['operation_id'],
                            'native_version': envelope['installed'].get('version'),
                            'native_source': envelope['installed'].get('source'),
                            'exists': False, 'installation_confirmed_at': envelope['installation_confirmed_at']}
                        released.append(retired)
                        changed = True
                    if changed:
                        value['released'] = released
                        db.execute('UPDATE operations SET value=?,updated=? WHERE id=?',
                                   (_json(value), self.clock(), row['id']))
            current = db.execute('SELECT value FROM operations WHERE id=? AND owner=?', (operation_id, owner)).fetchone()
            value = json.loads(current['value'])
            value.update(placement_pending=None, placement_receipts=receipts, pending_action=None, pending_before=None,
                         receipts=[r for r in receipts if r['request']['action'] in {'place_nf', 'place_application'}])
            db.execute('UPDATE operations SET value=?,updated=? WHERE id=?', (_json(value), self.clock(), operation_id))


def configured_placement_service(context):
    existing = getattr(context, 'placement_service', None)
    if existing is not None:
        if isinstance(existing, PlacementRuntime):
            existing.formal_service = getattr(context, 'intent_service', None)
        return existing
    service = getattr(context, 'intent_service', None)
    repository = getattr(service, 'repository', None)
    if repository is None:
        return None
    registry = getattr(context, 'placement_registry', None) or getattr(service, 'registry', None) or build_registry()
    reader = getattr(context, 'placement_context_reader', None)
    if reader is None:
        from .placement_configuration import configured_placement_reader
        reader = configured_placement_reader(context, repository, registry)
    from .placement_configuration import configured_placement_role_models, configured_topology_reader
    return PlacementRuntime(repository, registry, reader,
                            role_models=configured_placement_role_models(context), formal_service=service,
                            topology_reader=configured_topology_reader(context))
