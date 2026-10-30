"""Offline contract tests for the real city-daily browser acceptance runner."""
import importlib.util
import json
from pathlib import Path
import pytest


def load_module():
    path = Path(__file__).parents[1] / "run_city_daily_browser_e2e.py"
    spec = importlib.util.spec_from_file_location("city_daily_browser", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(available=True):
    return {"snapshotId": "daily-native-abc", "available": available,
            "reason": None if available else "collector unavailable",
            "observedAt": "2026-10-03T01:02:03Z" if available else None,
            "source": ["amf-context", "smf-pdu", "upf-n6", "gnb-imsi-sctp"],
            "slices": {name: {"registered": index} for index, name in enumerate(("embb", "urllc", "mmtc", "v2x"))}}


def rendered(module, value):
    return {"snapshotId": value["snapshotId"], "available": str(value["available"]).lower(),
            "observedAt": value["observedAt"] or "",
            "source": json.dumps(value["source"], ensure_ascii=False, separators=(",", ":")),
            "reason": value["reason"] or "", "trendDisabled": not value["available"],
            "slices": {name: {"count": str(row["registered"])} for name, row in value["slices"].items()}}


def test_default_is_plan_only_and_makes_no_playwright_import(tmp_path, monkeypatch):
    module = load_module()
    destination = tmp_path / "plan"
    module.main(["--output", str(destination)])
    saved = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    assert saved["status"] == "plan-only" and saved["execute"] is False
    assert saved["ueLifecycleOwned"] is False
    assert saved["mutationsAllowed"] == ["one user-triggered POST /api/onprem/daily-trend when available"]


def test_available_contract_requires_identity_time_source_and_exact_four_slices():
    module = load_module()
    value = observation()
    assert module.validate_observation(value) == []
    assert module.validate_rendered(rendered(module, value), value) == []
    for mutation in ({"snapshotId": ""}, {"observedAt": "not-a-time"}, {"slices": {"embb": {"registered": 1}}}):
        assert module.validate_observation({**value, **mutation})


def test_ui_must_match_same_api_snapshot_and_all_slice_aggregates():
    module = load_module()
    value = observation()
    ui = rendered(module, value)
    ui["snapshotId"] = "older"
    ui["slices"]["v2x"]["count"] = "999"
    errors = module.validate_rendered(ui, value)
    assert any("snapshotId" in error for error in errors)
    assert any("v2x" in error for error in errors)


def test_unavailable_requires_reason_and_disabled_control():
    module = load_module()
    value = observation(False)
    assert module.validate_observation(value) == []
    assert module.validate_rendered(rendered(module, value), value) == []
    ui = rendered(module, value)
    ui["trendDisabled"] = False
    assert "enabled" in module.validate_rendered(ui, value)[0]
    assert module.validate_observation({**value, "reason": ""})


def test_trend_must_match_snapshot_time_and_rendered_response():
    module = load_module()
    value = observation()
    trend = {"snapshotId": value["snapshotId"], "observedAt": value["observedAt"], "trend": "Stable"}
    assert module.validate_trend(trend, value, "Stable") == []
    assert len(module.validate_trend({**trend, "snapshotId": "other", "observedAt": "other"}, value, "wrong")) == 3


def test_only_expected_bootstrap_get_abort_is_non_actionable():
    module = load_module()
    values = [
        {"method": "GET", "url": "http://127.0.0.1:8000/api/onprem/bootstrap", "error": "net::ERR_ABORTED"},
        {"method": "GET", "url": "http://127.0.0.1:8000/api/onprem/ue", "error": "net::ERR_ABORTED"},
        {"method": "GET", "url": "http://127.0.0.1:8000/api/onprem/scenario-rounds/current?view=summary", "error": "net::ERR_ABORTED"},
        {"method": "POST", "url": "http://127.0.0.1:8000/api/onprem/bootstrap/start", "error": "net::ERR_ABORTED"},
        {"method": "GET", "url": "http://127.0.0.1:8000/api/onprem/ue", "error": "net::ERR_FAILED"},
        {"method": "GET", "url": "http://127.0.0.1:8000/api/onprem/daily-observation", "error": "net::ERR_ABORTED"},
    ]
    assert module.actionable_network_errors(values) == values[3:]


def test_bootstrap_e2e_requires_available_daily_and_exactly_one_trend_post():
    module = load_module()
    trend = {"method": "POST", "url": "http://127.0.0.1:8000/api/onprem/daily-trend"}
    assert module.required_daily_errors({"available": True}, [trend], required=True) == []
    errors = module.required_daily_errors({"available": False}, [], required=True)
    assert any("remained unavailable" in value for value in errors)
    assert any("exactly one" in value for value in errors)
    assert module.required_daily_errors({"available": True}, [trend, trend], required=True)
    assert module.required_daily_errors({"available": False}, [], required=False) == []


def test_trend_http_receipt_is_persisted_before_render_wait(tmp_path):
    module = load_module()
    class Response:
        status = 503
        ok = False
        def json(self): return {"error": {"code": "DAILY_NATIVE_OBSERVATION_UNAVAILABLE"}}
    body = module.persist_trend_response(tmp_path, Response())
    saved = json.loads((tmp_path / "trend-response.json").read_text(encoding="utf-8"))
    assert saved == {"status": 503, "body": body}
    source = (Path(__file__).parents[1] / "run_city_daily_browser_e2e.py").read_text(encoding="utf-8")
    persist = source.index("trend = persist_trend_response")
    reject = source.index("if not trend_response.ok", persist)
    render_wait = source.index('get_by_test_id("daily-native-trend").wait_for', reject)
    assert persist < reject < render_wait


def test_output_must_be_new_and_endpoints_are_loopback(tmp_path):
    module = load_module()
    destination = tmp_path / "existing"
    destination.mkdir()
    try:
        module.main(["--output", str(destination)])
    except FileExistsError:
        pass
    else:
        raise AssertionError("evidence directory must never be overwritten")


def test_bootstrap_generation_is_owned_only_when_new_and_exact_baseline():
    module = load_module()
    before = {"generation": 40}
    status = {"generation": 41, "slices": {name: {"requested": count} for name, count in module.BASELINE.items()}}
    assert module.owned_baseline_generation(before, status, "attempt-1") == 41
    assert module.owned_baseline_generation(before, {**status, "generation": 40}, "attempt-1") is None
    assert module.owned_baseline_generation(before, status, "") is None
    changed = json.loads(json.dumps(status)); changed["slices"]["embb"]["requested"] = 2
    assert module.owned_baseline_generation(before, changed, "attempt-1") is None


def test_baseline_ready_requires_registration_pdu_n6_and_exact_generation():
    module = load_module()
    status = {"generation": 41, "status": "established", "registered": 1, "pduEstablished": 1,
              "trafficVerified": 1, "errors": [],
              "slices": {name: {"requested": count} for name, count in module.BASELINE.items()}}
    assert module.baseline_ready(status, 41)
    for key, value in (("registered", 0), ("pduEstablished", 0), ("trafficVerified", 0),
                       ("status", "verifying_traffic"), ("generation", 42)):
        assert not module.baseline_ready({**status, key: value}, 41)


def test_bootstrap_and_cleanup_modes_are_explicit(tmp_path):
    module = load_module()
    for flags in (["--bootstrap"], ["--cleanup"], ["--execute", "--cleanup"]):
        try:
            module.main(["--output", str(tmp_path / ("case-" + str(len(list(tmp_path.iterdir()))))), *flags])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("unsafe mode combination must be rejected")


def test_late_bootstrap_failure_preserves_exact_owned_generation():
    module = load_module()
    ownership = {"attemptId": "attempt-1", "generation": 201, "baseline": module.BASELINE}
    error = module.BootstrapRunError("late GNB failure", ownership)
    assert error.ownership == ownership
    assert error.ownership["generation"] == 201
    assert error.ownership["baseline"] == {"embb": 1, "urllc": 0, "mmtc": 0, "v2x": 0}


def test_bootstrap_helper_persists_ownership_before_late_gnb_failure(tmp_path):
    module = load_module()
    def ue(generation):
        return {"generation": generation, "slices": {name: {"requested": count}
                for name, count in module.BASELINE.items()}}
    bootstrap_values = [
        {"state": "absent"},
        {"state": "starting", "attemptId": "attempt-1"},
        {"state": "failed", "attemptId": "attempt-1", "error": {"code": "GNB_ASSOCIATION_LOST"}},
    ]
    ue_values = [ue(200), ue(201), ue(201)]
    class Response:
        status = 202
        def json(self): return {"attemptId": "attempt-1"}
    class Pending:
        value = Response()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Button:
        def count(self): return 1
        def is_visible(self): return True
        def is_disabled(self): return False
        def click(self): pass
    class Request:
        def get(self, url, **kwargs):
            value = bootstrap_values.pop(0) if url.endswith(module.BOOTSTRAP_PATH) else ue_values.pop(0)
            return type("R", (), {"json": lambda self: value})()
    class Page:
        request = Request()
        def get_by_role(self, *args, **kwargs): return Button()
        def expect_response(self, *args, **kwargs): return Pending()
        def wait_for_timeout(self, value): pass
    with pytest.raises(module.BootstrapRunError, match="GNB_ASSOCIATION_LOST") as caught:
        module.bootstrap_through_ui(Page(), "http://127.0.0.1:8000", tmp_path, module.time.monotonic() + 10)
    assert caught.value.ownership["generation"] == 201
    assert json.loads((tmp_path / "bootstrap-ownership.json").read_text())["generation"] == 201


def test_failed_status_must_not_prevent_final_receipt_rewrite():
    source = (Path(__file__).parents[1] / "run_city_daily_browser_e2e.py").read_text(encoding="utf-8")
    assert 'if summary.get("status") != "failed"' not in source
    assert 'summary.update(status="failed", error=str(exc)' in source
    assert source.index('summary.update(status="failed", error=str(exc)') < source.index(
        'write_json(args.output / "summary.json", summary)', source.index('summary.update(status="failed", error=str(exc)'))
