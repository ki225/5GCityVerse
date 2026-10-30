"""Offline only: plans, evidence corruption and ownership; no live commissioning."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from backend.onprem.capacity.admission import CalibrationAdmission
from backend.onprem.traffic.workload import workload_metadata
from test.subsystems.onprem.test_onprem_calibration_manifest import trial, stamp, RELEASE
from test.subsystems.onprem.test_onprem_capacity_performance import complete_capacity_fixture, revise_receiver


@pytest.mark.parametrize('candidate', [25, 100])
def test_threshold_failure_after_normal_cleanup_keeps_case_and_browser_evidence(harness,monkeypatch,tmp_path,candidate):
    import sys
    from types import SimpleNamespace
    from contextlib import nullcontext
    reason='measuring_result:u15-ul:capacity_threshold_failed'
    events=[]
    class Page:
        def on(self,*args):pass
        def goto(self,*args):pass
        def wait_for_load_state(self,*args):pass
        def get_by_role(self,*args,**kwargs):return self
        def get_by_label(self,*args,**kwargs):return self
        def locator(self,*args,**kwargs):return self
        def count(self):return 0
        def fill(self,*args):pass
        def set_checked(self,*args):pass
        def check(self):pass
        def click(self):pass
        def wait_for_timeout(self,*args):pass
        def content(self):events.append('dom');return '<main>actual failed case DOM fixture</main>'
        def screenshot(self,*,path,**kwargs):
            events.append(Path(path).name);Path(path).write_bytes(b'offline-screenshot-fixture')
        def expect_response(self,*args):
            response=SimpleNamespace(status=200,request=SimpleNamespace(post_data_json={'requestId':'request'}),json=lambda:{'roundId':'owned'})
            return nullcontext(SimpleNamespace(value=response))
    page=Page();browser=SimpleNamespace(new_page=lambda **kwargs:page,close=lambda:events.append('close'))
    playwright=SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs:browser))
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=lambda:nullcontext(playwright),expect=None))
    snapshots=[{'roundId':'owned','stage':'results_ready','sequence':1,'executionOutcome':'passed'},
               {'roundId':'owned','stage':'completed','sequence':2,'executionOutcome':'passed','cleanupStatus':'verified'}]
    iterator=iter(snapshots)
    monkeypatch.setattr(harness,'read_summary',lambda *args:next(iterator))
    monkeypatch.setattr(harness,'read_snapshot',lambda *args:{'stage':'idle'})
    monkeypatch.setattr(harness,'capability_errors',lambda *args:[])
    monkeypatch.setattr(harness,'calibration_dom_errors',lambda *args,**kwargs:[])
    monkeypatch.setattr(harness,'read_calibration_dom',lambda *args:{'fixture':True,'workload':[{}]})
    monkeypatch.setattr(harness,'capture_result_dom',lambda *args:{'rendered':{'fixture':True},'uiReceivedAt':100})
    monkeypatch.setattr(harness,'capture_traffic_observation',lambda *args:{'errors':[], 'dom':[{'particles':0}],
        'snapshot':{'stage':'completed'}})
    monkeypatch.setattr(harness,'open_scenario_console',lambda *args:None)
    monkeypatch.setattr(harness,'confirmed_owner',lambda *args:'owned')
    monkeypatch.setattr(harness,'intent_rejection',lambda *args:None)
    monkeypatch.setattr(harness,'SummaryPollTracker',lambda *args:SimpleNamespace(plan=lambda snapshot,**kwargs:
        SimpleNamespace(persist_summary=False,full_boundary='result' if snapshot['sequence']==1 else 'cleanup')))
    monkeypatch.setattr(harness,'capture_full_boundary',lambda api,snapshot,*args,**kwargs:{'snapshot':snapshot,'fullReceivedAt':100})
    monkeypatch.setattr(harness,'audit_capacity',lambda *args:{'passed':False,'errors':[reason]})
    monkeypatch.setattr(harness,'asdict',lambda value:value)
    monkeypatch.setattr(harness,'captured_result_errors',lambda *args:([reason],[]))
    monkeypatch.setattr(harness,'released',lambda value:value.get('cleanupStatus')=='verified')
    monkeypatch.setattr(harness,'cleanup_errors',lambda *args:[])
    monkeypatch.setattr(harness,'clean_owned',lambda *args:events.append('cleanup-reconcile') or snapshots[-1])
    monkeypatch.setattr(harness,'submit_operator_none',lambda api,posted:(202,posted,{'roundId':'owned','requestId':posted['requestId']}))
    case={'id':'concentrated-mmtc','baselineUes':0,'events':dict.fromkeys(harness.EVENTS,0),'total':candidate}
    case['events']['factory_iot']=candidate
    args=SimpleNamespace(output=tmp_path,ui_url='http://127.0.0.1',api_url='http://127.0.0.1',candidate=candidate,timeout=30)
    summary={'executed':[{'case':'prior','status':'passed'}],'status':'planned'}
    with pytest.raises(RuntimeError,match='capacity_threshold_failed'):harness.execute(args,[case],summary)
    stored=json.loads((tmp_path/'summary.json').read_text())
    failed=stored['executed'][-1]
    assert failed['case']==case['id'] and failed['roundId']=='owned' and failed['status']=='failed'
    assert failed['stage']=='results_ready' and failed['cleanupObserved'] is True and failed['reason']==reason
    assert stored['error']==reason and stored['status']=='failed' and stored['coverageComplete'] is False
    assert json.loads((tmp_path/case['id']/'cleanup.json').read_text())==snapshots[-1]
    intent=json.loads((tmp_path/case['id']/'intent.json').read_text())
    assert intent['submission']==('operator-loopback-api' if candidate==100 else 'rendered-ui')
    if candidate==100:
        assert intent['request']['strategy']=='none' and intent['request']['events']['factory_iot']==100
        assert not (tmp_path/case['id']/'configured.png').exists()
    assert json.loads((tmp_path/case['id']/'capacity-audit.json').read_text())['passed'] is False
    for suffix in ('failed-result.png','failed-result.html','failed-result.json','failure.png','failure.html','failure.json'):
        assert (tmp_path/case['id']/suffix).exists()
    assert events.index('failed-result.png')<events.index('failure.png')<events.index('close')
    assert events[-1]=='close'


def test_capture_failure_retains_original_reason_and_explicit_unavailable(harness,monkeypatch,tmp_path):
    class Broken:
        def screenshot(self,**kwargs):raise TimeoutError()
        def content(self):raise RuntimeError()
    monkeypatch.setattr(harness,'capture_result_dom',lambda *args:(_ for _ in ()).throw(ValueError()))
    result=harness.preserve_failure_evidence(Broken(),tmp_path,case='mmtc',round_id='owned',phase='native_execution',
        reason='original threshold failure',snapshot={'stage':'completed'})
    assert result['reason']=='original threshold failure'
    assert result['captureErrors']=={'screenshot':'TimeoutError','html':'RuntimeError','dom':'ValueError'}
    assert json.loads((tmp_path/'failure.json').read_text())==result


@pytest.fixture
def harness(monkeypatch):
    folder = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("native_calibration_under_test", folder / "run_browser_native_calibration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_admission_waits_for_exact_hydration_and_fails_closed(harness, monkeypatch):
    from test.subsystems.onprem.test_onprem_capacity_100_shards import shard_snapshot
    snapshot = shard_snapshot()
    metadata = workload_metadata(snapshot['perUe'])
    expected = {'verifiedMaxUes': '0', 'candidateMaxUes': '100', 'admissionMaxUes': '100',
                'commissioningEnabled': 'true', 'profileId': harness.PROFILE_ID,
                'text': 'Unverified commissioning'}
    pending = {'admission': [{**expected, 'candidateMaxUes': ''}], 'workload': []}
    hydrated = {'admission': [expected], 'workload': [
        {**{key: str(value) for key, value in metadata.items()}, 'qosEffectClaimed': 'false'}]}
    views = iter([pending, hydrated])
    monkeypatch.setattr(harness, 'read_calibration_dom', lambda page: next(views))
    class Page:
        def __init__(self): self.waits = []
        def wait_for_timeout(self, ms): self.waits.append(ms)
    page = Page()
    dom, errors = harness.wait_for_configured_calibration_dom(page, snapshot, 100, require_workload=True, timeout_seconds=1)
    assert dom == hydrated and errors == [] and page.waits == [250]
    monkeypatch.setattr(harness, 'read_calibration_dom', lambda page: pending)
    dom, errors = harness.wait_for_configured_calibration_dom(page, snapshot, 100, require_workload=True, timeout_seconds=0)
    assert dom == pending and 'Rendered verified/candidate admission labels differ' in errors
    assert 'Missing unique visible frozen workload panel' in errors
    for malformed in ({}, {**hydrated['workload'][0], 'comparisonFingerprint': 'wrong'}):
        monkeypatch.setattr(harness, 'read_calibration_dom', lambda page: {
            'admission': [expected], 'workload': [malformed]})
        _, errors = harness.wait_for_configured_calibration_dom(page, snapshot, 100, require_workload=True, timeout_seconds=0)
        assert 'Rendered requested/applied workload differs from frozen plan' in errors


def test_operator_ui_preflight_rejects_changed_round_before_intent(harness):
    caps = {'start': True, **CalibrationAdmission(True, 100).capabilities(),
            'verifiedMaxUes': 0}
    before = {'roundId': 'old', 'sequence': 2194, 'stage': 'completed', 'capabilities': caps}
    assert harness.operator_preflight_snapshot_errors(before, dict(before)) == []
    for change in ({'roundId': 'new'}, {'sequence': 2195}, {'stage': 'preflight'}):
        assert 'Round changed during operator UI preflight' in harness.operator_preflight_snapshot_errors(
            before, {**before, **change})


def test_pre_measurement_future_clock_skew_retries_exact_receipts(harness, monkeypatch, tmp_path):
    observed_at = '2026-10-13T15:53:50.971414+00:00'
    snapshot = {'roundId': 'round', 'generation': 34, 'policyRevision': 0,
                'sequence': 2216, 'stage': 'registering',
                'observedAt': observed_at}
    dom = [{'roundId': 'round', 'generation': '34', 'policyRevision': '0',
            'sequence': '2216', 'stage': 'registering', 'observedAt': observed_at}]
    transient = {'snapshot': snapshot, 'uiReceivedAt': 1791906830.515782,
                 'dom': dom,
                 'errors': ['Native traffic map freshness differs from backend snapshot']}
    fresh = {'snapshot': snapshot, 'uiReceivedAt': 1791906831.1, 'dom': dom, 'errors': []}
    assert harness.retryable_ui_clock_skew(transient)
    receipts = iter([transient, fresh])
    paths = []
    monkeypatch.setattr(harness, 'capture_traffic_observation',
                        lambda api, page, path: paths.append(path.name) or next(receipts))
    class Page:
        def wait_for_timeout(self, ms): assert ms == 250
    assert harness.capture_traffic_with_clock_grace('api', Page(), tmp_path/'traffic.json') == fresh
    assert paths == ['traffic.json', 'traffic-clock-retry-01.json']


def test_clock_skew_grace_rejects_real_stale_and_identity_mismatch(harness):
    snapshot = {'roundId': 'round', 'generation': 34, 'policyRevision': 0,
                'sequence': 2216, 'stage': 'registering',
                'observedAt': '2026-10-13T15:53:50.971414+00:00'}
    dom = {'roundId': 'round', 'generation': '34', 'policyRevision': '0',
           'sequence': '2216', 'stage': 'registering', 'observedAt': snapshot['observedAt']}
    receipt = {'snapshot': snapshot, 'uiReceivedAt': 1791906830.515782,
               'dom': [dom],
               'errors': ['Native traffic map freshness differs from backend snapshot']}
    for changed in (
            {'uiReceivedAt': 1791906850},
            {'snapshot': {**snapshot, 'stage': 'measuring_baseline'}},
            {'dom': [{**dom, 'roundId': 'other'}]},
            {'dom': [{**dom, 'sequence': '2215'}]},
            {'dom': [{**dom, 'observedAt': '2026-10-13T15:53:40+00:00'}]},
            {'errors': ['Native traffic map is not the sampled backend snapshot']},
            {'errors': ['Native traffic map receiver count differs from backend join']}):
        assert not harness.retryable_ui_clock_skew({**receipt, **changed})


def test_observer_failure_cannot_cancel_registering_or_blocked_round(harness):
    current = {'roundId': 'owned', 'stage': 'results_ready', 'executionOutcome': 'passed'}
    assert harness.safe_observation_failure_cleanup(current, 'owned')
    for changed in ({'roundId': 'other'}, {'stage': 'registering'},
                    {'stage': 'cleaning', 'executionOutcome': 'cancelled'},
                    {'stage': 'cleaning', 'cleanupStatus': 'blocked'},
                    {'executionOutcome': 'failed'}):
        assert not harness.safe_observation_failure_cleanup({**current, **changed}, 'owned')


def result_fixture(harness):
    case = harness.cases(5)[0]
    result, _, cleanup, _ = trial(["video_streaming"] * 5, "round-test")
    result["capabilities"] = {"start": True, **CalibrationAdmission(True, 5).capabilities(),
                              "verifiedMaxUes": 0}
    complete_capacity_fixture(result)
    return result, case, cleanup


def test_summary_transport_explicit_view_and_projection_required(harness, monkeypatch):
    import io
    urls = []
    def fetch(url, timeout):
        urls.append((url, timeout))
        return io.StringIO(json.dumps({'viewSchema': 'cityverse.round-summary/v1', 'sequence': 1}))
    monkeypatch.setattr(harness, 'urlopen', fetch)
    assert harness.read_summary('http://127.0.0.1:8000')['sequence'] == 1
    assert urls == [('http://127.0.0.1:8000/api/onprem/scenario-rounds/current?view=summary', 15)]
    monkeypatch.setattr(harness, 'urlopen', lambda *a, **kw: io.StringIO('{}'))
    with pytest.raises(RuntimeError, match='summary projection'):
        harness.read_summary('http://127.0.0.1:8000')


def test_full_capture_uses_actual_response_completion_not_summary_time(harness, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(100, timezone.utc).isoformat()
    summary = {'roundId': 'owned', 'generation': 1, 'policyRevision': 0, 'sequence': 1,
        'stage': 'results_ready', 'executionOutcome': 'passed', 'observedAt': stamp,
        'viewSchema': 'cityverse.round-summary/v1'}
    full = {k: v for k, v in summary.items() if k != 'viewSchema'}
    clock = [101]
    def slow_full(api):
        clock[0] = 116  # Summary was fresh, full GET consumed remaining lifetime.
        return full
    monkeypatch.setattr(harness, 'read_snapshot', slow_full)
    monkeypatch.setattr(harness.time, 'time', lambda: clock[0])
    output = tmp_path / 'full-result.json'
    with pytest.raises(ValueError, match='expired'):
        harness.capture_full_boundary('unused', summary, 'result', output)
    preserved = json.loads(output.read_text())
    assert preserved['fullReceivedAt'] == 116 and preserved['snapshot']['observedAt'] == stamp


def test_stage_race_preserves_full_raw_response_without_faking_result(harness, monkeypatch, tmp_path):
    summary = {'roundId': 'owned', 'generation': 1, 'policyRevision': 0, 'sequence': 1,
        'stage': 'results_ready', 'executionOutcome': 'passed'}
    full = {**summary, 'stage': 'cleaning', 'sequence': 2, 'rawDiagnostic': {'original': True}}
    monkeypatch.setattr(harness, 'read_snapshot', lambda *a: full)
    output = tmp_path / 'full-crossed-boundary.json'
    with pytest.raises(ValueError, match='crossed'):
        harness.capture_full_boundary('unused', summary, 'result', output)
    assert json.loads(output.read_text())['snapshot'] == full


def test_valid_full_boundary_still_requires_unchanged_native_gates(harness, monkeypatch, tmp_path):
    result, case, _ = result_fixture(harness)
    # The join itself deliberately cannot certify stripped/forged native proof.
    result['connectivityEvidence'] = None
    monkeypatch.setattr(harness, 'read_snapshot', lambda *a: result)
    monkeypatch.setattr(harness.time, 'time', lambda: 100)
    result['observedAt'] = '1970-01-01T00:01:40+00:00'
    joined = harness.capture_full_boundary('unused', result, 'result', tmp_path / 'full.json')
    assert joined['requiresFullGateValidation'] is True
    assert harness.calibration_result_errors(joined['snapshot'], case, 100, 5)


def dom_fixture(result):
    workload = workload_metadata(result["perUe"])
    return {"admission": [{"verifiedMaxUes": "0", "candidateMaxUes": "5", "admissionMaxUes": "5",
        "commissioningEnabled": "true", "profileId": workload["profileId"], "text": "Unverified commissioning"}],
        "workload": [{**{k: str(v) for k, v in workload.items()}, "qosEffectClaimed": "false"}]}


def traffic_fixture(snapshot, *, scenario='video_streaming', particles=9, badge='FRESH RECEIVER EVIDENCE'):
    return [{'roundId': snapshot['roundId'], 'generation': str(snapshot['generation']),
        'sequence': str(snapshot['sequence']), 'policyRevision': str(snapshot['policyRevision']),
        'stage': snapshot['stage'], 'observedAt': snapshot['observedAt'], 'fresh': 'true',
        'selectedScenario': scenario, 'selectedReceiverRows': str(sum(
            row['scenario'] == scenario for row in snapshot['measurements'])),
        'particles': particles, 'badge': badge}]


def test_native_traffic_gate_requires_matching_raw_receiver_and_loading(harness):
    result, _, cleanup = result_fixture(harness)
    now = 106
    assert harness._receiver_rows(result, now)
    flowing = traffic_fixture(result)
    assert harness.traffic_dom_errors(flowing, result, now) == []
    waiting = deepcopy(result)
    waiting['measurements'] = []
    loading = traffic_fixture(waiting, particles=0, badge='AWAITING RECEIVER EVIDENCE')
    assert harness.traffic_dom_errors(loading, waiting, now) == []
    assert harness.traffic_dom_errors(flowing, waiting, now)
    assert harness.traffic_dom_errors(loading, result, now)
    wrong_sequence = deepcopy(flowing)
    wrong_sequence[0]['sequence'] = str(result['sequence'] - 1)
    assert harness.traffic_dom_errors(wrong_sequence, result, now) == [
        'Native traffic map is not the sampled backend snapshot']
    wrong_source = deepcopy(result)
    wrong_source['measurements'][0]['source'] = 'client'
    assert harness.traffic_dom_errors(flowing, wrong_source, now)
    stale_receiver = deepcopy(result)
    for measurement in stale_receiver['measurements']:
        measurement['observedAt'] = '1970-01-01T00:01:00+00:00'
    assert harness._receiver_rows(stale_receiver, now) == []
    assert harness.traffic_dom_errors(flowing, stale_receiver, now)
    assert harness.traffic_dom_errors(loading, waiting, now + 16)
    terminal_snapshot = {**result, **cleanup}
    terminal = traffic_fixture(terminal_snapshot, particles=0, badge='AWAITING RECEIVER EVIDENCE')
    terminal[0]['fresh'] = 'true'
    terminal[0]['selectedReceiverRows'] = '0'
    assert harness.traffic_dom_errors(terminal, terminal_snapshot, 109) == []
    assert harness.traffic_dom_errors(flowing, terminal_snapshot, 109)


def test_native_traffic_map_badge_cannot_claim_other_scenario_receiver(harness):
    result, _, _ = result_fixture(harness)
    other = traffic_fixture(result, scenario='web_browsing', particles=0, badge='FRESH RECEIVER EVIDENCE')
    other[0]['selectedReceiverRows'] = '0'
    assert harness.traffic_dom_errors(other, result, 106) == [
        'Native traffic loading badge claims receiver evidence without selected flow']


def test_native_traffic_receipt_keeps_distinct_raw_and_dom_times(harness, monkeypatch, tmp_path):
    result, _, _ = result_fixture(harness)
    monkeypatch.setattr(harness, 'read_snapshot', lambda _: result)
    monkeypatch.setattr(harness, 'read_traffic_dom', lambda _: traffic_fixture(result))
    times = iter((105, 106))
    monkeypatch.setattr(harness.time, 'time', lambda: next(times))
    path = tmp_path / 'traffic.json'
    receipt = harness.capture_traffic_observation('offline', object(), path)
    assert receipt['errors'] == []
    assert receipt['fullReceivedAt'] == 105 and receipt['uiReceivedAt'] == 106
    assert json.loads(path.read_text()) == receipt


@pytest.mark.parametrize("candidate", [5, 10, 25, 50, 100])
def test_plan_only_never_imports_browser_or_reads_native(harness, monkeypatch, tmp_path, candidate):
    monkeypatch.setattr(harness, "read_snapshot", lambda *_: pytest.fail("No native API in planning"))
    monkeypatch.setattr(harness, "execute", lambda *_: pytest.fail("No execution without --execute"))
    output = tmp_path / "plan"
    harness.main(["--candidate", str(candidate), "--output", str(output)])
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "plan-only" and summary["executed"] == []
    assert summary["verifiedMaxUes"] == 0 and not summary["promotionEligible"]
    assert not summary["coverageComplete"] and "not-96" in summary["scope"]
    assert len(summary["planned"]) == 5
    plan = json.loads((output / "plan.json").read_text())
    assert plan["capacityCriteria"] == harness.criteria_metadata()
    assert not plan["protocolPredeclarationVerified"] and plan["declaredAt"]
    embb = next(c for c in summary["planned"] if c["id"] == "concentrated-embb")
    assert embb["baselineUes"] == 0 and embb["events"]["video_streaming"] == candidate
    for case in summary["planned"]:
        assert case["strategy"] == "none" and case["total"] == candidate
        assert case["baselineUes"] + sum(case["events"].values()) == candidate
    mixed = next(c for c in summary["planned"] if c["id"] == "mixed")
    assert mixed["baselineUes"] > 0 and all(mixed["events"][k] > 0 for k in ("realtime_control", "connected_traffic", "factory_iot"))
    if candidate >= 10:
        assert all(mixed["events"].values())


def test_optional_zero_subset_does_not_claim_coverage(harness, tmp_path):
    output = tmp_path / "zero"
    harness.main(["--candidate", "5", "--include-zero", "--case", "zero", "--output", str(output)])
    summary = json.loads((output / "summary.json").read_text())
    assert len(summary["planned"]) == 1 and summary["planned"][0]["total"] == 0
    assert not summary["coverageComplete"]


def test_operator_100_none_intent_uses_loopback_round_route(harness, monkeypatch):
    import io
    case = harness.cases(100)[-1]
    posted = harness.operator_intent(case)
    assert posted['strategy'] == 'none' and posted['baselineUes'] + sum(posted['events'].values()) == 100
    seen = []
    class Response(io.StringIO):
        status = 202
    def fetch(request, timeout):
        seen.append((request, timeout))
        return Response(json.dumps({'roundId': 'round-100', 'requestId': posted['requestId']}))
    monkeypatch.setattr(harness, 'urlopen', fetch)
    status, submitted, accepted = harness.submit_operator_none('http://127.0.0.1:8000', posted)
    assert status == 202 and submitted == posted and accepted['requestId'] == posted['requestId']
    request, timeout = seen[0]
    assert request.full_url == 'http://127.0.0.1:8000/api/onprem/scenario-rounds'
    assert request.get_method() == 'POST' and timeout == 30
    assert json.loads(request.data) == posted


@pytest.mark.parametrize("url", ["https://localhost", "http://remote", "http://127.0.0.1.evil", "http://user@localhost",
    "http://localhost/?token=secret", "http://localhost/#fragment", "http://localhost:0", "http://localhost:99999"])
def test_nonloopback_or_credentialed_origins_rejected(harness, tmp_path, url):
    with pytest.raises(SystemExit):
        harness.main(["--candidate", "5", "--api-url", url, "--output", str(tmp_path / "bad")])
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("args", [["--candidate", "1"], ["--candidate", "51"],
    ["--candidate", "5", "--case", "zero"], ["--candidate", "5", "--timeout", "1"]])
def test_invalid_plan_does_not_create_output(harness, tmp_path, args):
    with pytest.raises(SystemExit):
        harness.main([*args, "--output", str(tmp_path / "bad")])
    assert not (tmp_path / "bad").exists()


def test_exact_native_workload_and_rendered_contract(harness):
    result, case, cleanup = result_fixture(harness)
    assert harness.calibration_result_errors(result, case, 106, 5) == []
    assert harness.calibration_dom_errors(dom_fixture(result), result, 5, result=True) == []
    assert harness.cleanup_errors(cleanup, 109) == []
    workload = result["actions"][0]["workload"]
    assert workload["requestedUplinkKbps"] == 20000
    assert workload["appliedUplinkKbps"] == 10000 and workload["capReason"]


@pytest.mark.parametrize("key,value", [("verifiedMaxUes", 1), ("verifiedMaxUes", 5), ("verifiedMaxUes", True),
    ("candidateMaxUes", 10), ("admissionMaxUes", 50), ("commissioningEnabled", False),
    ("staticPolicy", True), ("aiPolicy", True), ("commissioningProfile", "custom"),
    ("commissioningLabel", "Verified 50 UE capacity")])
def test_capability_promotion_or_policy_claims_rejected(harness, key, value):
    result, _, _ = result_fixture(harness)
    result["capabilities"][key] = value
    assert harness.capability_errors(result, 5)


def test_100_ue_commissioning_rejects_verified_one_in_api_and_dom(harness):
    caps = {"start": True, **CalibrationAdmission(True, 100).capabilities(),
            "verifiedMaxUes": 0}
    assert harness.capability_errors({"capabilities": caps}, 100) == []
    assert harness.capability_errors({"capabilities": {**caps, "verifiedMaxUes": 1}}, 100)
    admission = {"verifiedMaxUes": "0", "candidateMaxUes": "100",
                 "admissionMaxUes": "100", "commissioningEnabled": "true",
                 "profileId": harness.PROFILE_ID, "text": "Unverified commissioning"}
    assert harness.calibration_dom_errors({"admission": [admission]}, {}, 100) == []
    assert harness.calibration_dom_errors({"admission": [
        {**admission, "verifiedMaxUes": "1"}]}, {}, 100)


@pytest.mark.parametrize("mutation", ["rate", "metadata", "missing_direction", "cookie", "pfcp", "future", "ui_metric", "baseline_incomplete"])
def test_raw_or_frozen_workload_mutation_fails(harness, mutation):
    result, case, _ = result_fixture(harness)
    window = result["actions"][-1]
    if mutation == "rate": window["offeredBySupi"][result["perUe"][0]["supi"]] += 1
    if mutation == "metadata": window["workload"]["comparisonFingerprint"] = "0" * 64
    if mutation == "missing_direction": window["reports"].pop()
    if mutation == "cookie": window["reports"][0]["server"]["start"]["cookie"] = "a" * 36
    if mutation == "pfcp": result["perUe"][0]["pfcpSeid"] = "999"
    if mutation == "future": window["reports"][0]["finishedAt"] = 107
    if mutation == "ui_metric": result["measurements"][0]["receivedUplinkMbps"] = 999
    if mutation == "baseline_incomplete": result["actions"][0]["installed"] = False
    assert harness.calibration_result_errors(result, case, 106, 5)


@pytest.mark.parametrize("section,key,value", [("admission", "verifiedMaxUes", "1"),
    ("admission", "verifiedMaxUes", "5"),
    ("admission", "text", "Verified capacity"), ("workload", "appliedCombinedKbps", "100000"),
    ("workload", "comparisonFingerprint", "wrong"), ("workload", "qosEffectClaimed", "true")])
def test_dom_does_not_turn_configuration_into_capacity_or_qos(harness, section, key, value):
    result, _, _ = result_fixture(harness)
    dom = dom_fixture(result)
    dom[section][0][key] = value
    assert harness.calibration_dom_errors(dom, result, 5, result=True)


@pytest.mark.parametrize("check", RELEASE)
def test_every_cleanup_check_required(harness, check):
    _, _, cleanup = result_fixture(harness)
    cleanup["releaseProof"][check] = False
    assert harness.cleanup_errors(cleanup, 109)


def test_stale_cleanup_and_foreign_lost_response_never_establish_success(harness):
    _, _, cleanup = result_fixture(harness)
    assert harness.cleanup_errors(cleanup, 130)
    assert harness.confirmed_owner({"roundId": "other", "requestId": "foreign"}, "ours", "previous") is None
    assert harness.confirmed_owner({"roundId": "previous", "requestId": "ours"}, "ours", "previous") is None
    assert harness.confirmed_owner({"roundId": "owned", "requestId": "ours"}, "ours", "previous") == "owned"


def test_criteria_receipt_exists_before_execute(harness, monkeypatch, tmp_path):
    output = tmp_path / "prospective"
    def execution(args, selected, summary):
        plan = json.loads((output / "plan.json").read_text())
        assert plan["capacityCriteria"] == harness.criteria_metadata()
        assert plan["executed"] == [] and plan["planned"][0]["id"] == "mixed"
        assert not plan["promotionEligible"] and not plan["protocolPredeclarationVerified"]
    monkeypatch.setattr(harness, "execute", execution)
    harness.main(["--candidate", "5", "--case", "mixed", "--execute", "--output", str(output)])


@pytest.mark.parametrize("stage", [0, 1])
def test_capacity_loss_gate_applies_to_both_stages(harness, stage):
    result, case, _ = result_fixture(harness)
    revise_receiver(result["actions"][stage]["reports"][0], lost=100)
    assert harness.calibration_result_errors(result, case, 106, 5)
