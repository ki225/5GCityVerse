"""Bounded monotonic measurement diagnostics, separate from traffic evidence."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from threading import Lock
import time
from uuid import uuid4


_current = ContextVar('measurement_timing', default=None)


class MeasurementTiming:
    maximum_spans = 2048

    def __init__(self, snapshot, stage):
        self.origin = time.perf_counter()
        self.lock = Lock()
        self.spans, self.metrics, self.dropped = [], {}, 0
        self.polls, self.coalesced = {}, 0
        self.metadata = dict(schema='measurement-timings/v1', windowId=uuid4().hex,
            roundId=snapshot['roundId'], generation=snapshot['generation'], stage=stage,
            policyRevision=snapshot.get('policyRevision'), startedAt=time.time())

    @staticmethod
    def current():
        return _current.get()

    @contextmanager
    def bind(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def record(self, phase, started, *, status='ok', **fields):
        finished = time.perf_counter()
        row = dict(phase=phase, startOffsetMs=max(0., (started-self.origin)*1000),
            durationMs=max(0., (finished-started)*1000), status=status, **fields)
        with self.lock:
            if status == 'ok' and (phase == 'control.status' or fields.get('action') == 'status'
                                   or phase.startswith('ownership.')):
                key = (phase, fields.get('shardIndex'), fields.get('role'), fields.get('phaseAtRequest'),
                       fields.get('action'))
                item = self.polls.setdefault(key, dict(phase=phase,
                    **{k:fields[k] for k in ('shardIndex', 'role', 'phaseAtRequest', 'action') if k in fields},
                    count=0, totalDurationMs=0., minDurationMs=row['durationMs'], maxDurationMs=0.,
                    firstStartOffsetMs=row['startOffsetMs'], lastFinishOffsetMs=0.,
                    totalQueueMs=0., totalDispatchMs=0.))
                item['count'] += 1
                item['totalDurationMs'] += row['durationMs']
                item['minDurationMs'] = min(item['minDurationMs'], row['durationMs'])
                item['maxDurationMs'] = max(item['maxDurationMs'], row['durationMs'])
                item['firstStartOffsetMs'] = min(item['firstStartOffsetMs'], row['startOffsetMs'])
                item['lastFinishOffsetMs'] = max(item['lastFinishOffsetMs'], row['startOffsetMs']+row['durationMs'])
                item['totalQueueMs'] += fields.get('queueMs', 0.)
                item['totalDispatchMs'] += fields.get('dispatchMs', 0.)
                # Retain two concrete samples per scope, plus exact aggregates
                # over every successful poll / high-frequency ownership read.
                # Errors always retain full spans.
                if item['count'] > 2:
                    self.coalesced += 1
                    return
            if len(self.spans) < self.maximum_spans:
                self.spans.append(row)
            else:
                self.dropped += 1

    @contextmanager
    def span(self, phase, **fields):
        started, status = time.perf_counter(), 'ok'
        try:
            yield
        except BaseException as exc:
            status = 'error'
            fields['errorType'] = type(exc).__name__
            raise
        finally:
            self.record(phase, started, status=status, **fields)

    def snapshot(self, status):
        with self.lock:
            return {**self.metadata, 'status': status,
                'durationMs': (time.perf_counter()-self.origin)*1000,
                'spans': sorted(self.spans, key=lambda row: row['startOffsetMs']),
                'metrics': dict(self.metrics), 'droppedSpans': self.dropped,
                'coalescedSpans': self.coalesced,
                'pollSummaries': [dict(row) for row in self.polls.values()]}


def span(phase, **fields):
    recorder = MeasurementTiming.current()
    return recorder.span(phase, **fields) if recorder is not None else nullcontext()


def timed(phase):
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            with span(phase):
                return function(*args, **kwargs)
        return invoke
    return decorate


class PhaseTimeline:
    """Sequential shard phases; nested control/checkpoint spans may overlap."""
    def __init__(self, shard):
        self.recorder = MeasurementTiming.current()
        self.shard, self.phase, self.started = shard, None, None

    def advance(self, phase, *, status='ok'):
        if self.recorder is not None and self.phase is not None:
            self.recorder.record(self.phase, self.started, status=status, shardIndex=self.shard)
        self.phase, self.started = phase, time.perf_counter()

    def close(self, status='ok'):
        self.advance(None, status=status)
