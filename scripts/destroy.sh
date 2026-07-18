#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws terraform kubectl helm

"$SCRIPT_DIR/stop.sh" || true
PLAN_DIR="${ROOT_DIR}/artifacts/terraform-plans"
mkdir -p "$PLAN_DIR"
PLAN_FILE="${PLAN_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-destroy.tfplan"
terraform -chdir="$TF_DIR" plan -destroy -lock-timeout=10m -out="$PLAN_FILE"
terraform -chdir="$TF_DIR" show -no-color "$PLAN_FILE" | tee "${PLAN_FILE}.txt"
read -r -p "Review complete. Type 'destroy' to apply this exact saved plan: " answer
[[ "$answer" == "destroy" ]] || { echo "Destroy cancelled; plan retained at ${PLAN_FILE}." >&2; exit 1; }
terraform -chdir="$TF_DIR" apply -lock-timeout=10m "$PLAN_FILE"

echo "Terraform-managed AWS resources destroyed."
