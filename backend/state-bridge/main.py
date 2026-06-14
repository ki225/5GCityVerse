"""
State Bridge
────────────
Watches the free5GC namespace in EKS for real Pod events,
then pushes them to all connected WebSocket clients via
API Gateway Management API.

Also runs a metrics loop: scrapes Prometheus every 5s
and publishes metrics_update messages.

Deploy as: ECS Fargate task (long-running) inside VPC.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3
from kubernetes import client as k8s_client, config as k8s_config, watch as k8s_watch

# ─── Config ───────────────────────────────────────────────────────────────────
NAMESPACE       = os.environ.get('FREE5GC_NAMESPACE', 'free5gc')
DYNAMODB_TABLE  = os.environ['DYNAMODB_TABLE']
APIGW_ENDPOINT  = os.environ['APIGW_WS_ENDPOINT']
PROMETHEUS_URL  = os.environ.get('PROMETHEUS_URL', 'http://prometheus.monitoring.svc.cluster.local:9090')
METRICS_INTERVAL = int(os.environ.get('METRICS_INTERVAL_SEC', '5'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('state-bridge')

# ─── AWS clients ──────────────────────────────────────────────────────────────
dynamodb  = boto3.resource('dynamodb')
apigw_mgmt = boto3.client('apigatewaymanagementapi', endpoint_url=APIGW_ENDPOINT)

COMPONENT_MAP = {
    'upf':  'UPF',
    'amf':  'AMF',
    'smf':  'SMF',
    'nef':  'NEF',
    'pcf':  'PCF',
    'nssf': 'NSSF',
    'nrf':  'NRF',
    'udr':  'UDR',
    'ausf': 'AUSF',
}


def extract_component(pod_name: str) -> str:
    name_lower = pod_name.lower()
    for prefix, component in COMPONENT_MAP.items():
        if name_lower.startswith(prefix):
            return component
    return 'UNKNOWN'


# ─── Broadcast ────────────────────────────────────────────────────────────────
def broadcast(message: dict) -> None:
    table       = dynamodb.Table(DYNAMODB_TABLE)
    connections = table.query(
        IndexName='gsi-connections',
        KeyConditionExpression='pk = :pk',
        ExpressionAttributeValues={':pk': 'WS_CONNECTION'},
    ).get('Items', [])

    payload = json.dumps(message).encode('utf-8')
    for conn in connections:
        connection_id = conn['sk']
        try:
            apigw_mgmt.post_to_connection(ConnectionId=connection_id, Data=payload)
        except apigw_mgmt.exceptions.GoneException:
            log.info('Removing stale connection: %s', connection_id)
            table.delete_item(Key={'pk': 'WS_CONNECTION', 'sk': connection_id})
        except Exception as e:
            log.warning('Failed to push to %s: %s', connection_id, e)


# ─── Pod Watcher ──────────────────────────────────────────────────────────────
def watch_pods() -> None:
    """Infinite loop watching free5GC pod events."""
    log.info('Starting pod watcher — namespace=%s', NAMESPACE)
    v1 = k8s_client.CoreV1Api()
    w  = k8s_watch.Watch()

    while True:
        try:
            for event in w.stream(v1.list_namespaced_pod, namespace=NAMESPACE, timeout_seconds=300):
                pod   = event['object']
                etype = event['type']            # ADDED / MODIFIED / DELETED
                name  = pod.metadata.name
                phase = pod.status.phase or 'Unknown'

                component = extract_component(name)
                msg = {
                    'type': 'pod_event',
                    'payload': {
                        'event':     etype,
                        'pod':       name,
                        'phase':     phase,
                        'component': component,
                        'namespace': NAMESPACE,
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                    }
                }
                log.info('Pod event: %s %s %s', etype, name, phase)
                broadcast(msg)

        except Exception as e:
            log.error('Pod watch error: %s — restarting in 5s', e)
            time.sleep(5)


# ─── Metrics Loop ─────────────────────────────────────────────────────────────
PROM_QUERIES = {
    'upf_cpu':         'avg(rate(container_cpu_usage_seconds_total{container="upf"}[1m])) * 100',
    'upf_pods':        'count(kube_pod_status_phase{namespace="free5gc",pod=~"upf.*",phase="Running"})',
    'amf_pods':        'count(kube_pod_status_phase{namespace="free5gc",pod=~"amf.*",phase="Running"})',
    'pdu_sessions':    'sum(free5gc_smf_pdu_session_count)',
    'gtp_pps':         'sum(rate(gtp5g_packet_count[30s]))',
    'latency_ms':      'avg(free5gc_upf_packet_latency_ms)',
    'throughput_mbps': 'sum(rate(container_network_transmit_bytes_total{namespace="free5gc",pod=~"upf.*"}[30s])) / 1e6 * 8',
}


def query_prometheus() -> dict:
    results = {}
    for key, query in PROM_QUERIES.items():
        try:
            encoded = urllib.parse.urlencode({'query': query})
            url = f'{PROMETHEUS_URL}/api/v1/query?{encoded}'
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                vec  = data.get('data', {}).get('result', [])
                if vec:
                    results[key] = float(vec[0]['value'][1])
        except Exception:
            pass
    return results


def metrics_loop() -> None:
    log.info('Starting metrics loop — interval=%ds', METRICS_INTERVAL)
    while True:
        try:
            raw = query_prometheus()
            msg = {
                'type': 'metrics_update',
                'payload': {
                    'upfCpuPercent':    round(raw.get('upf_cpu', 0.0), 1),
                    'upfPodCount':      int(raw.get('upf_pods', 1)),
                    'amfPodCount':      int(raw.get('amf_pods', 1)),
                    'gtpPacketsPerSec': int(raw.get('gtp_pps', 0)),
                    'pduSessionCount':  int(raw.get('pdu_sessions', 0)),
                    'latencyMs':        round(raw.get('latency_ms', 8.0), 1),
                    'throughputMbps':   round(raw.get('throughput_mbps', 0.0), 1),
                    'timestamp':        int(time.time() * 1000),
                }
            }
            broadcast(msg)
        except Exception as e:
            log.error('Metrics loop error: %s', e)
        time.sleep(METRICS_INTERVAL)


# ─── Entry ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Load k8s config (in-cluster when running in ECS with IRSA / node role)
    try:
        k8s_config.load_incluster_config()
        log.info('Loaded in-cluster k8s config')
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        log.info('Loaded local kube config')

    import threading
    t_metrics = threading.Thread(target=metrics_loop, daemon=True)
    t_metrics.start()

    watch_pods()   # blocks main thread
