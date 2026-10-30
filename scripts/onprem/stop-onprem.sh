#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_bin=${ONPREM_PYTHON:-python3}
command -v "$python_bin" >/dev/null || { printf 'Missing Linux Python: %s\n' "$python_bin" >&2; exit 1; }
exec "$python_bin" "$script_dir/service_launcher.py" stop "$@"
