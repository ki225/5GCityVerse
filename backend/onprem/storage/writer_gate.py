"""Bounded FIFO admission for writers to one ledger in this process.

SQLite remains the cross-process fence. No thread, authority cache or queued
success is introduced: the caller executes and commits its own transaction.
"""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from os.path import normcase
from threading import Condition, Lock, get_ident
import sqlite3
import time
from weakref import WeakValueDictionary

_registry = WeakValueDictionary()
_registry_lock = Lock()


def _busy(message):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = 'SQLITE_BUSY'
    return error


class WriterGate:
    def __init__(self, maximum_waiters=32):
        self.maximum_waiters = maximum_waiters
        self.condition = Condition()
        self.waiters = deque()
        self.owner = None

    @contextmanager
    def enter(self, timeout=10):
        deadline, ticket = time.monotonic() + timeout, object()
        with self.condition:
            if self.owner == get_ident():
                raise _busy('Nested ledger writer transaction is not supported')
            if len(self.waiters) >= self.maximum_waiters:
                raise _busy('Ledger writer queue is full')
            self.waiters.append(ticket)
            try:
                while self.owner is not None or self.waiters[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _busy('Ledger writer queue deadline exceeded')
                    self.condition.wait(remaining)
                if deadline <= time.monotonic():
                    raise _busy('Ledger writer queue deadline exceeded')
                self.waiters.popleft()
                self.owner = get_ident()
            except BaseException:
                self.waiters.remove(ticket)
                self.condition.notify_all()
                raise
        try:
            # Share the original BEGIN wait budget with the FIFO wait.
            yield max(0., deadline - time.monotonic())
        finally:
            with self.condition:
                self.owner = None
                self.condition.notify_all()


def gate_for(path):
    key = normcase(str(Path(path).resolve()))
    with _registry_lock:
        gate = _registry.get(key)
        if gate is None:
            gate = _registry[key] = WriterGate()
        return gate


@contextmanager
def writer_slot(path, timeout=10):
    gate = gate_for(path)  # Hold the registry entry alive until release.
    with gate.enter(timeout) as remaining:
        yield remaining
