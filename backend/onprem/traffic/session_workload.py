"""Pure per-session offered demand, shared by reservation and native sampling.

Classification is supplied by a pinned service catalog. A SUPI or a logical
session name never determines an application. This describes requested traffic;
it does not certify capacity, native binding, or measured service effect.
"""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json

from .planning import _PROFILES, _apportion


SCHEMA = 'cityverse.session-workload/v2'


class SessionWorkloadError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


class SessionWorkloadCompiler:
    """Select an unambiguous commissioned scenario for each exact session.

    ``service_mapping`` has the same scenario -> service_type/region shape as
    native classification. Identity may be an admission nonce token before
    launch or a canonical SessionKey after native establishment. No synthetic
    SUPI is created, and multiple sessions belonging to one UE stay independent.
    """
    def __init__(self, service_mapping, *, functional_combined_budget_kbps=None):
        if (functional_combined_budget_kbps is not None
                and (type(functional_combined_budget_kbps) is not int
                     or not 2 <= functional_combined_budget_kbps <= 20_000
                     or functional_combined_budget_kbps % 2)):
            raise SessionWorkloadError('Invalid commissioned combined functional budget')
        self._functional_direction_budget = (functional_combined_budget_kbps // 2
                                            if functional_combined_budget_kbps is not None else 10_000)
        if (not isinstance(service_mapping, Mapping) or not service_mapping
                or len(service_mapping) > 100):
            raise SessionWorkloadError('Pinned service workload catalog is required')
        self._services = deepcopy(dict(service_mapping))
        for scenario, record in self._services.items():
            if (scenario not in _PROFILES or not isinstance(record, Mapping)
                    or not all(isinstance(record.get(k), str) and record[k]
                               for k in ('service_type', 'region'))):
                raise SessionWorkloadError('Invalid commissioned service workload catalog')

    def compile(self, rows, *, traffic_mode='functional', service_requirements=None):
        if (traffic_mode not in ('functional', 'qos') or not isinstance(rows, (list, tuple))
                or len(rows) > (100 if traffic_mode == 'qos' else 500)
                or traffic_mode == 'qos' and not rows):
            raise SessionWorkloadError('Invalid session workload population or mode')
        assignments, seen = [], set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise SessionWorkloadError('Session workload assignment must be an object')
            identity = row.get('identity')
            if (not isinstance(identity, str) or not identity or len(identity) > 4096
                    or identity in seen):
                raise SessionWorkloadError('Each session requires a distinct exact identity')
            seen.add(identity)
            matches = [name for name, record in self._services.items()
                       if record['service_type'] == row.get('service_type')
                       and record['region'] == row.get('region')
                       and _PROFILES[name][0] == row.get('slice')]
            if len(matches) != 1:
                raise SessionWorkloadError('Missing or ambiguous commissioned session workload')
            scenario = matches[0]
            assignments.append((identity, scenario, row['slice'], _PROFILES[scenario][1]))
        assignments.sort()
        if service_requirements is not None:
            return self._single_pdu_requirements(rows, assignments, service_requirements, traffic_mode)
        requested = [a[3] for a in assignments]
        budget = 400_000 if traffic_mode == 'qos' else self._functional_direction_budget
        if traffic_mode == 'qos' and sum(requested) > budget:
            raise SessionWorkloadError('QoS demand exceeds generator capacity')
        capped = sum(requested) > budget
        applied = _apportion(requested, budget) if capped else requested
        per_session = [dict(identity=identity, scenario=scenario, slice=slice_id,
                            requestedUplinkKbps=rate, requestedDownlinkKbps=rate,
                            appliedUplinkKbps=actual, appliedDownlinkKbps=actual)
                       for (identity, scenario, slice_id, rate), actual in zip(assignments, applied)]
        metadata = dict(schemaVersion=SCHEMA, profileId='session-' + traffic_mode + '-offered-load-v2',
                        trafficMode=traffic_mode, perSession=per_session,
                        budgetKbpsPerDirection=budget,
                        requestedUplinkKbps=sum(requested), requestedDownlinkKbps=sum(requested),
                        requestedCombinedKbps=2 * sum(requested),
                        appliedUplinkKbps=sum(applied), appliedDownlinkKbps=sum(applied),
                        appliedCombinedKbps=2 * sum(applied),
                        capReason='requested_per_direction_load_exceeds_budget' if capped else None,
                        qosEffectClaimed=False)
        metadata['comparisonFingerprint'] = _digest(metadata)
        return metadata

    def _single_pdu_requirements(self, rows, assignments, requirements, traffic_mode):
        """Map per-UE demand only when an exact one-UE/one-PDU bijection exists.

        Multi-PDU demand still needs a distinct per-session API contract. The
        original SLA/evaluation scopes remain unchanged in resolvedRequirements.
        This is demand arithmetic, not proof of capacity or SLA attainment.
        """
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        from backend.onprem.scenarios.contracts import SCENARIO_SLICES, ScenarioRequestError
        identifiers = [row.get('ue_id') for row in rows]
        if (any(not isinstance(value, str) or not value for value in identifiers)
                or len(set(identifiers)) != len(rows)):
            raise SessionWorkloadError('Explicit per-session service requirements required for multi-PDU or unbound UE')
        counts = {scenario: 0 for scenario in SCENARIO_SLICES}
        for _, scenario, _, _ in assignments:
            counts[scenario] += 1
        allocation = {'baselineUes': counts.pop('web_browsing'), 'events': counts}
        try:
            resolved = ServiceRequirementCompiler.compile(requirements, allocation, traffic_mode=traffic_mode)
        except ScenarioRequestError as error:
            raise SessionWorkloadError(str(error)) from None
        per_session = []
        for identity, scenario, slice_id, _ in assignments:
            service = resolved['services'][scenario]
            ul, dl = service['offeredUplinkKbpsPerUe'], service['offeredDownlinkKbpsPerUe']
            per_session.append(dict(identity=identity, scenario=scenario, slice=slice_id,
                requestedUplinkKbps=ul, requestedDownlinkKbps=dl,
                appliedUplinkKbps=ul, appliedDownlinkKbps=dl))
        total_ul = sum(row['appliedUplinkKbps'] for row in per_session)
        total_dl = sum(row['appliedDownlinkKbps'] for row in per_session)
        if max(total_ul, total_dl) > 400_000:
            raise SessionWorkloadError('QoS demand exceeds generator capacity')
        metadata = dict(schemaVersion=SCHEMA, profileId='session-service-offered-load-v2',
            trafficMode=traffic_mode, perSession=per_session, budgetKbpsPerDirection=400_000,
            requestedUplinkKbps=total_ul, requestedDownlinkKbps=total_dl,
            requestedCombinedKbps=total_ul + total_dl, appliedUplinkKbps=total_ul,
            appliedDownlinkKbps=total_dl, appliedCombinedKbps=total_ul + total_dl,
            capReason=None, qosEffectClaimed=False,
            mapping='one-native-PDU-per-UE', resolvedRequirements=resolved)
        metadata['comparisonFingerprint'] = _digest(metadata)
        return metadata


def session_rates(metadata):
    """Return directional kbps keyed by session identity after receipt checking."""
    if not isinstance(metadata, dict):
        raise SessionWorkloadError('Invalid session workload receipt')
    content = {k: v for k, v in metadata.items() if k != 'comparisonFingerprint'}
    if (metadata.get('schemaVersion') != SCHEMA
            or metadata.get('comparisonFingerprint') != _digest(content)):
        raise SessionWorkloadError('Session workload receipt changed')
    rows = metadata.get('perSession')
    if (not isinstance(rows, list) or len(rows) > 500
            or any(not isinstance(r, dict) or not isinstance(r.get('identity'), str)
                   or not r['identity'] or any(type(r.get(k)) is not int or r[k] < 1
                   for k in ('appliedUplinkKbps', 'appliedDownlinkKbps')) for r in rows)
            or len({r['identity'] for r in rows}) != len(rows)):
        raise SessionWorkloadError('Invalid directional session rates')
    return {r['identity']: {'ul': r['appliedUplinkKbps'], 'dl': r['appliedDownlinkKbps']} for r in rows}
