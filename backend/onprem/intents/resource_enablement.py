"""Compile explicit, deployment-owned resource grants into the formal configuration.

This is an offline operator tool. It performs no Kubernetes, controller or model
calls. Inventory identity is not evidence of capacity, readiness or SLA effect.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from .domain import ContractError
from .model_planner import ResourceBudget, TargetPolicy
from .observations import CapacityProfile

ACTION_PROVIDERS = {
    'update_qos': 'qos', 'resize_nf': 'kubernetes',
    'add_upf': 'upf', 'place_nf': 'upf', 'assign_sessions': 'upf',
    'drain_and_remove': 'upf', 'place_application': 'applications',
    'drain_application': 'applications', 'remove_application': 'applications',
    'bind_application_endpoint': 'service_routes',
    'drain_application_endpoint': 'service_routes',
    'reestablish_sessions': 'service_routes',
    'start_service_flows': 'service_routes', 'stop_service_flows': 'service_routes',
    'provision_node': 'nodes',
}
FRAGMENT_KEYS = frozenset({'providers', 'placement', 'placement_topology',
    'qos_telemetry_targets', 'nf_telemetry_targets', 'native_nf_load',
    'native_nf_services', 'service_route_observation', 'path_capacity_publication'})
CALIBRATED_ACTIONS = frozenset({'resize_nf', 'add_upf', 'place_nf'})


def _require(condition, reason):
    if not condition:
        raise ContractError(reason)


def compile_resource_configuration(base, manifest, inventory, fragment=None):
    """Return a new configuration and an honest configured/authorized matrix.

    Each grant is an exact TargetPolicy plus an inventory identity and explicit
    compensation scope. The resource list is an operator commissioning artifact;
    execution still requires independent, current native evidence and ownership.
    """
    _require(base.get('schema') == 'cityverse.intent-configuration/v1', 'invalid_base_configuration')
    _require(manifest.get('schema') == 'cityverse.resource-enablement/v1', 'invalid_enablement_manifest')
    _require(set(manifest) <= {'schema', 'budgets', 'grants', 'capacity_profiles'}, 'unknown_enablement_field')
    _require(inventory.get('schema') == 'cityverse.resource-inventory/v1', 'invalid_resource_inventory')
    fragment = fragment or {}
    _require(not set(fragment) - FRAGMENT_KEYS, 'unsupported_provider_fragment_field')
    if 'path_capacity_publication' in fragment:
        _require(type(fragment['path_capacity_publication']) is bool, 'path_capacity_publication_requires_boolean')
    config = deepcopy(base)
    for key, value in fragment.items():
        if isinstance(value, dict):
            config[key] = {**config.get(key, {}), **deepcopy(value)}
        else:
            config[key] = deepcopy(value)
    resources = {}
    for resource in inventory['resources']:
        target_id = resource['target_id']
        key = (target_id, resource.get('provider'))
        _require(key not in resources and isinstance(target_id, str) and bool(target_id), 'duplicate_inventory_target')
        _require(resource.get('commissioned') is True, 'resource_not_commissioned')
        _require(isinstance(resource.get('actions'), list) and bool(resource['actions']), 'inventory_actions_missing')
        resources[key] = resource
    budgets = {key: ResourceBudget(**value) for key, value in manifest['budgets'].items()}
    _require(bool(budgets), 'resource_budgets_missing')
    for budget in budgets.values():
        _require(0 < budget.maximum_cost <= 100, 'resource_budget_outside_operator_limit')
        _require(not budget.allowed_actions - ACTION_PROVIDERS.keys(), 'unsupported_budget_action')
        _require(not budget.allow_session_migration or 'reestablish_sessions' in budget.allowed_actions,
                 'migration_requires_explicit_reestablishment_grant')
    profile_rows = deepcopy(base.get('capacity_profiles', []))
    profile_rows.extend(deepcopy(manifest.get('capacity_profiles', [])))
    profiles = {p['profile_id']: CapacityProfile(**p) for p in profile_rows}
    _require(len(profiles) == len(profile_rows), 'duplicate_capacity_profile')
    targets, matrix, seen = [], [], set()
    for grant in manifest['grants']:
        _require(set(grant) == {'policy', 'budget_ids', 'restore_scope'}, 'invalid_resource_grant_fields')
        policy = TargetPolicy(**grant['policy'])
        key = (policy.action, policy.target_id)
        _require(key not in seen, 'duplicate_resource_grant')
        seen.add(key)
        provider = ACTION_PROVIDERS[policy.action]
        resource = resources.get((policy.target_id, provider))
        _require(resource is not None and policy.action in resource['actions'], 'target_not_in_commissioned_inventory')
        _require(resource.get('provider') == provider, 'inventory_provider_mismatch')
        provider_config = config.get('providers', {}).get(provider)
        _require(bool(provider_config), 'resource_provider_not_configured')
        _require(policy.preserves_guarantees is True, 'explicit_guarantee_policy_required')
        _require(0 < policy.fixed_cost <= 100, 'positive_bounded_action_cost_required')
        _require(0 <= policy.disruption_seconds <= 120, 'disruption_outside_operator_limit')
        _require(isinstance(grant['budget_ids'], list) and bool(grant['budget_ids']), 'grant_budget_missing')
        for budget_id in grant['budget_ids']:
            _require(budget_id in budgets and policy.action in budgets[budget_id].allowed_actions,
                     'grant_not_in_resource_budget')
            _require(policy.fixed_cost <= budgets[budget_id].maximum_cost, 'action_exceeds_resource_budget')
        # The current runtime scopes budgets by action, not by target. Do not
        # pretend that a manifest budget_ids field creates a narrower boundary.
        _require(set(grant['budget_ids']) == {key for key, value in budgets.items()
                     if policy.action in value.allowed_actions}, 'runtime_budget_target_scope_not_representable')
        _require(grant['restore_scope'] in {'owned_previous_state', 'owned_created_instance', 'owned_binding'},
                 'invalid_restore_scope')
        fixed = policy.fixed_parameters
        for field in ('node_id', 'template_id', 'container'):
            if policy.action == 'resize_nf' and field != 'container':
                continue
            if policy.action != 'resize_nf' and field == 'container':
                continue
            if field in resource and policy.action in {'resize_nf', 'add_upf', 'place_nf', 'place_application'}:
                _require(fixed.get(field) == resource[field], 'resource_identity_parameter_mismatch:' + field)
                _require(field not in policy.numeric_bounds, 'resource_identity_cannot_be_numeric')
        if provider == 'kubernetes':
            _require(policy.target_id in provider_config.get('pod_allowlist', ()), 'pod_not_in_provider_allowlist')
            _require(provider_config.get('namespace') == resource.get('namespace'), 'resource_namespace_mismatch')
            _require(provider_config.get('in_place_resize') is True, 'in_place_resize_not_enabled')
        if policy.action in {'add_upf', 'place_nf', 'place_application'}:
            _require(fixed.get('template_id') in provider_config.get('templates', ()), 'template_not_in_provider_allowlist')
        if policy.action == 'provision_node':
            _require(resource.get('pool_commissioned') is True and fixed.get('pool_id') in provider_config.get('pools', ()),
                     'native_node_pool_not_commissioned')
        if policy.action == 'reestablish_sessions':
            _require(all(budgets[k].allow_session_migration for k in grant['budget_ids']), 'session_interruption_not_authorized')
        blocked = []
        if policy.action in CALIBRATED_ACTIONS and policy.calibration_profile_id not in profiles:
            blocked.append('native_capacity_calibration_required')
        if policy.action == 'update_qos':
            blocked.append('current_native_qer_path_capacity_and_cohort_required')
        blocked.append('current_native_preflight_and_ownership_required')
        targets.append(deepcopy(grant['policy']))
        matrix.append({'action': policy.action, 'target_id': policy.target_id,
            'provider_configured': True, 'authorized_target': True,
            'native_capability_status': 'not_observed', 'budget_authorized': True,
            'budget_ids': grant['budget_ids'],
            'restore_scope': grant['restore_scope'], 'execution_requirements': blocked,
            'native_execution_verified': False})
    _require(bool(targets), 'resource_targets_missing')
    granted_actions = {p['action'] for p in targets}
    _require(all(b.allowed_actions <= granted_actions for b in budgets.values()), 'budget_action_has_no_target')
    config['targets'] = targets
    config['budgets'] = {key: asdict(value) | {'allowed_actions': sorted(value.allowed_actions)} for key, value in budgets.items()}
    config['capacity_profiles'] = profile_rows
    return config, {'schema': 'cityverse.resource-enablement-report/v1', 'targets': matrix,
        'native_execution_verified': False, 'note': 'Configured authority is not capacity, installation or service-effect proof.'}


def resource_action_readiness(config, capabilities):
    """Join real registry capabilities with configured targets and budgets.

    Never calls a provider or changes native state. A configured capability and
    authorization are only prerequisites; this does not approve any ResourcePlan.
    """
    capability_rows = {row['action']: row for row in capabilities}
    targets = tuple(TargetPolicy(**row) for row in config.get('targets', ()))
    budgets = {key: ResourceBudget(**row) for key, row in config.get('budgets', {}).items()}
    profiles = {row['profile_id'] for row in config.get('capacity_profiles', ())}
    rows = []
    for action, provider in ACTION_PROVIDERS.items():
        cap = capability_rows.get(action, {})
        action_targets = [row for row in targets if row.action == action]
        action_budgets = [key for key, row in budgets.items() if action in row.allowed_actions]
        missing = ['current_native_preflight_and_ownership_required']
        if action in CALIBRATED_ACTIONS and any(row.calibration_profile_id not in profiles for row in action_targets):
            missing.append('native_capacity_calibration_required')
        if action == 'update_qos':
            missing.append('current_native_qer_path_capacity_and_cohort_required')
        if not action_targets:
            missing.append('authorized_target_required')
        if not action_budgets:
            missing.append('resource_budget_required')
        if cap.get('status') != 'configured':
            missing.append('native_capability_not_configured')
        rows.append({'action': action, 'provider': provider,
            'provider_configured': bool(config.get('providers', {}).get(provider)),
            'native_capability_status': cap.get('status', 'not_observed'),
            'authorized_targets': [row.target_id for row in action_targets],
            'budget_ids': action_budgets,
            'configured_authorized': cap.get('status') == 'configured' and bool(action_targets) and bool(action_budgets),
            'execution_requirements': missing, 'native_execution_verified': False})
    return {'schema': 'cityverse.resource-action-readiness/v1', 'actions': rows}


def _read_pinned(path, expected):
    source = Path(path)
    _require(source.is_absolute() and not source.is_symlink(), 'configuration_requires_absolute_regular_path')
    raw = source.read_bytes()
    _require(len(raw) <= 1_048_576 and hashlib.sha256(raw).hexdigest() == expected, 'input_digest_mismatch')
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'manifest', 'inventory', 'fragment'):
        parser.add_argument('--' + name, required=name != 'fragment')
        parser.add_argument('--' + name + '-sha256', required=name != 'fragment')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    _require(bool(args.fragment) == bool(args.fragment_sha256), 'fragment_requires_path_and_digest')
    values = {name: _read_pinned(getattr(args, name), getattr(args, name + '_sha256'))
              for name in ('base', 'manifest', 'inventory')}
    fragment = _read_pinned(args.fragment, args.fragment_sha256) if args.fragment else None
    config, report = compile_resource_configuration(**values, fragment=fragment)
    destination = Path(args.output)
    _require(destination.is_absolute() and not destination.is_symlink(), 'output_requires_absolute_private_path')
    raw = (json.dumps(config, sort_keys=True, separators=(',', ':')) + '\n').encode()
    # Exclusive create prevents overwriting a running deployment's pinned config.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    print(json.dumps({'configuration_sha256': hashlib.sha256(raw).hexdigest(), 'report': report}, sort_keys=True))


if __name__ == '__main__':
    main()
