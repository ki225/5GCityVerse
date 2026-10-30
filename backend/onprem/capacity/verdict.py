"""Historical delivery verdict derived from raw receipts, never admission proof."""
from backend.onprem.capacity.performance import CRITERIA_ID, audit_capacity


def capacity_verdict(snapshot):
    """Audit complete historical NONE measurements independently of cleanup.

    This never renews receiver timestamps or changes execution/capability state.
    """
    result = {
        'status': 'unavailable', 'criteriaId': CRITERIA_ID,
        'scope': 'historical-bounded-offered-load-delivery',
        'reason': 'measurements_not_ready', 'flowCount': 0, 'failedFlowCount': 0,
        'failedFlows': [], 'promotionEligible': False, 'qosEffectClaimed': False,
        'provenance': {
            key: snapshot.get(key) if type(snapshot.get(key)) is expected else None
            for key, expected in (('roundId', str), ('generation', int),
                                  ('sequence', int), ('observedAt', str))},
    }
    result['provenance'].update(source='raw-native-iperf3-reports', historical=True,
                                currentNetworkStateClaimed=False)
    if snapshot.get('stage') not in ('results_ready', 'cleaning', 'completed'):
        return result
    if snapshot.get('strategy') != 'none':
        result['reason'] = 'strategy_not_supported'
        return result
    audit = audit_capacity(snapshot)
    if 'capacity_evidence_invalid_or_incomplete' in audit.errors or not audit.flows:
        result['reason'] = 'evidence_invalid_or_incomplete'
        return result
    # audit_capacity accepts at most 50 UEs, two stages and two directions.
    if len(audit.flows) > 200:
        result['reason'] = 'evidence_invalid_or_incomplete'
        return result
    failed = [flow for flow in audit.flows if not flow.passed]
    result.update(status='passed' if audit.passed else 'failed',
                  reason='delivery_criteria_met' if audit.passed else 'delivery_criteria_not_met',
                  flowCount=len(audit.flows), failedFlowCount=len(failed),
                  failedFlows=[{
                      'stage': flow.stage, 'supi': flow.supi, 'direction': flow.direction,
                      'appliedKbps': flow.applied_kbps,
                      'offeredAppliedRatio': flow.offered_applied_ratio,
                      'receivedAppliedRatio': flow.received_applied_ratio,
                      'conservativeUniqueAppliedRatio': flow.conservative_unique_applied_ratio,
                      'receiverLossPercent': flow.reported_loss_percent,
                  } for flow in failed])
    return result
