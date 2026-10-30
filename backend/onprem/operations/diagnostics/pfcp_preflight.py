"""Read-only startup-epoch PFCP association gate after UPF/SMF maintenance.

Pod readiness and an empty kernel table do not prove an SMF association. This
gate correlates accepted official exchange logs from both current Pod epochs.
It is not an end-to-end PDU or packet-effect check. Missing logs fail closed.
"""
import argparse
import gzip
import io
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
from urllib.parse import quote

UPFS = {"upf-embb": "172.22.4.21", "upf-urllc": "172.22.4.22", "upf-mmtc": "172.22.4.23", "upf-v2x": "172.22.4.24"}


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def retained_log_path(namespace, identity, container):
    parts = (identity["node"], namespace, identity["pod"], identity["uid"], container)
    if any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", part) for part in parts):
        raise ValueError("Invalid current Pod log identity")
    return ("/api/v1/nodes/" + quote(identity["node"]) + "/proxy/logs/pods/" +
            quote(namespace + "_" + identity["pod"] + "_" + identity["uid"]) + "/" + quote(container) + "/")


def retained_log_text(listing, read_file, restart_count, started_at,
                      coverage_floor=None, coverage_end=None):
    """Read one incarnation, requiring startup or an explicit continuous window."""
    prefix = str(restart_count) + ".log"
    files = re.findall(r'<a href="([^"]+)">', listing)
    selected = [name for name in files if name == prefix or name.startswith(prefix + ".")]
    if prefix not in selected or len(selected) > 10 or any(not re.fullmatch(re.escape(prefix) + r"(?:\.\d{8}-\d{6}(?:\.gz)?)?", name) for name in selected):
        raise ValueError("Incomplete or unsupported rotated container log set")
    selected.sort(key=lambda name: (name == prefix, name))
    records, first, last = [], None, None
    for name in selected:
        text = read_file(name)
        if isinstance(text, bytes):
            if name.endswith(".gz"):
                with gzip.GzipFile(fileobj=io.BytesIO(text)) as stream:
                    text = stream.read(30_000_001)
            text = text.decode("utf-8")
        if len(text) > 30_000_000:
            raise ValueError("Retained log exceeds bounded read limit")
        for line in text.splitlines():
            match = re.match(r"^(\S+) (?:stdout|stderr) ([FP]) (.*)$", line)
            if not match or match.group(2) != "F":
                raise ValueError("Incomplete CRI log record")
            at = timestamp(match.group(1))
            first = at if first is None else min(first, at)
            last = at if last is None else max(last, at)
            records.append((at, match.group(3)))
    required_floor = timestamp(coverage_floor or started_at)
    if first is None or first > required_floor + timedelta(seconds=5):
        reason = "association proof floor" if coverage_floor is not None else "current container startup"
        raise ValueError("Retained logs no longer cover " + reason)
    if coverage_floor is not None:
        if coverage_end is None:
            raise ValueError("Retained log coverage end is required")
        required_end = timestamp(coverage_end)
        if last < required_end - timedelta(seconds=5):
            raise ValueError("Retained logs do not reach association proof snapshot")
        ordered = sorted({at for at, _ in records if required_floor <= at <= required_end})
        if not ordered or any(right - left > timedelta(seconds=15)
                              for left, right in zip(ordered, ordered[1:])):
            raise ValueError("Retained logs have a gap in association proof window")
    return "\n".join(text for _, text in sorted(records, key=lambda row: row[0]))


