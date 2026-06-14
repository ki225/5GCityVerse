"""
NEF PFD Management Tool Lambda  (fn-nef-pfd-create)
────────────────────────────────────────────────────
Called by Bedrock Agent Action Group.
Creates a Packet Flow Description via free5GC NEF API (3GPP TS 29.522).

API: POST /3gpp-pfd-management/v1/{afId}/transactions
"""

import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone

NEF_BASE_URL = os.environ.get('NEF_BASE_URL', 'http://nef.free5gc.svc.cluster.local:8000')
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

    url = f'{NEF_BASE_URL}/3gpp-pfd-management/v1/{AF_ID}/transactions'
    status, body = _http_post(url, payload)

    result = {
        'success':     status in (200, 201),
        'http_status': status,
        'app_id':      app_id,
        'pfd_id':      pfd_id,
        'slice_sst':   slice_sst,
        'api_endpoint': url,
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
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 503, {}
