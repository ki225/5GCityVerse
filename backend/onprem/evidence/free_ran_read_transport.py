"""Fixed read-only UE log/address/ping commands for the explicit local WSL profile."""
import ipaddress
import json
import math
import re
import shlex
import subprocess
import time

from backend.onprem.runtime.ue import EvidenceUnavailable
from backend.onprem.evidence.read_diagnostics import diagnosed, stage, failure
from backend.onprem.traffic.transports.cri_read import (
    CRI_BINARY, ENVIRONMENT_KEY as READ_ENVIRONMENT_KEY, configured_read_transport,
)

ENVIRONMENT_KEY = 'CITYVERSE_ONPREM_UE_READ_TRANSPORT'


def ping_command(bindings, fanout):
    if (type(fanout) is not int or not 1 <= fanout <= 64
            or not isinstance(bindings, dict) or not 1 <= len(bindings) <= 250):
        raise EvidenceUnavailable('Invalid fixed UE ping batch')
    script = ['set -eu']
    for index, (supi, value) in enumerate(bindings.items()):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise EvidenceUnavailable('Invalid fixed UE ping binding')
        interface, _smf, endpoint = value
        if (not isinstance(supi, str) or re.fullmatch(r'imsi-[0-9]{15}', supi) is None
                or not isinstance(interface, str) or re.fullmatch(r'ueTun(?:0|[1-9][0-9]{0,2})', interface) is None):
            raise EvidenceUnavailable('Invalid fixed UE ping identity')
        try:
            if str(ipaddress.IPv4Address(endpoint)) != endpoint:
                raise ValueError
        except (TypeError, ValueError):
            raise EvidenceUnavailable('Invalid fixed UE ping endpoint') from None
        script.append(f"(if ping -n -I {shlex.quote(interface)} -c 1 -W 2 {shlex.quote(endpoint)} >/dev/null 2>&1; "
                      f"then code=0; else code=1; fi; printf '%s\\t%s\\t%s\\n' "
                      f"{shlex.quote(supi)} {shlex.quote(interface)} \"$code\") &")
        if (index + 1) % fanout == 0:
            script.append('wait')
    script.append('wait')
    return ['sh', '-c', '\n'.join(script)]