def association_proof(smf_log, upf_log, upf_address, smf_started, upf_started):
    floor = max(timestamp(smf_started), timestamp(upf_started))
    accepted, created, invalid = [], [], []
    for text, mode in ((smf_log, "smf"), (upf_log, "upf")):
        for line in text.splitlines():
            match = re.match(r"^(\d{4}-\d\d-\d\dT[^\s\x1b]+)", line)
            if not match:
                continue
            at = timestamp(match.group(1))
            if at < floor:
                continue
            if mode == "smf" and "Received PFCP Association Setup Accepted Response from UPF[" + upf_address + "]" in line:
                accepted.append(at)
            if mode == "upf" and "[CPNodeID:172.22.4.10]" in line and "New node" in line:
                created.append(at)
            if mode == "upf" and ("not found NodeID 172.22.4.10" in line or "handleAssociationReleaseRequest" in line):
                invalid.append(at)
    if not accepted or not created:
        raise ValueError("PFCP association unproven for current Pod epochs: " + upf_address)
    observed = min(max(accepted), max(created))
    if invalid and max(invalid) >= observed:
        raise ValueError("PFCP association invalidated after accepted setup: " + upf_address)
    return {"acceptedAt": max(accepted).isoformat(), "upfCreatedAt": max(created).isoformat()}


def check(kubectl, kubeconfig, context, namespace):
    if context != "cityverse-kubeadm-lab":
        raise ValueError("This commissioned lab gate requires explicit cityverse-kubeadm-lab context")
    prefix = [kubectl, "--kubeconfig", kubeconfig, "--context", context, "--request-timeout=10s", "-n", namespace]
    def run(*args, binary=False):
        return subprocess.check_output(prefix + list(args), text=not binary,
                                       encoding=None if binary else "utf-8", timeout=20)
    def pods():
        result = {}
        for p in json.loads(run("get", "pods", "-o", "json"))["items"]:
            name = p["metadata"].get("labels", {}).get("app.kubernetes.io/name")
            if name not in {"smf", *UPFS}:
                continue
            if name in result or p["metadata"].get("deletionTimestamp"):
                raise ValueError("Ambiguous or terminating PFCP workload: " + name)
            states = [c for c in p["status"].get("containerStatuses", []) if c["name"] == name]
            if len(states) != 1 or not states[0].get("ready") or "running" not in states[0]["state"]:
                raise ValueError("PFCP container not ready: " + name)
            result[name] = {"pod": p["metadata"]["name"], "uid": p["metadata"]["uid"],
                            "node": p["spec"]["nodeName"], "restartCount": states[0]["restartCount"],
                            "startedAt": states[0]["state"]["running"]["startedAt"]}
        if set(result) != {"smf", *UPFS}:
            raise ValueError("Missing expected PFCP workloads")
        return result
    before = pods()
    snapshot_end = datetime.now(timezone.utc).isoformat()
    smf_log = run("logs", before["smf"]["pod"], "-c", "smf", "--since-time=" + before["smf"]["startedAt"])
    proofs = {}
    retained = {}
    def all_logs(name):
        if name not in retained:
            path = retained_log_path(namespace, before[name], name)
            listing = run("get", "--raw", path)
            retained[name] = retained_log_text(
                listing, lambda file: run("get", "--raw", path + file, binary=file.endswith(".gz")),
                before[name]["restartCount"], before[name]["startedAt"],
                coverage_floor=before["smf"]["startedAt"] if name != "smf" else None,
                coverage_end=snapshot_end if name != "smf" else None)
            if run("get", "--raw", path) != listing:
                raise ValueError("Log rotation occurred during association preflight")
        return retained[name]
    for name, address in UPFS.items():
        log = run("logs", before[name]["pod"], "-c", name, "--since-time=" + before[name]["startedAt"])
        try:
            proof = association_proof(smf_log, log, address, before["smf"]["startedAt"], before[name]["startedAt"])
        except ValueError as error:
            if "unproven" not in str(error):
                raise
            proof = association_proof(all_logs("smf"), all_logs(name), address,
                                      before["smf"]["startedAt"], before[name]["startedAt"])
            proof["source"] = "continuous-current-smf-epoch-retained-logs"
        proofs[name] = {**before[name], **proof}
    if pods() != before:
        raise ValueError("PFCP workload identity changed during preflight")
    return {"context": context, "smf": before["smf"], "associations": proofs, "trafficAcceptance": "not_tested"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubectl", required=True); p.add_argument("--kubeconfig", required=True)
    p.add_argument("--context", required=True); p.add_argument("--namespace", default="free5gc-onprem")
    args = p.parse_args()
    print(json.dumps(check(args.kubectl, args.kubeconfig, args.context, args.namespace), indent=2))
