import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from scripts.onprem import run_ue_capacity_private as capacity
from scripts.onprem.run_ue_capacity_private import distribution, complete, install_failed_run_authority
from backend.onprem.runtime.ue import GenerationStore, allocate, shards


def test_distribution_covers_four_slices():
    assert distribution(2) == {'embb': 1, 'urllc': 1, 'mmtc': 0, 'v2x': 0}
    assert distribution(100) == {'embb': 25, 'urllc': 25, 'mmtc': 25, 'v2x': 25}
    assert distribution(250) == {'embb': 63, 'urllc': 63, 'mmtc': 62, 'v2x': 62}
    assert distribution(500) == {'embb': 125, 'urllc': 125, 'mmtc': 125, 'v2x': 125}
    assert [(part['slice'], len(part['supis'])) for part in shards(allocate(distribution(500)), 250)] == [
        ('embb', 125), ('urllc', 125), ('mmtc', 125), ('v2x', 125)]


def test_incomplete_real_ue_evidence_cannot_pass():
    rows = [{'supi': f'imsi-{i:015d}', 'registered': True,
             'pduEstablished': True, 'trafficVerified': True} for i in range(100)]
    status = dict(status='established', errors=[], failed=0, perUe=rows,
                  requested=100, allocated=100, registered=100,
                  pduEstablished=100, trafficVerified=100)
    assert complete(status, 100)
    rows[3]['trafficVerified'] = False
    assert not complete(status, 100)
    rows[3]['trafficVerified'] = True
    rows[3]['supi'] = rows[2]['supi']
    assert not complete(status, 100)


def test_two_ue_candidate_requires_distinct_native_traffic_and_60_second_stability():
    rows = [{'supi': f'imsi-{208930100000001 + i}', 'slice': name,
             'registered': True, 'pduEstablished': True, 'trafficVerified': True}
            for i, name in enumerate(('embb', 'urllc'))]
    stamp = lambda second: datetime.fromtimestamp(second, timezone.utc).isoformat()
    result = dict(status='established', errors=[], failed=0, perUe=rows,
                  requested=2, allocated=2, registered=2, pduEstablished=2,
                  trafficVerified=2, stableSince=stamp(1000), observedAt=stamp(1059))
    assert not capacity.stable_two_ue_candidate(result)
    result['observedAt'] = stamp(1060)
    assert capacity.stable_two_ue_candidate(result)
    rows[1]['trafficVerified'] = False
    assert not capacity.stable_two_ue_candidate(result)
    rows[1]['trafficVerified'] = True
    rows[1]['supi'] = rows[0]['supi']
    assert not capacity.stable_two_ue_candidate(result)
    rows[1]['supi'] = 'imsi-208930100000002'
    rows[1]['slice'] = 'embb'
    assert not capacity.stable_two_ue_candidate(result)
    assert capacity.stable_two_ue_candidate(result, ('embb', 'embb'))


def test_two_ue_poll_does_not_save_pass_until_stable_window(tmp_path, monkeypatch):
    stamp = lambda second: datetime.fromtimestamp(second, timezone.utc).isoformat()
    rows = [{'supi': f'imsi-{208930100000001 + i}', 'slice': name,
             'registered': True, 'pduEstablished': True, 'trafficVerified': True}
            for i, name in enumerate(('embb', 'urllc'))]
    base = dict(status='established', errors=[], failed=0, perUe=rows,
                requested=2, allocated=2, registered=2, pduEstablished=2,
                trafficVerified=2, stableSince=stamp(1000))
    samples = iter(({**base, 'observedAt': stamp(1059)},
                    {**base, 'observedAt': stamp(1060)}))
    class Controller:
        async def reconcile(self):
            return next(samples)
    async def no_wait(_):
        return None
    monkeypatch.setattr(capacity, 'health', lambda driver: [])
    monkeypatch.setattr(capacity.asyncio, 'sleep', no_wait)
    assert asyncio.run(capacity.poll(Controller(), object(), tmp_path, '2', 2, 10))
    assert (tmp_path / '2-passed.json').is_file()
    assert json.loads((tmp_path / '2-passed.json').read_text())['observedAt'] == stamp(1060)


