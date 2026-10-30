import json
from pathlib import Path
from playwright.sync_api import sync_playwright

url = "https://dlf3ts9zkseda.cloudfront.net"
out = Path("artifacts/browser-locale-qa")
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto(url, wait_until="networkidle", timeout=120_000)
    zh = page.locator("body").inner_text()
    page.get_by_role("button", name="EN", exact=True).click()
    page.wait_for_timeout(300)
    en = page.locator("body").inner_text()
    page.screenshot(path=str(out / "english.png"), full_page=True)
    zh_checks = {x: x in zh for x in ["AI 原生 B5G 智慧城市模擬平台", "城市事件控制台", "送出情境"]}
    en_checks = {x: x in en for x in ["AI-Native B5G Smart City Simulator", "City Event Console", "Submit Scenario"]}
    result = {
        "zhPresent": all(zh_checks.values()),
        "enPresent": all(en_checks.values()),
        "zhChecks": zh_checks,
        "enChecks": en_checks,
        "sessionId": page.evaluate("sessionStorage.getItem('5gcityverse.browserSessionId')"),
        "mapExplanationPresent": "Colored arrows show the metric-driven" in en,
        "consoleErrors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    browser.close()
