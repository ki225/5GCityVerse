"""One durable FIFO dispatcher; network execution stays with the coordinator."""
from backend.onprem.storage.current_state import read_cleanup, replay_current
import asyncio
from collections import deque
import json
import logging
import re
import sqlite3
import sys
from uuid import UUID
from backend.onprem.application.errors import ApplicationError
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.runtime.errors import EvidenceUnavailable, ReleaseProofBlocked, RoundRuntimeBlocked
from backend.onprem.scenarios.contracts import RequestIdConflict, RoundConflict, RoundUnavailable
from backend.onprem.storage.rounds import maintenance_pending


def _log_failure(error, phase, request_id=None):
    """Bounded source identifiers only; diagnostics cannot change dispatch."""
    try:
        if phase not in {'dispatch_coordinator', 'scheduler_tick'}:
            return
        def canonical(value):
            if type(value) is not str or len(value) != 36:
                return None
            try:
                parsed = str(UUID(value))
            except ValueError:
                return None
            return parsed if parsed == value else None

        request_id = canonical(request_id)
        error_types = {
            RuntimeError: 'RuntimeError', ValueError: 'ValueError', TypeError: 'TypeError',
            KeyError: 'KeyError', OSError: 'OSError', TimeoutError: 'TimeoutError',
            AttributeError: 'AttributeError', AssertionError: 'AssertionError',
            NotImplementedError: 'NotImplementedError', sqlite3.OperationalError: 'OperationalError',
            json.JSONDecodeError: 'JSONDecodeError', RoundConflict: 'RoundConflict',
            RoundUnavailable: 'RoundUnavailable', RequestIdConflict: 'RequestIdConflict',
            RoundRuntimeBlocked: 'RoundRuntimeBlocked', EvidenceUnavailable: 'EvidenceUnavailable',
            ReleaseProofBlocked: 'ReleaseProofBlocked', PolicyBlocked: 'PolicyBlocked',
            ApplicationError: 'ApplicationError',
        }

        def source_frames(exception, limit):
            nonlocal request_id
            frames = deque(maxlen=limit)
            trace = exception.__traceback__
            for _ in range(256):
                if trace is None:
                    break
                frame = trace.tb_frame
                module = frame.f_globals.get('__name__')
                function = frame.f_code.co_name
                filename = frame.f_code.co_filename.replace('\\', '/')
                loaded = sys.modules.get(module) if type(module) is str else None
                source = getattr(loaded, '__file__', None)
                if (type(module) is str and len(module) <= 128
                        and re.fullmatch(r'backend\.onprem(?:\.[A-Za-z_][A-Za-z0-9_]*)+', module)
                        and type(source) is str and filename == source.replace('\\', '/')
                        and any(filename == path or filename.endswith('/' + path)
                                for path in (module.replace('.', '/') + '.py', module.replace('.', '/') + '/__init__.py'))
                        and len(function) <= 64 and (re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', function)
                            or function in {'<module>', '<lambda>', '<listcomp>', '<dictcomp>', '<genexpr>'})):
                    frames.append({'module': module, 'function': function, 'line': trace.tb_lineno})
                    if request_id is None:
                        entry = frame.f_locals.get('entry')
                        if type(entry) is not dict:
                            entries = frame.f_locals.get('entries')
                            entry = entries[0] if type(entries) is list and entries else None
                        if type(entry) is dict:
                            request_id = canonical(entry.get('requestId'))
                trace = trace.tb_next
            return list(frames)

        exceptions, seen = [], set()
        current = error
        while isinstance(current, BaseException) and id(current) not in seen and len(exceptions) < 3:
            seen.add(id(current))
            exceptions.append(current)
            current = current.__cause__ if current.__cause__ is not None else current.__context__
        # Root plus at most two causes/contexts; all layers share eight frames.
        limits = (8,) if len(exceptions) == 1 else (4, 4) if len(exceptions) == 2 else (4, 2, 2)
        frames, chain = [], []
        for exception, limit in zip(exceptions, limits):
            selected = source_frames(exception, limit)
            chain.append({'errorType': error_types.get(type(exception), 'UnknownError'),
                          'frameStart': len(frames), 'frameCount': len(selected)})
            frames.extend(selected)
        logging.getLogger('uvicorn.error').warning(
            'scenario.scheduler_failure phase=%s errorType=%s requestId=%s exceptionChain=%s frames=%s',
            phase, error_types.get(type(error), 'UnknownError'), request_id or '-',
            json.dumps(chain, separators=(',', ':')), json.dumps(frames, separators=(',', ':')))
    except Exception:
        pass  # Logging failure must preserve the original exception and mutation.


class ScenarioScheduler:
    def __init__(self, context):
        self.context = context
        self.repository = context.scenario_schedule
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = None

    def wake(self):
        self._wake.set()

    async def start(self):
        await asyncio.to_thread(self.repository.recover_dispatches)
        self._stop.clear()
        self._task = asyncio.create_task(self.run())

    async def close(self):
        self._stop.set()
        self._wake.set()
        if self._task:
            await self._task

    async def dispatch(self, entry):
        ctx = self.context
        drained = getattr(ctx.round_coordinator, 'execution_drained', None)
        if callable(drained) and not drained():
            self.wake()
            return entry  # Final native/fixture cleanup still owns the task.
        self.repository.transition(entry['queueId'], 'dispatching', expected={'queued'})
        try:
            result = await ctx.round_coordinator.start(entry['configuration'])
        except Exception as error:
            _log_failure(error, 'dispatch_coordinator', entry.get('requestId'))
            # A native reservation may have committed before the transport failed.
            reserved = await asyncio.to_thread(replay_current, ctx.round_ledger, entry['configuration'])
            self.repository.transition(entry['queueId'], 'active' if reserved else 'blocked',
                round_id=reserved['roundId'] if reserved else None, error=None if reserved else 'DISPATCH_UNAVAILABLE')
            raise
        self.repository.transition(entry['queueId'], 'active', round_id=result['roundId'])
        return result

    async def tick(self):
        ctx = self.context
        async with ctx.application_admission_lock:
            if ctx.application_shutting_down or ctx.application_maintenance or ctx.round_coordinator is None:
                return
            entries = await asyncio.to_thread(self.repository.entries)
            if not entries or entries[0]['status'] != 'queued':
                return
            current = await asyncio.to_thread(read_cleanup, ctx.round_ledger)
            if ctx.application_shutting_down or ctx.application_maintenance or maintenance_pending(current):
                return
            state = self.repository.lifecycle(current)
            if current.get('roundId') and current['stage'] != 'completed':
                if state['scenarioState'] == 'running' and state['evaluationState'] == 'completed':
                    await ctx.round_coordinator.cancel(current['roundId'])
                return  # Never overlap cohorts or bypass release proof.
            if not await asyncio.to_thread(ctx.runtime_policy_clear):
                return
            await self.dispatch(entries[0])

    async def run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.tick()
            except Exception as error:
                # Durable blocked state is exposed by the API; no blind retry.
                _log_failure(error, 'scheduler_tick')
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass
