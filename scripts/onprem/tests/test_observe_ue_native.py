import pytest

from scripts.onprem.observe_ue_native import summarize_gnb_readouts


def test_gnb_summary_uses_fenced_driver_uid():
    observed = {'readouts': {'embb': {'uid': 'pod-uid', 'observedAt': '2026-10-03T17:00:00Z',
                                    'ueCount': 0}}}
    assert summarize_gnb_readouts(observed) == {
        'embb': {'podUid': 'pod-uid', 'observedAt': '2026-10-03T17:00:00Z'}}
    with pytest.raises(KeyError):
        summarize_gnb_readouts({'readouts': {'embb': {'observedAt': '2026-10-03T17:00:00Z'}}})
