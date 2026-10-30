"""Offline smoke planning/ownership checks, never commissioning execution."""
import importlib.util
from copy import deepcopy
import json
from pathlib import Path

import pytest


@pytest.fixture
def smoke(monkeypatch):
    folder = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('native_smoke_under_test', folder / 'run_browser_native_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_default_plan_is_seven_bounded_cases_and_no_live_calls(smoke, monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, 'read_snapshot', lambda *args: pytest.fail('Plan must not read live API'))
    smoke.main(['--output', str(tmp_path / 'plan')])
    result = json.loads((tmp_path / 'plan' / 'summary.json').read_text())
    assert result['status'] == 'plan-only' and result['executed'] == []
    assert len(result['planned']) == 7
    assert all(case['strategy'] == 'none' and case['total'] in (0, 1) for case in result['planned'])
    assert sum(case['total'] == 0 for case in result['planned']) == 1
    assert all(case['baselineUes'] + sum(case['events'].values()) == case['total'] for case in result['planned'])
    assert 'not-96' in result['scope']


def test_smoke_case_selection_does_not_change_matrix_coverage(smoke, tmp_path):
    smoke.main(['--output', str(tmp_path / 'plan'), '--case', 'zero', '--case', 'baseline'])
    result = json.loads((tmp_path / 'plan' / 'summary.json').read_text())
    assert [case['id'] for case in result['planned']] == ['zero', 'baseline']
    assert result['executed'] == []


def test_cleanup_refuses_different_round_before_any_ui_mutation(smoke, monkeypatch):
    monkeypatch.setattr(smoke, 'read_snapshot', lambda *_: {'roundId': 'unrelated', 'stage': 'preflight'})
    with pytest.raises(RuntimeError, match='not owned'):
        smoke.clean_owned(object(), 'unused', 'owned', 120)


def test_cleanup_released_owned_round_needs_no_ui_mutation(smoke, monkeypatch):
    current = {'roundId': 'owned', 'stage': 'completed', 'cleanupStatus': 'verified',
               'releaseProof': dict.fromkeys(('trafficStopped', 'policiesAbsent', 'rulesRestored', 'uesReleased', 'podsAbsent'), True)}
    monkeypatch.setattr(smoke, 'read_snapshot', lambda *_: current)
    assert smoke.clean_owned(object(), 'unused', 'owned', 120) == current


def test_native_round_release_accepts_verified_resident_fleet(smoke):
    current = {'roundId': 'owned', 'stage': 'completed', 'cleanupStatus': 'verified', 'generation': 197,
               'releaseProof': {'trafficStopped': True, 'policiesAbsent': True, 'rulesRestored': True,
                                'ueLeaseReleased': True, 'fleetPreserved': True, 'uesReleased': False,
                                'podsAbsent': False, 'generation': 197, 'scope': 'round-resources-only'}}
    assert smoke.native_round_released(current)
    for field, value in [('trafficStopped', False), ('ueLeaseReleased', False), ('fleetPreserved', False),
                         ('scope', 'global'), ('generation', 186), ('podsAbsent', True)]:
        changed = deepcopy(current)
        changed['releaseProof'][field] = value
        assert not smoke.native_round_released(changed)
    changed = deepcopy(current)
    changed['cleanupStatus'] = 'blocked'
    assert not smoke.native_round_released(changed)


def test_blocked_owned_cleanup_retries_visible_control_only_once(smoke, monkeypatch):
    from types import SimpleNamespace
    clock, clicks, names = [0], [], []
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(smoke, 'read_snapshot', lambda *_: {'roundId': 'owned', 'stage': 'cleaning', 'cleanupStatus': 'blocked'})
    button = SimpleNamespace(count=lambda: 1, click=lambda: clicks.append(True))
    button.filter = lambda **kwargs: button
    def locate(value):
        names.append(value); return button
    page = SimpleNamespace(get_by_test_id=locate, wait_for_timeout=lambda ms: clock.__setitem__(0, clock[0]+1))
    with pytest.raises(RuntimeError, match='deadline exceeded'):
        smoke.clean_owned(page, 'unused', 'owned', 3)
    assert names == ['scenario-cleanup'] and clicks == [True]


def test_pending_cleanup_does_not_send_repeated_cancel(smoke, monkeypatch):
    from types import SimpleNamespace
    clock = [0]
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(smoke, 'read_snapshot', lambda *_: {'roundId': 'owned', 'stage': 'cleaning', 'cleanupStatus': 'pending'})
    page = SimpleNamespace(get_by_test_id=lambda *a, **kw: pytest.fail('Pending cleanup must not be retried'),
        wait_for_timeout=lambda ms: clock.__setitem__(0, clock[0]+1))
    with pytest.raises(RuntimeError, match='deadline exceeded'):
        smoke.clean_owned(page, 'unused', 'owned', 3)


def test_pending_request_recovery_binds_only_exact_new_round(smoke):
    current = {'roundId': 'new', 'requestId': 'our-request'}
    assert smoke.confirmed_owner(current, 'our-request', 'previous') == 'new'
    for request_id, previous in ((None, 'previous'), ('', 'previous'), ('other', 'previous'), ('our-request', 'new')):
        assert smoke.confirmed_owner(current, request_id, previous) is None
    for round_id in (None, '', 7):
        assert smoke.confirmed_owner({**current, 'roundId': round_id}, 'our-request', 'previous') is None
    # Allocation correctness is a later assertion: the accepted request still
    # owns its round and must be cleaned if its actual allocation was wrong.
    assert smoke.confirmed_owner({**current, 'allocation': {'total': 50}}, 'our-request', 'previous') == 'new'


def test_zero_result_requires_no_actual_ue_or_traffic_state(smoke):
    value = {'allocation': {'baselineUes': 0, 'events': dict.fromkeys(smoke.EVENTS, 0), 'total': 0},
             'generation': None, 'perUe': [], 'measurements': [],
             'actions': [{'kind': 'zero_workload', 'requested': False}]}
    assert smoke.zero_result_valid(value)
    for change in ({'generation': 1}, {'perUe': [{'supi': 'unexpected'}]},
                   {'measurements': [{'source': 'receiver'}]}, {'actions': None},
                   {'actions': [{'kind': 'ue_allocation', 'requested': True}]},
                   {'actions': [{'kind': 'traffic_window', 'requested': True}]},
                   {'actions': [{'kind': 'application_policy', 'requested': False}]},
                   {'actions': [{'kind': 'zero_workload', 'requested': True}]}):
        assert not smoke.zero_result_valid({**value, **change})
    changed = deepcopy(value)
    changed['allocation']['events']['realtime_control'] = 1
    assert not smoke.zero_result_valid(changed)


def rendered_fixture():
    snapshot = {'roundId': 'r1', 'generation': 1, 'policyRevision': 0, 'sequence': 5,
                'allocation': {'total': 1}, 'counts': {'allocated': 1, 'registered': 1, 'pduEstablished': 1, 'trafficVerified': 1}}
    panel = {key: str(value) for key, value in snapshot.items()}
    panel.update(observedAt='1970-01-01T00:01:40+00:00', fresh='true', stage='results_ready',
                 allocated='1', registered='1', pduEstablished='1', trafficVerified='1',
                 visibleMeasurements='1', trafficVisible='true')
    return snapshot, {'alerts': [], 'evidence': [panel]}


def test_visible_abort_error_never_passes_even_without_console_errors(smoke):
    snapshot, rendered = rendered_fixture()
    assert smoke.rendered_result_errors(rendered, snapshot, 100) == []
    rendered['alerts'] = ['AbortError: signal is aborted without reason']
    assert 'Visible UI error' in smoke.rendered_result_errors(rendered, snapshot, 100)[0]


@pytest.mark.parametrize('field,value', [('fresh', 'false'), ('stage', 'completed'), ('roundId', 'other'),
    ('generation', '0'), ('policyRevision', '1'), ('sequence', '4'), ('observedAt', ''),
    ('observedAt', '1970-01-01T00:01:00+00:00')])
def test_native_api_pass_does_not_substitute_for_current_rendered_evidence(smoke, field, value):
    snapshot, rendered = rendered_fixture()
    rendered['evidence'][0][field] = value
    assert smoke.rendered_result_errors(rendered, snapshot, 100)


def test_missing_or_duplicate_visible_evidence_panels_fail(smoke):
    snapshot, rendered = rendered_fixture()
    for rows in ([], rendered['evidence'] * 2):
        assert smoke.rendered_result_errors({'alerts': [], 'evidence': rows}, snapshot, 100)


@pytest.mark.parametrize('field,value', [('allocated', '0'), ('registered', '0'), ('pduEstablished', '0'),
    ('trafficVerified', '0'), ('visibleMeasurements', '0'), ('trafficVisible', 'false')])
def test_visible_counts_and_flow_gate_must_match_api(smoke, field, value):
    snapshot, rendered = rendered_fixture()
    rendered['evidence'][0][field] = value
    assert smoke.rendered_result_errors(rendered, snapshot, 100)


def test_progress_rejects_false_motion_and_wrong_lifecycle(smoke):
    snapshot, rendered = rendered_fixture()
    snapshot.update(stage='registering', sequence=6)
    rendered['evidence'][0].update(stage='registering', sequence='6', trafficVisible='false')
    assert smoke.rendered_progress_errors(rendered, snapshot) == []
    rendered['evidence'][0]['trafficVisible'] = 'true'
    assert 'before/after' in smoke.rendered_progress_errors(rendered, snapshot)[0]
    rendered['evidence'][0].update(trafficVisible='false', roundId='other')
    assert 'identity/stage' in smoke.rendered_progress_errors(rendered, snapshot)[0]


def test_progress_does_not_treat_previous_result_frame_as_cleanup_motion(smoke):
    snapshot, rendered = rendered_fixture()
    snapshot.update(stage='cleaning', sequence=7)
    # The API is ahead of the next UI poll; the visible, verified result frame
    # still belongs to the same round and must not permanently fail the run.
    errors = smoke.rendered_progress_errors(rendered, snapshot)
    assert 'Rendered lifecycle identity/stage differs from API' in errors
    assert 'Rendered lifecycle snapshot is behind API' in errors
    assert not any('traffic motion' in error for error in errors)
    rendered['evidence'][0].update(stage='cleaning', sequence='7')
    assert 'before/after' in smoke.rendered_progress_errors(rendered, snapshot)[0]


def test_browser_errors_are_isolated_per_case(smoke):
    journal = smoke.BrowserErrorJournal()
    first = journal.begin('video_streaming')
    journal.capture('console', 'first failed')
    second = journal.begin('realtime_control')
    journal.capture('pageerror', 'second failed')
    assert first == ['console: first failed']
    assert second == ['pageerror: second failed']
    assert journal.history == {'video_streaming': first, 'realtime_control': second}


def test_hydrated_results_navigates_scenario_before_asserting_console(smoke):
    calls = []
    class Locator:
        def __init__(self, kind): self.kind = kind
        def get_by_role(self, role, **kwargs):
            assert self.kind == 'navigation' and role == 'button'
            assert kwargs['name'].search('1 Scenario')
            assert not kwargs['name'].search('5 Results')
            return Locator('scenario')
        def click(self): calls.append('click:' + self.kind)
    class Page:
        def get_by_role(self, role, **kwargs):
            assert role == 'navigation' and kwargs['name'] == 'Experiment steps'
            return Locator('navigation')
        def get_by_test_id(self, value):
            assert value == 'onprem-scenario-console'
            return Locator('console')
    class Assertion:
        def __init__(self, locator): self.locator = locator
        def to_be_visible(self):
            if self.locator.kind == 'console':
                assert 'click:scenario' in calls  # Results was initially selected.
            calls.append('visible:' + self.locator.kind)
    smoke.open_scenario_console(Page(), Assertion)
    assert calls == ['visible:scenario', 'click:scenario', 'visible:console']


def test_preflight_navigation_failure_is_saved_without_native_attempt(smoke, tmp_path):
    summary = {'executed': [], 'status': 'running'}
    smoke.record_failure(summary, AssertionError('Scenario control unavailable'),
                         'preflight_navigation', None, None)
    smoke.write_json(tmp_path / 'summary.json', summary)
    saved = json.loads((tmp_path / 'summary.json').read_text())
    assert saved['status'] == 'failed' and saved['executed'] == []
    assert saved['failurePhase'] == 'preflight_navigation'
    assert saved['intentObserved'] is False and saved['ownedRoundConfirmed'] is False
    assert saved['errorType'] == 'AssertionError'


def test_missed_short_result_window_reports_last_actual_dom_rejection(smoke):
    reason = smoke.missing_result_reason(['Rendered native snapshot is behind result',
                                           'Rendered native observation is stale'])
    assert 'behind result' in reason and 'observation is stale' in reason
    assert reason.startswith('No verified rendered result:')
