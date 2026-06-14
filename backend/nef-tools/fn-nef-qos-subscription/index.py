"""
NEF QoS Subscription Tool Lambda  (fn-nef-qos-subscription)
────────────────────────────────────────────────────────────
Subscribes to QoS for a session — used to request URLLC priority.
Maps to 3GPP TS 29.522 §4.2.2 AS Session with QoS API.

API: POST /3gpp-as-session-with-qos/v1/{afId}/subscriptions
"""

import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone

NEF_BASE_URL = os.environ.get('NEF_BASE_URL', 'http://nef.free5gc.svc.cluster.local:8000')
AF_ID        = os.environ.get('NEF_AF_ID', 'cityverse-af')

# QoS profile per event type
QOS_PROFILES = {
    'medical': {
        'qosReference': 'URLLC_MEDICAL',
        '5qi':          82,          # 3GPP: non-GBR ultra-low latency IoT
        'maxbrUl':      '10 Mbps',
        'maxbrDl':      '10 Mbps',
        'priorityLevel': 1,
    },
    'concert': {
        'qosReference': 'eMBB_HIGH',
        '5qi':          1,
        'maxbrUl':      '100 Mbps',
        'maxbrDl':      '1 Gbps',
        'priorityLevel': 5,
    },
    'typhoon': {
        'qosReference': 'URLLC_EMERGENCY',
        '5qi':          82,
        'maxbrUl':      '5 Mbps',
        'maxbrDl':      '5 Mbps',
        'priorityLevel': 1,
    },
    'default': {
        'qosReference': 'DEFAULT',
        '5qi':          9,
        'maxbrUl':      '50 Mbps',
        'maxbrDl':      '50 Mbps',
        'priorityLevel': 8,
    },
}


def lambda_handler(event: dict, _context) -> dict:
    params      = _extract_params(event)
    event_type  = params.get('event_type', 'default')
    ue_ip       = params.get('ue_ipv4', '10.0.0.1')

    profile = QOS_PROFILES.get(event_type, QOS_PROFILES['default'])

    # TS 29.522 §5.1.4 AsSessionWithQoS_Create
    body = {
        'ipv4Addr':     ue_ip,
        'dnn':          'internet',
        'snssai':       {'sst': 2, 'sd': '000002'},   # URLLC slice
        'qosReference': profile['qosReference'],
        'altQosReferences': [],
        'usageThreshold': {},
        'qosMonInfo': {
            'reqQosMonParams': ['PDU_SESSION_RELEASE'],
            'repThreshDl':     {'duration': 10, 'bytes': 0},
            'repThreshUl':     {'duration': 10, 'bytes': 0},
        },
        'notifUri': '',
        'requestTestNotification': False,
    }

    url    = f'{NEF_BASE_URL}/3gpp-as-session-with-qos/v1/{AF_ID}/subscriptions'
    status, _ = _http_post(url, body)

    result = {
        'success':      status in (200, 201),
        'http_status':  status,
        'qos_reference': profile['qosReference'],
        'priority_level': profile['priorityLevel'],
        'five_qi':       profile['5qi'],
        'event_type':   event_type,
        'api_endpoint': url,
        'timestamp':    datetime.now(timezone.utc).isoformat(),
    }

    return _bedrock_response(event, result)


def _extract_params(event: dict) -> dict:
    params = {}
    for p in event.get('parameters', []):
        params[p['name']] = p['value']
    return params


def _http_post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 503, {}


def _bedrock_response(event: dict, result: dict) -> dict:
    return {
        'messageVersion': '1.0',
        'response': {
            'actionGroup': event.get('actionGroup', ''),
            'function':    event.get('function', ''),
            'functionResponse': {
                'responseBody': {'TEXT': {'body': json.dumps(result)}}
            }
        }
    }
