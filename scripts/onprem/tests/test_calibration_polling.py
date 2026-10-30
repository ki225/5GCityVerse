"""Offline decision fixtures only; no API/browser or native commissioning."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

path = Path(__file__).parents[1] / 'calibration_polling.py'
spec = importlib.util.spec_from_file_location('calibration_polling_contract', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def snapshot(stage='results_ready', sequence=10):
    return {'roundId': 'owned', 'generation': 64, 'policyRevision': 0, 'sequence': sequence,
        'stage': stage, 'executionOutcome': 'passed',
        'observedAt': datetime.fromtimestamp(100, timezone.utc).isoformat()}


def test_summary_deduplicated_with_stage_changes_and_bounded_heartbeat():
    tracker = module.SummaryPollTracker('owned')
    value = snapshot('registering')
    assert tracker.plan(value, monotonic_now=0).persist_summary
    assert not tracker.plan(value, monotonic_now=.25).persist_summary
    assert tracker.plan(value, monotonic_now=5).persist_summary
    value['stage'] = 'measuring_baseline'
    assert tracker.plan(value, monotonic_now=5.25).persist_summary
    value['sequence'] += 1
    assert tracker.plan(value, monotonic_now=5.5).persist_summary


def test_full_result_attempts_bounded_and_no_repeat_same_sequence():
    tracker = module.SummaryPollTracker('owned')
    assert tracker.plan(snapshot(), monotonic_now=0).full_boundary == 'result'
    assert tracker.plan(snapshot(), monotonic_now=1).full_boundary is None
    assert tracker.plan(snapshot(sequence=11), monotonic_now=1.25).full_boundary == 'result'
    assert tracker.plan(snapshot(sequence=12), monotonic_now=1.5).full_boundary is None
    assert tracker.plan(snapshot(sequence=12), monotonic_now=2).full_boundary == 'result'
    assert tracker.plan(snapshot(sequence=13), monotonic_now=3).full_boundary is None


def test_failure_and_cleanup_each_once_and_preserve_failure():
    tracker = module.SummaryPollTracker('owned')
    value = {**snapshot('completed'), 'executionOutcome': 'failed'}
    assert tracker.plan(value, monotonic_now=0).full_boundary == 'failure'
    assert tracker.plan(value, monotonic_now=.25).full_boundary == 'cleanup'
    assert tracker.plan(value, monotonic_now=.5).full_boundary is None
    assert value['executionOutcome'] == 'failed'


@pytest.mark.parametrize('change', [{'roundId': 'foreign'}, {'sequence': 9}, {'sequence': True}])
def test_poll_foreign_regressed_or_malformed_summary_fails(change):
    tracker = module.SummaryPollTracker('owned'); tracker.plan(snapshot(), monotonic_now=0)
    with pytest.raises(module.PollingContractError): tracker.plan({**snapshot(), **change}, monotonic_now=1)


@pytest.mark.parametrize('change', [{'roundId': 'foreign'}, {'generation': 65}, {'policyRevision': 1},
    {'stage': 'completed'}, {'sequence': 9}, {'viewSchema': 'cityverse.round-summary/v1'}])
def test_full_cannot_cross_trigger_or_substitute_summary(change):
    with pytest.raises(module.PollingContractError):
        module.join_full_boundary(snapshot(), {**snapshot(), **change}, boundary='result', received_at=101)


def test_actual_full_receive_time_used_and_no_observation_redating():
    full = snapshot(sequence=11); original = deepcopy(full)
    result = module.join_full_boundary(snapshot(), full, boundary='result', received_at=115)
    assert result['requiresFullGateValidation'] and result['snapshot'] == original == full
    assert result['fullReceivedAt'] == 115 and result['triggerSequence'] == 10
    with pytest.raises(module.PollingContractError):
        module.join_full_boundary(snapshot(), full, boundary='result', received_at=115.001)


def test_cleanup_snapshot_cannot_reconstruct_live_result():
    summary = snapshot('completed'); full = deepcopy(summary)
    full['releaseProof'] = {'observedAt': full['observedAt']}
    with pytest.raises(module.PollingContractError):
        module.join_full_boundary(summary, full, boundary='result', received_at=101)
    result = module.join_full_boundary(summary, full, boundary='cleanup', received_at=101)
    assert result['requiresFullGateValidation']  # Missing five flags still needs full release gate.


def test_old_failure_can_be_recorded_but_never_certifies_fresh_result():
    full = {**snapshot('cleaning'), 'executionOutcome': 'failed'}
    receipt = module.join_full_boundary(full, full, boundary='failure', received_at=999)
    assert receipt['snapshot']['observedAt'] == full['observedAt']
    assert receipt['requiresFullGateValidation']
