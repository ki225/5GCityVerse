"""Plan or execute 96 serial, real-browser on-prem scenario cases.

Default: write a deterministic matrix, no network/browser/UE action. --execute
requires verified native capabilities and authenticated real AI readiness.
All mutations go through rendered UI; HTTP is used only for read-only evidence.
Blocked/failed/unrun cases are never passes. Servers remain operator-owned.
"""
from __future__ import annotations

if not __package__:  # Preserve the documented direct-file CLI invocation.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.onprem.evidence.scenario_receipts import (
    BINDING, DIAGNOSTIC_ACTIONS, NONPOLICY_ACTIONS, POLICY_ACTION, RELEASE, fresh, native_errors, released, result_errors, shard_aggregate_errors, timestamp
)

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from backend.onprem.scenarios.contracts import EVENT_SLICES
from backend.onprem.scenarios.profiles import public_catalog
EVENTS = tuple(EVENT_SLICES)
LABELS = tuple(row['name'] for row in public_catalog()['profiles'] if row['id'] in EVENT_SLICES)
STRATEGIES = ('none', 'static', 'ai')


def matrix_cases(total=50):
    if type(total) is not int or not 5 <= total <= 50:
        raise ValueError('Matrix total must be 5..50 to represent all five profiles')
    cases = []
    for mask in range(1 << len(EVENTS)):
        groups = ['web_browsing'] + [event for index, event in enumerate(EVENTS) if mask & (1 << index)]
        quotient, remainder = divmod(total, len(groups))
        allocation = {group: quotient + int(index < remainder) for index, group in enumerate(groups)}
        for strategy in STRATEGIES:
            cases.append({'id': f'{mask:02d}-{strategy}', 'mask': mask, 'strategy': strategy,
                          'baselineUes': allocation['web_browsing'],
                          'events': {event: allocation.get(event, 0) for event in EVENTS}, 'total': total})
    return cases








def round_resources_released(snapshot):
    """The coordinator can release round policies while retaining its UE fleet."""
    proof = snapshot.get('releaseProof') or {}
    return released(snapshot) or (snapshot.get('stage') == 'completed'
        and snapshot.get('cleanupStatus') == 'verified'
        and proof.get('scope') in {'round-resources-only', 'pre-established-round-resources-only'}
        and all(proof.get(key) is True for key in (
            'trafficStopped', 'policiesAbsent', 'rulesRestored', 'ueLeaseReleased', 'fleetPreserved')))


def preflight_errors(snapshot, now, *, initial=True):
    capabilities = snapshot.get('capabilities') or {}
    errors = [f'Capability unavailable: {key}' for key in ('start', 'staticPolicy', 'aiPolicy') if capabilities.get(key) is not True]
    if initial and (capabilities.get('aiAuthReady') is not True or not fresh(capabilities.get('aiAuthObservedAt'), now, 300)):
        errors.append('Real AI authentication readiness missing/stale; configured is not authenticated')
    if snapshot.get('stage') != 'idle' and not released(snapshot):
        errors.append('Previous known round has not been verified released')
    return errors








def read_snapshot(api):
    with urlopen(api + '/api/onprem/scenario-rounds/current', timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict): raise RuntimeError('Malformed snapshot')
    return value


class EvidenceWindow:
    """Native60s stabilization followed by a fresh identified receiver result.

    Repeated polling does not manufacture stabilization or re-date measurements;
    the persisted native controller proof binds the historical gate to these UEs.
    """
    def observe(self, snapshot, case, now):
        return not result_errors(snapshot, case, now)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def wait_snapshot(page, api, case_dir, predicate, timeout, round_id, *, allow_failed=False):
    deadline, index, previous_sequence = time.monotonic() + timeout, 0, -1
    while time.monotonic() < deadline:
        snapshot = read_snapshot(api)
        write_json(case_dir / f'observation-{index:05d}.json', snapshot); index += 1
        if snapshot.get('roundId') != round_id: raise RuntimeError('Round was superseded; refusing another identity')
        seq = snapshot.get('sequence')
        if type(seq) is not int or seq < previous_sequence: raise RuntimeError('Snapshot sequence regressed')
        previous_sequence = seq
        if snapshot.get('executionOutcome') == 'failed' and not allow_failed: raise RuntimeError('Backend execution failed; not a pass')
        if predicate(snapshot): return snapshot
        page.wait_for_timeout(2000)
    raise RuntimeError('Evidence/cleanup deadline expired; not a pass')


