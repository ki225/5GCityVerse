"""Pure optional polling projection; full ledger/GET remains authoritative.

This is a display view, never input to native, policy, recovery or admission
verification. It copies known fields without renewing timestamps and derives
a historical delivery verdict from raw reports. Nested raw reports, execution tokens and unknown fields are not
forwarded. No I/O, state writes or implicit endpoint wiring. Typed session
fences use current time only to expire evidence; source clocks are never renewed.
"""
from collections.abc import Mapping
import math
import re
from backend.onprem.capacity.verdict import capacity_verdict
from backend.onprem.runtime.errors import REQUEST_BLOCKERS, RequestGuardDiagnostic
from backend.onprem.runtime.current_sessions import IDENTITY_FIELDS as SESSION_FIELDS


VIEW_SCHEMA = "cityverse.round-summary/v1"
IDENTITY = ("scenario", "supi", "slice", "sessionId", "podUid", "ueIp", "ip", "tun", "interface", "upf",
            "roundId", "generation", "policyRevision", "smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch",
            "qfi", "uplinkTeid", "downlinkTeid")
IDENTITY = tuple(dict.fromkeys((*IDENTITY, *SESSION_FIELDS)))
WINDOW = ("direction", "receiverPodUid", "endpoint", "port", "durationSeconds", "offeredTargetMbps",
          "startedAt", "finishedAt", "receiverStartedAt", "receiverFinishedAt", "observedAt",
          "receivedMbps", "offeredObservedMbps", "receivedBytes", "packets", "lostPackets", "source",
          "clientExit", "serverExit", "qfi")
WORKLOAD = ("schemaVersion", "profileId", "budgetKbpsPerDirection", "serviceContractRevision", "requestedUplinkKbps",
            "requestedDownlinkKbps", "requestedCombinedKbps", "appliedUplinkKbps", "appliedDownlinkKbps",
            "appliedCombinedKbps", "capReason", "comparisonFingerprint", "qosEffectClaimed", "trafficMode")


