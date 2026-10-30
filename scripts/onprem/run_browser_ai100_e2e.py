"""Capture one official 100 UE AI round from the rendered operator UI.

Plan-only by default. An execute run never cancels an active round after an
observer failure; it preserves evidence and leaves recovery to the operator.
The private QER journal is independently verified after owned release.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from scripts.onprem.run_browser_native_calibration import (
    preserve_failure_evidence, read_calibration_dom)
from scripts.onprem.run_browser_native_smoke import (
    confirmed_owner, open_scenario_console, read_rendered_result)
from scripts.onprem.run_browser_scenario_matrix import EVENTS, read_snapshot, round_resources_released, write_json
from scripts.onprem.verify_ai100_e2e import configured_journal, read_qer_journal, verify, _published_workload
from backend.onprem.traffic.workload import workload_metadata


def case(event, ues=100):
    if type(ues) is not int or not 1 <= ues <= 100:
        raise ValueError("Requested cohort must contain 1..100 UEs")
    if event == "mixed":
        counts = dict(zip(("video_streaming", "realtime_control", "connected_traffic", "factory_iot"),
                          (ues // 4 + int(i < ues % 4) for i in range(4))))
        return {"baselineUes": 0, "events": {name: counts.get(name, 0) for name in EVENTS},
                "strategy": "ai"}
    if event not in EVENTS:
        raise ValueError("AI100 requires one supported 100 UE event cohort")
    return {"baselineUes": 0, "events": {name: ues if name == event else 0
                                      for name in EVENTS}, "strategy": "ai"}


def preflight_errors(snapshot, *, require_released=True):
    caps = snapshot.get("capabilities", {})
    expected = {"start": True, "admissionMaxUes": 100,
                "commissioningEnabled": False, "aiPolicy": True, "staticPolicy": False}
    if not require_released:
        # Once accepted, global admission may close while this round installs
        # its owned fixture. These flags authorize new requests, not execution.
        expected.pop('start')
        expected.pop('aiPolicy')
    errors = ["AI100 capability differs: " + key for key, value in expected.items()
              if type(caps.get(key)) is not type(value) or caps.get(key) != value]
    if not ((caps.get("verifiedMaxUes") == 100 and caps.get("capacityStatus") == "receipt_verified")
            or (caps.get("verifiedMaxUes") == 0 and caps.get("candidateMaxUes") == 100
                and caps.get("capacityStatus") == "unverified_ai_trial")):
        errors.append("AI100 verified or trial capacity scope is unavailable")
    if require_released and snapshot.get("stage") != "idle" and not round_resources_released(snapshot):
        errors.append("Previous round is not verified released")
    return errors


def _loopback(url):
    parsed = urlsplit(url)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} \
        and not parsed.username and not parsed.password and parsed.path in {"", "/"}


def read_progress(api):
    """Poll compact metadata; fetch complete native raw only as final evidence."""
    with urlopen(api + '/api/onprem/scenario-rounds/current?view=summary', timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError('Malformed scenario progress')
    return value


def read_ai_rendered_result(page):
    value = read_rendered_result(page)
    value['outcome'] = page.get_by_test_id('onprem-round-evidence').filter(visible=True).get_by_test_id('onprem-round-outcome').evaluate_all(
        '''els => els.map(el => ({executionOutcome:el.dataset.executionOutcome, cleanupStatus:el.dataset.cleanupStatus}))''')
    return value


def open_ai_result_evidence(page):
    """Use the operator's navigation and disclosure before checking visibility."""
    page.get_by_role("navigation", name="Experiment steps").get_by_role(
        "button", name=re.compile(r"Outcomes$")).click()
    panel = page.get_by_test_id("network-ai-outcomes")
    disclosure = panel.locator("details").filter(has=page.locator(
        "summary", has_text=re.compile(r"^Limits, data quality and daily observation$")))
    if disclosure.count() == 1 and disclosure.get_attribute("open") is None:
        disclosure.locator(":scope > summary").click()


