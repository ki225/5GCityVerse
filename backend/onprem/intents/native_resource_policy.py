"""Server-only contextual parameters; models cannot supply these identities."""
from dataclasses import dataclass, replace

from .actuators import ActuationBlocked
from backend.onprem.runtime.current_sessions import session_rows

SERVER_PARAMETERS = {
    'start_service_flows': frozenset({'round_id', 'generation'}),
    'stop_service_flows': frozenset({'round_id', 'generation'}),
    'reestablish_sessions': frozenset({'round_id', 'generation', 'expected_population_revision'}),
    'bind_application_endpoint': frozenset({'round_id', 'generation', 'service_type',
        'demand_group_id', 'upf_epoch', 'expected_app_version'}),
}


@dataclass(frozen=True)
class NativeOperationAnchor:
    intent_id: str
    operation_id: str
    traffic_request_id: str
    round_id: str
    generation: int

    def __post_init__(self):
        if (any(not isinstance(v, str) or not v for v in
                (self.intent_id, self.operation_id, self.traffic_request_id, self.round_id))
                or type(self.generation) is not int or self.generation < 1):
            raise ActuationBlocked('native_operation_anchor_invalid')


def native_snapshot_reader(provider):
    reader = getattr(provider, 'snapshot_reader', None) or getattr(provider, '_snapshot', None)
    return reader if callable(reader) else getattr(getattr(provider, 'registry', None), 'current_snapshot_reader', None)


def materialize_native_parameters(request, names, provider, anchor=None):
    names = frozenset(names)
    if not names:
        return request
    if not names <= SERVER_PARAMETERS.get(request.action, frozenset()):
        raise ActuationBlocked('unsupported_server_parameter')
    if names.intersection(request.parameters):
        raise ActuationBlocked('model_supplied_server_parameter')
    if type(anchor) is not NativeOperationAnchor:
        raise ActuationBlocked('native_operation_anchor_required')
    if (anchor.intent_id, anchor.operation_id) != (request.owner_id, request.operation_id):
        raise ActuationBlocked('native_operation_anchor_owner_changed')
    registry = getattr(provider, 'registry', None)
    reader = native_snapshot_reader(provider)
    if not callable(reader):
        raise ActuationBlocked('current_native_round_reader_required')
    snapshot = reader()
    def assert_anchor(value):
        if (value.get('roundId'), value.get('generation'), value.get('requestId')) != (
                anchor.round_id, anchor.generation, anchor.traffic_request_id):
            raise ActuationBlocked('native_operation_anchor_scope_changed')
    assert_anchor(snapshot)
    if (not snapshot.get('roundId') or type(snapshot.get('generation')) is not int
            or snapshot['generation'] < 1 or snapshot.get('stage') in {'completed', 'cleaning'}):
        raise ActuationBlocked('current_native_round_required')
    ownership_reader = getattr(registry, 'current_ownership_reader', None)
    if not callable(ownership_reader):
        raise ActuationBlocked('current_native_traffic_owner_required')
    ownership = ownership_reader(snapshot['roundId'])
    lease = (ownership or {}).get('lease', {})
    if ((ownership or {}).get('state') != 'active' or lease.get('intent_id') != request.owner_id
            or not snapshot.get('requestId') or lease.get('request_id') != snapshot['requestId']):
        raise ActuationBlocked('current_native_traffic_owner_changed')
    rows = session_rows(snapshot)
    selected = [row for row in rows if row.get('sessionKey') in request.session_keys]
    if not selected or len(selected) != len(request.session_keys) or len(set(request.session_keys)) != len(selected):
        raise ActuationBlocked('current_native_session_scope_required')
    if any(row.get('generation', snapshot['generation']) != snapshot['generation'] for row in selected):
        raise ActuationBlocked('current_native_session_generation_changed')
    values = {'round_id': snapshot['roundId'], 'generation': snapshot['generation']}
    if request.action in {'start_service_flows', 'stop_service_flows'} and registry is not None:
        if any(registry.for_session(snapshot, row).get('application_instance_id') != request.target_id for row in selected):
            raise ActuationBlocked('native_flow_application_target_changed')
    if 'expected_population_revision' in names:
        value = snapshot.get('populationRevision')
        if type(value) is not int or value < 0:
            raise ActuationBlocked('current_native_population_revision_required')
        values['expected_population_revision'] = value
    if request.action == 'bind_application_endpoint':
        if len(selected) != 1 or registry is None:
            raise ActuationBlocked('one_native_endpoint_session_required')
        row = selected[0]
        endpoint = registry.endpoint(request.parameters['application_instance_id'])
        path = registry.session(request.session_keys[0], endpoint['address'])
        if (endpoint.get('owner_id') != request.owner_id or path.get('upf_target_id') != request.parameters['upf_target_id']
                or path.get('session_key') != request.session_keys[0] or path.get('upf_epoch') != row.get('upfEpoch')):
            raise ActuationBlocked('native_endpoint_or_upf_owner_changed')
        values.update(service_type=row.get('service_type'), demand_group_id=row.get('demandGroupId'),
                      upf_epoch=path.get('upf_epoch'))
        app = registry.inspect_application(request.parameters['application_instance_id'])
        values['expected_app_version'] = app.get('version')
    if any(name not in values or values[name] is None or values[name] == '' for name in names):
        raise ActuationBlocked('current_native_parameter_evidence_missing')
    closing = reader()
    assert_anchor(closing)
    if any(closing.get(k) != snapshot.get(k) for k in ('roundId', 'generation', 'populationRevision')):
        raise ActuationBlocked('current_native_round_changed')
    if [row for row in session_rows(closing) if row.get('sessionKey') in request.session_keys] != selected:
        raise ActuationBlocked('current_native_session_scope_changed')
    if ownership_reader(snapshot['roundId']) != ownership:
        raise ActuationBlocked('current_native_traffic_owner_changed')
    return replace(request, parameters=dict(request.parameters) | {name: values[name] for name in names})
