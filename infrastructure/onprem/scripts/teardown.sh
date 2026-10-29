#!/usr/bin/env bash
# REQ-T1: Explicit cluster/namespace UID; REQ-T2: dedicated namespace only.
# REQ-T3: Retain host data/PVs; REQ-T4: never reset shared cluster/CNI.
set -euo pipefail
usage() {
  echo 'Usage: teardown.sh --kubeconfig FILE --context NAME --namespace free5gc-onprem --namespace-uid UID [--canary-uid UID] [--delete-retained-pv-definitions] --apply' >&2
}
kubeconfig='' context='' namespace='' expected_uid='' canary_uid='' apply=false
delete_pv=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig) kubeconfig=${2:?}; shift 2 ;;
    --context) context=${2:?}; shift 2 ;;
    --namespace) namespace=${2:?}; shift 2 ;;
    --namespace-uid) expected_uid=${2:?}; shift 2 ;;
    --canary-uid) canary_uid=${2:?}; shift 2 ;;
    --delete-retained-pv-definitions) delete_pv=true; shift ;;
    --apply) apply=true; shift ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$kubeconfig" == /* && -f "$kubeconfig" && -n "$context" && "$namespace" == free5gc-onprem && -n "$expected_uid" ]] || { usage; exit 2; }
for tool in kubectl helm python3; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 2; }; done
k=(kubectl --kubeconfig "$kubeconfig" --context "$context")
h=(helm --kubeconfig "$kubeconfig" --kube-context "$context")
check_namespace() {
  local name=$1 uid=$2
  "${k[@]}" get namespace "$name" --ignore-not-found -o json | python3 -c '
import json,sys
raw=sys.stdin.read().strip()
if not raw:print("absent");raise SystemExit(0)
x=json.loads(raw);name,uid=sys.argv[1:];m=x["metadata"]
assert m["name"]==name and m["uid"]==uid,"Namespace UID changed"
assert m.get("labels",{}).get("cityverse.io/platform")=="kubeadm","Not a commissioned CityVerse namespace"
if name=="cityverse-device-canary":assert m["labels"].get("cityverse.io/managed-by")=="isolated-device-ran","Canary owner changed"
print("present")
' "$name" "$uid"
}
main_state=$(check_namespace "$namespace" "$expected_uid")
canary_state=absent
if [[ -n "$canary_uid" ]]; then canary_state=$(check_namespace cityverse-device-canary "$canary_uid"); fi
echo "Verified dedicated project namespace $namespace UID $expected_uid"
echo 'PVCs inside selected namespaces will be deleted. Host data, source, host DBs, logs and credentials are preserved.'
pv_records=''
if "$delete_pv"; then
  pv_records=$("${k[@]}" get pv -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
 s=p["spec"];c=s.get("claimRef",{});m=p["metadata"]
 if c.get("namespace")!="free5gc-onprem":continue
 assert m["name"].startswith("cityverse-") and s.get("persistentVolumeReclaimPolicy")=="Retain"
 path=s.get("local",{}).get("path","")
 assert path.startswith("/var/lib/cityverse-") and ".." not in path and s.get("nodeAffinity"),"Uncommissioned local PV scope"
 print(m["name"],m["uid"],c["uid"],path,sep="\t")
')
fi
if ! "$apply"; then echo 'Read-only verification; add --apply to remove deployments'; exit 0; fi
# Delete the owned canary before its referenced main NADs disappear.
if [[ "$canary_state" == present ]]; then
  [[ $(check_namespace cityverse-device-canary "$canary_uid") == present ]]
  "${k[@]}" delete namespace cityverse-device-canary --wait=true --timeout=120s
fi
if [[ "$main_state" == present ]]; then
  releases=$("${h[@]}" list -n "$namespace" --all -q)
  while IFS= read -r release; do
    [[ -z "$release" ]] && continue
    [[ $(check_namespace "$namespace" "$expected_uid") == present ]]
    "${h[@]}" uninstall "$release" -n "$namespace" --wait --timeout 120s
  done <<< "$releases"
  [[ $(check_namespace "$namespace" "$expected_uid") == present ]]
  "${k[@]}" delete namespace "$namespace" --wait=true --timeout=180s
else
  echo 'Project namespace is already absent'
fi
if "$delete_pv"; then
  while IFS=$'\t' read -r pv uid claim_uid local_path; do
    [[ -z "$pv" ]] && continue
    "${k[@]}" get pv "$pv" -o json | python3 -c '
import json,sys
x=json.load(sys.stdin);name,uid,claim_uid,path=sys.argv[1:];s=x["spec"]
assert x["metadata"]["name"]==name and x["metadata"]["uid"]==uid
assert s["claimRef"]["uid"]==claim_uid and s["claimRef"]["namespace"]=="free5gc-onprem"
assert s["persistentVolumeReclaimPolicy"]=="Retain" and s["local"]["path"]==path and s.get("nodeAffinity")
assert x["status"]["phase"]=="Released","PV is not released"
' "$pv" "$uid" "$claim_uid" "$local_path"
    "${k[@]}" delete pv "$pv" --wait=true --timeout=60s
  done <<< "$pv_records"
fi
echo 'Project namespace runtime removed; shared kubeadm, CNI, Docker and local PV data preserved'
