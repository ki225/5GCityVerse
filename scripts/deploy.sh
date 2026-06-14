#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws terraform kubectl helm git npm python3

terraform -chdir="$TF_DIR" init
terraform -chdir="$TF_DIR" apply -auto-approve

EKS_CLUSTER_NAME="$(tf_output eks_cluster_name)"
API_URL="$(tf_output api_gateway_url)"
WS_URL="$(tf_output ws_endpoint)"
FRONTEND_BUCKET="$(tf_output frontend_bucket)"
CLOUDFRONT_ID="$(tf_output cloudfront_distribution_id)"
FRONTEND_URL="$(tf_output frontend_url)"

update_kubeconfig
sync_free5gc_chart
install_gtp5g
install_free5gc
install_ueransim
install_real_simulation_assets

FREE5GC_WEBUI_URL="$(free5gc_webui_url)"
FREE5GC_WEBUI_URL="$FREE5GC_WEBUI_URL" python3 "$ROOT_DIR/scripts/seed-subscribers.py"

(
  cd "$ROOT_DIR/frontend"
  npm ci
  VITE_API_URL="${API_URL%/}" VITE_WS_URL="$WS_URL" npm run build
)

aws_cli s3 sync "$ROOT_DIR/frontend/dist" "s3://${FRONTEND_BUCKET}" --delete
aws_cli cloudfront create-invalidation --distribution-id "$CLOUDFRONT_ID" --paths '/*' >/dev/null

cat <<EOF
Deployment complete.
Frontend: ${FRONTEND_URL}
API: ${API_URL}
WebSocket: ${WS_URL}
free5GC WebUI: ${FREE5GC_WEBUI_URL}
free5GC login: admin / free5gc
UERANSIM release: ${UERANSIM_RELEASE}
EOF
