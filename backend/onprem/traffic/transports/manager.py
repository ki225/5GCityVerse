"""Persistent, exact-Pod HTTP control. No exec or automatic write retry."""
from __future__ import annotations

from collections import OrderedDict
import base64
import hashlib
from http.client import HTTPConnection
import ipaddress
import json
import logging
import re
import threading
import time

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.runtime.traffic_manager import LEGACY_TRAFFIC_IMAGE, PORT
from backend.onprem.traffic.worker import command as worker_command


def _discard_connection(entry):
    connection, entry[1] = entry[1], None
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass  # Disposal cannot replace the original request failure.


class _TokenLocks:
    """Only active holders/waiters exist; register before waiting, release last."""
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def acquire(self, key, deadline):
        with self._lock:
            gate = self._entries.get(key)
            if gate is None:
                if len(self._entries) >= 256:
                    raise PolicyBlocked('Traffic manager token capacity exhausted')
                gate = [threading.Lock(), 0]
                self._entries[key] = gate
            gate[1] += 1

        def unref():
            with self._lock:
                gate[1] -= 1
                if gate[1] == 0 and self._entries.get(key) is gate:
                    del self._entries[key]

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not gate[0].acquire(timeout=remaining):
                raise TimeoutError('Traffic manager request deadline')
        except BaseException:
            unref()
            raise

        def release():
            gate[0].release()
            unref()
        return release


class _MutationPool:
    """Four reserved connection seats for independent tokens in one Pod epoch."""
    def __init__(self):
        self.entries = [[threading.Lock(), None, 0.0, 0] for _ in range(4)]
        self.users = 0  # Transport lock protects holders AND token/lane waiters.
        self._condition = threading.Condition()
        self._available = list(range(4))
        self._closing = False

    def closing(self, value):
        with self._condition:
            self._closing = value
            self._condition.notify_all()

    def acquire(self, deadline):
        with self._condition:
            while True:
                if self._closing:
                    raise PolicyBlocked('Traffic manager transport is closing')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Traffic manager request deadline')
                if self._available:
                    return self._available.pop(0)
                self._condition.wait(timeout=remaining)

    def release(self, index):
        with self._condition:
            self._available.append(index)
            self._condition.notify()


