"""Bounded display-only scoped capacities; never native/admission authority."""
from copy import deepcopy
import math
import re
import time


def project_scoped_traffic_profiles(value, *, now=None):
    from backend.onprem.capacity.typed_web import request_scope
    from backend.onprem.scenarios.contracts import EVENT_SLICES
    now = time.time() if now is None else now
    if not isinstance(value, list) or len(value) != 1:
        return []
    row = value[0]
    expected = {'schemaVersion', 'profileId', 'verifiedDevices', 'scope', 'validUntil',
                'policyWritesAllowed', 'uePduReady', 'evidenceId'}
    if (not isinstance(row, dict) or set(row) != expected
            or row['schemaVersion'] != 'cityverse.scoped-traffic-capabilities/v1'
            or row['profileId'] != 'web_browsing' or type(row['verifiedDevices']) is not int
            or row['verifiedDevices'] not in (0, 100)
            or row['policyWritesAllowed'] is not False or row['uePduReady'] is not False):
        return []
    scope = row['scope']
    try:
        if (not isinstance(scope, dict) or any(type(scope.get(k)) is not int for k in
                ('devices', 'pdusPerDevice', 'offeredUplinkKbpsPerDevice',
                 'offeredDownlinkKbpsPerDevice', 'topologyRevision'))):
            return []
        body = dict(requestId='display-scoped-capacity', strategy='none', trafficMode='qos',
                    baselineUes=100, events=dict.fromkeys(EVENT_SLICES, 0),
                    topologyRevision=scope['topologyRevision'], demandGroups=scope['demandGroups'],
                    serviceRequirements={'schemaVersion': 'cityverse.service-requirements/v2',
                        'services': {'web_browsing': {'traffic': {'protocol': 'udp',
                            'offeredUplinkMbpsPerUe': .1, 'offeredDownlinkMbpsPerUe': .1}}}})
        if request_scope(body) != scope:
            return []
        if row['verifiedDevices'] == 100:
            expiry = row['validUntil']
            if (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now
                    or not isinstance(row['evidenceId'], str)
                    or re.fullmatch('[0-9a-f]{64}', row['evidenceId']) is None):
                return []
        elif row['validUntil'] is not None or row['evidenceId'] is not None:
            return []
    except (KeyError, ValueError, TypeError, OverflowError):
        return []
    return [deepcopy(row)]
