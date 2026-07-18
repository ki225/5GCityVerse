from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from constants import RiskLevel, SliceType  # noqa: E402
from models import Bandwidth, EventConfig  # noqa: E402
from agent_runtime.loop import AgentxGCoreLoop  # noqa: E402


@pytest.fixture
def valid_event_config() -> EventConfig:
    """A legal EventConfig for the iot_surge scenario (mMTC slice)."""
    return EventConfig(
        slice_sst=3,
        slice_sd="000003",
        slice_type=SliceType.MMTC,
        ue_count=5,
        ue_ids=["ue-1", "ue-2", "ue-3", "ue-4", "ue-5"],
        dnn="internet",
        risk=RiskLevel.MEDIUM,
        imsi_suffix="00001",
        five_qi=79,
        ue_ambr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        session_ambr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        mbr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        gbr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        traffic_profile="1M",
    )


@pytest.fixture
def healthy_metrics() -> dict[str, Any]:
    """Metrics that comfortably satisfy the iot_surge SLA (throughput target 2 Mbps)."""
    return {
        "throughputMbps": 5.0,
        "latencyMs": 20.0,
        "upfCpuPercent": 30.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }


@pytest.fixture
def baseline_slices() -> list[dict[str, Any]]:
    return [
        {"sst": 3, "type": "mMTC", "sd": "000003", "load": 20, "sessions": 5, "trend": "stable", "throughputMbps": 5.0},
        {"sst": 1, "type": "eMBB", "sd": "000001", "load": 10, "sessions": 2, "trend": "stable", "throughputMbps": 50.0},
    ]


class StubFree5gc:
    """Minimal stand-in for the free5gc client used by ToolGateway."""

    def __init__(
        self,
        subscribers: list[dict[str, Any]] | None = None,
        upsert_result: dict[str, Any] | None = None,
        list_subscribers_error: Exception | None = None,
    ) -> None:
        self._subscribers = subscribers if subscribers is not None else []
        self._upsert_result = upsert_result or {"status": "success", "upserted": []}
        self._list_subscribers_error = list_subscribers_error

    def list_subscribers(self) -> list[dict[str, Any]]:
        if self._list_subscribers_error:
            raise self._list_subscribers_error
        return self._subscribers

    def is_cityverse_subscriber(self, subscriber: dict[str, Any]) -> bool:
        return True

    def upsert_subscribers(self, event_type: str, cfg: EventConfig, execution_id: str, limit: int) -> dict[str, Any]:
        return self._upsert_result


class StubEnvironment:
    """Minimal stand-in for the scenario environment trigger used by ToolGateway."""

    def __init__(self, trigger_result: dict[str, Any] | None = None) -> None:
        self._trigger_result = trigger_result or {"status": "success", "operation": "trigger"}

    def trigger(self, event_type: str, cfg: EventConfig, execution_id: str | None = None) -> dict[str, Any]:
        return self._trigger_result


def _metrics_callable(metrics: dict[str, Any]) -> Callable[..., dict[str, Any]]:
    def _current_metrics(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(metrics)

    return _current_metrics


def _slices_callable(slices: list[dict[str, Any]]) -> Callable[..., list[dict[str, Any]]]:
    def _current_slices(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(slices)

    return _current_slices


@pytest.fixture
def make_loop(healthy_metrics, baseline_slices) -> Callable[..., AgentxGCoreLoop]:
    """Factory fixture that builds an AgentxGCoreLoop with overridable stub dependencies.

    Any of the keyword arguments below can be overridden by the caller to inject
    specific failure/behavior stubs for a given test.
    """

    def _build(
        *,
        current_metrics: Callable[..., dict[str, Any]] | None = None,
        current_slices: Callable[..., list[dict[str, Any]]] | None = None,
        free5gc: Any = None,
        environment: Any = None,
        metrics: Any = None,
        runtime_subscriber_upsert_limit: int = 10,
        lambda_function_names: dict[str, str] | None = None,
        invalidate_metrics: Callable[[], None] | None = None,
    ) -> AgentxGCoreLoop:
        resolved_current_metrics = current_metrics or _metrics_callable(healthy_metrics)
        resolved_current_slices = current_slices or _slices_callable(baseline_slices)
        resolved_free5gc = free5gc if free5gc is not None else StubFree5gc()
        resolved_environment = environment if environment is not None else StubEnvironment()
        return AgentxGCoreLoop(
            metrics=metrics,
            free5gc=resolved_free5gc,
            environment=resolved_environment,
            current_metrics=resolved_current_metrics,
            current_slices=resolved_current_slices,
            runtime_subscriber_upsert_limit=runtime_subscriber_upsert_limit,
            lambda_function_names=lambda_function_names or {},
            invalidate_metrics=invalidate_metrics,
        )

    return _build
