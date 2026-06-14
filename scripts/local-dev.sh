#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd python3 npm

LOCAL_BACKEND_PORT="${LOCAL_BACKEND_PORT:-8090}"
export AWS_PROFILE
export LOCAL_BACKEND_PORT
export FREE5GC_WEBUI_URL="${FREE5GC_WEBUI_URL:-$(free5gc_webui_url 2>/dev/null || true)}"

python3 "$ROOT_DIR/backend/local-dev/server.py" &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

(
  cd "$ROOT_DIR/frontend"
  npm ci
  VITE_API_URL="http://127.0.0.1:${LOCAL_BACKEND_PORT}/api" \
    VITE_WS_URL="ws://127.0.0.1:${LOCAL_BACKEND_PORT}/ws" \
    npm run dev -- --host 127.0.0.1
)
