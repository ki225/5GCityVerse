"""Scenario demand, slice allocation and durable round envelope contracts."""
from copy import deepcopy

from .profiles import PROFILE_SLICES
SCENARIO_SLICES = dict(PROFILE_SLICES)
EVENT_SLICES = {key:value for key,value in SCENARIO_SLICES.items() if key != 'web_browsing'}

MAX_ROUND_UES = 100

STAGES = ("preflight", "allocating", "registering", "establishing_pdu",
          "verifying_connectivity", "measuring_baseline", "applying_policy",
          "verifying_policy", "measuring_result", "results_ready", "cleaning", "completed")

class RoundConflict(ValueError):
    pass

class ScenarioRequestError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code

class RequestIdConflict(RoundConflict):
    pass

class RoundUnavailable(RoundConflict):
    """Valid input cannot currently run against the native environment."""

def validate_round(body):
    """Validate the durable envelope, including historical commissioning rounds.

    Public admission must use validate_scenario_request before this validator.
    Historical 0/500-UE journals remain readable and recoverable.
    """
    required = {"requestId", "baselineUes", "events", "strategy"}
    if not isinstance(body, dict) or not required <= set(body) or set(body) - required - {"trafficMode", "serviceRequirements", "topologyRevision", "demandGroups"}:
        raise ValueError("Provide requestId, baselineUes, events and strategy")
    if "trafficMode" in body and body["trafficMode"] not in ("functional", "qos"):
        raise ValueError("trafficMode must be functional or qos")
    request_id = body["requestId"]
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("requestId must be a nonempty string of at most 128 characters")
    events = body["events"]
    if not isinstance(events, dict) or set(events) != set(EVENT_SLICES):
        raise ValueError("Provide exactly the four non-web profile counts")
    values = [body["baselineUes"], *events.values()]
    if any(type(value) is not int or not 0 <= value <= 500 for value in values):
        raise ValueError("UE counts must be integers from 0 to 500")
    if sum(values) > 500:
        raise ValueError("Baseline plus all event UE counts must not exceed 500")
    if body.get("trafficMode") == "qos" and not 1 <= sum(values) <= MAX_ROUND_UES:
        raise ValueError("QoS experiments require 1 to 100 traffic UEs")
    if body["strategy"] not in ("none", "static", "ai"):
        raise ValueError("strategy must be none, static or ai")
    from .spatial import validate_spatial_request
    validate_spatial_request(body)
    if "serviceRequirements" in body:
        from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
        ServiceRequirementCompiler.compile(body["serviceRequirements"], body, traffic_mode=body.get("trafficMode"))
    return deepcopy(body)

def validate_scenario_request(body, *, allow_none=False):
    """Public traffic admission: exact counts, AI only, 1..100 participants."""
    # Validate types/shape before adding; bool and numeric strings are invalid.
    try:
        valid = validate_round(body)
    except ScenarioRequestError:
        raise
    except ValueError as exc:
        # Counts above the historical envelope still have the public limit code.
        if (isinstance(body, dict) and set(body) - {"trafficMode", "serviceRequirements", "topologyRevision", "demandGroups"} == {"requestId", "baselineUes", "events", "strategy"}
                and isinstance(body.get("events"), dict) and set(body["events"]) == set(EVENT_SLICES)):
            values = [body["baselineUes"], *body["events"].values()]
            if all(type(n) is int and n >= 0 for n in values) and sum(values) > MAX_ROUND_UES:
                raise ScenarioRequestError("UE_LIMIT_EXCEEDED", f"Requested total {sum(values)} exceeds limit {MAX_ROUND_UES}") from None
        raise ScenarioRequestError("INVALID_SCENARIO_ALLOCATION", str(exc)) from None
    total = valid["baselineUes"] + sum(valid["events"].values())
    if total > MAX_ROUND_UES:
        raise ScenarioRequestError("UE_LIMIT_EXCEEDED", f"Requested total {total} exceeds limit {MAX_ROUND_UES}")
    if total == 0:
        raise ScenarioRequestError("EMPTY_TRAFFIC_REQUEST", "Request 1 to 100 traffic UEs; cancel an existing round to stop it")
    from backend.onprem.scenarios.intent_bridge import require_diagnostic_strategy
    require_diagnostic_strategy(valid)
    if valid["strategy"] != "ai" and not (allow_none and valid["strategy"] == "none"):
        raise ScenarioRequestError("STRATEGY_DISABLED", "Only ai strategy is currently enabled")
    return valid

def slice_counts(allocation):
    counts = dict.fromkeys(("embb", "urllc", "mmtc", "v2x"), 0)
    counts["embb"] = allocation["baselineUes"]
    for event, count in allocation["events"].items():
        counts[EVENT_SLICES[event]] += count
    return counts

def bind_scenarios(allocation, identities):
    """Join allocated SUPIs to disjoint groups, never synthesize observed UEs."""
    queues = {name: [] for name in ("embb", "urllc", "mmtc", "v2x")}
    seen = set()
    for row in identities:
        if row["supi"] in seen or row["slice"] not in queues:
            raise ValueError("Duplicate SUPI or unknown slice")
        seen.add(row["supi"])
        queues[row["slice"]].append(deepcopy(row))
    if {key: len(value) for key, value in queues.items()} != slice_counts(allocation):
        raise ValueError("Observed allocation differs from reserved slice counts")
    if allocation.get("demandGroups"):
        for queue in queues.values():
            queue.sort(key=lambda row: row["supi"])
    groups = [("web_browsing", "embb", allocation["baselineUes"])]
    groups += [(event, slice_name, allocation["events"][event]) for event, slice_name in EVENT_SLICES.items()]
    result = []
    for scenario, slice_name, count in groups:
        rows, queues[slice_name] = queues[slice_name][:count], queues[slice_name][count:]
        result.extend({**row, "scenario": scenario} for row in rows)
    from .spatial import bind_spatial_groups
    return bind_spatial_groups(allocation, result)

def idle_snapshot():
    return {"roundId": None, "generation": None, "policyRevision": 0, "sequence": 0,
            "observedAt": None, "stage": "idle", "executionOutcome": None,
            "cleanupStatus": "not_started", "allocation": {"baselineUes": 0,
            "events": dict.fromkeys(EVENT_SLICES, 0), "total": 0},
            "counts": dict.fromkeys(("allocated", "registered", "pduEstablished", "trafficVerified")),
            "perUe": [], "fleetLease": None, "actions": [], "measurements": [], "errors": [],
            "capabilities": {"start": False, "staticPolicy": False, "aiPolicy": False,
                             "reason": "Native scenario capability verification is incomplete"}}
