"""Plan-only by default: operator-selected NONE calibration, not 96-case acceptance.

--execute uses the rendered UI for 5..50 UE; 100 UE uses an explicit operator
API submission because the normal UI exposes AI only. Both paths preserve raw
API, DOM, intent, workload and cleanup receipts, including failures. This
harness never promotes capability.
"""
import argparse
import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.capacity.receipts import _native_and_windows
from backend.onprem.capacity.performance import audit_capacity, criteria_metadata
from backend.onprem.traffic.workload import plan_for_stage, validate_record_workload, workload_metadata
from backend.onprem.traffic.planning import PROFILE_ID
from run_browser_scenario_matrix import EVENTS, LABELS, read_snapshot, released, result_errors, write_json
from run_browser_native_smoke import (clean_owned, confirmed_owner, open_scenario_console,
    read_rendered_result, record_failure, rendered_result_errors, zero_result_valid)
from calibration_polling import SummaryPollTracker, join_full_boundary


def read_summary(api):
    with urlopen(api + '/api/onprem/scenario-rounds/current?view=summary', timeout=15) as response:
        value = json.load(response)
    if value.get('viewSchema') != 'cityverse.round-summary/v1':
        raise RuntimeError('Explicit summary projection missing')
    return value


def capture_result_dom(page):
    """Timestamp actual DOM completion independently of the raw GET receipt."""
    rendered, dom = read_rendered_result(page), read_calibration_dom(page)
    return {'rendered': rendered, 'calibration': dom, 'uiReceivedAt': time.time()}


def preserve_failure_evidence(page, destination, *, case, round_id, phase, reason, snapshot=None, label='failure'):
    """Best-effort read-only browser evidence; never replace the causal error.

    The first failed result is captured while visible, independently from a
    later cleanup/exception DOM. Capture failure is evidence of unavailability.
    """
    receipt={'case':case,'roundId':round_id,'phase':phase,'reason':str(reason),
        'stage':snapshot.get('stage') if isinstance(snapshot,dict) else None,
        'sequence':snapshot.get('sequence') if isinstance(snapshot,dict) else None,
        'capturedAt':time.time(),'snapshot':snapshot,'captureErrors':{}}
    def attempt(name,callback):
        try:callback()
        except Exception as error:receipt['captureErrors'][name]=type(error).__name__
    attempt('directory',lambda:destination.mkdir(parents=True,exist_ok=True))
    attempt('screenshot',lambda:page.screenshot(path=str(destination/(label+'.png')),full_page=True,timeout=5000))
    attempt('html',lambda:(destination/(label+'.html')).write_text(page.content(),encoding='utf-8'))
    attempt('dom',lambda:receipt.update(dom=capture_result_dom(page)))
    attempt('receipt',lambda:write_json(destination/(label+'.json'),receipt))
    return receipt


def capture_full_boundary(api, summary, boundary, destination, *, page=None):
    full = read_snapshot(api)
    received = time.time()  # AFTER complete full-response read and JSON parse.
    receipt = {'snapshot': full, 'fullReceivedAt': received,
               'triggerSequence': summary['sequence'], 'boundary': boundary}
    try:
        # Capture the live UI before disk serialization or the capacity audit.
        # Never transfer the raw receipt timestamp onto this later DOM sample.
        if page is not None and boundary == 'result' and full.get('stage') == 'results_ready':
            receipt.update(capture_result_dom(page))
        joined = join_full_boundary(summary, full, boundary=boundary, received_at=received)
        return {**receipt, **joined}
    finally:
        # Also retain raw evidence when the GET crossed a boundary or was stale.
        write_json(destination, receipt)


def captured_result_errors(capture, case, candidate):
    """Validate immutable receipts at their own acquisition times, not disk time."""
    snapshot = capture['snapshot']
    raw_errors = calibration_result_errors(snapshot, case, capture['fullReceivedAt'], candidate)
    ui_errors = rendered_result_errors(capture['rendered'], snapshot, capture['uiReceivedAt'])
    ui_errors += calibration_dom_errors(capture['calibration'], snapshot, candidate, result=True)
    return raw_errors, ui_errors


