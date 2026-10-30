import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://dlf3ts9zkseda.cloudfront.net"
API = "https://ypd6vyrold.execute-api.ap-northeast-1.amazonaws.com"
OUT = Path("artifacts/browser-combo-qa")
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    a = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    b = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    a.goto(URL, wait_until="networkidle", timeout=120_000)
    b.goto(URL, wait_until="networkidle", timeout=120_000)
    a.request.post(f"{API}/events/reset")
    time.sleep(2)

    a.get_by_label("City residents").fill("5000")
    for label in ["AR Concert", "Typhoon", "Traffic Accident", "ER Surge", "IoT Surge"]:
        a.get_by_label(f"Include {label}").set_checked(label in {"Traffic Accident", "ER Surge"})
    for label, value in [("Traffic Accident Impacted vehicles", "50"), ("ER Surge Patients/devices", "25")]:
        a.get_by_label(label).evaluate(f"(e) => {{e.value='{value}';e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}}")

    b_before = b.locator("body").inner_text()
    with a.expect_response(lambda r: "/api/scenario/trigger" in r.url, timeout=120_000) as info:
        a.get_by_role("button", name="Submit Scenario").click()
    response = info.value.json()
    execution_ids = response["executionIds"]
    time.sleep(8)
    b_after = b.locator("body").inner_text()

    states = {execution_id: None for execution_id in execution_ids}
    deadline = time.time() + 420
    terminal = {"AGENT_COMPLETE", "AGENT_DEGRADED", "AGENT_BLOCKED", "AGENT_CANCELLED", "AGENT_FAILED"}
    while time.time() < deadline and any(not state or state.get("status") not in terminal for state in states.values()):
        for execution_id in execution_ids:
            if states[execution_id] and states[execution_id].get("status") in terminal:
                continue
            res = a.request.get(f"{API}/events/status/{execution_id}")
            if res.ok:
                states[execution_id] = res.json()
        time.sleep(8)

    leaked = []
    for execution_id in execution_ids:
        for needle in [execution_id, execution_id[:8]]:
            if needle in b_after and needle not in b_before:
                leaked.append(needle)
    summary = {}
    for execution_id, state in states.items():
        state = state or {}
        decision = state.get("agentDecision") or {}
        intent = state.get("intent") or {}
        summary[execution_id] = {
            "status": state.get("status"),
            "eventType": state.get("eventType"),
            "runtimeObservedBeforePlanning": (state.get("scenarioContext") or {}).get("runtimePrime", {}).get("observedBeforePlanning"),
            "observedScenarios": (state.get("scenarioContext") or {}).get("runtimePrime", {}).get("observedScenarios"),
            "targetSlice": intent.get("targetSlice"),
            "cityResidents": intent.get("cityResidents"),
            "eventScale": intent.get("eventScale"),
            "decision": decision.get("decision"),
            "verification": (state.get("verification") or {}).get("status"),
        }
    report = {"trigger": response, "summary": summary, "otherTabExecutionLeaks": leaked, "otherTabChangedOnlyByGlobalTelemetry": b_before != b_after and not leaked}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    a.request.post(f"{API}/events/reset")
    browser.close()
