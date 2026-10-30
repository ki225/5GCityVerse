"""Server-calculated city geography and exact, revision-bound placement plans."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
from math import hypot
import time

from .domain import ContractError, finite, required, strict_fields
from .planning import PlanAction, ResourcePlan


def _contract_input(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, IndexError) as error:
            raise ContractError('missing or malformed placement fields') from error
    return checked


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _count(value, name, maximum=256):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContractError(name + ' must be an integer in 0..' + str(maximum))
    return value


def _position(value):
    strict_fields(value, {'x', 'y'})
    return {axis: finite(value[axis], axis) for axis in ('x', 'y')}


def validate_resource_plan_placement(plan, spatial_context, constraints, now=None):
    """Project mixed-plan deployment actions onto the server's native census."""
    try:
        context = PlacementContext.from_dict(spatial_context)
        data = context.data
        now = time.time() if now is None else now
        if not data['census_complete'] or not 0 <= now - data['observed_at'] <= data['maximum_age_seconds']:
            return ('placement_evidence_missing_or_stale',)
        if plan.intent_id != data['intent_id'] or plan.intent_revision != data['intent_revision']:
            return ('placement_intent_scope_mismatch',)
        slots = {s['instance_id']: dict(s) for s in data['instances']}
        workloads = {w['workload_id']: w for w in data['workloads']}
        sites = {s['site_id']: s for s in data['topology']['sites']}
        for slot in slots.values():
            if not 0 <= now - slot['observed_at'] <= data['maximum_age_seconds']:
                return ('placement_instance_evidence_stale',)
        for action in plan.actions:
            if action.kind not in {'place_nf', 'add_upf', 'place_application', 'drain_and_remove', 'remove_application'}:
                continue
            slot = slots.get(action.target_id)
            if slot is None:
                return ('placement_target_not_commissioned',)
            workload = workloads[slot['workload_id']]
            expected_kind = 'application' if action.kind in {'place_application', 'remove_application'} else 'nf'
            if workload['kind'] != expected_kind or expected_kind == 'nf' and workload.get('nf_type') != 'UPF':
                return ('placement_action_workload_kind_mismatch',)
            if not action.expected_version.startswith('dependency:') and action.expected_version != slot['version']:
                return ('placement_native_version_mismatch',)
            if slot.get('owner_id') not in (None, data['intent_id']):
                return ('placement_owner_changed',)
            if action.kind in {'place_nf', 'add_upf', 'place_application'}:
                if (slot['exists'] or action.parameters.get('node_id') != slot['node_id']
                        or action.parameters.get('template_id') != workload['template_id']
                        or slot['site_id'] not in workload['allowed_site_ids']
                        or workload['kind'] not in sites[slot['site_id']]['allowed_workload_types']):
                    return ('placement_candidate_mismatch',)
                slot['exists'] = True
            else:
                if slot.get('owner_id') != data['intent_id'] or slot.get('active_sessions') != 0:
                    return ('placement_removal_not_quiescent',)
                slot['exists'] = False
        counts = {}
        for slot in slots.values():
            if slot['exists']:
                key = (slot['workload_id'], slot['site_id'])
                counts[key] = counts.get(key, 0) + 1
        strict_fields(constraints, {'max_total_instances', 'max_geographic_chain_km'})
        maximum = _count(constraints.get('max_total_instances', 256), 'max_total_instances')
        if sum(counts.values()) > maximum:
            return ('placement_total_instance_budget_exceeded',)
        for name, workload in workloads.items():
            total = sum(v for (wid, _), v in counts.items() if wid == name)
            if not workload['min_instances'] <= total <= workload['max_instances']:
                return ('placement_workload_instance_bounds_violated',)
        distances = PlacementPlanner._distances(data, counts)
        limit = constraints.get('max_geographic_chain_km')
        if limit is not None:
            finite(limit, 'max_geographic_chain_km')
        if any(d['geographic_chain_km'] is None or limit is not None and d['geographic_chain_km'] > limit for d in distances):
            return ('placement_geographic_chain_constraint_violated',)
        return ()
    except (ContractError, KeyError, TypeError, ValueError):
        return ('placement_context_or_constraints_invalid',)


