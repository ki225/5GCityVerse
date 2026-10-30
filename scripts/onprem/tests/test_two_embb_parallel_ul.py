import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier
import time
from types import SimpleNamespace

import pytest

from backend.onprem.runtime.ue import ENDPOINTS
from scripts.onprem import two_embb_parallel_ul as probe
from scripts.onprem.two_embb_parallel_ul import exact_pair, fresh_observation, validate_parallel_samples


def pair():
    rows = []
    for index in range(2):
        rows.append({'supi': f'imsi-{208930100000001 + index}', 'slice': 'embb',
            'generation': 71, 'registered': True, 'pduEstablished': True,
            'trafficVerified': True, 'ip': f'10.101.0.{index + 2}',
            'sessionId': 1, 'podUid': 'ue-pod-uid', 'interface': f'uesimtun{index + 1}',
            'smContextRef': f'context-{index}', 'pfcpSeid': f'seid-{index}',
            'upf': 'upf-embb', 'upfEpoch': 'epoch-1'})
    return {'generation': 71, 'status': 'established', 'errors': [], 'perUe': rows}


def sample(row, start_ms):
    return {'binding': {**row, 'tun': row['interface']}, 'direction': 'ul',
            'offeredTargetMbps': 4, 'durationSeconds': 10,
            'source': 'native-iperf3-receiver', 'endpoint': ENDPOINTS['embb'],
            'packets': 2000, 'lostPackets': 0, 'receivedMbps': 3.95,
            'client': {'start': {'timestamp': {'timemillisecs': start_ms}},
                       'end': {'sum_sent': {'sender': True, 'seconds': 10.0,
                           'bits_per_second': 4_000_000, 'bytes': 5_000_000, 'packets': 4000}}}}


def test_exact_pair_requires_same_upf_and_pod_but_distinct_pdu_bindings():
    status = pair()
    assert len(exact_pair(status)) == 2
    for field, value in [('slice', 'urllc'), ('upf', 'upf-urllc'),
                         ('podUid', 'different-pod'), ('ip', status['perUe'][0]['ip']),
                         ('interface', status['perUe'][0]['interface']),
                         ('smContextRef', status['perUe'][0]['smContextRef']),
                         ('pfcpSeid', status['perUe'][0]['pfcpSeid']),
                         ('upfEpoch', 'different-epoch')]:
        invalid = deepcopy(status)
        invalid['perUe'][1][field] = value
        with pytest.raises(RuntimeError, match='Two independently bound'):
            exact_pair(invalid)


def test_parallel_ul_uses_actual_sender_clock_and_exact_receiver_bindings():
    rows = exact_pair(pair())
    samples = [sample(rows[0], 1_000_000), sample(rows[1], 1_001_000)]
    assert validate_parallel_samples(samples, rows) == 9
    late = deepcopy(samples)
    late[1]['client']['start']['timestamp']['timemillisecs'] = 1_003_000
    with pytest.raises(RuntimeError, match='overlap'):
        validate_parallel_samples(late, rows)
    wrong_pdu = deepcopy(samples)
    wrong_pdu[1]['binding']['tun'] = rows[0]['interface']
    with pytest.raises(RuntimeError, match='native PDU identity'):
        validate_parallel_samples(wrong_pdu, rows)
    no_delivery = deepcopy(samples)
    no_delivery[1]['receivedMbps'] = 0
    with pytest.raises(RuntimeError, match='receiver'):
        validate_parallel_samples(no_delivery, rows)
    low_offer = deepcopy(samples)
    low_offer[1]['client']['end']['sum_sent']['bits_per_second'] = 2_000_000
    with pytest.raises(RuntimeError, match='fixed 4 Mbps'):
        validate_parallel_samples(low_offer, rows)


def test_native_guard_tolerates_only_one_second_clock_lead_and_rejects_stale():
    stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    assert fresh_observation(stamp(1000.5), now=1000)
    assert fresh_observation(stamp(985), now=1000)
    assert not fresh_observation(stamp(1001.01), now=1000)
    assert not fresh_observation(stamp(984.99), now=1000)
    assert not fresh_observation('bad', now=1000)


def test_private_orchestrator_dispatches_distinct_ports_concurrently_and_labels_scope(tmp_path, monkeypatch):
    state = pair()
    state['observedAt'] = datetime.now(timezone.utc).isoformat()
    calls = []
    barrier = Barrier(2)
    ue_pod = {'metadata': {'name': 'cv-ue-g71-0', 'uid': 'ue-pod-uid'}}
    receiver = {'metadata': {'name': 'n6-embb-1', 'uid': 'n6-uid'}}
    monkeypatch.setattr(probe, 'native_receiver_pods', lambda pods, namespace: {'embb': receiver})

    class Controller:
        def status(self):
            state['observedAt'] = datetime.now(timezone.utc).isoformat()
            return deepcopy(state)

        async def reconcile(self):
            return self.status()

    class Traffic:
        def sample(self, binding, ue_pod_name, receiver_pod, receiver_uid, endpoint, port,
                   *, direction, offered_mbps, duration, guard):
            assert guard(binding)
            barrier.wait(timeout=3)
            calls.append((binding['supi'], ue_pod_name, receiver_pod, receiver_uid, port))
            return sample(binding, round(time.time() * 1000))

    driver = SimpleNamespace(namespace='free5gc-onprem', context='cityverse-kubeadm-lab',
        kubectl=lambda *args: {'items': [ue_pod]})
    receipt = asyncio.run(probe.run_parallel_ul(Controller(), driver, tmp_path, traffic=Traffic()))
    assert {row[-1] for row in calls} == {21000, 21001}
    assert {row[1:4] for row in calls} == {('cv-ue-g71-0', 'n6-embb-1', 'n6-uid')}
    assert receipt['actualSenderOverlapSeconds'] >= 8
    assert receipt['candidateOnly'] is True
    assert receipt['priorityEffectClaimed'] is False
    assert receipt['sharedBottleneckClaimed'] is False
    assert (tmp_path / '2-embb-ul-measured.json').is_file()
