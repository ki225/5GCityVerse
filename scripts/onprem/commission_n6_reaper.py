"""Guarded N6 traffic reaper commissioning helper.

Default mode is read-only planning. Live commissioning requires --execute.
The only intended live mutations are the traffic-container command on the four
N6 receiver Deployments and n6-embb replicas 0->1.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import posixpath
import subprocess
import sys
import time

DEPLOYMENTS = ("n6-embb", "n6-urllc", "n6-mmtc", "n6-v2x")
SLICES = ("embb", "urllc", "mmtc", "v2x")
ZERO_REPLICA_BASELINE = {"n6-embb": 0, "n6-urllc": 1, "n6-mmtc": 1, "n6-v2x": 1}
TRAFFIC_IMAGE = "networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f"
REAPER_COMMAND = ["bash", "-c", "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"]
OLD_COMMANDS = (["/bin/sh", "-ec", "exec sleep infinity"], ["sleep", "infinity"])
UE_LABEL = "cityverse.io/managed-by=onprem-ue-controller"
ROOT = Path(__file__).resolve().parents[2]
KUBECTL_EXE = ROOT / ".cache/kubeadm-lab/bin/kubectl.exe"
KUBECONFIG = ROOT / ".cache/kubeadm-lab/kubeconfig"
CONTEXT = "cityverse-kubeadm-lab"
NAMESPACE = "free5gc-onprem"
DEFAULT_OUTPUT = ROOT / ".cache/kubeadm-lab/n6-reaper-commission-02"


class Blocked(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def kubectl_argv(args: list[str]) -> list[str]:
    return [
        str(KUBECTL_EXE),
        "--kubeconfig",
        str(KUBECONFIG),
        "--context",
        CONTEXT,
        "--request-timeout=15s",
        "-n",
        NAMESPACE,
        *args,
    ]


def run_kubectl(args: list[str], *, stdin=None, timeout=60) -> str:
    result = subprocess.run(kubectl_argv(args), input=stdin, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise Blocked(f"kubectl failed: {' '.join(args[:5])}; stderr={result.stderr.strip()[:300]}")
    return result.stdout


def kjson(args: list[str], *, stdin=None, timeout=60):
    return json.loads(run_kubectl(args, stdin=stdin, timeout=timeout))


def traffic_index(deploy: dict) -> int:
    containers = deploy["spec"]["template"]["spec"]["containers"]
    matches = [i for i, c in enumerate(containers) if c.get("name") == "traffic"]
    if len(matches) != 1:
        raise Blocked(f"{deploy['metadata']['name']} must have exactly one traffic container")
    return matches[0]


def command_patch_ops(deploy: dict) -> list[dict]:
    idx = traffic_index(deploy)
    container = deploy["spec"]["template"]["spec"]["containers"][idx]
    old = container.get("command")
    if old == REAPER_COMMAND:
        return []
    if old not in OLD_COMMANDS:
        raise Blocked(f"{deploy['metadata']['name']} traffic command is not an accepted old command: {old!r}")
    base = f"/spec/template/spec/containers/{idx}"
    return [
        {"op": "test", "path": "/metadata/uid", "value": deploy["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": deploy["metadata"]["resourceVersion"]},
        {"op": "test", "path": f"{base}/name", "value": "traffic"},
        {"op": "test", "path": f"{base}/image", "value": TRAFFIC_IMAGE},
        {"op": "test", "path": f"{base}/command", "value": old},
        {"op": "replace", "path": f"{base}/command", "value": REAPER_COMMAND},
    ]


def scale_patch_ops(deploy: dict, replicas: int) -> list[dict]:
    old = deploy.get("spec", {}).get("replicas")
    if old != 0:
        raise Blocked(f"{deploy['metadata']['name']} scale requires exact old replicas 0, saw {old!r}")
    return [
        {"op": "test", "path": "/metadata/uid", "value": deploy["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": deploy["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": 0},
        {"op": "replace", "path": "/spec/replicas", "value": replicas},
    ]


def normalized_deploy_for_compare(deploy: dict) -> dict:
    result = copy.deepcopy(deploy)
    meta = result.get("metadata", {})
    for key in ("managedFields", "resourceVersion", "generation", "creationTimestamp"):
        meta.pop(key, None)
    tmpl_meta = result["spec"]["template"].setdefault("metadata", {})
    tmpl_meta.pop("creationTimestamp", None)
    result.pop("status", None)
    return result


def command_delta_only(before: dict, after: dict) -> bool:
    expected = normalized_deploy_for_compare(before)
    observed = normalized_deploy_for_compare(after)
    idx = traffic_index(expected)
    expected["spec"]["template"]["spec"]["containers"][idx]["command"] = REAPER_COMMAND
    return expected == observed


def scale_delta_only(before: dict, after: dict, replicas: int) -> bool:
    expected = normalized_deploy_for_compare(before)
    observed = normalized_deploy_for_compare(after)
    expected["spec"]["replicas"] = replicas
    return expected == observed


def security_fingerprint(deploy: dict) -> dict:
    idx = traffic_index(deploy)
    c = deploy["spec"]["template"]["spec"]["containers"][idx]
    return {
        "securityContext": c.get("securityContext"),
        "resources": c.get("resources"),
        "volumeMounts": c.get("volumeMounts"),
        "readinessProbe": c.get("readinessProbe"),
        "image": c.get("image"),
    }


def desired_replicas(deploy: dict) -> int:
    value = deploy.get("spec", {}).get("replicas")
    if type(value) is not int or value < 0:
        raise Blocked(f"{deploy['metadata']['name']} has invalid explicit replicas {value!r}")
    return value


def ready_replicas(deploy: dict) -> int:
    return int(deploy.get("status", {}).get("readyReplicas") or 0)


def available_replicas(deploy: dict) -> int:
    return int(deploy.get("status", {}).get("availableReplicas") or 0)


def pod_uids(pods: dict, deployment: str) -> set[str]:
    prefix = deployment + "-"
    return {
        p["metadata"]["uid"]
        for p in pods.get("items", [])
        if p["metadata"]["name"].startswith(prefix) and not p["metadata"].get("deletionTimestamp")
    }


def callback_pod(pods: dict) -> str:
    candidates = []
    for pod in pods.get("items", []):
        meta, status = pod.get("metadata", {}), pod.get("status", {})
        if not meta.get("name", "").startswith("cityverse-af-callback-") or meta.get("deletionTimestamp"):
            continue
        statuses = status.get("containerStatuses", [])
        if status.get("phase") == "Running" and all(c.get("ready") for c in statuses):
            candidates.append(meta["name"])
    if len(candidates) != 1:
        raise Blocked(f"expected one ready callback pod, saw {candidates}")
    return candidates[0]


ZERO_PROBE = r'''
import json,time,urllib.request,urllib.error
def read(url):
    try:
        with urllib.request.urlopen(url,timeout=5) as response:
            return {"status":response.status,"body":json.loads(response.read() or b"null"),"collectorNow":time.time()}
    except urllib.error.HTTPError as error:
        return {"status":error.code,"body":json.loads(error.read() or b"null"),"collectorNow":time.time()}
print(json.dumps({
 "amf":read("http://cityverse-free5gc-amf-service:8080/namf-oam/v1/registered-ue-context"),
 "upfs":{name:read("http://upf-"+name+"-evidence:9081/v1/upf-state") for name in ("embb","urllc","mmtc","v2x")}}))
'''


def seconds(value: str) -> float:
    if not isinstance(value, str) or not value:
        raise Blocked("timestamp missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def verify_zero_baseline(out: Path, prefix: str) -> dict:
    pods = kjson(["get", "pods", "-o", "json"])
    write_json(out / f"{prefix}-pods.json", pods)
    ue = [
        p["metadata"]["name"]
        for p in pods.get("items", [])
        if p.get("metadata", {}).get("labels", {}).get("cityverse.io/managed-by") == "onprem-ue-controller"
        or p.get("metadata", {}).get("name", "").startswith("cv-ue-")
    ]
    if ue:
        raise Blocked(f"managed UE pods are present: {ue[:5]}")
    cb = callback_pod(pods)
    obs = json.loads(run_kubectl(["exec", cb, "-c", "callback", "--", "python", "-c", ZERO_PROBE], timeout=30))
    write_json(out / f"{prefix}-zero-observations.json", obs)
    amf = obs.get("amf", {})
    if amf.get("status") != 200 or amf.get("body") not in (None, []):
        raise Blocked("AMF OAM registered UE context is not HTTP 200 empty")
    for name in SLICES:
        item = obs.get("upfs", {}).get(name, {})
        body = item.get("body")
        if item.get("status") != 200 or not isinstance(body, dict):
            raise Blocked(f"UPF {name} evidence is not HTTP 200 JSON")
        if body.get("available") is not True or body.get("errors") != []:
            raise Blocked(f"UPF {name} unavailable or has errors")
        if body.get("source") != "gtp5g-netlink" or body.get("upf") != "upf-" + name:
            raise Blocked(f"UPF {name} source/name mismatch")
        for key in ("sessions", "residuals", "pdrs", "qers"):
            if body.get(key) != []:
                raise Blocked(f"UPF {name} {key} not empty")
        age = float(item["collectorNow"]) - seconds(body.get("observedAt"))
        if not -1 <= age <= 15:
            raise Blocked(f"UPF {name} evidence is stale: age={age:.3f}")
    return {"callbackPod": cb, "zeroUE": True, "amfEmpty": True, "upfsEmptyFresh": True}


def capture_snapshot(out: Path, prefix: str) -> tuple[dict[str, dict], dict, dict]:
    deployments = {}
    for name in DEPLOYMENTS:
        deployments[name] = kjson(["get", "deployment", name, "-o", "json"])
        write_json(out / f"{prefix}-deployment-{name}.json", deployments[name])
    resources = {}
    for kind in ("deployments", "statefulsets", "services", "endpointslices", "configmaps", "networkpolicies"):
        resources[kind] = kjson(["get", kind, "-o", "json"])
        write_json(out / f"{prefix}-{kind}.json", resources[kind])
    pods = kjson(["get", "pods", "-o", "json"])
    write_json(out / f"{prefix}-pods-all.json", pods)
    return deployments, pods, resources


def canonical_resource(item: dict) -> dict:
    value = copy.deepcopy(item)
    meta = value.get("metadata", {})
    for key in ("managedFields", "resourceVersion", "generation", "creationTimestamp"):
        meta.pop(key, None)
    value.pop("status", None)
    return value


def resource_identity(item: dict) -> str:
    return item.get("metadata", {}).get("name", "")


def assert_other_resources_unchanged(before: dict, after: dict) -> None:
    for kind, old_list in before.items():
        new_by_name = {resource_identity(i): i for i in after.get(kind, {}).get("items", [])}
        for old in old_list.get("items", []):
            name = resource_identity(old)
            if kind == "deployments" and name in DEPLOYMENTS:
                continue
            if canonical_resource(old) != canonical_resource(new_by_name.get(name, {})):
                raise Blocked(f"unexpected {kind} change: {name}")


def assert_unaffected_pods_unchanged(before: dict, after: dict) -> None:
    def relevant(pods: dict) -> dict[str, str]:
        result = {}
        for pod in pods.get("items", []):
            name = pod["metadata"]["name"]
            if name.startswith(tuple(d + "-" for d in DEPLOYMENTS)):
                continue
            result[name] = pod["metadata"]["uid"]
        return result
    if relevant(before) != relevant(after):
        raise Blocked("non-N6 pod UID set changed")


def assert_n6_replica_baseline(deployments: dict[str, dict]) -> None:
    observed = {name: desired_replicas(dep) for name, dep in deployments.items()}
    if observed != ZERO_REPLICA_BASELINE:
        raise Blocked(f"N6 replica baseline mismatch: {observed}")


def wait_ready_new_pod(name: str, old_uids: set[str], expected_ready: int, out: Path) -> dict:
    deadline = time.monotonic() + 180
    last = {}
    while time.monotonic() < deadline:
        deploy = kjson(["get", "deployment", name, "-o", "json"])
        pods = kjson(["get", "pods", "-o", "json"])
        current = pod_uids(pods, name)
        old_gone = not (old_uids & current)
        ready = ready_replicas(deploy)
        available = available_replicas(deploy)
        desired = desired_replicas(deploy)
        if desired == expected_ready and ready == expected_ready and available == expected_ready and old_gone and current - old_uids:
            proof = {"deployment": deploy, "pods": pods}
            write_json(out / f"ready-{name}.json", proof)
            return {"readyReplicas": ready, "availableReplicas": available, "podUids": sorted(current), "oldGone": old_gone}
        last = {"desired": desired, "readyReplicas": ready, "availableReplicas": available, "podUids": sorted(current), "oldGone": old_gone}
        time.sleep(3)
    write_json(out / f"ready-timeout-{name}.json", last)
    raise Blocked(f"{name} did not reach expected ready/new-pod state")


def assert_final_n6_ready(out: Path) -> dict:
    result = {}
    for name in DEPLOYMENTS:
        dep = kjson(["get", "deployment", name, "-o", "json"])
        write_json(out / f"final-ready-{name}.json", dep)
        desired = desired_replicas(dep)
        if desired != 1 or ready_replicas(dep) != 1 or available_replicas(dep) != 1:
            raise Blocked(f"{name} final ready mismatch")
        result[name] = {"replicas": desired, "readyReplicas": ready_replicas(dep), "availableReplicas": available_replicas(dep)}
    return result


def assert_bash_pid1(pod: str, out: Path) -> dict:
    probe = r'''printf 'exe=%s\n' "$(readlink /proc/1/exe)"
printf 'cmd=%s\n' "$(tr '\0' ' ' </proc/1/cmdline)"
printf 'comm=%s\n' "$(awk '{print $2}' /proc/1/stat)"
printf 'start=%s\n' "$(awk '{print $22}' /proc/1/stat)"
'''
    stdout = run_kubectl(["exec", pod, "-c", "traffic", "--", "bash", "-lc", probe], timeout=30)
    (out / "bash-pid1-verify.txt").write_text(stdout, encoding="utf-8")
    values = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
    exe = posixpath.basename(values.get("exe", ""))
    if exe != "bash" or not values.get("cmd", "").startswith("bash -c ") or values.get("comm") != "(bash)" or not values.get("start", "").isdigit():
        raise Blocked("traffic PID 1 is not the expected Bash reaper")
    return values


FIXTURE = r'''
set -euo pipefail
case_name="${1:?case}"
owner="cvreaper-$case_name-$$"
dir="/tmp/$owner"
pids="$dir/pids.tsv"
mkdir "$dir"
cleanup() { rm -rf "$dir"; }
trap cleanup EXIT
stat_field() { local line; local -a fields; { IFS= read -r line <"/proc/$1/stat"; } 2>/dev/null || return 1; read -r -a fields <<< "${line##*) }"; printf '%s\n' "${fields[$2-3]}"; }
alive_same() { local pid="$1" start="$2"; [ -r "/proc/$pid/stat" ] && [ "$(stat_field "$pid" 22)" = "$start" ]; }
zombies_total() { local c=0 s st; for s in /proc/[0-9]*/stat; do s="${s#/proc/}"; s="${s%/stat}"; st=$(stat_field "$s" 3 || true); [ "$st" = Z ] && c=$((c+1)); done; echo "$c"; }
live_count() { local c=0 label pid start; while IFS=$'\t' read -r label pid start; do alive_same "$pid" "$start" && c=$((c+1)); done <"$pids"; echo "$c"; }
tracked_pid() { local needle="$1" tracked_label tracked_pid tracked_start; while IFS=$'\t' read -r tracked_label tracked_pid tracked_start; do [ "$tracked_pid" = "$needle" ] && alive_same "$tracked_pid" "$tracked_start" && return 0; done <"$pids"; return 1; }
untracked_child_count() { local parent="$1" c=0 child; local -a children; read -r -a children <"/proc/$parent/task/$parent/children" || true; for child in "${children[@]}"; do tracked_pid "$child" || c=$((c+1)); done; echo "$c"; }
wait_live_exact_50() { deadline=$((SECONDS+8)); while [ "$SECONDS" -lt "$deadline" ]; do [ -f "$dir/spawned" ] && [ -f "$pids" ] && [ "$(wc -l <"$pids")" -eq 50 ] && [ "$(live_count)" -eq 50 ] && return 0; sleep 0.05; done; return 1; }
wait_untracked_zero() { deadline=$((SECONDS+3)); while [ "$SECONDS" -lt "$deadline" ]; do [ "$(untracked_child_count "$supervisor")" = 0 ] && return 0; sleep 0.05; done; return 1; }
wait_children_gone() { deadline=$((SECONDS+10)); while [ "$SECONDS" -lt "$deadline" ]; do [ "$(live_count)" -eq 0 ] && return 0; sleep 0.1; done; return 1; }
supervisor_body='set -euo pipefail
dir="$1"; pids="$dir/pids.tsv"; : >"$pids"
for i in $(seq 1 25); do
  for direction in uplink downlink; do
    sleep 8 &
    pid="$!"
    read -r -a stat_fields <"/proc/$pid/stat"
    start="${stat_fields[21]}"
    printf "ue-%02d:%s\t%s\t%s\n" "$i" "$direction" "$pid" "$start" >>"$pids"
  done
done
: >"$dir/spawned"
wait'
echo "owner=$owner"
echo "pid1_exe=$(readlink /proc/1/exe)"
echo "pid1_comm=$(awk '{print $2}' /proc/1/stat)"
echo "zombies_before=$(zombies_total)"
[ "$(zombies_total)" = 0 ]
(bash -lc "$supervisor_body" supervisor "$dir" "$case_name") &
supervisor="$!"
supervisor_start="$(stat_field "$supervisor" 22)"
wait_live_exact_50
wait_untracked_zero
echo "supervisor=$supervisor start=$supervisor_start"
echo "live_before_parent_death=$(live_count)"
echo "untracked_children_before_parent_death=$(untracked_child_count "$supervisor")"
[ "$(live_count)" = 50 ]
[ "$(untracked_child_count "$supervisor")" = 0 ]
if [ "$case_name" = normal ]; then
  wait "$supervisor"
  supervisor_rc="$?"
  [ "$supervisor_rc" = 0 ]
else
  kill -KILL "$supervisor"
  set +e
  wait "$supervisor"
  supervisor_rc="$?"
  set -e
  echo "supervisor_signal_rc=$supervisor_rc"
  [ "$supervisor_rc" = 137 ]
  [ ! -d "/proc/$supervisor" ] || [ "$(stat_field "$supervisor" 22 2>/dev/null || true)" != "$supervisor_start" ]
  adopted=0
  while IFS="$(printf '\t')" read -r label pid start; do
    alive_same "$pid" "$start"
    [ "$(stat_field "$pid" 4)" = 1 ]
    adopted=$((adopted+1))
  done <"$pids"
  echo "adopted_to_pid1=$adopted"
  [ "$adopted" = 50 ]
fi
wait_children_gone
echo "leftovers=$(live_count)"
echo "zombies_after=$(zombies_total)"
[ "$(live_count)" = 0 ]
[ "$(zombies_total)" = 0 ]
'''


def traffic_pod(out: Path) -> tuple[str, str]:
    pods = kjson(["get", "pods", "-o", "json"])
    write_json(out / "fixture-pod-select-pods.json", pods)
    candidates = [
        p
        for p in pods["items"]
        if p["metadata"]["name"].startswith("n6-embb-")
        and not p["metadata"].get("deletionTimestamp")
        and p.get("status", {}).get("phase") == "Running"
    ]
    if len(candidates) != 1:
        raise Blocked("expected exactly one running n6-embb pod for fixture")
    return candidates[0]["metadata"]["name"], candidates[0]["metadata"]["uid"]


def run_fixture(out: Path) -> dict:
    pod, uid = traffic_pod(out)
    pid1 = assert_bash_pid1(pod, out)
    results = {}
    for case in ("normal", "supervisorloss"):
        stdout = run_kubectl(["exec", pod, "-c", "traffic", "--", "env", f"POD_UID={uid}", "bash", "-lc", FIXTURE, "fixture", case], timeout=60)
        (out / f"fixture-{case}.txt").write_text(stdout, encoding="utf-8")
        required = ["live_before_parent_death=50", "untracked_children_before_parent_death=0", "leftovers=0", "zombies_after=0"]
        if case == "supervisorloss":
            required.extend(["supervisor_signal_rc=137", "adopted_to_pid1=50"])
        if any(token not in stdout for token in required):
            raise Blocked(f"fixture {case} did not prove exact reaping")
        pod_after, uid_after = traffic_pod(out)
        if pod_after != pod or uid_after != uid:
            raise Blocked("fixture pod UID changed during process test")
        results[case] = {"pod": pod, "podUid": uid, "stdoutFile": f"fixture-{case}.txt"}
    return {"pid1": pid1, "cases": results}


def patch_command(name: str, before: dict, old_uids: set[str], old_security: dict, out: Path, *, execute: bool) -> dict:
    ops = command_patch_ops(before)
    if not ops:
        return {"deployment": name, "commandChanged": False}
    dry = kjson(["patch", "deployment", name, "--type=json", "--dry-run=server", "-o", "json", "-p", json.dumps(ops)])
    write_json(out / f"dryrun-command-{name}.json", dry)
    if not command_delta_only(before, dry) or security_fingerprint(dry) != old_security:
        raise Blocked(f"{name} command dry-run was not command-only")
    result = {"deployment": name, "commandChanged": True, "dryRunResourceVersion": dry["metadata"].get("resourceVersion")}
    if not execute:
        return result
    patched = kjson(["patch", "deployment", name, "--type=json", "-o", "json", "-p", json.dumps(ops)])
    write_json(out / f"patched-command-{name}.json", patched)
    if not command_delta_only(before, patched) or security_fingerprint(patched) != old_security:
        raise Blocked(f"{name} live patch was not command-only")
    result["resourceVersion"] = patched["metadata"]["resourceVersion"]
    desired = desired_replicas(patched)
    if desired > 0:
        result["rollout"] = wait_ready_new_pod(name, old_uids, desired, out)
    return result


def patch_scale_embb(before: dict, old_uids: set[str], out: Path, *, execute: bool) -> dict:
    ops = scale_patch_ops(before, 1)
    dry = kjson(["patch", "deployment", "n6-embb", "--type=json", "--dry-run=server", "-o", "json", "-p", json.dumps(ops)])
    write_json(out / "dryrun-scale-n6-embb.json", dry)
    if not scale_delta_only(before, dry, 1):
        raise Blocked("n6-embb scale dry-run was not replicas-only")
    result = {"deployment": "n6-embb", "scale": "0->1"}
    if not execute:
        return result
    patched = kjson(["patch", "deployment", "n6-embb", "--type=json", "-o", "json", "-p", json.dumps(ops)])
    write_json(out / "patched-scale-n6-embb.json", patched)
    if not scale_delta_only(before, patched, 1):
        raise Blocked("n6-embb live scale was not replicas-only")
    result["resourceVersion"] = patched["metadata"]["resourceVersion"]
    result["rollout"] = wait_ready_new_pod("n6-embb", old_uids, 1, out)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Perform the guarded live mutations after dry-run fences pass.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    out = args.output
    report = {
        "startedAt": utc(),
        "context": CONTEXT,
        "namespace": NAMESPACE,
        "kubectl": kubectl_argv([]),
        "execute": args.execute,
        "mutations": [],
        "checks": [],
    }
    try:
        out.mkdir(parents=True, exist_ok=False)
        report["checks"].append({"preZero": verify_zero_baseline(out, "pre")})
        before, before_pods, before_resources = capture_snapshot(out, "pre")
        assert_n6_replica_baseline(before)
        old_security = {name: security_fingerprint(dep) for name, dep in before.items()}
        old_pods = {name: pod_uids(before_pods, name) for name in DEPLOYMENTS}
        for name in DEPLOYMENTS:
            report["mutations"].append(patch_command(name, before[name], old_pods[name], old_security[name], out, execute=args.execute))
        embb_for_scale = kjson(["get", "deployment", "n6-embb", "-o", "json"]) if args.execute else before["n6-embb"]
        report["mutations"].append(patch_scale_embb(embb_for_scale, old_pods["n6-embb"], out, execute=args.execute))
        if args.execute:
            after, after_pods, after_resources = capture_snapshot(out, "post")
            for name in DEPLOYMENTS:
                if security_fingerprint(after[name]) != old_security[name]:
                    raise Blocked(f"{name} traffic security/resources changed")
                idx = traffic_index(after[name])
                if after[name]["spec"]["template"]["spec"]["containers"][idx].get("command") != REAPER_COMMAND:
                    raise Blocked(f"{name} traffic command is not the Bash reaper")
            assert_other_resources_unchanged(before_resources, after_resources)
            assert_unaffected_pods_unchanged(before_pods, after_pods)
            report["fixture"] = run_fixture(out)
            report["checks"].append({"postZero": verify_zero_baseline(out, "post")})
            report["finalN6Ready"] = assert_final_n6_ready(out)
        report["finishedAt"] = utc()
        report["passed"] = True
        write_json(out / "report.json", report)
        print(json.dumps({"passed": True, "execute": args.execute, "output": str(out), "mutations": report["mutations"]}, sort_keys=True))
        return 0
    except Exception as exc:
        report["finishedAt"] = utc()
        report["passed"] = False
        report["error"] = str(exc)
        if out.exists():
            write_json(out / "report.json", report)
        print(json.dumps({"passed": False, "execute": args.execute, "output": str(out), "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
