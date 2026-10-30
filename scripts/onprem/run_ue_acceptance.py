"""Run explicit live UE gates against the on-prem API; never fabricate evidence.

Without --execute this only reads status. Live runs require a configured kubeadm
runtime and retain one JSON observation per poll. This tests the public evidence
contract; independent NF/UPF collection remains the backend's responsibility.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import time
from urllib.request import Request, urlopen


SLICES = ("embb", "urllc", "mmtc", "v2x")
POOLS = dict(zip(SLICES, (ipaddress.ip_network(f"10.{n}.0.0/21") for n in range(101, 105))))
ENDPOINTS = {name: f"172.22.{61 + index}.20" for index, name in enumerate(SLICES)}
COUNTERS = ("requested", "allocated", "registered", "pduEstablished", "trafficVerified")
MAX_EVIDENCE_AGE = 15


def parse_gates(value: str) -> tuple[int, ...]:
    """Select explicit positive acceptance targets without changing runtime limits."""
    parts = value.split(",")
    if any(not part.isdecimal() for part in parts):
        raise argparse.ArgumentTypeError("gates must be comma-separated integers from 1 to 1000")
    gates = tuple(int(part) for part in parts)
    if any(not 1 <= total <= 1000 for total in gates) or tuple(sorted(set(gates))) != gates:
        raise argparse.ArgumentTypeError("gates must be unique, increasing integers from 1 to 1000")
    return gates


def timestamp_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def fresh_timestamp(value: object, now: float) -> bool:
    observed = timestamp_seconds(value)
    return observed is not None and now - MAX_EVIDENCE_AGE <= observed <= now + 1


def observation_sequence_errors(observed: float | None, previous: float | None) -> list[str]:
    """A fresh repeated observation is valid; only regression or a gap breaks continuity."""
    if observed is None or previous is not None and observed < previous:
        return ["Evidence timestamp regressed or is absent"]
    if previous is not None and observed - previous > MAX_EVIDENCE_AGE:
        return ["Evidence gap exceeds the freshness window"]
    return []


def terminal_gate_failure(status: dict) -> bool:
    """Transient unavailable evidence resets stability; a failed generation cannot recover."""
    return status.get("status") == "failed"


def ready_to_execute(status: dict, now: float | None = None) -> bool:
    """Require a verified baseline before authorizing the first mutating request."""
    now = time.time() if now is None else now
    zero = (status.get("status") == "stopped" and status.get("perUe") == []
            and type(status.get("failed")) is int and status["failed"] == 0
            and all(type(status.get(key)) is int and status[key] == 0 for key in COUNTERS))
    sources = status.get("evidenceSources")
    return (status.get("runtime") == "kubeadm"
            and (zero or status.get("status") == "established")
            and type(status.get("generation")) is int and status["generation"] >= 0
            and (status["generation"] > 0 or status["status"] == "stopped")
            and fresh_timestamp(status.get("observedAt"), now)
            and isinstance(sources, list)
            and len(sources) == (4 if zero else 5)
            and all(isinstance(source, str) and source.strip() for source in sources)
            and status.get("errors") == [])


def distribution(total: int) -> dict[str, int]:
    """Exercise all four slices, while retaining exact totals for small gates."""
    return {name: total // 4 + (index < total % 4) for index, name in enumerate(SLICES)}


def evidence_errors(status: dict, desired: dict[str, int], generation: int, now: float | None = None) -> list[str]:
    errors: list[str] = []
    now = time.time() if now is None else now
    if not isinstance(status, dict):
        return ["Status response must be an object"]
    total = sum(desired.values())
    if status.get("runtime") != "kubeadm" or type(status.get("generation")) is not int or status.get("generation") != generation:
        errors.append("Wrong runtime or generation")
    if status.get("status") != "established":
        errors.append("Generation is not established")
    if any(type(status.get(key)) is not int or status[key] != total for key in COUNTERS):
        errors.append("Aggregate counters do not equal the requested total")
    if type(status.get("failed")) is not int or status.get("failed") != 0:
        errors.append("Failed UE count is nonzero or absent")
    if not fresh_timestamp(status.get("observedAt"), now):
        errors.append("Observation timestamp is absent, malformed, stale or future-dated")
    sources = status.get("evidenceSources")
    if not isinstance(sources, list) or len(sources) != 5 or not all(isinstance(source, str) and source.strip() for source in sources):
        errors.append("Missing complete UE/AMF/SMF/UPF/traffic evidence sources")
    if status.get("errors") != []:
        errors.append("Backend reported errors or omitted its error list")
    stable = timestamp_seconds(status.get("stableSince"))
    observed_time = timestamp_seconds(status.get("observedAt"))
    if stable is None or observed_time is None or observed_time - stable < 60:
        errors.append("Backend has not evidenced a 60-second stable window")
    slices = status.get("slices")
    if not isinstance(slices, dict) or set(slices) != set(SLICES):
        errors.append("Missing or malformed slice observations")
        slices = {}
    for name, count in desired.items():
        observed = slices.get(name, {})
        if not isinstance(observed, dict):
            errors.append(f"{name}: malformed slice observation")
            continue
        if any(type(observed.get(key)) is not int or observed[key] != count for key in COUNTERS):
            errors.append(f"{name}: counters differ from {count}")
        if type(observed.get("failed")) is not int or observed.get("failed") != 0:
            errors.append(f"{name}: missing or nonzero failed count")
    records = status.get("perUe", [])
    if not isinstance(records, list) or len(records) != total:
        return errors + ["Missing per-UE records: sampling cannot pass a gate"]
    supis, addresses, interfaces = set(), set(), set()
    counts = dict.fromkeys(SLICES, 0)
    for record in records:
        if not isinstance(record, dict):
            errors.append("Malformed per-UE record")
            continue
        name, supi = record.get("slice"), record.get("supi")
        if not isinstance(name, str) or name not in desired or not isinstance(supi, str) or not supi:
            errors.append("Invalid UE identity or slice")
            continue
        counts[name] += 1
        if type(record.get("generation")) is not int or record["generation"] != generation:
            errors.append(f"{supi}: UE belongs to another generation")
        if supi in supis:
            errors.append(f"Duplicate SUPI: {supi}")
        supis.add(supi)
        if any(record.get(key) is not True for key in ("registered", "pduEstablished", "trafficVerified")):
            errors.append(f"{supi}: incomplete UE evidence")
        try:
            raw_address = record.get("ip")
            if not isinstance(raw_address, str):
                raise ValueError("PDU IP must be a string")
            address = ipaddress.ip_address(raw_address)
            if address not in POOLS[name] or address in addresses or address in {POOLS[name].network_address, POOLS[name].broadcast_address}:
                errors.append(f"{supi}: duplicate or wrong-pool PDU IP")
            addresses.add(address)
        except ValueError:
            errors.append(f"{supi}: invalid PDU IP")
        interface = (record.get("podUid"), record.get("interface"))
        if not all(isinstance(value, str) and value.strip() for value in interface):
            errors.append(f"{supi}: malformed UE TUN interface identity")
        elif interface in interfaces:
            errors.append(f"{supi}: missing or reused UE TUN interface")
        else:
            interfaces.add(interface)
        session_id = record.get("sessionId")
        if not isinstance(session_id, str) or not session_id.isdigit() or not 1 <= int(session_id) <= 255:
            errors.append(f"{supi}: invalid session identity")
        if record.get("upf") != f"upf-{name}":
            errors.append(f"{supi}: wrong UPF mapping")
        if record.get("endpoint") != ENDPOINTS[name]:
            errors.append(f"{supi}: wrong or missing N6 endpoint")
        if not fresh_timestamp(record.get("observedAt"), now):
            errors.append(f"{supi}: stale, future or malformed probe timestamp")
    if counts != desired:
        errors.append("Per-UE slice membership does not match the desired distribution")
    return errors


def request(base_url: str, body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode()
    req = Request(base_url.rstrip("/") + "/api/onprem/ue", data=payload,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Status response must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Actually request the explicitly selected UE gates")
    parser.add_argument("--gates", type=parse_gates, default=(1, 10, 100, 500, 1000),
                        help="Comma-separated increasing UE totals; use --gates 50 for the 50-UE acceptance run")
    parser.add_argument("--timeout", type=float, default=1800, help="Seconds per gate")
    parser.add_argument("--poll", type=float, default=5)
    args = parser.parse_args()
    if not math.isfinite(args.poll) or not math.isfinite(args.timeout) or not 0 < args.poll <= MAX_EVIDENCE_AGE or args.timeout < 60:
        parser.error("poll must be between 0 and 15 seconds; timeout must be finite and at least 60 seconds")
    run_dir = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    status = request(args.base_url)
    (run_dir / "initial.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    if not args.execute:
        print(json.dumps({"mode": "read_only", "status": status.get("status"), "evidence": str(run_dir)}))
        return 0
    if not ready_to_execute(status):
        raise RuntimeError("A configured kubeadm runtime with available evidence is required")
    for total in args.gates:
        desired = distribution(total)
        submitted = request(args.base_url, desired)
        generation = submitted.get("generation")
        if type(generation) is not int or generation <= 0:
            raise RuntimeError("Submission returned an invalid generation")
        deadline, stable_since, sequence = time.monotonic() + args.timeout, None, 0
        last_observed = None
        while time.monotonic() < deadline:
            status = request(args.base_url)
            sequence += 1
            errors = evidence_errors(status, desired, generation)
            observed = timestamp_seconds(status.get("observedAt"))
            errors.extend(observation_sequence_errors(observed, last_observed))
            last_observed = observed
            artifact = {"desired": desired, "status": status, "validationErrors": errors}
            (run_dir / f"gate-{total:04d}-{sequence:05d}.json").write_text(
                json.dumps(artifact, indent=2), encoding="utf-8")
            if status.get("generation") != generation:
                raise RuntimeError("Another request superseded this gate; no further mutations")
            if terminal_gate_failure(status):
                raise RuntimeError(f"Gate {total} cannot be verified: {status.get('status')}")
            if errors:
                stable_since = None
            elif stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 60:
                print(f"Gate {total} passed; evidence: {run_dir}", flush=True)
                break
            time.sleep(args.poll)
        else:
            raise RuntimeError(f"Gate {total} timed out; partial evidence retained at {run_dir}")
    print(f"All gates passed. The final {args.gates[-1]}-UE workload remains running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
