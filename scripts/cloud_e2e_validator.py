#!/usr/bin/env python3
"""Parameterised cloud E2E, resilience, load and accessibility validation.

This runner deliberately requires deployment URLs.  It never falls back to a stale
CloudFront or API Gateway endpoint.  Expensive suites are opt-in through --profile.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright


SCENARIOS = ("concert", "typhoon", "accident", "medical", "iot_surge")
STRATEGIES = ("none", "static", "ai")
SCALES = (1, 50, 100)
TERMINAL = {
    "AGENT_COMPLETE", "AGENT_DEGRADED", "AGENT_BLOCKED", "AGENT_FAILED", "AGENT_CANCELLED",
    "SIMULATION_COMPLETE", "SIMULATION_DEGRADED", "SIMULATION_BLOCKED", "SIMULATION_FAILED",
}
SUCCESS_TERMINAL = {"AGENT_COMPLETE", "AGENT_DEGRADED", "SIMULATION_COMPLETE", "SIMULATION_DEGRADED"}
RESET_TERMINAL = {"success", "failed"}


@dataclass(frozen=True)
class Case:
    name: str
    strategy: str
    scenarios: tuple[tuple[str, int], ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-url", default=os.getenv("E2E_UI_URL"), help="Cloud UI URL (or E2E_UI_URL)")
    parser.add_argument("--api-url", default=os.getenv("E2E_API_URL"), help="API base URL (or E2E_API_URL)")
    parser.add_argument("--api-token", default=os.getenv("E2E_API_TOKEN"), help="Shared token (or E2E_API_TOKEN)")
    parser.add_argument("--profile", choices=("smoke", "matrix", "combinations", "full"), default="smoke")
    parser.add_argument("--output", type=Path, default=Path("artifacts/cloud-e2e-result.json"))
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 5, 10), default=1)
    parser.add_argument("--max-cases", type=int, default=0, help="Cost guard; 0 runs every case in profile")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--case-timeout-seconds", type=int, default=480)
    parser.add_argument("--soak-minutes", type=int, default=0)
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--axe-path", type=Path, default=Path("frontend/node_modules/axe-core/axe.min.js"))
    args = parser.parse_args()
    if not args.ui_url and not args.skip_ui:
        parser.error("--ui-url or E2E_UI_URL is required unless --skip-ui is used")
    if not args.api_url:
        parser.error("--api-url or E2E_API_URL is required")
    if not args.api_token or not args.api_token.strip():
        parser.error("--api-token or E2E_API_TOKEN is required")
    if args.profile == "combinations" and args.concurrency != 1:
        parser.error("--profile combinations requires --concurrency 1 because every case resets shared runtime")
    args.api_url = args.api_url.rstrip("/")
    return args


def build_cases(profile: str) -> list[Case]:
    if profile == "smoke":
        return [Case("smoke-ai-concert", "ai", (("concert", 50),))]
    if profile == "combinations":
        return [
            Case("combo-capacity-ai", "ai", (("concert", 50), ("iot_surge", 50))),
            Case("combo-critical-ai", "ai", (("typhoon", 50), ("accident", 50), ("medical", 50))),
            Case("combo-all-five-ai", "ai", tuple((name, 50) for name in SCENARIOS)),
        ]
    matrix = [
        Case(f"matrix-{scenario}-{strategy}-{scale}", strategy, ((scenario, scale),))
        for scenario in SCENARIOS for strategy in STRATEGIES for scale in SCALES
    ]
    if profile == "matrix":
        return matrix
    return matrix + [Case(f"all-five-{strategy}", strategy, tuple((name, 50) for name in SCENARIOS)) for strategy in STRATEGIES]


def active_measured_edges(body: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = ((body.get("free5gc") or {}).get("networkSnapshot") or {})
    return [edge for edge in snapshot.get("edges", []) if edge.get("active") and edge.get("plane") != "control"
            and float(edge.get("throughputMbps") or 0) > 0]


def unavailable_paths(value: Any, path: str = "$") -> list[str]:
    """Locate explicit unavailable markers without treating absent optional fields as success."""
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            paths.extend(unavailable_paths(item, f"{path}.{key}"))
        return paths
    if isinstance(value, list):
        paths = []
        for index, item in enumerate(value):
            paths.extend(unavailable_paths(item, f"{path}[{index}]"))
        return paths
    if isinstance(value, str) and "unavailable" in value.lower():
        return [path]
    return []


def verify_cleanup_status(
    api: str,
    headers: dict[str, str],
    scenarios: set[str],
    *,
    samples: int = 3,
    poll_seconds: float = 5,
) -> dict[str, Any]:
    """Prove cleanup stays clean across multiple live free5GC snapshots."""
    observations: list[dict[str, Any]] = []
    for sample_index in range(samples):
        response = requests.get(f"{api}/free5gc/status", headers=headers, timeout=90)
        response.raise_for_status()
        body = response.json()
        metrics = body.get("metrics") or {}
        snapshot = body.get("networkSnapshot") or {}
        scenario_traffic = metrics.get("scenarioTraffic") or []
        residual_traffic = [
            item for item in scenario_traffic if str(item.get("scenario") or "") in scenarios
        ]
        residual_edges = [
            edge for edge in snapshot.get("edges", [])
            if edge.get("active") and str(edge.get("scenario") or "") in scenarios
        ]
        residual_pods: list[dict[str, Any]] = []
        for component in metrics.get("podComponents") or []:
            for pod in component.get("pods") or []:
                if str(pod.get("scenario") or "") in scenarios:
                    residual_pods.append({"component": component.get("component"), **pod})
        unavailable = unavailable_paths(body)
        observation = {
            "sample": sample_index + 1,
            "connected": body.get("connected") is True,
            "unavailablePaths": unavailable,
            "residualScenarioTraffic": residual_traffic,
            "residualActiveEdges": residual_edges,
            "residualScenarioPods": residual_pods,
        }
        observation["passed"] = bool(
            observation["connected"]
            and not unavailable
            and not residual_traffic
            and not residual_edges
            and not residual_pods
        )
        observations.append(observation)
        if sample_index + 1 < samples:
            time.sleep(poll_seconds)
    return {"passed": all(item["passed"] for item in observations), "observations": observations}


def reset(
    api: str,
    headers: dict[str, str],
    *,
    timeout_seconds: float = 930,
    poll_seconds: float = 2,
) -> dict[str, Any]:
    response = requests.post(f"{api}/events/reset", headers=headers, timeout=90)
    response.raise_for_status()
    try:
        current = response.json()
    except requests.JSONDecodeError:
        return {"statusCode": response.status_code}
    reset_id = current.get("resetId")
    if not reset_id:
        return current

    deadline = time.monotonic() + timeout_seconds
    last_poll_error: Exception | None = None
    while current.get("status") not in RESET_TERMINAL:
        if time.monotonic() >= deadline:
            detail = f" Last polling error: {last_poll_error!r}" if last_poll_error else ""
            raise TimeoutError(f"reset {reset_id} did not finish within {timeout_seconds:g} seconds.{detail}")
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        try:
            status = requests.get(
                f"{api}/events/reset/{requests.utils.quote(str(reset_id), safe='')}",
                headers=headers,
                timeout=30,
            )
            status.raise_for_status()
            current = status.json()
            last_poll_error = None
        except (requests.RequestException, requests.JSONDecodeError) as error:
            # A transient API interruption must not be mistaken for completed
            # cleanup; keep polling until the explicit validator deadline.
            last_poll_error = error
    if current.get("status") == "failed":
        raise AssertionError(current.get("error") or current.get("message") or f"reset {reset_id} failed")
    return current


def run_case(case: Case, args: argparse.Namespace) -> dict[str, Any]:
    session_id = f"cloud-e2e-{uuid.uuid4()}"
    headers = {"X-Session-Id": session_id, "Content-Type": "application/json",
               "Authorization": f"Bearer {args.api_token.strip()}"}
    started_at = utc_now()
    transitions: list[dict[str, Any]] = []
    result: dict[str, Any] = {"case": asdict(case), "sessionId": session_id, "startedAt": started_at}
    execution_id = None
    try:
        reset(args.api_url, headers)
        busy_retries = 0
        while True:
            response = requests.post(
                f"{args.api_url}/api/scenario/trigger", headers=headers,
                json={"city_residents": 180000, "locale": "zh-TW", "slice_strategy": case.strategy,
                      "scenarios": [{"event_type": name, "event_scale": scale} for name, scale in case.scenarios]},
                timeout=90,
            )
            try:
                response_body = response.json()
            except requests.JSONDecodeError:
                response_body = {}
            is_busy = response.status_code in {409, 423, 429} and "SESSION_BUSY" in json.dumps(response_body)
            if not is_busy:
                break
            busy_retries += 1
            if busy_retries >= 60:
                raise AssertionError("shared runtime remained SESSION_BUSY after retry window")
            time.sleep(min(10, 1 + busy_retries))
        response.raise_for_status()
        payload = response.json()
        execution_id = payload.get("executionId") or (payload.get("executionIds") or [None])[0]
        if not execution_id:
            raise AssertionError(f"trigger omitted execution id: {payload}")
        deadline = time.monotonic() + args.case_timeout_seconds
        rendered = False
        final: dict[str, Any] | None = None
        last_signature: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            status = requests.get(f"{args.api_url}/events/status/{execution_id}", headers=headers, timeout=30)
            status.raise_for_status()
            body = status.json()
            runtime = body.get("runtimePrime") or {}
            signature = (body.get("status"), body.get("progressStage"), body.get("awaitingTrafficRenderAck"),
                         bool(body.get("agentDecision")), tuple(runtime.get("missingScenarios") or []))
            if signature != last_signature:
                transitions.append({"at": utc_now(), "status": signature[0], "stage": signature[1],
                                    "awaitingTrafficRenderAck": signature[2], "hasAgentDecision": signature[3],
                                    "missingScenarios": list(signature[4])})
                last_signature = signature
            if body.get("awaitingTrafficRenderAck") and active_measured_edges(body) and not rendered:
                ack = requests.post(f"{args.api_url}/events/status/{execution_id}/traffic-rendered",
                                    headers=headers, timeout=30)
                ack.raise_for_status()
                rendered = True
            if body.get("status") in TERMINAL:
                final = body
                break
            time.sleep(args.poll_seconds)
        if final is None:
            raise AssertionError("case did not reach a terminal state")
        runtime = final.get("runtimePrime") or {}
        observed = set(runtime.get("observedScenarios") or [])
        expected = {name for name, _ in case.scenarios}
        assertions = {
            "successfulTerminal": final.get("status") in {"AGENT_COMPLETE", "SIMULATION_COMPLETE"},
            "strategyMatches": final.get("sliceStrategy") == case.strategy,
            "allScenariosObserved": expected.issubset(observed) and not runtime.get("missingScenarios"),
            "observedBeforePlanning": runtime.get("observedBeforePlanning") is True,
            "aiDecisionContract": bool(final.get("agentDecision")) == (case.strategy == "ai"),
            "trafficRendered": rendered,
            "noUnavailable": not unavailable_paths(final),
        }
        result.update({"executionId": execution_id, "status": final.get("status"), "sessionBusyRetries": busy_retries,
                       "assertions": assertions,
                       "passed": all(assertions.values()), "transitions": transitions, "finishedAt": utc_now(),
                       "runtimePrime": runtime, "error": final.get("error")})
    except Exception as error:  # evidence must survive individual case failure
        result.update({"passed": False, "exception": repr(error), "transitions": transitions, "finishedAt": utc_now()})
    finally:
        try:
            cleanup = reset(args.api_url, headers)
            post_cleanup = verify_cleanup_status(
                args.api_url,
                headers,
                {name for name, _ in case.scenarios},
            )
            result["cleanup"] = {"passed": post_cleanup["passed"], "response": cleanup,
                                 "postCleanup": post_cleanup}
            if not post_cleanup["passed"]:
                result["passed"] = False
        except Exception as cleanup_error:
            result["cleanup"] = {"passed": False, "exception": repr(cleanup_error)}
            result["passed"] = False
    return result


def validate_ui(args: argparse.Namespace) -> dict[str, Any]:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    axe_source = args.axe_path.read_text(encoding="utf-8")
    evidence: dict[str, Any] = {"startedAt": utc_now()}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        # Playwright Python accepts a script only (unlike page.evaluate, there is
        # no positional argument channel).  JSON encoding preserves quotes,
        # backslashes and Unicode without turning the token into executable JS.
        context.add_init_script(
            script=(
                "sessionStorage.setItem('5gcityverse.apiAccessToken', "
                f"{json.dumps(args.api_token)});"
            )
        )
        page = context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        response = page.goto(args.ui_url, wait_until="networkidle", timeout=120_000)
        if not response or not response.ok:
            raise AssertionError(f"UI navigation failed: {response.status if response else 'no response'}")
        # The language card's accessible name contains a rendered line break.
        # Match the stable CTA text instead of depending on whitespace
        # normalisation in a particular Playwright/browser release.
        language_button = page.get_by_role("button").filter(has_text="開始 5G Core 城市模擬")
        if language_button.count():
            language_button.first.click()
            page.wait_for_load_state("networkidle", timeout=120_000)

        # Exercise real scenario and strategy controls, not merely generic selectors.
        ai_strategy = page.locator('input[name="slice-strategy"][value="ai"]')
        ai_strategy.wait_for(state="visible", timeout=120_000)
        ai_strategy.check()
        controls = {
            "concert": "AR Concert",
            "typhoon": "Typhoon",
            "accident": "Traffic Accident",
            "medical": "ER Surge",
            "iot_surge": "IoT Surge",
        }
        selected = list(SCENARIOS) if args.profile == "combinations" else ["concert"]
        scale = 50 if args.profile == "combinations" else 1
        for scenario, label in controls.items():
            checkbox = page.get_by_role("checkbox", name=f"Include {label}")
            should_select = scenario in selected
            if should_select and not checkbox.is_checked():
                checkbox.check()
            elif not should_select and checkbox.is_checked():
                checkbox.uncheck()
            if should_select:
                page.locator(f"#scale-number-{scenario}").fill(str(scale))
        submit_button = page.get_by_role("button", name="送出情境")
        submit_button.wait_for(state="visible", timeout=120_000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('button')].some(button => button.textContent?.trim() === '送出情境' && !button.disabled)",
            timeout=180_000,
        )
        with page.expect_response(lambda item: item.request.method == "POST" and item.url.endswith("/api/scenario/trigger"), timeout=120_000) as trigger_info:
            submit_button.click()
        trigger_response = trigger_info.value
        request_payload = trigger_response.request.post_data_json or {}
        trigger_payload = trigger_response.json()
        posted_scenarios = [item.get("event_type") for item in request_payload.get("scenarios") or []]
        request_matches = posted_scenarios == selected and request_payload.get("slice_strategy") == "ai"
        execution_id = trigger_payload.get("executionId") or (trigger_payload.get("executionIds") or [None])[0]
        workflow = {"triggerStatus": trigger_response.status, "aiStrategySelected": True,
                    "scenarios": selected, "scale": scale, "postedScenarios": posted_scenarios,
                    "executionId": execution_id,
                    "requestMatchesSelection": request_matches,
                    "passed": trigger_response.status in {200, 202} and request_matches and bool(execution_id)}
        if not execution_id:
            raise AssertionError(f"UI trigger omitted execution id: {trigger_payload}")

        # Let the deployed frontend own traffic-render acknowledgement and
        # execution polling.  The validator observes the same session until a
        # strict successful terminal state; resetting immediately after HTTP
        # acceptance would only test a cancellation race, not the UI workflow.
        session_id = page.evaluate("sessionStorage.getItem('5gcityverse.browserSessionId')")
        ui_headers = {"X-Session-Id": session_id, "Authorization": f"Bearer {args.api_token.strip()}"}
        deadline = time.monotonic() + args.case_timeout_seconds
        ui_final: dict[str, Any] | None = None
        ui_transitions: list[dict[str, Any]] = []
        last_signature: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            status = requests.get(f"{args.api_url}/events/status/{execution_id}", headers=ui_headers, timeout=30)
            status.raise_for_status()
            body = status.json()
            runtime = body.get("runtimePrime") or {}
            signature = (
                body.get("status"), body.get("progressStage"),
                body.get("awaitingTrafficRenderAck"), bool(body.get("agentDecision")),
                tuple(runtime.get("missingScenarios") or []),
            )
            if signature != last_signature:
                ui_transitions.append({
                    "at": utc_now(), "status": signature[0], "stage": signature[1],
                    "awaitingTrafficRenderAck": signature[2], "hasAgentDecision": signature[3],
                    "missingScenarios": list(signature[4]),
                })
                last_signature = signature
            if body.get("status") in TERMINAL:
                ui_final = body
                break
            page.wait_for_timeout(int(args.poll_seconds * 1000))
        if ui_final is None:
            raise AssertionError("deployed UI workflow did not reach a terminal state")
        runtime = ui_final.get("runtimePrime") or {}
        observed = set(runtime.get("observedScenarios") or [])
        ui_assertions = {
            "successfulTerminal": ui_final.get("status") in {"AGENT_COMPLETE", "SIMULATION_COMPLETE"},
            "allScenariosObserved": set(selected).issubset(observed) and not runtime.get("missingScenarios"),
            "observedBeforePlanning": runtime.get("observedBeforePlanning") is True,
            "frontendAcknowledgedTraffic": bool(ui_final.get("trafficRenderedAt")),
            "aiDecisionPresent": bool(ui_final.get("agentDecision")),
            "noUnavailable": not unavailable_paths(ui_final),
        }
        workflow.update({
            "terminalStatus": ui_final.get("status"),
            "transitions": ui_transitions,
            "assertions": ui_assertions,
            "passed": workflow["passed"] and all(ui_assertions.values()),
        })
        page.locator("#workspace-tab-decision").click()
        decision_heading = page.get_by_role("heading", name="AI Agent 決策中心")
        decision_heading.wait_for(state="visible", timeout=120_000)
        plan_name = ((ui_final.get("agentDecision") or {}).get("selectedPlan") or {}).get("name")
        if plan_name:
            page.wait_for_function(
                "([selector, text]) => (document.querySelector(selector)?.innerText || '').includes(text)",
                arg=["#workspace-panel-decision", plan_name],
                timeout=120_000,
            )
        workflow["decisionVisible"] = bool(plan_name) and page.locator(
            "#workspace-panel-decision"
        ).evaluate("(element, text) => element.innerText.includes(text)", plan_name)
        workflow["passed"] = workflow["passed"] and workflow["decisionVisible"]
        active_screenshot = args.output.with_name(f"{args.output.stem}-ui-active.png")
        page.screenshot(path=str(active_screenshot), full_page=True)
        cleanup = reset(args.api_url, ui_headers)
        post_cleanup = verify_cleanup_status(args.api_url, ui_headers, set(selected))
        workflow["cleanupStatus"] = cleanup.get("status", "success")
        workflow["postCleanup"] = post_cleanup
        workflow["passed"] = workflow["passed"] and cleanup.get("status") != "failed" and post_cleanup["passed"]
        page.reload(wait_until="networkidle", timeout=120_000)
        clean_screenshot = args.output.with_name(f"{args.output.stem}-ui-clean.png")
        page.screenshot(path=str(clean_screenshot), full_page=True)
        workflow["screenshots"] = {"active": str(active_screenshot), "clean": str(clean_screenshot)}

        page.add_script_tag(content=axe_source)
        axe = page.evaluate("async () => await axe.run(document)")
        violations = [{"id": item["id"], "impact": item["impact"], "nodes": len(item["nodes"]),
                       "help": item["help"],
                       "targets": [node.get("target") for node in item["nodes"][:10]],
                       "html": [node.get("html") for node in item["nodes"][:10]],
                       "failureSummary": [node.get("failureSummary") for node in item["nodes"][:10]]} for item in axe["violations"]
                      if item.get("impact") in {"serious", "critical"}]
        body_overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        # Disabled controls are intentionally removed from the tab order and
        # cannot receive focus; treating them as keyboard failures creates a
        # false accessibility regression while a simulation is running.
        interactive = page.locator(
            "button:visible:not(:disabled), a[href]:visible, "
            "input:visible:not(:disabled), select:visible:not(:disabled), "
            "textarea:visible:not(:disabled)"
        )
        keyboard_failures: list[str] = []
        for index in range(min(interactive.count(), 100)):
            node = interactive.nth(index)
            try:
                node.focus()
                if not node.evaluate("el => el === document.activeElement"):
                    keyboard_failures.append(node.evaluate("el => el.outerHTML.slice(0, 500)"))
            except Exception as error:
                keyboard_failures.append(
                    f"{node.evaluate('el => el.outerHTML.slice(0, 300)')} :: {error}"
                )
        unnamed = page.locator("button:visible:not([aria-label]):not([aria-labelledby])").evaluate_all(
            "nodes => nodes.filter(n => !n.innerText.trim() && !n.getAttribute('title')).length")
        baseline_console_errors = list(console_errors)
        baseline_page_errors = list(page_errors)

        # Browser-level network fault injection: the UI must remain rendered and responsive.
        api_fault: dict[str, Any]
        try:
            page.route("**/events/status/**", lambda route: route.abort("failed"))
            page.reload(wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2_000)
            api_fault = {"survived": page.locator("body").is_visible() and page.locator("button:visible").count() > 0}
            page.unroute("**/events/status/**")
        except Exception as error:
            api_fault = {"survived": False, "exception": repr(error)}
        ws_fault: dict[str, Any]
        try:
            fault_context = browser.new_context(viewport={"width": 1280, "height": 800})
            fault_context.add_init_script(
                script=(
                    "sessionStorage.setItem('5gcityverse.apiAccessToken', "
                    f"{json.dumps(args.api_token)});"
                )
            )
            if hasattr(fault_context, "route_web_socket"):
                fault_context.route_web_socket("**", lambda websocket: websocket.close())
                fault_page = fault_context.new_page()
                fault_page.goto(args.ui_url, wait_until="domcontentloaded", timeout=120_000)
                fault_page.wait_for_timeout(2_000)
                ws_fault = {"supported": True, "survived": fault_page.locator("body").is_visible()
                            and fault_page.locator("button:visible").count() > 0}
                fault_context.close()
            else:
                ws_fault = {"supported": False, "skipped": "Playwright lacks route_web_socket"}
        except Exception as error:
            ws_fault = {"supported": True, "survived": False, "exception": repr(error)}
        context.close()
        browser.close()
    checks = {"axeSeriousCriticalZero": not violations, "noHorizontalOverflow": not body_overflow,
              "keyboardFocus": not keyboard_failures, "namedIconButtons": unnamed == 0,
              "noBaselineConsoleErrors": not baseline_console_errors, "noBaselinePageErrors": not baseline_page_errors,
              "apiFailureSurvived": api_fault.get("survived") is True,
              "webSocketFailureSurvived": ws_fault.get("survived") is True if ws_fault.get("supported") else None,
              "realWorkflow": workflow.get("passed") is True}
    evidence.update({"passed": all(value is not False for value in checks.values()), "checks": checks,
                     "axeViolations": violations, "horizontalOverflow": body_overflow,
                     "keyboardFailures": keyboard_failures, "unnamedIconButtons": unnamed,
                     "consoleErrors": baseline_console_errors, "pageErrors": baseline_page_errors,
                     "apiFault": api_fault, "webSocketFault": ws_fault, "workflow": workflow, "finishedAt": utc_now()})
    return evidence


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = build_cases(args.profile)
    if args.max_cases > 0:
        cases = cases[:args.max_cases]
    suite_started = time.monotonic()
    results: list[dict[str, Any]] = []
    deadline = suite_started + args.soak_minutes * 60 if args.soak_minutes else None
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            results.extend(executor.map(lambda case: run_case(case, args), cases))
        if deadline is None or time.monotonic() >= deadline:
            break
    ui = {"skipped": True, "reason": "--skip-ui"} if args.skip_ui else validate_ui(args)
    cleanup_passed = all(item.get("cleanup", {}).get("passed") for item in results)
    document = {
        "schemaVersion": 1, "verifiedAt": utc_now(), "profile": args.profile,
        "parameters": {"uiUrl": args.ui_url, "apiUrl": args.api_url, "concurrency": args.concurrency,
                       "maxCases": args.max_cases, "soakMinutes": args.soak_minutes},
        "costNotice": "matrix=45 single-scenario cases; combinations=three AI multi-scenario cases; full=45 cases plus three all-scenario batches",
        "humanSUS": {"status": "not_collected", "reason": "Automated tests cannot represent human satisfaction."},
        "summary": {"cases": len(results), "passed": sum(bool(item.get("passed")) for item in results),
                    "failed": sum(not item.get("passed") for item in results), "cleanupPassed": cleanup_passed,
                    "durationSeconds": round(time.monotonic() - suite_started, 1)},
        "ui": ui, "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = redact(document, args.api_token)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document["summary"], ensure_ascii=False), flush=True)
    return 0 if document["summary"]["failed"] == 0 and cleanup_passed and ui.get("passed", True) else 1


def redact(value: Any, secret: str) -> Any:
    """Return evidence with accidental secret occurrences removed."""
    if isinstance(value, dict):
        return {key: redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secret) for item in value)
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


if __name__ == "__main__":
    sys.exit(main())
