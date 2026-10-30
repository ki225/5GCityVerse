"""Shared Pod configuration; legacy and manager images are digest pinned."""
import os
import re
import secrets

LEGACY_TRAFFIC_IMAGE = 'networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f'
IMAGE_ENV = 'CITYVERSE_ONPREM_TRAFFIC_MANAGER_IMAGE'
PORT = 9084


def traffic_image(environment=None):
    environment = os.environ if environment is None else environment
    value = environment.get(IMAGE_ENV, LEGACY_TRAFFIC_IMAGE)
    if not isinstance(value, str) or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', value):
        raise ValueError('Traffic image must be digest pinned')
    return value


def manager_container(container, *, image=None):
    """Configure the existing traffic container, with no extra sidecar."""
    image = traffic_image() if image is None else image
    if image == LEGACY_TRAFFIC_IMAGE:
        return container
    if not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('Traffic manager image must be digest pinned')
    container['image'] = image
    container['command'] = ['/usr/local/bin/cityverse-traffic-manager']
    container['env'] = [
        {'name': 'CITYVERSE_TRAFFIC_MANAGER_TOKEN', 'value': secrets.token_hex(32)},
        *({'name': key, 'valueFrom': {'fieldRef': {'fieldPath': field}}}
          for key, field in [('POD_UID', 'metadata.uid'), ('POD_NAME', 'metadata.name'),
                             ('POD_NAMESPACE', 'metadata.namespace')]),
    ]
    container['ports'] = [{'name': 'traffic-control', 'containerPort': PORT}]
    container['readinessProbe'] = {'httpGet': {'path': '/healthz', 'port': PORT},
                                   'periodSeconds': 5, 'timeoutSeconds': 2}
    return container
