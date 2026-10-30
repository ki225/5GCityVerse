"""Join owned scenario demand to commissioned placement workloads.

The scenario snapshot is the source of geography and native radio evidence;
the operator catalog only authorizes the UPF/application pair. Neither a model
nor a static catalog may invent current UE identities or a serving cell.
"""
from copy import deepcopy
import hashlib
import json

from .domain import ContractError
from .repository import IntentConflict


def placement_demands(snapshot, traffic, config, topology):
    spatial = snapshot.get('spatialDemand')
    if not isinstance(spatial, dict):
        raise ContractError('Current city traffic has no spatial demand evidence')
    receipt = traffic.get('receipt', {})
    if (traffic.get('native_restoration_complete') is True
            or snapshot.get('stage') in {'cleaning', 'completed'}
            or any(snapshot.get(k) != receipt.get(k) for k in ('requestId', 'roundId'))
            or (receipt.get('generation') is not None and snapshot.get('generation') != receipt['generation'])
            or type(snapshot.get('generation')) is not int):
        raise IntentConflict('Spatial demand does not belong to current intent traffic')
    if spatial.get('topologyRevision') != topology.get('radio_revision', topology['revision']):
        raise IntentConflict('Spatial demand topology changed; resolve a new scenario round')
    groups = snapshot.get('allocation', {}).get('demandGroups')
    if not isinstance(groups, list) or not groups:
        raise ContractError('Spatial demand groups are unavailable')
    bindings = spatial.get('actualBindings')
    if not isinstance(bindings, list) or not bindings:
        raise ContractError('Native spatial connectivity has not been verified')
    native = snapshot.get('connectivityEvidence', {}).get('spatialRan', {})
    if (native.get('source') not in {'native-free-ran-ue-context+reciprocal-ngap-sctp','native-ueransim-single-commissioned-gnb/v1','native-free-ran-ue-single-session/v2'}
            or native.get('generation') != snapshot['generation']
            or native.get('bindings') != bindings):
        raise ContractError('Native spatial attachment proof does not match the current generation')
    if native['source'] in {'native-ueransim-single-commissioned-gnb/v1','native-free-ran-ue-single-session/v2'}:
        from backend.onprem.runtime.current_sessions import V2,session_rows
        from backend.onprem.runtime.ue import seconds
        import time
        rows=session_rows(snapshot)
        if (snapshot.get('schemaVersion')!=V2 or native.get('roundId')!=snapshot['roundId']
                or snapshot.get('spatialRan')!=native or {r['supi'] for r in rows}!={b.get('supi') for b in bindings}
                or any(b.get('source')!=native['source'] or b.get('pdr_far_gnb_verified') is not True
                    or not b.get('gnbN3Address') or not -1<=time.time()-seconds(b.get('observedAt'))<=15
                    or not any(r['supi']==b.get('supi') and r.get('demandGroupId')==b.get('demandGroupId')
                        and r.get('servingCellId')==b.get('servingCellId') and r.get('proof',{}).get('spatialRan')==b for r in rows)
                    for b in bindings)):
            raise ContractError('Native device geographic forwarding proof does not match current sessions')
        if native['source']=='native-free-ran-ue-single-session/v2' and any(b.get('vendor')!='free-ran-ue'
                or not b.get('gnbIncarnation') or not b.get('amfIncarnation') or b.get('peer',{}).get('ngSetupState')!='CONNECTED' for b in bindings):
            raise ContractError('Native FreeRAN reciprocal attachment proof missing')
    assignments = config.get('service_bindings', {})
    if not isinstance(assignments, dict):
        raise ContractError('Invalid commissioned placement service bindings')
    cells = {r['cell_id'] for r in topology['cells'] if r['serving_enabled']}
    seen, demands = set(), []
    services = snapshot.get('resolvedRequirements', {}).get('services', {})
    from backend.onprem.traffic.planning import _PROFILES
    for group in groups:
        group_id, event = group['groupId'], group['profileId']
        rows = [r for r in bindings if r.get('demandGroupId') == group_id]
        if len(rows) != group['ueCount']:
            raise ContractError('Spatial native UE count differs from demand group')
        pair = (assignments.get(group['serviceId']) if 'serviceId' in group
                else assignments.get(group_id, assignments.get(event)))
        if not isinstance(pair, dict) or set(pair) != {'upf_workload_id', 'application_workload_id'}:
            raise ContractError('Missing commissioned workload pair for spatial demand ' + group_id)
        buckets = {}
        for row in rows:
            supi = row.get('supi')
            cell = row.get('servingCellId')
            if not supi or supi in seen or cell not in cells or not row.get('gnbId') or not row.get('gnbPodUid'):
                raise ContractError('Duplicate UE or uncommissioned native serving cell')
            seen.add(supi)
            buckets.setdefault(cell, []).append(row)
        service = services.get(event, {})
        ul = service.get('offeredUplinkKbpsPerUe', _PROFILES[event][1])
        dl = service.get('offeredDownlinkKbpsPerUe', _PROFILES[event][1])
        for cell, members in sorted(buckets.items()):
            count = len(members)
            demands.append(dict(group_id=group_id + ':' + cell, parent_group_id=group_id,
                serving_cell_id=cell, weight=count * (ul + dl) / 1000,
                requested_ue_count=count, requested_ul_mbps=count * ul / 1000,
                requested_dl_mbps=count * dl / 1000, weight_basis='requested_ul_plus_dl_mbps', **pair))
    if len(seen) != len(bindings):
        raise ContractError('Native spatial bindings include an unknown demand group')
    evidence = dict(round_id=snapshot['roundId'], generation=snapshot['generation'],
        request_id=snapshot['requestId'], topology_revision=topology['revision'],
        spatial_revision=hashlib.sha256(json.dumps(spatial, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        native_binding_source=native['source'],
        native_bindings=deepcopy(bindings))
    if 'radio_revision' in topology:
        evidence['radio_topology_revision'] = topology['radio_revision']
    return demands, evidence
