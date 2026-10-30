"""Private two-UE same-UPF UL observation; never a priority or capacity promotion."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import time

from backend.onprem.traffic.capability import IperfCapabilityTraffic, sender_summary
from backend.onprem.runtime.ue import ENDPOINTS, seconds
from backend.onprem.runtime.round_resources import native_receiver_pods


IDENTITY = ('supi', 'slice', 'generation', 'ip', 'sessionId', 'podUid',
            'interface', 'smContextRef', 'pfcpSeid', 'upf', 'upfEpoch')


def exact_pair(status):
    """Require two real sessions on one eMBB UE Pod and one UPF incarnation."""
    rows = status.get('perUe')
    if (status.get('status') != 'established' or status.get('errors') != []
            or not isinstance(rows, list) or len(rows) != 2
            or any(not all(row.get(key) is True for key in
                           ('registered', 'pduEstablished', 'trafficVerified')) for row in rows)
            or any(row.get('slice') != 'embb' or row.get('upf') != 'upf-embb'
                   or any(not row.get(key) for key in IDENTITY) for row in rows)
            or len({row['supi'] for row in rows}) != 2
            or len({row['ip'] for row in rows}) != 2
            or len({row['interface'] for row in rows}) != 2
            or len({row['smContextRef'] for row in rows}) != 2
            or len({row['pfcpSeid'] for row in rows}) != 2
            or len({row['podUid'] for row in rows}) != 1
            or len({row['upfEpoch'] for row in rows}) != 1
            or {row['generation'] for row in rows} != {status.get('generation')}):
        raise RuntimeError('Two independently bound eMBB PDU sessions on one UE Pod and UPF are required')
    return sorted(rows, key=lambda row: row['supi'])


def client_window(sample):
    """Use actual sender timestamps from the shared UE Pod clock."""
    raw = sample['client']
    stamp = raw['start']['timestamp']['timemillisecs']
    sender = sender_summary(raw)
    duration = sender['seconds']
    if (type(stamp) is not int or not math.isfinite(duration)
            or not 8 <= duration <= 13 or not math.isfinite(sender['bits_per_second'])
            or not 3.2e6 <= sender['bits_per_second'] <= 4.8e6):
        raise RuntimeError('Raw UL sender did not offer the fixed 4 Mbps for the 10-second window')
    start = stamp / 1000
    return start, start + duration


def fresh_observation(observed_at, *, now=None):
    """Accept bounded guest clock lead, reject stale or unparseable evidence."""
    try:
        age = (time.time() if now is None else now) - seconds(observed_at)
    except (TypeError, ValueError):
        return False
    return -1 <= age <= 15


def validate_parallel_samples(samples, rows):
    if not isinstance(samples, list) or len(samples) != 2:
        raise RuntimeError('Exactly two UL receiver samples required')
    expected = {row['supi']: row for row in rows}
    if len(expected) != 2 or {sample.get('binding', {}).get('supi') for sample in samples} != set(expected):
        raise RuntimeError('UL samples do not bind both distinct SUPIs')
    intervals = []
    for sample in samples:
        row = expected[sample['binding']['supi']]
        binding = sample['binding']
        if (sample.get('direction') != 'ul' or sample.get('offeredTargetMbps') != 4
                or sample.get('durationSeconds') != 10 or sample.get('source') != 'native-iperf3-receiver'
                or sample.get('endpoint') != ENDPOINTS['embb']
                or binding.get('ip') != row['ip'] or binding.get('tun') != row['interface']
                or any(binding.get(key) != row[key] for key in IDENTITY)
                or type(sample.get('packets')) is not int or sample['packets'] <= 0
                or type(sample.get('lostPackets')) is not int or not 0 <= sample['lostPackets'] <= sample['packets']
                or type(sample.get('receivedMbps')) not in (float, int)
                or not math.isfinite(sample['receivedMbps']) or sample['receivedMbps'] <= 0):
            raise RuntimeError('Raw UL receiver or native PDU identity is incomplete')
        intervals.append(client_window(sample))
    overlap = min(end for _, end in intervals) - max(start for start, _ in intervals)
    if not 8 <= overlap <= 13:
        raise RuntimeError('Two actual sender UL windows did not overlap for eight seconds')
    return round(overlap, 6)


async def run_parallel_ul(controller, driver, path: Path, *, traffic=None):
    """Measure two bounded UL flows and drain workers before UE cleanup."""
    before = controller.status()
    rows = exact_pair(before)
    pods = driver.kubectl('get', 'pods', '-o', 'json')['items']
    ue_pods = [pod for pod in pods if pod.get('metadata', {}).get('uid') == rows[0]['podUid']]
    if len(ue_pods) != 1 or ue_pods[0]['metadata'].get('name') is None:
        raise RuntimeError('Exact shared UE Pod UID is unavailable')
    ue_pod = ue_pods[0]['metadata']['name']
    receiver = native_receiver_pods(pods, driver.namespace)['embb']
    traffic = traffic or IperfCapabilityTraffic(
        Path(__file__).resolve().parents[2] / '.cache/kubeadm-lab/bin/kubectl.exe',
        Path(__file__).resolve().parents[2] / '.cache/kubeadm-lab/kubeconfig',
        driver.context, driver.namespace)
    identity = {row['supi']: {key: row[key] for key in IDENTITY} for row in rows}

    def guard(binding):
        state = controller.status()
        try:
            current = exact_pair(state)
            return (fresh_observation(state['observedAt'])
                    and state['generation'] == before['generation']
                    and {row['supi']: {key: row[key] for key in IDENTITY} for row in current} == identity
                    and binding['supi'] in identity)
        except (KeyError, ValueError, TypeError, RuntimeError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(traffic.sample,
            {**row, 'tun': row['interface']}, ue_pod,
            receiver['metadata']['name'], receiver['metadata']['uid'],
            ENDPOINTS['embb'], 21000 + index, direction='ul', offered_mbps=4,
            duration=10, guard=guard) for index, row in enumerate(rows)]
        deadline = time.monotonic() + 90
        while not all(future.done() for future in futures):
            if time.monotonic() >= deadline:
                raise RuntimeError('Parallel UL workers exceeded bounded observation time')
            await controller.reconcile()
            await asyncio.sleep(1)
        samples = [future.result() for future in futures]
    after = controller.status()
    if {row['supi']: {key: row[key] for key in IDENTITY} for row in exact_pair(after)} != identity:
        raise RuntimeError('Native UE/SMF/UPF identity changed during UL observation')
    overlap = validate_parallel_samples(samples, rows)
    receipt = {'schemaVersion': 'cityverse.private-two-embb-ul/v1',
               'candidateOnly': True, 'priorityEffectClaimed': False,
               'sharedBottleneckClaimed': False, 'generation': before['generation'],
               'upf': 'upf-embb', 'upfEpoch': rows[0]['upfEpoch'],
               'actualSenderOverlapSeconds': overlap, 'offeredTotalMbps': 8,
               'receivedTotalMbps': sum(sample['receivedMbps'] for sample in samples),
               'flows': samples}
    receipt_path = path / '2-embb-ul-measured.json'
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return receipt
