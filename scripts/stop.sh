#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws kubectl helm

update_kubeconfig

helm uninstall "$UERANSIM_RELEASE" --namespace "$FREE5GC_NAMESPACE" --ignore-not-found || true
helm uninstall "$HELM_RELEASE" --namespace "$FREE5GC_NAMESPACE" --ignore-not-found || true
kubectl delete namespace "$FREE5GC_NAMESPACE" --ignore-not-found
kubectl delete -f "$ROOT_DIR/k8s/gtp5g-installer.yaml" --ignore-not-found

cat <<EOF
Runtime stopped.
AWS infrastructure is still present. Use scripts/destroy.sh to remove Terraform-managed AWS resources.
EOF
