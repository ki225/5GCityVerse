#!/usr/bin/env python3
"""Compile pinned controller configurations; does not launch or change any NF."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.onprem.intents.resource_controller_commissioning import compile_controller_configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--native-inputs', required=True, type=Path)
    parser.add_argument('--upf-config', required=True, type=Path)
    parser.add_argument('--intent-config', required=True, type=Path)
    parser.add_argument('--state-directory', required=True, type=Path)
    args = parser.parse_args()
    if not args.state_directory.is_absolute() or args.state_directory.exists():
        parser.error('A new absolute operator state directory is required; existing configuration is never overwritten')
    read = lambda p: json.loads(p.read_text(encoding='utf-8'))
    inventory = read(args.inventory)
    compiled = compile_controller_configuration(inventory, read(args.native_inputs), read(args.upf_config),
        read(args.intent_config), state_directory=str(args.state_directory),
        collector_source=(ROOT/'backend/onprem/integrations/upf.py').read_text(encoding='utf-8'))
    args.state_directory.mkdir(parents=True, mode=0o700)
    files = {}
    for key in ('upf_controller', 'application_controller', 'provider_fragment', 'smf_environment'):
        path = args.state_directory / (key.replace('_', '-')+'.json')
        raw = (json.dumps(compiled[key], indent=2)+'\n').encode()
        with path.open('xb') as stream: stream.write(raw)
        os.chmod(path, 0o600)
        files[key] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    inventory['resources'].extend(compiled['resources'])
    path = args.state_directory/'inventory.json'
    path.write_text(json.dumps(inventory, indent=2)+'\n', encoding='utf-8'); os.chmod(path, 0o600)
    files['inventory'] = {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    (args.state_directory/'compiled-files.json').write_text(json.dumps(files,indent=2)+'\n', encoding='utf-8')
    print(json.dumps(files, indent=2))


if __name__ == '__main__': main()
