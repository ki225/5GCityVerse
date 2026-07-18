"""
NEF QoS Subscription Tool Lambda  (fn-nef-qos-subscription)

Subscribes to QoS for a session — used to request URLLC priority.
Maps to 3GPP TS 29.522 §4.2.2 AS Session with QoS API.

API: POST /3gpp-as-session-with-qos/v1/{afId}/subscriptions
"""

import json
import os
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlparse
from datetime import datetime, timezone

NEF_BASE_URL = os.environ.get('NEF_BASE_URL', '').rstrip('/')
AF_ID        = os.environ.get('NEF_AF_ID', 'cityverse-af')
FREE5GC_WEBUI_URL = os.environ.get('FREE5GC_WEBUI_URL', '').rstrip('/')
FREE5GC_WEBUI_USERNAME = os.environ.get('FREE5GC_WEBUI_USERNAME', 'admin')
FREE5GC_WEBUI_PASSWORD = os.environ.get('FREE5GC_WEBUI_PASSWORD', 'free5gc')

# QoS profile per event type
QOS_PROFILES = {
    'medical': {
        'qosReference': 'URLLC_MEDICAL',
        '5qi':          1,
        'dnn':          'emergency',
        'slice_sd':     '000002',
        'maxbrUl':      '10 Mbps',
        'maxbrDl':      '10 Mbps',
        'priorityLevel': 1,
    },
    'concert': {
        'qosReference': 'eMBB_HIGH',
        '5qi':          9,
        'dnn':          'citizen',
        'slice_sd':     '000001',
        'maxbrUl':      '100 Mbps',
        'maxbrDl':      '1 Gbps',
        'priorityLevel': 5,
    },
    'typhoon': {
        'qosReference': 'URLLC_EMERGENCY',
        '5qi':          2,
        'dnn':          'emergency',
        'slice_sd':     '000003',
        'maxbrUl':      '5 Mbps',
        'maxbrDl':      '5 Mbps',
        'priorityLevel': 1,
    },
    'default': {
        'qosReference': 'DEFAULT',
        '5qi':          9,
        'dnn':          'citizen',
        'slice_sd':     '000001',
        'maxbrUl':      '50 Mbps',
        'maxbrDl':      '50 Mbps',
        'priorityLevel': 8,
    },
}

# Representative subscribers are provisioned by the same EVENT_CONFIG used by
# the runtime.  WebUI's /api/profile endpoint is optional and may be empty even
# when subscriber-level SessionManagementSubscriptionData is active.
EVENT_SUBSCRIBERS = {
    'concert': 'imsi-208930000000004',
    'medical': 'imsi-208930000000002',
    'typhoon': 'imsi-208930000000010',
    'accident': 'imsi-208930000000003',
    'iot_surge': 'imsi-208930000000100',
    'default': 'imsi-208930000000001',
}
FREE5GC_PLMN_ID = os.environ.get('FREE5GC_PLMN_ID', '20893')


