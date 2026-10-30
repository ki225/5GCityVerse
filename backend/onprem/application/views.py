"""Read model projections shared by the application services."""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def epoch_millis() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def unavailable_metrics() -> dict[str, Any]:
    return {
        "upfCpuPercent": 0,
        "upfPodCount": 0,
        "amfPodCount": 0,
        "gtpPacketsPerSec": 0,
        "pduSessionCount": 0,
        "latencyMs": 0,
        "throughputMbps": 0,
        "timestamp": epoch_millis(),
        "dataSource": "unavailable",
        "evidenceLevel": "unavailable",
        "error": "Local Kubernetes/free5GC runtime is not configured",
    }

def configured_slices() -> list[dict[str, Any]]:
    return [
        {"sst": 1, "type": "eMBB", "sd": "000001", "load": 0, "sessions": 0,
         "trend": "stable", "dataSource": "unavailable", "selectionStage": "configured"},
        {"sst": 2, "type": "URLLC", "sd": "000002", "load": 0, "sessions": 0,
         "trend": "stable", "dataSource": "unavailable", "selectionStage": "configured"},
        {"sst": 3, "type": "mMTC", "sd": "000004", "load": 0, "sessions": 0,
         "trend": "stable", "dataSource": "unavailable", "selectionStage": "configured"},
        {"sst": 4, "type": "V2X", "sd": "000005", "load": 0, "sessions": 0,
         "trend": "stable", "dataSource": "unavailable", "selectionStage": "configured"},
    ]

def unavailable_free5gc_status() -> dict[str, Any]:
    metrics = unavailable_metrics()
    slices = configured_slices()
    return {
        "connected": False,
        "status": "not_configured",
        "source": "on-prem backend",
        "error": "free5GC/Kubernetes runtime is not configured",
        "subscribers": [],
        "eventSubscribers": [],
        "registeredUes": [],
        "profiles": [],
        "metrics": metrics,
        "slices": slices,
        "networkSnapshot": {
            "id": f"unavailable-{epoch_millis()}",
            "timestamp": metrics["timestamp"],
            "source": "unavailable",
            "metrics": metrics,
            "slices": slices,
            "edges": [],
            "evidenceLevel": "unavailable",
        },
        "podComponents": [],
        "checkedAt": utc_now(),
    }

def public_record(record: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(record)
    value.pop("sessionId", None)
    return value

