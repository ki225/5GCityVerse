"""Optional bounded HTTP latency evidence; no request bodies, query values or secrets."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import math
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
from uuid import uuid4
from urllib.parse import parse_qs


_ADMISSION_PHASES = ('admission.lock_wait', 'admission.guards', 'admission.capacity_grant',
                     'admission.native_baseline', 'admission.db_reserve')
_ADMISSION_CONTEXT = ContextVar('onprem_admission_timing', default=None)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class _AdmissionTiming:
    """Request-local fixed numeric aggregates, owned by exactly one ASGI task."""
    def __init__(self, started, owner):
        self.started, self.owner = started, owner
        self.closed, self.truncated, self.entries = False, False, {}

    def owns(self):
        return not self.closed and self.owner is not None and _current_task() is self.owner

    def add(self, phase, started, finished, failed, cancelled):
        if not self.owns():
            return
        numbers = ((finished-started)*1000, (started-self.started)*1000,
                   (finished-self.started)*1000)
        if not all(math.isfinite(value) and value >= 0 for value in numbers):
            self.truncated = True
            return
        elapsed, offset, end = numbers
        if any(value > 1e9 for value in numbers):
            self.truncated = True
        elapsed, offset, end = (min(value, 1e9) for value in numbers)
        entry = self.entries.get(phase)
        if entry is None:
            entry = dict(phase=phase, count=0, totalMs=0., minMs=elapsed, maxMs=elapsed,
                         firstStartOffsetMs=offset, lastFinishOffsetMs=end,
                         failedCount=0, cancelledCount=0)
            self.entries[phase] = entry
        if entry['count'] >= 1024:
            self.truncated = True
            return
        entry['count'] += 1
        total = entry['totalMs'] + elapsed
        self.truncated = self.truncated or total > 1e9
        entry['totalMs'] = min(total, 1e9)
        entry['minMs'], entry['maxMs'] = min(entry['minMs'],elapsed), max(entry['maxMs'],elapsed)
        entry['lastFinishOffsetMs'] = end
        entry['failedCount'] += int(failed)
        entry['cancelledCount'] += int(cancelled)

    def close(self):
        self.closed, self.owner = True, None
        rows = [{key:round(value,3) if type(value) is float else value for key,value in
                 self.entries[phase].items()} for phase in _ADMISSION_PHASES if phase in self.entries]
        if len(json.dumps(rows,separators=(',',':'),allow_nan=False).encode()) > 4096:
            rows, self.truncated = [], True
        return dict(admissionSpans=rows, admissionSpansTruncated=self.truncated)


class admission_span:
    """No-op outside the original HTTP task, including inherited child/thread context."""
    def __init__(self, phase):
        self.phase, self.state, self.done = phase, None, False

    def __enter__(self):
        try:
            state = _ADMISSION_CONTEXT.get()
            if (type(self.phase) is str and self.phase in _ADMISSION_PHASES
                    and state is not None and state.owns()):
                self.state, self.started = state, time.perf_counter()
        except Exception:
            pass
        return self

    def finish(self, error_type=None):
        if self.done:
            return
        self.done = True
        try:
            if self.state is not None and self.state.owns():
                cancelled = error_type is not None and issubclass(error_type,asyncio.CancelledError)
                self.state.add(self.phase,self.started,time.perf_counter(),
                               error_type is not None and not cancelled,cancelled)
        except Exception:
            pass  # Diagnostics cannot mask the original error/cancellation.

    def __exit__(self, error_type, error, traceback):
        self.finish(error_type)
        return False


@asynccontextmanager
async def timed_admission_lock(lock):
    with admission_span('admission.lock_wait') as waiting:
        async with lock:  # Preserve the original lock's enter/exit/cancellation behavior.
            waiting.finish()
            yield


class RequestTimingMiddleware:
    def __init__(self, app, *, path):
        self.app = app
        target = Path(path)
        if not target.is_absolute() or target.is_symlink():
            raise ValueError('HTTP timing path must be absolute and not a symlink')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler = RotatingFileHandler(target, maxBytes=8*1024*1024, backupCount=2, encoding='utf-8', delay=True)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started_at, started = time.time(), time.perf_counter()
        supplied = next((v.decode('ascii', errors='ignore') for k, v in scope.get('headers', [])
                         if k.lower() == b'x-cityverse-request-id'), '')
        request_id = supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', supplied) else uuid4().hex
        status, first_byte, completed, outcome = 500, None, False, 'application_error'
        admission, admission_token = None, None
        if scope.get('method') == 'POST' and scope.get('path') == '/api/onprem/scenario-rounds':
            try:
                admission = _AdmissionTiming(started,_current_task())
                admission_token = _ADMISSION_CONTEXT.set(admission)
            except Exception:
                admission = None
        request_view = None
        if scope.get('path') == '/api/onprem/scenario-rounds/current':
            try:
                query = parse_qs(scope.get('query_string', b'').decode('ascii', errors='replace'),
                    keep_blank_values=True, max_num_fields=32)
                candidate = query.get('view', ['full'])[-1]
                request_view = candidate if candidate in {'full', 'summary'} else 'invalid'
            except ValueError:
                request_view = 'invalid'

        async def timed_send(message):
            nonlocal status, first_byte, completed, outcome
            if message['type'] == 'http.response.start':
                status = message['status']
                first_byte = round((time.perf_counter()-started)*1000, 3)
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in {b'x-cityverse-request-id', b'server-timing'}]
                headers += [(b'x-cityverse-request-id', request_id.encode()),
                            (b'server-timing', f'app;dur={first_byte}'.encode())]
                message = {**message, 'headers': headers}
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                completed = True
                outcome = 'http_error' if status >= 400 else 'completed'

        try:
            await self.app(scope, receive, timed_send)
        finally:
            value = dict(schemaVersion='cityverse.http-timing/v1', requestId=request_id,
                startedAt=started_at, finishedAt=time.time(), method=scope.get('method'),
                path=scope.get('path'), status=status, outcome=outcome, responseCompleted=completed,
                applicationMs=first_byte, totalMs=round((time.perf_counter()-started)*1000, 3),
                source='asgi-perf-counter', pid=os.getpid())
            if admission is not None:
                try:
                    value.update(admission.close())
                except Exception:
                    admission.closed = True
                    value['admissionSpansTruncated'] = True
                finally:
                    if admission_token is not None:
                        try:
                            _ADMISSION_CONTEXT.reset(admission_token)
                        except Exception:
                            pass
            if request_view is not None:
                value['requestView'] = request_view
            try:
                # Timing persistence must not turn an already completed mutation
                # into an ambiguous HTTP failure if its diagnostic disk fills.
                self.handler.handle(logging.LogRecord('cityverse.http.timing', logging.INFO, '', 0,
                    json.dumps(value, separators=(',', ':'), allow_nan=False), (), None))
            except (OSError, ValueError):
                logging.getLogger(__name__).warning('http.timing_write_failed')
