"""Failure-only fixed metadata for native reads; never authority or retries."""
from contextvars import ContextVar
from functools import wraps
import json
import logging
import math
import time

_CURRENT = ContextVar('native_read_diagnostic', default=None)
_SOURCES = {'ue_read', 'ue_tun', 'oam'}
_STAGES = {'input_owner', 'epoch_preflight', 'dispatch', 'output_bound', 'wire_frame',
    'utf8_decode', 'epoch_postflight', 'deadline', 'logs_read', 'logs_validate',
    'addresses_read', 'addresses_decode', 'binding_match', 'ping_read', 'ping_decode',
    'population_check', 'pod_postflight', 'batch_wait', 'request', 'json_decode',
    'schema_validate', 'reference_join', 'reference_persist', 'context_wait',
    'boot_epoch_check', 'inventory_seal', 'context_schema'}
_KINDS = {'none', 'logs', 'addresses', 'ping', 'rtt', 'smf_health',
    'smf_inventory_open', 'amf_contexts', 'smf_context', 'smf_inventory_close'}
_ERRORS = {'EvidenceUnavailable', 'TimeoutError', 'TimeoutExpired', 'HTTPError',
    'URLError', 'RemoteDisconnected', 'ConnectionError', 'ConnectionResetError',
    'ConnectionRefusedError', 'BrokenPipeError', 'OSError', 'JSONDecodeError',
    'UnicodeDecodeError', 'ValueError', 'TypeError', 'KeyError', 'RuntimeError',
    'CancelledError', 'OperationalError'}


def stage(name, *, kind=None, deadline=None, **counts):
    """Call-site enum only; diagnostic failures cannot affect the native read."""
    try:
        current = _CURRENT.get()
        if current is None:
            return
        if name in _STAGES:
            if name != current['stage'] or kind in _KINDS and kind != current['kind']:
                now = time.monotonic()
                previous = current['stage']
                current['completed'][previous] = current['completed'].get(previous, 0.) + max(0., now-current['phaseStarted'])
                current['phaseStarted'] = now
            current['stage'] = name
        if kind in _KINDS:
            current['kind'] = kind
        if type(deadline) in (int, float) and math.isfinite(deadline):
            current['deadline'] = deadline
        for key in ('requestedCount', 'returnedCount', 'generation', 'returnCode', 'outputBytes'):
            value = counts.get(key)
            if type(value) is int and -(1 << 31) <= value < (1 << 63):
                current[key] = value
    except BaseException:
        pass


def failure(error, *, http_status=None):
    try:
        current = _CURRENT.get()
        if current is None or current.get('recorded'):
            return
        current['recorded'] = True
        now = time.monotonic()
        name = type(error).__name__
        reason = {'HTTPError': 'http_status', 'RemoteDisconnected': 'remote_disconnect',
            'JSONDecodeError': 'json_decode', 'TimeoutError': 'timeout',
            'TimeoutExpired': 'timeout', 'URLError': 'connection',
            'ConnectionResetError': 'connection', 'ConnectionRefusedError': 'connection',
            'OSError': 'connection', 'UnicodeDecodeError': 'decode'}.get(name, 'read_rejected')
        row = {'source': current['source'], 'stage': current['stage'], 'kind': current['kind'],
            'errorType': name if name in _ERRORS else 'UnexpectedError', 'reason': reason,
            'wallTimeUnixMs': round(time.time() * 1000),
            'elapsedMs': round((now-current['started']) * 1000, 3),
            'currentPhaseElapsedMs': round(max(0., now-current['phaseStarted']) * 1000, 3),
            'completedPhaseMs': {key: round(value*1000, 3) for key, value in current['completed'].items()}}
        if 'deadline' in current:
            row['remainingMs'] = round((current['deadline']-now) * 1000, 3)
        if type(http_status) is int and 100 <= http_status <= 599:
            row['httpStatus'] = http_status
        for key in ('requestedCount', 'returnedCount', 'generation', 'returnCode', 'outputBytes'):
            if key in current:
                row[key] = current[key]
        logging.getLogger('uvicorn.error').warning('native_read_failure %s', json.dumps(row, sort_keys=True, allow_nan=False))
    except BaseException:
        pass


def diagnosed(source):
    if source not in _SOURCES:
        raise ValueError('Unknown native diagnostic source')
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            try:
                parent = _CURRENT.get()
                started = time.monotonic()
                current = {'source': source, 'stage': 'input_owner', 'phaseStarted': started, 'completed': {},
                           'kind': parent['kind'] if parent and parent['source'] == source else 'none',
                           'started': started}
                if parent and 'deadline' in parent:
                    current['deadline'] = parent['deadline']
                token = _CURRENT.set(current)
            except BaseException:
                return function(*args, **kwargs)
            try:
                return function(*args, **kwargs)
            except BaseException as error:
                failure(error)
                raise
            finally:
                try:
                    _CURRENT.reset(token)
                except BaseException:
                    pass
        return invoke
    return decorate
