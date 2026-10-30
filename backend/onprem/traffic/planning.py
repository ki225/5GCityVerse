"""Pure, versioned offered-load planning; not a native capability or QoS proof.

Input rows bind exact SUPIs to scenario/slice owners. Transient round, generation,
Pod and PDU fields are deliberately outside the comparison fingerprint: callers
must separately validate and journal those identities before traffic execution.
Rates are integer kbps PER DIRECTION; simultaneous UL+DL doubles their sum.
Over-budget workloads use proportional largest-remainder apportionment with a
one-kbps floor. No runtime imports, I/O, admission changes or policy operations.
"""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import re


PROFILE_ID = "scenario-offered-load-v1"
QOS_PROFILE_ID = "scenario-qos-offered-load-v1"
SERVICE_PROFILE_ID = "scenario-service-offered-load-v1"
QOS_MAX_KBPS = 400_000  # Public limit: 100 participants, at most 4,000 each.
PROBE_PROFILE_ID = "controlled_qer_probe_v1"
from backend.onprem.scenarios.profiles import DEFAULT_TRAFFIC
_PROFILES = {name:(row['slice'],round(max(row['offeredUplinkMbpsPerUe'],row['offeredDownlinkMbpsPerUe'])*1000)) for name,row in DEFAULT_TRAFFIC.items()}


def valid_rate(value):
    return (type(value) is int and value >= 1 or isinstance(value, dict)
            and set(value) == {"ul", "dl"} and all(type(v) is int and v >= 1 for v in value.values()))


def direction_rate(value, direction):
    if direction not in {"ul", "dl"} or not valid_rate(value):
        raise ValueError("Invalid directional offered load")
    return value if type(value) is int else value[direction]


def rate_budget(rates):
    """One scalar envelope bounds each direction, including asymmetric cohorts."""
    return max((sum(direction_rate(v, d) for v in rates.values()) for d in ("ul", "dl")), default=0)


@dataclass(frozen=True)
class UEWorkload:
    supi: str
    scenario: str
    slice: str
    requested_kbps: int
    applied_kbps: int


@dataclass(frozen=True)
class WorkloadPlan:
    profile_id: str
    budget_kbps_per_direction: int
    per_ue: tuple[UEWorkload, ...]
    requested_ul_kbps: int
    requested_dl_kbps: int
    requested_combined_kbps: int
    applied_ul_kbps: int
    applied_dl_kbps: int
    applied_combined_kbps: int
    cap_reason: str | None
    comparison_fingerprint: str
    qos_effect_claimed: bool = False


def _apportion(requested, budget):
    """Integer proportional shares with deterministic ties and a positive floor."""
    active = list(range(len(requested)))
    rates = [0] * len(requested)
    remaining = budget
    while active:
        weight = sum(requested[i] for i in active)
        small = [i for i in active if requested[i] * remaining < weight]
        if not small:
            fractions = {}
            for i in active:
                rates[i], fractions[i] = divmod(requested[i] * remaining, weight)
            leftover = remaining - sum(rates[i] for i in active)
            # Input is already canonical SUPI/scenario order.
            for i in sorted(active, key=lambda i: (-fractions[i], i))[:leftover]:
                rates[i] += 1
            break
        for i in small:
            rates[i] = 1
            remaining -= 1
        active = [i for i in active if i not in small]
    return rates


