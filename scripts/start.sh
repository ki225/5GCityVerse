#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws terraform kubectl helm git python3 node npm

EKS_CLUSTER_NAME="$(tf_output eks_cluster_name)"
log "Building frontend dist"
ensure_frontend_dependencies
(
  cd "$ROOT_DIR/frontend"
  npm run build
)
update_kubeconfig
sync_free5gc_chart
install_multus
install_gtp5g
install_free5gc
install_ueransim
install_real_simulation_assets
FREE5GC_WEBUI_URL="$(free5gc_webui_url)" python3 "$ROOT_DIR/scripts/seed-subscribers.py"
API_URL="$(tf_output api_gateway_url)"
API_URL="${API_URL%/}" python3 "$ROOT_DIR/scripts/verify-baseline-mmtc.py"

cat <<EOF
System started.
Frontend: $(tf_output frontend_url)
API: ${API_URL}
WebSocket: $(tf_output ws_endpoint)
free5GC WebUI: $(free5gc_webui_url)
free5GC login: admin / free5gc
UERANSIM release: ${UERANSIM_RELEASE}
EOF
