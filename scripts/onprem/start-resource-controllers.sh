#!/usr/bin/env bash
# REQ-001: Use pinned operator config; REQ-002: foreground single-worker supervisor.
# REQ-003: Loopback only; REQ-004: check-only never imports native controller factories.
set -euo pipefail

usage() {
  echo 'Usage: start-resource-controllers.sh upf|applications CONFIG SHA256 PORT [--check-only]' >&2
}

if [[ $# -lt 4 || $# -gt 5 ]]; then usage; exit 2; fi
kind=$1
config=$2
digest=$3
port=$4
mode=${5:-}
case "$kind" in
  upf) prefix=CITYVERSE_UPF_CONTROLLER; factory=backend.onprem.intents.upf_controller:configured_resource_controller ;;
  applications) prefix=CITYVERSE_APPLICATION_CONTROLLER; factory=backend.onprem.intents.application_controller:configured_application_controller ;;
  *) usage; exit 2 ;;
esac
if [[ -n "$mode" && "$mode" != --check-only ]]; then usage; exit 2; fi
if [[ "$config" != /* || ! "$digest" =~ ^[a-f0-9]{64}$ || ! "$port" =~ ^[0-9]{1,5}$ ]]; then
  echo 'Absolute config, lowercase SHA256, and numeric port required' >&2; exit 2
fi
if (( 10#$port < 1024 || 10#$port > 65535 )); then echo 'Port outside 1024..65535' >&2; exit 2; fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../.." && pwd)
python_bin=${CITYVERSE_PYTHON:-python3}
command -v "$python_bin" >/dev/null || { echo 'Python executable unavailable' >&2; exit 2; }
"$python_bin" - "$config" "$digest" "$kind" <<'PY'
import hashlib, json, pathlib, sys
p = pathlib.Path(sys.argv[1])
if p.is_symlink() or not p.is_file() or p.stat().st_size > 1_048_576:
    raise SystemExit('Bounded regular pinned controller configuration required')
raw = p.read_bytes()
if hashlib.sha256(raw).hexdigest() != sys.argv[2]:
    raise SystemExit('Controller configuration digest mismatch')
c = json.loads(raw)
expected = 'cityverse.upf-controller/v1' if sys.argv[3] == 'upf' else 'cityverse.application-controller/v1'
if not isinstance(c, dict) or c.get('schema') != expected:
    raise SystemExit('Controller configuration schema mismatch')
for key in ('journal', 'controller_token_file'):
    if not isinstance(c.get(key), str) or not pathlib.Path(c[key]).is_absolute():
        raise SystemExit('Absolute controller state and credential paths required')
PY
if [[ "$mode" == --check-only ]]; then echo 'Pinned controller configuration checked; no controller started'; exit 0; fi
export "${prefix}_CONFIG_FILE=$config" "${prefix}_CONFIG_SHA256=$digest"
cd -- "$repo_dir"
exec "$python_bin" -m uvicorn "$factory" --factory --host 127.0.0.1 --port "$port" --workers 1
