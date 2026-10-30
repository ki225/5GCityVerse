"""Bounded NONE-only live UI commissioning, separate from the 96-case matrix.

Default is plan-only. No mocked routes; all mutations use the rendered controls.
Requires explicit --execute and the separately enabled native coordinator.
"""
import argparse
import json
from pathlib import Path
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from run_browser_scenario_matrix import EVENTS, LABELS, read_snapshot, released as legacy_released, result_errors, write_json


def native_round_released(snapshot):
    """Accept verified round cleanup that deliberately preserves the resident UE fleet."""
    if legacy_released(snapshot):
        return True
    proof = snapshot.get('releaseProof')
    if (snapshot.get('stage') != 'completed' or snapshot.get('cleanupStatus') != 'verified'
            or not isinstance(proof, dict)
            or proof.get('scope') not in {'round-resources-only', 'zero-workload-round-resources-only'}
            or any(proof.get(key) is not True for key in
                   ('trafficStopped', 'policiesAbsent', 'rulesRestored', 'ueLeaseReleased', 'fleetPreserved'))
            or proof.get('uesReleased') is not False or proof.get('podsAbsent') is not False):
        return False
    generation = snapshot.get('generation')
    return (generation is None and proof['scope'] == 'zero-workload-round-resources-only'
            or type(generation) is int and proof.get('generation') == generation)


class BrowserErrorJournal:
    """Keep browser errors attributable to exactly one navigation/case phase."""
    def __init__(self):
        self.current = []
        self.history = {}

    def begin(self, phase):
        self.current = []
        self.history[phase] = self.current
        return self.current

    def capture(self, kind, message):
        self.current.append(f'{kind}: {message}')


def cases():
    groups = (None, 'baseline', *EVENTS)
    return [dict(id='zero' if group is None else group, strategy='none',
                 baselineUes=int(group == 'baseline'),
                 events={event: int(event == group) for event in EVENTS},
                 total=int(group is not None)) for group in groups]


def confirmed_owner(snapshot, request_id, previous_round):
    candidate = snapshot.get('roundId')
    return (candidate if isinstance(candidate, str) and candidate and candidate != previous_round
            and isinstance(request_id, str) and request_id
            and snapshot.get('requestId') == request_id else None)


def zero_result_valid(snapshot):
    return (snapshot.get('allocation') == {'baselineUes': 0, 'events': dict.fromkeys(EVENTS, 0), 'total': 0}
            and snapshot.get('generation') is None and snapshot.get('perUe') == []
            and snapshot.get('measurements') == [] and isinstance(snapshot.get('actions'), list)
            and all(action.get('kind') == 'zero_workload' and action.get('requested') is False
                    for action in snapshot['actions']))


def rendered_result_errors(rendered, snapshot, now):
    """Visible DOM must corroborate the native result, not just console logs."""
    errors = []
    if rendered.get('alerts'):
        errors.append('Visible UI error: ' + '; '.join(rendered['alerts']))
    rows = rendered.get('evidence', [])
    if len(rows) != 1:
        return errors + ['No unique visible native evidence panel']
    row = rows[0]
    if any(row.get(key) != str(snapshot.get(key)) for key in ('roundId', 'generation', 'policyRevision')):
        errors.append('Rendered native identity does not match result')
    try:
        if int(row.get('sequence', '-1')) < snapshot['sequence']:
            errors.append('Rendered native snapshot is behind result')
        stamp = datetime.fromisoformat(row.get('observedAt', '').replace('Z', '+00:00'))
        if stamp.tzinfo is None or not -1 <= now - stamp.timestamp() <= 15:
            errors.append('Rendered native observation is stale')
    except (ValueError, TypeError, KeyError):
        errors.append('Rendered native observation is missing')
    if row.get('fresh') != 'true' or row.get('stage') != 'results_ready':
        errors.append('Rendered result does not have fresh native evidence')
    expected_counts = snapshot.get('counts') or {}
    for key in ('allocated', 'registered', 'pduEstablished', 'trafficVerified'):
        if row.get(key) != str(expected_counts.get(key)):
            errors.append(f'Rendered {key} count differs from API')
    total = snapshot.get('allocation', {}).get('total')
    if row.get('visibleMeasurements') != str(total):
        errors.append('Rendered receiver measurement count differs from allocation')
    if row.get('trafficVisible') != str(bool(total)).lower():
        errors.append('Rendered traffic visibility does not match verified receiver evidence')
    return errors


