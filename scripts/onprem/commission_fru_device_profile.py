"""Read native FreeRAN commissioning evidence and emit sealed V2 compiler input.

Run on the Kubernetes/CRI host with its existing kubeconfig and spatial registry.
This command performs GET/exec readbacks and CRI image inspection only. It does
not reserve subscriptions, install admission rules or start UE/traffic Pods.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.onprem.runtime.device_sessions import DeviceDriverProfile
from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver, UE_IMAGE
from backend.onprem.runtime.spatial_ran import configured_registry, native_readouts
from backend.onprem.runtime.ue import timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', default='free5gc-onprem')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = subprocess.run(['crictl', 'inspecti', UE_IMAGE], capture_output=True,
        text=True, timeout=20, check=True)
    image = json.loads(result.stdout)
    if UE_IMAGE not in image['status']['repoDigests']:
        raise ValueError('CRI image differs from commissioned FreeRAN UE image')
    labels = image['info']['imageSpec']['config'].get('Labels', {})
    revision = labels.get('org.opencontainers.image.revision')
    if not revision or not labels.get('cityverse.binary.sha256'):
        raise ValueError('Native FreeRAN image build provenance missing')
    registry = configured_registry()
    driver = FreeRanKubeadmDriver(args.context, namespace=args.namespace)
    native = native_readouts(driver, registry)
    if any(native[g['gnbId']].get('available') is not True for g in registry['gnbs']):
        raise ValueError('Commissioned native FreeRAN RAN unavailable')
    endpoints = tuple(g['endpoint'] for g in registry['gnbs'])
    default = next(g['endpoint'] for g in registry['gnbs'] if 'embb' in g['slices'])
    reference = str(args.output.resolve())
    profile = DeviceDriverProfile('free-ran-ue', UE_IMAGE.split('@')[1], revision,
        reference, gnb_search_endpoint=default, commissioned_gnb_endpoints=endpoints)
    compiled = FreeRanKubeadmDriver(args.context, namespace=args.namespace, device_profile=profile)
    payload = {'source':'native-free-ran-device-commissioning/v1', 'observedAt':timestamp(),
        'ue_device_image':UE_IMAGE, 'ue_device_profile':asdict(profile),
        'admission_configuration_digest':compiled.reservation_configuration_digest,
        'image_provenance':labels, 'gnbs':native,
        'verification':'native-RAN-and-cached-image-read-only; UE admission/session/traffic not exercised'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps({'output':reference, 'gnb_count':len(native), 'source_revision':revision,
        'configuration_digest':compiled.reservation_configuration_digest}))


if __name__ == '__main__':
    main()
