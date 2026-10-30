"""Fixed, replay-safe contract for detached batch workers."""
import base64
import hashlib
import ipaddress
import json
import math
import re

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.tuning import TUNED_SCHEMA, tuning_manifest, validate_tuning

SCHEMA = "cityverse.detached-shard/v1"
ACTIONS = frozenset(("identity", "capability", "start", "status", "release", "collect", "cancel"))
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"

SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# == 3 ]] || exit 2
action=$1; encoded=$2; epoch=$3
[[ $action =~ ^(identity|start|status|release|collect|cancel)$ && $epoch =~ ^[0-9]+$ ]] || exit 2
boot=$(< /proc/sys/kernel/random/boot_id); netns=$(readlink /proc/self/ns/net)
[[ $boot =~ ^[a-f0-9-]{36}$ && $netns =~ ^net:\[[0-9]+\]$ ]] || exit 5
if [[ $action == identity ]]; then
  [[ $encoded == - && $epoch == 0 ]] || exit 2
  printf 'IDENTITYv1 %s %s\n' "$boot" "$netns"
  exit 0
fi
raw=$(printf '%s' "$encoded" | base64 --decode) || exit 2
token=$(printf '%s' "$raw" | sed -n 's/.*"token":"\([a-f0-9]\{32\}\)".*/\1/p')
expected_boot=$(printf '%s' "$raw" | sed -n 's/.*"selectedBootId":"\([a-f0-9-]\{36\}\)".*/\1/p')
expected_netns=$(printf '%s' "$raw" | sed -n 's/.*"selectedNetns":"\(net:\[[0-9]\+\]\)".*/\1/p')
[[ $token =~ ^[a-f0-9]{32}$ && $expected_boot == "$boot" && $expected_netns == "$netns" ]] || exit 4
directory=/tmp/cityverse-detached-$token
umask 077
digest=$(printf '%s' "$raw" | sha256sum); digest=${digest%% *}
monotonic_now() { cut -d' ' -f1 /proc/uptime; }
json_number() { printf '%s' "$raw" | sed -n "s/.*\"$1\":\([0-9.]*\).*/\1/p"; }
before_deadline() { awk -v now="$(monotonic_now)" -v limit="$1" 'BEGIN { exit !(now <= limit) }'; }
identity() {
  local pid starttime pgid observed_starttime observed_pgid observed_state
  [[ -f $directory/process && ! -L $directory/process ]] || return 1
  read -r pid starttime pgid < "$directory/process"
  [[ $pid =~ ^[0-9]+$ && $starttime =~ ^[0-9]+$ && $pgid =~ ^[0-9]+$ && -r /proc/$pid/stat ]] || return 1
  observed_starttime=$(awk '{print $22}' "/proc/$pid/stat") || return 1
  observed_pgid=$(awk '{print $5}' "/proc/$pid/stat") || return 1
  observed_state=$(awk '{print $3}' "/proc/$pid/stat") || return 1
  [[ $observed_state != Z && $observed_starttime == "$starttime" && $observed_pgid == "$pgid" && $pid == "$pgid" ]]
}
state() {
  if [[ -f $directory/seal ]]; then printf SEALED
  elif [[ -f $directory/cancelled ]]; then printf CANCELLED
  elif [[ -f $directory/failed ]]; then printf FAILED
  elif [[ -f $directory/listening ]]; then printf LISTENING
  elif [[ -f $directory/release ]]; then printf RUNNING
  else printf ARMED; fi
}
compact_sealed() {
  local target=$1 expected actual count observed key port
  [[ -d $target && ! -L $target && -f $target/seal && ! -L $target/seal \
      && -f $target/raw && ! -L $target/raw && -f $target/keys && ! -L $target/keys ]] || return 0
  read -r expected count < "$target/seal" || return 0
  [[ $expected =~ ^[a-f0-9]{64}$ && $count =~ ^[0-9]+$ ]] || return 0
  actual=$(sha256sum "$target/raw"); actual=${actual%% *}
  [[ $actual == "$expected" && $(wc -l < "$target/raw") == "$count" ]] || return 0
  # Per-job files are byte-for-byte inputs of the sealed aggregate.  Remove
  # only when their ordered stream proves the same digest and line count.
  observed=$(while read -r key port; do
    [[ $key =~ ^[a-z0-9-]{1,64}$ && -f $target/$key.raw && ! -L $target/$key.raw ]] || exit 1
    cat "$target/$key.raw"
  done < "$target/keys" | sha256sum) || return 0
  observed=${observed%% *}; [[ $observed == "$expected" ]] || return 0
  while read -r key port; do rm -f -- "$target/$key.raw"; done < "$target/keys"
  sync -- "$target"
}
assert_sockets_absent() {
  local table
  for table in "$directory/sockets" /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
    [[ -r $table ]] || return 2
  done
  # Read each socket table once. An unreadable/malformed table is unknown,
  # never an absence receipt; a surviving exact inode still blocks closure.
  awk '
    FILENAME==ARGV[1] {if($1 !~ /^[1-9][0-9]*$/) bad=1; wanted[$1]=1; next}
    FNR==1 {if($1!="sl" || $2!="local_address") bad=1; headers++; next}
    $10 in wanted {found=1}
    END {if(bad || headers!=4) exit 2; exit found?5:0}
  ' "$directory/sockets" /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6
}
if [[ $action == start ]]; then
  [[ $epoch == 0 ]] || exit 2
  before_deadline "$(json_number admissionDeadline)" || exit 5
  if mkdir "$directory" 2>/dev/null; then
    ( set -o noclobber; printf '%s' "$raw" > "$directory/reservation.tmp" ) || exit 4
    chmod 400 "$directory/reservation.tmp"; sync -- "$directory/reservation.tmp"
    mv "$directory/reservation.tmp" "$directory/reservation.json"
    printf '%s %s %s\n' "$digest" "$boot" "$netns" > "$directory/claim.tmp"
    chmod 400 "$directory/claim.tmp"; sync -- "$directory/claim.tmp"
    mv "$directory/claim.tmp" "$directory/claim"; sync -- "$directory"
    cat > "$directory/supervisor.tmp" <<'SUPERVISOR'
