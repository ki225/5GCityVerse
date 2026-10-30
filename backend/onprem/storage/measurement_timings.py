"""Bounded diagnostic storage, isolated from native evidence transactions."""
from contextlib import closing
import json
import logging
from pathlib import Path
from queue import Queue, Empty, Full
import sqlite3
from threading import Event, Lock, Thread
import time


class MeasurementTimingRepository:
    retention = 64
    maximum_record_bytes = 512 * 1024

    def __init__(self, path, *, queue_capacity=8, retry_delays=(.05, .15, .3)):
        if type(queue_capacity) is not int or not 1 <= queue_capacity <= 64:
            raise ValueError('Diagnostic queue capacity must be between 1 and 64')
        self.legacy_path = Path(path)
        self.path = self.legacy_path.with_name(self.legacy_path.stem+'.measurement-timings.sqlite3')
        self._queue = Queue(maxsize=queue_capacity)
        self._retry_delays = tuple(retry_delays)
        self._lock, self._stop = Lock(), Event()
        self._worker = None
        self._accepting = True
        self._state = dict(submitted=0, pending=0, written=0, dropped=0, failed=0,
                           retried=0, rejected=0, lastErrorType=None)

    def status(self):
        with self._lock:
            return dict(self._state, accepting=self._accepting,
                        workerAlive=bool(self._worker and self._worker.is_alive()),
                        queueCapacity=self._queue.maxsize,
                        maximumRecordBytes=self.maximum_record_bytes,
                        semantics='Queue admission is not durable persistence; process loss may discard pending diagnostics.')

    def _encode(self, record):
        raw = json.dumps(record, allow_nan=False, separators=(',', ':'))
        if len(raw.encode('utf-8')) > self.maximum_record_bytes:
            raise ValueError('Measurement diagnostic record exceeds storage bound')
        return record['roundId'], record['generation'], raw

    def append(self, record):
        """Synchronous import/test entry point; native paths use submit()."""
        self._write(self._encode(record))

    def submit(self, record):
        """Nonblocking bounded admission; never throws into native control."""
        try:
            payload = self._encode(record)  # Immutable and bounded; outside SQL.
        except Exception as exc:
            with self._lock:
                self._state['rejected'] += 1
                self._state['lastErrorType'] = type(exc).__name__
            return False
        with self._lock:
            if not self._accepting:
                self._state['rejected'] += 1
                return False
            try:
                self._queue.put_nowait(payload)
            except Full:
                self._state['dropped'] += 1
                return False
            self._state['submitted'] += 1
            self._state['pending'] += 1
            if self._worker is None:
                self._worker = Thread(target=self._run, name='measurement-diagnostics', daemon=True)
                try:
                    self._worker.start()
                except Exception as exc:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self._state['pending'] -= 1
                    self._state['failed'] += 1
                    self._state['lastErrorType'] = type(exc).__name__
                    self._worker = None
                    return False
        return True

    def _write(self, payload):
        # No native ledger writer uses this file. Construction/GET create none.
        with closing(sqlite3.connect(self.path, timeout=.05)) as db:
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(64 * 1024 * 1024 // page_size))
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS measurement_timings ('
                    'id INTEGER PRIMARY KEY, round_id TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL)')
                db.execute('INSERT INTO measurement_timings(round_id,generation,payload) VALUES (?,?,?)',
                    payload)
                db.execute('DELETE FROM measurement_timings WHERE id NOT IN '
                    '(SELECT id FROM measurement_timings ORDER BY id DESC LIMIT ?)', (self.retention,))

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                payload = self._queue.get(timeout=.05)
            except Empty:
                continue
            failure = None
            try:
                for attempt in range(len(self._retry_delays)+1):
                    try:
                        self._write(payload)
                        with self._lock:
                            self._state['written'] += 1
                        failure = None
                        break
                    except Exception as exc:
                        failure = exc
                        error_code = getattr(exc, 'sqlite_errorcode', 0) or 0
                        retryable = (isinstance(exc, sqlite3.OperationalError) and
                            error_code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
                        if not retryable or attempt == len(self._retry_delays):
                            break
                        with self._lock:
                            self._state['retried'] += 1
                        time.sleep(self._retry_delays[attempt])
                if failure is not None:
                    with self._lock:
                        self._state['failed'] += 1
                        self._state['lastErrorType'] = type(failure).__name__
                    logging.getLogger(__name__).warning('Measurement diagnostic write failed errorType=%s', type(failure).__name__)
            finally:
                with self._lock:
                    self._state['pending'] -= 1
                self._queue.task_done()

    def close(self, timeout=5.):
        """Stop admission and attempt bounded drainage; report unfinished work."""
        with self._lock:
            self._accepting = False
            worker = self._worker
            self._stop.set()
        if worker is not None:
            worker.join(timeout=max(0., timeout))
        status = self.status()
        if status['pending']:
            logging.getLogger(__name__).warning('Measurement diagnostic drain incomplete pending=%s', status['pending'])
        return status

    @staticmethod
    def _read(path, round_id, generation, after, limit):
        result = dict(items=[], nextCursor=after, hasMore=False)
        if not path.exists():
            return result
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.05)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='measurement_timings'").fetchone() is None:
                return result
            rows = db.execute('SELECT id,payload FROM measurement_timings '
                'WHERE round_id=? AND generation=? AND id>? ORDER BY id LIMIT ?',
                (round_id, generation, after, limit+1)).fetchall()
        result.update(items=[dict(json.loads(raw), cursor=cursor) for cursor,raw in rows[:limit]],
            nextCursor=rows[min(len(rows), limit)-1][0] if rows else after, hasMore=len(rows)>limit)
        return result

    def page(self, round_id, generation, *, after=0, limit=10, legacy_after=0):
        result = dict(schema='measurement-timings/v1', roundId=round_id, generation=generation,
            retentionWindows=self.retention, storage=self.status(),
            semantics='Nested and parallel spans overlap; do not sum. Sidecar and legacy cursors are independent.')
        for key, path, cursor in (('current', self.path, after), ('legacy', self.legacy_path, legacy_after)):
            try:
                page = self._read(path, round_id, generation, cursor, limit)
            except sqlite3.Error as exc:
                page = dict(items=[], nextCursor=cursor, hasMore=False, unavailable=True, errorType=type(exc).__name__)
            if key == 'current':
                result.update(page)
            else:
                result['legacy'] = dict(page, cursorParameter='legacyAfter', source='round_database')
        return result
