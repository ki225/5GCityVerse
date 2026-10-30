"""Real-browser acceptance for the city-daily native observation and AI trend UI.

Plan-only by default. ``--execute`` performs browser GETs and, only when the
native snapshot is available, one user-triggered AI trend POST. The additional
``--bootstrap`` mode may create its UI-owned baseline. It never mocks routes or
owns server/process lifecycle.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


SLICES = ("embb", "urllc", "mmtc", "v2x")
OBSERVATION_PATH = "/api/onprem/daily-observation"
TREND_PATH = "/api/onprem/daily-trend"
BOOTSTRAP_PATH = "/api/onprem/bootstrap"
UE_PATH = "/api/onprem/ue"
BASELINE = {"embb": 1, "urllc": 0, "mmtc": 0, "v2x": 0}


class BootstrapRunError(RuntimeError):
    def __init__(self, message: str, ownership: dict | None = None):
        super().__init__(message)
        self.ownership = ownership


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_observation(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["observation is not an object"]
    errors: list[str] = []
    for key in ("snapshotId", "available", "source", "observedAt", "slices"):
        if key not in value:
            errors.append(f"observation missing {key}")
    if not isinstance(value.get("snapshotId"), str) or not value.get("snapshotId"):
        errors.append("observation snapshotId is empty")
    if not isinstance(value.get("available"), bool):
        errors.append("observation availability is not boolean")
    if value.get("available") is True:
        if not value.get("observedAt"):
            errors.append("available observation has no observedAt")
        try:
            stamp = datetime.fromisoformat(str(value.get("observedAt", "")).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone missing")
        except ValueError:
            errors.append("observedAt is not timezone-aware ISO-8601")
        slices = value.get("slices")
        if not isinstance(slices, dict) or set(slices) != set(SLICES):
            errors.append("available observation does not contain exactly four required slices")
        else:
            for name in SLICES:
                row = slices[name]
                if not isinstance(row, dict) or not any(k in row for k in ("ueCount", "registered")):
                    errors.append(f"slice {name} has no aggregate count")
    elif not isinstance(value.get("reason"), str) or not value.get("reason", "").strip():
        errors.append("unavailable observation has no reason")
    return errors


def validate_rendered(rendered: dict, observation: dict) -> list[str]:
    errors: list[str] = []
    expected = {
        "snapshotId": str(observation.get("snapshotId", "")),
        "observedAt": str(observation.get("observedAt") or ""),
        "source": json.dumps(observation.get("source"), ensure_ascii=False, separators=(",", ":")),
        "available": str(observation.get("available", False)).lower(),
    }
    for key, value in expected.items():
        if rendered.get(key) != value:
            errors.append(f"rendered {key} differs from API")
    if observation.get("available"):
        rows = rendered.get("slices", {})
        if set(rows) != set(SLICES):
            errors.append("rendered slice set is incomplete")
        else:
            for name in SLICES:
                api_row = observation["slices"][name]
                expected_count = api_row.get("ueCount", api_row.get("registered"))
                if rows[name].get("count") != str(expected_count):
                    errors.append(f"rendered {name} aggregate differs from API")
    else:
        if rendered.get("reason") != str(observation.get("reason") or ""):
            errors.append("rendered unavailable reason differs from API")
        if rendered.get("trendDisabled") is not True:
            errors.append("AI trend control is enabled while evidence is unavailable")
    return errors


def validate_trend(trend: object, observation: dict, rendered_text: str) -> list[str]:
    if not isinstance(trend, dict):
        return ["trend response is not an object"]
    errors = []
    if trend.get("snapshotId") != observation.get("snapshotId"):
        errors.append("trend snapshotId differs from displayed observation")
    if trend.get("observedAt") != observation.get("observedAt"):
        errors.append("trend observedAt differs from displayed observation")
    if not isinstance(trend.get("trend"), str) or not trend.get("trend", "").strip():
        errors.append("trend response text is empty")
    elif rendered_text.strip() != trend["trend"].strip():
        errors.append("rendered trend differs from POST response")
    return errors


def actionable_network_errors(values: list[dict]) -> list[dict]:
    """Ignore only React's expected cancellation of known polling GETs."""
    cancellable_gets = {BOOTSTRAP_PATH, UE_PATH, "/api/onprem/scenario-rounds/current"}
    return [value for value in values if not (
        value.get("method") == "GET" and urlsplit(value.get("url", "")).path in cancellable_gets
        and "ERR_ABORTED" in str(value.get("error", "")))]