def read_rendered_result(page):
    evidence = page.get_by_test_id('onprem-round-evidence').filter(visible=True)
    return {'alerts': page.get_by_role('alert').filter(visible=True).all_text_contents(),
            'evidence': evidence.evaluate_all('''els => els.map(el => ({
              roundId: el.dataset.roundId, generation: el.dataset.generation,
              policyRevision: el.dataset.policyRevision, sequence: el.dataset.sequence,
              observedAt: el.dataset.observedAt, stage: el.dataset.stage, fresh: el.dataset.fresh,
              allocated: el.dataset.allocated, registered: el.dataset.registered,
              pduEstablished: el.dataset.pduEstablished, trafficVerified: el.dataset.trafficVerified,
              visibleMeasurements: el.dataset.visibleMeasurements, trafficVisible: el.dataset.trafficVisible
            }))''')}


def rendered_progress_errors(rendered, snapshot):
    """Correlate an in-progress/cleanup DOM with the same API revision."""
    rows = rendered.get('evidence', [])
    if len(rows) != 1:
        return ['No unique visible native evidence panel']
    row, errors = rows[0], []
    generation = '' if snapshot.get('generation') is None else str(snapshot.get('generation'))
    same_lifecycle = (row.get('roundId') == snapshot.get('roundId')
                      and row.get('stage') == snapshot.get('stage')
                      and row.get('generation') == generation)
    if not same_lifecycle:
        errors.append('Rendered lifecycle identity/stage differs from API')
    current_sequence = False
    try:
        current_sequence = int(row.get('sequence', '-1')) >= snapshot.get('sequence', -1)
        if not current_sequence:
            errors.append('Rendered lifecycle snapshot is behind API')
    except (TypeError, ValueError):
        errors.append('Rendered lifecycle sequence is missing')
    # A previous results_ready frame can remain visible for one UI poll after
    # the API enters cleaning. Judge traffic motion only for the same, current
    # lifecycle; cleanup is checked again once the completed frame renders.
    if (same_lifecycle and current_sequence and row.get('trafficVisible') == 'true'
            and snapshot.get('stage') not in ('measuring_baseline', 'measuring_result', 'results_ready')):
        errors.append('UI shows traffic motion before/after a verified measurement stage')
    return errors


def open_scenario_console(page, expect):
    """Navigate rendered UI even when persisted history hydrates Results first."""
    scenario = page.get_by_role('navigation', name='Experiment steps').get_by_role(
        'button', name=re.compile(r'Scenario$'))
    expect(scenario).to_be_visible()
    scenario.click()
    expect(page.get_by_test_id('onprem-scenario-console')).to_be_visible()


def record_failure(summary, error, phase, pending_request, owned_round):
    summary.update(status='failed', error=str(error), errorType=type(error).__name__,
                   failurePhase=phase, intentObserved=isinstance(pending_request, dict),
                   ownedRoundConfirmed=owned_round is not None)


def missing_result_reason(last_rendered_errors):
    return ('No verified rendered result: ' + '; '.join(last_rendered_errors)
            if last_rendered_errors else 'Missing verified result or browser error')


