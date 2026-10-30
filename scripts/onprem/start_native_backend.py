"""Supervise local-only native evidence forwards and the single-worker backend.

Explicit kubeconfig/context only. No provisioning or UE request is performed.
Keep this process running; Ctrl-C terminates its own children and forwards.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import ipaddress
import json
import math
import sqlite3
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # Windows legacy profile has a separate process fence.
    fcntl = None

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.onprem.runtime.traffic_manager import traffic_image
LAB = ROOT / '.cache' / 'kubeadm-lab'
FORWARDS = {
    'amf': ('cityverse-free5gc-amf-service', 8080, 18080),
    'smf': ('smf', 8000, 18081),
    'mongo': ('mongodb', 27017, 27018),
    'webui': ('webui-service', 5000, 15000),
    **{name: (f'upf-{name}-evidence', 9081, 19081 + index)
       for index, name in enumerate(('embb', 'urllc', 'mmtc', 'v2x'))},
}
POLICY_FORWARDS = {
    'pcf': ('cityverse-free5gc-pcf-service', 8080, 18082),
    'callback': ('cityverse-af-callback', 9082, 19092),
}
SMF_INVENTORY_IMAGE = 'docker.io/cityverse/free5gc-smf@sha256:4eb3b03d2c85c3c3f9f7d64a34ea4251c828b043b90d0b70c09710b9d643472a'
UE_IMAGE = 'docker.io/cityverse/free-ran-ue@sha256:1b6d966a0016df5f543f3b132d9ae704946feb87bc79fd5540bde37f6d5af55c'
INIT_IMAGE = 'python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e'
TRAFFIC_IMAGE = traffic_image()
PROFILES = ('legacy-windows', 'wsl-kubeadm')
WSL_PYTHON = Path('/home/kiki/.venvs/5gcityverse-onprem/bin/python')
WSL_RUNTIME = Path('/home/kiki/.local/state/5gcityverse/onprem')
BACKEND_LIFETIME_LOCK = 'backend-lifetime.lock'


def acquire_backend_lifetime_lock(runtime_dir, *, profile):
    """Hold the cooperative WSL writer fence for the launcher's lifetime."""
    if profile != 'wsl-kubeadm':
        return None
    if fcntl is None:
        raise SystemExit('wsl-kubeadm backend lifetime lock requires POSIX flock')
    path = Path(runtime_dir) / BACKEND_LIFETIME_LOCK
    handle = path.open('a+b')
    try:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, PermissionError):
        handle.close()
        raise SystemExit('Offline maintenance owns the backend lifetime lock') from None
    return handle


def validate_smf_inventory_image(image):
    if not isinstance(image, str) or not re.fullmatch(
            r'docker\.io/cityverse/free5gc-smf@sha256:[0-9a-f]{64}', image):
        raise SystemExit('SMF inventory image must be an immutable cityverse/free5gc-smf digest')
    return image


def resolve_runtime_paths(args):
    """Resolve profile defaults while keeping WSL credentials explicit."""
    if args.profile == 'legacy-windows':
        kubeconfig = args.kubeconfig or LAB / 'kubeconfig'
        python = args.python or LAB / 'backend-venv' / 'Scripts' / 'python.exe'
        kubectl = args.kubectl or LAB / 'bin' / 'kubectl.exe'
        checksum = args.kubectl_checksum or kubectl.with_suffix('.exe.sha256')
        runtime_dir = args.runtime_dir or LAB
    else:
        if args.kubeconfig is None:
            raise SystemExit('wsl-kubeadm requires explicit --kubeconfig (use /etc/kubernetes/admin.conf as root or a private dedicated copy)')
        kubeconfig = args.kubeconfig
        python = args.python or WSL_PYTHON
        kubectl = args.kubectl or Path('/usr/bin/kubectl')
        if args.kubectl_checksum is None:
            raise SystemExit('wsl-kubeadm requires explicit --kubectl-checksum')
        checksum = args.kubectl_checksum
        runtime_dir = args.runtime_dir or WSL_RUNTIME
        if hasattr(os, 'geteuid') and os.geteuid() != 0:
            stat = kubeconfig.stat() if kubeconfig.is_file() else None
            if stat is None or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
                raise SystemExit('Non-root WSL kubeconfig must be owned by the current user and mode 0600')
    return kubeconfig, python, kubectl, checksum, runtime_dir


def resolve_cluster_target(args):
    if args.profile == 'wsl-kubeadm' and (args.context is None or args.namespace is None):
        raise SystemExit('wsl-kubeadm requires explicit --context and --namespace')
    return args.context or 'cityverse-kubeadm-lab', args.namespace or 'free5gc-onprem'


