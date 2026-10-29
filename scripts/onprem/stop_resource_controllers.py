"""Stop only exact recorded project controller groups; retain state and journals."""
import argparse
import json
import os
from pathlib import Path
import time

from service_launcher import identity, stop_record, write_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path, workspace = args.state_file, args.workspace
    if not path.is_absolute() or path.is_symlink() or not workspace.is_absolute():
        parser.error('Absolute regular state path and workspace required')
    workspace = workspace.resolve(strict=True)
    if not path.exists():
        print('No controller state; nothing to stop')
        return
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
        parser.error('State must be owned by the invoking user and not group/world writable')
    state = json.loads(path.read_text())
    records = []
    for kind, module in [('upf', 'upf_controller:configured_resource_controller'),
                         ('applications', 'application_controller:configured_application_controller')]:
        record = state.get(kind)
        if not record:
            continue
        expected = {key: record[key] for key in ('pid', 'pgrp', 'start_ticks', 'boot_id')}
        current = identity(record['pid'])
        if current is None:
            continue
        if current != expected or expected['pid'] != expected['pgrp']:
            raise SystemExit('Controller PID/boot/start-time identity changed; refusing stop')
        proc = Path('/proc') / str(record['pid'])
        argv = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if (str((proc / 'cwd').resolve()) != str(workspace)
                or 'backend.onprem.intents.' + module not in argv
                or '--port' not in argv or argv[argv.index('--port') + 1] != str(record['port'])):
            raise SystemExit('Controller command/workspace/port identity changed; refusing stop')
        records.append(expected)
    print('Verified controller groups:', [row['pid'] for row in records])
    if args.apply:
        for record in records:
            stop_record(record)
        state['status'] = 'stopped'
        state['stopped_at'] = time.time()
        write_state(path, state)
        print('Stopped owned resource controllers; configuration, tokens and journals retained')
    else:
        print('Read-only check; add --apply to stop')


if __name__ == '__main__':
    main()