@dataclass(frozen=True)
class CityTopology:
    data: dict

    @classmethod
    def default(cls):
        sites = [{'site_id': name, 'tier': tier, 'position': {'x': x, 'y': y},
                  'node_ids': [], 'allowed_workload_types': ['nf', 'application']}
                 for name, tier, x, y in [('edge-nw', 'edge', 6, 6), ('edge-ne', 'edge', 18, 6),
                    ('edge-sw', 'edge', 6, 18), ('edge-se', 'edge', 18, 18),
                    ('distributed', 'distributed', 12, 12), ('central', 'central', 12, 112)]]
        return cls.from_dict({'revision': 1, 'coordinate_system': {'unit': 'km', 'origin': 'top_left',
            'x_direction': 'right', 'y_direction': 'down'},
            'city_bounds': {'min_x': 0, 'min_y': 0, 'max_x': 24, 'max_y': 24}, 'sites': sites,
            'cell_grid': {'start_x': 1.5, 'start_y': 1.5, 'spacing': 3, 'columns': 8, 'rows': 8}})

    @classmethod
    @_contract_input
    def from_dict(cls, raw):
        strict_fields(raw, {'revision', 'coordinate_system', 'city_bounds', 'sites', 'cell_grid', 'cells', 'placement_mode', 'radio_revision'})
        data = deepcopy(raw)
        if type(data.get('revision')) is not int or data['revision'] < 1:
            raise ContractError('topology revision must be a positive integer')
        if 'radio_revision' in data and (type(data['radio_revision']) is not int or data['radio_revision'] < 1):
            raise ContractError('radio_revision must be a positive integer')
        expected = {'unit': 'km', 'origin': 'top_left', 'x_direction': 'right', 'y_direction': 'down'}
        if data.get('coordinate_system') != expected:
            raise ContractError('unsupported coordinate_system')
        bounds = data.get('city_bounds')
        if bounds != {'min_x': 0, 'min_y': 0, 'max_x': 24, 'max_y': 24}:
            raise ContractError('city_bounds must describe the 24 km city')
        mode = data.setdefault('placement_mode', 'physical_nodes')
        if mode not in {'physical_nodes', 'colocated'}:
            raise ContractError('unsupported placement_mode')
        if not isinstance(data.get('sites'), list) or not 1 <= len(data['sites']) <= 256:
            raise ContractError('topology requires a bounded site catalog')
        sites, nodes = {}, set()
        for site in data.get('sites', ()):
            strict_fields(site, {'site_id', 'tier', 'position', 'node_ids', 'allowed_workload_types'})
            name = required(site.get('site_id'), 'site_id')
            if name in sites or site.get('tier') not in {'edge', 'distributed', 'central'}:
                raise ContractError('duplicate site or unsupported tier')
            expected_tier = 'edge' if name.startswith('edge-') else 'distributed' if name.startswith('distributed') else 'central' if name == 'central' else None
            if expected_tier and site['tier'] != expected_tier:
                raise ContractError('site tier must match its commissioned identity')
            site['position'] = _position(site['position'])
            ids = site.get('node_ids', [])
            if not isinstance(ids, list) or len(ids) != len(set(ids)):
                raise ContractError('node_ids requires unique IDs')
            for node in ids:
                required(node, 'node_id')
                if node in nodes and mode != 'colocated':
                    raise ContractError('node belongs to multiple sites')
                nodes.add(node)
            if not set(site.get('allowed_workload_types', ())) <= {'nf', 'application'}:
                raise ContractError('unsupported workload kind')
            sites[name] = site
        grid = data.get('cell_grid')
        if grid is not None and grid != {'start_x': 1.5, 'start_y': 1.5, 'spacing': 3, 'columns': 8, 'rows': 8}:
            raise ContractError('unsupported candidate cell_grid')
        if 'cells' not in data and grid is None:
            raise ContractError('provide commissioned cells or a candidate cell_grid')
        if grid is not None:
            cells = [{'cell_id': f'cell-r{r}-c{c}', 'position': {'x': 1.5 + c * 3, 'y': 1.5 + r * 3},
                      'serving_enabled': False} for r in range(8) for c in range(8)]
        else:
            cells = []
        if 'cells' in data:
            supplied = data['cells']
            if not isinstance(supplied, list) or not 1 <= len(supplied) <= 1024 or grid is not None and len(supplied) != 64:
                raise ContractError('invalid commissioned cell catalog')
            seen = set()
            for index, actual in enumerate(supplied):
                strict_fields(actual, {'cell_id', 'position', 'serving_enabled'})
                name = required(actual.get('cell_id'), 'cell_id')
                position = _position(actual['position'])
                if (name in seen or type(actual.get('serving_enabled')) is not bool
                        or not 0 <= position['x'] <= 24 or not 0 <= position['y'] <= 24):
                    raise ContractError('invalid commissioned cell')
                if grid is not None and (name != cells[index]['cell_id'] or position != cells[index]['position']):
                    raise ContractError('invalid candidate cell')
                seen.add(name)
            cells = supplied
        data['cells'], data['cell_grid'] = cells, grid
        return cls(data)

    @classmethod
    def from_city_inventory(cls, city, *, revision, node_ids_by_site, placement_mode):
        if city.get('schemaVersion') != 'cityverse.city-infra/v1' or city.get('map') != {'widthKm': 24, 'heightKm': 24}:
            raise ContractError('unsupported native city inventory')
        sites = [{'site_id': name, 'tier': value['tier'],
                  'position': dict(zip(('x', 'y'), value['position'])),
                  'node_ids': list(node_ids_by_site.get(name, ())),
                  'allowed_workload_types': ['nf', 'application']}
                 for name, value in city['sites'].items()]
        cells = [{'cell_id': gnb['id'], 'position': dict(zip(('x', 'y'), gnb['position'])),
                  'serving_enabled': True} for gnb in city['gnbs']]
        return cls.from_dict({'revision': revision,
            'coordinate_system': {'unit': 'km', 'origin': 'top_left', 'x_direction': 'right', 'y_direction': 'down'},
            'city_bounds': {'min_x': 0, 'min_y': 0, 'max_x': 24, 'max_y': 24},
            'sites': sites, 'cells': cells, 'placement_mode': placement_mode,
            'radio_revision': city.get('topologyRevision', 1)})

    @property
    def revision(self):
        return self.data['revision']

    def to_dict(self):
        return deepcopy(self.data)


