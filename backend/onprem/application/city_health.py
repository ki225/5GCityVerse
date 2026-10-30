"""Bounded Kubernetes inventories -> public site workload health (no mutations)."""
from datetime import datetime, timezone


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def inventory_items(value, namespace=None):
    if (not isinstance(value, dict) or not isinstance(value.get('items'), list)
            or len(value['items']) > 4096 or value.get('metadata', {}).get('continue')):
        raise ValueError('Incomplete inventory')
    if namespace and any(p.get('metadata', {}).get('namespace') != namespace for p in value['items']):
        raise ValueError('Foreign namespace')
    return value['items']


def project_health(inventory, nodes, config, namespace, observed):
    items = inventory_items(inventory, namespace)
    site_ids = {s['id'] for s in config['sites']}
    node_sites = {n['metadata']['name']: n['metadata'].get('labels', {}).get('cityverse.io/site')
                  for n in inventory_items(nodes)} if nodes is not None else {}
    pods = [p for p in items if p.get('kind') == 'Pod']
    controllers = [p for p in items if p.get('kind') in ('Deployment', 'StatefulSet')]
    workloads, expected = [], set()
    for obj in controllers:
        meta, spec, status = (obj.get(k, {}) for k in ('metadata', 'spec', 'status'))
        template = spec.get('template', {})
        labels = template.get('metadata', {}).get('labels', {})
        app, nf = labels.get('app.kubernetes.io/name', ''), labels.get('nf', '')
        matched = next((w for w in config['workloads'] if w['workload'] in (app, meta.get('name'))
                        or w['workload'] == 'free5gc-' + nf), None)
        site = (template.get('spec', {}).get('nodeSelector', {}).get('cityverse.io/site')
                or labels.get('cityverse.io/site') or (matched or {}).get('siteId'))
        if matched and matched['category'] == 'ran':
            site = matched['siteId']
        if site not in site_ids:
            continue
        if matched:
            expected.add(matched['workload'])
        selector = spec.get('selector', {})
        match = selector.get('matchLabels', {})
        # Unknown selector expressions must never accidentally select every Pod.
        supported = bool(match) and not selector.get('matchExpressions')
        selected = [p for p in pods if supported and all(p['metadata'].get('labels', {}).get(k) == v
                    for k, v in match.items()) and not p['metadata'].get('deletionTimestamp')]
        desired = spec.get('replicas', 1)
        if type(desired) is not int or not 0 <= desired <= 10000:
            raise ValueError('Invalid replica count')
        ready = sum(any(c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in p.get('status', {}).get('conditions', [])) for p in selected)
        current = status.get('observedGeneration', -1) >= meta.get('generation', 0)
        failed = any(p.get('status', {}).get('phase') == 'Failed' for p in selected)
        if not supported:
            health, reason = 'unknown', 'unsupported_selector'
        elif desired == 0:
            health, reason = 'inactive', 'scaled_to_zero'
        elif not current:
            health, reason = 'degraded', 'rollout_pending'
        elif ready == 0:
            health, reason = 'error', 'required_replicas_unavailable'
        elif ready < desired or failed or status.get('updatedReplicas', desired) < desired:
            health, reason = 'degraded', 'replicas_not_ready'
        elif any(_recent_restart(c, observed) for p in selected
                 for c in p.get('status', {}).get('containerStatuses', [])):
            health, reason = 'degraded', 'container_restarted'
        else:
            health, reason = 'healthy', 'required_replicas_ready'
        workloads.append(dict(id=meta['name'], siteId=site,
            function=(matched or {}).get('function', nf.upper() or app or meta['name']),
            required=(matched or {}).get('function') != 'WebUI', health=health, reason=reason,
            desiredReplicas=desired, readyReplicas=ready,
            placementSource='workload-spec', observedAt=iso(observed), validUntil=iso(observed + 30)))
    for row in config['workloads']:
        if row['workload'] not in expected and row['function'] != 'WebUI':
            workloads.append(dict(id=row['workload'], function=row['function'], siteId=row['siteId'],
                required=True, health='error', reason='configured_workload_missing',
                desiredReplicas=None, readyReplicas=0, placementSource='operator-configuration',
                observedAt=iso(observed), validUntil=iso(observed + 30)))
    infra = []
    for site in config['sites']:
        rows = [r for r in workloads if r['siteId'] == site['id']]
        required = [r for r in rows if r['required']]
        health = next((s for s in ('error', 'unknown', 'degraded', 'healthy', 'inactive')
                       if any(r['health'] == s for r in required)), 'reserved')
        infra.append(dict(id=site['id'], kind=site['tier'], health=health,
            reason=next((r['reason'] for r in required if r['health'] == health), 'not_deployed'),
            desiredReplicas=(sum(r['desiredReplicas'] for r in required)
                if all(r['desiredReplicas'] is not None for r in required) else None),
            readyReplicas=sum(r['readyReplicas'] for r in required), workloads=rows,
            source='kubernetes-workload-readiness', observedAt=iso(observed), validUntil=iso(observed + 30)))
    # Private join material; only the sanitized infra projection leaves the API.
    return infra, pods, node_sites


def _recent_restart(container, observed):
    value = container.get('lastState', {}).get('terminated', {}).get('finishedAt')
    try:
        return 0 <= observed - datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() <= 300
    except (ValueError, TypeError, AttributeError):
        return False


def unknown_health(config):
    return [dict(id=s['id'], kind=s['tier'], health='unknown', reason='observation_unavailable',
                 desiredReplicas=None, readyReplicas=None, workloads=[], source='kubernetes-workload-readiness',
                 observedAt=None, validUntil=None) for s in config['sites']]