def lambda_handler(event: dict, _context) -> dict:
    params      = _extract_params(event)
    event_type  = params.get('event_type', 'default')
    ue_ip       = params.get('ue_ipv4', '10.0.0.1')

    profile = QOS_PROFILES.get(event_type, QOS_PROFILES['default'])
    dnn = params.get('dnn') or profile.get('dnn') or 'internet'
    slice_sst = _int_param(params.get('slice_sst'), 2)
    slice_sd = str(params.get('slice_sd') or profile.get('slice_sd') or '000002')
    five_qi = _int_param(params.get('five_qi'), profile['5qi'])

    # TS 29.522 §5.1.4 AsSessionWithQoS_Create
    body = {
        'ipv4Addr':     ue_ip,
        'dnn':          dnn,
        'snssai':       {'sst': slice_sst, 'sd': slice_sd},
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

    configured = _nef_endpoint_configured(NEF_BASE_URL)
    url = f'{NEF_BASE_URL}/3gpp-as-session-with-qos/v1/{AF_ID}/subscriptions' if configured else ''
    status, response_body = _http_post(url, body) if configured else (
        503,
        {'error': 'NEF_BASE_URL is not a resolved private endpoint'},
    )
    unsupported = status in (404, 405, 501)
    actual_qos_state = _free5gc_qos_state(event_type)
    profile_ready = bool(
        (actual_qos_state.get('profileFound') or actual_qos_state.get('subscriberFound'))
        and (actual_qos_state.get('qosFlows') or actual_qos_state.get('sessionQosProfiles'))
    )
    compensated = unsupported and profile_ready

    success = status in (200, 201) or compensated
    result = {
        'success':      success,
        'http_status':  200 if compensated else status,
        'native_http_status': status,
        'qos_reference': profile['qosReference'],
        'priority_level': profile['priorityLevel'],
        'five_qi':       five_qi,
        'dnn':           dnn,
        'snssai':        {'sst': slice_sst, 'sd': slice_sd},
        'event_type':   event_type,
        'api_endpoint': url,
        'native_response': response_body,
        'free5gc_profile': actual_qos_state,
        'mode':         'nef-native' if status in (200, 201) else 'free5gc-profile-qos',
        'compensated':  compensated,
        'reason': (
            'free5GC NEF v4.2.2 does not expose AS Session with QoS; '
            'QoS configured at SM-policy/profile level; user-plane QER enforcement not verified (no PFCP evidence).'
        ) if compensated else '',
        'timestamp':    datetime.now(timezone.utc).isoformat(),
    }

    return _bedrock_response(event, result)


def _extract_params(event: dict) -> dict:
    params = {}
    for p in event.get('parameters', []):
        params[p['name']] = p['value']
    return params


def _int_param(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nef_endpoint_configured(base_url: str) -> bool:
    parsed = urlparse(base_url)
    hostname = (parsed.hostname or '').lower()
    return bool(
        parsed.scheme in ('http', 'https')
        and hostname
        and not hostname.endswith('.cluster.local')
        and not hostname.endswith('.invalid')
        and hostname not in {'localhost', 'placeholder'}
    )


def _http_post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {'body': raw}
        return e.code, body
    except Exception as exc:
        return 503, {'error': str(exc)}


def _free5gc_qos_state(event_type: str) -> dict:
    if not FREE5GC_WEBUI_URL:
        return {
            'source': 'free5GC WebUI API',
            'available': False,
            'profileFound': False,
            'error': 'FREE5GC_WEBUI_URL is not configured',
        }

    profile_name = f'5GCityVerse-{event_type}'
    try:
        token = _webui_login()
        status, profiles = _webui_request('GET', '/api/profile', token)
        if status >= 300 or not isinstance(profiles, list):
            return {
                'source': 'free5GC WebUI API /api/profile',
                'available': False,
                'profileFound': False,
                'httpStatus': status,
                'response': profiles,
            }

        profile = None
        profile_found = False
        for item in profiles:
            if isinstance(item, str) and item == profile_name:
                profile_found = True
                detail_status, detail = _webui_request('GET', f'/api/profile/{profile_name}', token)
                profile = detail if detail_status < 300 and isinstance(detail, dict) else None
                break
            if isinstance(item, dict) and item.get('profileName') == profile_name:
                profile_found = True
                profile = item
                break
        qos_flows = profile.get('QosFlows', []) if profile else []
        flow_rules = profile.get('FlowRules', []) if profile else []
        session_qos_profiles = []
        for session in (profile.get('SessionManagementSubscriptionData', []) if profile else []):
            if not isinstance(session, dict):
                continue
            for dnn_config in (session.get('dnnConfigurations') or {}).values():
                if isinstance(dnn_config, dict) and dnn_config.get('5gQosProfile'):
                    session_qos_profiles.append(dnn_config['5gQosProfile'])
        state = {
            'source': 'free5GC WebUI API /api/profile',
            'available': True,
            'profileName': profile_name,
            'profileFound': profile_found,
            'qosFlows': qos_flows,
            'flowRules': flow_rules,
            'sessionQosProfiles': session_qos_profiles,
        }
        if profile_found and (qos_flows or session_qos_profiles):
            return state

        ue_id = EVENT_SUBSCRIBERS.get(event_type, EVENT_SUBSCRIBERS['default'])
        detail_status, subscriber = _webui_request(
            'GET', f'/api/subscriber/{ue_id}/{FREE5GC_PLMN_ID}', token
        )
        if detail_status >= 300 or not isinstance(subscriber, dict):
            state.update({
                'subscriberFound': False,
                'subscriberHttpStatus': detail_status,
            })
            return state

        subscriber_qos_profiles = []
        for session in subscriber.get('SessionManagementSubscriptionData', []):
            if not isinstance(session, dict):
                continue
            for dnn_config in (session.get('dnnConfigurations') or {}).values():
                if isinstance(dnn_config, dict) and dnn_config.get('5gQosProfile'):
                    subscriber_qos_profiles.append(dnn_config['5gQosProfile'])
        state.update({
            'source': 'free5GC WebUI API /api/subscriber/{ueId}/{plmnId}',
            'subscriberFound': bool(subscriber.get('ueId')),
            'sessionQosProfiles': subscriber_qos_profiles,
        })
        return state
    except Exception as exc:
        return {
            'source': 'free5GC WebUI API /api/profile',
            'available': False,
            'profileFound': False,
            'profileName': profile_name,
            'error': str(exc),
        }


def _webui_login() -> str:
    status, data = _webui_request(
        'POST',
        '/api/login',
        '',
        {'username': FREE5GC_WEBUI_USERNAME, 'password': FREE5GC_WEBUI_PASSWORD},
    )
    token = data.get('access_token') or data.get('token') if isinstance(data, dict) else None
    if status >= 300 or not token:
        raise RuntimeError(f'free5GC login failed: HTTP {status} {data}')
    return token


def _webui_request(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, object]:
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Token'] = token
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(f'{FREE5GC_WEBUI_URL}{path}', data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {'body': raw}
        return exc.code, parsed


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
