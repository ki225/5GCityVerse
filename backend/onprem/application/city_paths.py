"""Read-only display projection; original native clocks remain authoritative."""
import hashlib
import json
import math

from backend.onprem.application.city_health import iso
from backend.onprem.runtime.current_sessions import IDENTITY_FIELDS
from backend.onprem.runtime.ue import seconds
from backend.onprem.scenarios.native_session_presentation import project_native_session_bindings
from backend.onprem.scenarios.profiles import PROFILE_SLICES


def stamp(value):
    try:
        result = value if type(value) in (int, float) else seconds(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, AttributeError):
        return None


def fresh(value, now):
    value = stamp(value)
    return value is not None and -1 <= now - value <= 15


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def actual_site(pod, node_sites, config):
    site = node_sites.get(pod.get('spec', {}).get('nodeName'))
    return site if site in {s['id'] for s in config['sites']} else None


def predates_proof(pod, observed):
    """A replaced/restarted target cannot inherit its predecessor's session proof."""
    states = pod.get('status', {}).get('containerStatuses', [])
    return bool(states) and all(stamp(c.get('state', {}).get('running', {}).get('startedAt')) is not None
        and stamp(c['state']['running']['startedAt']) <= observed for c in states)


def project_paths(raw, config, pods, node_sites, now, *, topology_revision=None):
    if not raw or not raw.get('roundId') or raw.get('stage') in ('completed', 'cleaning'):
        return [], None, 'no_active_round'
    spatial = raw.get('spatialDemand') or {}
    revision = spatial.get('topologyRevision')
    scope = {k: raw.get(k) for k in ('roundId', 'generation', 'populationRevision', 'sequence')}
    scope['populationRevision'] = raw.get('populationRevision', 0)
    scope['sequence'] = raw.get('sequence', 0)
    scope['topologyRevision'] = revision
    if revision is None or (topology_revision is not None and revision != topology_revision):
        return [], scope, 'topology_revision_unavailable_or_changed'
    groups = spatial.get('requestedGroups', raw.get('allocation', {}).get('demandGroups', []))
    if not isinstance(groups, list) or len(groups) > 100:
        return [], scope, 'invalid_demand_groups'
    resolved = {r['groupId']: r for r in (spatial.get('resolution') or {}).get('resolvedGroups', [])}
    by_uid = {p['metadata'].get('uid'): p for p in pods if not p['metadata'].get('deletionTimestamp')}
    fence = project_native_session_bindings(raw, now=now)
    bindings = fence.get('bindings', []) if fence.get('available') else []
    # Use producer-owned exact-session windows; never use wrapper totals as traffic.
    reports = [r for a in raw.get('actions', []) if a.get('kind') == 'traffic_window'
               for r in a.get('reports', [])]
    reports += [w for m in raw.get('measurements', []) for w in m.get('windows', [])]
    paths = []
    for group in groups:
        profile, gid = group.get('profileId'), group.get('groupId')
        if profile not in PROFILE_SLICES or not isinstance(gid, str):
            continue
        location = group.get('sourceLocation') or {}
        point = location.get('position') or {}
        if set(point) != {'x', 'y'} or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 24 for v in point.values()):
            continue
        members = [b for b in bindings if b.get('demandGroupId') == gid and b.get('scenario') == profile
                   and b.get('sourceLocation') == location]
        if not members:
            access = resolved.get(gid, {})
            upf = next((w for w in config['workloads'] if w['workload'] == 'upf-' + PROFILE_SLICES[profile]), {})
            segments = [dict(fromId=location.get('buildingId'), toId=access.get('gnbId'), interface='access', state='configured', observedAt=None, validUntil=None),
                        dict(fromId=access.get('gnbId'), toId=upf.get('siteId'), interface='N3', state='configured', observedAt=None, validUntil=None)]
            paths.append(dict(id=digest([scope, gid, 'configured']), demandGroupId=gid, profileId=profile,
                slice=PROFILE_SLICES[profile], sourceLocation=location, deviceCount=None, sessionCount=None,
                requestedUes=group.get('ueCount'), segments=segments, application=None, traffic=None,
                state='configured', reason='native_session_evidence_unavailable'))
            continue
        # Exact routes remain separate when a building is split across gNBs/UPFs/apps.
        aggregates = {}
        for binding in members:
            proof = binding['proof']
            radio = proof['spatialRan']
            gnb = radio.get('gnbId')
            gnb_workload = next((w for w in config['workloads'] if w['siteId'] == gnb and w['category'] == 'ran'), None)
            gnb_pod = by_uid.get(radio.get('gnbPodUid'))
            if not gnb_workload or not gnb_pod:
                continue
            labels = gnb_pod['metadata'].get('labels', {})
            if labels.get('app.kubernetes.io/name') != gnb_workload['workload']:
                continue
            upf_pods = [p for p in by_uid.values() if p['metadata'].get('labels', {}).get('app.kubernetes.io/name') == binding['upf']]
            observed = min(stamp(proof['observedAt']), stamp(radio['observedAt']), stamp(fence['observedAt']))
            upf_site = actual_site(upf_pods[0], node_sites, config) if len(upf_pods) == 1 and predates_proof(upf_pods[0], observed) else None
            segments = [dict(fromId=location.get('buildingId'), toId=gnb, interface='access', state='verified', observedAt=iso(observed), validUntil=iso(observed + 15)),
                        dict(fromId=gnb, toId=upf_site, interface='N3', state='verified' if upf_site else 'unknown', observedAt=iso(observed), validUntil=iso(observed + 15))]
            latest = {}
            for report in reports:
                owner = report.get('binding') or {}
                if (report.get('source') != 'native-iperf3-receiver' or report.get('direction') not in ('ul', 'dl')
                        or any(owner.get(k) != binding.get(k) for k in (*IDENTITY_FIELDS, 'pfcpSeid'))
                        or not fresh(report.get('finishedAt'), now) or not fresh(report.get('receiverFinishedAt'), now)
                        or type(report.get('receivedMbps')) not in (int, float) or not math.isfinite(report['receivedMbps']) or report['receivedMbps'] < 0):
                    continue
                start, end = stamp(report.get('startedAt')), stamp(report.get('finishedAt'))
                if start is None or not start < end:
                    continue
                direction = report['direction']
                if direction not in latest or end > stamp(latest[direction]['finishedAt']):
                    latest[direction] = report
            app = None
            for report in latest.values():
                try:
                    from backend.onprem.intents.evidence_contracts import PathEvidence
                    evidence = PathEvidence.from_dict(report.get('pathEvidence'))
                    app_pod = by_uid.get(evidence.application_pod_uid)
                    app_site = actual_site(app_pod, node_sites, config) if app_pod and predates_proof(app_pod, evidence.window_started_at) else None
                    if (not app_site or evidence.session_key != binding['sessionKey'] or evidence.upf_epoch != binding['upfEpoch']
                            or evidence.pfcp_seid != binding['pfcpSeid'] or evidence.round_id != raw['roundId']
                            or evidence.generation != raw['generation'] or evidence.receiver_id != report.get('receiverPodUid')
                            or evidence.application_pod_uid != report.get('receiverPodUid') or evidence.flow_id != report.get('flow_id')
                            or abs(evidence.observed_at - stamp(report['finishedAt'])) > .001):
                        continue
                    app = dict(id=evidence.application_instance_id, siteId=app_site)
                    segments.append(dict(fromId=upf_site, toId=app_site, interface='N6', state='verified' if upf_site else 'unknown',
                        observedAt=iso(evidence.observed_at), validUntil=iso(evidence.observed_at + 15)))
                    break
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            # Exact measurement intervals are part of grouping: never average unrelated windows.
            window_key = [(d, r.get('startedAt'), r.get('finishedAt')) for d, r in sorted(latest.items())]
            key = digest([gnb, binding['upf'], binding['upfEpoch'], app, window_key])
            if key not in aggregates:
                aggregates[key] = dict(id=digest([scope, gid, key]), demandGroupId=gid, profileId=profile,
                    slice=PROFILE_SLICES[profile], sourceLocation=location, requestedUes=group.get('ueCount'),
                    deviceCount=0, sessionCount=0, segments=segments, application=app,
                    state='verified' if app and upf_site else 'partial', reason='native_session_path', traffic=None, _devices=set())
            path = aggregates[key]
            for previous, incoming in zip(path['segments'], segments):
                if previous['observedAt'] and incoming['observedAt']:
                    earliest = min(stamp(previous['observedAt']), stamp(incoming['observedAt']))
                    previous.update(observedAt=iso(earliest), validUntil=iso(earliest + 15))
            path['_devices'].add(binding['deviceId'])
            path['sessionCount'] += 1
            if latest:
                if path['traffic'] is None:
                    path['traffic'] = dict(ulMbps=None, dlMbps=None, source='native-iperf3-receiver',
                        observedAt=iso(min(stamp(r['finishedAt']) for r in latest.values())),
                        validUntil=iso(min(stamp(r['finishedAt']) for r in latest.values()) + 15),
                        windowStart=iso(min(stamp(r['startedAt']) for r in latest.values())),
                        windowEnd=iso(max(stamp(r['finishedAt']) for r in latest.values())))
                for direction, r in latest.items():
                    metric = direction + 'Mbps'
                    path['traffic'][metric] = (path['traffic'][metric] or 0) + r['receivedMbps']
        for path in aggregates.values():
            path['deviceCount'] = len(path.pop('_devices'))
            paths.append(path)
    return paths, scope, 'available' if paths else 'native_path_unavailable'
