"""Read the operator's Helm city values; never accept a values path from HTTP.

Site placement and radio coverage are simulation configuration, not native
attachment or SMF selection evidence. UE positions remain request data.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import yaml

from .errors import EvidenceUnavailable


def city_values():
    path = os.environ.get('CITYVERSE_ONPREM_CITY_VALUES')
    if not path:
        raise EvidenceUnavailable('not_configured:city_helm_values_required')
    with Path(path).open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise EvidenceUnavailable('City values exceed the configured size limit')
    value = yaml.safe_load(raw)
    city = value['city']
    if city['map'] != {'widthKm': 24, 'heightKm': 24}:
        raise EvidenceUnavailable('Native city demand requires a 24 km city')
    return city


def city_registry():
    city = city_values()
    rows = []
    for gnb in city['gnbs']:
        x, y = gnb['position']
        workload = gnb['workload']
        rows.append(dict(gnbId=gnb['id'], cellId=f'cell-r{int(y // 3)}-c{int(x // 3)}',
            position={'x': x, 'y': y}, endpoint=workload, app=workload,
            container=workload, gnbName='cv-fru-' + workload.removeprefix('gnb-'),
            slices=[gnb['slice']], capacity=gnb.get('capacity', 100),
            coverageKm=gnb.get('coverageKm', 34)))
    return {'topologyRevision': city.get('topologyRevision', 1), 'gnbs': rows}


def public_city_catalog():
    from .spatial_ran import configured_registry
    registry = configured_registry()
    digest = hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()
    result = dict(source='operator-configuration', topologyRevision=registry['topologyRevision'],
        spatialRevision=digest, coordinateUnit='km', gnbs=[{
            k: deepcopy(row[k]) for k in ('gnbId', 'cellId', 'position', 'slices', 'capacity', 'coverageKm')
        } for row in registry['gnbs']], coverageModel='simulated-distance-and-count',
        nativeConnectivityVerified=False)
    if os.environ.get('CITYVERSE_ONPREM_CITY_VALUES'):
        city = city_values()
        result['sites'] = [dict(siteId=name, tier=row['tier'], positionKm=row['position'],
            relativeCapacity=row['relativeCapacity']) for name, row in city['sites'].items()]
        result['configuredSlicePaths'] = [dict(slice=name, snssai=dict(sst=row['sst'], sd=row['sd']),
            dnn='internet', upfWorkload='upf-' + name, upfSite=row['site'],
            n6Target=row['n6Subnet'] + '.20') for name, row in city['slices'].items()]
    return result


def placement_input(body, resolution=None, bindings=None):
    """Demand-only projection. It cannot authorize workloads or network writes."""
    from backend.onprem.scenarios.contracts import SCENARIO_SLICES
    from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
    from backend.onprem.traffic.planning import _PROFILES
    from .ue import SLICES
    services = (ServiceRequirementCompiler.compile(body['serviceRequirements'], body,
        traffic_mode=body.get('trafficMode'))['services'] if 'serviceRequirements' in body else {})
    resolved = {r['groupId']: r for r in (resolution or {}).get('resolvedGroups', [])}
    groups = []
    for group in body.get('demandGroups', []):
        event, count = group['profileId'], group['ueCount']
        name = SCENARIO_SLICES[event]
        service = services.get(event, {})
        ul, dl = (service.get(key, _PROFILES[event][1]) for key in
                  ('offeredUplinkKbpsPerUe', 'offeredDownlinkKbpsPerUe'))
        groups.append(dict(groupId=group['groupId'], serviceId=group.get('serviceId', event),
            profileId=event, sourceLocation=deepcopy(group['sourceLocation']), ueCount=count,
            slice=name, snssai={'sst': SLICES[name]['sst'], 'sd': SLICES[name]['sd']}, dnn='internet',
            requestedUplinkMbps=count * ul / 1000, requestedDownlinkMbps=count * dl / 1000,
            sla=deepcopy(service.get('sla')), evaluation=deepcopy(service.get('evaluation')),
            resolvedAccess=deepcopy(resolved.get(group['groupId']))))
    return dict(schemaVersion='cityverse.placement-demand/v1', groups=groups,
        topologyRevision=body.get('topologyRevision'),
        spatialRevision=(resolution or {}).get('spatialRevision'),
        actualBindings=deepcopy(bindings or []),
        workloadAuthority='operator-service-bindings',
        placementExecuted=False, sessionPathVerified=False)
