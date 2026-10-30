import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://dlf3ts9zkseda.cloudfront.net"
API = "https://ypd6vyrold.execute-api.ap-northeast-1.amazonaws.com"
OUT = Path("artifacts/browser-scenario-qa")
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context_a = browser.new_context(viewport={"width": 1440, "height": 900})
    context_b = browser.new_context(viewport={"width": 1440, "height": 900})
    page_a = context_a.new_page()
    page_b = context_b.new_page()
    errors = {"a": [], "b": []}
    page_a.on("console", lambda m: errors["a"].append({"type": m.type, "text": m.text}) if m.type == "error" else None)
    page_b.on("console", lambda m: errors["b"].append({"type": m.type, "text": m.text}) if m.type == "error" else None)
    page_a.goto(URL, wait_until="networkidle", timeout=120_000)
    page_b.goto(URL, wait_until="networkidle", timeout=120_000)

    reset = page_a.request.post(f"{API}/events/reset")
    time.sleep(2)
    before_b = page_b.locator("body").inner_text()

    number = page_a.locator('input[type="number"]')
    number.fill("1000")
    checks = page_a.locator('input[type="checkbox"]')
    for i in range(checks.count()):
        checks.nth(i).set_checked(i == 0)
    ranges = page_a.locator('input[type="range"]')
    ranges.nth(0).evaluate("(e) => { e.value='100'; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); }")

    with page_a.expect_response(lambda r: "/api/scenario/trigger" in r.url, timeout=120_000) as response_info:
        page_a.get_by_role("button", name="Submit Scenario").click()
    trigger_response = response_info.value
    trigger_body = trigger_response.json()
    execution_id = trigger_body.get("executionId") or (trigger_body.get("executionIds") or [None])[0]
    time.sleep(5)
    after_b = page_b.locator("body").inner_text()

    statuses = []
    deadline = time.time() + 900
    final = None
    while execution_id and time.time() < deadline:
        res = page_a.request.get(f"{API}/events/status/{execution_id}")
        body = res.json()
        status = body.get("status")
        statuses.append({"at": time.time(), "http": res.status, "status": status, "progressStage": body.get("progressStage")})
        final = body
        if status in {"AGENT_COMPLETE", "AGENT_DEGRADED", "AGENT_BLOCKED", "AGENT_CANCELLED"}:
            break
        time.sleep(10)

    metrics = page_a.request.get(f"{API}/metrics/current").json()
    slices = page_a.request.get(f"{API}/network/slices").json()
    page_a.screenshot(path=str(OUT / "after-a.png"), full_page=True)
    page_b.screenshot(path=str(OUT / "after-b.png"), full_page=True)
    report = {
        "reset": {"status": reset.status, "body": reset.text()},
        "trigger": {"status": trigger_response.status, "body": trigger_body},
        "executionId": execution_id,
        "statuses": statuses,
        "final": final,
        "metrics": metrics,
        "slices": slices,
        "contextBChanged": before_b != after_b,
        "contextBNewText": [line for line in after_b.splitlines() if line and line not in before_b.splitlines()],
        "storage": {
            "a": page_a.evaluate("() => ({local:{...localStorage},session:{...sessionStorage}})"),
            "b": page_b.evaluate("() => ({local:{...localStorage},session:{...sessionStorage}})"),
        },
        "consoleErrors": errors,
    }
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "reset": report["reset"], "trigger": report["trigger"], "executionId": execution_id,
        "statuses": statuses, "finalStatus": (final or {}).get("status"),
        "runtimePrime": (final or {}).get("scenarioContext", {}).get("runtimePrime"),
        "decision": (final or {}).get("agentDecision"), "metrics": metrics,
        "contextBChanged": report["contextBChanged"], "contextBNewText": report["contextBNewText"],
        "storage": report["storage"], "consoleErrors": errors,
    }, ensure_ascii=False, indent=2))
    browser.close()
