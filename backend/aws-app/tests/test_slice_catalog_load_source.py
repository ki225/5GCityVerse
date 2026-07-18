from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import CityVerseBackendApp
from free5gc_utils import Free5gcClient
from slice_catalog import SliceCatalog


class _FakeMetricsService:
    """Stub for PrometheusMetricsService.real_slice_metrics(), used to drive
    Free5gcClient.current_slices() down either the Prometheus or the
    registered-UE-estimate branch without any network access."""

    def __init__(self, real_slices: list | None) -> None:
        self._real_slices = real_slices

    def real_slice_metrics(self):
        return self._real_slices


def _make_client(metrics: _FakeMetricsService) -> Free5gcClient:
    return Free5gcClient(
        webui_url="",
        username="admin",
        password="free5gc",
        plmn_id="00101",
        metrics=metrics,
    )


def test_current_slices_uses_prometheus_load_source_when_available() -> None:
    real_slices = SliceCatalog.slices_from_prometheus({1: 10.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 1, 2: 0, 3: 0, 4: 0})
    client = _make_client(_FakeMetricsService(real_slices))

    slices = client.current_slices(registered_ues=[], query_prometheus=True)

    assert slices == real_slices
    for item in slices:
        assert item["loadSource"] == "prometheus"


def test_current_slices_falls_back_to_estimated_load_source_when_prometheus_unavailable() -> None:
    client = _make_client(_FakeMetricsService(None))
    registered_ues = [{"PduSessions": [{"SNssai": {"Sst": 1}}]}]

    slices = client.current_slices(registered_ues=registered_ues, query_prometheus=True)

    assert len(slices) == 4
    for item in slices:
        assert item["loadSource"] == "estimated-from-registered-ues"


def test_slices_from_registered_ues_marks_load_as_estimated() -> None:
    """Fallback path (Prometheus unavailable, derived from WebUI registered UEs)
    must be labeled so the frontend can render it as an estimate, not a measurement."""
    registered_ues = [
        {"PduSessions": [{"SNssai": {"Sst": 1}}]},
        {"PduSessions": [{"SNssai": {"Sst": 2}}]},
    ]

    slices = SliceCatalog.slices_from_registered_ues(registered_ues)

    assert len(slices) == 4
    for item in slices:
        assert item["loadSource"] == "estimated-from-registered-ues"


def test_slices_from_registered_ues_marks_load_as_estimated_with_no_ues() -> None:
    """Even with zero registered UEs, the fallback path is still an estimate (of zero load),
    not an absence of data — it must carry the estimated label."""
    slices = SliceCatalog.slices_from_registered_ues([])

    assert len(slices) == 4
    for item in slices:
        assert item["loadSource"] == "estimated-from-registered-ues"


def test_slices_from_prometheus_marks_load_as_prometheus() -> None:
    """Real measured path (Prometheus query results) must be labeled distinctly
    from the estimated fallback so the UI can show it as measured."""
    raw = {1: 100.0, 2: 50.0, 3: 0.0, 4: 0.0}
    sessions = {1: 3, 2: 1, 3: 0, 4: 0}

    slices = SliceCatalog.slices_from_prometheus(raw, sessions)

    assert slices is not None
    assert len(slices) == 4
    for item in slices:
        assert item["loadSource"] == "prometheus"


def test_slices_from_prometheus_returns_none_when_all_values_missing() -> None:
    """When Prometheus has no data at all, the function still returns None (unchanged
    behavior) so callers fall through to the registered-UE estimate."""
    raw = {1: None, 2: None, 3: None, 4: None}
    sessions = {1: 0, 2: 0, 3: 0, 4: 0}

    slices = SliceCatalog.slices_from_prometheus(raw, sessions)

    assert slices is None


def test_slices_from_prometheus_marks_evidence_level_measured() -> None:
    raw = {1: 100.0, 2: 0.0, 3: 0.0, 4: 0.0}
    sessions = {1: 3, 2: 0, 3: 0, 4: 0}

    slices = SliceCatalog.slices_from_prometheus(raw, sessions)

    assert slices is not None
    for item in slices:
        assert item["evidenceLevel"] == "measured"


def test_slices_from_registered_ues_marks_evidence_level_estimated() -> None:
    slices = SliceCatalog.slices_from_registered_ues([])

    assert len(slices) == 4
    for item in slices:
        assert item["evidenceLevel"] == "estimated"


