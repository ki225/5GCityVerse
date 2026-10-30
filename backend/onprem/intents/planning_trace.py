"""Per-operation planning timings; no shared last-result or report history."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from .domain import ContractError

_current=ContextVar('intent_planning_timings',default=None)


class ReobserveBeforeWrite(ContractError):
    """Expected current-input change, with no current-plan native dispatch."""


@contextmanager
def capture():
    values={}
    token=_current.set(values)
    try:yield values
    finally:_current.reset(token)


@contextmanager
def phase(name):
    started=time.perf_counter()
    try:yield
    finally:
        values=_current.get()
        if values is not None:
            values[name]=values.get(name,0)+time.perf_counter()-started


def retryable(error):
    from .planning import PlanningError
    if not isinstance(error,PlanningError):return False
    if 'stale_observation_or_plan' in str(error):return True
    cause=error
    for _ in range(5):
        if isinstance(cause,TimeoutError) or type(cause).__name__ in {
                'APITimeoutError','ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout'}:
            return True
        cause=cause.__cause__
        if cause is None:break
    return False
