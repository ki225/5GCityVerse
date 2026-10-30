from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.onprem.application.city_infra import CityInfraReader, configured_infra, project_pods
from backend.onprem.api.city_infra import CityInfraController

ROOT = Path(__file__).resolve().parents[4]


def inventory():
    return {'items': [{'metadata': {'name': 'upf-embb-abc', 'namespace': 'lab', 'uid': 'uid-1',
        'labels': {'app.kubernetes.io/name': 'upf-embb', 'cityverse.io/site': 'central'}},
        'spec': {'nodeName': 'node-1', 'containers': [{'name': 'upf', 'image': 'upf@sha256:example',
            'env': [{'name': 'PRIVATE', 'value': 'SENSITIVE_ENV_MARKER'}],
            'resources': {'requests': {'cpu': '300m', 'memory': '256Mi'}, 'limits': {'cpu': '2'}}}]},
        'status': {'phase': 'Running', 'podIP': '10.0.0.1', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                   'containerStatuses': [{'name': 'upf', 'ready': True, 'restartCount': 3}]}}]}


def test_frontend_configuration_matches_current_helm_values():
    assert configured_infra() == json.loads((ROOT / 'frontend/src/city-map/data/infra-config.json').read_text())


def test_actual_pod_resources_and_site_metadata_override_config_without_leaking_env():
    rows = project_pods(inventory(), configured_infra(), 'lab')
    assert rows[0]['siteId'] == 'central'  # configured eMBB is at distributed-north
    assert rows[0]['siteBinding'] == 'pod-metadata'
    assert rows[0]['ready'] and rows[0]['containers'][0]['restarts'] == 3
    assert rows[0]['containers'][0]['requests'] == {'cpu': '300m', 'memory': '256Mi'}
    assert rows[0]['containers'][0]['limits']['memory'] is None
    assert 'SENSITIVE_ENV_MARKER' not in json.dumps(rows)
    fallback = inventory()
    fallback['items'][0]['metadata']['labels'].pop('cityverse.io/site')
    row = project_pods(fallback, configured_infra(), 'lab')[0]
    assert (row['siteId'], row['siteBinding']) == ('distributed-north', 'configured-workload')


def test_partial_or_foreign_inventory_is_rejected():
    data = inventory()
    data['metadata'] = {'continue': 'next-page'}
    with pytest.raises(ValueError):
        project_pods(data, configured_infra(), 'lab')
    with pytest.raises(ValueError):
        project_pods(inventory(), configured_infra(), 'other-namespace')


def test_endpoint_coalesces_reads_and_failures_never_become_empty_or_stale_pods():
    calls = []
    def read():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError('SENSITIVE_ENV_MARKER')
        return deepcopy(inventory())
    reader = CityInfraReader(read, 'lab')
    controller = CityInfraController(SimpleNamespace())
    controller.reader = reader
    app = Starlette(routes=[Route('/api/onprem/city-infra', controller.status)])
    with TestClient(app) as client:
        assert client.get('/api/onprem/city-infra?namespace=other').status_code == 400
        first = client.get('/api/onprem/city-infra')
        assert first.status_code == 200 and first.json()['usageAvailable'] is False
        assert client.get('/api/onprem/city-infra').json() == first.json()
        assert len(calls) == 1
        reader.expires = 0
        failed = client.get('/api/onprem/city-infra')
        assert failed.status_code == 503
        assert failed.json() == {'error': 'INFRA_OBSERVATION_UNAVAILABLE'}


def test_backend_without_a_cluster_returns_unavailable():
    controller = CityInfraController(SimpleNamespace(ue_controller=SimpleNamespace(driver=None)))
    app = Starlette(routes=[Route('/api/onprem/city-infra', controller.status)])
    with TestClient(app) as client:
        assert client.get('/api/onprem/city-infra').status_code == 503