def cases(candidate, include_zero=False):
    if type(candidate) is not int or candidate not in (5, 10, 25, 50, 100):
        raise ValueError("Candidate must be 5, 10, 25, 50 or 100")
    def case(identifier, groups):
        return {"id": identifier, "strategy": "none", "baselineUes": groups.count("web_browsing"),
                "events": {event: groups.count(event) for event in EVENTS}, "total": len(groups)}
    result = [case("concentrated-" + slice_name, [group] * candidate) for slice_name, group in
              (("embb", "video_streaming"), ("urllc", "realtime_control"), ("v2x", "connected_traffic"), ("mmtc", "factory_iot"))]
    groups = ["web_browsing", "realtime_control", "connected_traffic", "factory_iot", "video_streaming"]
    result.append(case("mixed", [groups[i % len(groups)] for i in range(candidate)]))
    return ([case("zero", [])] if include_zero else []) + result


def capability_errors(snapshot, candidate):
    caps = snapshot.get("capabilities", {})
    expected = {"start": True, "verifiedMaxUes": 0, "commissioningEnabled": True,
        "candidateMaxUes": candidate, "admissionMaxUes": candidate,
        "commissioningProfile": PROFILE_ID, "commissioningLabel": "Unverified commissioning",
        "staticPolicy": False, "aiPolicy": False}
    return ["Operator commissioning capability mismatch: " + key for key, value in expected.items()
            if type(caps.get(key)) is not type(value) or caps.get(key) != value]