def test_failed_run_authority_binds_generation_pod_and_failure_file(tmp_path):
    generation = {'generation': 41, 'desired': distribution(100),
                  'allocation': [{'supi': f'imsi-{208930100000001 + i}'} for i in range(100)]}
    pod = {'metadata': {'name': 'cv-ue-g41-0', 'uid': 'uid-1',
                        'labels': {'cityverse.io/generation': '41'},
                        'annotations': {'cityverse.io/supis': json.dumps(['imsi-208930100000001'])}}}
    controller = SimpleNamespace(store=SimpleNamespace(current=lambda: generation))
    driver = SimpleNamespace(kubectl=lambda *args: {'items': [pod]}, terminal_authorizer=None)
    failure_file = tmp_path / '100-failed.json'
    rows = [{'supi': f'imsi-{208930100000001 + i}', 'registered': True,
             'pduEstablished': True, 'trafficVerified': True} for i in range(100)]
    rows[4]['trafficVerified'] = False
    failure_file.write_text(json.dumps({'generation': 41, 'requested': 100,
                                        'status': 'registering',
                                        'perUe': rows}), encoding='utf-8')
    install_failed_run_authority(controller, driver, tmp_path, failure_file)
    assert driver.terminal_authorizer(pod)['podUid'] == 'uid-1'
    changed = {'metadata': {**pod['metadata'], 'uid': 'uid-2'}}
    with pytest.raises(RuntimeError, match='identity'):
        driver.terminal_authorizer(changed)
    failure_file.write_text('{}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='evidence changed'):
        driver.terminal_authorizer(pod)


def test_failed_run_authority_is_revocable(tmp_path):
    generation = {'generation': 7, 'desired': distribution(10),
                  'allocation': [{'supi': f'imsi-{208930100000001 + i}'} for i in range(10)]}
    pod = {'metadata': {'name': 'cv-ue-g7-0', 'uid': 'uid-7',
                        'labels': {'cityverse.io/generation': '7'},
                        'annotations': {'cityverse.io/supis': json.dumps(['imsi-208930100000001'])}}}
    controller = SimpleNamespace(store=SimpleNamespace(current=lambda: generation))
    driver = SimpleNamespace(kubectl=lambda *args: {'items': [pod]}, terminal_authorizer=None)
    failed = tmp_path / '10-failed.json'
    rows = [{'supi': f'imsi-{208930100000001 + i}', 'registered': True,
             'pduEstablished': True, 'trafficVerified': True} for i in range(10)]
    rows[0]['registered'] = False
    failed.write_text(json.dumps({'generation': 7, 'requested': 10,
                                  'status': 'registering',
                                  'perUe': rows}), encoding='utf-8')
    install_failed_run_authority(controller, driver, tmp_path, failed)
    receipt_file = tmp_path / 'capacity-terminal-authority.json'
    receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
    receipt['status'] = 'revoked'
    receipt_file.write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(RuntimeError, match='changed or revoked'):
        driver.terminal_authorizer(pod)


def test_zero_intent_crash_reentry_cannot_skip_postflight(tmp_path, monkeypatch):
    controller = SimpleNamespace(store=SimpleNamespace(current=lambda: {
        'generation': 8, 'desired': capacity.ZERO}), status=lambda: {'status': 'registering'})
    driver = SimpleNamespace(kubectl=lambda *args: {'items': [{'metadata': {'name': 'cv-ue-g7-0'}}]})
    clocks = iter((0, 601))
    monkeypatch.setattr(capacity, 'time', SimpleNamespace(monotonic=lambda: next(clocks)))
    assert asyncio.run(capacity.cleanup(controller, driver, tmp_path)) is False
    assert (tmp_path / 'cleanup-blocked.json').is_file()


@pytest.mark.parametrize('gate', [2, 100, '2-embb-ul'])
@pytest.mark.parametrize('passed, expected_exit', [(True, 0), (False, 2)])
def test_single_100_gate_never_launches_higher_gate_and_always_cleans_up(
        tmp_path, monkeypatch, gate, passed, expected_exit):
    events = []
    monkeypatch.setattr(capacity.sys, 'argv', ['run_ue_capacity_private.py', '--gates', str(gate)])
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    for key, value in {'CITYVERSE_ONPREM_RUNTIME': 'kubeadm',
                       'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
                       'CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED': 'true',
                       'CITYVERSE_ONPREM_TRAFFIC_ENABLED': 'true',
                       'CITYVERSE_ONPREM_UE_STATE_FILE': str(tmp_path / 'state.sqlite3')}.items():
        monkeypatch.setenv(key, value)

    class Controller:
        adapter = SimpleNamespace(subscribers=SimpleNamespace(collect=lambda: {
            'verifiedCount': 1000, 'credentialsMatched': True}))

        def acquire(self):
            events.append('acquire')

        def release(self):
            events.append('release')

        async def reconcile(self):
            return {'status': 'stopped', 'errors': []}

        async def request(self, counts):
            events.append(('request', counts))

    monkeypatch.setattr(capacity, 'configured_driver',
                        lambda: SimpleNamespace(backend='free-ran-ue'))
    monkeypatch.setattr(capacity, 'GenerationStore', lambda path: object())
    monkeypatch.setattr(capacity, 'configured_native_collector', lambda store, driver: object())
    monkeypatch.setattr(capacity, 'UEController', lambda store, adapter, driver: Controller())
    monkeypatch.setattr(capacity, 'health', lambda driver: [])
    monkeypatch.setattr(capacity, 'ran_connected', lambda result: True)

    async def fake_poll(controller, driver, path, label, total, timeout, *, two_ue_slices):
        events.append(('poll', label, total))
        if gate == '2-embb-ul':
            assert two_ue_slices == ('embb', 'embb')
        return passed

    async def fake_parallel_ul(controller, driver, path):
        events.append('parallel-ul-measured')
        return {'actualSenderOverlapSeconds': 9.5, 'receivedTotalMbps': 7.8}

    from scripts.onprem import two_embb_parallel_ul
    monkeypatch.setattr(two_embb_parallel_ul, 'run_parallel_ul', fake_parallel_ul)

    async def fake_cleanup(controller, driver, path):
        events.append('guarded-cleanup')
        return True

    monkeypatch.setattr(capacity, 'poll', fake_poll)
    monkeypatch.setattr(capacity, 'cleanup', fake_cleanup)
    monkeypatch.setattr(capacity, 'install_failed_run_authority',
                        lambda controller, driver, path, failure: events.append(('failed-authority', failure.name)))
    assert asyncio.run(capacity.main()) == expected_exit
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'request'] == [
        ('request', {'embb': 2, 'urllc': 0, 'mmtc': 0, 'v2x': 0}
         if gate == '2-embb-ul' else distribution(gate))]
    assert ('poll', '2-embb-attach' if gate == '2-embb-ul' else str(gate),
            2 if gate == '2-embb-ul' else gate) in events
    assert ('parallel-ul-measured' in events) is (gate == '2-embb-ul' and passed)
    assert events[-2:] == ['guarded-cleanup', 'release']
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'failed-authority'] == (
        [] if passed else [('failed-authority',
            '2-embb-attach-failed.json' if gate == '2-embb-ul' else f'{gate}-failed.json')])


