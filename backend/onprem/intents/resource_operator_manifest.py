"""Conservative grants for the exact commissioned CityVerse resource inventory."""
from copy import deepcopy

from .domain import ContractError
from .native_resource_policy import SERVER_PARAMETERS


def build_operator_manifest(inventory):
    """Derive identities from commissioned inventory; never derive capacity.

    Logical binding slots and operation targets are explicit exact IDs, not
    wildcards. Same target may have distinct provider-specific capabilities.
    """
    current = deepcopy(inventory)
    grants = []

    def grant(resource, action, fixed=None, bounds=None, restore='owned_previous_state', **extra):
        policy = dict(target_id=resource['target_id'], action=action, fixed_cost=1,
            preserves_guarantees=True, fixed_parameters=fixed or {}, numeric_bounds=bounds or {}, **extra)
        grants.append({'policy': policy, 'budget_ids': ['local'], 'restore_scope': restore})

    upfs = {r['target_id']: r for r in current['resources'] if r['provider'] == 'upf'}
    applications = [r for r in current['resources'] if r['provider'] == 'applications']
    for resource in list(current['resources']):
        provider = resource['provider']
        if provider == 'qos':
            grant(resource, 'update_qos', bounds={'rates_kbps.ul': [1, 10000], 'rates_kbps.dl': [1, 10000]},
                  required_diagnosis='policy_limited')
        elif provider == 'kubernetes':
            # No memory NotRequired continuity policy has been commissioned.
            # Keep existing RAM quantities unchanged and never expand limits.
            old = resource['resources']
            grant(resource, 'resize_nf', fixed={'container': resource['container'], 'resources': {
                'requests': {'memory': old['requests']['memory']}, 'limits': old['limits']}},
                bounds={'resources.requests.cpu': [.25, 1]}, derive_impacted_services=True)
        elif provider in {'upf', 'applications'}:
            for action in resource['actions']:
                if action in {'place_nf', 'add_upf', 'place_application'}:
                    grant(resource, action, fixed={key: resource[key] for key in ('node_id', 'template_id')},
                          restore='owned_created_instance')
                elif action == 'assign_sessions':
                    grant(resource, action, fixed={'mode': 'new_sessions'}, restore='owned_binding')
                else:
                    grant(resource, action)
    for app in applications:
        upf_id = app['target_id'].replace('cv-app-', 'cv-upf-', 1)
        if upf_id not in upfs:
            raise ContractError('application_has_no_commissioned_upf_pair')
        route = dict(target_id=app['target_id'].replace('cv-app-', 'cv-route-', 1),
            provider='service_routes', actions=['bind_application_endpoint', 'drain_application_endpoint'],
            commissioned=True, namespace=app['namespace'], site_id=app['site_id'],
            logical_control_slot=True)
        current['resources'].append(route)
        grant(route, 'bind_application_endpoint', fixed={'application_instance_id': app['target_id'],
            'upf_target_id': upf_id}, restore='owned_binding',
            server_parameter_names=sorted(SERVER_PARAMETERS['bind_application_endpoint']))
        grant(route, 'drain_application_endpoint', restore='owned_binding')
        flows = dict(target_id=app['target_id'], provider='service_routes',
            actions=['start_service_flows', 'stop_service_flows'], commissioned=True,
            namespace=app['namespace'], logical_control_slot=True)
        current['resources'].append(flows)
        for action in flows['actions']:
            grant(flows, action, restore='owned_binding', server_parameter_names=sorted(SERVER_PARAMETERS[action]))
        rebuild = dict(target_id=upf_id, provider='service_routes', actions=['reestablish_sessions'],
            commissioned=True, namespace=app['namespace'], logical_control_slot=True)
        current['resources'].append(rebuild)
        grant(rebuild, 'reestablish_sessions', restore='owned_binding', disruption_seconds=120,
            server_parameter_names=sorted(SERVER_PARAMETERS['reestablish_sessions']))
    actions = sorted({g['policy']['action'] for g in grants})
    return {'schema': 'cityverse.resource-enablement/v1', 'budgets': {'local': {
        'maximum_cost': 12, 'allowed_actions': actions, 'allow_session_migration': True}}, 'grants': grants}, current