def validate_wsl_policy_paths(inventory, quarantine, *, round_db=None):
    """Require existing clear inventory and clear-or-absent quarantine evidence."""
    if inventory is None or quarantine is None:
        raise SystemExit('wsl-kubeadm requires --policy-inventory and --policy-quarantine-registry')
    if not inventory.is_absolute() or not quarantine.is_absolute():
        raise SystemExit('WSL policy inventory and quarantine registry paths must be absolute')
    for parent in (inventory.parent, quarantine.parent):
        if not parent.is_dir() or (os.name != 'nt' and parent.stat().st_mode & 0o077):
            raise SystemExit('WSL policy evidence parent directory must exist and be private')
    path = ROOT / 'backend' / 'onprem' / 'policy' / 'safety.py'
    spec = importlib.util.spec_from_file_location('cityverse_policy_safety', path)
    policy_safety = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy_safety)
    if not inventory.is_file() or not policy_safety.policy_inventory_clear(inventory):
        raise SystemExit('WSL policy inventory is missing or unresolved')
    if not policy_safety.policy_quarantine_clear(quarantine):
        from backend.onprem.validation.restore import restored_cleanup_boot_allowed
        if not restored_cleanup_boot_allowed(round_db, quarantine,
                os.environ.get('CITYVERSE_ONPREM_AI100_CONFIG_FILE'),
                os.environ.get('CITYVERSE_ONPREM_AI100_CONFIG_SHA256')):
            raise SystemExit('WSL policy quarantine registry is unresolved')
    return inventory, quarantine


def apply_wsl_environment(env, policy_paths, runtime_dir):
    env['CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY'] = str(policy_paths[0])
    env['CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY'] = str(policy_paths[1])
    env['CITYVERSE_ONPREM_ROUND_STATE_FILE'] = str(runtime_dir / 'onprem-rounds.sqlite3')
    env['CITYVERSE_LOCAL_STATE_FILE'] = str(runtime_dir / 'cityverse-onprem-state.json')


def verify_wsl_python(python, env, *, free_ran_smoke=False):
    """Check the selected process dependencies without constructing another app."""
    module = 'scripts.onprem.free_ran_device_smoke' if free_ran_smoke else 'backend.onprem.api.server'
    subprocess.check_output([str(python), '-c',
        'import ' + module], cwd=ROOT,
        env=env, text=True, stderr=subprocess.STDOUT, timeout=60)


def verify_cluster_target(command, env, context, namespace):
    """Prove the requested context and namespace before reading workloads."""
    observed = subprocess.check_output(
        command[:3] + ['config', 'get-contexts', context, '-o', 'name'],
        env=env, text=True, timeout=20).strip()
    if observed != context:
        raise SystemExit(f'Explicit kube context unavailable: {context}')
    raw = subprocess.check_output(command + ['get', 'namespace', namespace, '-o', 'json'],
                                  env=env, text=True, timeout=20)
    if json.loads(raw).get('metadata', {}).get('name') != namespace:
        raise SystemExit(f'Explicit namespace unavailable: {namespace}')


def verify_smf_deployment_image(command, env, expected):
    raw = subprocess.check_output(command + ['get', 'deployment', 'smf', '-o', 'json'],
                                  env=env, text=True, timeout=20)
    images = [item.get('image') for item in
              json.loads(raw).get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])]
    if images.count(expected) != 1:
        raise SystemExit('Running SMF deployment does not match the required immutable digest')


def verify_scenario_images_cached(command, env):
    raw = subprocess.check_output(command + ['get', 'nodes', '-o', 'json'],
                                  env=env, text=True, timeout=20)
    cached = {name for node in json.loads(raw).get('items', [])
              for image in node.get('status', {}).get('images', [])
              for name in image.get('names', [])}
    missing = [image for image in (UE_IMAGE, INIT_IMAGE, TRAFFIC_IMAGE)
               if not any(name.endswith('@' + image.split('@', 1)[1]) for name in cached)]
    if missing:
        raise SystemExit('Pinned scenario images are not cached on the WSL node: ' + ', '.join(missing))


def install_posix_sigterm_handler(enabled):
    if not enabled:
        return None
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    return signal.signal(signal.SIGTERM, terminate)


def current_ue_generation(path):
    """Read the latest durable generation without starting a UE controller."""
    uri = path.resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute('SELECT MAX(id) FROM generations').fetchone()
    finally:
        connection.close()
    if not row or type(row[0]) is not int or row[0] < 1:
        raise RuntimeError('No durable UE generation for native observation')
    return row[0]