@pytest.mark.parametrize('gates', ['2', '2-embb-ul', '10', '50', '100', '100,250,500', '500'])
def test_partial_request_mutation_always_arms_guarded_cleanup(tmp_path, monkeypatch, gates):
    events = []
    args = ['run_ue_capacity_private.py', '--gates', gates]
    if gates == '500':
        args += ['--prior-100-run', str(tmp_path / 'prior')]
    monkeypatch.setattr(capacity.sys, 'argv', args)
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    for key, value in {'CITYVERSE_ONPREM_RUNTIME': 'kubeadm',
                       'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
                       'CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED': 'true',
                       'CITYVERSE_ONPREM_TRAFFIC_ENABLED': 'true',
                       'CITYVERSE_ONPREM_UE_STATE_FILE': str(tmp_path / 'state.sqlite3')}.items():
        monkeypatch.setenv(key, value)
    current = {'generation': 10, 'desired': distribution(2)}
    store = SimpleNamespace(current=lambda: current)

    class Controller:
        def __init__(self, *args):
            self.store = store
            self.adapter = SimpleNamespace(subscribers=SimpleNamespace(collect=lambda: {
                'verifiedCount': 1000, 'credentialsMatched': True}))

        def acquire(self): events.append('acquire')
        def release(self): events.append('release')
        async def reconcile(self): return {'status': 'stopped', 'errors': []}
        async def request(self, counts):
            current.update(generation=11, desired=counts)
            events.append('partially-mutated')
            raise RuntimeError('request failed after durable mutation')

    monkeypatch.setattr(capacity, 'configured_driver', lambda: SimpleNamespace(
        backend='free-ran-ue', batch_size=250, traffic_enabled=True))
    monkeypatch.setattr(capacity, 'GenerationStore', lambda path: store)
    monkeypatch.setattr(capacity, 'configured_native_collector', lambda store, driver: object())
    monkeypatch.setattr(capacity, 'UEController', Controller)
    monkeypatch.setattr(capacity, 'health', lambda driver: [])
    monkeypatch.setattr(capacity, 'ran_connected', lambda result: True)
    monkeypatch.setattr(capacity, 'validate_prior_100_run', lambda *args: None)

    async def guarded_cleanup(controller, driver, path):
        assert controller.store.current()['generation'] == 11
        assert sum(controller.store.current()['desired'].values()) > 0
        events.append('guarded-cleanup')
        return True

    monkeypatch.setattr(capacity, 'cleanup', guarded_cleanup)
    with pytest.raises(RuntimeError, match='durable mutation'):
        asyncio.run(capacity.main())
    assert events[-3:] == ['partially-mutated', 'guarded-cleanup', 'release']


