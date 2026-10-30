"""Offline refusal diagnostics; no controller or browser writes."""
import importlib.util
from pathlib import Path
import pytest


@pytest.fixture
def harness(monkeypatch):
    folder = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('calibration_diagnostics', folder / 'run_browser_native_calibration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('status', [403, 409])
@pytest.mark.parametrize('body', [None, {}, {'error': 'UNKNOWN', 'detail': 'SECRET'}, {'error': 'ROUND_CONFLICT', 'detail': 'SECRET' * 10000}])
def test_refusal_bounded_and_no_remote_text(harness, status, body):
    text = harness.intent_rejection(status, body, None, {}, {})
    assert f'HTTP {status}' in text and len(text) < 256 and 'SECRET' not in text


def test_baseline_reason_ownership_and_payload_separate(harness):
    case = {'baselineUes': 0, 'events': {}, 'strategy': 'none'}
    body = {'error': 'ROUND_CONFLICT', 'detail': 'Fresh known-scope resource baseline is unavailable'}
    assert 'baseline is unavailable' in harness.intent_rejection(409, body, None, case, case)
    assert 'ownership' in harness.intent_rejection(202, {}, None, case, case)
    assert 'allocation or strategy' in harness.intent_rejection(202, {}, 'owned', {**case, 'baselineUes': 1}, case)
    assert harness.intent_rejection(202, {}, 'owned', case, case) is None