def read_smf_inventory_token(command, env):
    """Load the lab's approved bearer from Kubernetes without printing it."""
    try:
        raw = subprocess.check_output(command + ['get', 'secret', 'cityverse-smf-inventory', '-o', 'json'],
                                      env=env, text=True, timeout=20)
        encoded = json.loads(raw)['data']['token']
        token = base64.b64decode(encoded, validate=True).decode('ascii')
        if len(token) != 64 or not token.isalnum():
            raise ValueError('invalid token shape')
        return token
    except (KeyError, ValueError, UnicodeError, base64.binascii.Error, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        raise SystemExit('Pinned SMF inventory Secret is unavailable or invalid') from None


def verify_pfcp_associations(kubectl, kubeconfig, context, namespace):
    """Refuse stale SMF association state after any UPF Pod restart."""
    script = ROOT / 'backend' / 'onprem' / 'operations' / 'diagnostics' / 'pfcp_preflight.py'
    raw = subprocess.check_output([sys.executable, str(script), '--kubectl', str(kubectl),
        '--kubeconfig', str(kubeconfig), '--context', context, '--namespace', namespace],
        text=True, encoding='utf-8', timeout=120)
    proof = json.loads(raw)
    if set(proof.get('associations', {})) != {'upf-embb', 'upf-urllc', 'upf-mmtc', 'upf-v2x'}:
        raise RuntimeError('All four current-epoch PFCP associations must be proven')
    return proof


def native_service_targets(services, forwards, namespace):
    """Resolve fixed, namespace-scoped Service VIPs for a native WSL host."""
    targets = {}
    for name, (service, remote, _) in forwards.items():
        matches = [item for item in services['items']
                   if item.get('metadata', {}).get('name') == service
                   and item.get('metadata', {}).get('namespace') == namespace]
        if len(matches) != 1:
            raise ValueError('Native forward requires one namespace-scoped Service')
        spec = matches[0]['spec']
        address = ipaddress.IPv4Address(spec.get('clusterIP', ''))
        if (not address.is_private or address.is_loopback or address.is_unspecified
                or address.is_multicast or spec.get('type') == 'ExternalName'
                or not any(p.get('port') == remote and p.get('protocol', 'TCP') == 'TCP'
                           for p in spec.get('ports', []))):
            raise ValueError('Native forward requires a private TCP ClusterIP endpoint')
        targets[name] = (str(address), remote)
    return targets


def verify_archived_pfcp_proof(path, expected_sha256, before, snapshot_end, all_logs,
                               pfcp_preflight):
    """Validate one operator-pinned proof against the still-current PFCP epoch."""
    if (path is None) != (expected_sha256 is None):
        raise RuntimeError('Archived PFCP proof path and SHA-256 must be supplied together')
    if path is None:
        raise RuntimeError('Current SMF startup PFCP proof is unavailable')
    if not path.is_absolute() or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise RuntimeError('Archived PFCP proof requires an absolute path and lowercase SHA-256')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError('Archived PFCP proof SHA-256 mismatch')
    try:
        archived = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError('Archived PFCP proof is not valid UTF-8 JSON') from None
    rows = archived.get('associations') if isinstance(archived, dict) else None
    if not isinstance(rows, dict) or set(rows) != set(pfcp_preflight.UPFS):
        raise RuntimeError('Archived PFCP proof must contain exactly four associations')
    smf_floor = pfcp_preflight.timestamp(before['smf']['startedAt'])
    snapshot = pfcp_preflight.timestamp(snapshot_end)
    proofs = {}
    for name in pfcp_preflight.UPFS:
        row = rows[name]
        if not isinstance(row, dict) or any(row.get(key) != before[name][key]
                for key in ('pod', 'uid', 'node', 'restartCount', 'startedAt')):
            raise RuntimeError('Archived PFCP proof UPF identity is not current: ' + name)
        upf_floor = pfcp_preflight.timestamp(before[name]['startedAt'])
        try:
            accepted = pfcp_preflight.timestamp(row['acceptedAt'])
            created = pfcp_preflight.timestamp(row['upfCreatedAt'])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('Archived PFCP proof timestamps are invalid: ' + name) from None
        floor = max(smf_floor, upf_floor)
        if not (floor <= accepted <= snapshot and floor <= created <= snapshot):
            raise RuntimeError('Archived PFCP proof is outside the current SMF/UPF epoch: ' + name)
        log = all_logs(name)
        observed = min(accepted, created)
        for line in log.splitlines():
            match = re.match(r'^(\d{4}-\d\d-\d\dT[^\s\x1b]+)', line)
            if not match:
                continue
            at = pfcp_preflight.timestamp(match.group(1))
            if at >= observed and ('not found NodeID 172.22.4.10' in line or
                                   'handleAssociationReleaseRequest' in line):
                raise RuntimeError('Archived PFCP association was subsequently invalidated: ' + name)
        proofs[name] = {**before[name], 'acceptedAt': accepted.isoformat(),
                        'upfCreatedAt': created.isoformat(),
                        'source': 'operator-pinned-archived-proof-with-continuous-upf-logs'}
    return proofs


def verify_pfcp_associations_wsl(command, namespace, env, archived_path=None,
                                 archived_sha256=None):
    """Equivalent current-Pod-epoch PFCP proof without the legacy KVM context fence."""
    path = ROOT / 'backend' / 'onprem' / 'operations' / 'diagnostics' / 'pfcp_preflight.py'
    spec = importlib.util.spec_from_file_location('cityverse_pfcp_preflight', path)
    pfcp_preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pfcp_preflight)
    prefix = command[:5] + ['--request-timeout=10s', '-n', namespace]
    def run(*args, binary=False):
        return subprocess.check_output(prefix + list(args), env=env, text=not binary,
            encoding=None if binary else 'utf-8', timeout=20)
    def pods():
        result = {}
        for pod in json.loads(run('get', 'pods', '-o', 'json'))['items']:
            name = pod['metadata'].get('labels', {}).get('app.kubernetes.io/name')
            if name not in {'smf', *pfcp_preflight.UPFS}:
                continue
            if name in result or pod['metadata'].get('deletionTimestamp'):
                raise RuntimeError('Ambiguous or terminating PFCP workload: ' + name)
            states = [item for item in pod['status'].get('containerStatuses', []) if item['name'] == name]
            if len(states) != 1 or not states[0].get('ready') or 'running' not in states[0]['state']:
                raise RuntimeError('PFCP container not ready: ' + name)
            result[name] = {'pod': pod['metadata']['name'], 'uid': pod['metadata']['uid'],
                'node': pod['spec']['nodeName'], 'restartCount': states[0]['restartCount'],
                'startedAt': states[0]['state']['running']['startedAt']}
        if set(result) != {'smf', *pfcp_preflight.UPFS}:
            raise RuntimeError('Missing expected PFCP workloads')
        return result
    before = pods()
    snapshot_end = pfcp_preflight.datetime.now(pfcp_preflight.timezone.utc).isoformat()
    retained, retained_source = {}, {}
    def all_logs(name):
        if name not in retained:
            path = pfcp_preflight.retained_log_path(namespace, before[name], name)
            listing = run('get', '--raw', path)
            read_file = lambda file: run('get', '--raw', path + file, binary=file.endswith('.gz'))
            try:
                # A complete retained container epoch proves the actual setup
                # exchange even when an idle UPF emits no logs for >15s.
                # Prefer that existing full-startup proof before considering
                # the stricter continuous-SMF-window rotated-log fallback.
                retained[name] = pfcp_preflight.retained_log_text(
                    listing, read_file, before[name]['restartCount'], before[name]['startedAt'])
                retained_source[name] = 'current-container-startup-retained-logs'
            except ValueError as error:
                if name == 'smf' or str(error) != 'Retained logs no longer cover current container startup':
                    raise
                retained[name] = pfcp_preflight.retained_log_text(
                    listing, read_file, before[name]['restartCount'], before[name]['startedAt'],
                    coverage_floor=before['smf']['startedAt'], coverage_end=snapshot_end)
                retained_source[name] = 'continuous-current-smf-epoch-retained-logs'
            if run('get', '--raw', path) != listing:
                raise RuntimeError('Log rotation occurred during association preflight')
        return retained[name]
    smf_log = run('logs', before['smf']['pod'], '-c', 'smf', '--since-time=' + before['smf']['startedAt'])
    proofs = {}
    try:
        for name, address in pfcp_preflight.UPFS.items():
            log = run('logs', before[name]['pod'], '-c', name, '--since-time=' + before[name]['startedAt'])
            try:
                proof = pfcp_preflight.association_proof(
                    smf_log, log, address, before['smf']['startedAt'], before[name]['startedAt'])
            except ValueError as error:
                if 'unproven' not in str(error):
                    raise
                proof = pfcp_preflight.association_proof(
                    all_logs('smf'), all_logs(name), address,
                    before['smf']['startedAt'], before[name]['startedAt'])
                proof['source'] = retained_source[name]
            proofs[name] = {**before[name], **proof}
    except ValueError as error:
        if str(error) != 'Retained logs no longer cover current container startup':
            raise
        proofs = verify_archived_pfcp_proof(
            archived_path, archived_sha256, before, snapshot_end, all_logs, pfcp_preflight)
    if pods() != before:
        raise RuntimeError('PFCP workload identity changed during preflight')
    return {'associations': proofs}