def test_parallel_ul_failure_receipt_contains_only_fixed_diagnostics(tmp_path):
    capacity.record_parallel_ul_failure(tmp_path, 71)
    raw = (tmp_path / '2-embb-ul-measure-failed.json').read_text(encoding='utf-8')
    assert json.loads(raw) == {
        'generation': 71, 'errorType': 'ParallelUlBlocked',
        'phase': 'parallel_ul_measurement',
        'reasonCode': 'native_parallel_ul_unverified',
        'priorityEffectClaimed': False}
    assert 'error' not in json.loads(raw)


@pytest.mark.parametrize('gates', ['100,250', '100,500', '250', '500'])
def test_partial_or_higher_capacity_sequence_is_rejected_before_launch(monkeypatch, gates):
    monkeypatch.setattr(capacity.sys, 'argv', ['run_ue_capacity_private.py', '--gates', gates])
    monkeypatch.setattr(capacity, 'configured_driver',
                        lambda: pytest.fail('invalid gate reached live driver construction'))
    with pytest.raises(SystemExit, match='2'):
        asyncio.run(capacity.main())


def prior_100_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    path = tmp_path / '.tmp' / 'ue-capacity' / 'prior-run'
    path.mkdir(parents=True)
    store = GenerationStore(tmp_path / 'state.sqlite3')
    generation, _ = store.request(distribution(100))
    now = capacity.time.time()
    stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    ran = {name: {'podUid': f'gnb-{name}-uid',
                  'amfIncarnation': {'podUid': 'amf-uid'}} for name in capacity.ZERO}
    rows = [{'supi': row['supi'], 'slice': row['slice'], 'registered': True,
             'pduEstablished': True, 'trafficVerified': True, 'upfEpoch': f'epoch-{row["slice"]}'}
            for row in generation['allocation']]
    passed = {'generation': generation['generation'], 'status': 'established',
              'requested': 100, 'allocated': 100, 'registered': 100,
              'pduEstablished': 100, 'trafficVerified': 100, 'failed': 0,
              'perUe': rows, 'errors': [], 'stableSince': stamp(now - 180),
              'observedAt': stamp(now - 119), 'ranHealth': {'slices': ran}}
    store.save(passed)
    stopped, _ = store.request(capacity.ZERO)
    cleaned = {'generation': stopped['generation'], 'status': 'stopped',
               'requested': 0, 'allocated': 0, 'registered': 0,
               'pduEstablished': 0, 'trafficVerified': 0, 'failed': 0,
               'perUe': [], 'errors': [], 'observedAt': stamp(now - 30)}
    store.save(cleaned)
    zero = {'generation': stopped['generation'], 'observedAt': stamp(now - 29),
            'nativeUeRows': 0, 'ownedPods': 0, 'amfAvailable': True,
            'smfInventoryComplete': True, 'upfAvailable': True}
    for filename, value in [('100-passed.json', passed), ('cleanup-passed.json', cleaned),
                            ('cleanup-native-zero.json', zero)]:
        (path / filename).write_text(json.dumps(value), encoding='utf-8')
    native = {'generation': stopped['generation'], 'ues': [], 'sources': {
        'amf': {'available': True}, 'smf': {'available': True, 'inventoryComplete': True},
        'upf': {'available': True, 'epoch': json.dumps({name: f'epoch-{name}' for name in capacity.ZERO})}}}
    adapter = SimpleNamespace(observe_release=lambda generation: native)
    baseline = {'ranHealth': {'slices': ran}}
    return path, store, baseline, adapter, native, now


