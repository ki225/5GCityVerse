"""Pure validation for an exact shard failure; no database or native I/O."""
import math
from backend.onprem.traffic.protocol import BATCH_ERROR_CODES


def validate_failure(record, phase, code, exits):
    phases = {"native_preflight", "receiver_prepare", "client_measurement", "native_postflight",
              "evidence_reduce", "cleanup", "final_freshness"}
    if phase not in phases or code not in BATCH_ERROR_CODES or not isinstance(exits, list) or len(exits) > 100:
        return False
    if (not isinstance(record, dict) or not isinstance(record.get('jobs'), list)
            or any(not isinstance(j, dict) or not isinstance(j.get('key'), str) for j in record['jobs'])
            or ('detachedReservation' in record and not isinstance(record['detachedReservation'], dict))):
        return False
    maximum_budget = 52 if record.get('shardIndex') is not None else 38
    keys = {j["key"] for j in record["jobs"]}
    for row in exits:
        if not isinstance(row, dict):
            return False
        if row.get("kind") == "transport":
            if (set(row) != {"kind", "role", "pod", "transportSubstage", "elapsedSeconds", "budgetSeconds"}
                    or row.get("role") not in {"server", "client", "ready", "client_ready", "release", "cancel", "unknown"}
                    or not isinstance(row.get("pod"), str) or len(row["pod"]) > 253
                    or row.get("transportSubstage") not in {"command_validate", "before_preflight_read",
                                                            "preflight_fence", "dispatch_nonzero",
                                                            "after_preflight_read", "dispatch", "after_dispatch",
                                                            "postflight_fence", "after_postflight_read", "complete"}
                    or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0
                           for k in ("elapsedSeconds", "budgetSeconds"))
                    or row["budgetSeconds"] > maximum_budget):
                return False
        elif row.get("kind") == "detached_preparation_timing":
            timing_fields = {"reservationAgeAtPreflightSeconds", "nativePreflightSeconds", "allStartsAfterSeconds", "allStatusesAfterSeconds"}
            controls = row.get("controlTimings")
            role_uids = {(r.get("role"), r.get("podUid")) for r in
                         record.get("detachedReservation", {}).get("roles", [])}
            if (set(row) != timing_fields | {"kind", "elapsedSeconds", "controlTimings"}
                    or type(row.get("elapsedSeconds")) not in (int, float)
                    or not math.isfinite(row["elapsedSeconds"]) or row["elapsedSeconds"] < 0
                    or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
                           for k, v in row.items() if k in timing_fields)
                    or not isinstance(controls, list) or len(controls) > 22):
                return False
            for control_row in controls:
                if (not isinstance(control_row, dict)
                        or set(control_row) != {"action", "role", "podUid", "requestedAfterSeconds",
                                              "queueSeconds", "dispatchSeconds", "dispatched", "accepted"}
                        or (control_row.get("role"), control_row.get("podUid")) not in role_uids
                        or control_row.get("action") not in {"start", "status"}
                        or type(control_row.get("accepted")) is not bool
                        or type(control_row.get("dispatched")) is not bool
                        or control_row["accepted"] and not control_row["dispatched"]
                        or any(type(control_row.get(k)) not in (int, float)
                               or not math.isfinite(control_row[k]) or control_row[k] < 0
                               for k in ("requestedAfterSeconds", "queueSeconds", "dispatchSeconds"))):
                    return False
        elif row.get("kind") == "release_budget":
            if (set(row) != {"kind", "reasonCode", "observerMonotonic", "childDeadline",
                             "transportDeadline", "requiredSeconds", "remainingSeconds"}
                    or row.get("reasonCode") not in {"insufficient_child_completion",
                        "no_release_dispatch_budget", "common_release_deadline_expired"}
                    or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                           for k, v in row.items() if k not in {"kind", "reasonCode"})):
                return False
        elif row.get("kind") == "detached_control":
            if (set(row) != {"kind", "role", "action", "substage", "errorCategory"}
                    or row.get("role") not in {"client", "server"}
                    or row.get("action") not in {"start", "status", "release", "collect", "cancel"}
                    or row.get("substage") not in {"preflight_pod", "exec", "postflight_pod"}
                    or row.get("errorCategory") not in {"blocked", "timeout", "nonzero"}):
                return False
        elif row.get("kind") == "worker_result":
            if (set(row) != {"kind", "role", "pod", "podUid", "jobKeys", "substage",
                             "errorCategory", "elapsedSeconds", "budgetSeconds"}
                    or row.get("role") not in {"client", "server"}
                    or row.get("substage") not in {"wait", "parse"}
                    or row.get("errorCategory") not in {"timeout", "blocked", "invalid_value", "other"}
                    or not isinstance(row.get("jobKeys"), list)
                    or any(not isinstance(k, str) for k in row["jobKeys"])
                    or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0
                           for k in ("elapsedSeconds", "budgetSeconds"))
                    or row["budgetSeconds"] > maximum_budget):
                return False
            prefix = "ue" if row["role"] == "client" else "receiver"
            expected = sorted(j["key"] for j in record["jobs"]
                              if j.get(prefix + "_pod") == row.get("pod")
                              and j.get(prefix + "_uid") == row.get("podUid"))
            if not expected or row["jobKeys"] != expected:
                return False
        elif (set(row) - {"key", "clientExit", "serverExit"}
              or row.get("key") not in keys
              or any(type(v) is not int or not -128 <= v <= 255 for k, v in row.items() if k != "key")):
            return False
    return True