def plan_workload(bindings, *, budget_kbps=10_000, traffic_mode="functional"):
    """Return an immutable plan for 0..500 exact SUPI/scenario/slice assignments.

    Budget must be integer 1..10000 kbps and sufficient for one kbps per UE.
    Extra native-binding fields are not trusted or copied; this is allocation
    planning, not validation of a live UE. No caller data is mutated.
    """
    if traffic_mode not in ("functional", "qos"):
        raise ValueError("Unknown traffic mode")
    limit = QOS_MAX_KBPS if traffic_mode == "qos" else 10_000
    if type(budget_kbps) is not int or not 1 <= budget_kbps <= limit:
        raise ValueError("Budget must be integer 1..10000 kbps per direction")
    if not isinstance(bindings, (list, tuple)) or len(bindings) > 500:
        raise ValueError("Bindings must contain 0..500 exact UE assignments")
    if traffic_mode == "qos" and not 1 <= len(bindings) <= 100:
        raise ValueError("QoS experiments require 1..100 exact UE assignments")
    rows, seen = [], set()
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise ValueError("Each binding must be an assignment object")
        supi, scenario, slice_name = (binding.get(k) for k in ("supi", "scenario", "slice"))
        if not isinstance(supi, str) or re.fullmatch(r"imsi-[0-9]{15}", supi) is None or supi in seen:
            raise ValueError("Each assignment requires one unique exact SUPI")
        if not isinstance(scenario, str) or scenario not in _PROFILES:
            raise ValueError("Unknown scenario")
        expected_slice, requested = _PROFILES[scenario]
        if slice_name != expected_slice:
            raise ValueError("Scenario slice assignment differs from the fixed profile")
        seen.add(supi)
        rows.append((supi, scenario, slice_name, requested))
    rows.sort(key=lambda row: (row[0], row[1]))
    if budget_kbps < len(rows):
        raise ValueError("Budget cannot provide positive traffic to every UE")
    requested = [row[3] for row in rows]
    total = sum(requested)
    if traffic_mode == "qos" and total > budget_kbps:
        raise ValueError("QoS workload exceeds generator limit; load cannot be reduced")
    capped = total > budget_kbps
    applied = _apportion(requested, budget_kbps) if capped else requested
    per_ue = tuple(UEWorkload(*row, rate) for row, rate in zip(rows, applied))
    applied_total = sum(applied)
    profile_id = QOS_PROFILE_ID if traffic_mode == "qos" else PROFILE_ID
    canonical = {"profileId": profile_id, "budgetKbpsPerDirection": budget_kbps,
                 "perUe": [asdict(row) for row in per_ue]}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return WorkloadPlan(profile_id, budget_kbps, per_ue, total, total, 2 * total,
                        applied_total, applied_total, 2 * applied_total,
                        "requested_per_direction_load_exceeds_budget" if capped else None,
                        fingerprint)


def plan_probe_workload(bindings, *, probe_kbps=100, budget_kbps=10_000):
    """Explicit uniform diagnostic load; never inferred from an application plan."""
    original = plan_workload(bindings, budget_kbps=budget_kbps)
    if (type(probe_kbps) is not int or not 1 <= probe_kbps <= 100
            or not 1 <= len(original.per_ue) <= 100 or len(original.per_ue) * probe_kbps > budget_kbps):
        raise ValueError("Diagnostic probe must fit the unchanged per-direction budget")
    rows = tuple(UEWorkload(row.supi, row.scenario, row.slice, probe_kbps, probe_kbps) for row in original.per_ue)
    total = len(rows) * probe_kbps
    canonical = {"profileId": PROBE_PROFILE_ID, "budgetKbpsPerDirection": budget_kbps,
                 "probeKbpsPerDirection": probe_kbps, "perUe": [asdict(row) for row in rows]}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return WorkloadPlan(PROBE_PROFILE_ID, budget_kbps, rows, total, total, 2 * total,
                        total, total, 2 * total, None, fingerprint)

import math

def shard_supis(rows, *, minimum_shards=1):
    if not rows or len(rows) > 500:
        raise ValueError("Shard population must contain 1..500 unique owned UEs")
    pods, seen = {}, set()
    for row in rows:
        uid, supi = row.get("podUid"), row.get("supi")
        if (not isinstance(uid, str) or not uid or not isinstance(supi, str)
                or not supi or supi in seen):
            raise ValueError("Shard population requires unique SUPIs and exact Pod ownership")
        seen.add(supi)
        pods.setdefault(uid, []).append(supi)
    # Retain the existing efficient 2x50 partition; all other layouts are valid.
    paired = len(rows) == 100 and len(pods) == 2 and all(len(p) == 50 for p in pods.values())
    if type(minimum_shards) is not int or not 1 <= minimum_shards <= min(10,len(rows)):
        raise ValueError('Invalid minimum shard count')
    count = max(minimum_shards, 5 if paired else min(10, math.ceil(len(rows) / 10)))
    buckets = [[] for _ in range(count)]
    cursor = 0
    for uid in sorted(pods):
        for offset, supi in enumerate(sorted(pods[uid])):
            buckets[(cursor + offset) % count].append(supi)
        cursor = (cursor + len(pods[uid])) % count
    return tuple(frozenset(bucket) for bucket in buckets)