@dataclass(frozen=True)
class PlacementContext:
    data: dict

    @classmethod
    @_contract_input
    def from_dict(cls, raw):
        strict_fields(raw, {'intent_id', 'intent_revision', 'observation_id', 'observed_at', 'maximum_age_seconds',
            'topology', 'workloads', 'instances', 'demands', 'configured_actions', 'census_complete', 'context_ref', 'placement_counts', 'spatial_evidence'})
        data = deepcopy(raw)
        supplied_ref = data.pop('context_ref', None)
        data.pop('placement_counts', None)
        required(data.get('intent_id'), 'intent_id')
        required(data.get('observation_id'), 'observation_id')
        if _count(data.get('intent_revision'), 'intent_revision', 2**31) == 0:
            raise ContractError('intent_revision must be positive')
        finite(data.get('observed_at'), 'observed_at')
        finite(data.get('maximum_age_seconds'), 'maximum_age_seconds', .001)
        data['topology'] = CityTopology.from_dict(data['topology']).to_dict()
        for name in ('workloads', 'instances', 'demands', 'configured_actions'):
            if not isinstance(data.get(name), list):
                raise ContractError(name + ' must be an array')
        if type(data.get('census_complete')) is not bool:
            raise ContractError('census_complete must be boolean')
        sites = {v['site_id']: v for v in data['topology']['sites']}
        nodes = {n: s for s in sites.values() for n in s['node_ids']}
        workloads = {}
        for workload in data['workloads']:
            strict_fields(workload, {'workload_id', 'kind', 'nf_type', 'template_id', 'allowed_site_ids', 'min_instances', 'max_instances', 'slice_scope'})
            name = required(workload.get('workload_id'), 'workload_id')
            if name in workloads or workload.get('kind') not in {'nf', 'application'}:
                raise ContractError('invalid workload')
            if workload['kind'] == 'nf' and workload.get('nf_type') != 'UPF':
                raise ContractError('only commissioned UPF NF placement is supported')
            required(workload.get('template_id'), 'template_id')
            if not isinstance(workload.get('allowed_site_ids'), list) or not workload['allowed_site_ids'] or not set(workload['allowed_site_ids']) <= sites.keys():
                raise ContractError('invalid allowed_site_ids')
            low, high = workload.setdefault('min_instances', 0), workload.setdefault('max_instances', 16)
            if _count(low, 'min_instances') > _count(high, 'max_instances'):
                raise ContractError('min_instances exceeds max_instances')
            workloads[name] = workload
        seen = set()
        for instance in data['instances']:
            strict_fields(instance, {'instance_id', 'workload_id', 'node_id', 'version', 'exists', 'ready', 'owner_id',
                'active_sessions', 'observed_at', 'pod_uid', 'native_id', 'site_id'})
            name = required(instance.get('instance_id'), 'instance_id')
            if name in seen or instance.get('workload_id') not in workloads or instance.get('node_id') not in nodes:
                raise ContractError('invalid instance slot')
            possible = [s['site_id'] for s in sites.values() if instance['node_id'] in s['node_ids']]
            if 'site_id' not in instance:
                if len(possible) != 1:
                    raise ContractError('colocated instance requires an explicit commissioned site_id')
                instance['site_id'] = possible[0]
            if instance['site_id'] not in possible:
                raise ContractError('instance site and native node mapping differ')
            seen.add(name)
            required(instance.get('version'), 'native version')
            if type(instance.get('exists')) is not bool or type(instance.get('ready')) is not bool:
                raise ContractError('instance census requires explicit exists/ready')
            if instance['ready'] and not instance['exists']:
                raise ContractError('absent instance cannot be ready')
            finite(instance.get('observed_at'), 'instance observed_at')
            if instance.get('active_sessions') is not None:
                _count(instance['active_sessions'], 'active_sessions', 2**31)
        cells = {v['cell_id']: v for v in data['topology']['cells']}
        groups = set()
        for demand in data['demands']:
            strict_fields(demand, {'group_id', 'serving_cell_id', 'weight', 'upf_workload_id', 'application_workload_id',
                'parent_group_id', 'requested_ue_count', 'requested_ul_mbps', 'requested_dl_mbps', 'weight_basis'})
            group = required(demand.get('group_id'), 'group_id')
            if group in groups or demand.get('serving_cell_id') not in cells:
                raise ContractError('invalid demand group or serving cell')
            if not cells[demand['serving_cell_id']]['serving_enabled']:
                raise ContractError('demand serving cell is only an uncommissioned candidate')
            groups.add(group)
            finite(demand.get('weight'), 'demand weight', .001)
            if 'parent_group_id' in demand:
                required(demand['parent_group_id'], 'parent_group_id')
                count = _count(demand.get('requested_ue_count'), 'requested_ue_count', 2**31)
                if count == 0 or demand.get('weight_basis') != 'requested_ul_plus_dl_mbps':
                    raise ContractError('invalid spatial demand weight basis')
                ul = finite(demand.get('requested_ul_mbps'), 'requested_ul_mbps', .001)
                dl = finite(demand.get('requested_dl_mbps'), 'requested_dl_mbps', .001)
                if abs(demand['weight'] - ul - dl) > 1e-9:
                    raise ContractError('spatial demand directional weight mismatch')
            upf, app = workloads.get(demand.get('upf_workload_id')), workloads.get(demand.get('application_workload_id'))
            if not upf or not app or upf['kind'] != 'nf' or app['kind'] != 'application':
                raise ContractError('demand requires an approved UPF/application pair')
        result = cls(data)
        if supplied_ref is not None and supplied_ref != result.context_ref:
            raise ContractError('context_ref_mismatch')
        return result

    @property
    def context_ref(self):
        return _digest(self.data)

    def to_dict(self):
        return {**deepcopy(self.data), 'context_ref': self.context_ref,
                'placement_counts': self.placement_counts()}

    def placement_counts(self, now=None):
        now = time.time() if now is None else now
        node_site = {node: s['site_id'] for s in self.data['topology']['sites'] for node in s['node_ids']}
        result = []
        for workload in self.data['workloads']:
            for site_id in workload['allowed_site_ids']:
                values = [v for v in self.data['instances'] if v['workload_id'] == workload['workload_id'] and v['site_id'] == site_id]
                known = (self.data['census_complete']
                    and 0 <= now - self.data['observed_at'] <= self.data['maximum_age_seconds']
                    and all(0 <= now - v['observed_at'] <= self.data['maximum_age_seconds'] for v in values))
                result.append({'workload_id': workload['workload_id'], 'site_id': site_id,
                    'observed_count': sum(v['exists'] for v in values) if known else None,
                    'ready_count': sum(v['exists'] and v['ready'] for v in values) if known else None})
        return result


