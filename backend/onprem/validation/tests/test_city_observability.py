"""Read-only monitor contracts: readiness, freshness, identity joins, sanitized API."""
from copy import deepcopy
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.onprem.application.city_health import iso, project_health
from backend.onprem.application.city_paths import project_paths
from backend.onprem.application.city_observability import CityObservabilityReader
from backend.onprem.api.city_observability import CityObservabilityController

NOW = 1800000000
CONFIG = {'sites': [dict(id='gnb-0-0', tier='ran'), dict(id='distributed', tier='distributed'),
                    dict(id='central', tier='central')], 'workloads': [
    dict(workload='gnb-embb', function='gNB', siteId='gnb-0-0', category='ran'),
    dict(workload='upf-embb', function='UPF', siteId='distributed', category='nf')]}


def pod(app, uid, *, ready=True):
    return dict(kind='Pod', metadata=dict(name=app + '-123', uid=uid, namespace='lab',
                labels={'app.kubernetes.io/name': app}), spec=dict(nodeName='node', containers=[{'env': [{'value': 'PRIVATE_MARKER'}]}]),
                status=dict(phase='Running', conditions=[dict(type='Ready', status='True' if ready else 'False')],
                    containerStatuses=[dict(state={'running': {'startedAt': iso(NOW - 60)}})]))


def deployment(app, replicas=1):
    return dict(kind='Deployment', metadata=dict(name=app, namespace='lab', generation=2),
        spec=dict(replicas=replicas, selector={'matchLabels': {'app.kubernetes.io/name': app}},
            template=dict(metadata={'labels': {'app.kubernetes.io/name': app}}, spec={})),
        status=dict(observedGeneration=2, updatedReplicas=replicas))


def test_health_uses_desired_replicas_including_missing_workloads():
    items = [deployment('gnb-embb'), pod('gnb-embb', 'g'), deployment('upf-embb', 2), pod('upf-embb', 'u')]
    sites, _, _ = project_health({'items': items}, None, CONFIG, 'lab', NOW)
    assert sites[0]['health'] == 'healthy'
    assert sites[1]['health'] == 'degraded'
    assert (sites[1]['readyReplicas'], sites[1]['desiredReplicas']) == (1, 2)
    assert sites[2]['health'] == 'reserved'
    assert 'PRIVATE_MARKER' not in str(sites)
    sites, _, _ = project_health({'items': items[:2]}, None, CONFIG, 'lab', NOW)
    assert sites[1]['health'] == 'error' and sites[1]['reason'] == 'configured_workload_missing'


def test_zero_replicas_and_unobserved_rollout_are_not_green():
    item = deployment('upf-embb', 0)
    sites, _, _ = project_health({'items': [item]}, None, CONFIG, 'lab', NOW)
    assert sites[1]['health'] == 'inactive'
    item['spec']['replicas'] = 1
    item['status']['observedGeneration'] = 1
    sites, _, _ = project_health({'items': [item, pod('upf-embb', 'u')]}, None, CONFIG, 'lab', NOW)
    assert sites[1]['health'] == 'degraded'


def raw_round():
    return dict(roundId='r', generation=1, populationRevision=0, sequence=1, stage='measuring_baseline',
        spatialDemand=dict(topologyRevision=1, requestedGroups=[dict(groupId='home', profileId='web_browsing', ueCount=2,
            sourceLocation=dict(type='point', buildingId='home-0', position={'x': 2.2, 'y': 2.2}))],
            resolution={'resolvedGroups': [dict(groupId='home', gnbId='gnb-0-0')]}), actions=[], measurements=[])


def test_missing_or_expired_native_fence_only_yields_configured_route():
    paths, scope, _ = project_paths(raw_round(), CONFIG, [], {}, NOW, topology_revision=1)
    assert scope['roundId'] == 'r' and paths[0]['state'] == 'configured'
    assert paths[0]['traffic'] is None and paths[0]['deviceCount'] is None
    assert all(s['state'] == 'configured' and s['observedAt'] is None for s in paths[0]['segments'])
    assert project_paths(raw_round(), CONFIG, [], {}, NOW, topology_revision=2)[0] == []
    ended = raw_round(); ended['stage'] = 'completed'
    assert project_paths(ended, CONFIG, [], {}, NOW)[0] == []


def native_fixture(monkeypatch, count=2):
    import backend.onprem.application.city_paths as module
    from backend.onprem.runtime.current_sessions import IDENTITY_FIELDS
    raw = raw_round()
    bindings = []
    for n in range(count):
        b = {k: f'{k}-{n}' for k in IDENTITY_FIELDS}
        b.update(sessionKey=f'["scope","supi-{n}",1,"epoch"]', deviceId=f'd-{n}', pfcpSeid='123',
            upf='upf-embb', upfEpoch='kernel-epoch', scenario='web_browsing', demandGroupId='home',
            sourceLocation=deepcopy(raw['spatialDemand']['requestedGroups'][0]['sourceLocation']))
        b['proof'] = dict(observedAt=iso(NOW - 1), spatialRan=dict(gnbId='gnb-0-0', gnbPodUid='g', observedAt=iso(NOW - 2)))
        bindings.append(b)
    # Unit seam: the existing strict producer-fence validator owns validation;
    # these tests independently exercise downstream Pod/receiver joins.
    monkeypatch.setattr(module, 'project_native_session_bindings', lambda raw, now: dict(available=True, observedAt=iso(NOW - 1), bindings=bindings))
    return raw, bindings, [pod('gnb-embb', 'g'), pod('upf-embb', 'u')]