class LocalCriUeReadTransport:
    def __init__(self, driver, runner):
        self.driver, self.runner = driver, runner

    def _epoch(self, pod):
        meta, spec, status = pod.get('metadata', {}), pod.get('spec', {}), pod.get('status', {})
        statuses = [c for c in status.get('containerStatuses', []) if c.get('name') == 'ue']
        specs = [c for c in spec.get('containers', []) if c.get('name') == 'ue']
        if (meta.get('namespace') != self.driver.namespace or meta.get('deletionTimestamp')
                or meta.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'
                or re.fullmatch(r'cv-ue-g[1-9][0-9]*-[0-9]+', str(meta.get('name'))) is None
                or not isinstance(meta.get('uid'), str) or not meta['uid']
                or status.get('phase') != 'Running' or len(statuses) != 1 or len(specs) != 1
                or statuses[0].get('ready') is not True
                or type(statuses[0].get('restartCount')) is not int
                or statuses[0]['restartCount'] < 0
                or specs[0].get('image') != self.driver.image
                or re.fullmatch(r'containerd://[0-9a-f]{64}', str(statuses[0].get('containerID'))) is None
                or not statuses[0].get('imageID', '').endswith('@'+self.driver.image.rsplit('@', 1)[-1])):
            raise EvidenceUnavailable('Local UE read container identity unavailable')
        return (meta['name'], meta['uid'], statuses[0]['containerID'], statuses[0]['restartCount'],
                specs[0]['image'], meta.get('annotations', {}).get('cityverse.io/supis'))

    def _pod(self, name, deadline=None):
        if deadline is not None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise EvidenceUnavailable('Local UE read deadline expired')
            metadata = getattr(self.driver, '_metadata_pod_reader', None)
            if metadata is not None:
                return metadata.pod(name, deadline=deadline)
            result = self.driver.runner(['kubectl', '--context', self.driver.context,
                '--namespace', self.driver.namespace, 'get', 'pod', name, '-o', 'json'],
                text=True, capture_output=True, timeout=remaining, check=False)
            if result.returncode or len(result.stdout.encode()) > 4*1024*1024:
                raise EvidenceUnavailable('Local UE read identity unavailable')
            return json.loads(result.stdout)
        reader = getattr(self.driver, 'read_pod', None)
        return reader(name) if callable(reader) else self.driver.kubectl('get', 'pod', name, '-o', 'json')

    @diagnosed('ue_read')
    def execute(self, kind, pod, *, timeout, bindings=None, fanout=64):
        if (kind not in {'logs', 'addresses', 'ping', 'rtt'} or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise EvidenceUnavailable('Invalid bounded local UE read')
        if kind not in {'ping', 'rtt'} and bindings is not None:
            raise EvidenceUnavailable('Unexpected local UE read arguments')
        if kind != 'logs' and timeout > 12:
            raise EvidenceUnavailable('Local UE exec read exceeds original bound')
        if kind == 'rtt':
            from backend.onprem.evidence.rtt import rtt_command
            command = rtt_command(bindings, fanout)
        else:
            command = ping_command(bindings, fanout) if kind == 'ping' else ['ip', '-j', 'addr', 'show']
        deadline = time.monotonic()+timeout
        stage('epoch_preflight', kind=kind, deadline=deadline)
        epoch = self._epoch(pod)
        if self._epoch(self._pod(epoch[0], deadline=deadline)) != epoch:
            raise EvidenceUnavailable('Local UE read preflight identity changed')
        cid = epoch[2][len('containerd://'):]
        base = [CRI_BINARY.as_posix(), '--runtime-endpoint', 'unix:///run/containerd/containerd.sock']
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise EvidenceUnavailable('Local UE read deadline expired')
        if kind == 'logs':
            argv = [*base, 'logs', cid]
        else:
            if kind in {'ping', 'rtt'}:
                owned = json.loads(epoch[5])
                if any(supi not in owned or value[0] != 'ueTun'+str(owned.index(supi))
                       for supi, value in bindings.items()):
                    raise EvidenceUnavailable('Local UE ping ownership changed')
            argv = [*base, 'exec', '--sync', '--timeout', str(math.ceil(remaining)), cid, *command]
        stage('dispatch')
        try:
            result = self.runner(argv, capture_output=True, text=False, shell=False,
                                 timeout=remaining, check=False)
        except subprocess.TimeoutExpired as error:
            failure(error)
            if kind == 'rtt':
                raise TimeoutError('rtt.transport_deadline') from None
            raise EvidenceUnavailable('Local UE read dispatch failed; no fallback') from None
        except OSError as error:
            failure(error)
            raise EvidenceUnavailable('Local UE read dispatch failed; no fallback') from None
        stage('output_bound', returnCode=result.returncode,
              outputBytes=len(result.stdout) if isinstance(result.stdout, bytes) else -1)
        if (type(result.returncode) is not int or result.returncode != 0
                or not isinstance(result.stdout, bytes) or not isinstance(result.stderr, bytes)
                or len(result.stderr) > (4*1024*1024 if kind == 'logs' else 1024)
                or len(result.stdout) > 4*1024*1024+2):
            raise EvidenceUnavailable('Local UE read failed or exceeded output bound')
        wire = result.stdout
        if kind == 'logs':
            # crictl preserves container stdout/stderr as separate host streams;
            # Go UE diagnostics use stderr. kubectl logs returns both. Completion
            # checks count per-SUPI records and do not depend on stream ordering.
            wire += result.stderr
        else:
            stage('wire_frame')
            if not wire.endswith(b'\n\n'):
                raise EvidenceUnavailable('Local UE read framing invalid')
            wire = wire[:-2]  # Installed ExecSync CLI appends LF for each remote stream.
        if len(wire) > 4*1024*1024:
            raise EvidenceUnavailable('Local UE read exceeded output bound')
        stage('utf8_decode')
        try:
            output = wire.decode('utf-8')
        except UnicodeError:
            raise EvidenceUnavailable('Local UE read encoding invalid') from None
        stage('epoch_postflight')
        if self._epoch(self._pod(epoch[0], deadline=deadline)) != epoch:
            raise EvidenceUnavailable('Local UE read postflight identity changed')
        stage('deadline')
        if time.monotonic() >= deadline:
            raise EvidenceUnavailable('Local UE read deadline expired')
        return output


def configured_ue_read_transport(environ, driver):
    mode = environ.get(ENVIRONMENT_KEY, 'kubectl')
    if mode == 'kubectl':
        return None
    if mode != 'local-cri' or getattr(driver, 'backend', None) != 'free-ran-ue':
        raise EvidenceUnavailable('Invalid local UE read profile')
    checked = configured_read_transport({**environ, READ_ENVIRONMENT_KEY: 'local-cri'}, driver,
        detached_enabled=environ.get('CITYVERSE_ONPREM_DETACHED_BATCH_ENABLED', '').lower() == 'true')
    return LocalCriUeReadTransport(driver, checked.runner)