#!/usr/bin/env bash
set -Eeuo pipefail
directory=$1
fail() { printf '%s\n' "$1" > "$directory/failed.tmp"; chmod 400 "$directory/failed.tmp"; mv "$directory/failed.tmp" "$directory/failed"; }
trap 'fail supervisor-crash' ERR
pid=$$; starttime=$(awk '{print $22}' "/proc/$$/stat"); pgid=$(awk '{print $5}' "/proc/$$/stat")
[[ $pid == "$pgid" ]] || exit 70
role=$(sed -n 's/.*"selectedRole":"\([^"]*\)".*/\1/p' "$directory/reservation.json")
# Parse and filter the immutable plan before the client release epoch so launch
# skew contains only fork overhead, not repeated JSON extraction.
grep -o '{"direction":"[^"]*","duration":[0-9]*,"endpoint":"[^"]*","key":"[^"]*","offeredKbps":[0-9]*,"port":[0-9]*,"receiverPod":"[^"]*","receiverUid":"[^"]*","tun":"[^"]*","ueIp":"[^"]*","uePod":"[^"]*","ueUid":"[^"]*"}' "$directory/reservation.json" > "$directory/jobs"
selected_uid=$(sed -n 's/.*"selectedPodUid":"\([^"]*\)".*/\1/p' "$directory/reservation.json")
# Each line above is a canonical, host-validated IPv4 job with twelve fixed
# fields and no escaped delimiters. Parse it once instead of forking sed for
# every field of every flow; five server shards otherwise launch thousands of
# short processes while competing with their own admission deadline.
awk -F '[,:]' -v selected_uid="$selected_uid" -v role="$role" '
  BEGIN { OFS="\t" }
  NF != 24 { exit 71 }
  {
    for (i=1; i<=NF; i+=2) {
      key=$i; value=$(i+1); gsub(/[{}"]/, "", key); gsub(/[{}"]/, "", value)
      values[key]=value
    }
    owner=(role=="server" ? values["receiverUid"] : values["ueUid"])
    if (owner==selected_uid)
      print values["key"], values["endpoint"], values["port"], values["ueIp"], values["tun"], values["offeredKbps"], values["duration"], values["direction"]
  }
' "$directory/jobs" > "$directory/plan"
[[ -s $directory/plan ]] || exit 71
if [[ $role == client ]]; then
  printf '%s %s %s\n' "$pid" "$starttime" "$pgid" > "$directory/ready.tmp"; chmod 400 "$directory/ready.tmp"; sync -- "$directory/ready.tmp"; mv "$directory/ready.tmp" "$directory/ready"; sync -- "$directory"
  child_deadline=$(sed -n 's/.*"clientChildDeadline":\([0-9.]*\).*/\1/p' "$directory/reservation.json")
  while [[ ! -f $directory/release && ! -f $directory/cancel.request ]]; do
    [[ -d $directory ]] || exit 73
    now=$(cut -d' ' -f1 /proc/uptime)
    awk -v n="$now" -v d="$child_deadline" 'BEGIN{exit !(n<=d)}' || { fail release-deadline-expired; exit 73; }
    sleep .05
  done
  [[ ! -f $directory/cancel.request ]] || { printf cancelled > "$directory/cancelled.tmp"; mv "$directory/cancelled.tmp" "$directory/cancelled"; exit 0; }
  release_epoch=$(< "$directory/release")
  while (( $(date +%s) < release_epoch )); do [[ ! -f $directory/cancel.request ]] || exit 0; sleep .02; done
  child_deadline=$(sed -n 's/.*"clientChildDeadline":\([0-9.]*\).*/\1/p' "$directory/reservation.json")
  now=$(cut -d' ' -f1 /proc/uptime); awk -v n="$now" -v d="$child_deadline" 'BEGIN{exit !(n<=d)}' || exit 73
else
  child_deadline=$(sed -n 's/.*"serverChildDeadline":\([0-9.]*\).*/\1/p' "$directory/reservation.json")
  now=$(cut -d' ' -f1 /proc/uptime); awk -v n="$now" -v d="$child_deadline" 'BEGIN{exit !(n<=d)}' || exit 73
