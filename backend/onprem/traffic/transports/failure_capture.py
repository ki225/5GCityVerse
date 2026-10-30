"""Best-effort diagnostic text projection; never native or cleanup authority."""
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
from logging import Formatter, LogRecord
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import re
import threading
import time
from uuid import uuid4


_FIELDS = frozenset(('roundId', 'generation', 'namespace', 'name', 'uid',
                     'containerID', 'restartCount', 'image', 'role', 'requestStartedAt', 'failureAt'))
_UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_STAMP = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z'
_LINE = re.compile(r'(' + _STAMP + r') (?:[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} )?'
                   r'traffic control action=capability phase=(spawn|wait|complete) '
                   r'elapsedSeconds=([0-9]{1,3}\.[0-9]{6})'
                   r'(?: exitCode=(-1|[0-9]{1,3}) deadline=(true|false))?')


def pod_epoch(pod):
    """Return only the same traffic epoch; deliberately ignore env/credentials."""
    meta = pod['metadata']
    containers = [x for x in pod['spec']['containers'] if x.get('name') == 'traffic']
    statuses = [x for x in pod['status']['containerStatuses'] if x.get('name') == 'traffic']
    if len(containers) != 1 or len(statuses) != 1:
        raise ValueError()
    status, container = statuses[0], containers[0]
    if type(status['restartCount']) is not int or status['restartCount'] < 0:
        raise ValueError()
    return {k: meta[k] for k in ('namespace', 'name', 'uid')} | {
        'containerID': status['containerID'], 'restartCount': status['restartCount'],
        'image': container['image']}


class _QuietRotation(RotatingFileHandler):
    def handleError(self, record):
        # logging's default error handler can print a traceback/path to stderr.
        raise OSError('Diagnostic sink unavailable') from None