def open_ai_workload_evidence(page):
    panel = page.get_by_test_id("network-ai-outcomes")
    disclosure = panel.locator("details").filter(has=page.locator(
        "summary", has_text=re.compile(r"^Action audit and raw evidence$")))
    if disclosure.count() == 1 and disclosure.get_attribute("open") is None:
        disclosure.locator(":scope > summary").click()


def capture_stage_screenshot(page, output, snapshot, *, label=None):
    """Record the actual DOM beside the observed API phase without restaging it."""
    folder = output / 'stages'
    folder.mkdir(exist_ok=True)
    phase = (snapshot.get('agentRuntime') or {}).get('phase') or 'no-agent'
    label = label or f"{snapshot.get('stage', 'unknown')}-{phase}"
    name = f"{snapshot.get('sequence', 0):05d}-" + re.sub(r'[^a-zA-Z0-9_-]', '_', label)
    rendered = page.get_by_test_id('onprem-round-evidence').evaluate_all(
        '''els => els.map(el => ({roundId:el.dataset.roundId, generation:el.dataset.generation,
          sequence:el.dataset.sequence, stage:el.dataset.stage}))''')
    page.screenshot(path=str(folder / (name + '.png')), full_page=True, animations='disabled')
    write_json(folder / (name + '.json'), {
        'capturedAt': time.time(), 'roundId': snapshot.get('roundId'),
        'generation': snapshot.get('generation'), 'sequence': snapshot.get('sequence'),
        'apiStage': snapshot.get('stage'), 'agentPhase': phase,
        'renderedPanels': rendered, 'counts': snapshot.get('counts'),
        'note': 'Actual browser frame; API phase and DOM phase are recorded separately.'})


def execute(args, summary):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.browser_channel)
        page = browser.new_page(viewport={"width": 1560, "height": 1200})
        try:
            return execute_on_page(args, summary, page, expect)
        finally:
            browser.close()


