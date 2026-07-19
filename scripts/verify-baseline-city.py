#!/usr/bin/env python3
"""
Wait until the deployed API reports resident baseline traffic.

This verifies the user-visible path used by the dashboard:
API Gateway -> backend Lambda -> free5GC OAM/EKS -> city resident bearer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


API_URL = os.environ.get("API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("CITYVERSE_API_TOKEN", "").strip()
EXPECTED_SESSIONS = int(os.environ.get("EXPECTED_BASELINE_SESSIONS", "1"))
ATTEMPTS = int(os.environ.get("BASELINE_VERIFY_ATTEMPTS", "18"))
DELAY_SECONDS = int(os.environ.get("BASELINE_VERIFY_DELAY_SECONDS", "20"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("BASELINE_VERIFY_HTTP_TIMEOUT_SECONDS", "20"))
NAMESPACE = os.environ.get("FREE5GC_NAMESPACE", "free5gc")
RESIDENT_UE_SELECTOR = os.environ.get(
    "RESIDENT_UE_SELECTOR",
    "app=ueransim,component=ue",
)
RESIDENT_BASELINE_CONTAINER = "resident-baseline-iperf3"
RESIDENT_BASELINE_RATE = os.environ.get("RESIDENT_BASELINE_RATE", "1M")


def kubectl(*args: str, timeout: int = 20) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def resident_pod_evidence() -> tuple[bool, str]:
    pod = kubectl(
        "-n", NAMESPACE, "get", "pod", "-l", RESIDENT_UE_SELECTOR,
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    if not pod:
        return False, "resident UE pod not found"
    containers = kubectl(
        "-n", NAMESPACE, "get", "pod", pod,
        "-o", "jsonpath={.spec.containers[*].name}",
    ).split()
    if RESIDENT_BASELINE_CONTAINER not in containers:
        return False, f"{pod} has no {RESIDENT_BASELINE_CONTAINER} sidecar"
    kubectl(
        "-n", NAMESPACE, "exec", pod, "-c", RESIDENT_BASELINE_CONTAINER,
        "--", "sh", "-c", "test -e /sys/class/net/uesimtun0",
    )
    logs = kubectl(
        "-n", NAMESPACE, "logs", pod, "-c", RESIDENT_BASELINE_CONTAINER,
        "--tail=120",
    )
    marker = (
        "transport=free5gc-tun" in logs
        and "interface=uesimtun0" in logs
        and f"rate={RESIDENT_BASELINE_RATE}" in logs
    )
    measured = bool(re.search(r"\b(?:K|M|G)bits/sec\b", logs))
    if not marker:
        return False, f"{pod} baseline provenance marker missing"
    if not measured:
        return False, f"{pod} baseline has not produced an iperf measurement"
    return True, f"pod={pod}, container={RESIDENT_BASELINE_CONTAINER}, interface=uesimtun0"


def is_resident_tun_sample(metrics: dict) -> bool:
    sample = metrics.get("iperf3") or {}
    pod = str(sample.get("pod") or "")
    return (
        sample.get("interface") == "uesimtun0"
        and sample.get("transport") == "free5gc-tun"
        and pod.startswith("ueransim-city-ue-")
    )


def fetch_status() -> dict:
    headers = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
    req = urllib.request.Request(
        f"{API_URL}/free5gc/status", headers=headers, method="GET"
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as res:
        data = json.loads(res.read().decode("utf-8") or "{}")
    if isinstance(data.get("body"), str):
        return json.loads(data["body"])
    return data


def main() -> int:
    if not API_URL:
        print("API_URL is required", file=sys.stderr)
        return 2

    last_status: dict = {}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            last_status = fetch_status()
            metrics = last_status.get("metrics") or {}
            pdu_sessions = int(metrics.get("pduSessionCount") or 0)
            throughput = float(metrics.get("throughputMbps") or 0)
            tun_probe = metrics.get("ueTunProbe") or {}
            tun_ready = tun_probe.get("ready") is True
            resident_sample = is_resident_tun_sample(metrics)
            pod_ok, pod_evidence = resident_pod_evidence()
            warning = last_status.get("warning") or ""
            print(
                f"resident baseline check {attempt}/{ATTEMPTS}: "
                f"PDU sessions={pdu_sessions}, throughput={throughput:.3f} Mbps, "
                f"TUN ready={tun_ready}, resident sample={resident_sample}, "
                f"pod evidence={pod_evidence}, warning={warning or '-'}"
            )
            if (
                last_status.get("connected")
                and pdu_sessions >= EXPECTED_SESSIONS
                and throughput > 0
                and tun_ready
                and resident_sample
                and pod_ok
            ):
                print("resident baseline verification passed")
                return 0
        except (
            TimeoutError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            print(f"resident baseline check {attempt}/{ATTEMPTS} failed: {exc}", file=sys.stderr)

        if attempt < ATTEMPTS:
            time.sleep(DELAY_SECONDS)

    print("resident baseline verification failed; final status:", file=sys.stderr)
    print(json.dumps(last_status, indent=2, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