def required_daily_errors(observation: dict, trend_requests: list[dict], *, required: bool) -> list[str]:
    if not required:
        return []
    errors = []
    if observation.get("available") is not True:
        errors.append("city daily observation remained unavailable after bounded retry")
    posts = [item for item in trend_requests if item.get("method") == "POST"
             and urlsplit(item.get("url", "")).path == TREND_PATH]
    if len(posts) != 1:
        errors.append(f"expected exactly one AI trend POST, observed {len(posts)}")
    return errors


def persist_trend_response(output: Path, response) -> object:
    """Persist the HTTP outcome before any DOM wait can time out."""
    try:
        body = response.json()
    except Exception:
        body = {"nonJsonBody": response.text()[:2048]}
    write_json(output / "trend-response.json", {"status": response.status, "body": body})
    return body


def read_panel(page) -> dict:
    panel = page.get_by_test_id("daily-native-observation")
    rows = {}
    for name in SLICES:
        row = page.get_by_test_id(f"daily-native-slice-{name}")
        if row.count() == 1:
            rows[name] = {"count": row.get_attribute("data-count"), "text": row.inner_text()}
    source = panel.get_attribute("data-source") or ""
    return {
        "snapshotId": panel.get_attribute("data-snapshot-id") or "",
        "observedAt": panel.get_attribute("data-observed-at") or "",
        "source": source,
        "available": panel.get_attribute("data-available") or "",
        "reason": panel.get_attribute("data-reason") or "",
        "trendDisabled": page.get_by_role("button", name=re.compile(r"^(AI trend|AI 趨勢)$")).is_disabled(),
        "slices": rows,
    }


def remaining_ms(deadline: float) -> int:
    value = int((deadline - time.monotonic()) * 1000)
    if value <= 0:
        raise RuntimeError("overall deadline exceeded")
    return value


def ue_counts(status: dict) -> dict:
    return {name: status.get("slices", {}).get(name, {}).get("requested") for name in SLICES}


def owned_baseline_generation(before: dict, status: dict, attempt_id: str) -> int | None:
    generation = status.get("generation")
    if (not attempt_id or type(generation) is not int or generation <= before.get("generation", -1)
            or ue_counts(status) != BASELINE):
        return None
    return generation


def baseline_ready(status: dict, generation: int) -> bool:
    return (status.get("generation") == generation and ue_counts(status) == BASELINE
            and status.get("status") == "established" and status.get("registered") == 1
            and status.get("pduEstablished") == 1 and status.get("trafficVerified") == 1
            and status.get("errors") == [])


def bootstrap_through_ui(page, api_url: str, output: Path, deadline: float) -> dict:
    """Click the visible gate and bind the resulting attempt to one new baseline generation."""
    before_bootstrap = page.request.get(api_url + BOOTSTRAP_PATH, timeout=remaining_ms(deadline)).json()
    before_ue = page.request.get(api_url + UE_PATH, timeout=remaining_ms(deadline)).json()
    write_json(output / "bootstrap-before.json", {"bootstrap": before_bootstrap, "ue": before_ue})
    if before_bootstrap.get("state") != "absent":
        raise RuntimeError("--bootstrap requires the rendered bootstrap state to be absent")
    refresh = page.get_by_role("button", name=re.compile(r"^(Check status again|重新查詢狀態)$"))
    if refresh.count() == 1 and refresh.is_visible():
        refresh.click()
    button = page.get_by_role("button", name=re.compile(r"^(Start city|啟動城市)$"))
    visible_deadline = min(deadline, time.monotonic() + 15)
    while (button.count() != 1 or not button.is_visible() or button.is_disabled()) and time.monotonic() < visible_deadline:
        page.wait_for_timeout(250)
    if button.count() != 1 or not button.is_visible() or button.is_disabled():
        raise RuntimeError("No unique enabled visible Start city control")
    with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == BOOTSTRAP_PATH + "/start",
                              timeout=remaining_ms(deadline)) as pending:
        button.click()
    accepted_response = pending.value
    accepted = accepted_response.json()
    attempt_id = accepted.get("attemptId")
    write_json(output / "bootstrap-intent.json", {"status": accepted_response.status, "response": accepted})
    if accepted_response.status != 202 or not isinstance(attempt_id, str) or not attempt_id:
        raise RuntimeError("UI bootstrap intent was not uniquely accepted")
    generation = None
    sequence = 0
    while time.monotonic() < deadline:
        bootstrap = page.request.get(api_url + BOOTSTRAP_PATH, timeout=remaining_ms(deadline)).json()
        status = page.request.get(api_url + UE_PATH, timeout=remaining_ms(deadline)).json()
        sequence += 1
        write_json(output / f"bootstrap-sample-{sequence:05d}.json", {"bootstrap": bootstrap, "ue": status})
        if bootstrap.get("attemptId") != attempt_id:
            raise RuntimeError("Bootstrap attempt identity changed")
        candidate = owned_baseline_generation(before_ue, status, attempt_id)
        if candidate is not None:
            if generation is not None and candidate != generation:
                raise BootstrapRunError("Owned bootstrap generation changed",
                                        {"attemptId": attempt_id, "generation": generation, "baseline": BASELINE})
            generation = candidate
            ownership = {"attemptId": attempt_id, "generation": generation, "baseline": BASELINE}
            write_json(output / "bootstrap-ownership.json", ownership)
        if bootstrap.get("state") == "failed":
            raise BootstrapRunError(
                "Owned bootstrap failed: " + str(bootstrap.get("error", {}).get("code") or "unknown"),
                {"attemptId": attempt_id, "generation": generation, "baseline": BASELINE}
                if generation is not None else None)
        if bootstrap.get("state") == "ready" and generation is not None and baseline_ready(status, generation):
            page.get_by_test_id("daily-native-observation").wait_for(state="visible", timeout=remaining_ms(deadline))
            return {"attemptId": attempt_id, "generation": generation, "baseline": BASELINE}
        page.wait_for_timeout(2000)
    raise BootstrapRunError("Owned bootstrap deadline exceeded",
                            {"attemptId": attempt_id, "generation": generation, "baseline": BASELINE}
                            if generation is not None else None)


