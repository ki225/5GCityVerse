"""Fail-closed contract for the proposed active-context AMF UE-AMBR OAM field.

Not wired to runtime/policy capabilities. The cached AMF patch is not yet a
verified deployed producer. This is context-observed subscribed UE-AMBR, NOT
Session-AMBR, an NGAP acknowledgement, or measured/enforced throughput.

The provider must obtain ExpectedUe before this target response through a
separately fenced observation plus owned UE/PDU and gNB evidence. The approved
same-AMF-authority model proves repeatability, not independent NGAP provenance.
It binds the request to that Pod/container and re-reads incarnation afterward.
A load-balanced URL plus matching response fields is NOT transport binding.
The existing NativeOamAdapter does not currently provide this contract.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import math
import re

SOURCE = 'amf-active-ue-context'
MAX_AGE = 15.0
SKEW = 1.0


@dataclass(frozen=True)
class AmfIncarnation:
    pod_uid: str
    container_id: str
    image_id: str
    restart_count: int
    started_at: str
    nf_instance_id: str


@dataclass(frozen=True)
class ExpectedUe:
    supi: str
    access_type: str
    amf_ue_ngap_id: str
    ran_ue_ngap_id: str
    observed_at: str
    incarnation: AmfIncarnation


@dataclass(frozen=True)
class BoundAmfRead:
    """Trusted provider envelope, never deserialized from AMF response JSON."""
    before: AmfIncarnation
    after: AmfIncarnation
    request_started_at: str
    response_received_at: str


def _seconds(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp timezone')
    return parsed.timestamp()


def _fresh(value, now):
    return -SKEW <= now - _seconds(value) <= MAX_AGE


def _ngap(value, maximum):
    return isinstance(value, str) and bool(re.fullmatch(r'0|[1-9][0-9]{0,12}', value)) and int(value) <= maximum


def _incarnation(value):
    if not isinstance(value, AmfIncarnation):
        return False
    if any(not isinstance(v, str) or not v for v in
           (value.pod_uid, value.container_id, value.image_id, value.nf_instance_id)):
        return False
    return type(value.restart_count) is int and value.restart_count >= 0 and math.isfinite(_seconds(value.started_at))


def _mbps(value):
    if not isinstance(value, str) or len(value) > 96:
        raise ValueError('bitrate')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?) (bps|Kbps|Mbps|Gbps|Tbps)', value)
    if not match:
        raise ValueError('bitrate')
    units = {'bps': Decimal('0.000001'), 'Kbps': Decimal('0.001'), 'Mbps': Decimal(1),
             'Gbps': Decimal(1000), 'Tbps': Decimal(1000000)}
    rate = float(Decimal(match[1]) * units[match[2]])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError('bitrate')
    return rate


def parse_ue_ambr(observation, *, expected=None, transport=None, now):
    """Return available only for an exact fresh binding; never synthesize rates."""
    unavailable = lambda reason: {'available': False, 'source': SOURCE, 'reason': reason,
                                  'enforcementVerified': False}
    try:
        if type(now) not in (int, float) or not math.isfinite(now):
            return unavailable('clock_unavailable')
        if not isinstance(expected, ExpectedUe) or not isinstance(transport, BoundAmfRead):
            return unavailable('transport_binding_unavailable')
        if (not all(_incarnation(value) for value in (expected.incarnation, transport.before, transport.after))
                or transport.before != expected.incarnation
                or transport.after != expected.incarnation):
            return unavailable('amf_incarnation_mismatch')
        if (not isinstance(expected.supi, str) or not re.fullmatch(r'imsi-[0-9]{5,15}', expected.supi)
                or expected.access_type != '3GPP_ACCESS'
                or not _ngap(expected.amf_ue_ngap_id, 2**40-1)
                or not _ngap(expected.ran_ue_ngap_id, 2**32-2)):
            return unavailable('ue_binding_invalid')
        start, end = _seconds(transport.request_started_at), _seconds(transport.response_received_at)
        # Binding and container start are provider-side identity observations:
        # no cross-clock skew allowance may move a binding before this epoch.
        if (start > end or _seconds(expected.incarnation.started_at) > start
                or _seconds(expected.observed_at) < _seconds(expected.incarnation.started_at)
                or not all(_fresh(t, now) for t in (transport.request_started_at,
                           transport.response_received_at, expected.observed_at))):
            return unavailable('binding_stale')
        if not isinstance(observation, dict) or observation.get('available') is not True:
            return unavailable('active_context_unavailable')
        if observation.get('source') != SOURCE:
            return unavailable('source_mismatch')
        fields = {'nfInstanceId': expected.incarnation.nf_instance_id, 'supi': expected.supi,
                  'accessType': expected.access_type, 'amfUeNgapId': expected.amf_ue_ngap_id,
                  'ranUeNgapId': expected.ran_ue_ngap_id}
        if any(observation.get(key) != value for key, value in fields.items()):
            return unavailable('ue_identity_mismatch')
        observed = _seconds(observation.get('observedAt'))
        if not _fresh(observation['observedAt'], now) or not start-SKEW <= observed <= end+SKEW:
            return unavailable('observation_stale')
        rates = {'ul': _mbps(observation.get('uplink')), 'dl': _mbps(observation.get('downlink'))}
        return {'available': True, 'source': SOURCE, 'observedAt': observation['observedAt'],
                **fields, 'ueAmbrMbps': rates, 'podUid': expected.incarnation.pod_uid,
                'containerId': expected.incarnation.container_id, 'enforcementVerified': False}
    except (ValueError, TypeError, OverflowError):
        return unavailable('malformed_observation')
