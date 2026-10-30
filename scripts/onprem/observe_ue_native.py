"""Read-only native evidence snapshot for the dedicated kubeadm capacity lab."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.onprem.evidence.collector import configured_native_collector  # noqa: E402
from backend.onprem.runtime.kubeadm import LABEL  # noqa: E402
from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver  # noqa: E402
from backend.onprem.runtime.ue import GenerationStore  # noqa: E402


def summarize_gnb_readouts(gnb):
    return {name: {'observedAt': values['observedAt'], 'podUid': values['uid']}
            for name, values in gnb['readouts'].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT / '.tmp' not in output.parents or output.suffix != '.json':
        raise SystemExit('Output must be one JSON file beneath workspace .tmp')
    store = GenerationStore(Path(os.environ['CITYVERSE_ONPREM_UE_STATE_FILE']))
    current = store.current()
    if not current or current['generation'] != args.generation:
        raise SystemExit('Generation changed before native observation')
    driver = FreeRanKubeadmDriver(os.environ['CITYVERSE_ONPREM_KUBE_CONTEXT'],
                                  os.environ['CITYVERSE_ONPREM_NAMESPACE'])
    adapter = configured_native_collector(store, driver)
    subscribers = adapter.subscribers.collect()
    gnb = driver.observe_gnb_contexts(exec_timeout=30, parallel=True)
    evidence = adapter.observe_release(args.generation)
    pods = driver.kubectl('get', 'pods', '-l', LABEL, '-o', 'json')['items']
    rows = []
    for row in evidence['ues']:
        rows.append({'supi': row['supi'], 'generation': row.get('generation'),
                     'amfRegistered': row.get('amf', {}).get('registered') is True,
                     'smfSessionCount': row.get('smf', {}).get('sessionCount', 0),
                     'smfIp': row.get('smf', {}).get('ip'),
                     'upfPfcpPresent': bool(row.get('upf', {}).get('pfcpSeid')),
                     'upf': row.get('upf', {}).get('upf'),
                     'upfIp': row.get('upf', {}).get('ip'),
                     'pfcpSeid': row.get('upf', {}).get('pfcpSeid')})
    summary = {'generation': args.generation, 'observedAt': evidence['observedAt'],
               'captureDurationMs': evidence.get('captureDurationMs'),
               'ownedPods': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid']}
                             for p in pods],
               'amfRegistered': sum(r['amfRegistered'] for r in rows),
               'smfSessions': sum(r['smfSessionCount'] for r in rows),
               'upfPfcpRows': sum(r['upfPfcpPresent'] for r in rows),
               'unattributedUpfRows': sum(r['supi'].startswith('residual-upf-') for r in rows),
               'gnbUeCounts': {name: values['ueCount'] for name, values in gnb['readouts'].items()},
               'gnbSource': gnb['source'],
               'gnbReadouts': summarize_gnb_readouts(gnb),
               'subscriberVerifiedCount': subscribers['verifiedCount'],
               'subscriberCredentialsMatched': subscribers['credentialsMatched'],
               'sources': {name: {'available': value.get('available'),
                                  'observedAt': value.get('observedAt'),
                                  'inventoryComplete': value.get('inventoryComplete'),
                                  'source': value.get('source'), 'epoch': value.get('epoch')}
                           for name, value in evidence['sources'].items()},
               'rows': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key not in ('rows', 'sources', 'ownedPods')},
                     sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