def test_500_prior_proof_binds_durable_generation_live_pod_ids_and_upf_epochs(tmp_path, monkeypatch):
    path, store, baseline, adapter, _, now = prior_100_fixture(tmp_path, monkeypatch)
    capacity.validate_prior_100_run(path, store, baseline, adapter, now=now)


@pytest.mark.parametrize('damage', ['expired', 'wrong_generation', 'missing_receipt',
                                    'incomplete_ue', 'changed_gnb_uid', 'changed_amf_uid',
                                    'changed_upf_epoch', 'wrong_native_generation', 'wrong_slice'])
def test_500_prior_proof_rejects_stale_incomplete_or_other_scope(tmp_path, monkeypatch, damage):
    path, store, baseline, adapter, native, now = prior_100_fixture(tmp_path, monkeypatch)
    if damage == 'expired':
        now += 6 * 3600 + 1
    elif damage == 'wrong_generation':
        receipt = path / 'cleanup-native-zero.json'
        value = json.loads(receipt.read_text(encoding='utf-8'))
        value['generation'] += 1
        receipt.write_text(json.dumps(value), encoding='utf-8')
    elif damage == 'missing_receipt':
        (path / 'cleanup-native-zero.json').unlink()
    elif damage == 'incomplete_ue':
        receipt = path / '100-passed.json'
        value = json.loads(receipt.read_text(encoding='utf-8'))
        value['perUe'][0]['trafficVerified'] = False
        receipt.write_text(json.dumps(value), encoding='utf-8')
    elif damage == 'changed_gnb_uid':
        baseline['ranHealth']['slices']['embb'] = {
            **baseline['ranHealth']['slices']['embb'], 'podUid': 'new-gnb-uid'}
    elif damage == 'changed_amf_uid':
        baseline['ranHealth']['slices']['embb'] = {
            **baseline['ranHealth']['slices']['embb'],
            'amfIncarnation': {'podUid': 'new-amf-uid'}}
    elif damage == 'changed_upf_epoch':
        native['sources']['upf']['epoch'] = json.dumps({name: f'new-epoch-{name}'
                                                         for name in capacity.ZERO})
    elif damage == 'wrong_native_generation':
        native['generation'] += 1
    else:
        receipt = path / '100-passed.json'
        value = json.loads(receipt.read_text(encoding='utf-8'))
        value['perUe'][0]['slice'] = 'urllc'
        value['perUe'][0]['upfEpoch'] = 'epoch-urllc'
        receipt.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(RuntimeError, match='Prior 100 capacity proof'):
        capacity.validate_prior_100_run(path, store, baseline, adapter, now=now)


