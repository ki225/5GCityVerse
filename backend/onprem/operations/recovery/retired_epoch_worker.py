"""Fixed exceptional closure primitive, not wired into normal batch recovery.

The authenticated provider validates runtime proof; this worker checks the
preserved files and its own namespace. It never signals or creates traffic.
"""
import base64

from backend.onprem.operations.recovery.retired_epoch import canonical

WORKER = r'''#!/usr/bin/env bash
set -euo pipefail
usage() { printf '%s\n' 'Usage: close_retired_epoch TOKEN NAMESPACE BOOT EXPIRY FILES DIRS RECEIPT' >&2; }
[[ $# == 7 ]] || { usage; exit 2; }
token=$1; namespace=$2; boot=$3; expiry=$4
[[ $token =~ ^[a-f0-9]{32}$ && $namespace =~ ^pid:\[[1-9][0-9]{0,19}\]$ && $boot =~ ^[a-f0-9-]{36}$ && $expiry =~ ^[1-9][0-9]{0,10}$ ]] || exit 2
for dependency in base64 sha256sum stat readlink flock sync date cmp; do command -v "$dependency" >/dev/null || exit 5; done
[[ $(readlink /proc/self/ns/pid) == "$namespace" && $(</proc/sys/kernel/random/boot_id) == "$boot" ]] || exit 4
directory=/tmp/cityverse-batch-$token
[[ -d $directory && ! -L $directory && ! -L /tmp ]] || exit 4
[[ ! -L $directory/control.lock && -f $directory/control.lock ]] || exit 4
exec 9< "$directory/control.lock"
flock -x -w 2 9 || exit 4
receipt=$(printf '%s' "$7" | base64 -d)
[[ ${#receipt} -le 4096 && -n $receipt ]] || exit 4
replay=0
if [[ -e $directory/closed || -L $directory/closed ]]; then
  [[ -d $directory/closed && ! -L $directory/closed && -f $directory/retired-epoch-closure.json && ! -L $directory/retired-epoch-closure.json ]] || exit 4
  cmp -s "$directory/retired-epoch-closure.json" <(printf '%s' "$receipt") || exit 4
  replay=1
fi
if (( replay == 0 )); then
  [[ ! -e $directory/retired-epoch-claim && ! -L $directory/retired-epoch-claim && ! -e $directory/retired-epoch-closure.json && ! -L $directory/retired-epoch-closure.json ]] || exit 4
  (( $(date +%s) < expiry )) || exit 4
fi
declare -A expected=() directories=()
manifest=$(printf '%s' "$5" | base64 -d)
while IFS=$'\t' read -r name hash extra; do
  [[ $name =~ ^(owner|children|keys|manifest|control\.lock|u[0-4][0-9]-(ul|dl)\.(json|meta|err|armed|started))$ && $hash =~ ^[a-f0-9]{64}$ && -z $extra && ! ${expected[$name]+present} ]] || exit 4
  expected[$name]=$hash
done <<< "$manifest"
(( ${#expected[@]} >= 4 && ${#expected[@]} <= 600 )) || exit 4
for name in owner children keys manifest; do [[ ${expected[$name]+present} ]] || exit 4; done
[[ $6 == none || $6 == cancelling ]] || exit 4
if [[ $6 == cancelling ]]; then directories[cancelling]=1; fi
shopt -s nullglob dotglob
count=0; dircount=0
for path in "$directory"/*; do
  name=${path##*/}
  [[ ! -L $path ]] || exit 4
  if (( replay == 1 )); then
    if [[ $name == closed || $name == retired-epoch-claim ]]; then
      [[ -d $path ]] || exit 4
      contents=("$path"/*); (( ${#contents[@]} == 0 )) || exit 4
      continue
    elif [[ $name == retired-epoch-closure.json ]]; then
      [[ -f $path && $(stat -c %h "$path") == 1 ]] || exit 4
      continue
    fi
  fi
  if [[ -d $path ]]; then
    [[ ${directories[$name]+present} ]] || exit 4
    contents=("$path"/*); (( ${#contents[@]} == 0 )) || exit 4
    dircount=$((dircount+1))
  else
    [[ -f $path && ${expected[$name]+present} && $(stat -c %h "$path") == 1 && $(stat -c %s "$path") -le 33554432 ]] || exit 4
    actual=$(sha256sum -- "$path"); [[ ${actual%% *} == "${expected[$name]}" ]] || exit 4
    count=$((count+1))
  fi
done
(( count == ${#expected[@]} && dircount == ${#directories[@]} )) || exit 4
if (( replay == 1 )); then printf '%s\n' "$receipt"; exit 0; fi
(( $(date +%s) < expiry )) || exit 4
# Permanent ambiguous claim on crash: never silently resume a partial write.
mkdir "$directory/retired-epoch-claim"
sync -f "$directory"
(set -o noclobber; printf '%s' "$receipt" > "$directory/retired-epoch-closure.json")
sync -f "$directory/retired-epoch-closure.json"
mkdir "$directory/closed"
sync -f "$directory"
printf '%s\n' "$receipt"
'''


def command(approval, receipt):
    """Only feed output of trusted-proof validation; no public argument builder."""
    files = '\n'.join(f'{name}\t{sha}' for name, sha in sorted(approval['files'].items())).encode()
    return ['bash', '-c', WORKER, 'cityverse-close-retired-epoch', approval['token'],
            approval['newPidNamespace'], approval['nodeBootId'], str(int(approval['expiresAt'])),
            base64.b64encode(files).decode(), 'cancelling' if approval['directories'] else 'none',
            base64.b64encode(canonical(receipt)).decode()]
