#!/usr/bin/env bash
# REQ-D1: scoped N3 delay; REQ-D2: ownership/rollback; REQ-D3: no API logic.
set -euo pipefail
usage() { echo 'Usage: n3-delay.sh {apply|remove|show} PID ENDPOINT_JSON_FILE'; }
[[ $# == 3 ]] || { usage >&2; exit 2; }
action=$1 pid=$2 plan=$3
[[ $action =~ ^(apply|remove|show)$ && $pid =~ ^[1-9][0-9]*$ && -f $plan ]] || { usage >&2; exit 2; }
[[ $EUID == 0 ]] || { echo 'root is required' >&2; exit 2; }
for tool in nsenter tc jq; do command -v "$tool" >/dev/null; done
# No arbitrary host interface: callers must resolve a current Pod sandbox PID.
[[ $(readlink "/proc/$pid/ns/net") != "$(readlink /proc/1/ns/net)" ]] || { echo 'host network namespace refused' >&2; exit 2; }
jq -e '.interface == "n3" and (.peers|length >= 1 and length <= 7) and
  all(.peers[]; (.ip|test("^172\\.22\\.(31|32|33|34)\\.[0-9]{1,3}$")) and
  (.delayUs|type == "number" and . >= 0 and . <= 100000 and floor == .)) and
  ([.peers[].ip]|length == (unique|length))' "$plan" >/dev/null
run() { nsenter -t "$pid" -n -- "$@"; }
run ip link show dev n3 >/dev/null
existing=$(run tc -j qdisc show dev n3)
if [[ $action == show ]]; then run tc -s -j qdisc show dev n3; exit; fi
# c170 is exclusively owned here. Refuse foreign qdiscs instead of replacing them.
if ! jq -e 'all(.[]; (.kind == "noqueue" and .handle == "0:") or
  (.kind == "prio" and .handle == "c170:") or
  (.kind == "netem" and (.parent|startswith("c170:"))) or
  (.kind == "pfifo" and (.parent|startswith("c170:"))))' <<<"$existing" >/dev/null; then
  echo 'foreign N3 qdisc present; refusing to overwrite' >&2; exit 1
fi
if jq -e 'any(.[]; .handle == "c170:" and .kind == "prio")' <<<"$existing" >/dev/null; then
  run tc qdisc del dev n3 root handle c170:
fi
[[ $action == apply ]] || exit 0
rollback() { run tc qdisc del dev n3 root handle c170: 2>/dev/null || true; }
trap rollback ERR
# All unmatched packets go to band 16. Match all UDP to named N3 peers:
# a port match would exclude non-initial IPv4 fragments of GTP-U packets.
# One class per gNB gives a shared UPF distinct distances.
run tc qdisc add dev n3 root handle c170: prio bands 16 priomap 15 15 15 15 15 15 15 15 15 15 15 15 15 15 15 15
index=0
while IFS=$'\t' read -r peer delay; do
  index=$((index + 1))
  class=$(printf '%x' "$index")
  handle=$(printf '%x' "$((0xd000 + index))")
  run tc qdisc add dev n3 parent "c170:$class" handle "$handle:" netem delay "${delay}us" limit 10000
  run tc filter add dev n3 parent c170: protocol ip pref "$index" u32 \
    match ip protocol 17 0xff match ip dst "$peer/32" flowid "c170:$class"
done < <(jq -r '.peers[]|[.ip,.delayUs]|@tsv' "$plan")
trap - ERR
run tc -s -j qdisc show dev n3
