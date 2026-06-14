#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws terraform kubectl helm

"$SCRIPT_DIR/stop.sh" || true
terraform -chdir="$TF_DIR" destroy -auto-approve

echo "Terraform-managed AWS resources destroyed."