def test_single_500_gate_requires_prior_receipt_before_driver(monkeypatch):
    monkeypatch.setattr(capacity.sys, 'argv', ['run_ue_capacity_private.py', '--gates', '500'])
    monkeypatch.setattr(capacity, 'configured_driver',
                        lambda: pytest.fail('missing prior receipt reached driver'))
    with pytest.raises(SystemExit, match='2'):
        asyncio.run(capacity.main())


def test_single_500_gate_rejects_wrong_driver_batch_before_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity.sys, 'argv', ['run_ue_capacity_private.py', '--gates', '500',
                                              '--prior-100-run', str(tmp_path / 'prior')])
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    for key, value in {'CITYVERSE_ONPREM_RUNTIME': 'kubeadm',
                       'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
                       'CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED': 'true',
                       'CITYVERSE_ONPREM_TRAFFIC_ENABLED': 'true'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(capacity, 'configured_driver', lambda: SimpleNamespace(
        backend='free-ran-ue', batch_size=50, traffic_enabled=True))
    monkeypatch.setattr(capacity, 'GenerationStore',
                        lambda path: pytest.fail('wrong batch reached controller state'))
    with pytest.raises(RuntimeError, match='batch size 250'):
        asyncio.run(capacity.main())


@pytest.mark.parametrize('passed, expected_exit', [(True, 0), (False, 2)])
def test_single_500_gate_uses_only_500_and_finally_guarded_cleanup(
        tmp_path, monkeypatch, passed, expected_exit):
    events = []
    monkeypatch.setattr(capacity.sys, 'argv', ['run_ue_capacity_private.py', '--gates', '500',
                                              '--prior-100-run', str(tmp_path / 'prior')])
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    for key, value in {'CITYVERSE_ONPREM_RUNTIME': 'kubeadm',
                       'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
                       'CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED': 'true',
                       'CITYVERSE_ONPREM_TRAFFIC_ENABLED': 'true',
                       'CITYVERSE_ONPREM_UE_STATE_FILE': str(tmp_path / 'state.sqlite3')}.items():
        monkeypatch.setenv(key, value)

    class Controller:
        adapter = SimpleNamespace(subscribers=SimpleNamespace(collect=lambda: {
            'verifiedCount': 1000, 'credentialsMatched': True}))
        store = SimpleNamespace()

        def acquire(self): events.append('acquire')
        def release(self): events.append('release')
        async def reconcile(self): return {'status': 'stopped', 'errors': []}
        async def request(self, counts): events.append(('request', counts))

    driver = SimpleNamespace(backend='free-ran-ue', batch_size=250, traffic_enabled=True)
    monkeypatch.setattr(capacity, 'configured_driver', lambda: driver)
    monkeypatch.setattr(capacity, 'GenerationStore', lambda path: object())
    monkeypatch.setattr(capacity, 'configured_native_collector', lambda store, driver: object())
    monkeypatch.setattr(capacity, 'UEController', lambda store, adapter, driver: Controller())
    monkeypatch.setattr(capacity, 'health', lambda driver: [])
    monkeypatch.setattr(capacity, 'ran_connected', lambda result: True)
    monkeypatch.setattr(capacity, 'validate_prior_100_run',
                        lambda *args: events.append('prior-validated'))

    async def fake_poll(controller, driver, path, label, total, timeout, *, two_ue_slices):
        events.append(('poll', label, total))
        return passed

    async def fake_cleanup(controller, driver, path):
        events.append('guarded-cleanup')
        return True

    monkeypatch.setattr(capacity, 'poll', fake_poll)
    monkeypatch.setattr(capacity, 'cleanup', fake_cleanup)
    monkeypatch.setattr(capacity, 'install_failed_run_authority',
                        lambda controller, driver, path, failure: events.append(('failed-authority', failure.name)))
    assert asyncio.run(capacity.main()) == expected_exit
    assert events.index('prior-validated') < events.index(('request', distribution(500)))
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'request'] == [
        ('request', distribution(500))]
    assert ('poll', '500', 500) in events
    assert events[-2:] == ['guarded-cleanup', 'release']
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'failed-authority'] == (
        [] if passed else [('failed-authority', '500-failed.json')])