class TrafficManagerTransport:
    def __init__(self, *, connection_factory=HTTPConnection, idle_timeout_seconds=60, failure_capture=None):
        # The Pod server closes idle connections after 120 seconds. Retire
        # cached connections earlier, before sending any new operation.
        if type(idle_timeout_seconds) not in (int, float) or not 0 < idle_timeout_seconds <= 60:
            raise ValueError('Traffic manager client idle timeout must be in (0, 60] seconds')
        self._factory = connection_factory
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._connections = OrderedDict()
        self._mutation_pools = {}
        self._token_locks = _TokenLocks()
        self._active_requests = 0
        self._closing = False
        self.failure_capture = failure_capture

    def _evict_idle(self, key, needed):
        """Under the transport lock, retire complete idle pools or plain lanes."""
        for old in list(self._connections):
            if old not in self._connections:
                continue
            if not ((old[0] == key[0] and old[:5] != key)
                    or len(self._connections) + needed > 128):
                continue
            if isinstance(old[-1], tuple):
                pool = self._mutation_pools[old[:5]]
                if pool.users:
                    continue
                # A pool with zero users has no waiters or leased seats.
                for index, entry in enumerate(pool.entries):
                    _discard_connection(entry)
                    del self._connections[old[:5] + (('mutation', index),)]
                del self._mutation_pools[old[:5]]
            else:
                entry = self._connections[old]
                if entry[3]:
                    continue
                _discard_connection(entry)
                del self._connections[old]

    def _lease_connection(self, key, lane, token, deadline, diagnostic):
        """Pin before waiting so eviction/close cannot replace a waiter's mutex."""
        pool = None
        entry = None
        with self._condition:
            if self._closing:
                raise PolicyBlocked('Traffic manager transport is closing')
            if token is not None:
                pool = self._mutation_pools.get(key)
                self._evict_idle(key, 0 if pool is not None else 4)
                if pool is None:
                    if len(self._connections) + 4 > 128:
                        raise PolicyBlocked('Traffic manager connection capacity exhausted')
                    pool = _MutationPool()
                    self._mutation_pools[key] = pool
                    for index, seat in enumerate(pool.entries):
                        self._connections[key + (('mutation', index),)] = seat
                pool.users += 1
            else:
                cache_key = key + (lane,)
                entry = self._connections.get(cache_key)
                self._evict_idle(key, 0 if entry is not None else 1)
                if entry is None:
                    if len(self._connections) >= 128:
                        raise PolicyBlocked('Traffic manager connection capacity exhausted')
                    entry = [threading.Lock(), None, 0.0, 0]
                    self._connections[cache_key] = entry
                entry[3] += 1
                self._connections.move_to_end(cache_key)
            self._active_requests += 1

        def unpin():
            with self._condition:
                if pool is not None:
                    pool.users -= 1
                else:
                    entry[3] -= 1
                self._active_requests -= 1
                self._condition.notify_all()

        release_token, index, locked = None, None, False
        try:
            if pool is not None:
                diagnostic.update(phase='mutation_token_wait', reason='deadline')
                release_token = self._token_locks.acquire(key + (token,), deadline)
                diagnostic.update(phase='mutation_lane_wait', reason='deadline')
                index = pool.acquire(deadline)
                entry = pool.entries[index]
                locked = entry[0].acquire(blocking=False)
                if not locked:
                    raise PolicyBlocked('Traffic manager connection seat unavailable')
                with self._condition:
                    self._connections.move_to_end(key + (('mutation', index),))
            else:
                diagnostic.update(phase='lane_wait', reason='deadline')
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not entry[0].acquire(timeout=remaining):
                    raise TimeoutError('Traffic manager request deadline')
                locked = True
                with self._condition:
                    if self._closing:
                        raise PolicyBlocked('Traffic manager transport is closing')
        except BaseException:
            if locked:
                entry[0].release()
            if index is not None:
                pool.release(index)
            if release_token is not None:
                release_token()
            unpin()
            raise

        def release():
            try:
                entry[0].release()
                if pool is not None:
                    pool.release(index)
                    release_token()
            finally:
                unpin()
        return entry, release

    def execute_batch(self, argv, pod, *, timeout, maximum):
        return self.execute('batch', pod, timeout=timeout, maximum=maximum, _batch_argv=argv)

    def absence(self, pod):
        return self.execute('absence', pod, timeout=5, maximum=128)

    def execute(self, action, pod, *, reservation=None, epoch=0, timeout, maximum, _batch_argv=None):
        diagnostic = {'phase': 'command', 'reason': 'command', 'httpStatus': None,
                      'responseBytes': None, 'receivedBytes': 0, 'errorType': None}
        started = time.monotonic()
        request_started_at = time.time() if self.failure_capture is not None else None
        deadline = started + timeout if type(timeout) in {int, float} else started
        try:
            if type(timeout) not in {int, float} or not 0 < timeout <= (52 if _batch_argv is not None else 10):
                raise PolicyBlocked('Traffic manager identity or bounds invalid')
            for attempt in range(2):
                try:
                    return self._execute(action, pod, reservation=reservation, epoch=epoch,
                        timeout=max(.001, deadline-time.monotonic()), maximum=maximum,
                        _batch_argv=_batch_argv, _diagnostic=diagnostic)
                except PolicyBlocked:
                    # These two commands only observe the same exact reservation.
                    # A transient manager refusal is confirmed once inside the
                    # original budget; every new request rechecks the Pod epoch.
                    remaining = deadline-time.monotonic()
                    if (attempt or _batch_argv is not None or action not in {'status', 'capability'}
                            or diagnostic.get('httpStatus') != 409 or remaining <= .05):
                        raise
                    logging.getLogger(__name__).warning(
                        'Traffic manager read confirmation action=%s priorHttpStatus=409 attempt=2', action)
                    time.sleep(.025)
                    diagnostic.update(phase='command', reason='command', httpStatus=None,
                                      responseBytes=None, receivedBytes=0, errorType=None)
        except Exception as error:
            target = diagnostic.get('captureTarget')
            if self.failure_capture is not None and target is not None:
                try:
                    self.failure_capture.submit_nowait({**target,
                        'requestStartedAt': request_started_at, 'failureAt': time.time()})
                except Exception:
                    pass  # Background diagnostics cannot mask the control error.
            # Failure-only telemetry must not replace the original exception,
            # serialize remote values, or accidentally expose the bearer token.
            known = {'PolicyBlocked', 'TimeoutError', 'RemoteDisconnected', 'ConnectionRefusedError',
                     'ConnectionResetError', 'BrokenPipeError', 'IncompleteRead', 'OSError',
                     'UnicodeDecodeError', 'JSONDecodeError', 'ValueError', 'TypeError', 'KeyError'}
            category = diagnostic['errorType'] or type(error).__name__
            control = (_batch_argv[4] if isinstance(_batch_argv, list) and len(_batch_argv) > 4 else action)
            control = control if type(control) is str and control in {'identity', 'absence', 'capability', 'start', 'status',
                'release', 'collect', 'cancel', 'server', 'client', 'ready', 'client_ready'} else 'unknown'
            role = reservation.get('selectedRole') if isinstance(reservation, dict) else None
            role = role if type(role) is str and role in {'client', 'server'} else 'unknown'
            try:
                logging.getLogger(__name__).warning(
                    'Traffic manager request failure phase=%s reasonCode=%s errorType=%s '
                    'httpStatus=%s responseBytes=%s receivedBytes=%s elapsedSeconds=%.6f action=%s role=%s',
                    diagnostic['phase'], 'timeout' if isinstance(error, TimeoutError) else diagnostic['reason'],
                    category if category in known else 'other', diagnostic['httpStatus'],
                    diagnostic['responseBytes'], diagnostic['receivedBytes'], time.monotonic() - started,
                    control, role)
            except Exception:
                pass
            raise

    def _execute(self, action, pod, *, reservation, epoch, timeout, maximum,
                 _batch_argv, _diagnostic):
        # Always revalidate using the same closed builder as the exec path.
        if _batch_argv is None:
            argv = worker_command('identity' if action == 'absence' else action, reservation, epoch=epoch)
        else:
            from backend.onprem.traffic.protocol import (WORKER_SCRIPT, FREE_RAN_WORKER_SCRIPT,
                SHARD_WORKER_SCRIPT, FREE_RAN_SHARD_WORKER_SCRIPT)
            argv = _batch_argv
            if (action != 'batch' or not isinstance(argv, list) or len(argv) != 8
                    or argv[:2] != ['bash', '-c'] or argv[3] != 'cityverse-batch'
                    or argv[2] not in {WORKER_SCRIPT, FREE_RAN_WORKER_SCRIPT, SHARD_WORKER_SCRIPT, FREE_RAN_SHARD_WORKER_SCRIPT}
                    or argv[4] not in {'server','client','ready','client_ready','release','cancel','status'}
                    or not re.fullmatch(r'[a-f0-9]{32}', str(argv[5]))
                    or not re.fullmatch(r'[0-9]{1,15}', str(argv[6]))):
                raise PolicyBlocked('Traffic manager batch command is not immutable')
        _diagnostic.update(phase='metadata', reason='metadata')
        meta = pod.get('metadata', {})
        containers = [c for c in pod.get('spec', {}).get('containers', []) if c.get('name') == 'traffic']
        statuses = [s for s in pod.get('status', {}).get('containerStatuses', []) if s.get('name') == 'traffic']
        if len(containers) != 1 or len(statuses) != 1:
            raise PolicyBlocked('Traffic manager container is ambiguous')
        container, status = containers[0], statuses[0]
        auth = [e.get('value') for e in container.get('env', [])
                if e.get('name') == 'CITYVERSE_TRAFFIC_MANAGER_TOKEN']
        try:
            address = ipaddress.ip_address(pod['status']['podIP'])
        except (ValueError, KeyError, TypeError):
            raise PolicyBlocked('Traffic manager Pod address unavailable') from None
        if (address.is_unspecified or address.is_multicast or address.is_loopback
                or len(auth) != 1 or not isinstance(auth[0], str)
                or not re.fullmatch(r'[a-f0-9]{64}', auth[0])
                or container.get('image') == LEGACY_TRAFFIC_IMAGE
                or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', str(container.get('image')))
                or container.get('command') != ['/usr/local/bin/cityverse-traffic-manager']
                or status.get('ready') is not True
                or not re.fullmatch(r'containerd://[a-f0-9]{64}', str(status.get('containerID')))
                or type(status.get('restartCount')) is not int
                or type(timeout) not in {int, float} or not 0 < timeout <= (52 if _batch_argv is not None else 10)
                or type(maximum) is not int or not 0 < maximum <= 7*1024*1024+512):
            raise PolicyBlocked('Traffic manager identity or bounds invalid')
        if reservation is not None:
            _diagnostic.update(phase='reservation', reason='reservation_epoch')
            selected = next(r for r in reservation['roles']
                            if r['podUid'] == reservation['selectedPodUid'] and r['role'] == reservation['selectedRole'])
            if (selected['podUid'] != meta.get('uid') or selected['pod'] != meta.get('name')
                    or selected['namespace'] != meta.get('namespace')
                    or selected['containerID'] != status['containerID']
                    or selected['restartCount'] != status['restartCount']):
                raise PolicyBlocked('Traffic manager reservation epoch changed')
        key = (meta.get('uid'), status['containerID'], status['restartCount'], str(address), auth[0])
        deadline = time.monotonic() + timeout
        _diagnostic.update(phase='connection_pool', reason='connection_capacity')
        # Read lane and legacy batch lanes retain their existing semantics.
        control = argv[4] if _batch_argv is not None else action
        lane = (2 if control in {'server','client'} else
                0 if control in {'identity','absence','status','ready','client_ready'} else 1)
        token = None
        if (_batch_argv is None and reservation is not None
                and control in {'capability','start','release','collect','cancel'}):
            # Bind serialization to the closed command bytes, not a caller's
            # mutable reservation after the validated builder returned.
            matches = re.findall(rb'"token":"([a-f0-9]{32})"',
                                 base64.b64decode(argv[-2], validate=True))
            if len(matches) != 1:
                raise PolicyBlocked('Traffic manager command token is ambiguous')
            token = matches[0]
        if self.failure_capture is not None and action == 'capability' and _batch_argv is None:
            try:
                frozen = json.loads(base64.b64decode(argv[-2], validate=True))
                _diagnostic['captureTarget'] = {
                    'roundId': frozen['roundId'], 'generation': frozen['generation'],
                    'role': frozen['selectedRole'], 'namespace': selected['namespace'],
                    'name': selected['pod'], 'uid': selected['podUid'],
                    'containerID': selected['containerID'], 'restartCount': selected['restartCount'],
                    'image': container['image']}
            except Exception:
                pass
        entry, release_connection = self._lease_connection(key, lane, token, deadline, _diagnostic)
        try:
            _diagnostic.update(phase='connection', reason='connection')
            if entry[1] is not None and time.monotonic() - entry[2] >= self._idle_timeout_seconds:
                _discard_connection(entry)
            def budget():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Traffic manager request deadline')
                if entry[1] is not None:
                    entry[1].timeout = remaining
                    if entry[1].sock is not None:
                        entry[1].sock.settimeout(remaining)
                return remaining
            if entry[1] is None:
                entry[1] = self._factory(str(address), PORT, timeout=budget())
            _diagnostic.update(phase='payload_encode', reason='json_encode')
            payload = {'action': action, 'encoded': argv[-2], 'epoch': epoch,
                               'scriptSha256': hashlib.sha256(argv[2].encode()).hexdigest(),
                               'timeout': budget(), 'podUid': meta.get('uid')}
            if _batch_argv is not None:
                payload.update(mode='batch', token=argv[5], action=argv[4], epoch=int(argv[6]), encoded=argv[7])
            body = json.dumps(payload,
                              separators=(',', ':'), allow_nan=False).encode()
            _diagnostic.update(phase='request', reason='connection')
            entry[1].request('POST', '/v1/control', body=body,
                             headers={'Authorization': 'Bearer ' + auth[0], 'Content-Type': 'application/json'})
            budget()
            _diagnostic.update(phase='response_headers', reason='connection')
            response = entry[1].getresponse()
            _diagnostic.update(httpStatus=response.status if type(response.status) is int else None,
                responseBytes=response.length if type(response.length) is int else None)
            if response.status != 200 or response.length is None or response.length > maximum:
                _diagnostic['reason'] = 'http_status' if response.status != 200 else 'response_size'
                raise PolicyBlocked('Traffic manager response rejected')
            output = bytearray()
            _diagnostic.update(phase='response_body', reason='response_incomplete')
            if response.length == 0:
                # HTTPResponse still owns the connection until EOF is consumed,
                # including a successful release/cancel with Content-Length: 0.
                # Otherwise the next request is sent but getresponse raises
                # ResponseNotReady, making native cleanup appear to fail.
                if response.read1(0) != b'':
                    raise PolicyBlocked('Traffic manager empty response invalid')
            while response.length:
                budget()
                chunk = response.read1(min(65536, maximum + 1 - len(output)))
                _diagnostic['receivedBytes'] = len(output) + len(chunk)
                if not chunk or len(output) + len(chunk) > maximum:
                    _diagnostic['reason'] = 'response_incomplete' if not chunk else 'response_size'
                    raise PolicyBlocked('Traffic manager response incomplete or oversized')
                output.extend(chunk)
            budget()
            entry[2] = time.monotonic()
            _diagnostic.update(phase='response_decode', reason='response_encoding')
            return output.decode('ascii')
        except TimeoutError as error:
            _diagnostic['errorType'] = type(error).__name__
            _discard_connection(entry)
            raise TimeoutError('Traffic manager request deadline') from None
        except Exception as error:
            _diagnostic['errorType'] = type(error).__name__
            _discard_connection(entry)
            raise PolicyBlocked('Traffic manager request failed; no replay') from None
        finally:
            release_connection()

    def close(self):
        # Block new admissions and wake lane waiters; drain original calls.
        # Once closed/drained, a later call can open a fresh connection as before.
        with self._condition:
            while self._closing:
                self._condition.wait()
            self._closing = True
            for pool in self._mutation_pools.values():
                pool.closing(True)
            while self._active_requests:
                self._condition.wait()
            entries = list(self._connections.values())
        try:
            for entry in entries:
                _discard_connection(entry)
        finally:
            with self._condition:
                for pool in self._mutation_pools.values():
                    pool.closing(False)
                self._closing = False
                self._condition.notify_all()


def configured_manager_transport(environment, *, detached_enabled):
    image = environment.get('CITYVERSE_ONPREM_TRAFFIC_MANAGER_IMAGE')
    if image is None:
        return None
    if (not detached_enabled or environment.get('CITYVERSE_ONPREM_PROFILE') != 'wsl-kubeadm'
            or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image) or image == LEGACY_TRAFFIC_IMAGE):
        raise PolicyBlocked('Traffic manager requires a pinned image and WSL detached profile')
    return TrafficManagerTransport()
