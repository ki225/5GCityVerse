#!/usr/bin/env bash
# REQ-S1: Explicit state and workspace; REQ-S2: identity-fenced process stop.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_bin=${ONPREM_PYTHON:-python3}
exec "$python_bin" "$script_dir/stop_resource_controllers.py" "$@"
