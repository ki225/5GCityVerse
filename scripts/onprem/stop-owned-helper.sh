#!/usr/bin/env bash
# REQ-P1: Explicit PID identity + exact project script, never process-name kill.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_bin=${ONPREM_PYTHON:-python3}
"$python_bin" - "$script_dir" "$@" <<'PY'
import argparse,pathlib,sys
sys.path.insert(0,sys.argv.pop(1))
from service_launcher import identity,stop_record
p=argparse.ArgumentParser(description='Stop an exact recorded project Python helper')
p.add_argument('--pid',type=int,required=True);p.add_argument('--start-ticks',required=True)
p.add_argument('--boot-id',required=True);p.add_argument('--workspace',type=pathlib.Path,required=True)
p.add_argument('--script',type=pathlib.Path,required=True);p.add_argument('--apply',action='store_true')
a=p.parse_args();workspace=a.workspace.resolve(strict=True);script=a.script.resolve(strict=True)
assert a.workspace.is_absolute() and a.script.is_absolute() and script.is_relative_to(workspace)
assert script.suffix=='.py' and not any(x in script.name.lower() for x in ('chrome','driver','playwright')),'Browser helpers require their own control channel'
expected={'pid':a.pid,'pgrp':a.pid,'start_ticks':a.start_ticks,'boot_id':a.boot_id}
current=identity(a.pid)
if current is None:print('Recorded helper already stopped');raise SystemExit(0)
assert current==expected,'Helper process identity changed'
proc=pathlib.Path('/proc')/str(a.pid)
argv=(proc/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
assert str(script) in argv and (proc/'cwd').resolve()==workspace,'Helper command or workspace changed'
print('Verified helper',a.pid,str(script))
if a.apply:stop_record(expected);print('Owned helper stopped')
else:print('Read-only verification; add --apply to stop')
PY