def execute_case(page, api, case, destination, timeout):
    before = read_snapshot(api)
    errors = preflight_errors(before, time.time(), initial=False)
    if errors: raise RuntimeError('; '.join(errors))
    page.get_by_role('navigation', name='Experiment steps').get_by_role('button').first.click()
    page.locator('#scenario-baseline-ues').fill(str(case['baselineUes']))
    for key, label in zip(EVENTS, LABELS):
        page.get_by_label('Include ' + label, exact=True).set_checked(case['events'][key] > 0)
        if case['events'][key]: page.locator('#scenario-ues-' + key).fill(str(case['events'][key]))
    page.locator(f'input[name="onprem-strategy"][value="{case["strategy"]}"]').check()
    page.screenshot(path=str(destination / 'configured.png'), full_page=True)
    with page.expect_response(lambda r: r.request.method == 'POST' and urlsplit(r.url).path == '/api/onprem/scenario-rounds') as response_info:
        page.get_by_role('button', name='Submit actual UE scenarios', exact=True).click()
    response = response_info.value
    posted, accepted = response.request.post_data_json, response.json()
    write_json(destination / 'intent.json', {'httpStatus': response.status, 'request': posted, 'response': accepted})
    if response.status != 202 or any(posted.get(key) != case[key] for key in ('baselineUes', 'events', 'strategy')): raise RuntimeError('Actual UI submission mismatch/rejection')
    round_id = accepted.get('roundId')
    if not round_id or round_id == before.get('roundId') or accepted.get('requestId') != posted.get('requestId'): raise RuntimeError('Round request identity not confirmed')
    window = EvidenceWindow()
    def positive(snapshot):
        return window.observe(snapshot, case, time.time())
    failure = None
    try:
        positive_dir = destination / 'positive'; positive_dir.mkdir()
        result = wait_snapshot(page, api, positive_dir, positive, timeout, round_id)
        write_json(destination / 'result-evidence.json', result)
        page.screenshot(path=str(destination / 'results.png'), full_page=True)
    except BaseException as error:
        failure = error
        write_json(destination / 'execution-failure.json', {'status': 'failed', 'error': str(error)})
    # Only the exact accepted round may be stopped, through its visible UI.
    current = read_snapshot(api)
    if current.get('roundId') != round_id: raise RuntimeError('Owned round superseded before cleanup; no mutation attempted')
    if not released(current) and current.get('stage') != 'cleaning':
        stop = page.get_by_role('button', name='Stop and clean this round', exact=True).filter(visible=True)
        if stop.count() != 1: raise RuntimeError('No unique visible owned-round cleanup control; operator reconciliation required')
        stop.click()
    cleanup_dir = destination / 'cleanup'; cleanup_dir.mkdir()
    final = wait_snapshot(page, api, cleanup_dir, released, timeout, round_id, allow_failed=True)
    write_json(destination / 'cleanup-evidence.json', final)
    page.screenshot(path=str(destination / 'completed.png'), full_page=True)
    if failure is not None: raise failure
    if final.get('executionOutcome') != 'passed': raise RuntimeError('Cleanup did not preserve passed execution outcome')
    return {'status': 'passed', 'roundId': round_id, 'generation': final.get('generation'), 'sequence': final.get('sequence'), 'cleanupStatus': final.get('cleanupStatus')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--total-ues', type=int, default=50)
    parser.add_argument('--timeout', type=int, default=1800, help='Per positive/cleanup phase,120..1800 seconds')
    parser.add_argument('--ui-url', default='http://127.0.0.1:5173')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    args = parser.parse_args(argv)
    if not 120 <= args.timeout <= 1800: parser.error('timeout must be120..1800')
    for value in (args.ui_url, args.api_url):
        if urlsplit(value).scheme != 'http' or urlsplit(value).hostname not in {'127.0.0.1', 'localhost'}: parser.error('Only loopback HTTP URLs permitted')
    try: cases = matrix_cases(args.total_ues)
    except ValueError as error: parser.error(str(error))
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / 'matrix.json', {'caseCount': 96, 'frozenAllocation': True, 'cases': cases})
    summary = {'status': 'not_executed', 'passed': 0, 'cases': {case['id']: {'status': 'unrun'} for case in cases}}
    write_json(args.output / 'summary.json', summary)
    if not args.execute:
        print('PLANNED96; EXECUTED0; PASSED0. Use --execute only after native gates pass.'); return
    try: initial = read_snapshot(args.api_url)
    except Exception as error:
        summary.update(status='blocked',errors=[type(error).__name__ + ': preflight evidence unavailable'])
        write_json(args.output / 'summary.json',summary)
        raise
    write_json(args.output / 'preflight.json', initial)
    errors = preflight_errors(initial, time.time())
    if errors:
        summary.update(status='blocked', errors=errors); write_json(args.output / 'summary.json', summary)
        raise RuntimeError('; '.join(errors))
    from playwright.sync_api import sync_playwright
    observations = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1560,'height':1200}, reduced_motion='reduce')
            page.on('console', lambda item: observations.append({'kind':'console','type':item.type,'text':item.text}))
            page.on('pageerror', lambda item: observations.append({'kind':'pageerror','text':str(item)}))
            page.on('requestfailed', lambda item: observations.append({'kind':'networkerror','url':item.url,'error':item.failure}))
            page.on('request', lambda item: observations.append({'kind':'request','url':item.url,'method':item.method,'body':item.post_data_json}) if item.method == 'POST' and '/api/onprem/scenario-rounds' in item.url else None)
            page.on('response', lambda item: observations.append({'kind':'http','url':item.url,'status':item.status}) if '/api/' in item.url else None)
            try:
                page.goto(args.ui_url); page.wait_for_load_state('networkidle')
                language = page.get_by_role('button', name=re.compile('English'))
                if language.count(): language.click(); page.wait_for_load_state('networkidle')
                write_json(args.output / 'discovered-buttons.json', page.get_by_role('button').all_text_contents())
                for case in cases:
                    destination = args.output / case['id']; destination.mkdir()
                    start = len(observations)
                    try:
                        summary['cases'][case['id']] = execute_case(page, args.api_url, case, destination, args.timeout)
                        bad = [entry for entry in observations[start:] if entry['kind'] in ('pageerror','networkerror') or (entry['kind']=='console' and entry['type']=='error') or (entry['kind']=='http' and entry['status'] >= 400)]
                        if bad: raise RuntimeError('Browser/network errors occurred; see evidence')
                        summary['passed'] += 1
                    except BaseException as error:
                        summary['cases'][case['id']] = {'status':'failed','error':str(error)}
                        raise
                    finally:
                        write_json(destination / 'network-console.json', observations[start:])
                        write_json(args.output / 'summary.json', summary)
                summary['status'] = 'passed' if summary['passed'] == 96 else 'failed'
            finally: browser.close()
    except BaseException:
        summary['status'] = 'failed'; raise
    finally: write_json(args.output / 'summary.json', summary)
    print(f'EXECUTED96; PASSED{summary["passed"]}; all known rounds verified released.')


if __name__ == '__main__':
    main()