@dataclass(frozen=True)
class PlacementPlan:
    resource_plan: ResourcePlan
    details: dict

    @classmethod
    @_contract_input
    def from_dict(cls, raw):
        strict_fields(raw, {'resource_plan', 'plan_id', 'status', 'context_ref', 'topology_revision',
            'distance_model', 'desired_placements', 'constraints', 'distance_estimates',
            'weighted_geographic_chain_km', 'routing_impact', 'violations'})
        value = deepcopy(raw)
        resource = ResourcePlan.from_dict(value.pop('resource_plan'))
        if value.get('plan_id') != resource.plan_id:
            raise ContractError('placement plan identity mismatch')
        return cls(resource, value)

    def to_dict(self):
        return {**deepcopy(self.details), 'resource_plan': self.resource_plan.to_dict()}


class PlacementPlanner:
    @staticmethod
    def validate_context(plan, context, now=None):
        now = time.time() if now is None else now
        data = context.data
        if (plan.details['context_ref'] != context.context_ref or now >= plan.resource_plan.expires_at
                or not 0 <= now - data['observed_at'] <= data['maximum_age_seconds']):
            raise ContractError('stale_placement_context')

    @_contract_input
    def build(self, context, request, now=None):
        now = time.time() if now is None else now
        strict_fields(request, {'request_id', 'context_ref', 'topology_revision', 'desired_placements', 'constraints'})
        data = context.data
        required(request.get('request_id'), 'request_id')
        if (request.get('context_ref') != context.context_ref or request.get('topology_revision') != data['topology']['revision']
                or not 0 <= now - data['observed_at'] <= data['maximum_age_seconds']):
            raise ContractError('stale_placement_context')
        if not data['census_complete']:
            raise ContractError('instance_census_not_configured')
        if any(not 0 <= now - v['observed_at'] <= data['maximum_age_seconds'] for v in data['instances']):
            raise ContractError('stale_instance_census')
        sites = {v['site_id']: v for v in data['topology']['sites']}
        node_site = {n: s['site_id'] for s in sites.values() for n in s['node_ids']}
        workloads = {v['workload_id']: v for v in data['workloads']}
        desired = {}
        if not isinstance(request.get('desired_placements'), list):
            raise ContractError('desired_placements must be an array')
        for target in request['desired_placements']:
            strict_fields(target, {'workload_id', 'site_id', 'desired_count'})
            key = (target.get('workload_id'), target.get('site_id'))
            workload = workloads.get(key[0])
            if key in desired or not workload or key[1] not in workload['allowed_site_ids'] or workload['kind'] not in sites[key[1]]['allowed_workload_types']:
                raise ContractError('duplicate or unauthorized placement')
            desired[key] = _count(target.get('desired_count'), 'desired_count')
        # A proposal is a complete desired state, so omission cannot silently retain resources.
        for workload in workloads.values():
            count = sum(v for (wid, _), v in desired.items() if wid == workload['workload_id'])
            if not workload['min_instances'] <= count <= workload['max_instances']:
                raise ContractError('workload_instance_bounds_violated')
        constraints = request.get('constraints', {})
        strict_fields(constraints, {'max_total_instances', 'max_geographic_chain_km'})
        if sum(desired.values()) > _count(constraints.get('max_total_instances', 256), 'max_total_instances'):
            raise ContractError('total_instance_budget_exceeded')
        distance_limit = constraints.get('max_geographic_chain_km')
        if distance_limit is not None:
            finite(distance_limit, 'max_geographic_chain_km')
        existing = {}
        for instance in data['instances']:
            if instance['exists']:
                key = (instance['workload_id'], instance['site_id'])
                existing[key] = existing.get(key, 0) + 1
        before = self._distances(data, existing)
        proposed = self._distances(data, desired)
        if any(v['geographic_chain_km'] is None or distance_limit is not None and v['geographic_chain_km'] > distance_limit for v in proposed):
            raise ContractError('geographic_chain_constraint_violated')
        actions, additions, removals = [], [], []
        for key in sorted(set(existing) | set(desired)):
            workload = workloads[key[0]]
            slots = sorted((v for v in data['instances'] if (v['workload_id'], v['site_id']) == key), key=lambda v: v['instance_id'])
            delta = desired.get(key, 0) - existing.get(key, 0)
            selected = [v for v in slots if v['exists'] is (delta < 0)]
            if len(selected) < abs(delta):
                raise ContractError('commissioned_instance_slots_exhausted')
            for slot in selected[:abs(delta)]:
                if slot.get('owner_id') not in (None, data['intent_id']):
                    raise ContractError('instance_owned_by_another_intent')
                if delta < 0 and (slot.get('owner_id') != data['intent_id'] or slot.get('active_sessions') != 0):
                    raise ContractError('instance_not_safe_to_remove')
                kind = ('place_nf' if workload['kind'] == 'nf' else 'place_application') if delta > 0 else ('drain_and_remove' if workload['kind'] == 'nf' else 'remove_application')
                if kind not in data['configured_actions']:
                    raise ContractError('not_configured:' + kind)
                value = (kind, slot, workload)
                (additions if delta > 0 else removals).append(value)
        for kind, slot, workload in additions + removals:
            removing = kind in {'drain_and_remove', 'remove_application'}
            parameters = {} if removing else {'node_id': slot['node_id'], 'template_id': workload['template_id']}
            dependencies = tuple(a.action_id for a in actions if a.kind in {'place_nf', 'place_application'}) if removing else ()
            expected_version = slot['version']
            if kind == 'remove_application' and 'drain_application' in data['configured_actions']:
                drain_id = 'placement-' + str(len(actions))
                actions.append(PlanAction(action_id=drain_id, kind='drain_application', target_id=slot['instance_id'],
                    parameters={}, expected_version=slot['version'], depends_on=dependencies))
                dependencies = (*dependencies, drain_id)
                expected_version = 'dependency:' + drain_id
            actions.append(PlanAction(action_id='placement-' + str(len(actions)), kind=kind, target_id=slot['instance_id'],
                parameters=parameters, expected_version=expected_version, depends_on=dependencies))
        if len(actions) > 256:
            raise ContractError('placement_action_budget_exceeded')
        plan_id = 'placement-' + _digest({'context': context.context_ref, 'request': request})[:24]
        expires_at = min([data['observed_at']] + [v['observed_at'] for v in data['instances']]) + data['maximum_age_seconds']
        resource = ResourcePlan(plan_id, data['intent_id'], data['intent_revision'], data['observation_id'], tuple(actions),
            (context.context_ref,), ('Geographic estimate does not prove traffic routing or latency improvement.',),
            0, float(sum(desired.values())), 0, float(len(actions)), True, expires_at)
        details = {'plan_id': plan_id, 'status': 'validated', 'context_ref': context.context_ref,
            'topology_revision': data['topology']['revision'], 'distance_model': 'euclidean_km',
            'desired_placements': deepcopy(request['desired_placements']), 'constraints': deepcopy(constraints),
            'distance_estimates': {'before': before, 'proposed': proposed},
            'weighted_geographic_chain_km': {'before': self._weighted(before), 'proposed': self._weighted(proposed)},
            'routing_impact': 'requires_session_and_endpoint_binding', 'violations': []}
        return PlacementPlan(resource, details)

    @staticmethod
    def _weighted(values):
        if not values or any(v['geographic_chain_km'] is None for v in values):
            return None
        return sum(v['weight'] * v['geographic_chain_km'] for v in values) / sum(v['weight'] for v in values)

    @staticmethod
    def _distances(data, counts):
        sites = {v['site_id']: v['position'] for v in data['topology']['sites']}
        cells = {v['cell_id']: v['position'] for v in data['topology']['cells']}
        def distance(a, b):
            return hypot(a['x'] - b['x'], a['y'] - b['y'])
        result = []
        for demand in data['demands']:
            candidates = [(distance(cells[demand['serving_cell_id']], sites[upf]) + distance(sites[upf], sites[app]), upf, app)
                for (wid, upf), n in counts.items() if wid == demand['upf_workload_id'] and n > 0
                for (aid, app), m in counts.items() if aid == demand['application_workload_id'] and m > 0]
            best = min(candidates) if candidates else (None, None, None)
            result.append({'group_id': demand['group_id'], 'geographic_chain_km': best[0],
                'upf_site_id': best[1], 'application_site_id': best[2], 'weight': demand['weight'],
                'basis': 'nearest_feasible_placement_estimate'})
        return result
