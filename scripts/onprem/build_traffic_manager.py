"""Export exact worker scripts and build a static Linux manager (no downloads)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

IPERF_SOURCE_SHA256 = 'dd289b6700d3bc33eda7fa3ce6db217d6ca42239edbcb2e7f152bf7bf5c8a5aa'


def build_iperf3(source, directory):
    """Build pinned ESnet 3.21 with listener and accounting-clock patches."""
    if os.name != 'posix':
        raise RuntimeError('Build the optional static iperf3 binary on Linux/WSL')
    source = source.resolve()
    if hashlib.sha256(source.read_bytes()).hexdigest() != IPERF_SOURCE_SHA256:
        raise ValueError('Expected the pinned ESnet iperf 3.21 source archive')
    patch = directory / 'patches/iperf3-3.21-single-udp-listener.patch'
    clock_patch = directory / 'patches/iperf3-3.21-accounting-clock.patch'
    cache = ROOT / '.tmp/traffic-manager-build'
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='iperf3-', dir=cache) as temporary:
        work = Path(temporary)
        with tarfile.open(source) as archive:
            if any(not (work / m.name).resolve().is_relative_to(work) for m in archive.getmembers()):
                raise ValueError('Unsafe source archive path')
            archive.extractall(work, filter='data')
        project = work / 'iperf-3.21'
        with (cache / 'iperf3-build.log').open('w') as log:
            commands = [
                ['patch', '--fuzz=0', '-p1', '-i', str(patch)],
                ['patch', '--fuzz=0', '-p1', '-i', str(clock_patch)],
                ['./bootstrap.sh'],
                ['./configure', '--disable-shared', '--enable-static-bin', '--without-openssl', '--disable-profiling'],
                ['make', '-j4'],
            ]
            for command in commands:
                subprocess.run(command, cwd=project, stdout=log, stderr=log, check=True, timeout=180)
        binary = project / 'src/iperf3'
        kind = subprocess.run(['file', str(binary)], capture_output=True, text=True, check=True, timeout=5).stdout
        if 'statically linked' not in kind and 'static-pie linked' not in kind:
            raise RuntimeError('iperf3 must run without host libc dependencies')
        version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, check=True, timeout=5).stdout
        shutil.copy2(binary, directory / 'iperf3')
    receipt = {'sourceUrl': 'https://codeload.github.com/esnet/iperf/tar.gz/refs/tags/3.21',
               'sourceSha256': IPERF_SOURCE_SHA256,
               'patchSha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
               'accountingClockPatchSha256': hashlib.sha256(clock_patch.read_bytes()).hexdigest(),
               'binarySha256': hashlib.sha256((directory / 'iperf3').read_bytes()).hexdigest(),
               'version': version}
    (cache / 'iperf3-build.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return receipt


def export_scripts(directory=None):
    from backend.onprem.traffic.worker import SCRIPT, TUNED_SCRIPT, TUNED_CAPABILITY_SCRIPT
    from backend.onprem.traffic.protocol import (WORKER_SCRIPT, FREE_RAN_WORKER_SCRIPT,
        SHARD_WORKER_SCRIPT, FREE_RAN_SHARD_WORKER_SCRIPT)
    directory = directory or ROOT / 'backend/onprem/traffic_manager'
    target = directory / 'scripts'
    target.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, source in [('legacy', SCRIPT), ('tuned', TUNED_SCRIPT), ('batch', WORKER_SCRIPT),
                         ('batch-free-ran', FREE_RAN_WORKER_SCRIPT), ('batch-shard', SHARD_WORKER_SCRIPT),
                         ('batch-shard-free-ran', FREE_RAN_SHARD_WORKER_SCRIPT),
                         ('tuned-capability', TUNED_CAPABILITY_SCRIPT)]:
        raw = source.encode('utf-8')
        (target / (name + '.sh')).write_bytes(raw)
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--iperf3-source', type=Path,
                        help='Optional pinned ESnet 3.21 tarball; builds the Linux single-listener binary')
    args = parser.parse_args()
    directory = ROOT / 'backend/onprem/traffic_manager'
    digests = export_scripts(directory)
    if not args.export_only:
        cache = ROOT / '.tmp/traffic-manager-build'
        cache.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, GOOS='linux', GOARCH='amd64', CGO_ENABLED='0',
                           GOCACHE=str(cache / 'cache'), GOTMPDIR=str(cache), GOTOOLCHAIN='local')
        subprocess.run(['go', 'build', '-trimpath', '-ldflags=-s -w',
                        '-o', 'cityverse-traffic-manager', '.'], cwd=directory,
                       env=environment, check=True)
    result = {'scripts': digests, 'built': not args.export_only}
    if args.iperf3_source:
        result['iperf3'] = build_iperf3(args.iperf3_source, directory)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