def validate_detached_batch_mode(*, profile, detached, batch, scenario):
    if detached and (profile != 'wsl-kubeadm' or not batch or not scenario):
        raise SystemExit('Detached batch requires the WSL profile with scenario and batch modes')
    return detached is True


def verified_free_ran_smoke_command(args, python):
    """Seal the data file, never accept an executable or shell from a job."""
    if bool(args.free_ran_device_smoke) != bool(args.free_ran_device_smoke_sha256):
        raise SystemExit('FreeRAN smoke job and SHA-256 must be supplied together')
    if not args.free_ran_device_smoke:
        return None
    if any((args.capacity_gates, args.capacity_cleanup_from, args.capacity_observe_output,
            args.capacity_prior_100)):
        raise SystemExit('FreeRAN smoke and capacity modes are mutually exclusive')
    if not (args.enable_mutations and args.enable_traffic and args.enable_smf_inventory):
        raise SystemExit('FreeRAN smoke requires mutations, traffic, and authoritative SMF inventory')
    digest = args.free_ran_device_smoke_sha256
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise SystemExit('FreeRAN smoke requires an exact lowercase SHA-256')
    try:
        path = args.free_ran_device_smoke.resolve(strict=True)
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('hash')
        job = json.loads(raw)
        if not isinstance(job, dict):
            raise ValueError('object')
        # Pure schema validation only. Importing the fixed worker does not
        # create a controller or touch native resources.
        from scripts.onprem.free_ran_device_smoke import validate_job
        validate_job(path, digest)
    except (OSError, ValueError, UnicodeError):
        raise SystemExit('FreeRAN smoke job must be a bounded JSON object with matching SHA-256') from None
    return [str(python), str(ROOT / 'scripts/onprem/free_ran_device_smoke.py'),
            '--job-file', str(path), '--job-sha256', digest]