def execute_on_page(args, summary, page, expect, *, stage_callback=None):
    """Drive an owned round on an existing page for consecutive user actions."""
    owned, last, phase = None, None, "navigation"
    try:
        page.goto(args.ui_url + "/?operator-ai100=1")
        page.wait_for_load_state("networkidle")
        language = page.get_by_role("button", name=re.compile("English"))
        if language.count():
            language.click()
        before = read_progress(args.api_url)
        invalid = preflight_errors(before)
        if invalid:
            raise RuntimeError("; ".join(invalid))
        if before.get("roundId"):
            # Wait for persisted history hydration before choosing the draft tab.
            # Otherwise the first history response can auto-select Outcomes mid-edit.
            hydrated = page.locator('[data-testid="onprem-round-evidence"][data-round-id="'
                                    + before["roundId"] + '"]')
            # Native bootstrap may temporarily return UE_ZERO_SETTLING between
            # the API preflight and the browser's own read. Exercise the visible
            # status refresh until the ordinary UI admits the verified fleet.
            hydration_deadline = time.monotonic() + 180
            while not hydrated.count() and time.monotonic() < hydration_deadline:
                refresh = page.get_by_role("button", name="Check status again", exact=True)
                if refresh.count() and refresh.is_enabled():
                    refresh.click()
                page.wait_for_timeout(3000)
            expect(hydrated.first).to_be_attached(timeout=1000)
        open_scenario_console(page, expect)
        page.locator("#scenario-baseline-ues").fill("0")
        wanted = case(args.event, getattr(args, "ues", 100))["events"]
        for event in EVENTS:
            page.get_by_test_id("scenario-include-" + event).set_checked(wanted[event] > 0)
            if wanted[event]:
                page.locator("#scenario-ues-" + event).fill(str(wanted[event]))
        ai = page.locator('input[name="onprem-strategy"][value="ai"]')
        if ai.count() != 1 or not ai.is_enabled():
            raise RuntimeError("The official AI mode is not available in the visible form")
        ai.check()
        page.screenshot(path=str(args.output / "configured.png"), full_page=True)
        fresh = read_progress(args.api_url)
        if (preflight_errors(fresh) or fresh.get("roundId") != before.get("roundId")
                or fresh.get("sequence") != before.get("sequence")):
            raise RuntimeError("AI100 preflight changed before UI submission")
        phase = "intent_submission"
        def submission_started(request):
            if request.method == 'POST' and urlsplit(request.url).path == '/api/onprem/scenario-rounds':
                payload = request.post_data_json
                wanted = case(args.event, getattr(args, 'ues', 100))
                if (isinstance(payload, dict) and set(payload) == {*wanted, 'requestId'}
                        and all(payload.get(k) == v for k,v in wanted.items())
                        and isinstance(payload.get('requestId'),str) and payload['requestId']):
                    write_json(args.output/'submitted-request.json', {'request':payload,
                        'previousRoundId':before.get('roundId'),'submission':'rendered-ui',
                        'responseObserved':False})
        capture_stage_screenshot(page, args.output, before, label='configured-before-submit')
        page.on('request', submission_started)
        try:
            with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == "/api/onprem/scenario-rounds", timeout=90000) as received:
                page.get_by_test_id("scenario-submit").click()
        finally:
            page.remove_listener('request', submission_started)
        response = received.value
        posted = response.request.post_data_json
        accepted = response.json()
        write_json(args.output / "intent.json", {"status": response.status,
            "request": posted, "response": accepted, "submission": "rendered-ui"})
        owned = confirmed_owner(accepted, posted.get("requestId"), before.get("roundId"))
        if (response.status != 202 or not owned or
                any(posted.get(key) != value for key, value in case(args.event, getattr(args, "ues", 100)).items())):
            raise RuntimeError("AI100 UI intent was rejected or changed")
        summary["roundId"] = owned
        phase = "native_execution"
        deadline, sequence, stage, result = time.monotonic() + args.timeout, -1, None, None
        captured_phase = None
        while time.monotonic() < deadline:
            try:
                current = read_progress(args.api_url)
            except (TimeoutError, URLError) as error:
                if isinstance(error, URLError) and not isinstance(error.reason, TimeoutError):
                    raise
                # Preserve a failed API observation, but keep watching the
                # already-owned round until it can close safely. A slow read
                # must not trigger repeated fresh experiments or hide writes.
                failures = summary.setdefault("pollFailures", [])
                if len(failures) < 100:
                    failures.append({"observedAt": time.time(), "errorType": type(error).__name__})
                page.wait_for_timeout(3000)
                continue
            last = current
            if (current.get("roundId") != owned or type(current.get("sequence")) is not int
                    or current["sequence"] < sequence):
                raise RuntimeError("AI100 round identity or sequence changed")
            sequence = current["sequence"]
            if preflight_errors(current, require_released=False):
                raise RuntimeError("AI100 capability or ownership changed")
            observed_phase = (current.get('stage'), (current.get('agentRuntime') or {}).get('phase'))
            if observed_phase != captured_phase:
                capture_stage_screenshot(page, args.output, current)
                captured_phase = observed_phase
            if current.get("stage") != stage:
                stage = current.get("stage")
                write_json(args.output / f"stage-{sequence:05d}-{stage}.json", current)
                if stage_callback is not None and stage != "results_ready":
                    stage_callback(stage, current)
            if current.get("executionOutcome") in {"failed", "cancelled"}:
                raise RuntimeError("AI100 native execution failed")
            # Capture stable historical results after verified closure. Raw
            # measurements keep their original clocks; the UI must not animate
            # them as live traffic. results_ready can last less than one poll.
            if (stage == "completed" and result is None and any(
                    a.get("kind") == "adaptive_ai_outcomes" for a in current.get("actions", []))):
                current = read_snapshot(args.api_url)
                if (current.get('roundId') != owned or current.get('stage') not in {'results_ready', 'completed'}
                        or current.get('executionOutcome') != 'passed'):
                    raise RuntimeError('Owned full result changed during evidence capture')
                stage = current['stage']
                result = current
                write_json(args.output / "result.json", result)
                open_ai_result_evidence(page)
                ui_deadline = time.monotonic() + 10
                while True:
                    rendered = read_ai_rendered_result(page)
                    rows = rendered.get("evidence", [])
                    if (len(rows) == 1 and rows[0].get("roundId") == owned
                            and rows[0].get("sequence") == str(result["sequence"])):
                        break
                    if time.monotonic() >= ui_deadline:
                        raise RuntimeError("Rendered AI result did not reach the captured sequence")
                    page.wait_for_timeout(100)
                write_json(args.output / "rendered-result.json", rendered)
                page.get_by_role("navigation", name="Experiment steps").get_by_role(
                    "button", name=re.compile(r"Outcomes$")).click()
                open_ai_workload_evidence(page)
                fingerprint = _published_workload(result)["comparisonFingerprint"]
                ui_deadline = time.monotonic() + 10
                while True:
                    workload = read_calibration_dom(page)
                    rows = workload.get("workload", [])
                    if len(rows) == 1 and rows[0].get("comparisonFingerprint") == fingerprint:
                        break
                    if time.monotonic() >= ui_deadline:
                        raise RuntimeError("Visible AI workload did not match the native cohort")
                    page.wait_for_timeout(100)
                write_json(args.output / "rendered-calibration.json", workload)
                capture_stage_screenshot(page, args.output, result, label='completed-visible-evidence')
                page.screenshot(path=str(args.output / "result.png"), full_page=True)
            if stage == "completed":
                current = result if result and result['stage'] == 'completed' else read_snapshot(args.api_url)
                write_json(args.output / "cleanup.json", current)
                if not round_resources_released(current) or result is None:
                    raise RuntimeError("AI100 result or owned cleanup is unavailable")
                if summary.get("pollFailures"):
                    raise RuntimeError("Backend polling timed out during native execution; round closure captured")
                if args.capture_only:
                    summary.update(status="captured_unverified", released=True,
                                   reason="Private native QER journal requires separate read-only verification")
                    return
                journal = configured_journal(args.ai100_config, args.ai100_config_sha256)
                actions, reads = read_qer_journal(journal, owned)
                proof = verify(result, rendered, current,
                    json.loads((args.output / "intent.json").read_bytes()),
                    actions, reads, workload, expected_ues=getattr(args, "ues", 100))
                write_json(args.output / "verification.json", proof)
                summary.update(status="passed", verification=proof)
                return
            # Full native receipts are large; polling them at 4 Hz competes with
            # the executor for the round ledger. Keep complete evidence reads
            # while matching an interactive monitoring cadence.
            page.wait_for_timeout(3000)
        raise RuntimeError("AI100 observation deadline expired; no active cancel was sent")
    except BaseException as error:
        summary.update(status="failed", phase=phase, errorType=type(error).__name__,
                       reason=str(error), cleanupDeferred=owned is not None)
        preserve_failure_evidence(page, args.output, case="ai100-" + args.event,
            round_id=owned, phase=phase, reason=error, snapshot=last)
        raise
    finally:
        write_json(args.output / "summary.json", summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--event", choices=(*EVENTS, "mixed"), default="video_streaming")
    parser.add_argument("--ai100-config", type=Path)
    parser.add_argument("--ai100-config-sha256")
    parser.add_argument("--capture-only", action="store_true",
                        help="Preserve browser receipts for later private-journal verification")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ui-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--ues", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--browser-channel", choices=("chrome", "msedge"),
                        help="Use an installed browser when Playwright's bundled Chromium is absent")
    args = parser.parse_args(argv)
    if not 120 <= args.timeout <= 1800 or not all(_loopback(url) for url in (args.ui_url, args.api_url)):
        parser.error("Only bounded timeouts and loopback HTTP origins are accepted")
    if args.execute and not args.capture_only and (args.ai100_config is None or
            args.ai100_config_sha256 is None):
        parser.error("Verified execute requires the pinned private AI100 startup configuration")
    if args.capture_only and not args.execute:
        parser.error("Capture-only requires explicit --execute")
    if args.execute and not args.capture_only:
        try:
            configured_journal(args.ai100_config, args.ai100_config_sha256)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            parser.error("Pinned private AI100 QER journal preflight failed: " + type(exc).__name__)
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"scope": f"{args.ues}-UE-AI-live-E2E", "event": args.event,
               "intent": case(args.event, args.ues), "status": "planned", "verified": False}
    write_json(args.output / "plan.json", summary)
    write_json(args.output / "summary.json", summary)
    if args.execute:
        execute(args, summary)


if __name__ == "__main__":
    main()
