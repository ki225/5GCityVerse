"""
NEF Traffic Influence Tool Lambda  (fn-nef-traffic-influence)
──────────────────────────────────────────────────────────────
Requests traffic steering for specific UE/slice via free5GC NEF.
Maps to 3GPP TS 29.522 §4.4.2 Traffic Influence API.

API: POST /3gpp-traffic-influence/v1/{afId}/subscriptions
"""

import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone

NEF_BASE_URL = os.environ.get('NEF_BASE_URL', 'http://nef.free5gc.svc.cluster.local:8000')
AF_ID        = os.environ.get('NEF_AF_ID', 'cityverse-af')

# SST → DNAI (Data Network Access Identifier) mapping
SST_DNAI = {
    1: 'edge-upf-embb',
    2: 'edge-upf-urllc',
    3: 'edge-upf-mmtc',
    4: 'edge-upf-v2x',
}


def lambda_handler(event: dict, _context) -> dict:
    params       = _extract_params(event)
    slice_sst    = int(params.get('slice_sst', 1))
    af_service_id = params.get('af_service_id', f'svc-{uuid.uuid4().hex[:6]}')
    ue_ip        = params.get('ue_ipv4', '')

    # Build Traffic Influence subscription body (TS 29.522 §4.4.2)
    body: dict = {
        'afServiceId': af_service_id,
        'snssai':      {'sst': slice_sst, 'sd': f'{slice_sst:06d}'},
        'dnn':         'internet',
        'trafficFilters': [
            {
                'flowDescriptions': _flow_descriptions_for_sst(slice_sst)
            }
        ],
        'trafficRoutes': [
            {
                'dnai':      SST_DNAI.get(slice_sst, 'edge-upf'),
                'routeInfo': {
                    'ipv4Addr':   '10.100.1.1',
                    'portNumber': 2152,
                },
            }
        ],
        'notifUri': '',  # no callback needed in demo
    }

    if ue_ip:
        body['ipv4Addr'] = ue_ip

    url = f'{NEF_BASE_URL}/3gpp-traffic-influence/v1/{AF_ID}/subscriptions'
    status, resp_body = _http_post(url, body)

    result = {
        'success':      status in (200, 201),
        'http_status':  status,
        'af_service_id': af_service_id,
        'slice_sst':    slice_sst,
        'dnai':         SST_DNAI.get(slice_sst, 'edge-upf'),
        'api_endpoint': url,
        'timestamp':    datetime.now(timezone.utc).isoformat(),
    }

    return _bedrock_response(event, result)


def _flow_descriptions_for_sst(sst: int) -> list[str]:
    return {
        1: ['permit in udp from any to any', 'permit out udp from any to any'],           # eMBB: UDP video
        2: ['permit in ip from any to any', 'permit out ip from any to any'],              # URLLC: all
        3: ['permit in udp from any to any 5683', 'permit out udp from any 5683 to any'], # mMTC: CoAP
        4: ['permit out udp from any to 224.0.0.0/4 5000',                                # V2X: multicast
            'permit in udp from 224.0.0.0/4 5000 to any'],
    }.get(sst, ['permit in ip from any to any'])


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