def launch_pacing_environment(args, environment):
    """Resolve explicit operator settings before any child import or launch."""
    def value(attribute, key, default):
        explicit = getattr(args, attribute, None)
        return explicit if explicit is not None else environment.get(key, default)
    try:
        interval = float(value('ue_batch_interval_seconds', 'CITYVERSE_ONPREM_UE_BATCH_INTERVAL', '30'))
        burst = int(value('typed_ue_launch_burst', 'CITYVERSE_ONPREM_TYPED_UE_LAUNCH_BURST', '1'))
        pending = int(value('typed_ue_max_pending', 'CITYVERSE_ONPREM_TYPED_UE_MAX_PENDING', '8'))
    except (TypeError, ValueError, OverflowError):
        raise SystemExit('Invalid operator UE launch pacing') from None
    if (not math.isfinite(interval) or interval < 2 or not 1 <= burst <= 10
            or not 1 <= pending <= 20 or burst > pending):
        raise SystemExit('UE pacing requires finite interval >=2 seconds, burst 1..10, pending 1..20 and burst <= pending')
    return {'CITYVERSE_ONPREM_UE_BATCH_INTERVAL': format(interval, '.17g'),
            'CITYVERSE_ONPREM_TYPED_UE_LAUNCH_BURST': str(burst),
            'CITYVERSE_ONPREM_TYPED_UE_MAX_PENDING': str(pending)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, default='legacy-windows')
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--context')
    parser.add_argument('--namespace')
    parser.add_argument('--python', type=Path)
    parser.add_argument('--kubectl', type=Path)
    parser.add_argument('--kubectl-checksum', type=Path,
                        help='File containing the expected lowercase SHA-256 of kubectl')
    parser.add_argument('--runtime-dir', type=Path,
                        help='Private directory for mutable SQLite state and launcher logs')
    parser.add_argument('--policy-inventory', type=Path)
    parser.add_argument('--policy-quarantine-registry', type=Path)
    parser.add_argument('--credentials', type=Path, default=LAB / 'groups.json')
    parser.add_argument('--city-values', type=Path,
                        help='Operator Helm values for city gNB geography; enables all-city native RAN observation')
    parser.add_argument('--amf-local-port', type=int, default=18080,
                        help='Loopback AMF port; choose another port if the default is occupied')
    parser.add_argument('--backend-local-port', type=int, default=8000,
                        help='Loopback API listener for normal backend mode only')
    parser.add_argument('--enable-mutations', action='store_true')
    parser.add_argument('--enable-traffic', action='store_true', help='Enable the pinned UE traffic sidecar for future intents; submits no UE request')
    parser.add_argument('--enable-batch-traffic', action='store_true')
    parser.add_argument('--enable-detached-batch', action='store_true',
                        help='Use detached shard supervisors; WSL scenario and batch modes are required')
    parser.add_argument('--native-service-forwards', action='store_true',
                        help='WSL-only loopback TCP relays to actual Service ClusterIPs, avoiding kubectl streaming')
    parser.add_argument('--archived-pfcp-proof', type=Path,
                        help='WSL-only operator-pinned fallback used solely when current SMF startup logs rotated')
    parser.add_argument('--archived-pfcp-proof-sha256',
                        help='Expected lowercase SHA-256 for --archived-pfcp-proof')
    parser.add_argument('--enable-scenario-rounds', action='store_true',
                        help='Enable formal scenario rounds only with mutations, traffic, and batch traffic')
    parser.add_argument('--enable-smf-inventory', action='store_true',
                        help='Use the pinned custom SMF and Secret-backed authoritative context inventory')
    parser.add_argument('--smf-inventory-image', default=SMF_INVENTORY_IMAGE,
                        help='Explicit immutable image digest expected for SMF inventory; defaults to the proven inventory-v2 image')
    parser.add_argument('--policy-forwards', action='store_true', help='Also expose the verified PCF service and callback health/admin transport on loopback; enables no policy action')
    parser.add_argument('--ue-timeout-seconds', type=int, choices=(900, 1800), default=900,
                        help='Bound for a new UE test attempt; never use to relabel or extend an existing failed attempt')
    parser.add_argument('--ue-batch-interval-seconds', type=float,
                        help='Finite launch interval >=2 seconds; overrides operator environment, otherwise default30')
    parser.add_argument('--typed-ue-launch-burst', type=int,
                        help='Typed one-Pod-per-device launch burst1..10; overrides operator environment, otherwise default1')
    parser.add_argument('--typed-ue-max-pending', type=int,
                        help='Maximum pending typed Pods1..20, at least burst; overrides operator environment, otherwise default8')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--capacity-gates', default='',
                        help='Private controller-only UE run: 2, 2-embb-ul, or 100,250,500; never opens a public API')
    parser.add_argument('--capacity-prior-100', type=Path,
                        help='Exact successful and native-cleaned 100 UE run required for a single 500 UE gate')
    parser.add_argument('--capacity-cleanup-from', type=Path,
                        help='Resume guarded cleanup of one failed private capacity evidence directory')
    parser.add_argument('--capacity-observe-output', type=Path,
                        help='Write a read-only native generation snapshot to this exact local JSON path')
    parser.add_argument('--free-ran-device-smoke', type=Path,
                        help='Private fixed FreeRAN V2 verification worker; sealed JSON job, no backend API')
    parser.add_argument('--free-ran-device-smoke-sha256',
                        help='Exact lowercase SHA-256 of --free-ran-device-smoke')
    args = parser.parse_args(argv)
    pacing_environment = launch_pacing_environment(args, os.environ)
    # Reject conflicting modes and unsealed jobs before any native reads or
    # process launches. The worker repeats job validation before mutations.
    verified_free_ran_smoke_command(args, '')
    if args.native_service_forwards and (args.profile != 'wsl-kubeadm' or os.name == 'nt'):
        parser.error('Native Service forwarding requires the native WSL profile')
    if bool(args.archived_pfcp_proof) != bool(args.archived_pfcp_proof_sha256):
        raise SystemExit('Archived PFCP proof path and SHA-256 must be supplied together')
    if args.archived_pfcp_proof and args.profile != 'wsl-kubeadm':
        raise SystemExit('Archived PFCP proof fallback is WSL-only')
    validate_detached_batch_mode(profile=args.profile, detached=args.enable_detached_batch,
                                 batch=args.enable_batch_traffic, scenario=args.enable_scenario_rounds)
    expected_smf_image = validate_smf_inventory_image(args.smf_inventory_image)
    kubeconfig, python, kubectl, checksum, runtime_dir = resolve_runtime_paths(args)
    smoke_run = verified_free_ran_smoke_command(args, python)
    api_mode = not (smoke_run or args.capacity_gates or args.capacity_cleanup_from or args.capacity_observe_output)
    if not 1024 <= args.backend_local_port <= 65535:
        raise SystemExit('Backend loopback port must be in 1024..65535')
    context, namespace = resolve_cluster_target(args)
    policy_paths = (validate_wsl_policy_paths(args.policy_inventory, args.policy_quarantine_registry,
                                            round_db=runtime_dir/'onprem-rounds.sqlite3')
                    if args.profile == 'wsl-kubeadm' else None)
    if bool(args.capacity_prior_100) != (args.capacity_gates == '500'):
        raise SystemExit('Single 500 capacity gate requires --capacity-prior-100; other modes must not supply it')
    if not 1024 <= args.amf_local_port <= 65535 or args.amf_local_port in {
            8000, *(entry[2] for name, entry in FORWARDS.items() if name != 'amf'),
            *(entry[2] for entry in POLICY_FORWARDS.values())}:
        raise SystemExit('AMF loopback port must be distinct and in 1024..65535')
    if args.enable_scenario_rounds and not (
            args.enable_mutations and args.enable_traffic and args.enable_batch_traffic):
        raise SystemExit('Scenario rounds require mutations, traffic, and batch traffic')
    forwards = {**FORWARDS, **(POLICY_FORWARDS if args.policy_forwards else {})}
    forwards['amf'] = (*FORWARDS['amf'][:2], args.amf_local_port)
    if api_mode and args.backend_local_port in {entry[2] for entry in forwards.values()}:
        raise SystemExit('Backend loopback port must be distinct from native forwards')
    for path in (kubeconfig, args.credentials, python, kubectl, checksum):
        if not path.is_file(): raise SystemExit(f'Required file missing: {path}')
    if not runtime_dir.is_dir():
        raise SystemExit(f'Required private runtime directory missing: {runtime_dir}')
    lifetime_lock = acquire_backend_lifetime_lock(runtime_dir, profile=args.profile)
    if args.profile == 'wsl-kubeadm' and runtime_dir.resolve().as_posix().startswith('/mnt/'):
        raise SystemExit('WSL runtime directory must be on the Linux filesystem, not DrvFS')
    if hashlib.sha256(kubectl.read_bytes()).hexdigest() != checksum.read_text().strip().lower():
        raise SystemExit('Pinned kubectl checksum mismatch')
    env = dict(os.environ)
    env.update(pacing_environment)
    # Dependency verification imports the configured driver, including its
    # resource-profile traffic guard. Pin the same explicit flag the backend
    # receives before that import; inherited values must not override the CLI.
    env['CITYVERSE_ONPREM_TRAFFIC_ENABLED'] = str(args.enable_traffic).lower()
    if args.city_values is not None:
        from backend.onprem.runtime.city_catalog import city_registry
        from backend.onprem.runtime.spatial_ran import validate_registry
        env['CITYVERSE_ONPREM_CITY_VALUES'] = str(args.city_values.resolve(strict=True))
        previous = os.environ.get('CITYVERSE_ONPREM_CITY_VALUES')
        try:
            os.environ['CITYVERSE_ONPREM_CITY_VALUES'] = env['CITYVERSE_ONPREM_CITY_VALUES']
            validate_registry(city_registry())
        finally:
            if previous is None:
                os.environ.pop('CITYVERSE_ONPREM_CITY_VALUES', None)
            else:
                os.environ['CITYVERSE_ONPREM_CITY_VALUES'] = previous
    # Inventory is supervisor-only. State files are profile-scoped and WSL
    # assigns its explicit runtime paths below; never inherit another backend's DB.
    for key in ('CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED',
                'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE',
                'CITYVERSE_ONPREM_SMF_INVENTORY_URL',
                'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN',
                'CITYVERSE_ONPREM_ROUND_STATE_FILE', 'CITYVERSE_LOCAL_STATE_FILE'):
        env.pop(key, None)
    env['PATH'] = str(kubectl.parent) + os.pathsep + env.get('PATH', '')
    env['KUBECONFIG'] = str(kubeconfig.resolve())
    if args.profile == 'wsl-kubeadm':
        try:
            verify_wsl_python(python, env, free_ran_smoke=bool(smoke_run))
        except subprocess.TimeoutExpired:
            raise SystemExit('WSL backend import exceeded 60 seconds; check filesystem/import latency') from None
        except subprocess.CalledProcessError:
            raise SystemExit('WSL Python venv is missing required backend dependencies') from None
    command = [str(kubectl), '--kubeconfig', env['KUBECONFIG'], '--context', context, '-n', namespace]
    verify_cluster_target(command, env, context, namespace)
    services = json.loads(subprocess.check_output(command + ['get', 'services', '-o', 'json'], env=env, text=True, timeout=20))
    native_targets = native_service_targets(services, forwards, namespace) if args.native_service_forwards else None
    available = {item['metadata']['name']: {port['port'] for port in item['spec']['ports']} for item in services['items']}
    for service, remote, local in forwards.values():
        if remote not in available.get(service, set()):
            raise SystemExit(f'Required actual service/port absent: {service}:{remote}')
    if args.profile == 'wsl-kubeadm':
        verify_pfcp_associations_wsl(command, namespace, env, args.archived_pfcp_proof,
                                     args.archived_pfcp_proof_sha256)
    else:
        verify_pfcp_associations(kubectl, kubeconfig.resolve(), context, namespace)
    if args.enable_smf_inventory:
        verify_smf_deployment_image(command, env, expected_smf_image)
    if args.enable_scenario_rounds:
        verify_scenario_images_cached(command, env)
    inventory_token = read_smf_inventory_token(command, env) if args.enable_smf_inventory else None
    for local in [*((args.backend_local_port,) if api_mode else ()), *(entry[2] for entry in forwards.values())]:
        with socket.socket() as probe:
            # A recently closed local forward may leave TIME_WAIT sockets.
            # They do not occupy the listening port for the next forward.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: probe.bind(('127.0.0.1', local))
            except OSError: raise SystemExit(f'Loopback port already occupied: {local}') from None
    env.update({
        'CITYVERSE_ONPREM_RUNTIME': 'kubeadm',
        'CITYVERSE_ONPREM_PROFILE': args.profile,
        'CITYVERSE_ONPREM_RAN_BACKEND': 'free-ran-ue',
        'CITYVERSE_ONPREM_EVIDENCE_MODE': 'native',
        'CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED': str(args.enable_mutations).lower(),
        'CITYVERSE_ONPREM_TRAFFIC_ENABLED': str(args.enable_traffic).lower(),
        'CITYVERSE_ONPREM_BATCH_TRAFFIC_ENABLED': str(args.enable_batch_traffic).lower(),
        'CITYVERSE_ONPREM_DETACHED_BATCH_ENABLED': str(args.enable_detached_batch).lower(),
        'CITYVERSE_ONPREM_SCENARIO_ROUNDS_ENABLED': str(args.enable_scenario_rounds).lower(),
        'CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE': UE_IMAGE,
        'CITYVERSE_ONPREM_UE_INIT_IMAGE': INIT_IMAGE,
        'CITYVERSE_ONPREM_UE_TEMPO_MS': '500',
        'CITYVERSE_ONPREM_UE_TIMEOUT': str(args.ue_timeout_seconds),
        'CITYVERSE_ONPREM_KUBE_CONTEXT': context,
        'CITYVERSE_ONPREM_NAMESPACE': namespace,
        'CITYVERSE_ONPREM_AMF_OAM_URL': f'http://127.0.0.1:{args.amf_local_port}',
        'CITYVERSE_ONPREM_SMF_OAM_URL': 'http://127.0.0.1:18081',
        'CITYVERSE_ONPREM_MONGO_URI': 'mongodb://127.0.0.1:27018',
        'CITYVERSE_ONPREM_MONGO_DATABASE': 'free5gc',
        'CITYVERSE_ONPREM_CREDENTIALS_FILE': str(args.credentials.resolve()),
        'CITYVERSE_ONPREM_UE_STATE_FILE': str(runtime_dir / 'ue-state.sqlite3'),
        'CITYVERSE_ONPREM_UPF_EVIDENCE_URLS': json.dumps({name: f'http://127.0.0.1:{FORWARDS[name][2]}' for name in ('embb', 'urllc', 'mmtc', 'v2x')}),
    })
    if policy_paths is not None:
        apply_wsl_environment(env, policy_paths, runtime_dir)
    if inventory_token is not None:
        env.update({
            'CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED': 'true',
            'CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE': expected_smf_image,
            'CITYVERSE_ONPREM_SMF_INVENTORY_URL': 'http://127.0.0.1:18081/nsmf-oam/v1/sm-contexts',
            'CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN': inventory_token,
        })
    if args.check_only:
        print('Pinned client checksum, required files, actual service ports, four current-epoch PFCP associations and loopback ports verified.')
        return
    if sum(bool(value) for value in (args.capacity_gates, args.capacity_cleanup_from, args.capacity_observe_output)) > 1:
        raise SystemExit('Capacity run, cleanup recovery, and observation are mutually exclusive')
    if (args.capacity_gates or args.capacity_cleanup_from) and (not args.enable_mutations or not args.enable_traffic or not args.enable_smf_inventory):
        raise SystemExit('Capacity run requires mutations, traffic, and authoritative SMF inventory')
    children, logs = [], []
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    old_sigterm = install_posix_sigterm_handler(os.name != 'nt')
    try:
        for name, (service, remote, local) in forwards.items():
            log = (runtime_dir / f'forward-{name}.log').open('a', encoding='utf-8'); logs.append(log)
            forward_command = (command + ['port-forward', '--address', '127.0.0.1', f'service/{service}', f'{local}:{remote}']
                               if native_targets is None else [str(python), str(ROOT / 'scripts/onprem/forward_native_service.py'),
                                   '--listen-port', str(local), '--target-ip', native_targets[name][0],
                                   '--target-port', str(native_targets[name][1])])
            children.append(subprocess.Popen(forward_command, env=env, stdout=log, stderr=log, creationflags=flags))
        deadline = time.monotonic() + 45
        for _, _, local in forwards.values():
            while True:
                if any(child.poll() is not None for child in children): raise RuntimeError('A forward exited; inspect private forward logs')
                try:
                    with socket.create_connection(('127.0.0.1', local), timeout=0.25): break
                except OSError:
                    if time.monotonic() > deadline: raise RuntimeError('Forward readiness timeout') from None
                    time.sleep(0.2)
        if smoke_run:
            # The sole controller is this fixed worker. No uvicorn/backend is
            # spawned, so it cannot compete for the sealed runtime state lock.
            smoke_log = (runtime_dir / 'free-ran-device-smoke-worker.log').open('a', encoding='utf-8')
            logs.append(smoke_log)
            worker = subprocess.Popen(smoke_run, cwd=ROOT, env=env, stdout=smoke_log,
                                      stderr=smoke_log, creationflags=flags)
            children.append(worker)
            print('Private FreeRAN device verification worker started; no backend API.', flush=True)
            while worker.poll() is None:
                if any(child.poll() is not None for child in children[:-1]):
                    worker.terminate()
                    raise RuntimeError('A native forward exited during FreeRAN device verification')
                time.sleep(1)
            if worker.returncode:
                raise RuntimeError(f'Private FreeRAN device verification exited {worker.returncode}')
            return
        if args.capacity_gates or args.capacity_cleanup_from or args.capacity_observe_output:
            env['CITYVERSE_ONPREM_UE_TIMEOUT'] = '1800'
            if args.capacity_gates == '500':
                env['CITYVERSE_ONPREM_UE_BATCH_SIZE'] = '250'
            run = [str(python), str(ROOT / 'scripts' / 'onprem' / 'run_ue_capacity_private.py'),
                   '--gates', args.capacity_gates] + (
                   ['--prior-100-run', str(args.capacity_prior_100)] if args.capacity_prior_100 else []) if args.capacity_gates else [
                   str(python), str(ROOT / 'scripts' / 'onprem' / 'run_ue_capacity_private.py'),
                   '--cleanup-from', str(args.capacity_cleanup_from)] if args.capacity_cleanup_from else [
                   str(python), str(ROOT / 'scripts' / 'onprem' / 'observe_ue_native.py'),
                   '--generation', str(current_ue_generation(runtime_dir / 'ue-state.sqlite3')),
                   '--output', str(args.capacity_observe_output)]
            capacity_log = (runtime_dir / 'ue-capacity-worker.log').open('a', encoding='utf-8')
            logs.append(capacity_log)
            worker = subprocess.Popen(run, cwd=ROOT, env=env, stdout=capacity_log, stderr=capacity_log, creationflags=flags)
            children.append(worker)
            print('Private capacity controller started; evidence/DB forwards are loopback-only.', flush=True)
            while worker.poll() is None:
                if any(child.poll() is not None for child in children[:-1]):
                    worker.terminate()
                    raise RuntimeError('A native forward exited during capacity run')
                time.sleep(1)
            if worker.returncode:
                raise RuntimeError(f'Private capacity run exited {worker.returncode}')
            return
        log = (runtime_dir / 'backend.log').open('a', encoding='utf-8'); logs.append(log)
        children.append(subprocess.Popen([str(python), '-m', 'uvicorn', 'backend.onprem.api.server:app', '--host', '127.0.0.1', '--port', str(args.backend_local_port), '--workers', '1'], cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags))
        print(f'Backend launched on 127.0.0.1:{args.backend_local_port}; evidence/DB forwards are loopback-only. No UE intent submitted.', flush=True)
        while all(child.poll() is None for child in children): time.sleep(1)
        raise RuntimeError('A supervised process exited; inspect private logs')
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None: child.terminate()
        for child in children:
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        for log in logs: log.close()
        if old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == '__main__':
    main()