fi
: > "$directory/keys"; : > "$directory/children.tmp"
run_job() {
  local key=$1 endpoint=$2 port=$3 ue_ip=$4 tun=$5 rate=$6 duration=$7 direction=$8 started status output encoded
  started=$(date +%s.%N); status=0; output=
  if [[ $role == server ]]; then
    server_timeout=$(awk -v d="$child_deadline" -v n="$(cut -d' ' -f1 /proc/uptime)" 'BEGIN{x=d-n; if(x<1) exit 1; print int(x)+(x>int(x))}') || { status=124; server_timeout=1; }
    output=$(timeout --signal=TERM --kill-after=2 "$server_timeout" iperf3 -s -1 -B "$endpoint" -p "$port" -J 2>&1) || status=$?
  else
    # The pinned TUN selects the UE source address. The collector independently
    # verifies the observed socket source against ue_ip. Avoid explicit bind(0),
    # which can stall in WSL mirrored networking before any packet is sent.
    command=(iperf3 -c "$endpoint" -p "$port" -u -b "$((rate*1000))" -l 1200 -t "$duration" --bind-dev "$tun" -J)
    [[ $direction != dl ]] || command+=(-R)
    # TERM is scheduled no later than the immutable child deadline.  The
    # kill-after grace is cleanup only; it cannot turn a timed-out result into
    # success, publish raw as accepted evidence, or renew the deadline.
    client_timeout=$(awk -v d="$child_deadline" -v n="$(cut -d' ' -f1 /proc/uptime)" -v cap="$((duration+12))" 'BEGIN{x=d-n; if(x<=0) exit 1; remaining=int(x); if(remaining<=0) exit 1; print remaining<cap?remaining:cap}') || { status=124; client_timeout=0; }
    if (( client_timeout > 0 )); then
      output=$(timeout --signal=TERM --kill-after=2 "$client_timeout" "${command[@]}" 2>&1) || status=$?
    fi
  fi
  (( ${#output} <= 49152 )) || { output=${output:0:49152}; status=125; }
  finished=$(date +%s.%N); encoded=$(printf '%s' "$output" | base64 -w0)
  printf 'BATCHv1\t%s\t%s\t%s\t%s\t%s\n' "$key" "$started" "$finished" "$status" "$encoded" > "$directory/$key.raw.tmp"
  chmod 400 "$directory/$key.raw.tmp"; sync -- "$directory/$key.raw.tmp"; mv "$directory/$key.raw.tmp" "$directory/$key.raw"
}
while IFS=$'\t' read -r key endpoint port ue_ip tun rate duration direction; do
  printf '%s %s\n' "$key" "$port" >> "$directory/keys"
  run_job "$key" "$endpoint" "$port" "$ue_ip" "$tun" "$rate" "$duration" "$direction" & child=$!
  child_start=$(awk '{print $22}' "/proc/$child/stat"); child_pgid=$(awk '{print $5}' "/proc/$child/stat")
  printf '%s %s %s %s\n' "$key" "$child" "$child_start" "$child_pgid" >> "$directory/children.tmp"
done < "$directory/plan"
chmod 400 "$directory/children.tmp"; mv "$directory/children.tmp" "$directory/children"
listener_snapshot() {
  local table
  for table in "$directory/keys" /proc/net/tcp /proc/net/tcp6; do
    [[ -r $table ]] || return 2
  done
  # All expected ports must have exactly one TCP listener in this namespace.
  # Capture their inodes from the same scan used to decide readiness.
  awk '
    FILENAME==ARGV[1] {
      if($2 !~ /^[0-9]+$/ || $2<1 || $2>65535) bad=1
      p=sprintf("%04X",$2); if(p in expected) bad=1
      expected[p]=1; size++; next
    }
    FNR==1 {if($1!="sl" || $2!="local_address") bad=1; headers++; next}
    $4=="0A" {
      p=$2; sub(/^.*:/,"",p)
      if(p in expected) {count[p]++; inode[p]=$10}
    }
    END {
      if(bad || !size || headers!=2) exit 2
      for(p in expected) if(count[p]!=1 || inode[p] !~ /^[1-9][0-9]*$/) exit 1
      for(p in expected) print inode[p]
    }
  ' "$directory/keys" /proc/net/tcp /proc/net/tcp6
}
if [[ $role == server ]]; then
  ready=0
  while awk -v n="$(cut -d' ' -f1 /proc/uptime)" -v d="$child_deadline" 'BEGIN{exit !(n<=d)}'; do
    ready=0
    if listener_snapshot > "$directory/sockets.tmp"; then ready=1; fi
    (( ready == 0 )) || break; sleep .01
  done
  (( ready == 1 )) || exit 75
  [[ $(wc -l < "$directory/sockets.tmp") == $(wc -l < "$directory/keys") ]] || exit 75
  chmod 400 "$directory/sockets.tmp"; mv "$directory/sockets.tmp" "$directory/sockets"
  printf listening > "$directory/listening.tmp"; chmod 400 "$directory/listening.tmp"; mv "$directory/listening.tmp" "$directory/listening"
  printf '%s %s %s\n' "$pid" "$starttime" "$pgid" > "$directory/ready.tmp"; chmod 400 "$directory/ready.tmp"; sync -- "$directory/ready.tmp"; mv "$directory/ready.tmp" "$directory/ready"; sync -- "$directory"
fi
wait || true
: > "$directory/raw.tmp"
while read -r key port; do [[ -f $directory/$key.raw ]] || exit 72; cat "$directory/$key.raw" >> "$directory/raw.tmp"; done < "$directory/keys"
# Measured 40-job server aggregates are about 0.67 MiB.  A 1 MiB hard
# ceiling leaves 50% headroom and bounds five concurrent shards to at most
# 10 MiB while both per-job inputs and the aggregate exist.
(( $(wc -c < "$directory/raw.tmp") <= 1048576 )) || exit 76
transport_deadline=$(sed -n 's/.*"transportDeadline":\([0-9.]*\).*/\1/p' "$directory/reservation.json")
now=$(cut -d' ' -f1 /proc/uptime); awk -v n="$now" -v d="$transport_deadline" 'BEGIN{exit !(n<=d)}' || exit 74
chmod 400 "$directory/raw.tmp"; sync -- "$directory/raw.tmp"; mv "$directory/raw.tmp" "$directory/raw"
raw_digest=$(sha256sum "$directory/raw"); raw_digest=${raw_digest%% *}; count=$(wc -l < "$directory/raw")
printf '%s %s\n' "$raw_digest" "$count" > "$directory/seal.tmp"; chmod 400 "$directory/seal.tmp"; sync -- "$directory/seal.tmp"; mv "$directory/seal.tmp" "$directory/seal"; sync -- "$directory"
# The aggregate was just built from these exact ordered files and sealed.
# Keep the aggregate plus seal; reclaim only its duplicate inputs.
while read -r key port; do rm -f -- "$directory/$key.raw"; done < "$directory/keys"
sync -- "$directory"
SUPERVISOR
    chmod 500 "$directory/supervisor.tmp"; sync -- "$directory/supervisor.tmp"; mv "$directory/supervisor.tmp" "$directory/supervisor"
    setsid bash -c 'for path in /proc/$$/fd/*; do fd=${path##*/}; [[ $fd =~ ^[0-9]+$ ]] && (( fd > 2 )) && eval "exec ${fd}>&-"; done; exec bash "$1" "$2"' detached-supervisor "$directory/supervisor" "$directory" </dev/null >/dev/null 2>&1 &
    supervisor=$!
    admission=$(json_number admissionDeadline)
    while before_deadline "$admission"; do [[ -f $directory/ready ]] && break; kill -0 "$supervisor" 2>/dev/null || exit 5; sleep .01; done
    [[ -f $directory/ready ]] || exit 5
    mv "$directory/ready" "$directory/process"; chmod 400 "$directory/process"; sync -- "$directory/process"; sync -- "$directory"
  else
    [[ -d $directory && ! -L $directory && -f $directory/reservation.json && ! -L $directory/reservation.json ]] || exit 4
    actual=$(sha256sum "$directory/reservation.json"); actual=${actual%% *}
    [[ $actual == "$digest" && "$(< "$directory/reservation.json")" == "$raw" ]] || exit 4
  fi
  identity || exit 5
  read -r pid starttime pgid < "$directory/process"
  printf 'STARTEDv1 %s %s %s %s %s %s %s\n' "$token" "$digest" "$pid" "$starttime" "$pgid" "$boot" "$netns"
  exit 0
fi
if [[ $action == cancel && ! -e $directory ]]; then
  [[ $epoch == 0 ]] || exit 2
  mkdir "$directory" 2>/dev/null || true
  if [[ ! -f $directory/reservation.json ]]; then
    ( set -o noclobber; printf '%s' "$raw" > "$directory/reservation.json" ) 2>/dev/null || true
    chmod 400 "$directory/reservation.json"; printf '%s %s %s\n' "$digest" "$boot" "$netns" > "$directory/claim"; chmod 400 "$directory/claim"
    printf cancelled > "$directory/cancelled"; chmod 400 "$directory/cancelled"
    closure=$(printf '%s\n' "$digest cancelled" | sha256sum); closure=${closure%% *}; printf '%s\n' "$closure" > "$directory/closure"; chmod 400 "$directory/closure"; sync -- "$directory"
    printf 'ABSENTv2 %s %s %s\n' "$token" "$digest" "$closure"; exit 0
  fi
fi
[[ -d $directory && ! -L $directory && -f $directory/reservation.json && ! -L $directory/reservation.json ]] || exit 4
actual=$(sha256sum "$directory/reservation.json"); actual=${actual%% *}; [[ $actual == "$digest" ]] || exit 4
read -r claim_digest claim_boot claim_netns < "$directory/claim"; [[ $claim_digest == "$digest" && $claim_boot == "$boot" && $claim_netns == "$netns" ]] || exit 4
if [[ $action == release ]]; then
  [[ $epoch -gt 0 ]] || exit 2
  [[ $(sed -n 's/.*"selectedRole":"\([^"]*\)".*/\1/p' "$directory/reservation.json") == client ]] || exit 4
  if [[ ! -f $directory/release ]]; then
    wall_now=$(date +%s); (( epoch > wall_now )) || exit 4
    if [[ $(sed -n 's/.*"selectedRole":"\([^"]*\)".*/\1/p' "$directory/reservation.json") == server ]]; then deadline=$(json_number serverChildDeadline); else deadline=$(json_number clientChildDeadline); fi
    projected=$(awk -v m="$(monotonic_now)" -v delay="$((epoch-wall_now))" 'BEGIN{print m+delay}')
    awk -v p="$projected" -v d="$deadline" 'BEGIN{exit !(p<=d)}' || exit 5
  fi
  if ( set -o noclobber; printf '%s\n' "$epoch" > "$directory/release" ) 2>/dev/null; then chmod 400 "$directory/release"; sync -- "$directory/release"; sync -- "$directory"
  else [[ -f $directory/release && $(< "$directory/release") == "$epoch" ]] || exit 4; fi
  printf 'RELEASEDv1 %s %s %s\n' "$token" "$digest" "$epoch"; exit 0
fi
[[ $epoch == 0 ]] || exit 2
if [[ $action == status ]]; then
  read -r pid starttime pgid < "$directory/process" || exit 5
  release_epoch=-; [[ ! -f $directory/release ]] || release_epoch=$(< "$directory/release")
  current=$(state); if [[ $current == ARMED || $current == RUNNING || $current == LISTENING ]]; then identity || exit 5; fi
  printf 'STATUSv1 %s %s %s %s %s %s %s %s %s\n' "$token" "$current" "$digest" "$pid" "$starttime" "$pgid" "$boot" "$netns" "$release_epoch"; exit 0
fi
if [[ $action == collect ]]; then
  before_deadline "$(json_number transportDeadline)" || exit 5
  [[ -f $directory/seal && -f $directory/raw && ! -L $directory/seal && ! -L $directory/raw ]] || exit 5
  read -r seal_digest count < "$directory/seal"; actual=$(sha256sum "$directory/raw"); actual=${actual%% *}
  [[ $seal_digest == "$actual" && $count =~ ^[0-9]+$ && $(wc -l < "$directory/raw") == "$count" ]] || exit 5
  printf 'SEALEDv1 %s %s %s %s\n' "$token" "$digest" "$seal_digest" "$count"; cat "$directory/raw"; exit 0
fi
if [[ $action == cancel ]]; then
  if [[ -f $directory/closure ]]; then
    compact_sealed "$directory"
    printf 'ABSENTv2 %s %s %s\n' "$token" "$digest" "$(< "$directory/closure")"; exit 0
  fi
  ( set -o noclobber; printf cancel > "$directory/cancel.request" ) 2>/dev/null || true
  if identity; then read -r pid starttime pgid < "$directory/process"; kill -TERM -- "-$pgid" 2>/dev/null || true; for _ in $(seq 1 50); do identity || break; sleep .02; done; identity && kill -KILL -- "-$pgid" 2>/dev/null || true; fi
  for _ in $(seq 1 50); do identity || break; sleep .02; done; identity && exit 5
  if [[ -f $directory/children ]]; then
    while read -r key child child_start child_pgid; do
      if [[ -r /proc/$child/stat ]]; then observed=$(awk '{print $22" "$5" "$3}' "/proc/$child/stat"); [[ $observed != "$child_start $child_pgid "* || $observed == *" Z" ]] || exit 5; fi
    done < "$directory/children"
  fi
  if [[ -f $directory/sockets ]]; then
    assert_sockets_absent
  fi
  [[ -f $directory/cancelled ]] || { printf cancelled > "$directory/cancelled.tmp"; chmod 400 "$directory/cancelled.tmp"; mv "$directory/cancelled.tmp" "$directory/cancelled"; }
  closure=$(printf '%s\n' "$digest cancelled" | sha256sum); closure=${closure%% *}; printf '%s\n' "$closure" > "$directory/closure.tmp"; chmod 400 "$directory/closure.tmp"; mv "$directory/closure.tmp" "$directory/closure"
  compact_sealed "$directory"
  printf 'ABSENTv2 %s %s %s\n' "$token" "$digest" "$closure"; exit 0
fi
exit 2
'''

# Keep v1 byte-for-byte: historical reservations retain their original argv,
# manifest and cancellation protocol regardless of current process settings.
TUNED_CLOCK_HELPERS = r'''
deadline_decimal_le() {
  local LC_ALL=C left=${1-} right=${2-} li lf ri rf
  # The complete reservation already has this fixed wire ceiling.
  (( ${#left} <= 65536 && ${#right} <= 65536 )) || return 2
  [[ $left =~ ^[0-9]+([.][0-9]+)?$ && $right =~ ^[0-9]+([.][0-9]+)?$ ]] || return 2
  li=${left%%.*}; ri=${right%%.*}; lf=; rf=
  [[ $left != *.* ]] || lf=${left#*.}
  [[ $right != *.* ]] || rf=${right#*.}
  li=${li#"${li%%[!0]*}"}; li=${li:-0}
  ri=${ri#"${ri%%[!0]*}"}; ri=${ri:-0}
  lf=${lf%"${lf##*[!0]}"}; rf=${rf%"${rf##*[!0]}"}
  if (( ${#li} < ${#ri} )); then return 0; fi
  if (( ${#li} > ${#ri} )); then return 1; fi
  if [[ $li < $ri ]]; then return 0; fi
  if [[ $li > $ri ]]; then return 1; fi
  if [[ $lf == "$rf" || $lf < $rf ]]; then return 0; fi
  return 1
}
deadline_clock_before() {
  local now idle
  IFS=' ' builtin read -r now idle < /proc/uptime || return 2
  # read puts any third field in idle too; malformed/partial reads are unknown.
  [[ $idle =~ ^[0-9]+([.][0-9]+)?$ ]] || return 2
  deadline_decimal_le "$now" "${1-}"
}
deadline_wall_epoch() {
  local observed=${EPOCHSECONDS-}
  deadline_wall_value=
  [[ $observed =~ ^(0|[1-9][0-9]{0,14})$ ]] || return 2
  deadline_wall_value=$observed
}
deadline_wall_timestamp() {
  local observed=${EPOCHREALTIME-}
  deadline_wall_value=
  [[ $observed =~ ^(0|[1-9][0-9]{0,14})[.][0-9]{6}$ ]] || return 2
  deadline_wall_value=$observed
}
deadline_remaining() {
  local limit=${1-} now=${2-} mode=${3-} cap=${4-} li ni lf nf whole
  deadline_remaining_value=
  [[ $mode == floor || $mode == ceil ]] || return 2
  (( ${#limit} <= 64 && ${#now} <= 64 )) || return 2
  # Validate the complete decimal wire before any arithmetic expansion. Bound
  # normalized integer parts to 15 digits, safely below Bash's signed64 limit.
  [[ $limit =~ ^[0-9]+([.][0-9]+)?$ && $now =~ ^[0-9]+([.][0-9]+)?$ ]] || return 2
  li=${limit%%.*}; ni=${now%%.*}; lf=; nf=
  [[ $limit != *.* ]] || lf=${limit#*.}
  [[ $now != *.* ]] || nf=${now#*.}
  li=${li#"${li%%[!0]*}"}; li=${li:-0}
  ni=${ni#"${ni%%[!0]*}"}; ni=${ni:-0}
  (( ${#li} <= 15 && ${#ni} <= 15 )) || return 2
  lf=${lf%"${lf##*[!0]}"}; nf=${nf%"${nf##*[!0]}"}
  whole=$((10#$li - 10#$ni))
  if [[ $mode == floor ]]; then
    [[ $cap =~ ^[1-9][0-9]{0,14}$ ]] || return 2
    if [[ $lf < $nf ]]; then whole=$((whole-1)); fi
    (( whole > 0 )) || return 1
    if (( whole > 10#$cap )); then whole=$((10#$cap)); fi
  else
    # The original server rejects x<1 before computing ceil(x), including a
    # positive fraction. Keep its expired/short-budget fallback unchanged.
    (( whole >= 1 )) || return 1
    if (( whole == 1 )) && [[ $lf < $nf ]]; then return 1; fi
    if [[ $lf > $nf ]]; then whole=$((whole+1)); fi
  fi
  deadline_remaining_value=$whole
}
deadline_clock_remaining() {
  local now idle
  deadline_remaining_value=
  IFS=' ' builtin read -r now idle < /proc/uptime || return 2
  [[ $idle =~ ^[0-9]+([.][0-9]+)?$ ]] || return 2
  deadline_remaining "${1-}" "$now" "${2-}" "${3-}"
}
'''

# Only v2 batches validated immutable job-output paths into bounded argv. The
# host reservation already permits at most 100 unique u00-ul..u49-dl jobs.
# Keep the array through sealing: cleanup uses precisely the inputs just sealed.
TUNED_SEAL_INPUTS = r'''[[ -d $directory && ! -L $directory && -f $directory/keys && ! -L $directory/keys ]] || exit 72
raw_inputs=(); declare -A raw_keys=()
while IFS= read -r key_line || [[ -n $key_line ]]; do
  [[ $key_line =~ ^(u[0-4][0-9]-(ul|dl))[[:blank:]]+([0-9]{4})$ ]] || exit 72
  key=${BASH_REMATCH[1]}; port=${BASH_REMATCH[3]}
  [[ $port > 5199 && $port < 6200 && ! ${raw_keys[$key]+present} ]] || exit 72
  (( ${#raw_inputs[@]} < 100 )) || exit 72
  [[ -f $directory/$key.raw && ! -L $directory/$key.raw ]] || exit 72
  raw_keys[$key]=1; raw_inputs+=("$directory/$key.raw")
done < "$directory/keys"
(( ${#raw_inputs[@]} > 0 )) || exit 72
cat -- "${raw_inputs[@]}" > "$directory/raw.tmp"'''

TUNED_SCRIPT = SCRIPT.replace(
    "identity|start|status|release|collect|cancel", "identity|capability|start|status|release|collect|cancel", 1
).replace('if [[ $action == start ]]; then', r'''if [[ $action == capability ]]; then
  [[ $epoch == 0 ]] || exit 2
  deadline_wall_epoch && deadline_wall_timestamp || exit 5
  help=$(iperf3 --help 2>&1) || exit 5
  version=$(iperf3 --version 2>&1) || exit 5
  (( ${#help} <= 32768 && ${#version} <= 4096 )) || exit 5
  if [[ $raw == *'"udpGsro":true'* ]]; then
    [[ $help == *--gsro* && $version == *'GSO/GRO support'* ]] || exit 5
  fi
  size=$(json_number socketBufferBytes)
  [[ $size =~ ^[0-9]+$ ]] || exit 2
  if (( size > 0 )); then [[ $help == *--window* ]] || exit 5; fi
  printf 'CAPABLEv1 %s %s\n' "$token" "$digest"
  exit 0
fi
if [[ $action == start ]]; then
  deadline_wall_epoch && deadline_wall_timestamp || exit 5''', 1).replace(
    '# Parse and filter the immutable plan', r'''# These are requested settings frozen before launch, never process environment.
gsro_requested=0; socket_buffer=0
grep -q '"udpGsro":true' "$directory/reservation.json" && gsro_requested=1
socket_buffer=$(sed -n 's/.*"socketBufferBytes":\([0-9]*\).*/\1/p' "$directory/reservation.json")
[[ $socket_buffer =~ ^[0-9]+$ ]] || exit 71
# Parse and filter the immutable plan''', 1).replace(
    '    [[ $direction != dl ]] || command+=(-R)',
    '    (( gsro_requested == 0 )) || command+=(--gsro)\n'
    '    (( socket_buffer == 0 )) || command+=(-w "$socket_buffer")\n'
    '    [[ $direction != dl ]] || command+=(-R)', 1).replace(
    "before_deadline() { awk -v now=\"$(monotonic_now)\" -v limit=\"$1\" 'BEGIN { exit !(now <= limit) }'; }",
    TUNED_CLOCK_HELPERS + '\nbefore_deadline() { deadline_clock_before "$1"; }', 1).replace(
    'set -Eeuo pipefail\ndirectory=$1',
    'set -Eeuo pipefail\n' + TUNED_CLOCK_HELPERS + '\ndirectory=$1', 1).replace(
    "    now=$(cut -d' ' -f1 /proc/uptime)\n"
    "    awk -v n=\"$now\" -v d=\"$child_deadline\" 'BEGIN{exit !(n<=d)}' || { fail release-deadline-expired; exit 73; }",
    '    deadline_clock_before "$child_deadline" || { fail release-deadline-expired; exit 73; }', 1).replace(
    '    [[ -f $directory/ready ]] || exit 5\n    mv "$directory/ready" "$directory/process";',
    '    before_deadline "$admission" || exit 5\n'
    '    [[ -f $directory/ready ]] || exit 5\n    mv "$directory/ready" "$directory/process";', 1).replace(
    ': > "$directory/raw.tmp"\n'
    'while read -r key port; do [[ -f $directory/$key.raw ]] || exit 72; cat "$directory/$key.raw" >> "$directory/raw.tmp"; done < "$directory/keys"',
    TUNED_SEAL_INPUTS, 1).replace(
    'while read -r key port; do rm -f -- "$directory/$key.raw"; done < "$directory/keys"',
    'rm -f -- "${raw_inputs[@]}"', 1).replace(
    '  while (( $(date +%s) < release_epoch )); do [[ ! -f $directory/cancel.request ]] || exit 0; sleep .02; done',
    r'''  [[ $release_epoch =~ ^(0|[1-9][0-9]{0,14})$ ]] || exit 73
  while true; do
    deadline_wall_epoch || exit 73
    if deadline_decimal_le "$release_epoch" "$deadline_wall_value"; then break; fi
    [[ ! -f $directory/cancel.request ]] || exit 0
    sleep .02
  done''', 1).replace(
    '  started=$(date +%s.%N); status=0; output=',
    '  deadline_wall_timestamp || return 73\n  started=$deadline_wall_value; status=0; output=', 1).replace(
    '''    server_timeout=$(awk -v d="$child_deadline" -v n="$(cut -d' ' -f1 /proc/uptime)" 'BEGIN{x=d-n; if(x<1) exit 1; print int(x)+(x>int(x))}') || { status=124; server_timeout=1; }''',
    '    if deadline_clock_remaining "$child_deadline" ceil; then server_timeout=$deadline_remaining_value\n'
    '    else\n      clock_status=$?; (( clock_status == 1 )) || return 73\n'
    '      status=124; server_timeout=1\n    fi', 1).replace(
    '''    client_timeout=$(awk -v d="$child_deadline" -v n="$(cut -d' ' -f1 /proc/uptime)" -v cap="$((duration+12))" 'BEGIN{x=d-n; if(x<=0) exit 1; remaining=int(x); if(remaining<=0) exit 1; print remaining<cap?remaining:cap}') || { status=124; client_timeout=0; }''',
    '    if deadline_clock_remaining "$child_deadline" floor "$((duration+12))"; then client_timeout=$deadline_remaining_value\n'
    '    else\n      clock_status=$?; (( clock_status == 1 )) || return 73\n'
    '      status=124; client_timeout=0\n    fi', 1).replace(
    '  finished=$(date +%s.%N); encoded=$(printf \'%s\' "$output" | base64 -w0)',
    '  deadline_wall_timestamp || return 73\n  finished=$deadline_wall_value; encoded=$(printf \'%s\' "$output" | base64 -w0)', 1)

# Publish the complete immutable release epoch before making its name visible.
# O_EXCL on the final path creates an observable empty file before printf runs;
# a waiting supervisor can reject that partial epoch and exit without a job.
TUNED_RELEASE_PUBLISH = r'''  release_pending=$(mktemp "$directory/.release.XXXXXXXX") || exit 5
  trap 'rm -f -- "$release_pending"' EXIT
  printf '%s\n' "$epoch" > "$release_pending"
  chmod 400 "$release_pending"; sync -- "$release_pending"
  if ln -- "$release_pending" "$directory/release" 2>/dev/null; then
    rm -f -- "$release_pending"; sync -- "$directory"
  else
    [[ -f $directory/release && ! -L $directory/release && $(< "$directory/release") == "$epoch" ]] || exit 4
  fi'''

TUNED_SCRIPT = TUNED_SCRIPT.replace(
    r'''  if ( set -o noclobber; printf '%s\n' "$epoch" > "$directory/release" ) 2>/dev/null; then chmod 400 "$directory/release"; sync -- "$directory/release"; sync -- "$directory"
  else [[ -f $directory/release && $(< "$directory/release") == "$epoch" ]] || exit 4; fi''',
    TUNED_RELEASE_PUBLISH, 1).replace(
    # GNU timeout otherwise gives each iperf its own process group, outside the
    # exact supervisor group that cancel validates and terminates. iperf uses
    # threads; foreground mode retains the same deadlines and kill-after grace.
    'timeout --signal=TERM --kill-after=2',
    'timeout --foreground --signal=TERM --kill-after=2', 2)

# The manager has already decoded the reservation. Keep the child-side identity,
# clock, feature and receipt expressions byte-for-byte from the full worker;
# only base64 decoding and SHA calculation move to Go. Full worker bytes stay
# unchanged, including the script hash used by existing reservations.
TUNED_CAPABILITY_SCRIPT = (
    TUNED_SCRIPT[:TUNED_SCRIPT.index('monotonic_now()')]
    .replace('[[ $# == 3 ]] || exit 2', '[[ $# == 4 ]] || exit 2', 1)
    .replace('action=$1; encoded=$2; epoch=$3', 'action=$1; raw=$2; epoch=$3; digest=$4', 1)
    .replace('raw=$(printf \'%s\' "$encoded" | base64 --decode) || exit 2\n', '', 1)
    .replace('digest=$(printf \'%s\' "$raw" | sha256sum); digest=${digest%% *}\n', '', 1)
    + next(line + '\n' for line in TUNED_SCRIPT.splitlines() if line.startswith('json_number()'))
    + TUNED_CLOCK_HELPERS
    + TUNED_SCRIPT[TUNED_SCRIPT.index('if [[ $action == capability ]]; then'):
                   TUNED_SCRIPT.index('if [[ $action == start ]]; then')]
)

FIELDS = {"schemaVersion","profile","roundId","generation","shardIndex","shardCount","token",
          "manifestSha256","createdMonotonic","admissionDeadline","clientChildDeadline",
          "serverChildDeadline","transportDeadline","clockSource","clockBootId","roles","selectedRole","selectedPodUid",
          "selectedBootId","selectedNetns","jobs"}

def command(action, reservation=None, *, epoch=0):
    if action not in ACTIONS or type(epoch) is not int or epoch < 0:
        raise PolicyBlocked("Invalid detached worker action")
    if action == "identity":
        if reservation is not None or epoch:
            raise PolicyBlocked("Detached identity must be a pure read")
        return ["bash", "-c", SCRIPT, "cityverse-detached", action, "-", "0"]
    if (action == "release" and epoch < 1) or (action != "release" and epoch != 0):
        raise PolicyBlocked("Invalid detached worker epoch")
    tuned = isinstance(reservation, dict) and reservation.get("schemaVersion") == TUNED_SCHEMA
    if (not isinstance(reservation, dict)
            or set(reservation) != FIELDS | ({"trafficTuning"} if tuned else set())):
        raise PolicyBlocked("Invalid detached reservation schema")
    tuning = validate_tuning(reservation["trafficTuning"]) if tuned else None
    if action == "capability" and not tuned:
        raise PolicyBlocked("Legacy detached reservation has no tuning capability contract")
    if (reservation["schemaVersion"] not in {SCHEMA, TUNED_SCHEMA} or reservation["profile"] != "round-500-bidir18s-v4"
            or reservation["clockSource"] != "CLOCK_BOOTTIME"
            or re.fullmatch(UUID, str(reservation["clockBootId"])) is None
            or re.fullmatch(UUID, str(reservation["roundId"])) is None
            or type(reservation["generation"]) is not int or reservation["generation"] < 1
            or type(reservation["shardCount"]) is not int or not 2 <= reservation["shardCount"] <= 10
            or type(reservation["shardIndex"]) is not int or not 0 <= reservation["shardIndex"] < reservation["shardCount"]
            or re.fullmatch(r"[a-f0-9]{32}", str(reservation["token"])) is None
            or re.fullmatch(r"[a-f0-9]{64}", str(reservation["manifestSha256"])) is None
            or re.fullmatch(UUID, str(reservation["selectedBootId"])) is None
            or re.fullmatch(r"net:\[[0-9]+\]", str(reservation["selectedNetns"])) is None):
        raise PolicyBlocked("Invalid detached reservation identity")
    clocks=[reservation[k] for k in ("createdMonotonic","admissionDeadline","clientChildDeadline","serverChildDeadline","transportDeadline")]
    if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in clocks) or clocks[0] >= min(clocks[1:]):
        raise PolicyBlocked("Invalid detached reservation deadlines")
    roles=reservation["roles"]; keys={"role","namespace","pod","podUid","containerID","restartCount","bootId","netns"}
    if (not isinstance(roles,list) or not 1 <= len(roles) <= 11 or reservation["selectedRole"] not in ("server","client")
            or re.fullmatch(UUID,str(reservation["selectedPodUid"])) is None
            or sum(r.get("role")==reservation["selectedRole"] and r.get("podUid")==reservation["selectedPodUid"]
                   for r in roles if isinstance(r,dict)) != 1
            or any(not isinstance(r,dict) or set(r)!=keys or r["role"] not in ("server","client")
                   or type(r["restartCount"]) is not int or r["restartCount"] < 0
                   or re.fullmatch(UUID,str(r["podUid"])) is None or re.fullmatch(UUID,str(r["bootId"])) is None
                   or re.fullmatch(r"net:\[[0-9]+\]",str(r["netns"])) is None for r in roles)):
        raise PolicyBlocked("Invalid detached reservation roles")
    selected=next(r for r in roles if r["role"]==reservation["selectedRole"]
                  and r["podUid"]==reservation["selectedPodUid"])
    if (selected["bootId"] != reservation["selectedBootId"] or selected["bootId"] != reservation["clockBootId"]
            or any(r["bootId"] != reservation["clockBootId"] for r in roles)
            or selected["netns"] != reservation["selectedNetns"]):
        raise PolicyBlocked("Detached selected role epoch changed")
    jobs=reservation["jobs"]
    job_keys={"key","endpoint","port","ueIp","tun","direction","offeredKbps","duration",
              "uePod","ueUid","receiverPod","receiverUid"}
    if not isinstance(jobs,list) or not 1 <= len(jobs) <= 100:
        raise PolicyBlocked("Invalid detached jobs")
    seen_keys=set();seen_ports=set();manifest=[];selected_jobs=0
    for job in jobs:
        if not isinstance(job,dict) or set(job)!=job_keys:
            raise PolicyBlocked("Invalid detached job schema")
        try:
            valid=(re.fullmatch(r"u[0-4][0-9]-(?:ul|dl)",str(job["key"])) is not None
                   and str(ipaddress.IPv4Address(job["endpoint"])).startswith("172.22.6")
                   and str(ipaddress.IPv4Address(job["ueIp"])).startswith("10.10")
                   and re.fullmatch(r"(?:uesimtun|ueTun)[0-9]+",str(job["tun"])) is not None
                   and job["direction"] in ("ul","dl") and type(job["port"]) is int
                   and 5200 <= job["port"] <= 6199 and type(job["offeredKbps"]) is int
                   and 1 <= job["offeredKbps"] <= 10000 and job["duration"] == 18
                   and re.fullmatch(r"cv-ue-g[0-9]+-[0-9]+",str(job["uePod"])) is not None
                   and re.fullmatch(UUID,str(job["ueUid"])) is not None
                   and re.fullmatch(r"n6-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+",str(job["receiverPod"])) is not None
                   and re.fullmatch(UUID,str(job["receiverUid"])) is not None)
        except ValueError:
            valid=False
        if not valid or job["key"] in seen_keys or job["port"] in seen_ports:
            raise PolicyBlocked("Invalid detached job identity")
        seen_keys.add(job["key"]);seen_ports.add(job["port"])
        owner=(job["receiverPod"],job["receiverUid"]) if reservation["selectedRole"]=="server" else (job["uePod"],job["ueUid"])
        selected_jobs += owner==(selected["pod"],selected["podUid"])
        manifest.append(f'{job["key"]}\t{job["endpoint"]}\t{job["port"]}\t{job["ueIp"]}\t{job["tun"]}\t{job["direction"]}\t{job["offeredKbps"]}\t{reservation["profile"]}\n')
    manifest_text = "".join(manifest)
    if tuned:
        manifest_text = tuning_manifest(manifest_text, tuning)
    if selected_jobs < 1 or hashlib.sha256(manifest_text.encode()).hexdigest()!=reservation["manifestSha256"]:
        raise PolicyBlocked("Detached job manifest mismatch")
    raw=json.dumps(reservation,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>65536: raise PolicyBlocked("Oversized detached reservation")
    return ["bash","-c",TUNED_SCRIPT if tuned else SCRIPT,"cityverse-detached",action,base64.b64encode(raw).decode(),str(epoch)]
