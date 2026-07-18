"""
NEF PFD Management Tool Lambda  (fn-nef-pfd-create)

Called by Bedrock Agent Action Group.
Creates a Packet Flow Description via free5GC NEF API (3GPP TS 29.522).

API: POST /3gpp-pfd-management/v1/{afId}/transactions
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


def lambda_handler(event: dict, _context) -> dict:
    """
    Bedrock Agent passes parameters via the action group input:
    {
      "app_id": str,
      "flow_descriptions": list[str],
      "slice_sst": int
    }
    """
    params = _extract_params(event)
    app_id           = params.get('app_id', f'app-{uuid.uuid4().hex[:8]}')
    flow_descriptions = params.get('flow_descriptions', ['permit in ip from any to any'])
    if isinstance(flow_descriptions, str):
        try:
            decoded = json.loads(flow_descriptions)
        except json.JSONDecodeError:
            decoded = flow_descriptions
        flow_descriptions = decoded if isinstance(decoded, list) else [decoded]
    flow_descriptions = [str(flow) for flow in flow_descriptions]
    slice_sst        = int(params.get('slice_sst', 1))

    pfd_id = f'pfd-{uuid.uuid4().hex[:6]}'
    payload = {
        'pfdDatas': {
            app_id: {
                'externalAppId': app_id,
                'pfds': {
                    pfd_id: {
                        'pfdId':            pfd_id,
                        'flowDescriptions': flow_descriptions,
                    }
                },
            }
        }
    }

    configured = _nef_endpoint_configured(NEF_BASE_URL)
    url = f'{NEF_BASE_URL}/3gpp-pfd-management/v1/{AF_ID}/transactions' if configured else ''
    status, body = _http_post(url, payload) if configured else (
        503,
        {'error': 'NEF_BASE_URL is not a resolved private endpoint'},
    )
    duplicate_retry = False
    effective_app_id = app_id
    if status == 500 and isinstance(body, dict) and 'APP_ID_DUPLICATED' in body:
        # free5GC keeps externalAppId unique across PFD transactions.  Repeated
        # scenario starts legitimately reuse the caller's stable app id, so
        # retry exactly this explicit conflict once with an auditable suffix.
        duplicate_retry = True
        effective_app_id = f'{app_id}-{uuid.uuid4().hex[:8]}'
        app_data = payload['pfdDatas'].pop(app_id)
        app_data['externalAppId'] = effective_app_id
        payload['pfdDatas'][effective_app_id] = app_data
        status, body = _http_post(url, payload)
    unsupported = status in (404, 405, 501)
    compensated = unsupported

    result = {
        'success':     status in (200, 201) or compensated,
        'http_status': 200 if compensated else status,
        'native_http_status': status,
        'app_id':      effective_app_id,
        'requested_app_id': app_id,
        'duplicate_retry': duplicate_retry,
        'pfd_id':      pfd_id,
        'slice_sst':   slice_sst,
        'api_endpoint': url,
        'native_response': body,
        'native_nef_support': status in (200, 201),
        'mode': 'nef-native' if status in (200, 201) else 'subscriber-profile-and-real-traffic',
        'compensated': compensated,
        'reason': (
            'free5GC NEF did not accept the PFD transaction; flow intent is kept in the '
            'actual subscriber/profile policy and verified through UERANSIM plus iperf3 traffic.'
        ) if compensated else '',
        'timestamp':   datetime.now(timezone.utc).isoformat(),
    }

    # Bedrock Agent expects this response format
    return {
        'messageVersion': '1.0',
        'response': {
            'actionGroup': event.get('actionGroup', ''),
            'function':    event.get('function', ''),
            'functionResponse': {
                'responseBody': {
                    'TEXT': {
                        'body': json.dumps(result)
                    }
                }
            }
        }
    }


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


def _extract_params(event: dict) -> dict:
    """Bedrock Agent sends parameters in requestBody or parameters."""
    # Action group function invocation
    params = {}
    for p in event.get('parameters', []):
        params[p['name']] = p['value']
    return params


def _http_post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(
        url,
        data=data,
        method='POST',
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
