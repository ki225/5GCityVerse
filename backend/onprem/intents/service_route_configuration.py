"""Server-only factories for native endpoint, session-path and flow providers."""
from .actuators import ActuationBlocked


class CurrentRouteResources:
    """Resources are created by the async native lifespan after the API factory."""
    def __init__(self, context, registry):
        self.context, self.registry = context, registry
    @property
    def service_routes(self):
        return self.registry
    def __getattr__(self, name):
        resources = getattr(getattr(self.context, 'round_coordinator', None), 'resources', None)
        if resources is None:
            raise ActuationBlocked('Current native scenario resources are unavailable')
        resources.service_routes = self.registry
        return getattr(resources, name)


def configure_service_routes(context, config, providers):
    values = config.get('service_routes')
    if not values:
        return
    if context is None or not providers.get('qos') or not providers.get('applications'):
        raise ValueError('Service routes require server context, native QoS and application providers')
    from pathlib import Path
    from .resource_adapters import KubectlResourceClient
    from .upf_controller import KubectlUpfReadback
    from .native_service_routes import NativeServiceSessionReader
    from .service_routes import ServiceRouteRegistry, ApplicationEndpointProvider
    from .service_flows import ServiceFlowProvider
    from .session_rebuild import SessionRebuildProvider
    path = Path(values['journal_file'])
    if not path.is_absolute():
        raise ValueError('Service route journal must be an absolute operator path')
    kubernetes = KubectlResourceClient(context=values['context'], namespace=values['namespace'])
    pins = {}
    for target, spec in values['session_targets'].items():
        if bool(spec.get('pod_name')) != bool(spec.get('pod_uid')):
            raise ValueError('Existing session route Pod name and UID must be pinned together')
        if spec.get('pod_name'):
            pins[target] = {field: spec[field] for field in ('pod_name', 'pod_uid')}
    reader = NativeServiceSessionReader(providers['qos'].api, KubectlUpfReadback(kubernetes, pinned_pods=pins), kubernetes,
        values['session_targets'])
    registry = ServiceRouteRegistry(path, native_reader=providers['applications'].api.endpoint, session_reader=reader)
    registry.current_snapshot_reader = context.round_ledger.live_snapshot
    registry.current_ownership_reader = getattr(context.round_ledger, 'intent_traffic_lease', None)
    registry.application_activity_reader = providers['applications'].api.inspect
    context.intent_service_routes = registry
    resources = CurrentRouteResources(context, registry)
    providers['application_endpoints'] = ApplicationEndpointProvider(registry)
    providers['service_flows'] = ServiceFlowProvider(resources, registry, context.round_ledger.live_snapshot,
        loop_getter=lambda:getattr(getattr(context,'intent_service',None),'event_loop',None))
    providers['session_routes'] = SessionRebuildProvider(context, registry)
    existing = getattr(getattr(context, 'round_coordinator', None), 'resources', None)
    if existing is not None:
        existing.service_routes = registry