def test_native_route_joins_all_ues_and_exact_receiver_windows(monkeypatch):
    raw, bindings, pods = native_fixture(monkeypatch)
    reports = [dict(source='native-iperf3-receiver', direction=d, binding={**b},
        startedAt=NOW - 6, finishedAt=NOW - 1, receiverFinishedAt=NOW - 1, receivedMbps=2)
        for b in bindings for d in ('ul', 'dl')]
    raw['actions'] = [dict(kind='traffic_window', reports=reports)]
    paths, _, _ = project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)
    assert len(paths) == 1
    assert (paths[0]['deviceCount'], paths[0]['sessionCount']) == (2, 2)
    assert paths[0]['traffic']['ulMbps'] == 4 and paths[0]['traffic']['dlMbps'] == 4
    assert paths[0]['state'] == 'partial' and paths[0]['application'] is None
    assert paths[0]['segments'][1]['toId'] == 'distributed'
    assert 'supi-' not in str(paths) and 'kernel-epoch' not in str(paths)
    # Fresh wrappers cannot resurrect old or foreign receiver observations.
    for r in reports:
        r['receiverFinishedAt'] = NOW - 60
    paths, _, _ = project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)
    assert paths[0]['traffic'] is None


def test_replaced_gnb_or_unbound_upf_does_not_inherit_path_proof(monkeypatch):
    raw, _, pods = native_fixture(monkeypatch)
    pods[0]['metadata']['uid'] = 'replacement'
    assert project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)[0] == []
    pods[0]['metadata']['uid'] = 'g'
    paths, _, _ = project_paths(raw, CONFIG, pods, {}, NOW)
    assert paths[0]['segments'][1]['state'] == 'unknown'


def test_n6_requires_exact_current_application_pod_and_receiver_proof(monkeypatch):
    from backend.onprem.intents.evidence_contracts import PathEvidence
    raw, bindings, pods = native_fixture(monkeypatch, count=1)
    b = bindings[0]
    pods.append(pod('app', 'app-uid'))
    proof = PathEvidence(evidence_id='e', intent_id='i', operation_id='op', round_id='r', generation=1,
        session_key=b['sessionKey'], upf_target_id='upf-embb', upf_epoch=b['upfEpoch'], pfcp_seid=b['pfcpSeid'],
        pdr_ids=(1,), far_ids=(2,), application_instance_id='app', application_pod_uid='app-uid',
        endpoint_revision='rev', flow_id='flow', receiver_id='app-uid', native_evidence_refs=('forwarding', 'receiver'),
        window_started_at=NOW-6, observed_at=NOW-1)
    report = dict(source='native-iperf3-receiver', direction='ul', binding={**b},
        startedAt=NOW-6, finishedAt=NOW-1, receiverFinishedAt=NOW-1, receivedMbps=2,
        receiverPodUid='app-uid', flow_id='flow', pathEvidence=proof.to_dict())
    raw['actions'] = [dict(kind='traffic_window', reports=[report])]
    paths, _, _ = project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)
    assert paths[0]['state'] == 'verified' and paths[0]['segments'][-1]['interface'] == 'N6'
    report['pathEvidence']['generation'] = 2
    paths, _, _ = project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)
    assert paths[0]['application'] is None and paths[0]['state'] == 'partial'
    pods[1]['status']['containerStatuses'][0]['state']['running']['startedAt'] = iso(NOW)
    paths, _, _ = project_paths(raw, CONFIG, pods, {'node': 'distributed'}, NOW)
    assert paths[0]['segments'][1]['state'] == 'unknown'


def test_endpoint_coalesces_and_keeps_original_clocks_and_sanitizes_failures():
    raw = raw_round()
    context = SimpleNamespace(ue_controller=SimpleNamespace(driver=SimpleNamespace(namespace='lab')),
        round_ledger=SimpleNamespace(live_snapshot=lambda: deepcopy(raw), control_snapshot=lambda: deepcopy(raw)))
    calls = []
    def inventory():
        calls.append(1)
        return {'items': [deployment('gnb-embb'), pod('gnb-embb', 'g'), deployment('upf-embb'), pod('upf-embb', 'u')]}
    reader = CityObservabilityReader(context, inventory_reader=inventory, nodes_reader=lambda: {'items': []}, config=CONFIG, topology_revision=1, clock=lambda: NOW)
    controller = CityObservabilityController(context); controller.reader = reader
    app = Starlette(routes=[Route('/api/onprem/city-observability', controller.status)])
    with TestClient(app) as client:
        assert client.get('/api/onprem/city-observability?namespace=other').status_code == 400
        first = client.get('/api/onprem/city-observability')
        assert first.status_code == 200 and first.headers['cache-control'] == 'no-store'
        assert client.get('/api/onprem/city-observability').json() == first.json() and len(calls) == 1
        assert 'PRIVATE_MARKER' not in first.text
        reader.expires = reader.health_expires = 0
        reader.inventory_reader = lambda: (_ for _ in ()).throw(ValueError('PRIVATE_MARKER'))
        result = client.get('/api/onprem/city-observability').json()
        assert all(s['health'] == 'unknown' for s in result['infra'])
        assert 'PRIVATE_MARKER' not in str(result)


def test_scope_change_during_projection_suppresses_paths():
    raw = raw_round()
    context = SimpleNamespace(ue_controller=SimpleNamespace(driver=SimpleNamespace(namespace='lab')),
        round_ledger=SimpleNamespace(live_snapshot=lambda: raw, control_snapshot=lambda: dict(raw, generation=2)))
    reader = CityObservabilityReader(context, inventory_reader=lambda: {'items': []}, nodes_reader=lambda: {'items': []}, config=CONFIG, topology_revision=1, clock=lambda: NOW)
    assert reader.snapshot()['paths'] == [] and reader.snapshot()['pathStatus'] == 'round_scope_changed'
