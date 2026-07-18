from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field, ValidationError
except ImportError:  # pragma: no cover - keeps Lambda usable if pydantic is not packaged.
    class BaseModel:  # type: ignore
        def __init__(self, **data: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for key in annotations:
                if key in data:
                    setattr(self, key, data[key])
                elif hasattr(self.__class__, key):
                    setattr(self, key, getattr(self.__class__, key))
                else:
                    raise ValidationError(f"Missing field: {key}")

    Field = lambda default=None, **_: default  # type: ignore

    class ValidationError(ValueError):
        pass

from constants import DataSource, EventType, RiskLevel, SliceStrategy, SliceType


class CityVerseModel(BaseModel):
    model_config = {"use_enum_values": True}

    def to_dict(self) -> dict[str, Any]:
        if hasattr(self, "model_dump"):
            return self.model_dump()
        return {
            key: value.to_dict() if hasattr(value, "to_dict") else value
            for key, value in self.__dict__.items()
        }


class Bandwidth(CityVerseModel):
    uplink: str = ""
    downlink: str = ""


class EventConfig(CityVerseModel):
    slice_sst: int = Field(ge=1, le=255)
    slice_sd: str
    slice_type: SliceType
    ue_count: int = Field(ge=0)
    ue_ids: list[str]
    dnn: str
    risk: RiskLevel
    imsi_suffix: str
    five_qi: int = Field(ge=1)
    ue_ambr: Bandwidth
    session_ambr: Bandwidth
    mbr: Bandwidth
    gbr: Bandwidth
    traffic_profile: str


class ScenarioTrigger(CityVerseModel):
    event_type: EventType
    event_scale: int = 0


class TriggerRequest(CityVerseModel):
    event_type: EventType | None = None
    event_scale: int = 0
    city_residents: int = 0
    scenarios: list[ScenarioTrigger] = Field(default_factory=list)
    # Safe default: an omitted field must never silently opt a caller into AI/NEF
    # orchestration. Browser clients explicitly select and submit one strategy.
    slice_strategy: SliceStrategy = SliceStrategy.NONE

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        # pydantic validates the enum when packaged. The Lambda zip intentionally
        # supports a dependency-free fallback BaseModel too, so enforce the same
        # contract here rather than accepting arbitrary strings in production.
        try:
            self.slice_strategy = SliceStrategy(self.slice_strategy)
        except (TypeError, ValueError) as exc:
            raise ValueError("slice_strategy must be one of: none, static, ai") from exc


class NetworkMetrics(CityVerseModel):
    upfCpuPercent: float = 0.0
    upfPodCount: int = 0
    amfPodCount: int = 0
    gtpPacketsPerSec: int = 0
    pduSessionCount: int = 0
    latencyMs: float = 0.0
    throughputMbps: float = 0.0
    timestamp: int
    dataSource: DataSource = DataSource.UNAVAILABLE
    amfCpuPercent: float | None = None
    registeredUeCount: int | None = None
    uplinkMbps: float | None = None
    downlinkMbps: float | None = None


class NetworkSlice(CityVerseModel):
    sst: int
    type: SliceType
    sd: str
    load: int = Field(ge=0, le=100)
    sessions: int = Field(ge=0)
    trend: str
    throughputMbps: float | None = None
    dataSource: DataSource | None = None
