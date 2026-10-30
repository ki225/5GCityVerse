"""Operator commissioned RAN geography, joined to current native gNB evidence.

The registry is configuration, never attachment evidence. Native UE membership
and reciprocal SCTP associations are read again before traffic is admitted.
"""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time

from .ue import EvidenceUnavailable, SLICES, seconds, timestamp


def configured_registry():
    path = os.environ.get('CITYVERSE_ONPREM_SPATIAL_RAN_CONFIG')
    if path:
        return validate_registry(json.loads(Path(path).read_text(encoding='utf-8')))
    from .city_catalog import city_registry
    return validate_registry(city_registry())


def validate_registry(value):
    if (not isinstance(value, dict) or type(value.get('topologyRevision')) is not int
            or value['topologyRevision'] < 1 or not isinstance(value.get('gnbs'), list) or not value['gnbs']):
        raise EvidenceUnavailable('Invalid commissioned gNB registry')
    ids = set()
    for row in value['gnbs']:
        if (not isinstance(row, dict) or not isinstance(row.get('gnbId'), str)
                or row['gnbId'] in ids or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', row['gnbId'])
                or not re.fullmatch(r'cell-r[0-7]-c[0-7]', row.get('cellId', ''))
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', row.get('endpoint', ''))
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', row.get('app', ''))
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', row.get('container', ''))
                or not isinstance(row.get('gnbName'), str) or not row['gnbName']
                or not isinstance(row.get('slices'), list) or len(row['slices']) != 1
                or row['slices'][0] not in SLICES
                or type(row.get('capacity')) is not int or row['capacity'] < 1
                or type(row.get('coverageKm')) not in (int, float) or not math.isfinite(row['coverageKm']) or row['coverageKm'] <= 0):
            raise EvidenceUnavailable('Invalid commissioned gNB record')
        match = re.fullmatch(r'cell-r([0-7])-c([0-7])', row['cellId'])
        position = row.get('position')
        if (not isinstance(position, dict) or set(position) != {'x', 'y'}
                or any(type(position[k]) not in (int, float) or not math.isfinite(position[k])
                       or not 0 <= position[k] < 24 for k in ('x', 'y'))
                or int(position['x'] // 3) != int(match[2])
                or int(position['y'] // 3) != int(match[1])):
            raise EvidenceUnavailable('Commissioned gNB position must belong to city cell')
        ids.add(row['gnbId'])
    return deepcopy(value)


def _incarnation(pod, container, namespace):
    meta, status = pod.get('metadata', {}), pod.get('status', {})
    rows = [r for r in status.get('containerStatuses', []) if r['name'] == container]
    if (meta.get('namespace') != namespace or not meta.get('uid') or meta.get('deletionTimestamp')
            or status.get('phase') != 'Running' or len(rows) != 1 or rows[0].get('ready') is not True
            or not rows[0].get('containerID') or not rows[0].get('imageID')
            or type(rows[0].get('restartCount')) is not int):
        raise EvidenceUnavailable('Current commissioned RAN incarnation unavailable')
    return (meta['uid'], rows[0]['containerID'], rows[0]['imageID'], rows[0]['restartCount'])


def native_readouts(driver, registry, *, pods=None):
    """Read existing free-ran-ue evidence sidecars; no deployment or reset."""
    from backend.onprem.evidence.collector import sctp_associations, canonical_fru_imsi
    if getattr(driver, 'backend', None) != 'free-ran-ue':
        raise EvidenceUnavailable('not_configured:spatial_native_ran_reader_requires_free_ran_ue')
    pods = driver.read_pods()['items'] if pods is None else pods
    reader = getattr(driver, '_metadata_pod_reader', None)
    services = (reader.services(deadline=time.monotonic() + 5) if reader is not None
                else driver._read_kubectl('get', 'services', '-o', 'json'))['items']
    def one(app):
        found = [p for p in pods if p.get('metadata', {}).get('labels', {}).get('app.kubernetes.io/name') == app]
        if len(found) != 1:
            raise EvidenceUnavailable('Unique commissioned gNB or AMF Pod unavailable')
        return found[0]
    def read(pod):
        if getattr(driver, 'ran_evidence_transport', None) == 'pod-http':
            from backend.onprem.evidence.ran_http import RanEvidenceClient
            data = json.loads(RanEvidenceClient().read(pod, time.monotonic() + 10))
        else:
            data = driver._read_kubectl('get', '--raw', f"/api/v1/namespaces/{driver.namespace}/pods/{pod['metadata']['name']}:9083/proxy/v1/evidence")
        if (data.get('schemaVersion') != 'cityverse.ran-evidence/v1'
                or not -1 <= time.time() - seconds(data.get('observedAt')) <= 15):
            raise EvidenceUnavailable('Commissioned RAN evidence unavailable or stale')
        return data
    amf = one('free5gc-amf')
    amf_id = _incarnation(amf, 'amf', driver.namespace)
    amf_data = read(amf)
    if amf_data.get('role') != 'amf':
        raise EvidenceUnavailable('Commissioned AMF evidence role mismatch')
    def read_gnb(row):
        if row['endpoint'] not in {row['endpoint'].split('.')[0],
                row['endpoint'].split('.')[0] + '.' + driver.namespace + '.svc.cluster.local'}:
            raise EvidenceUnavailable('Commissioned gNB endpoint must belong to native namespace')
        pod = one(row['app'])
        incarnation = _incarnation(pod, row['container'], driver.namespace)
        data = read(pod)
        cfg, info = data['config'], data['info']['gnbInfo']
        sl = SLICES[row['slices'][0]]
        if (data.get('role') != 'gnb' or cfg['gnbName'] != row['gnbName']
                or info.get('gnbName') != row['gnbName'] or info.get('plmnId') != '20893'
                or info.get('snssai') != {'sst': str(sl['sst']), 'sd': sl['sd']}):
            raise EvidenceUnavailable('Commissioned gNB native identity mismatch')
        # Resolve the very endpoint passed to the UE from within the same RAN
        # namespace. This proves the registry points to this exact native Pod.
        endpoints = [s for s in services if s.get('metadata', {}).get('name') == row['endpoint'].split('.')[0]]
        if len(endpoints) != 1:
            raise EvidenceUnavailable('Unique commissioned gNB Service unavailable')
        endpoint = endpoints[0]
        selector = endpoint.get('spec', {}).get('selector', {})
        if not selector or not all(pod['metadata'].get('labels', {}).get(k) == v for k, v in selector.items()):
            raise EvidenceUnavailable('Commissioned gNB endpoint does not select native Pod')
        local, remote = str(ipaddress.IPv4Address(cfg['ranN2Ip'])), str(ipaddress.IPv4Address(cfg['amfN2Ip']))
        associations = [r for r in sctp_associations(data['sctp']) if r['state'] == 3
                        and r['local'] == [local] and r['remote'] == [remote] and r['remotePort'] == cfg['amfN2Port']]
        if len(associations) != 1:
            raise EvidenceUnavailable('Commissioned gNB NGAP association unavailable')
        association = associations[0]
        reciprocal = [r for r in sctp_associations(amf_data['sctp']) if r['state'] == 3
                      and r['local'] == [remote] and r['remote'] == [local]
                      and r['localPort'] == cfg['amfN2Port'] and r['remotePort'] == association['localPort']]
        if len(reciprocal) != 1:
            raise EvidenceUnavailable('Commissioned gNB reciprocal AMF association unavailable')
        supis = ['imsi-' + canonical_fru_imsi(r.get('imsi')) for r in info['ranUeList']]
        if len(supis) != len(set(supis)):
            raise EvidenceUnavailable('Commissioned gNB duplicate UE contexts')
        def identity(value):
            return dict(zip(('podUid', 'containerId', 'imageId', 'restartCount'), value))
        return row['gnbId'], {'gnbId': row['gnbId'], 'cellId': row['cellId'], 'endpoint': row['endpoint'],
            'supis': supis, 'podUid': incarnation[0], 'observedAt': data['observedAt'], 'ngapAssociated': True,
            'available': True, 'source': 'free-ran-ue-api+native-ngap-sctp/v1',
            'gnbIncarnation': identity(incarnation), 'amfIncarnation': identity(amf_id),
            'peer': {'address': remote, 'port': cfg['amfN2Port'], 'gnbAddress': local,
                'gnbPort': association['localPort'], 'gnbAssociationId': association['id'],
                'amfAssociationId': reciprocal[0]['id'], 'ngSetupState': 'CONNECTED'}}
    with ThreadPoolExecutor(max_workers=min(4, len(registry['gnbs']))) as pool:
        result = dict(pool.map(read_gnb, registry['gnbs']))
    current = {p['metadata']['name']: p for p in driver.read_pods()['items']}
    if _incarnation(current.get(amf['metadata']['name'], {}), 'amf', driver.namespace) != amf_id:
        raise EvidenceUnavailable('Commissioned AMF incarnation changed')
    for row in registry['gnbs']:
        before = one(row['app'])
        if (_incarnation(current.get(before['metadata']['name'], {}), row['container'], driver.namespace)
                != _incarnation(before, row['container'], driver.namespace)):
            raise EvidenceUnavailable('Commissioned gNB incarnation changed')
    return result


def augment_context_readouts(driver, readouts):
    """Include additional commissioned gNBs in existing release/count checks."""
    if (not os.environ.get('CITYVERSE_ONPREM_SPATIAL_RAN_CONFIG')
            and not os.environ.get('CITYVERSE_ONPREM_CITY_VALUES')):
        return readouts
    registry = configured_registry()
    native = native_readouts(driver, registry)
    for name, base in readouts.items():
        records = [native[r['gnbId']] for r in registry['gnbs'] if name in r['slices']]
        base['spatialReadouts'] = records
        supis = set(base.get('supis', []))
        for proof in records:
            supis.update(proof['supis'])
        base['supis'] = sorted(supis)
        base['ueCount'] = len(supis)
    return readouts


def resolve_groups(body, registry, observations):
    if body.get('topologyRevision') != registry['topologyRevision']:
        raise EvidenceUnavailable('Spatial topology revision changed')
    from backend.onprem.scenarios.contracts import EVENT_SLICES
    selected, requested = [], {}
    for group in body['demandGroups']:
        slice_name = 'embb' if group['profileId'] == 'web_browsing' else EVENT_SLICES[group['profileId']]
        p = group['sourceLocation']['position']
        eligible = []
        for row in registry['gnbs']:
            if slice_name not in row['slices']:
                continue
            proof = observations.get(row['gnbId'], {})
            distance = math.hypot(p['x'] - row['position']['x'], p['y'] - row['position']['y'])
            if (proof.get('ngapAssociated') is True and isinstance(proof.get('podUid'), str) and proof['podUid']
                    and -1 <= time.time() - seconds(proof.get('observedAt')) <= 15
                    and distance <= row['coverageKm'] and len(proof['supis']) + requested.get(row['gnbId'], 0) + group['ueCount'] <= row['capacity']):
                eligible.append((distance, row['gnbId'], row))
        if not eligible:
            raise EvidenceUnavailable('No commissioned reachable gNB has coverage and capacity for demand')
        distance, _, row = min(eligible, key=lambda r: (r[0], r[1]))
        requested[row['gnbId']] = requested.get(row['gnbId'], 0) + group['ueCount']
        selected.append({'groupId': group['groupId'], 'gnbId': row['gnbId'], 'cellId': row['cellId'],
                         'endpoint': row['endpoint'], 'distanceKm': distance, 'gnbPodUid': observations[row['gnbId']]['podUid']})
    return {'topologyRevision': registry['topologyRevision'], 'resolvedGroups': selected}


def assign_allocation(allocation, body, resolution):
    from backend.onprem.scenarios.contracts import bind_scenarios
    rows = bind_scenarios(body, allocation)
    groups = {g['groupId']: g for g in resolution['resolvedGroups']}
    lookup = {r['supi']: r for r in rows}
    result=[]
    for item in allocation:
        member=lookup[item['supi']]
        access=groups[member['demandGroupId']]
        row=dict(item,spatialAccess=deepcopy(access),sourceLocation=deepcopy(member['sourceLocation']))
        if 'device' in item:
            row.update(demandGroupId=member['demandGroupId'],servingCellId=access['cellId'],scenario=member['scenario'])
        result.append(row)
    return result


def verify_bindings(allocation, observations):
    bindings = []
    for item in allocation:
        access = item.get('spatialAccess')
        if not access:
            raise EvidenceUnavailable('Spatial UE allocation lacks commissioned gNB binding')
        matches = [r for r in observations.values() if item['supi'] in r['supis']]
        if len(matches) != 1 or matches[0]['gnbId'] != access['gnbId']:
            raise EvidenceUnavailable('Native UE serving gNB does not match spatial allocation')
        proof = matches[0]
        if not access.get('gnbPodUid') or proof.get('podUid') != access['gnbPodUid']:
            raise EvidenceUnavailable('Native serving gNB incarnation differs from pinned allocation')
        if not -1 <= time.time() - seconds(proof.get('observedAt')) <= 15:
            raise EvidenceUnavailable('Native UE serving gNB observation expired')
        bindings.append({'supi': item['supi'], 'demandGroupId': access['groupId'], 'gnbId': proof['gnbId'],
                         'servingCellId': proof['cellId'], 'gnbPodUid': proof['podUid'], 'observedAt': proof['observedAt']})
    return {'source': 'native-free-ran-ue-context+reciprocal-ngap-sctp', 'observedAt': timestamp(), 'bindings': bindings}
