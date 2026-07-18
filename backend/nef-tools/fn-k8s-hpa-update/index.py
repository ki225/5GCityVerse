"""
K8s HPA Update Tool Lambda  (fn-k8s-hpa-update)

Adjusts the desired replica count for free5GC components
by patching the HorizontalPodAutoscaler min/max replicas.
Uses the Kubernetes Python client inside the VPC.
"""

import json
import os
from datetime import datetime, timezone
from kubernetes import client as k8s_client, config as k8s_config

NAMESPACE   = os.environ.get('FREE5GC_NAMESPACE', 'free5gc')
KUBECONFIG  = os.environ.get('KUBECONFIG', '')  # empty = in-cluster

# Component → HPA name mapping
HPA_NAMES = {
    'UPF': 'upf-hpa',
    'AMF': 'amf-hpa',
    'SMF': 'smf-hpa',
}


def lambda_handler(event: dict, _context) -> dict:
    params    = _extract_params(event)
    component = params.get('component', 'UPF').upper()

    if component not in HPA_NAMES:
        result = {'success': False, 'error': f'Unknown component: {component}'}
        return _bedrock_response(event, result)

    # Load k8s config
    try:
        if KUBECONFIG:
            k8s_config.load_kube_config(config_file=KUBECONFIG)
        else:
            k8s_config.load_incluster_config()
    except Exception as e:
        return _bedrock_response(event, {'success': False, 'error': str(e)})

    autoscaling = k8s_client.AutoscalingV2Api()
    hpa_name    = HPA_NAMES[component]

    try:
        current_hpa = autoscaling.read_namespaced_horizontal_pod_autoscaler(
            name=hpa_name,
            namespace=NAMESPACE,
        )
        current_status = {
            'min_replicas':     current_hpa.spec.min_replicas,
            'max_replicas':     current_hpa.spec.max_replicas,
            'current_replicas': current_hpa.status.current_replicas,
            'desired_replicas': current_hpa.status.desired_replicas,
        }
        if 'target_replicas' not in params:
            result = {
                'success':   False,
                'component': component,
                'hpa_name':  hpa_name,
                'namespace': NAMESPACE,
                'error':     'target_replicas is required; no default scaling target is applied',
                'current':   current_status,
            }
            return _bedrock_response(event, result)

        target_replicas = int(params['target_replicas'])
        # Patch min/max replicas on the HPA
        patch = {
            'spec': {
                'minReplicas': 1,
                'maxReplicas': max(target_replicas, 1),
            }
        }
        autoscaling.patch_namespaced_horizontal_pod_autoscaler(
            name=hpa_name,
            namespace=NAMESPACE,
            body=patch,
        )
        result = {
            'success':         True,
            'component':       component,
            'target_replicas': target_replicas,
            'hpa_name':        hpa_name,
            'namespace':       NAMESPACE,
            'previous':        current_status,
            'timestamp':       datetime.now(timezone.utc).isoformat(),
        }
    except k8s_client.ApiException as e:
        result = {
            'success':   False,
            'component': component,
            'error':     f'K8s API error {e.status}: {e.reason}',
        }

    return _bedrock_response(event, result)


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
