#!/usr/bin/env python3
"""
Wait until the deployed API reports a live baseline mMTC PDU session.

This verifies the user-visible path used by the dashboard:
API Gateway -> backend Lambda -> free5GC OAM/EKS -> mMTC slice state.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


API_URL = os.environ.get("API_URL", "").rstrip("/")
EXPECTED_SESSIONS = int(os.environ.get("EXPECTED_MMTC_SESSIONS", "1"))
ATTEMPTS = int(os.environ.get("BASELINE_VERIFY_ATTEMPTS", "18"))
DELAY_SECONDS = int(os.environ.get("BASELINE_VERIFY_DELAY_SECONDS", "20"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("BASELINE_VERIFY_HTTP_TIMEOUT_SECONDS", "20"))


def fetch_status() -> dict:
    req = urllib.request.Request(f"{API_URL}/free5gc/status", method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as res:
        data = json.loads(res.read().decode("utf-8") or "{}")
    if isinstance(data.get("body"), str):
        return json.loads(data["body"])
    return data


def mmtc_sessions(status: dict) -> int:
    metrics = status.get("metrics") or {}
    slice_sessions = metrics.get("sliceSessions") or {}
    if "3" in slice_sessions:
        return int(slice_sessions["3"] or 0)
    if 3 in slice_sessions:
        return int(slice_sessions[3] or 0)
    for item in status.get("slices") or []:
        if int(item.get("sst") or 0) == 3:
            return int(item.get("sessions") or 0)
    return 0


def main() -> int:
    if not API_URL:
        print("API_URL is required", file=sys.stderr)
        return 2

    last_status: dict = {}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            last_status = fetch_status()
            sessions = mmtc_sessions(last_status)
            pdu_sessions = int((last_status.get("metrics") or {}).get("pduSessionCount") or 0)
            warning = last_status.get("warning") or ""
            print(
                f"baseline mMTC check {attempt}/{ATTEMPTS}: "
                f"mMTC sessions={sessions}, PDU sessions={pdu_sessions}, warning={warning or '-'}"
            )
            if last_status.get("connected") and sessions >= EXPECTED_SESSIONS and pdu_sessions >= EXPECTED_SESSIONS:
                print("baseline mMTC verification passed")
                return 0
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            print(f"baseline mMTC check {attempt}/{ATTEMPTS} failed: {exc}", file=sys.stderr)

        if attempt < ATTEMPTS:
            time.sleep(DELAY_SECONDS)

    print("baseline mMTC verification failed; final status:", file=sys.stderr)
    print(json.dumps(last_status, indent=2, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
