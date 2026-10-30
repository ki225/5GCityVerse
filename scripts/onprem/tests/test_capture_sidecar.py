"""Packaging structure only; real image/capability tests are separate."""
import copy
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = runpy.run_path(str(ROOT / 'backend/onprem/operations/deployment/capture_sidecar.py'))
add = MODULE['add_capture_sidecar']
# Explicit synthetic test digest, never a deployable image pin.
IMAGE = 'fixture.invalid/capture@sha256:' + 'a' * 64


def deployment():
    return {'metadata': {'name': 'upf-embb'}, 'spec': {'template': {'spec': {
        'containers': [{'name': 'upf-embb'}, {'name': 'upf-evidence'}], 'volumes': []}}}}


def test_scoped_private_sidecar_no_network_listener_or_privilege():
    dep = add(deployment(), IMAGE); pod = dep['spec']['template']['spec']
    sidecar = pod['containers'][-1]; init = pod['initContainers'][0]
    assert sidecar['name'] == 'policy-capture'
    assert sidecar['securityContext']['capabilities'] == {'drop': ['ALL'], 'add': ['NET_RAW']}
    assert sidecar['securityContext']['runAsUser'] == 0
    assert sidecar['securityContext']['runAsNonRoot'] is False
    assert sidecar['securityContext']['readOnlyRootFilesystem'] is True
    assert 'ports' not in sidecar and 'hostNetwork' not in pod and 'hostPID' not in pod
    assert init['securityContext']['capabilities'] == {'drop': ['ALL']}
    assert init['securityContext']['runAsUser'] == 0
    assert init['securityContext']['readOnlyRootFilesystem'] is True
    mounts = sidecar['volumeMounts']
    assert [m['subPath'] for m in mounts if not m.get('readOnly')] == ['claims']
    assert all(m['name'] == 'capture-private' for m in mounts)
    assert pod['volumes'][0]['emptyDir']['sizeLimit'] == '8Mi'
    assert 'secretKeyRef' not in str(sidecar)


@pytest.mark.parametrize('image', [None, '', 'capture:latest', 'capture:1', 'capture@sha256:bad'])
def test_missing_actual_image_digest_fails_before_manifest_mutation(image):
    dep = deployment(); before = copy.deepcopy(dep)
    with pytest.raises(ValueError): add(dep, image)
    assert dep == before


def test_non_embb_duplicate_and_missing_collector_rejected():
    for case in ('other', 'duplicate', 'missing'):
        dep = deployment()
        if case == 'other': dep['metadata']['name'] = 'upf-urllc'
        elif case == 'duplicate': dep['spec']['template']['spec']['containers'].append({'name': 'policy-capture'})
        else: dep['spec']['template']['spec']['containers'].pop()
        before = copy.deepcopy(dep)
        with pytest.raises(ValueError): add(dep, IMAGE)
        assert dep == before


def test_fixed_bootstrap_refuses_reinitialization_and_contains_no_shell():
    init = add(deployment(), IMAGE)['spec']['template']['spec']['initContainers'][0]
    assert init['command'][:2] == ['python', '-c']
    assert 'if list(p.iterdir()):raise RuntimeError' in init['command'][2]
    assert 'assert ' not in init['command'][2]
    assert 'O_EXCL' in init['command'][2] and 'O_NOFOLLOW' in init['command'][2]
    assert 'print(' not in init['command'][2]
