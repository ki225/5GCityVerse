"""Pinned operator configuration for the placement census and AI role models."""
from copy import deepcopy

from .domain import ContractError


def configured_placement_reader(context, repository, registry):
    """Use the existing intent configuration path+SHA256 boundary.

    JSON contains allowlisted slot identities and demand assignments, never
    readiness, native versions or session counts. Those come from inspect().
    Geography may be read from the commissioned Kubernetes city inventory;
    without that source the authenticated city-topology PUT remains available.
    """
    from .integration import load_configuration
    from .placement_runtime import NativePlacementContextReader
    configured = getattr(context, 'placement_config', None)
    if configured is None:
        configured = load_configuration().get('placement')
    if not configured:
        return None
    if not isinstance(configured, dict):
        raise ContractError('placement configuration must map intent IDs to commissioned catalogs')
    from backend.onprem.storage.current_state import current_reader
    ledger = getattr(context, 'round_ledger', None)
    reader = current_reader(ledger) if ledger is not None else None
    return NativePlacementContextReader(deepcopy(configured), registry, repository, scenario_reader=reader)


def configured_placement_role_models(context):
    explicit = getattr(context, 'placement_role_models', None)
    if explicit is not None:
        return explicit
    # The existing resource planner already uses the Responses tool protocol.
    # Reuse its configured transport; never fabricate a planner model.
    planner = getattr(getattr(context, 'intent_service', None), 'planner', None)
    model = getattr(getattr(planner, 'provider', None), 'model', None)
    if callable(getattr(model, 'complete', None)):
        return {role: model for role in ('planner', 'executor', 'validator')}
    return None


class NativeCityTopologyReader:
    """Read Helm's city inventory and the actual nodes selected by its placement.

    The ConfigMap resourceVersion fences commissioning. A native node remap must
    be accompanied by recommissioning the inventory; the runtime rejects changed
    content at the same topology revision.
    """
    def __init__(self, client, configmap='cityverse-city-inventory'):
        import re
        if not isinstance(configmap, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', configmap):
            raise ContractError('invalid city inventory ConfigMap name')
        self.client, self.configmap = client, configmap

    def __call__(self):
        import json
        from .placement import CityTopology
        from .placement_runtime import PlacementUnavailable
        value = self.client.call('get', 'configmap', self.configmap, '-o', 'json')
        try:
            revision = value['metadata']['resourceVersion']
            if not isinstance(revision, str) or not revision.isdecimal() or int(revision) < 1:
                raise ValueError()
            city = json.loads(value['data']['city.json'])
            placement = city.get('placement') or json.loads(value['data'].get('placement.json', '{}'))
            mode = placement['mode']
            if mode not in {'physical', 'colocated'}:
                raise ValueError()
            nodes = self.client.call('get', 'nodes', '-o', 'json')['items']
            mapping = {}
            for site in city['sites']:
                selector = placement.get('nodeSelector') if mode == 'colocated' else {'cityverse.io/site': site}
                # Older commissioned single-node inventories contain only
                # colocated mode. Its sole native node is unambiguous; on a
                # multi-node cluster a selector remains mandatory.
                if mode == 'colocated' and not selector:
                    eligible = [n for n in nodes if not n['metadata'].get('deletionTimestamp')]
                    if len(eligible) != 1:
                        raise PlacementUnavailable('Colocated city inventory requires an explicit node selector')
                    mapping[site] = [eligible[0]['metadata']['name']]
                    continue
                if not isinstance(selector, dict) or not selector:
                    raise ValueError()
                mapping[site] = [n['metadata']['name'] for n in nodes
                    if not n['metadata'].get('deletionTimestamp') and
                    all(n['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())]
                if not mapping[site]:
                    raise PlacementUnavailable('City inventory site has no commissioned native nodes: ' + site)
            closing = self.client.call('get', 'configmap', self.configmap, '-o', 'json')
            closing_nodes = self.client.call('get', 'nodes', '-o', 'json')['items']
            def node_scope(rows):
                return sorted((n['metadata']['name'], n['metadata'].get('uid'),
                    json.dumps(n['metadata'].get('labels', {}), sort_keys=True),
                    n['metadata'].get('deletionTimestamp')) for n in rows)
            if (closing.get('metadata', {}).get('resourceVersion') != revision
                    or closing.get('data') != value.get('data') or node_scope(closing_nodes) != node_scope(nodes)):
                raise PlacementUnavailable('Native city inventory changed during topology observation')
            return CityTopology.from_city_inventory(city, revision=int(revision), node_ids_by_site=mapping,
                placement_mode='colocated' if mode == 'colocated' else 'physical_nodes').to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise PlacementUnavailable('Native city inventory is incomplete or malformed') from error


def configured_topology_reader(context):
    explicit = getattr(context, 'placement_topology_reader', None)
    if explicit is not None:
        return explicit
    from .integration import load_configuration
    config = getattr(context, 'placement_topology_config', None)
    if config is None:
        config = load_configuration().get('placement_topology')
    if not config:
        return None
    from .domain import strict_fields
    strict_fields(config, {'context', 'namespace', 'configmap'})
    from .resource_adapters import KubectlResourceClient
    return NativeCityTopologyReader(KubectlResourceClient(context=config['context'], namespace=config['namespace']),
        config.get('configmap', 'cityverse-city-inventory'))
