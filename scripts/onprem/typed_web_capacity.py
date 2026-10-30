"""Freeze a separate scoped protocol or verify already-captured private receipts.

Freeze performs native GET readbacks only. Build reads existing SQLite/artifacts
only. Neither mode starts UEs, changes grants, installs a policy or acquires the
controller lock. The native/backend owner must run these with its pinned env.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backend.onprem.capacity.typed_web import (CONFIG,MANIFEST,encoded,digest,need,read_pinned,
    make_protocol,runtime_scope)
from backend.onprem.capacity.typed_web_receipts import verify_manifest,released_snapshot,archived_actions,CAPTURE

def collect_artifacts(folder,protocol_sha,capture_directory):
    artifacts={}
    for path in folder.glob('*.json'):
        need(not path.is_symlink() and 0<path.stat().st_size<=16*1024*1024,'scoped_capacity_capture_file_bound')
        raw=path.read_bytes();value=json.loads(raw)
        if value.get('schemaVersion')!=CAPTURE or value.get('protocolSha256')!=protocol_sha:continue
        phase=value.get('phase')
        need(phase!='failure','scoped_capacity_selected_window_failed')
        need(phase in ('opening-native','closing-native','result','cleanup','rendered')
            and value.get('roundId')==folder.name,'scoped_capacity_capture_schema')
        need(phase not in artifacts,'scoped_capacity_duplicate_capture')
        artifacts[phase]=dict(ref=str(path.relative_to(capture_directory)),sha256=hashlib.sha256(raw).hexdigest())
    return artifacts
from backend.onprem.scenarios.profile_request import parse_profile_request

def write(path,value):
    need(path.is_absolute() and not path.exists() and path.parent.is_dir(),'scoped_capacity_new_output_required')
    raw=encoded(value)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as out:out.write(raw)
    return hashlib.sha256(raw).hexdigest()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    freeze=sub.add_parser('freeze')
    for flag in ('request','protocol-output','config-output','capture-directory'):
        freeze.add_argument('--'+flag,type=Path,required=True)
    build=sub.add_parser('build')
    for flag in ('protocol','capture-directory','ledger','output'):
        build.add_argument('--'+flag,type=Path,required=True)
    build.add_argument('--protocol-sha256',required=True);build.add_argument('--round-id',required=True)
    args=parser.parse_args(argv)
    from backend.onprem.runtime.kubeadm import configured_driver
    driver=configured_driver();need(driver is not None,'scoped_capacity_driver_required')
    if args.mode=='freeze':
        from backend.onprem.intents.integration import load_configuration
        from backend.onprem.intents.model_planner import ResourceBudget
        config=load_configuration()
        need(config.get('baseline_device_admission') is True and not config.get('targets')
            and not config.get('capacity_profiles') and config.get('budgets')
            and all(not ResourceBudget(**b).allowed_actions for b in config['budgets'].values()),
            'scoped_capacity_zero_write_configuration_required')
        need(args.capture_directory.is_absolute() and args.capture_directory.is_dir()
            and not args.capture_directory.is_symlink(),'scoped_capacity_private_capture_directory_required')
        # Freeze the same public wire body accepted by the formal API. Counts
        # are derived by its strict parser, never operator-supplied internals.
        body=parse_profile_request(json.loads(args.request.read_bytes()))
        protocol=make_protocol(body,runtime_scope(driver))
        protocol['runtime']['writeAuthorizationSha256']=digest({key:config.get(key) for key in
            ('targets','capacity_profiles','budgets','authority')})
        protocol['runtimeFingerprint']=digest(protocol['runtime'])
        protocol_sha=write(args.protocol_output,protocol)
        config['scoped_traffic_capacity']=dict(schemaVersion=CONFIG,protocolFile=str(args.protocol_output),
            protocolSha256=protocol_sha,captureDirectory=str(args.capture_directory))
        config_sha=write(args.config_output,config)
        print(json.dumps(dict(protocol=str(args.protocol_output),protocol_sha256=protocol_sha,
            configuration=str(args.config_output),configuration_sha256=config_sha,verifiedDevices=0,nativeMutation=False)))
        return
    protocol=read_pinned(args.protocol,args.protocol_sha256)
    folder=args.capture_directory/args.round_id
    artifacts=collect_artifacts(folder,args.protocol_sha256,args.capture_directory)
    manifest=dict(schemaVersion=MANIFEST,protocolSha256=args.protocol_sha256,artifacts=artifacts)
    def read(ref):
        path=(args.capture_directory/ref).resolve(strict=True)
        need(path.is_relative_to(args.capture_directory.resolve()),'scoped_capacity_artifact_path')
        return path.read_bytes()
    runtime=runtime_scope(driver)
    from backend.onprem.intents.integration import load_configuration
    config=load_configuration()
    runtime['writeAuthorizationSha256']=digest({key:config.get(key) for key in ('targets','capacity_profiles','budgets','authority')})
    grant=verify_manifest(manifest,read,protocol,protocol_sha=args.protocol_sha256,runtime=runtime,
        completed_reader=lambda rid:released_snapshot(args.ledger,rid),
        archived_reader=lambda rid,ids:archived_actions(args.ledger,rid,ids))
    sha=write(args.output,manifest)
    print(json.dumps(dict(manifest=str(args.output),manifest_sha256=sha,scope='typed-web100-only',
        verifiedDevices=grant.verified_devices,validUntil=grant.valid_until,activated=False,nativeMutation=False)))

if __name__=='__main__':main()
