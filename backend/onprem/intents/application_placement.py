"""Application placement adapter for a commissioned native application controller.

The controller must enforce owner/version CAS and independently inspect actual
runtime placement, readiness and endpoint bindings. No synthetic ready state.
"""
from .actuators import ActuationBlocked, Capability


class FixedOriginApplicationControlApi:
    """Authenticated port for an operator's native application controller.

    The existing UPF controller does not implement these routes. Applications
    remain not_configured until their separate controller proves CAS support.
    """
    def __init__(self, client):
        self.client = client

    def capabilities(self):
        return self.client.commissioned('applications')

    def inspect(self, target_id):
        return self.client.call('applications/read', {'target_id': target_id})

    def endpoint(self, target_id):
        return self.client.call('applications/endpoint-read', {'target_id': target_id})

    def drain(self, target_id, *, owner_id, operation_id, expected_version):
        return self.client.call('applications/drain', dict(target_id=target_id,
            owner_id=owner_id, operation_id=operation_id, expected_version=expected_version))

    def deploy(self, target_id, *, template_id, node_id, owner_id, operation_id, expected_version):
        return self.client.call('applications/deploy', dict(target_id=target_id, template_id=template_id,
            node_id=node_id, owner_id=owner_id, operation_id=operation_id, expected_version=expected_version))

    def remove(self, target_id, *, owner_id, operation_id, expected_version):
        return self.client.call('applications/remove', dict(target_id=target_id,
            owner_id=owner_id, operation_id=operation_id, expected_version=expected_version))

    def restore(self, target_id, before, *, owner_id, operation_id, expected_version):
        return self.client.call('applications/restore', dict(target_id=target_id, before=before,
            owner_id=owner_id, operation_id=operation_id, expected_version=expected_version))


class ApplicationPlacementProvider:
    actions = ('place_application', 'drain_application', 'remove_application')
    state_fields = frozenset({'source', 'target_id', 'version', 'exists', 'ready', 'owner_id', 'operation_id',
        'node_id', 'template_id', 'placement_capacity_verified', 'active_connections', 'endpoint_references',
        'endpoint_verified', 'pod_uid', 'accepting', 'binding_version', 'container_id', 'endpoint_revision',
        'activity_ref', 'observed_at', 'readback_pending', 'cleanup_pending'})

    def __init__(self, controller, *, templates):
        self.api = controller
        self.templates = frozenset(templates)

    def capabilities(self):
        proof = self.api.capabilities()
        return tuple(Capability(a, 'configured' if proof.get(a) is True and self.templates else 'not_configured')
                     for a in self.actions)

    def inspect(self, request):
        value = self.api.inspect(request.target_id)
        if (value.get('source') != 'native-application-controller' or not value.get('version')
                or value.get('target_id') != request.target_id):
            raise ActuationBlocked('Independent application state unavailable')
        return {k: v for k, v in value.items() if k in self.state_fields}

    def preflight(self, request, before):
        if before.get('owner_id') not in (None, request.owner_id):
            raise ActuationBlocked('Application belongs to another intent')
        if request.action == 'place_application':
            if before.get('exists') is not False:
                raise ActuationBlocked('Application creation requires an absent native instance')
            if (request.parameters.get('template_id') not in self.templates
                    or not request.parameters.get('node_id')
                    or before.get('placement_capacity_verified') is not True):
                raise ActuationBlocked('Approved application template and capacity proof required')
        elif request.action == 'drain_application':
            if before.get('exists') is not True or before.get('owner_id') != request.owner_id:
                raise ActuationBlocked('Drain requires an owned application instance')
        else:
            if before.get('exists') is not True or before.get('owner_id') != request.owner_id:
                raise ActuationBlocked('Removal requires an owned application instance')
            if before.get('active_connections') != 0 or before.get('endpoint_references') != 0:
                raise ActuationBlocked('Application still carries traffic or endpoint references')
            if before.get('accepting') is not False:
                raise ActuationBlocked('Application binding admissions must close before removal')
        return {'version': before['version'], 'owner_checked': True}

    def mutate(self, request, before):
        self.preflight(request, before)
        kwargs = dict(owner_id=request.owner_id, operation_id=request.operation_id,
                      expected_version=before['version'])
        if request.action == 'place_application':
            self.api.deploy(request.target_id, template_id=request.parameters['template_id'],
                            node_id=request.parameters['node_id'], **kwargs)
        elif request.action == 'drain_application':
            self.api.drain(request.target_id, **kwargs)
        else:
            self.api.remove(request.target_id, **kwargs)
        return {'phase': 'native_request_complete'}

    def observe(self, request, mutation):
        return self.inspect(request)

    def confirmed(self, request, observed):
        if observed.get('owner_id') != request.owner_id or observed.get('operation_id') != request.operation_id:
            return False
        if observed.get('readback_pending') is True:
            return False
        if request.action == 'drain_application':
            return observed.get('exists') is True and observed.get('accepting') is False
        if request.action == 'remove_application':
            return (observed.get('exists') is False and observed.get('active_connections') == 0
                    and observed.get('endpoint_references') == 0)
        return (observed.get('exists') is True and observed.get('ready') is True
                and observed.get('endpoint_verified') is True and bool(observed.get('pod_uid'))
                and observed.get('node_id') == request.parameters['node_id']
                and observed.get('template_id') == request.parameters['template_id'])

    def restore(self, request, before, installed):
        actual = self.inspect(request)
        if actual.get('owner_id') != request.owner_id or actual.get('operation_id') != request.operation_id:
            raise ActuationBlocked('Application operation owner changed')
        if before.get('exists') is False and actual.get('exists') is True:
            if actual.get('active_connections') != 0 or actual.get('endpoint_references') != 0:
                raise ActuationBlocked('Application must drain connections and endpoints before restoration')
        self.api.restore(request.target_id, before, owner_id=request.owner_id,
                         operation_id=request.operation_id, expected_version=actual['version'])
        return self.restoration_state(request, before, installed)

    def restoration_state(self, request, before, installed):
        """Independently inspect compensation; never reissue its native command."""
        actual = self.inspect(request)
        fields = ('exists', 'node_id', 'template_id', 'owner_id', 'operation_id')
        owners = (request.owner_id,) if before.get('exists') is False and actual.get('exists') is True else (request.owner_id, before.get('owner_id'))
        operations = (request.operation_id,) if before.get('exists') is False and actual.get('exists') is True else (request.operation_id, before.get('operation_id'))
        if actual.get('owner_id') not in owners or actual.get('operation_id') not in operations:
            return {'restored': False, 'pending': False, 'source': 'native-application-controller',
                    'reason': 'Application native ownership changed'}
        if actual.get('exists') is True:
            expected_uid = before.get('pod_uid') if before.get('exists') is True else installed.get('pod_uid')
            if expected_uid is not None and actual.get('pod_uid') != expected_uid:
                return {'restored': False, 'pending': False, 'source': 'native-application-controller',
                        'reason': 'Application native ownership or incarnation changed'}
        if actual.get('readback_pending') is True or actual.get('cleanup_pending') is True:
            return {'restored': False, 'pending': True, 'source': 'native-application-controller'}
        native_ok = (actual.get('active_connections') == 0 and actual.get('endpoint_references') == 0
                     and actual.get('pod_uid') is None and actual.get('endpoint_verified') is False
                     if before.get('exists') is False else
                     all(actual.get(k) == before.get(k) for k in ('pod_uid', 'container_id', 'ready', 'endpoint_revision', 'accepting')))
        return {'restored': native_ok and all(actual.get(k) == before.get(k) for k in fields),
                'pending': False, 'source': 'native-application-controller'}
