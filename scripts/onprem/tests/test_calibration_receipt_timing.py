"""Disk latency never re-dates evidence; a late DOM remains stale."""
from datetime import datetime
import json

import pytest

from scripts.onprem.tests.test_browser_native_calibration import harness, result_fixture, dom_fixture


@pytest.mark.parametrize('dom_delay,ui_stale', [(0, False), (1, True)])
def test_disk_delay_keeps_raw_receipt_and_independent_dom_time(harness, monkeypatch, tmp_path, dom_delay, ui_stale):
    snapshot, case, _ = result_fixture(harness)
    observed = datetime.fromisoformat(snapshot['observedAt']).timestamp()
    clock = [observed + 14.96]
    events = []
    monkeypatch.setattr(harness.time, 'time', lambda: clock[0])
    monkeypatch.setattr(harness, 'read_snapshot', lambda _: snapshot)
    rendered = {'alerts': [], 'evidence': [{
        **{key: str(snapshot[key]) for key in ('roundId', 'generation', 'policyRevision', 'sequence')},
        **{key: str(snapshot['counts'][key]) for key in
           ('allocated', 'registered', 'pduEstablished', 'trafficVerified')},
        'visibleMeasurements': str(snapshot['allocation']['total']),
        'trafficVisible': str(bool(snapshot['allocation']['total'])).lower(),
        'observedAt': snapshot['observedAt'], 'fresh': 'true', 'stage': 'results_ready'}]}
    def read_ui(page):
        events.append('dom')
        clock[0] += dom_delay
        return rendered
    monkeypatch.setattr(harness, 'read_rendered_result', read_ui)
    monkeypatch.setattr(harness, 'read_calibration_dom', lambda _: dom_fixture(snapshot))
    original_write = harness.write_json
    def slow_write(path, value):
        events.append('disk')
        clock[0] += 1  # Simulated 8 MB artifact latency, no actual sleep.
        original_write(path, value)
    monkeypatch.setattr(harness, 'write_json', slow_write)
    path = tmp_path / 'receipt.json'
    capture = harness.capture_full_boundary('unused', snapshot, 'result', path, page=object())
    assert events == ['dom', 'disk']
    assert capture['fullReceivedAt'] == observed + 14.96
    assert capture['uiReceivedAt'] == observed + 14.96 + dom_delay
    assert json.loads(path.read_text())['fullReceivedAt'] == observed + 14.96
    raw_errors, ui_errors = harness.captured_result_errors(capture, case, 5)
    assert raw_errors == []
    assert ('Rendered native observation is stale' in ui_errors) is ui_stale
    if not ui_stale:
        assert ui_errors == []
    # Using post-serialization wall time would incorrectly reject the same GET.
    assert 'Snapshot evidence stale/future/missing' in harness.calibration_result_errors(snapshot, case, clock[0], 5)


def test_stale_at_real_receipt_is_still_rejected_and_preserved(harness, monkeypatch, tmp_path):
    snapshot, _, _ = result_fixture(harness)
    observed = datetime.fromisoformat(snapshot['observedAt']).timestamp()
    monkeypatch.setattr(harness, 'read_snapshot', lambda _: snapshot)
    monkeypatch.setattr(harness.time, 'time', lambda: observed + 15.01)
    path = tmp_path / 'stale.json'
    with pytest.raises(ValueError, match='expired'):
        harness.capture_full_boundary('unused', snapshot, 'result', path)
    assert json.loads(path.read_text())['fullReceivedAt'] == observed + 15.01