def clean_owned(page, api, round_id, timeout):
    """Reconcile only this accepted round using its visible cleanup control."""
    deadline, requested = time.monotonic() + timeout, False
    while time.monotonic() < deadline:
        current = read_snapshot(api)
        if current.get('roundId') != round_id:
            raise RuntimeError('Cleanup refused: current round is not owned')
        if native_round_released(current):
            return current
        retry = current.get('stage') == 'cleaning' and current.get('cleanupStatus') == 'blocked'
        if (current.get('stage') != 'cleaning' or retry) and not requested:
            stop = page.get_by_test_id('scenario-cleanup').filter(visible=True)
            if stop.count() != 1:
                raise RuntimeError('No unique visible owned cleanup control')
            stop.click()
            requested = True
        page.wait_for_timeout(1000)
    raise RuntimeError('Owned cleanup deadline exceeded; do not admit another case')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--case', choices=[case['id'] for case in cases()], action='append')
    parser.add_argument('--ui-url', default='http://127.0.0.1:5173')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    if not 120 <= args.timeout <= 1800:
        parser.error('Timeout must be 120..1800 seconds')
    for url in (args.ui_url, args.api_url):
        if urlsplit(url).scheme != 'http' or urlsplit(url).hostname not in {'localhost', '127.0.0.1'}:
            parser.error('Only loopback HTTP endpoints are allowed')
    selected = [case for case in cases() if not args.case or case['id'] in args.case]
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {'scope': 'one-UE-NONE-commissioning-not-96-case-matrix',
               'planned': selected, 'executed': [], 'status': 'plan-only'}
    write_json(args.output / 'summary.json', summary)
    if not args.execute:
        print(f'Planned {len(selected)} cases; executed 0')
        return
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1560, 'height': 1200})
        journal = BrowserErrorJournal()
        preflight_errors = journal.begin('preflight')
        case_errors = preflight_errors
        owned_round = None
        pending_request = None
        previous_round = None
        phase = 'preflight_navigation'
        def track_intent(request):
            nonlocal pending_request
            if request.method == 'POST' and urlsplit(request.url).path == '/api/onprem/scenario-rounds':
                pending_request = request.post_data_json
        page.on('request', track_intent)
        page.on('pageerror', lambda error: journal.capture('pageerror', str(error)))
        page.on('console', lambda entry: journal.capture('console', entry.text) if entry.type == 'error' else None)
        summary['status'] = 'running'
        write_json(args.output / 'summary.json', summary)
        try:
            page.goto(args.ui_url)
            page.wait_for_load_state('networkidle')
            language = page.get_by_role('button', name=re.compile('English'))
            if language.count():
                language.click()
            open_scenario_console(page, expect)
            if preflight_errors:
                raise RuntimeError('; '.join(preflight_errors))
            for case in selected:
                case_errors = journal.begin(case['id'])
                phase = 'case_preflight'
                before = read_snapshot(args.api_url)
                if before.get('capabilities', {}).get('start') is not True:
                    raise RuntimeError('Native commissioning capability unavailable')
                if before.get('stage') != 'idle' and not native_round_released(before):
                    raise RuntimeError('Previous owned resources have not been released')
                destination = args.output / case['id']
                destination.mkdir()
                pending_request, previous_round = None, before.get('roundId')
                open_scenario_console(page, expect)
                page.locator('#scenario-baseline-ues').fill(str(case['baselineUes']))
                for event, label in zip(EVENTS, LABELS):
                    page.get_by_test_id('scenario-include-' + event).set_checked(bool(case['events'][event]))
                    if case['events'][event]:
                        page.locator('#scenario-ues-' + event).fill('1')
                page.locator('input[name="onprem-strategy"][value="none"]').check()
                page.screenshot(path=str(destination / 'configured.png'), full_page=True)
                phase = 'intent_submission'
                with page.expect_response(lambda r: r.request.method == 'POST' and urlsplit(r.url).path == '/api/onprem/scenario-rounds', timeout=90000) as response:
                    page.get_by_test_id('scenario-submit').click()
                response = response.value
                posted, accepted = response.request.post_data_json, response.json()
                owned_round = confirmed_owner(accepted, posted.get('requestId'), previous_round)
                write_json(destination / 'intent.json', {'status': response.status, 'request': posted, 'response': accepted})
                if response.status != 202 or any(posted.get(key) != case[key] for key in ('baselineUes', 'events', 'strategy')):
                    raise RuntimeError('UI intent rejected or changed')
                round_id = owned_round
                if not round_id:
                    raise RuntimeError('Round identity missing')
                phase = 'native_execution'
                result_seen, loading_seen, cleanup_seen, failure, final = case['total'] == 0, case['total'] == 0, False, None, None
                last_rendered_errors, last_progress_errors = [], []
                deadline, sequence, index = time.monotonic() + args.timeout, -1, 0
                while time.monotonic() < deadline:
                    current = read_snapshot(args.api_url)
                    write_json(destination / f'observation-{index:05d}.json', current)
                    index += 1
                    if current.get('roundId') != round_id or current.get('sequence', -1) < sequence:
                        raise RuntimeError('Round identity or sequence changed unexpectedly')
                    sequence = current['sequence']
                    rendered_progress = read_rendered_result(page)
                    progress_errors = rendered_progress_errors(rendered_progress, current)
                    last_progress_errors = progress_errors
                    if any('traffic motion' in error for error in progress_errors):
                        failure = '; '.join(progress_errors)
                    if not progress_errors and current.get('stage') not in ('idle', 'results_ready', 'cleaning', 'completed'):
                        loading_seen = True
                    if not progress_errors and current.get('stage') == 'cleaning':
                        cleanup_seen = True
                    if current.get('executionOutcome') == 'failed':
                        failure = 'Backend execution failed'
                    if current.get('stage') == 'results_ready':
                        invalid = result_errors(current, case, time.time())
                        if invalid:
                            failure = '; '.join(invalid)
                        else:
                            rendered = read_rendered_result(page)
                            visible_errors = rendered_result_errors(rendered, current, time.time())
                            last_rendered_errors = visible_errors
                            write_json(destination / f'rendered-{index:05d}.json', {**rendered, 'errors': visible_errors})
                            if rendered['alerts']:
                                failure = '; '.join(visible_errors)
                                page.screenshot(path=str(destination / 'visible-error.png'), full_page=True)
                            elif not visible_errors:
                                result_seen = True
                                write_json(destination / 'result.json', current)
                                write_json(destination / 'rendered-result.json', rendered)
                                page.screenshot(path=str(destination / 'result.png'), full_page=True)
                    if native_round_released(current):
                        final = current
                        break
                    page.wait_for_timeout(1000)
                if final is None:
                    raise RuntimeError('Commissioning deadline expired; cleanup must be reconciled before another case')
                write_json(destination / 'cleanup.json', final)
                page.wait_for_timeout(3000)
                cleanup_rendered = read_rendered_result(page)
                cleanup_errors = rendered_progress_errors(cleanup_rendered, final)
                write_json(destination / 'rendered-cleanup.json', {**cleanup_rendered, 'errors': cleanup_errors})
                if cleanup_errors or cleanup_rendered['evidence'][0].get('trafficVisible') == 'true':
                    failure = '; '.join(cleanup_errors or ['Traffic remains visible after verified cleanup'])
                cleanup_seen = cleanup_seen or (final.get('stage') == 'completed' and final.get('cleanupStatus') == 'verified' and not cleanup_errors)
                owned_round = None
                if case['total'] == 0 and not zero_result_valid(final):
                    failure = 'Zero workload created unexpected allocation, UE or traffic state'
                if failure or not loading_seen or not result_seen or not cleanup_seen or final.get('executionOutcome') != 'passed' or case_errors:
                    detail = failure or ('Missing visible loading/cleanup lifecycle state: ' + '; '.join(last_progress_errors)
                                         if not loading_seen or not cleanup_seen else None)
                    raise RuntimeError(detail or '; '.join(case_errors) or missing_result_reason(last_rendered_errors if not result_seen else []))
                summary['executed'].append({'case': case['id'], 'roundId': round_id, 'status': 'passed', 'browserErrors': []})
                pending_request = None
                write_json(args.output / 'summary.json', summary)
            summary['status'] = 'passed'
            print(f'PASS {len(summary["executed"])} one-UE/zero NONE UI commissioning cases; not the 96-case matrix')
        except BaseException as error:
            record_failure(summary, error, phase, pending_request, owned_round)
            write_json(args.output / 'summary.json', summary)
            try:
                page.screenshot(path=str(args.output / 'failure.png'), full_page=True)
            except Exception as capture_error:
                summary['screenshotErrorType'] = type(capture_error).__name__
            if owned_round is None and isinstance(pending_request, dict):
                try:
                    owned_round = confirmed_owner(read_snapshot(args.api_url), pending_request.get('requestId'), previous_round)
                except Exception as reconciliation_error:
                    summary['intentReconciliationError'] = str(reconciliation_error)
            if owned_round:
                try:
                    summary['failureCleanup'] = clean_owned(page, args.api_url, owned_round, args.timeout)
                except Exception as cleanup_error:
                    summary['cleanupError'] = str(cleanup_error)
            raise
        finally:
            summary['preflightBrowserErrors'] = preflight_errors
            summary['activeCaseBrowserErrors'] = case_errors if 'case_errors' in locals() else []
            summary['browserErrorsByPhase'] = journal.history
            write_json(args.output / 'summary.json', summary)
            browser.close()


if __name__ == '__main__':
    main()