class ManagerFailureCapture:
    """One daemon, four pending descriptors, a submit-to-finish 1s budget."""
    def __init__(self, reader, path):
        self.reader, self.path = reader, Path(path)
        self._condition = threading.Condition()
        self._queue, self._pending = deque(), set()
        self._accepting, self._thread = True, None
        self._counts = dict.fromkeys(('submitted', 'written', 'dropped', 'expired',
                                     'notAvailable', 'failed', 'rejected'), 0)
        self._sink = None

    def _count(self, name, count=1):
        self._counts[name] = min((1 << 63) - 1, self._counts[name] + count)

    @staticmethod
    def _descriptor(value):
        if type(value) is not dict or set(value) != _FIELDS:
            raise ValueError()
        if (not re.fullmatch(_UUID, value['roundId']) or not re.fullmatch(_UUID, value['uid'])
                or type(value['generation']) is not int or not 0 < value['generation'] < (1 << 63)
                or value['role'] not in {'client', 'server'}
                or type(value['restartCount']) is not int or not 0 <= value['restartCount'] < (1 << 31)
                or not re.fullmatch(r'containerd://[a-f0-9]{64}', value['containerID'])
                or not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', value['image'])
                or len(value['image']) > 512
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value['namespace'])
                or len(value['name']) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x)
                       for x in value['name'].split('.'))):
            raise ValueError()
        for key in ('requestStartedAt', 'failureAt'):
            if type(value[key]) not in {int, float} or not math.isfinite(value[key]) or not 0 <= value[key] <= 1e11:
                raise ValueError()
        if value['requestStartedAt'] > value['failureAt'] or len(json.dumps(value).encode()) > 2048:
            raise ValueError()
        return dict(value)

    def submit_nowait(self, value):
        """No file/network access or waits for the diagnostic job on this path."""
        submitted = time.monotonic()
        try:
            job = self._descriptor(value)
            key = tuple(job[k] for k in ('roundId', 'uid', 'containerID', 'restartCount', 'role'))
        except Exception:
            with self._condition:
                self._count('rejected')
            return False
        with self._condition:
            if not self._accepting or len(self._queue) >= 4 or key in self._pending:
                self._count('dropped')
                return False
            job.update(deadline=submitted + 1., captureId=uuid4().hex, key=key)
            self._queue.append(job)
            self._pending.add(key)
            self._count('submitted')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='manager-failure-capture', daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._queue.pop()
                    self._pending.remove(key)
                    self._count('failed')
                    return False
            self._condition.notify()
        return True

    def status(self):
        with self._condition:
            return {**self._counts, 'pending': len(self._pending), 'accepting': self._accepting,
                    'workerAlive': self._thread is not None and self._thread.is_alive()}

    @staticmethod
    def _budget(job):
        if time.monotonic() >= job['deadline']:
            raise TimeoutError()

    @staticmethod
    def _events(raw, job):
        if type(raw) is not bytes or len(raw) > 65536:
            raise ValueError()
        events, dropped = [], 0
        for line in raw.splitlines(keepends=True):
            ManagerFailureCapture._budget(job)
            # Never join a truncated line into an event.
            if not line.endswith(b'\n'):
                dropped += 1
                continue
            try:
                text = line[:-1].decode('ascii')
                match = _LINE.fullmatch(text)
                if match is None:
                    raise ValueError()
                stamp, phase, seconds, code, deadline = match.groups()
                at = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
                elapsed = float(seconds)
                if (not job['requestStartedAt'] - 1 <= at <= job['failureAt'] + 1
                        or not 0 <= elapsed <= 60 or (phase == 'wait') != (code is not None)
                        or code is not None and not -1 <= int(code) <= 255):
                    raise ValueError()
                event = {'timestamp': stamp, 'phase': phase, 'elapsedSeconds': elapsed}
                if code is not None:
                    event.update(exitCode=int(code), deadline=deadline == 'true')
                if len(events) < 64:
                    events.append(event)
                else:
                    dropped += 1
            except (ValueError, UnicodeError, OverflowError):
                dropped += 1
        return events, dropped

    def _capture(self, job):
        expected = {k: job[k] for k in ('namespace', 'name', 'uid', 'containerID', 'restartCount', 'image')}
        record = {'schemaVersion': 'cityverse.manager-failure-capture/v1',
                  'roundId': job['roundId'], 'generation': job['generation'],
                  'captureId': job['captureId'], 'scopeSha256': hashlib.sha256(
                      json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                  'action': 'capability', 'role': job['role'],
                  'requestStartedAt': job['requestStartedAt'], 'failureAt': job['failureAt'],
                  'authority': False, 'status': 'not_available', 'reasonCode': 'diagnostic_deadline'}
        try:
            self._budget(job)
            record['reasonCode'] = 'pre_epoch_unverified'
            if pod_epoch(self.reader.pod(job['name'], deadline=job['deadline'])) != expected:
                raise ValueError()
            self._budget(job)
            record['reasonCode'] = 'read_unavailable'
            since = datetime.fromtimestamp(max(0., job['requestStartedAt'] - 1), timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            raw = self.reader.traffic_logs(job['name'], deadline=job['deadline'], since_time=since)
            self._budget(job)
            record['reasonCode'] = 'post_epoch_unverified'
            if pod_epoch(self.reader.pod(job['name'], deadline=job['deadline'])) != expected:
                raise ValueError()
            self._budget(job)
            events, dropped = self._events(raw, job)
            self._budget(job)
            record.update(status='captured' if events else 'not_available',
                          reasonCode='same_epoch' if events else 'no_matching_records',
                          events=events, droppedLineCount=dropped,
                          correlation='ambiguous' if (sum(x['phase'] == 'spawn' for x in events) > 1
                              or sum(x['phase'] in {'wait', 'complete'} for x in events) > 1)
                              else 'same_container_action_time')
        except TimeoutError:
            record['reasonCode'] = 'diagnostic_deadline'
        except Exception:
            pass  # Keep the fixed stage; never serialize exception or remote text.
        if time.monotonic() >= job['deadline']:
            record.update(status='not_available', reasonCode='diagnostic_deadline')
            record.pop('events', None)
        record['captureFinishedAt'] = time.time()
        return record

    def _write(self, record):
        text = json.dumps(record, separators=(',', ':'), allow_nan=False)
        if len(text.encode()) > 16384:
            raise ValueError()
        if self._sink is None:
            self._sink = _QuietRotation(self.path, maxBytes=256 * 1024, backupCount=1,
                                        encoding='utf-8', delay=True)
            self._sink.setFormatter(Formatter('%(message)s'))
        self._sink.handle(LogRecord('manager.failure_capture', 20, '', 0, text, (), None))

    def _run(self):
        try:
            while True:
                with self._condition:
                    while self._accepting and not self._queue:
                        self._condition.wait()
                    if not self._queue:
                        return
                    job = self._queue.popleft()
                try:
                    record = self._capture(job)
                    self._write(record)
                    with self._condition:
                        self._count('written')
                        if record['status'] != 'captured': self._count('notAvailable')
                        if record['reasonCode'] == 'diagnostic_deadline': self._count('expired')
                except Exception:
                    with self._condition:
                        self._count('failed')
                finally:
                    with self._condition:
                        self._pending.discard(job['key'])
                        self._condition.notify_all()
        finally:
            try:
                self.reader.close()
            except Exception:
                pass
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass

    def close(self, timeout=2.):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 2:
            raise ValueError()
        with self._condition:
            self._accepting = False
            self._count('dropped', len(self._queue))
            while self._queue:
                self._pending.discard(self._queue.popleft()['key'])
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='manager-failure-capture', daemon=True)
                self._thread.start()
            self._condition.notify_all()
            thread = self._thread
        thread.join(timeout)
        state = self.status()
        return {**state, 'drained': not state['workerAlive'] and state['pending'] == 0}
