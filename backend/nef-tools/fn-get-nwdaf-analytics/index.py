"""
Network Analytics Tool Lambda  (fn-get-nwdaf-analytics)
────────────────────────────────────────────────────────
Called by Bedrock Supervisor Agent to get current network state.
Queries Prometheus (via HTTP) for free5GC metrics.
In Phase 3+, also queries NWDAF Analytics API.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

PROMETHEUS_URL = os.environ.get('PROMETHEUS_URL', 'http://prometheus.monitoring.svc.cluster.local:9090')


def lambda_handler(event: dict, _context) -> dict:
    metrics = _query_prometheus()
    slices  = _compute_slice_loads(metrics)

    result = {
        'timestamp':          datetime.now(timezone.utc).isoformat(),
        'upf_cpu_percent':    metrics.get('upf_cpu',  20.0),
        'upf_pod_count':      int(metrics.get('upf_pods', 1)),
        'amf_pod_count':      int(metrics.get('amf_pods', 1)),
        'pdu_session_count':  int(metrics.get('pdu_sessions', 0)),
        'gtp_packets_per_sec': int(metrics.get('gtp_pps', 0)),
        'latency_ms':         metrics.get('latency_ms', 8.0),
        'throughput_mbps':    metrics.get('throughput_mbps', 100.0),
        'slice_loads':        slices,
        'hpa_enabled':        True,
        'scale_recommendation': _recommend_scale(metrics),
    }

    return _bedrock_response(event, result)


# ─── Prometheus queries ───────────────────────────────────────────────────────
_QUERIES = {
    'upf_cpu':        'avg(rate(container_cpu_usage_seconds_total{container="upf"}[1m])) * 100',
    'upf_pods':       'count(kube_pod_status_phase{namespace="free5gc",pod=~"upf.*",phase="Running"})',
    'amf_pods':       'count(kube_pod_status_phase{namespace="free5gc",pod=~"amf.*",phase="Running"})',
    'pdu_sessions':   'sum(free5gc_smf_pdu_session_count)',
    'gtp_pps':        'sum(rate(gtp5g_packet_count[30s]))',
    'latency_ms':     'avg(free5gc_upf_packet_latency_ms)',
    'throughput_mbps':'sum(rate(container_network_transmit_bytes_total{namespace="free5gc",pod=~"upf.*"}[30s])) / 1e6 * 8',
}


def _query_prometheus() -> dict:
    results = {}
    for key, query in _QUERIES.items():
        try:
            encoded = urllib.parse.urlencode({'query': query})
            url = f'{PROMETHEUS_URL}/api/v1/query?{encoded}'
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                vec = data.get('data', {}).get('result', [])
                if vec:
                    results[key] = float(vec[0]['value'][1])
        except Exception:
            pass  # return whatever we have
    return results


def _compute_slice_loads(metrics: dict) -> list:
    """Estimate per-slice load from total throughput / session count."""
    total_mbps   = metrics.get('throughput_mbps', 100.0)
    total_sess   = max(metrics.get('pdu_sessions', 1), 1)
    return [
        {'sst': 1, 'type': 'eMBB',  'load': min(int(total_mbps * 0.6), 100), 'sessions': int(total_sess * 0.5)},
        {'sst': 2, 'type': 'URLLC', 'load': min(int(total_mbps * 0.1), 100), 'sessions': int(total_sess * 0.1)},
        {'sst': 3, 'type': 'mMTC',  'load': min(int(total_mbps * 0.2), 100), 'sessions': int(total_sess * 0.35)},
        {'sst': 4, 'type': 'V2X',   'load': min(int(total_mbps * 0.1), 100), 'sessions': int(total_sess * 0.05)},
    ]


def _recommend_scale(metrics: dict) -> str:
    cpu = metrics.get('upf_cpu', 0)
    if cpu > 70:
        return f'UPF CPU at {cpu:.0f}% — recommend scale-out to {min(int(cpu / 25) + 1, 4)} replicas'
    return 'No scale action required'


def _extract_params(event: dict) -> dict:
    params = {}
    for p in event.get('parameters', []):
        params[p['name']] = p['value']
    return params


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
