"""Shared pure Kubernetes label selection and Pod incarnation fences."""
from datetime import datetime
import re


def select_unique_pod(inventory, *, app=None, nf=None, role=None, runtime=None):
    matches = []
    for pod in inventory:
        meta = pod.get("metadata", {})
        labels = meta.get("labels", {})
        if meta.get("deletionTimestamp"):
            continue
        if all(wanted is None or labels.get(key) == wanted for key, wanted in (
                ("app.kubernetes.io/name", app), ("nf", nf),
                ("cityverse.io/role", role), ("cityverse.io/runtime", runtime))):
            matches.append(pod)
    if len(matches) != 1 or not isinstance(matches[0].get("metadata", {}).get("name"), str):
        raise ValueError("Pod inventory is ambiguous or incomplete")
    return matches[0]


def pod_incarnation(pod, *, name, container, namespace, now):
    """The existing daily native observer's exact container identity contract."""
    meta, status = pod["metadata"], pod["status"]
    rows = [c for c in status.get("containerStatuses", []) if c.get("name") == container]
    if (meta.get("name") != name or meta.get("namespace") != namespace or meta.get("deletionTimestamp")
            or status.get("phase") != "Running" or len(rows) != 1 or rows[0].get("ready") is not True):
        raise ValueError("Pod incarnation unavailable")
    c = rows[0]
    identity = {"podUid": meta.get("uid"), "containerId": c.get("containerID"), "imageId": c.get("imageID"),
                "restartCount": c.get("restartCount"), "startedAt": c.get("state", {}).get("running", {}).get("startedAt")}
    if (any(not isinstance(identity[k], str) or not identity[k] for k in ("podUid", "containerId", "imageId", "startedAt"))
            or type(identity["restartCount"]) is not int or identity["restartCount"] < 0
            or re.fullmatch(r".+@sha256:[0-9a-f]{64}", identity["imageId"]) is None):
        raise ValueError("Pod incarnation malformed")
    stamp = datetime.fromisoformat(identity["startedAt"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.timestamp() > now + 1:
        raise ValueError("Pod incarnation clock invalid")
    return identity
