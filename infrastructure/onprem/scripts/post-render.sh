#!/usr/bin/env bash
set -euo pipefail
# Pinned official AMF has no strategy value; a fixed N2 IP needs Recreate.
chart_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
namespace=${CITYVERSE_NAMESPACE:-free5gc-onprem}
[[ $namespace =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#namespace} -le 63 ]] || exit 2
stage=$(mktemp -d -t cityverse-helm.XXXXXXXX)
cleanup() { [[ $stage == /tmp/cityverse-helm.* && -d $stage ]] && rm -rf -- "$stage"; }
trap cleanup EXIT
cat > "$stage/resources.yaml"
cp "$chart_root/patches/"*.yaml "$stage/"
printf '\nnamespace: %s\n' "$namespace" >> "$stage/kustomization.yaml"
kubectl kustomize "$stage" | python3 "$chart_root/scripts/normalize-node-selectors.py"
