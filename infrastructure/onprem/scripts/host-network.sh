#!/usr/bin/env bash
set -euo pipefail
# Single-node simulation only; physical deployments provision real VLANs.
if [[ ${1:-} == --help ]]; then
  printf 'Usage: sudo bash %s --apply  (single-node dummy N2/N3/N4/N6 networks only)\n' "${0##*/}"
  exit 0
fi
[[ ${1:-} == --apply && $# == 1 && $EUID == 0 ]] || { printf 'Root and explicit --apply are required\n' >&2; exit 2; }
[[ -d /sys/module/gtp5g && -d /sys/module/sctp ]] || { printf 'Load compatible gtp5g and SCTP modules first\n' >&2; exit 1; }
for name in n2 n4 n3-embb n3-urllc n3-mmtc n3-v2x n6-embb n6-urllc n6-mmtc n6-v2x; do
  parent=cv-$name
  if ! ip link show "$parent" >/dev/null 2>&1; then
    ip link add "$parent" type dummy
  fi
  ip -details link show "$parent" | grep -q dummy || { printf 'Refuse a non-dummy parent: %s\n' "$parent" >&2; exit 1; }
  ip link set "$parent" up
done
for pair in embb:61 urllc:62 mmtc:63 v2x:64; do
  slice=${pair%%:*}
  octet=${pair##*:}
  gateway=cv-gw-$slice
  if ! ip link show "$gateway" >/dev/null 2>&1; then
    ip link add link "cv-n6-$slice" name "$gateway" type ipvlan mode l2
  fi
  ip addr replace "172.22.$octet.1/24" dev "$gateway"
  ip link set "$gateway" up
done
sysctl -w net.ipv4.ip_forward=1
# Docker's FORWARD policy can otherwise block the lab's primary bridge CNI.
if iptables -nL DOCKER-USER >/dev/null 2>&1; then
  for direction in -i -o; do
    iptables -C DOCKER-USER "$direction" cni0 -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER "$direction" cni0 -j ACCEPT
  done
fi
