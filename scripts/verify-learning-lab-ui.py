import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "learning-lab-ui"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def main() -> None:
    target_url = os.environ.get("TARGET_URL", "http://127.0.0.1:5173")
    access_token = os.environ.get("UI_ACCESS_TOKEN", "visual-verification-token")
    block_external = os.environ.get("BLOCK_EXTERNAL_REQUESTS") == "1"
    mock_websocket = os.environ.get("MOCK_WEBSOCKET") == "1"
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        if mock_websocket:
            page.add_init_script("""class MockWebSocket { constructor() { this.readyState = 0; setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen({}); }, 0); } send() {} close() { this.readyState = 3; } } MockWebSocket.OPEN = 1; window.WebSocket = MockWebSocket;""")
        if block_external:
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(target_url) else route.abort())

        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        if page.get_by_role("button", name="繁體中文").count():
            page.get_by_role("button", name="繁體中文").click()
            page.locator("#api-access-token").wait_for(timeout=10_000)
        if page.locator("#api-access-token").count():
            page.locator("#api-access-token").fill(access_token)
            page.get_by_role("button", name="進入模擬平台").click()

        try:
            page.get_by_text("B5G Network Slicing 學習實驗室").wait_for(timeout=15_000)
        except Exception:
            page.screenshot(path=str(ARTIFACTS / "verification-failure.png"), full_page=True)
            print(page.locator("body").inner_text())
            raise
        page.get_by_text("情境需求 → gNB → 5GC → Data Network").wait_for(timeout=15_000)
        assert page.get_by_text("5G Core 控制面", exact=True).count() == 1
        assert page.get_by_text("使用者面 UPF", exact=True).count() == 1
        assert page.get_by_text("封包路徑（目前路徑）", exact=True).count() == 1
        assert page.get_by_text("gNB", exact=True).count() >= 1
        assert page.get_by_text("SMF", exact=True).count() >= 1
        assert page.get_by_text("UPF", exact=True).count() >= 1
        assert page.get_by_text("Data Network", exact=True).count() >= 1
        assert page.get_by_text(re.compile(r"UL (?:—|[0-9])")).count() >= 1
        assert page.get_by_text(re.compile(r"DL (?:—|[0-9])")).count() >= 1
        if not block_external:
            page.get_by_text("CONNECTED", exact=True).wait_for(timeout=30_000)

        page.screenshot(path=str(ARTIFACTS / "desktop-step-1.png"), full_page=True)

        page.get_by_role("button", name="2 選擇策略").click()
        page.get_by_text("同一個需求，換一種資源分法").wait_for()
        page.screenshot(path=str(ARTIFACTS / "desktop-step-2.png"), full_page=True)

        page.get_by_role("button", name="工程資訊").click()
        page.get_by_text("系統與核心網觀測").wait_for()
        page.screenshot(path=str(ARTIFACTS / "engineering-drawer.png"), full_page=True)
        page.get_by_role("dialog", name="工程資訊").locator("button").first.click()

        mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        if mock_websocket:
            mobile.add_init_script("""class MockWebSocket { constructor() { this.readyState = 0; setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen({}); }, 0); } send() {} close() { this.readyState = 3; } } MockWebSocket.OPEN = 1; window.WebSocket = MockWebSocket;""")
        if block_external:
            mobile.route("**/*", lambda route: route.continue_() if route.request.url.startswith(target_url) else route.abort())
        mobile.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        if mobile.get_by_role("button", name="繁體中文").count():
            mobile.get_by_role("button", name="繁體中文").click()
            mobile.locator("#api-access-token").wait_for(timeout=10_000)
        if mobile.locator("#api-access-token").count():
            mobile.locator("#api-access-token").fill(access_token)
            mobile.get_by_role("button", name="進入模擬平台").click()
        mobile.get_by_text("B5G Network Slicing 學習實驗室").wait_for(timeout=15_000)
        mobile.screenshot(path=str(ARTIFACTS / "mobile-step-1.png"), full_page=True)

        browser.close()

    ignored_errors = ("WebSocket", "VITE_API_URL") + (("ERR_FAILED",) if block_external else ()) + (("ERR_UNSAFE_PORT", "ERR_CONNECTION_REFUSED") if mock_websocket else ())
    relevant_errors = [error for error in console_errors if not any(ignored in error for ignored in ignored_errors)]
    if relevant_errors:
        raise AssertionError("Unexpected browser console errors: " + " | ".join(relevant_errors))
    print(f"verified: {ARTIFACTS}")


if __name__ == "__main__":
    main()