def calibration_result_errors(snapshot, case, now, candidate):
    errors = capability_errors(snapshot, candidate) + result_errors(snapshot, case, now)
    if errors:
        return errors
    try:
        if candidate < 100:
            windows = [a for a in snapshot["actions"] if a.get("kind") == "traffic_window" and a.get("mode") == "batch"]
            if len(windows) != 2 or {a.get("stage") for a in windows} != {"measuring_baseline", "measuring_result"}:
                raise ValueError("Exact baseline and result windows required")
            for record in windows:
                validate_record_workload(record, snapshot, allow_legacy=False)
            baseline = next(a for a in windows if a["stage"] == "measuring_baseline")
            plan_for_stage({**snapshot, "stage": "measuring_result", "actions": [baseline]})
        # For 100 UE, _native_and_windows validates the complete shard/aggregate
        # pair and frozen workload. A single shard cannot represent all UEs.
        plan = workload_metadata(snapshot["perUe"])
        coverage = _native_and_windows(snapshot, {
            "resultCapturedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "workloadFingerprint": plan["comparisonFingerprint"]}, candidate)
        expected = "mixed" if case["id"] == "mixed" else case["id"].replace("concentrated-", "concentrated:")
        if coverage != expected:
            raise ValueError("Wrong native distribution")
    except Exception:
        errors.append("Raw native windows, frozen workload or distribution validation failed")
    capacity = audit_capacity(snapshot)
    if not capacity.passed:
        errors.extend(capacity.errors)
    return errors


def read_calibration_dom(page):
    def rows(test_id):
        return page.get_by_test_id(test_id).filter(visible=True).evaluate_all(
            "els => els.map(el => ({...el.dataset, text: el.innerText}))")
    return {"admission": rows("onprem-calibration-admission"), "workload": rows("onprem-round-workload")}


def read_traffic_dom(page):
    """Read the map and its visible evidence from one rendered document."""
    maps = page.get_by_test_id('onprem-native-topology').filter(visible=True)
    return maps.evaluate_all("els => els.map(el => ({...el.dataset, particles: el.querySelectorAll('[data-testid=\"scenario-flow-particle\"]').length, badge: el.querySelector('[data-testid=\"onprem-traffic-receiver-badge\"]')?.innerText ?? null}))")


def _fresh(value, now):
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if observed.tzinfo is None:
            return False
        age = now - observed.timestamp()
        return -1 <= age <= 15
    except (AttributeError, TypeError, ValueError):
        return False


def _receiver_rows(snapshot, now):
    """Mirror the UI's conservative receiver join; never infer from allocations."""
    if not snapshot.get('roundId') or not _fresh(snapshot.get('observedAt'), now) or snapshot.get('stage') not in (
            'measuring_baseline', 'applying_policy', 'verifying_policy', 'measuring_result', 'results_ready'):
        return []
    keys = ('scenario', 'supi', 'sessionId', 'podUid', 'ueIp', 'tun', 'upf')
    owners = {}
    for binding in snapshot.get('perUe', []):
        supi = binding.get('supi')
        if supi in owners or any(type(binding.get(key)) is not str or not binding[key] for key in keys):
            return []
        owners[supi] = binding
    seen, rows = set(), []
    for row in snapshot.get('measurements', []):
        supi = row.get('supi')
        owner = owners.get(supi)
        if owner is None or supi in seen or any(row.get(key) != owner.get(key) for key in keys):
            return []
        seen.add(supi)
        if (row.get('roundId') != snapshot['roundId'] or row.get('generation') != snapshot.get('generation')
                or row.get('policyRevision') != snapshot.get('policyRevision') or row.get('source') != 'receiver'
                or not _fresh(row.get('observedAt'), now)):
            continue
        rates = (row.get('receivedUplinkMbps'), row.get('receivedDownlinkMbps'))
        if any(type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0 for rate in rates if rate is not None):
            continue
        if any(rate is not None and rate > 0 for rate in rates):
            rows.append(row)
    return rows


def traffic_dom_errors(dom, snapshot, now):
    """Require a single map at the exact backend sequence before judging motion."""
    if not isinstance(dom, list) or len(dom) != 1:
        return ['Missing unique visible native traffic map']
    row = dom[0]
    def shown(value):
        return '' if value is None else str(value)
    expected = {'roundId': snapshot.get('roundId') or '', 'generation': shown(snapshot.get('generation')),
        'sequence': shown(snapshot.get('sequence')), 'policyRevision': shown(snapshot.get('policyRevision')),
        'stage': snapshot.get('stage') or '', 'observedAt': snapshot.get('observedAt') or ''}
    if any(row.get(key) != value for key, value in expected.items()):
        return ['Native traffic map is not the sampled backend snapshot']
    scenario = row.get('selectedScenario')
    if scenario not in ('baseline', *EVENTS):
        return ['Native traffic map selection is unknown']
    fresh = _fresh(snapshot.get('observedAt'), now)
    if row.get('fresh') != str(fresh).lower():
        return ['Native traffic map freshness differs from backend snapshot']
    matching = [r for r in _receiver_rows(snapshot, now) if r.get('scenario') == scenario]
    if row.get('selectedReceiverRows') != str(len(matching)):
        return ['Native traffic map receiver count differs from backend join']
    flow = bool(matching)
    if type(row.get('particles')) is not int or (row['particles'] > 0) != flow:
        return ['Native traffic motion differs from fresh selected receiver evidence']
    if not flow and row.get('badge') != 'AWAITING RECEIVER EVIDENCE':
        return ['Native traffic loading badge claims receiver evidence without selected flow']
    if flow and row.get('badge') != 'FRESH RECEIVER EVIDENCE':
        return ['Native traffic badge omits fresh selected receiver evidence']
    return []


def capture_traffic_observation(api, page, destination):
    snapshot = read_snapshot(api)
    full_received = time.time()
    dom = read_traffic_dom(page)
    ui_received = time.time()
    errors = traffic_dom_errors(dom, snapshot, ui_received)
    receipt = {'snapshot': snapshot, 'dom': dom, 'fullReceivedAt': full_received,
        'uiReceivedAt': ui_received, 'errors': errors}
    write_json(destination, receipt)
    return receipt


def retryable_ui_clock_skew(receipt):
    """A same-sequence pre-measurement timestamp can precede the UI's 1s clock tick."""
    snapshot = receipt.get('snapshot', {})
    if (receipt.get('errors') != ['Native traffic map freshness differs from backend snapshot']
            or snapshot.get('stage') not in {'preflight', 'allocating', 'registering',
                                              'establishing_pdu', 'verifying_connectivity'}):
        return False
    rows = receipt.get('dom')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    expected = {'roundId': snapshot.get('roundId') or '',
                'generation': str(snapshot.get('generation')) if snapshot.get('generation') is not None else '',
                'sequence': str(snapshot.get('sequence')) if snapshot.get('sequence') is not None else '',
                'policyRevision': str(snapshot.get('policyRevision')) if snapshot.get('policyRevision') is not None else '',
                'stage': snapshot.get('stage') or '',
                'observedAt': snapshot.get('observedAt') or ''}
    if any(row.get(key) != value for key, value in expected.items()):
        return False
    try:
        observed = datetime.fromisoformat(snapshot['observedAt'].replace('Z', '+00:00')).timestamp()
        skew = observed - receipt['uiReceivedAt']
        return 0 <= skew <= 1
    except (KeyError, TypeError, ValueError):
        return False


def capture_traffic_with_clock_grace(api, page, destination, *, retry_window_seconds=2):
    """Bound retry scheduling; each transport retains its own I/O timeout."""
    receipt = capture_traffic_observation(api, page, destination)
    if not retryable_ui_clock_skew(receipt):
        return receipt
    deadline, attempt = time.monotonic() + retry_window_seconds, 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        attempt += 1
        retry_path = destination.with_name(f'{destination.stem}-clock-retry-{attempt:02d}.json')
        receipt = capture_traffic_observation(api, page, retry_path)
        if not retryable_ui_clock_skew(receipt):
            break
    return receipt


def safe_observation_failure_cleanup(snapshot, round_id):
    """Only a published result may be explicitly cleaned after observer failure."""
    return (isinstance(snapshot, dict) and snapshot.get('roundId') == round_id
            and snapshot.get('stage') == 'results_ready'
            and snapshot.get('executionOutcome') == 'passed')


def calibration_dom_errors(dom, snapshot, candidate, *, result=False):
    errors = []
    rows = dom.get("admission")
    if not isinstance(rows, list) or len(rows) != 1:
        return ["Missing unique visible commissioning admission label"]
    expected = {"verifiedMaxUes": "0", "candidateMaxUes": str(candidate), "admissionMaxUes": str(candidate),
                "commissioningEnabled": "true", "profileId": PROFILE_ID}
    if any(rows[0].get(k) != v for k, v in expected.items()) or "Unverified commissioning" not in rows[0].get("text", ""):
        errors.append("Rendered verified/candidate admission labels differ")
    if result:
        rows = dom.get("workload")
        if not isinstance(rows, list) or len(rows) != 1:
            return errors + ["Missing unique visible frozen workload panel"]
        try:
            metadata = workload_metadata(snapshot["perUe"])
            keys = ("profileId", "comparisonFingerprint", "budgetKbpsPerDirection", "requestedUplinkKbps",
                    "requestedDownlinkKbps", "requestedCombinedKbps", "appliedUplinkKbps",
                    "appliedDownlinkKbps", "appliedCombinedKbps")
            if any(rows[0].get(key) != str(metadata[key]) for key in keys) or rows[0].get("qosEffectClaimed") != "false":
                errors.append("Rendered requested/applied workload differs from frozen plan")
        except Exception:
            errors.append("Rendered workload contract is malformed")
    return errors


def wait_for_configured_calibration_dom(page, snapshot, candidate, *, require_workload=False, timeout_seconds=10):
    """Wait only for the exact rendered admission state to hydrate."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        dom = read_calibration_dom(page)
        errors = calibration_dom_errors(dom, snapshot, candidate, result=require_workload)
        if not errors or time.monotonic() >= deadline:
            return dom, errors
        page.wait_for_timeout(250)


def operator_preflight_snapshot_errors(before, after):
    errors = capability_errors(after, 100)
    if (after.get('roundId'), after.get('sequence'), after.get('stage')) != (
            before.get('roundId'), before.get('sequence'), before.get('stage')):
        errors.append('Round changed during operator UI preflight')
    return errors


def cleanup_errors(snapshot, now):
    try:
        proof = snapshot["releaseProof"]
        observed = datetime.fromisoformat(proof["observedAt"].replace("Z", "+00:00"))
        if (not released(snapshot) or proof.get("scope") != "known-round-and-managed-runtime"
                or observed.tzinfo is None or not 0 <= now - observed.timestamp() < 15):
            return ["Cleanup lacks fresh complete known-scope release proof"]
    except (KeyError, TypeError, ValueError):
        return ["Cleanup proof is malformed"]
    return []


def loopback(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
                and parsed.username is None and parsed.password is None and parsed.path in ("", "/")
                and not parsed.query and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def intent_rejection(status, accepted, owned_round, posted, case):
    if status != 202:
        # Never copy arbitrary server text into public diagnostics; raw receipt is retained separately.
        code = accepted.get('error') if isinstance(accepted, dict) else None
        code = code if code in ('ROUND_CONFLICT', 'POLICY_OUTCOME_UNKNOWN', 'SCENARIO_CAPABILITY_UNAVAILABLE', 'FORBIDDEN', 'UNAUTHORIZED') else 'UNCLASSIFIED_API_ERROR'
        detail = ('Fresh known-scope resource baseline is unavailable'
                  if code == 'ROUND_CONFLICT' and accepted.get('detail') == 'Fresh known-scope resource baseline is unavailable'
                  else 'Request was not accepted; inspect the saved intent receipt')
        return f'Round API rejection: HTTP {status}; {code}; {detail}'
    if not owned_round:
        return 'Round API response did not confirm request ownership'
    if any(posted.get(k) != case[k] for k in ('baselineUes', 'events', 'strategy')):
        return 'Round API request allocation or strategy differs from planned case'
    return None


def operator_intent(case):
    return {'requestId': str(uuid4()), 'baselineUes': case['baselineUes'],
            'events': case['events'], 'strategy': 'none'}


def submit_operator_none(api, posted):
    """Submit one 100 UE calibration round via the same server route as the UI."""
    request = Request(api + '/api/onprem/scenario-rounds',
        data=json.dumps(posted).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, posted, json.load(response)
    except HTTPError as error:
        try:
            accepted = json.load(error)
        except (ValueError, UnicodeDecodeError):
            accepted = {}
        return error.code, posted, accepted


def execute(args, selected, summary):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1200})
        browser_errors, owned_round, pending_request, previous_round = [], None, None, None
        phase = "preflight_navigation"
        active_case, destination, round_id, latest_snapshot, first_failure = None, args.output, None, None, None
        def track(request):
            nonlocal pending_request
            if request.method == "POST" and urlsplit(request.url).path == "/api/onprem/scenario-rounds":
                pending_request = request.post_data_json
        page.on("request", track)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on("console", lambda item: browser_errors.append(item.text) if item.type == "error" else None)
        summary["status"] = "running"
        try:
            page.goto(args.ui_url + ('/?operator-calibration=1' if args.candidate == 100 else ''))
            page.wait_for_load_state("networkidle")
            language = page.get_by_role("button", name=re.compile("English"))
            if language.count():
                language.click()
            for case in selected:
                active_case,round_id,latest_snapshot,first_failure=case['id'],None,None,None
                destination=args.output/case['id']
                phase = "case_preflight"
                before = read_snapshot(args.api_url)
                invalid = capability_errors(before, args.candidate)
                if invalid or before.get("stage") != "idle" and not released(before):
                    raise RuntimeError("Commissioning preflight failed: " + "; ".join(invalid))
                pending_request, previous_round = None, before.get("roundId")
                destination = args.output / case["id"]
                destination.mkdir()
                open_scenario_console(page, expect)
                scenario_dom, invalid = wait_for_configured_calibration_dom(page, before, args.candidate)
                write_json(destination / "configured-admission.json", scenario_dom)
                if invalid:
                    raise RuntimeError("; ".join(invalid))
                if args.candidate == 100:
                    page.get_by_role('navigation', name='Experiment steps').get_by_role(
                        'button', name=re.compile(r'Outcomes$')).click()
                    dom, invalid = wait_for_configured_calibration_dom(page, before, 100, require_workload=True)
                    write_json(destination / 'configured-workload.json', dom)
                    if invalid:
                        raise RuntimeError('; '.join(invalid))
                    open_scenario_console(page, expect)
                    scenario_dom, invalid = wait_for_configured_calibration_dom(page, before, 100)
                    write_json(destination / 'configured-admission-before-intent.json', scenario_dom)
                    after = read_snapshot(args.api_url)
                    invalid += operator_preflight_snapshot_errors(before, after)
                    if invalid:
                        raise RuntimeError('; '.join(invalid))
                phase = "intent_submission"
                if args.candidate == 100:
                    # The public form exposes AI only. Record its visible state,
                    # then use the operator's loopback API submission explicitly.
                    page.screenshot(path=str(destination / 'operator-before-submit.png'), full_page=True)
                    posted = operator_intent(case)
                    pending_request = posted
                    status, posted, accepted = submit_operator_none(args.api_url, posted)
                    submission = 'operator-loopback-api'
                else:
                    page.locator("#scenario-baseline-ues").fill(str(case["baselineUes"]))
                    for event, label in zip(EVENTS, LABELS):
                        page.get_by_label("Include " + label, exact=True).set_checked(bool(case["events"][event]))
                        if case["events"][event]:
                            page.locator("#scenario-ues-" + event).fill(str(case["events"][event]))
                    page.locator('input[name="onprem-strategy"][value="none"]').check()
                    page.screenshot(path=str(destination / "configured.png"), full_page=True)
                    with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == "/api/onprem/scenario-rounds") as response:
                        page.get_by_role("button", name="Submit actual UE scenarios", exact=True).click()
                    response = response.value
                    status, posted, accepted = response.status, response.request.post_data_json, response.json()
                    submission = 'rendered-ui'
                owned_round = confirmed_owner(accepted, posted.get("requestId"), previous_round)
                write_json(destination / "intent.json", {"status": status, "request": posted, "response": accepted,
                    "submission": submission})
                rejection = intent_rejection(status, accepted, owned_round, posted, case)
                if rejection:
                    raise RuntimeError(rejection)
                round_id = owned_round
                phase = "native_execution"
                deadline, sequence, index = time.monotonic() + args.timeout, -1, 0
                result_seen, failure, final, last_ui = False, None, None, []
                traffic_states = set()
                polling, result_capture = SummaryPollTracker(round_id), None
                while time.monotonic() < deadline:
                    summary_snapshot = read_summary(args.api_url)
                    decision = polling.plan(summary_snapshot, monotonic_now=time.monotonic())
                    if decision.persist_summary:
                        write_json(destination / f"summary-observation-{index:05d}.json",
                            {'snapshot': summary_snapshot, 'summaryReceivedAt': time.time()})
                    index += 1
                    if summary_snapshot.get("roundId") != round_id or summary_snapshot.get("sequence", -1) < sequence:
                        raise RuntimeError("Round identity or sequence changed")
                    sequence = summary_snapshot['sequence']
                    latest_snapshot=summary_snapshot
                    if capability_errors(summary_snapshot, args.candidate):
                        raise RuntimeError("Commissioning capability changed during owned round")
                    if summary_snapshot.get('executionOutcome') == 'failed':
                        failure = "Backend execution failed"
                    if decision.persist_summary or decision.full_boundary or (result_capture and summary_snapshot['stage'] == 'results_ready'):
                        traffic = capture_traffic_with_clock_grace(args.api_url, page,
                            destination / f'traffic-observation-{index:05d}.json')
                        if traffic['errors']:
                            raise RuntimeError('; '.join(traffic['errors']))
                        if not traffic['errors']:
                            visible = traffic['dom'][0]
                            if visible['particles']:
                                traffic_states.add('flow')
                            elif traffic['snapshot']['stage'] not in ('cleaning', 'completed'):
                                traffic_states.add('loading')
                            if traffic['snapshot']['stage'] in ('cleaning', 'completed') and not visible['particles']:
                                traffic_states.add('cleaned')
                    capture = None
                    if decision.full_boundary:
                        capture = capture_full_boundary(args.api_url, summary_snapshot, decision.full_boundary,
                            destination / f'full-{decision.full_boundary}-{index:05d}.json', page=page)
                        sequence = capture['snapshot']['sequence']
                        if decision.full_boundary == 'result':
                            result_capture = capture
                    elif (result_capture and summary_snapshot['stage'] == 'results_ready'
                          and summary_snapshot['sequence'] == result_capture['snapshot']['sequence']):
                        # Same immutable full receipt; DOM is a separate new observation.
                        capture = {**result_capture, **capture_result_dom(page)}
                    if capture is None:
                        page.wait_for_timeout(250)
                        continue
                    current, captured = capture['snapshot'], capture['fullReceivedAt']
                    latest_snapshot=current
                    if capability_errors(current, args.candidate):
                        raise RuntimeError('Full commissioning capability changed')
                    if current.get("stage") == "results_ready":
                        write_json(destination / "capacity-audit.json", asdict(audit_capacity(current)))
                        invalid, last_ui = captured_result_errors(capture, case, args.candidate)
                        if invalid:
                            failure = "; ".join(invalid)
                        else:
                            rendered, dom = capture['rendered'], capture['calibration']
                            ui_now = capture['uiReceivedAt']
                            write_json(destination / f"rendered-{index:05d}.json", {"rendered": rendered, "calibration": dom,
                                "uiReceivedAt": ui_now, "fullReceivedAt": captured, "errors": last_ui})
                            if rendered.get("alerts"):
                                failure = "Visible UI error"
                            if not last_ui:
                                result_seen = True
                                write_json(destination / "result.json", current)
                                write_json(destination / "rendered-result.json", rendered)
                                write_json(destination / "rendered-calibration.json", dom)
                                write_json(destination / "capture-times.json", {
                                    "resultCapturedAt": datetime.fromtimestamp(captured, timezone.utc).isoformat(),
                                    "uiCapturedAt": datetime.fromtimestamp(ui_now, timezone.utc).isoformat()})
                                page.screenshot(path=str(destination / "result.png"), full_page=True)
                    if failure and first_failure is None:
                        first_failure=preserve_failure_evidence(page,destination,case=active_case,round_id=round_id,
                            phase=phase,reason=failure,snapshot=current,label='failed-result')
                    if decision.full_boundary == 'cleanup' and released(current):
                        final = current
                        break
                    page.wait_for_timeout(250)
                if final is None:
                    raise RuntimeError("Calibration deadline expired; owned cleanup required")
                write_json(destination / "cleanup.json", final)
                cleanup_now = time.time()
                write_json(destination / "cleanup-capture.json", {"cleanupCapturedAt": datetime.fromtimestamp(cleanup_now, timezone.utc).isoformat()})
                owned_round = None
                if case["total"] == 0:
                    result_seen = zero_result_valid(final)
                required_traffic = {'loading', 'cleaned'} | ({'flow'} if case['total'] else set())
                if not required_traffic <= traffic_states:
                    failure = failure or 'Native UI receiver/loading/cleanup gate not observed at matching backend sequence'
                if failure or not result_seen or final.get("executionOutcome") != "passed" or browser_errors or cleanup_errors(final, cleanup_now):
                    raise RuntimeError(failure or "; ".join(last_ui) or "Missing verified calibration result")
                summary["executed"].append({"case": case["id"], "roundId": round_id, "status": "passed"})
                pending_request = None
                write_json(args.output / "summary.json", summary)
            summary["status"] = "passed"
            summary["coverageComplete"] = {c["id"] for c in cases(args.candidate)} <= {c["case"] for c in summary["executed"]}
        except BaseException as error:
            record_failure(summary, error, phase, pending_request, owned_round)
            evidence=preserve_failure_evidence(page,destination,case=active_case,round_id=round_id or owned_round,
                phase=phase,reason=error,snapshot=latest_snapshot,label='failure')
            summary['failureEvidence']=evidence
            if active_case is not None:
                failed={'case':active_case,'roundId':round_id or owned_round,'status':'failed',
                    'stage':(first_failure or evidence).get('stage'),'phase':phase,'reason':str(error),
                    'evidenceDirectory':str(destination),'cleanupObserved':released(latest_snapshot) if isinstance(latest_snapshot,dict) else False}
                summary['executed'].append(failed)
            summary['coverageComplete']=False
            try:
                page.screenshot(path=str(args.output / "failure.png"), full_page=True)
            except Exception as capture_error:
                summary["screenshotErrorType"] = type(capture_error).__name__
            if owned_round is None and isinstance(pending_request, dict):
                try:
                    owned_round = confirmed_owner(read_snapshot(args.api_url), pending_request.get("requestId"), previous_round)
                except Exception as reconcile_error:
                    summary["reconciliationErrorType"] = type(reconcile_error).__name__
            if owned_round:
                try:
                    cleanup_snapshot = read_snapshot(args.api_url)
                    if safe_observation_failure_cleanup(cleanup_snapshot, owned_round):
                        summary["failureCleanup"] = clean_owned(page, args.api_url, owned_round, args.timeout)
                    else:
                        summary["failureCleanupDeferred"] = {
                            'roundId': owned_round, 'stage': cleanup_snapshot.get('stage'),
                            'sequence': cleanup_snapshot.get('sequence'),
                            'reason': 'Observer failure cannot cancel an active or blocked round'}
                except Exception as cleanup_error:
                    summary["cleanupErrorType"] = type(cleanup_error).__name__
            raise
        finally:
            summary["browserErrors"] = browser_errors
            write_json(args.output / "summary.json", summary)
            browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", type=int, choices=(5, 10, 25, 50, 100), required=True)
    parser.add_argument("--include-zero", action="store_true")
    parser.add_argument("--case", choices=("zero", "concentrated-embb", "concentrated-urllc", "concentrated-v2x", "concentrated-mmtc", "mixed"), action="append")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ui-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    if not 120 <= args.timeout <= 1800 or not all(loopback(url) for url in (args.ui_url, args.api_url)):
        parser.error("Timeout must be 120..1800 seconds and endpoints must be loopback HTTP origins")
    if args.case and "zero" in args.case and not args.include_zero:
        parser.error("Zero case requires explicit --include-zero")
    selected = [c for c in cases(args.candidate, args.include_zero) if not args.case or c["id"] in args.case]
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"scope": "NONE-capacity-calibration-not-96-case-matrix", "candidateMaxUes": args.candidate,
        "verifiedMaxUes": 0, "promotionEligible": False, "coverageComplete": False,
        "profileId": PROFILE_ID, "planned": selected, "executed": [], "status": "plan-only"}
    summary["capacityCriteria"] = criteria_metadata()
    summary["protocolPredeclarationVerified"] = False
    # Immutable-in-this-run plan receipt is written before browser import/navigation
    # or any intent. Its presence is not independent proof of predeclaration.
    write_json(args.output / "plan.json", {**summary, "declaredAt": datetime.now(timezone.utc).isoformat()})
    write_json(args.output / "summary.json", summary)
    if args.execute:
        execute(args, selected, summary)
    else:
        print(f"Planned {len(selected)} candidate-{args.candidate} cases; executed 0; verified maximum unchanged")


if __name__ == "__main__":
    main()
