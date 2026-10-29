"""Read-only, short-lived permission to request a new baseline V2 device.

This is not UE/PDU readiness, capacity validation, or a resource reservation.
The formal admission port repeats all mutation fences before creating a hold.
"""
from __future__ import annotations

import asyncio
import time

from backend.onprem.runtime.ue import EvidenceVerifier, population_revision, ran_connected, seconds, timestamp


def empty_native_snapshot(raw, generation, now):
    """Require affirmative global absence; missing inventory fields fail closed."""
    if raw.get('schemaVersion') != 'cityverse.evidence/v1' or raw.get('generation') != generation or raw.get('ues') != []:
        return False
    sources = raw.get('sources', {})
    smf, amf, upf = [sources.get(key, {}) for key in ('smf', 'amf', 'upf')]
    if (smf.get('inventoryComplete') is not True or smf.get('schemaVersion') != 'cityverse.smf-oam/v2'
            or smf.get('consistency') != 'atomic-membership-and-projection' or not smf.get('bootEpoch')
            or type(smf.get('contextCount')) is not int or smf['contextCount'] != 0
            or type(amf.get('contextCount')) is not int or amf['contextCount'] != 0):
        return False
    inventories = upf.get('inventories', {})
    if set(inventories) != {'embb', 'urllc', 'mmtc', 'v2x'}:
        return False
    for row in [raw, smf, amf, upf, *inventories.values()]:
        if not -1 <= now - seconds(row.get('observedAt')) <= 15:
            return False
    return all(type(row.get('sessionCount')) is int and row['sessionCount'] == 0
               and type(row.get('residualCount')) is int and row['residualCount'] == 0
               and bool(row.get('epoch')) for row in inventories.values())


async def execution_admission(context, bootstrap):
    now = time.time()
    result = {'schemaVersion': 'cityverse.native-execution-admission/v1',
        'mode': 'baseline-device-reservation', 'state': 'unavailable',
        'observedAt': timestamp(now), 'validUntil': timestamp(now),
        'coreRanReady': False, 'nativeFleetEmpty': False, 'profileValid': False,
        'profileRequired': False, 'launchContractReady': False,
        'reasonCode': 'baseline_not_absent',
        'existingUePduReady': False, 'serviceEffectVerified': False}
    if bootstrap.get('state') != 'absent' or bootstrap.get('stage') != 'ue':
        return result
    try:
        service = getattr(context, 'intent_service', None)
        preparer = getattr(service, 'pre_traffic_admission', None)
        capabilities = getattr(preparer, 'capabilities', None)
        if not callable(capabilities):
            result['reasonCode'] = 'baseline_admission_not_configured'
            return result
        caps = await capabilities()
        result['profileValid'] = caps.get('profile_valid') is True  # Diagnostic only.
        if (caps.get('configured') is not True or caps.get('mode') != result['mode']
                or caps.get('profile_required') is not False
                or caps.get('launch_contract_ready') is not True
                or caps.get('launch_contract_source') != 'current-server-free-ran-config/v1'
                or caps.get('changes_selection') is not False):
            result['reasonCode'] = caps.get('reason_code') or 'current_launch_contract_unavailable'
            return result
        result['launchContractReady'] = True
        result['reasonCode'] = 'fresh_stopped_fleet_required'
        controller = context.ue_controller
        status = controller.status()
        if (status.get('status') != 'stopped' or status.get('errors') or status.get('perUe') != []
                or not -1 <= now - seconds(status.get('observedAt')) <= 15
                or any(type(status.get(key)) is not int or status[key] != 0
                       for key in ('requested', 'allocated', 'registered', 'pduEstablished', 'trafficVerified', 'failed'))):
            return result
        current = controller.store.current()
        if current is None or current.get('allocation') != [] or any(current.get('desired', {}).values()):
            return result
        if context.bootstrap_blocker() or controller.mutation_blocker({'embb': 1, 'urllc': 0, 'mmtc': 0, 'v2x': 0}):
            result['reasonCode'] = 'runtime_mutation_blocked'
            return result
        raw = await asyncio.to_thread(controller.adapter.observe, current['generation'])
        now = time.time()
        if not empty_native_snapshot(raw, current['generation'], now):
            result['reasonCode'] = 'fresh_global_empty_inventory_required'
            return result
        result['nativeFleetEmpty'] = True
        # A separate verifier cannot reset the live controller's stability clock.
        verifier = EvidenceVerifier(max_age=15, stable_seconds=0, endpoints=controller.verifier.endpoints)
        verified = verifier.evaluate(current, raw, now=now)
        if verified.get('status') != 'stopped' or verified.get('errors') or not ran_connected(verified, now):
            result['reasonCode'] = 'fresh_core_ran_evidence_required'
            return result
        result['reasonCode'] = 'commissioned_spatial_gnb_evidence_required'
        # Include every configured spatial gNB, rather than only one per slice.
        expected_endpoints = tuple(getattr(getattr(getattr(controller, 'driver', None), 'device_profile', None), 'commissioned_gnb_endpoints', ()))
        observed_endpoints = []
        if not expected_endpoints or len(set(expected_endpoints)) != len(expected_endpoints):
            return result
        for group in raw.get('gnb', {}).values():
            rows = group.get('spatialReadouts', [group])
            if not rows:
                return result
            for row in rows:
                observed_endpoints.append(row.get('endpoint'))
                if row.get('available') is not True or row.get('ngapAssociated') is not True or not -1 <= now-seconds(row.get('observedAt')) <= 15:
                    return result
        if len(observed_endpoints) != len(set(observed_endpoints)) or set(observed_endpoints) != set(expected_endpoints):
            return result
        result['coreRanReady'] = True
        result['reasonCode'] = 'native_generation_changed'
        latest = controller.store.current()
        if (latest is None or latest.get('generation') != current['generation']
                or latest.get('allocation') != [] or latest.get('desired') != current.get('desired')
                or population_revision(latest) != population_revision(current)
                or context.bootstrap_blocker()):
            return result
        # Only fresh native observations bound this short-lived readiness hint.
        until = min(now + 5,
                    *(seconds(row['observedAt']) + 15 for row in raw['sources'].values() if row.get('available') is True))
        if until <= now:
            return result
        result.update(state='ready_to_start', observedAt=timestamp(now), validUntil=timestamp(until),
            generation=current['generation'], reasonCode=None)
    except Exception:
        # Missing/stale/changed evidence is unavailable, never a native readiness claim.
        result['reasonCode'] = 'native_admission_evidence_unavailable'
    return result