def cleanup_owned_baseline(page, api_url: str, output: Path, ownership: dict, deadline: float) -> dict:
    """Use the rendered fleet form to request zero only for this runner's exact generation."""
    current = page.request.get(api_url + UE_PATH, timeout=remaining_ms(deadline)).json()
    if current.get("generation") != ownership["generation"] or ue_counts(current) != BASELINE:
        raise RuntimeError("Cleanup refused: current baseline generation is not owned")
    for name in SLICES:
        page.locator("#ue-" + name).fill("0")
    with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == UE_PATH,
                              timeout=remaining_ms(deadline)) as pending:
        page.get_by_role("button", name=re.compile(r"^(Apply persistent UE settings|套用常駐 UE 設定)$")).click()
    response = pending.value
    intent = response.json()
    zero_generation = intent.get("generation")
    write_json(output / "cleanup-intent.json", {"status": response.status, "request": response.request.post_data_json,
                                                 "response": intent, "owned": ownership})
    if (response.status != 202 or response.request.post_data_json != dict.fromkeys(SLICES, 0)
            or type(zero_generation) is not int or zero_generation <= ownership["generation"]):
        raise RuntimeError("Owned zero cleanup was not accepted exactly")
    sequence = 0
    while time.monotonic() < deadline:
        status = page.request.get(api_url + UE_PATH, timeout=remaining_ms(deadline)).json()
        sequence += 1
        write_json(output / f"cleanup-sample-{sequence:05d}.json", status)
        if status.get("generation") != zero_generation:
            raise RuntimeError("Cleanup generation was superseded")
        if (ue_counts(status) == dict.fromkeys(SLICES, 0) and status.get("registered") == 0
                and status.get("pduEstablished") == 0 and status.get("trafficVerified") == 0
                and status.get("status") == "stopped" and status.get("errors") == []):
            return {"generation": zero_generation, "status": "verified-zero"}
        page.wait_for_timeout(2000)
    raise RuntimeError("Owned baseline cleanup deadline exceeded")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Open the live UI and permit one AI trend POST")
    parser.add_argument("--bootstrap", action="store_true", help="When the visible gate is absent, click Start city and verify its baseline")
    parser.add_argument("--cleanup", action="store_true", help="After success, normally zero only the baseline generation owned by this run")
    parser.add_argument("--ui-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=900, help="Overall browser deadline in seconds (10..1800)")
    args = parser.parse_args(argv)
    if not 10 <= args.timeout <= 1800:
        parser.error("Timeout must be 10..1800 seconds")
    if args.bootstrap and not args.execute:
        parser.error("--bootstrap requires --execute")
    if args.cleanup and not args.bootstrap:
        parser.error("--cleanup requires --bootstrap ownership")
    for url in (args.ui_url, args.api_url):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            parser.error("Only loopback HTTP endpoints are allowed")
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "plan-only", "execute": args.execute, "scope": "city-daily-live-browser-no-mocks",
               "uiUrl": args.ui_url, "apiUrl": args.api_url, "timeoutSeconds": args.timeout,
               "bootstrap": args.bootstrap, "cleanup": args.cleanup,
               "mutationsAllowed": ["one user-triggered POST /api/onprem/daily-trend when available"]
               + (["visible Start city click"] if args.bootstrap else [])
               + (["visible Apply UE counts zero click for owned baseline"] if args.cleanup else []),
               "ueLifecycleOwned": False}
    write_json(args.output / "summary.json", summary)
    if not args.execute:
        print("Plan only; browser/API requests executed 0")
        return

    from playwright.sync_api import sync_playwright
    deadline = time.monotonic() + args.timeout
    console_errors: list[str] = []
    network_errors: list[dict] = []
    trend_requests: list[dict] = []
    ownership = None
    cleanup_started = False
    browser = None
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1560, "height": 1200})
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))
            page.on("requestfailed", lambda req: network_errors.append(
                {"method": req.method, "url": req.url, "error": req.failure}))
            page.on("response", lambda res: network_errors.append({"url": res.url, "status": res.status})
                    if res.status >= 400 else None)
            page.on("request", lambda req: trend_requests.append({"method": req.method, "url": req.url})
                    if urlsplit(req.url).path == TREND_PATH else None)
            summary["status"] = "running"
            write_json(args.output / "summary.json", summary)
            page.goto(args.ui_url, wait_until="networkidle", timeout=max(1, int((deadline-time.monotonic())*1000)))
            language = page.get_by_role("button", name="English")
            if language.count() == 1:
                language.click()
            bootstrap_state = page.request.get(args.api_url.rstrip("/") + BOOTSTRAP_PATH,
                                               timeout=remaining_ms(deadline)).json()
            settling_deadline = min(deadline, time.monotonic() + 90)
            while (bootstrap_state.get("error", {}).get("code") == "UE_ZERO_SETTLING"
                   and time.monotonic() < settling_deadline):
                page.wait_for_timeout(2000)
                bootstrap_state = page.request.get(args.api_url.rstrip("/") + BOOTSTRAP_PATH,
                                                   timeout=remaining_ms(deadline)).json()
            if bootstrap_state.get("state") == "absent":
                if not args.bootstrap:
                    raise RuntimeError("City bootstrap is absent; rerun explicitly with --execute --bootstrap")
                try:
                    ownership = bootstrap_through_ui(page, args.api_url.rstrip("/"), args.output, deadline)
                except BootstrapRunError as bootstrap_error:
                    ownership = bootstrap_error.ownership
                    if ownership is not None:
                        summary.update(ueLifecycleOwned=True, bootstrapOwnership=ownership,
                                       cleanupRequired=args.cleanup,
                                       cleanupBlockedReason="bootstrap gate does not render the persistent UE form before ready")
                        write_json(args.output / "summary.json", summary)
                    raise
                summary["ueLifecycleOwned"] = True
                summary["bootstrapOwnership"] = ownership
                write_json(args.output / "summary.json", summary)
            elif bootstrap_state.get("state") != "ready":
                raise RuntimeError("Bootstrap preflight is neither absent nor ready: "
                                   + str(bootstrap_state.get("error", {}).get("code")
                                         or bootstrap_state.get("state") or "unknown"))
            panel = page.get_by_test_id("daily-native-observation")
            panel.wait_for(state="visible", timeout=max(1, int((deadline-time.monotonic())*1000)))
            require_daily = args.execute and args.bootstrap
            availability_deadline = min(deadline, time.monotonic() + 120)
            observation_attempt = 0
            while True:
                if observation_attempt and time.monotonic() >= availability_deadline:
                    break
                response = page.request.get(args.api_url.rstrip("/") + OBSERVATION_PATH,
                                            timeout=remaining_ms(availability_deadline))
                observation = response.json()
                rendered = read_panel(page)
                observation_attempt += 1
                write_json(args.output / f"observation-sample-{observation_attempt:05d}.json",
                           {"status": response.status, "body": observation, "rendered": rendered})
                daily_ready = (observation.get("available") is True
                               and not validate_observation(observation)
                               and not validate_rendered(rendered, observation))
                if daily_ready or not require_daily or time.monotonic() >= availability_deadline:
                    break
                page.wait_for_timeout(min(2000, remaining_ms(availability_deadline)))
            write_json(args.output / "observation.json", {"status": response.status, "body": observation})
            write_json(args.output / "rendered-observation.json", rendered)
            errors = ([] if response.ok else [f"observation HTTP {response.status}"])
            errors += validate_observation(observation) + validate_rendered(rendered, observation)
            page.screenshot(path=str(args.output / "observation.png"), full_page=True)
            if observation.get("available") is True and not errors:
                with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == TREND_PATH,
                                          timeout=max(1, int((deadline-time.monotonic())*1000))) as pending:
                    page.get_by_role("button", name=re.compile(r"^(AI trend|AI 趨勢)$")).click()
                trend_response = pending.value
                trend = persist_trend_response(args.output, trend_response)
                if not trend_response.ok:
                    raise RuntimeError(f"AI trend POST failed HTTP {trend_response.status}")
                page.get_by_test_id("daily-native-trend").wait_for(state="visible")
                rendered_trend = page.get_by_test_id("daily-native-trend").inner_text()
                latest_response = page.request.get(args.api_url.rstrip("/") + OBSERVATION_PATH,
                                                   timeout=remaining_ms(deadline))
                latest_observation = latest_response.json()
                latest_rendered = read_panel(page)
                write_json(args.output / "trend.json", {"status": trend_response.status, "body": trend,
                                                         "rendered": rendered_trend,
                                                         "observation": latest_observation,
                                                         "renderedObservation": latest_rendered})
                errors += ([] if trend_response.ok else [f"trend HTTP {trend_response.status}"])
                errors += ([] if latest_response.ok else [f"post-trend observation HTTP {latest_response.status}"])
                errors += validate_observation(latest_observation)
                errors += validate_rendered(latest_rendered, latest_observation)
                errors += validate_trend(trend, latest_observation, rendered_trend)
            elif observation.get("available") is False and trend_requests:
                errors.append("trend request occurred while evidence was unavailable")
            errors += required_daily_errors(observation, trend_requests, required=require_daily)
            errors += [f"console: {value}" for value in console_errors]
            errors += [f"network: {value}" for value in actionable_network_errors(network_errors)]
            if time.monotonic() > deadline:
                errors.append("overall deadline exceeded")
            summary.update(status="failed" if errors else "passed", errors=errors,
                           observationAvailable=observation.get("available"), trendRequests=trend_requests,
                           consoleErrors=console_errors, networkErrors=network_errors)
            write_json(args.output / "summary.json", summary)
            page.screenshot(path=str(args.output / ("failure.png" if errors else "passed.png")), full_page=True)
            if errors:
                raise RuntimeError("; ".join(errors))
            if args.cleanup:
                if ownership is None:
                    raise RuntimeError("Cleanup refused: this run did not create the baseline")
                cleanup_started = True
                summary["cleanup"] = cleanup_owned_baseline(
                    page, args.api_url.rstrip("/"), args.output, ownership,
                    time.monotonic() + min(args.timeout, 600))
                summary["ueLifecycleOwned"] = False
                write_json(args.output / "summary.json", summary)
            print("PASS city-daily browser/API evidence and AI trend contract")
        except BaseException as exc:
            if args.cleanup and ownership is not None and not cleanup_started:
                try:
                    cleanup_started = True
                    summary["failureCleanup"] = cleanup_owned_baseline(
                        page, args.api_url.rstrip("/"), args.output, ownership,
                        time.monotonic() + min(args.timeout, 600))
                except Exception as cleanup_exc:
                    summary["cleanupError"] = str(cleanup_exc)
            elif args.cleanup and ownership is not None and cleanup_started and "cleanup" not in summary:
                summary["cleanupNeedsReconciliation"] = True
            if ownership is not None:
                summary["ueLifecycleOwned"] = "cleanup" not in summary and "failureCleanup" not in summary
            summary.update(status="failed", error=str(exc), errorType=type(exc).__name__,
                           consoleErrors=console_errors, networkErrors=network_errors,
                           trendRequests=trend_requests)
            write_json(args.output / "summary.json", summary)
            try:
                page.screenshot(path=str(args.output / "failure.png"), full_page=True)
            except Exception as capture_exc:
                summary["screenshotError"] = str(capture_exc)
                write_json(args.output / "summary.json", summary)
            raise
        finally:
            if browser is not None:
                browser.close()


if __name__ == "__main__":
    main()