def _scalar(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _object(value, fields, children=None):
    if not isinstance(value, Mapping):
        return None
    result = {name: _scalar(value[name]) for name in fields if name in value}
    for name, project in (children or {}).items():
        if name in value:
            result[name] = project(value[name])
    return result


def _list(value, project):
    return [project(row) for row in value] if isinstance(value, (list, tuple)) else None


def _identity(value):
    return _object(value, (*IDENTITY, 'failureCode'), {'nas': lambda row: _object(row,
        ('source', 'supi', 'generation', 'podUid', 'observedAt', 'rmState', 'mmState', 'updateState'))})


def _window(value):
    return _object(value, WINDOW, {"binding": _identity})


def _workload(value, *, typed=False):
    result = _object(value, WORKLOAD, {"perUe": lambda rows: _list(rows, lambda row:
        _object(row, ("supi", "scenario", "slice", "requestedKbps", "appliedKbps",
                      "requestedUplinkKbps", "requestedDownlinkKbps", "appliedUplinkKbps", "appliedDownlinkKbps")))})
    if isinstance(value, Mapping) and value.get("profileId") == "controlled_qer_probe_v1":
        if (value.get("workloadKind") != "diagnostic_probe"
                or type(value.get("probeKbpsPerDirection")) is not int
                or not 1 <= value["probeKbpsPerDirection"] <= 100):
            return None
        result.update(workloadKind="diagnostic_probe", probeKbpsPerDirection=value["probeKbpsPerDirection"])
    if (typed and isinstance(value, Mapping)
            and value.get("schemaVersion") == "cityverse.round-workload/v1"
            and value.get("profileId") == "scenario-service-offered-load-v1"
            and value.get("trafficMode") == "qos" and value.get("qosEffectClaimed") is False):
        # A V2 device summary is not a PDU owner. Publish the existing bounded
        # declaration-only summary; native binding/receiver proof stays separate.
        result.pop("perUe", None)
        result.pop("serviceContractRevision", None)
        result.update(schemaVersion="cityverse.session-workload/v2",
                      profileId="session-service-offered-load-v2")
    return result


def _counts(value):
    return _object(value, ("allocated", "registered", "pduEstablished", "trafficVerified", "requested", "failed",
                           "embb", "urllc", "mmtc", "v2x"))


def _sla(value):
    if isinstance(value, Mapping) and value.get("schemaVersion") == "cityverse.service-sla/v1":
        return deepcopy(value)
    return _object(value, ("status", "reason", "scope", "historical", "windowId"), {
        "scenarios": lambda rows: _list(rows, lambda row:
            _object(row, ("scenario", "sampleId", "status", "reason")))})


def _display_sla(snapshot):
    """Read-only compatibility for completed v1 journals; never control input."""
    from backend.onprem.scenarios.contracts import ScenarioRequestError
    from backend.onprem.scenarios.objectives import round_sla_assessment
    try:
        return round_sla_assessment(snapshot)
    except ScenarioRequestError as error:
        raw = snapshot.get("serviceRequirements")
        if not (snapshot.get("stage") == "completed" and isinstance(raw, Mapping)
                and raw.get("schemaVersion") == "cityverse.service-requirements/v1"
                and error.code == "INVALID_SERVICE_REQUIREMENTS"
                and str(error) == "Invalid service requirement schema or services"):
            raise
        return {"status": "unknown", "reason": "unsupported_historical_service_requirements",
                "scope": "historical-journal", "historical": True,
                "sourceSchemaVersion": raw["schemaVersion"], "supported": False,
                "scenarios": []}


def _action(value, *, typed_workload=False):
    result = _object(value, ("id", "kind", "operation", "roundId", "generation", "policyRevision",
        "sourcePolicyRevision", "sourceSequence", "supi", "profileId", "strategy", "requested", "accepted",
        "installed", "effectVerified", "requestedAt", "finishedAt", "observedAt", "stage", "mode",
        "cleanupVerified", "cleanupState", "reconciliationRequired", "phase", "code", "errorType", "reason", "trafficActionId", "qfi"), {
        "counts": _counts, "workload": lambda row: _workload(row, typed=typed_workload),
        "reports": lambda rows: _list(rows, _window),
        "workerExits": lambda rows: _list(rows, lambda row: _object(row, ("key", "clientExit", "serverExit"))),
    })
    if isinstance(value, Mapping):
        if isinstance(value.get('nativeRequestBlocker'), str) and value['nativeRequestBlocker'] in REQUEST_BLOCKERS:
            result['nativeRequestBlocker'] = value['nativeRequestBlocker']
        diagnostic = RequestGuardDiagnostic.project(value.get('nativeRequestDiagnostic'))
        if diagnostic:
            result['nativeRequestDiagnostic'] = diagnostic
    return result


def _measurement(value):
    return _object(value, (*IDENTITY, "source", "observedAt", "receivedUplinkMbps", "receivedDownlinkMbps", "rttMs"),
                   {"windows": lambda rows: _list(rows, _window)})


def _source(value):
    return (_object(value, ("name", "source", "available", "observedAt", "epoch"))
            if isinstance(value, Mapping) else _scalar(value))


def project_agent_runtime(value):
    """Keep the existing public progress vocabulary while rich events stay private.

    This display projection never modifies the runtime or native evidence.
    Optional nulls and new event metadata must not break strict v1 clients.
    """
    if not isinstance(value, Mapping):
        return None
    statuses = {"started": "started", "completed": "completed", "failed": "failed",
        "entered": "started", "waiting": "started", "running": "started",
        "finished": "completed", "received": "completed", "proposed": "completed",
        "installed": "completed", "restored": "completed", "effect_verified": "completed",
        "no_op": "completed", "no_effect": "completed", "regressed": "completed", "no_holdout": "completed"}
    result = _object(value, ("roundId", "generation", "phase", "updatedAt"))
    if (not isinstance(result.get("roundId"), str) or not result["roundId"]
            or type(result.get("generation")) is not int or result["generation"] < 0
            or result.get("phase") not in {"plan", "precheck", "apply", "postcheck", "restore", "restoration", "observe", "learn", "cleanup", "completed", "failed"}
            or type(result.get("updatedAt")) not in (int, float) or result["updatedAt"] <= 0
            or not isinstance(value.get("status"), str) or value["status"] not in statuses):
        return None
    result["status"] = statuses[value["status"]]
    if isinstance(value.get("role"), str) and value["role"] in {"planner", "executor", "validator"}:
        result["role"] = value["role"]
    if type(value.get("cycle")) is int and 0 <= value["cycle"] <= 10000:
        result["cycle"] = value["cycle"]
    for key in ("tool", "model", "response_id", "code"):
        if isinstance(value.get(key), str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value[key]):
            result[key] = value[key]
    return result


def project_capabilities(value):
    from .scoped_capacity_presentation import project_scoped_traffic_profiles
    return _object(value, ("start", "staticPolicy", "aiPolicy", "reason", "applicationMaxUes", "verifiedMaxUes", "verifiedMaxUesScope",
        "candidateMaxUes", "admissionMaxUes", "commissioningEnabled", "commissioningProfile", "commissioningLabel", "capacityStatus",
        "formalAiEndpoint", "formalAiRequiresContract", "legacyAiDiagnostic", "legacyAiDiagnosticLabel"),
        {"scopedVerifiedTrafficProfiles": project_scoped_traffic_profiles})


def project_round_summary(snapshot):
    """Copy a bounded-schema display view; never repair missing observations.

    Absent keys remain absent; null stays null. Malformed nested/scalar values
    project to null rather than fabricated zero/empty evidence. Callers must
    opt in explicitly and keep the full evidence endpoint/ledger unchanged.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError("Round summary requires a snapshot object")
    result = _object(snapshot, ("roundId", "requestId", "generation", "policyRevision", "sequence", "observedAt",
        "stage", "executionOutcome", "cleanupStatus", "strategy", "trafficMode", "populationRevision"), {
        "populationPending": lambda value: _object(value, ('operationId', 'baseRevision')),
        "allocation": lambda value: _object(value, ("baselineUes", "total"), {
            "events": lambda events: _object(events, ("video_streaming", "realtime_control", "connected_traffic", "factory_iot"))}),
        "spatialDemand": lambda value: deepcopy(value) if isinstance(value, Mapping) else None,
        "requestedSliceUes": lambda value: _object(value, ("embb", "urllc", "v2x", "mmtc")),
        "trafficCounts": lambda value: _object(value, ("scope", "requested", "selected", "registered",
            "pduEstablished", "connectivityVerified", "observedTransmitting", "measurementScope", "observedAt")),
        "counts": _counts,
        "agentRuntime": project_agent_runtime,
        "lifecycle": lambda value: deepcopy(value) if isinstance(value, Mapping) else None,
        "capabilities": project_capabilities,
        "perUe": lambda rows: _list(rows, _identity),
        "measurements": lambda rows: _list(rows, _measurement),
        "actions": lambda rows: _list(rows, lambda row: _action(row,
            typed_workload=snapshot.get("schemaVersion") == "cityverse.current-state/v2")),
        "errors": lambda rows: _list(rows, _scalar),
        "workload": _workload,
        "serviceRequirements": lambda value: deepcopy(value) if isinstance(value, Mapping) else None,
        "resolvedRequirements": lambda value: deepcopy(value) if isinstance(value, Mapping) else None,
        "connectivityEvidence": lambda value: _object(value, ("source", "roundId", "generation", "stableSince",
            "observedAt", "verifiedAt"), {"identities": lambda rows: _list(rows, _identity),
                "evidenceSources": lambda rows: _list(rows, _source)}),
        "releaseProof": lambda value: _object(value, ("trafficStopped", "policiesAbsent", "rulesRestored",
            "uesReleased", "podsAbsent", "observedAt", "scope", "source", "roundId", "generation", "globalSmfInventoryComplete",
            "ueLeaseReleased", "fleetPreserved")),
    })
    result["viewSchema"] = VIEW_SCHEMA
    if snapshot.get('schemaVersion') == 'cityverse.current-state/v2' or 'nativeSessionBindings' in snapshot:
        from .native_session_presentation import project_native_session_bindings
        result['nativeSessionBindings'] = project_native_session_bindings(snapshot)
    if result.get("agentRuntime") is None:
        result.pop("agentRuntime", None)
    if "stage" in snapshot:
        result["performance"] = capacity_verdict(snapshot)
        from backend.onprem.scenarios.objectives import round_sla_assessment
        result["sla"] = (_sla(snapshot["sla"]) if snapshot.get("viewSchema") == VIEW_SCHEMA
                         and isinstance(snapshot.get("sla"), Mapping) else _display_sla(snapshot))
    return result

from copy import deepcopy

import math

def with_traffic_counts(snapshot, *, copy_snapshot=True):
    """Decorate counts without mutating the input.

    Service responses already own their freshly loaded nested evidence. They
    can retain that tree rather than copying every raw worker report again.
    Other callers keep the original independent deep-copy behavior by default.
    """
    result = deepcopy(snapshot) if copy_snapshot else dict(snapshot)
    if snapshot.get("viewSchema") != VIEW_SCHEMA:
        from backend.onprem.scenarios.objectives import round_sla_assessment
        result["sla"] = _display_sla(snapshot)
    owners = result.get("perUe", [])
    counts = result.get("counts", {})
    measurements = result.get("measurements", [])
    indexed = {row.get("supi"): row for row in owners}
    transmitting = None
    if (owners and len(indexed) == len(owners) == len(measurements)
            and {row.get("supi") for row in measurements} == set(indexed)
            and all(row.get("source") == "receiver" and row.get("roundId") == result.get("roundId")
                    and row.get("generation") == result.get("generation")
                    and row.get("sessionId") == indexed[row["supi"]].get("sessionId")
                    and all(type(row.get(key)) in (int, float) and math.isfinite(row[key]) and row[key] >= 0
                            for key in ("receivedUplinkMbps", "receivedDownlinkMbps")) for row in measurements)):
        transmitting = sum(any(row[key] > 0 for key in ("receivedUplinkMbps", "receivedDownlinkMbps"))
                           for row in measurements)
    result["trafficCounts"] = {
        "scope": "selected-round-ues", "requested": result["allocation"]["total"],
        "selected": len(owners), "registered": counts.get("registered"),
        "pduEstablished": counts.get("pduEstablished"), "connectivityVerified": counts.get("trafficVerified"),
        "observedTransmitting": transmitting,
        "measurementScope": "last-receiver-window" if transmitting is not None else "unavailable",
        "observedAt": result.get("observedAt") if transmitting is not None else None,
    }
    return result
