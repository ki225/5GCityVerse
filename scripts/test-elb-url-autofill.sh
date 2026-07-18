#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

mock_hostname=""
mock_scheme="internal"
mock_endpoint="10.60.20.42"
mock_type="LoadBalancer"
mock_annotation="internal"

kubectl() {
  local args="$*"
  if [[ "$args" == *"status.loadBalancer.ingress[0].hostname"* ]]; then
    printf '%s' "$mock_hostname"
  elif [[ "$args" == *"{.spec.type}"* ]]; then
    printf '%s' "$mock_type"
  elif [[ "$args" == *"aws-load-balancer-scheme"* ]]; then
    printf '%s' "$mock_annotation"
  elif [[ "$args" == *"aws-load-balancer-internal"* ]]; then
    printf 'true'
  elif [[ "$args" == *"subsets[0].addresses[0].ip"* ]]; then
    printf '%s' "$mock_endpoint"
  else
    return 1
  fi
}

aws_cli() {
  [[ "$*" == *"elbv2 describe-load-balancers"* ]] || return 1
  printf '%s\n' "$mock_scheme"
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  local label="$3"
  if [[ "$expected" != "$actual" ]]; then
    printf 'FAIL %s: expected=%s actual=%s\n' "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

# The first hostname lookup is empty, proving readiness does not accept a
# Service before Kubernetes publishes its LoadBalancer ingress.
if internal_nlb_ready free5gc-webui-private; then
  echo "FAIL readiness accepted an empty ingress hostname" >&2
  exit 1
fi
mock_hostname="a7e70123456789.elb.ap-northeast-1.amazonaws.com"
assert_eq "http://${mock_hostname}:5000" "$(free5gc_webui_url)" "webui URL"

assert_eq "http://${mock_hostname}:8080" "$(free5gc_nef_url)" "NEF URL"

mock_scheme="internet-facing"
if free5gc_webui_url >/dev/null 2>&1; then
  echo "FAIL internet-facing ELB was accepted" >&2
  exit 1
fi

mock_scheme="internal"
mock_hostname="free5gc.invalid"
if free5gc_webui_url >/dev/null 2>&1; then
  echo "FAIL malformed ELB hostname was accepted" >&2
  exit 1
fi

mock_hostname="a7e70123456789.elb.ap-northeast-1.amazonaws.com"
mock_endpoint=""
if free5gc_webui_url >/dev/null 2>&1; then
  echo "FAIL load balancer without a ready Service endpoint was accepted" >&2
  exit 1
fi

echo "ELB URL auto-fill tests passed"
