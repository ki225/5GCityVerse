"""Private, single-owner native UE capacity probe. Never exposes a public API.

Run only through start_native_backend.py --capacity-gates, which owns the
loopback forwards, pins kubectl/context, and shuts them down on completion.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.onprem.evidence.collector import configured_native_collector  # noqa: E402
from backend.onprem.runtime.kubeadm import configured_driver, LABEL  # noqa: E402
from backend.onprem.runtime.ue import GenerationStore, UEController, ran_connected, seconds  # noqa: E402

ZERO = dict.fromkeys(('embb', 'urllc', 'mmtc', 'v2x'), 0)
LIMITS = {100: 900, 250: 1200, 500: 1800, 50: 900, 10: 900, 2: 900}


def health(driver):
    """Fail closed on node pressure or any baseline NF/gNB/UPF degradation."""
    base = ['kubectl', '--context', driver.context, '--namespace', driver.namespace]
    nodes = json.loads(subprocess.check_output(base + ['get', 'nodes', '-o', 'json'], text=True, timeout=30))['items']
    if len(nodes) != 3 or any(any(c['status'] != ('True' if c['type'] == 'Ready' else 'False')
                                  for c in node['status']['conditions']
                                  if c['type'] in ('Ready', 'MemoryPressure', 'DiskPressure', 'PIDPressure'))
                              for node in nodes):
        raise RuntimeError('Node readiness or pressure gate failed')
    pods = driver.kubectl('get', 'pods', '-o', 'json')['items']
    for pod in pods:
        name = pod['metadata']['name']
        if name.startswith('cv-ue-') or pod['status'].get('phase') == 'Succeeded':
            continue
        statuses = pod['status'].get('containerStatuses', [])
        if pod['status'].get('phase') != 'Running' or not statuses or any(not c.get('ready') for c in statuses):
            raise RuntimeError(f'Baseline workload unhealthy: {name}')
    return pods


def summary(result):
    return {key: result.get(key) for key in ('generation', 'status', 'requested', 'allocated',
            'registered', 'pduEstablished', 'trafficVerified', 'failed', 'observedAt', 'stableSince', 'errors')}


def save(path, label, result):
    (path / f'{label}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'gate': label, **summary(result)}, separators=(',', ':')), flush=True)


def record_parallel_ul_failure(path, generation):
    """Persist only fixed diagnostics; exception text may contain private output."""
    (path / '2-embb-ul-measure-failed.json').write_text(json.dumps({
        'generation': generation, 'errorType': 'ParallelUlBlocked',
        'phase': 'parallel_ul_measurement',
        'reasonCode': 'native_parallel_ul_unverified',
        'priorityEffectClaimed': False}, indent=2), encoding='utf-8')


def distribution(total):
    return {name: total // 4 + (i < total % 4) for i, name in enumerate(ZERO)}


def complete(result, total):
    return (result.get('status') == 'established' and result.get('errors') == []
            and result.get('failed') == 0 and len(result.get('perUe', [])) == total
            and len({row['supi'] for row in result['perUe']}) == total
            and all(result.get(k) == total for k in ('requested', 'allocated', 'registered',
                                                     'pduEstablished', 'trafficVerified'))
            and all(all(row.get(k) is True for k in ('registered', 'pduEstablished', 'trafficVerified'))
                    for row in result['perUe']))


def stable_two_ue_candidate(result, required_slices=('embb', 'urllc')):
    """Require a real 60-second two-slice native state, never promote capacity."""
    if not complete(result, 2):
        return False
    if sorted(row.get('slice') for row in result['perUe']) != sorted(required_slices):
        return False
    try:
        observed = seconds(result['observedAt'])
        stable = seconds(result['stableSince'])
    except (KeyError, TypeError, ValueError):
        return False
    return 60 <= observed - stable < 24 * 3600


def validate_prior_100_run(path, store, baseline, adapter, *, now=None):
    """Bind a recent successful 100-only run to this zero-state lab and native epochs."""
    root = (ROOT / '.tmp' / 'ue-capacity').resolve()
    path = path.resolve()
    if path.parent != root or not path.is_dir():
        raise RuntimeError('Prior 100 evidence must be one exact private capacity run directory')
    names = ('100-passed.json', 'cleanup-passed.json', 'cleanup-native-zero.json')
    try:
        files = [path / name for name in names]
        if any(not file.is_file() or file.resolve().parent != path for file in files):
            raise ValueError('missing or redirected receipt')
        passed, cleaned, zero = [json.loads(file.read_text(encoding='utf-8')) for file in files]
        generation = passed['generation']
        if (type(generation) is not int or not complete(passed, 100)
                or passed['requested'] != 100 or passed['status'] != 'established'
                or seconds(passed['observedAt']) - seconds(passed['stableSince']) < 60):
            raise ValueError('incomplete 100 UE proof')
        prior_time = seconds(passed['observedAt'])
        cleanup_time = seconds(zero['observedAt'])
        stopped_time = seconds(cleaned['observedAt'])
        now = time.time() if now is None else now
        if not (0 <= prior_time - seconds(passed['stableSince'])
                and prior_time <= stopped_time <= cleanup_time <= now
                and now - prior_time <= 6 * 3600):
            raise ValueError('prior proof expired or time order invalid')
        if (cleaned.get('generation') != generation + 1 or cleaned.get('status') != 'stopped'
                or cleaned.get('errors') != [] or cleaned.get('perUe') != []
                or any(cleaned.get(key) != 0 for key in
                       ('requested', 'allocated', 'registered', 'pduEstablished', 'trafficVerified', 'failed'))
                or zero.get('generation') != generation + 1
                or zero.get('nativeUeRows') != 0 or zero.get('ownedPods') != 0
                or any(zero.get(key) is not True for key in
                       ('amfAvailable', 'smfInventoryComplete', 'upfAvailable'))):
            raise ValueError('native zero proof incomplete')
        current = store.current()
        with store.connect() as db:
            historical = store.decode(db.execute('SELECT * FROM generations WHERE id=?', (generation,)).fetchone())
        if (not current or current['generation'] != generation + 1
                or current['desired'] != ZERO or current['allocation'] != []
                or not historical or historical['result'].get('generation') != generation
                or not complete(historical['result'], 100)
                or {(row['supi'], row['slice']) for row in passed['perUe']}
                   != {(row['supi'], row['slice']) for row in historical['allocation']}):
            raise ValueError('prior proof differs from durable UE generations')
        old_ran = passed['ranHealth']['slices']
        live_ran = baseline['ranHealth']['slices']
        if set(old_ran) != set(ZERO) or set(live_ran) != set(ZERO):
            raise ValueError('four-slice RAN identity missing')
        for name in ZERO:
            for key in ('podUid',):
                if (not isinstance(old_ran[name].get(key), str) or not old_ran[name][key]
                        or old_ran[name][key] != live_ran[name].get(key)):
                    raise ValueError('gNB Pod identity changed')
            old_amf = old_ran[name]['amfIncarnation']['podUid']
            live_amf = live_ran[name]['amfIncarnation']['podUid']
            if not isinstance(old_amf, str) or not old_amf or old_amf != live_amf:
                raise ValueError('AMF Pod identity changed')
        native = adapter.observe_release(current['generation'])
        sources = native['sources']
        if (native.get('generation') != current['generation'] or native.get('ues') != []
                or any(sources[name].get('available') is not True
                for name in ('amf', 'smf', 'upf'))
                or sources['smf'].get('inventoryComplete') is not True):
            raise ValueError('current native zero evidence unavailable')
        epochs = json.loads(sources['upf']['epoch'])
        if not isinstance(epochs, dict) or set(epochs) != set(ZERO):
            raise ValueError('current four-UPF epoch proof missing')
        for name in ZERO:
            proven = {row.get('upfEpoch') for row in passed['perUe'] if row.get('slice') == name}
            if len(proven) != 1 or None in proven or '' in proven or proven != {epochs[name]}:
                raise ValueError('UPF epoch changed since 100 UE proof')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise RuntimeError(f'Prior 100 capacity proof unavailable or mismatched: {exc}') from None


def install_failed_run_authority(controller, driver, path, failure_file):
    """Scope existing terminal fences to a durable failed private capacity run."""
    failure = json.loads(failure_file.read_text(encoding='utf-8'))
    current = controller.store.current()
    rows = failure.get('perUe', [])
    if (not current or failure.get('generation') != current['generation']
            or failure.get('requested') != sum(current['desired'].values())
            or failure.get('status') in ('established', 'stopped')
            or not isinstance(rows, list) or len(rows) != failure.get('requested')
            or {row.get('supi') for row in rows if isinstance(row, dict)}
               != {item['supi'] for item in current['allocation']}
            or not any(not all(row.get(key) is True for key in ('registered', 'pduEstablished', 'trafficVerified'))
                       for row in rows)):
        raise RuntimeError('Failed capacity evidence does not match live generation')
    pods = driver.kubectl('get', 'pods', '-l', LABEL, '-o', 'json')['items']
    identities = {}
    for pod in pods:
        meta = pod['metadata']
        if meta.get('labels', {}).get('cityverse.io/generation') != str(current['generation']):
            raise RuntimeError('A different owned UE generation exists')
        identities[meta['name']] = {'uid': meta['uid'],
                                    'supis': json.loads(meta['annotations']['cityverse.io/supis'])}
    if not identities:
        raise RuntimeError('No exact failed-generation UE Pods to retire')
    receipt_file = path / 'capacity-terminal-authority.json'
    receipt = {'roundId': 'capacity-' + str(uuid4()), 'generation': current['generation'],
               'failureSha256': hashlib.sha256(failure_file.read_bytes()).hexdigest(),
               'status': 'cleaning', 'pods': identities}
    if receipt_file.exists():
        old = json.loads(receipt_file.read_text(encoding='utf-8'))
        if old['generation'] != receipt['generation'] or old['failureSha256'] != receipt['failureSha256'] or old['pods'] != identities:
            raise RuntimeError('Existing terminal authority has different identity')
        receipt = old
    else:
        receipt_file.write_text(json.dumps(receipt, indent=2), encoding='utf-8')

    def authorize(pod):
        recorded = json.loads(receipt_file.read_text(encoding='utf-8'))
        if recorded != receipt or recorded.get('status') != 'cleaning' or controller.store.current()['generation'] != recorded['generation']:
            raise RuntimeError('Capacity terminal authority changed or revoked')
        if hashlib.sha256(failure_file.read_bytes()).hexdigest() != recorded['failureSha256']:
            raise RuntimeError('Capacity failure evidence changed')
        meta = pod['metadata']
        identity = recorded['pods'].get(meta['name'])
        if identity != {'uid': meta['uid'], 'supis': json.loads(meta['annotations']['cityverse.io/supis'])}:
            raise RuntimeError('Capacity Pod authority identity changed')
        return {'roundId': recorded['roundId'], 'generation': recorded['generation'],
                'podUid': identity['uid'], 'supis': identity['supis'],
                'stage': 'cleaning', 'executionOutcome': 'failed'}

    driver.terminal_authorizer = authorize


async def poll(controller, driver, path, label, total, timeout, *, two_ue_slices=('embb', 'urllc')):
    deadline = time.monotonic() + timeout
    last_health = 0
    last_print = 0
    last_progress = time.monotonic()
    last_registered = -1
    result = {}
    while time.monotonic() < deadline:
        result = await controller.reconcile()
        now = time.monotonic()
        if now - last_health > 15:
            health(driver)
            last_health = now
        if now - last_print > 20:
            save(path, f'{label}-progress', result)
            last_print = now
        registered = result.get('registered')
        if type(registered) is int and registered > last_registered:
            last_registered, last_progress = registered, now
        if complete(result, total) and (total != 2 or stable_two_ue_candidate(result, two_ue_slices)):
            save(path, f'{label}-passed', result)
            return True
        if result.get('status') in ('failed', 'degraded'):
            break
        if last_registered >= 0 and last_registered < total and now - last_progress >= 180:
            print(f'Gate {label} plateaued at {last_registered}/{total} for 180 seconds', flush=True)
            break
        await asyncio.sleep(2)
    save(path, f'{label}-failed', result)
    return False


async def cleanup(controller, driver, path):
    """Use normal retirement; recovery only after native empty-context proof."""
    current = controller.store.current()
    if current is None:
        raise RuntimeError('No durable generation exists for cleanup proof')
    if current['desired'] != ZERO:
        try:
            await controller.request(ZERO)
        except Exception:
            # A failed generation is quarantined by the public controller. Do
            # not erase it: first ask real UEs to deregister and prove absence.
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                try:
                    evidence = await asyncio.to_thread(controller.adapter.observe_release, current['generation'])
                except Exception as exc:
                    print(f'Cleanup observation waiting: {type(exc).__name__}', flush=True)
                    await asyncio.sleep(3)
                    continue
                try:
                    await asyncio.to_thread(driver.cleanup, current['generation'], evidence)
                except Exception as exc:
                    # A cohort advances other Pods before reporting a bounded
                    # retry/observation error. Never delete around that guard.
                    age = {}
                    for name in ('amf', 'smf', 'upf'):
                        value = evidence.get('sources', {}).get(name, {})
                        try:
                            age[name] = round(time.time() - seconds(value.get('observedAt')), 2)
                        except (TypeError, ValueError):
                            age[name] = None
                    print(f'Cleanup waiting: {type(exc).__name__}: {exc}; '
                          f'captureMs={evidence.get("captureDurationMs")}; sourceAges={age}', flush=True)
                pods = driver.kubectl('get', 'pods', '-l', LABEL, '-o', 'json')['items']
                if not pods and not evidence.get('ues'):
                    controller.store.request(ZERO)
                    break
                await asyncio.sleep(3)
            else:
                save(path, 'cleanup-blocked', controller.status())
                return False
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = await controller.reconcile()
        pods = driver.kubectl('get', 'pods', '-l', LABEL, '-o', 'json')['items']
        if result.get('status') == 'stopped' and not pods and result.get('errors') == []:
            current = controller.store.current()
            try:
                release = await asyncio.to_thread(controller.adapter.observe_release, current['generation'])
            except Exception:
                await asyncio.sleep(3)
                continue
            sources = release.get('sources', {})
            if (release.get('ues') != []
                    or any(sources.get(name, {}).get('available') is not True for name in ('amf', 'smf', 'upf'))
                    or sources.get('smf', {}).get('inventoryComplete') is not True):
                await asyncio.sleep(3)
                continue
            (path / 'cleanup-native-zero.json').write_text(json.dumps({
                'generation': current['generation'], 'observedAt': release.get('observedAt'),
                'nativeUeRows': 0, 'amfAvailable': True, 'smfInventoryComplete': True,
                'upfAvailable': True, 'ownedPods': 0}, indent=2), encoding='utf-8')
            save(path, 'cleanup-passed', result)
            health(driver)
            return True
        await asyncio.sleep(3)
    save(path, 'cleanup-blocked', controller.status())
    return False


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gates', default='')
    parser.add_argument('--cleanup-from', type=Path)
    parser.add_argument('--prior-100-run', type=Path)
    args = parser.parse_args()
    same_upf_ul = args.gates == '2-embb-ul'
    gates = [2] if same_upf_ul else [int(value) for value in args.gates.split(',')] if args.gates else []
    if args.cleanup_from and (gates or args.prior_100_run):
        parser.error('Cleanup recovery cannot be combined with a capacity gate or prior proof')
    if not args.cleanup_from and gates not in ([100], [500], [100, 250, 500], [50], [10], [2]):
        parser.error('Use 2, 2-embb-ul, 10, 50 or 100 for a single gate, 500 with prior proof, or 100,250,500 for the full run')
    if bool(args.prior_100_run) != (gates == [500]):
        parser.error('Single 500 gate requires --prior-100-run; other gates must not supply it')
    if (os.environ.get('CITYVERSE_ONPREM_RUNTIME') != 'kubeadm'
            or os.environ.get('CITYVERSE_ONPREM_EVIDENCE_MODE') != 'native'
            or os.environ.get('CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED') != 'true'
            or os.environ.get('CITYVERSE_ONPREM_TRAFFIC_ENABLED') != 'true'):
        raise RuntimeError('Private native capacity preconditions absent')
    capacity_root = (ROOT / '.tmp' / 'ue-capacity').resolve()
    if args.cleanup_from:
        path = args.cleanup_from.resolve()
        if path.parent != capacity_root or not path.is_dir():
            raise RuntimeError('Cleanup evidence must be one exact private capacity run directory')
    else:
        path = capacity_root / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path.mkdir(parents=True, exist_ok=False)
    driver = configured_driver()
    if gates and getattr(driver, 'backend', None) != 'free-ran-ue':
        raise RuntimeError('Native capacity gates require free-ran-ue RAN backend')
    if gates == [500] and (getattr(driver, 'batch_size', None) != 250
                           or getattr(driver, 'traffic_enabled', None) is not True):
        raise RuntimeError('Single 500 gate requires traffic-enabled free-ran-ue driver with batch size 250')
    store = GenerationStore(Path(os.environ['CITYVERSE_ONPREM_UE_STATE_FILE']))
    controller = UEController(store, configured_native_collector(store, driver), driver)
    controller.acquire()
    launched = False
    try:
        if args.cleanup_from:
            failed = sorted(path.glob('*-failed.json'))
            current = controller.store.current()
            matches = [candidate for candidate in failed
                       if json.loads(candidate.read_text(encoding='utf-8')).get('generation') == current['generation']]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous failed capacity evidence for current generation')
            if matches:
                install_failed_run_authority(controller, driver, path, matches[0])
            # No failed snapshot can exist after a crash before the first
            # observation. Normal deregistration may still run, but terminal
            # shutdown remains unavailable without durable failure evidence.
            return 0 if await cleanup(controller, driver, path) else 3
        pods = health(driver)
        if any(p['metadata'].get('labels', {}).get('cityverse.io/managed-by') == 'onprem-ue-controller' for p in pods):
            raise RuntimeError('Owned UE Pods exist before benchmark')
        baseline_deadline = time.monotonic() + 90
        while True:
            baseline = await controller.reconcile()
            if baseline.get('status') == 'stopped' and not baseline.get('errors') and ran_connected(baseline):
                break
            if time.monotonic() >= baseline_deadline or baseline.get('errors'):
                save(path, 'baseline-failed', baseline)
                raise RuntimeError('Native zero-UE and four-gNB baseline not verified')
            await asyncio.sleep(2)
        save(path, 'baseline', baseline)
        if gates == [500]:
            await asyncio.to_thread(validate_prior_100_run, args.prior_100_run,
                                    controller.store, baseline, controller.adapter)
        subscriber = await asyncio.to_thread(controller.adapter.subscribers.collect)
        if subscriber.get('verifiedCount') != 1000 or subscriber.get('credentialsMatched') is not True:
            raise RuntimeError('1000 subscriber identities/credentials not verified')
        print('Subscriber gate: 1000 verified; credentials matched (secrets omitted)', flush=True)
        for total in gates:
            # A request can persist a new generation before its await raises.
            # Arm guarded retirement first, even for a partial mutation.
            launched = True
            await controller.request({'embb': 2, 'urllc': 0, 'mmtc': 0, 'v2x': 0}
                                     if same_upf_ul else distribution(total))
            label = '2-embb-attach' if same_upf_ul else str(total)
            if not await poll(controller, driver, path, label, total, LIMITS[total],
                              two_ue_slices=('embb', 'embb') if same_upf_ul else ('embb', 'urllc')):
                install_failed_run_authority(controller, driver, path, path / f'{label}-failed.json')
                print(f'Gate {label} failed; no higher gate will launch', flush=True)
                return 2
            if same_upf_ul:
                from scripts.onprem.two_embb_parallel_ul import run_parallel_ul
                try:
                    receipt = await run_parallel_ul(controller, driver, path)
                    print(json.dumps({'gate': label, 'parallelUl': 'measured',
                        'actualSenderOverlapSeconds': receipt['actualSenderOverlapSeconds'],
                        'receivedTotalMbps': receipt['receivedTotalMbps'],
                        'priorityEffectClaimed': False}), flush=True)
                except Exception:
                    record_parallel_ul_failure(path, controller.store.current()['generation'])
                    raise RuntimeError('Private parallel UL measurement failed') from None
        return 0
    finally:
        try:
            if launched and not await cleanup(controller, driver, path):
                raise RuntimeError('CRITICAL: guarded cleanup incomplete; operator reconciliation required')
        finally:
            controller.release()
            print(f'Evidence directory: {path}', flush=True)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
