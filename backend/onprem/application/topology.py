"""One 15-second observer per application with bounded, immutable history."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from copy import deepcopy
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from backend.onprem.evidence.topology import PodTopologyCollector, timestamp, unknown_nodes

logger = logging.getLogger(__name__)


class TopologyHistory:
    def __init__(self, path):
        self.path = Path(path)

    def append(self, sample, captured_at):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS samples (at REAL PRIMARY KEY, round_id TEXT, generation INTEGER, value TEXT NOT NULL)')
            db.execute('INSERT INTO samples VALUES (?,?,?,?)',
                (captured_at, sample['roundId'], sample['generation'], json.dumps(sample, allow_nan=False)))
            db.execute('DELETE FROM samples WHERE at < ?', (captured_at - 86400,))
            db.execute('DELETE FROM samples WHERE at NOT IN (SELECT at FROM samples ORDER BY at DESC LIMIT 5760)')
            db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def at(self, when, round_id, generation):
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            row = db.execute('SELECT value FROM samples WHERE at<=? AND at>? AND round_id=? AND generation=? ORDER BY at DESC LIMIT 1',
                (when, when-30, round_id, generation)).fetchone()
        return json.loads(row[0]) if row else None


class TopologyMonitor:
    interval_seconds = 15
    max_age_seconds = 30

    def __init__(self, collector, history, scope_reader, *, clock=time.time, monotonic=time.monotonic):
        self.collector, self.history, self.scope_reader = collector, history, scope_reader
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._latest = None
        self._next_poll = float('-inf')

    @classmethod
    def from_context(cls, context):
        return cls(PodTopologyCollector.from_context(context),
            TopologyHistory(context.settings.round_state_file.with_name('topology-health.sqlite3')),
            context.round_ledger.ownership_control)

    def poll_due(self):
        """A slow read never overlaps or causes catch-up bursts; GETs cannot trigger it."""
        with self._lock:
            now = self.monotonic()
            if now < self._next_poll:
                return False
            self._next_poll = now + self.interval_seconds
            before = self.scope_reader()
            checked_at = self.clock()
            status = 'available'
            try:
                if self.collector is None:
                    raise ValueError('Topology collector not configured')
                # Source time is taken before the API read, never after persistence.
                nodes = self.collector.collect(observed_at=checked_at)
            except Exception as error:
                logger.warning('Topology read unavailable errorType=%s', type(error).__name__)
                status, nodes = 'unavailable', unknown_nodes('source_unavailable')
            after = self.scope_reader()
            owner = before if all(before.get(k) == after.get(k) for k in ('roundId','generation')) else {}
            sample = dict(schemaVersion='cityverse.topology-health/v1', status=status,
                roundId=owner.get('roundId'), generation=owner.get('generation'),
                observedAt=timestamp(checked_at), capturedAt=checked_at, nodeStates=nodes,
                intervalSeconds=15, maxAgeSeconds=30, source='kubernetes-pod-readiness')
            self.history.append(sample, checked_at)
            self._latest = sample
            return True

    def snapshot(self, *, at=None, round_id=None, generation=None):
        historical = at is not None
        sample = self.history.at(at, round_id, generation) if historical else self._latest
        if sample is None:
            return dict(schemaVersion='cityverse.topology-health/v1', status='not_captured',
                mode='historical' if historical else 'current', observedAt=None, nodeStates=unknown_nodes('no_saved_evidence'),
                intervalSeconds=15, maxAgeSeconds=30, ageSeconds=None, fresh=False,
                roundId=round_id, generation=generation, source='kubernetes-pod-readiness')
        result = deepcopy(sample)
        age = (at if historical else self.clock()) - result.pop('capturedAt')
        fresh = 0 <= age < self.max_age_seconds and result['status'] == 'available'
        if not fresh:
            for node in result['nodeStates']:
                node.update(status='unknown', reason='source_unavailable' if result['status'] != 'available' else 'node_evidence_stale')
        result.update(mode='historical' if historical else 'current', fresh=fresh, ageSeconds=max(0,age))
        return result

    async def run(self, stop):
        while not stop.is_set():
            started = self.monotonic()
            try:
                await asyncio.to_thread(self.poll_due)
            except Exception as error:
                logger.warning('Topology observer unavailable errorType=%s', type(error).__name__)
            delay = max(0.1, self.interval_seconds - (self.monotonic()-started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def topology_lifespan(context):
    """Stop the read-only observer before the existing context releases its fence."""
    @asynccontextmanager
    async def lifespan(app):
        async with context.lifespan(app):
            stop = asyncio.Event()
            task = asyncio.create_task(context.topology_monitor.run(stop))
            try:
                yield
            finally:
                stop.set()
                await task
    return lifespan