def test_slices_from_runtime_metrics_overwrites_load_source_and_evidence_level() -> None:
    """Regression test for the E2 defect: when slices_from_runtime_metrics overwrites
    load/dataSource with real pod-log-derived session counts, it must also update
    loadSource and evidenceLevel — otherwise a measured value keeps displaying the
    'estimated' badge, which is actively misleading."""
    backend = CityVerseBackendApp.__new__(CityVerseBackendApp)
    starting_slices = SliceCatalog.slices_from_registered_ues([])
    for item in starting_slices:
        assert item["loadSource"] == "estimated-from-registered-ues"
        assert item["evidenceLevel"] == "estimated"

    metrics = {
        "dataSource": "eks+ueransim-logs",
        "sliceSessions": {1: 2},
        "pduSessionCount": 2,
    }

    updated = backend.slices_from_runtime_metrics(starting_slices, metrics)

    touched = next(item for item in updated if item["sst"] == 1)
    assert touched["dataSource"] == "eks+ueransim-logs"
    assert touched["loadSource"] == "eks-runtime-logs"
    assert touched["evidenceLevel"] == "measured"


def test_default_slices_marks_selection_stage_configured() -> None:
    """default_slices() has no session evidence at all, so every slice is merely
    a platform-defined default — 'configured', not 'active-session'."""
    slices = SliceCatalog.default_slices()

    assert len(slices) == 4
    for item in slices:
        assert item["selectionStage"] == "configured"


def test_slices_from_registered_ues_marks_selection_stage_active_session_when_pdu_session_present() -> None:
    """A slice backed by a registered UE's PduSession SNssai has session evidence,
    so it should be tagged 'active-session' rather than merely 'configured'."""
    registered_ues = [{"PduSessions": [{"SNssai": {"Sst": 1}}]}]

    slices = SliceCatalog.slices_from_registered_ues(registered_ues)

    touched = next(item for item in slices if item["sst"] == 1)
    assert touched["selectionStage"] == "active-session"
    untouched = [item for item in slices if item["sst"] != 1]
    for item in untouched:
        assert item["selectionStage"] == "configured"


def test_slices_from_registered_ues_marks_selection_stage_configured_with_no_ues() -> None:
    """No registered UEs means no session evidence for any slice, so all stay 'configured'."""
    slices = SliceCatalog.slices_from_registered_ues([])

    assert len(slices) == 4
    for item in slices:
        assert item["selectionStage"] == "configured"


def test_slices_from_prometheus_marks_selection_stage_active_session_when_traffic_present() -> None:
    """A slice with nonzero Prometheus traffic has session evidence ('active-session');
    a slice with zero traffic is only 'configured'."""
    raw = {1: 100.0, 2: 0.0, 3: 0.0, 4: 0.0}
    sessions = {1: 3, 2: 0, 3: 0, 4: 0}

    slices = SliceCatalog.slices_from_prometheus(raw, sessions)

    assert slices is not None
    by_sst = {item["sst"]: item for item in slices}
    assert by_sst[1]["selectionStage"] == "active-session"
    assert by_sst[2]["selectionStage"] == "configured"
    assert by_sst[3]["selectionStage"] == "configured"
    assert by_sst[4]["selectionStage"] == "configured"


def test_slices_from_runtime_metrics_marks_selection_stage_active_session_when_sessions_present() -> None:
    """F5: slices_from_runtime_metrics overwrites load/sessions from live runtime data,
    but previously left selectionStage at its prior value (often 'configured'). When the
    runtime observed sessions>0 for a slice, that slice has session evidence and must be
    tagged 'active-session', consistent with the loadSource/evidenceLevel injected at the
    same point."""
    backend = CityVerseBackendApp.__new__(CityVerseBackendApp)
    starting_slices = SliceCatalog.default_slices()
    for item in starting_slices:
        assert item["selectionStage"] == "configured"

    metrics = {
        "dataSource": "eks+ueransim-logs",
        "sliceSessions": {1: 2, 2: 0},
        "pduSessionCount": 2,
    }

    updated = backend.slices_from_runtime_metrics(starting_slices, metrics)

    touched = next(item for item in updated if item["sst"] == 1)
    assert touched["sessions"] == 2
    assert touched["selectionStage"] == "active-session"

    zero_session_item = next(item for item in updated if item["sst"] == 2)
    assert zero_session_item["sessions"] == 0
    assert zero_session_item["selectionStage"] == "configured"


def test_slices_from_runtime_metrics_returns_unchanged_when_no_slice_sessions() -> None:
    """No sliceSessions in metrics means nothing was actually measured this round,
    so the previous loadSource/evidenceLevel labels must be left untouched."""
    backend = CityVerseBackendApp.__new__(CityVerseBackendApp)
    starting_slices = SliceCatalog.slices_from_registered_ues([])

    updated = backend.slices_from_runtime_metrics(starting_slices, {"dataSource": "unavailable"})

    assert updated == starting_slices
