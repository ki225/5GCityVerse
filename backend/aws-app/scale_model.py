"""Single source of truth for population -> real iperf3 demand.

One UERANSIM bearer represents a much larger city population.  The population is never
reported as measured traffic; it only selects an aggregate iperf3 target.  The packets
shown by the application still come from the TUN-bound iperf3 process in EKS.

The coefficients intentionally compress city-scale demand into a bounded lab workload:
all five maximum-scale scenarios together request at most 285 Mbps.  Within the usable
range, doubling the population doubles the requested rate.  A 0.2 Mbps floor exists only
so very small UDP tests remain observable and is explicitly exposed in the model.

Where a source's *text description* disagrees with what actually executes, the
`discrepancy` field records that in prose and the numeric fields below are set to the
value that actually executes at runtime.

Note: k8s/iperf3-jobs/iot-surge.yaml previously hardcoded "-P 50", diverging from
config.py's "-P 12"; it was aligned to config.py on 2026-07-05 and no discrepancy
remains.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import EVENT_CONFIG, IPERF3_ARGS
from constants import EventType


@dataclass(frozen=True)
class ScaleProfile:
    """Base-scale (event_scale == 1x) profile for one event type.

    Fields mirror config.py's EVENT_CONFIG/IPERF3_ARGS at base scale. event_scale-driven
    up-scaling (ue_count/bandwidth/parallel_streams growth as event_scale increases) is
    computed at runtime by app.py's event_config_for_request()/traffic_profile_for_scale()
    and is out of scope for this table (Phase 4+).
    """

    event_type: str
    ue_count: int
    bandwidth: str
    parallel_streams: int
    packet_length: int
    default_scale: int
    max_scale: int
    mbps_per_scale_unit: float
    min_mbps: float
    max_mbps: float
    sla_fraction: float
    expected_slice_load_band: tuple[int, int]
    source: str
    discrepancy: str | None = None


# expected_slice_load_band reuses the generic (warn, critical) thresholds already applied
# to every event's target slice load in decision_service.py's
# build_observations()/severity_for_threshold(target_load, 70, 85) -- there is no existing
# per-event expected-load constant anywhere in the codebase to copy, so this table points
# at that existing generic threshold rather than inventing new numbers.
_SEVERITY_LOAD_BAND: tuple[int, int] = (70, 85)  # decision_service.py:126 severity_for_threshold(target_load, 70, 85)

_SCALE_PROFILES: dict[str, ScaleProfile] = {
    EventType.CONCERT.value: ScaleProfile(
        event_type=EventType.CONCERT.value,
        ue_count=1,
        bandwidth="80M",
        parallel_streams=1,
        packet_length=1400,
        default_scale=80_000,
        max_scale=120_000,
        mbps_per_scale_unit=0.001,
        min_mbps=0.2,
        max_mbps=120.0,
        sla_fraction=0.8,
        expected_slice_load_band=_SEVERITY_LOAD_BAND,
        source="backend/aws-app/config.py:38,48,128",
    ),
    EventType.TYPHOON.value: ScaleProfile(
        event_type=EventType.TYPHOON.value,
        ue_count=3,
        bandwidth="12M",
        parallel_streams=1,
        packet_length=200,
        default_scale=1_200_000,
        max_scale=1_500_000,
        mbps_per_scale_unit=0.00001,
        min_mbps=0.2,
        max_mbps=15.0,
        sla_fraction=0.8,
        expected_slice_load_band=_SEVERITY_LOAD_BAND,
        source="backend/aws-app/config.py:54,64,130",
    ),
    EventType.ACCIDENT.value: ScaleProfile(
        event_type=EventType.ACCIDENT.value,
        ue_count=1,
        bandwidth="36M",
        parallel_streams=1,
        packet_length=1400,
        default_scale=1_800,
        max_scale=5_000,
        mbps_per_scale_unit=0.02,
        min_mbps=0.2,
        max_mbps=100.0,
        sla_fraction=0.8,
        expected_slice_load_band=_SEVERITY_LOAD_BAND,
        source="backend/aws-app/config.py:70,80,132",
    ),
    EventType.MEDICAL.value: ScaleProfile(
        event_type=EventType.MEDICAL.value,
        ue_count=1,
        bandwidth="13M",
        parallel_streams=1,
        packet_length=200,
        default_scale=650,
        max_scale=2_000,
        mbps_per_scale_unit=0.02,
        min_mbps=0.2,
        max_mbps=40.0,
        sla_fraction=0.8,
        expected_slice_load_band=_SEVERITY_LOAD_BAND,
        source="backend/aws-app/config.py:86,96,129",
    ),
    EventType.IOT_SURGE.value: ScaleProfile(
        event_type=EventType.IOT_SURGE.value,
        ue_count=50,
        bandwidth="417K",
        parallel_streams=12,
        packet_length=64,
        default_scale=50_000,
        max_scale=100_000,
        mbps_per_scale_unit=0.0001,
        min_mbps=0.2,
        max_mbps=10.0,
        sla_fraction=0.8,
        expected_slice_load_band=_SEVERITY_LOAD_BAND,
        source="backend/aws-app/config.py:102,112,131",
    ),
}


def expected_profile(event_type: str) -> ScaleProfile:
    """Look up the base-scale ScaleProfile for an event_type (e.g. "concert").

    Raises KeyError if event_type is not a known scenario, mirroring EVENT_CONFIG[...]'s
    own lookup behavior.
    """
    return _SCALE_PROFILES[event_type]


def all_profiles() -> dict[str, ScaleProfile]:
    """Return the full event_type -> ScaleProfile table."""
    return dict(_SCALE_PROFILES)


def target_mbps(event_type: str, event_scale: int) -> float:
    """Return the bounded aggregate rate requested from the real iperf3 client."""
    profile = expected_profile(event_type)
    safe_scale = max(1, min(int(event_scale), profile.max_scale))
    requested = safe_scale * profile.mbps_per_scale_unit
    return round(max(profile.min_mbps, min(requested, profile.max_mbps)), 3)


def target_mbps_for_ratio(event_type: str, scale_ratio: float) -> float:
    profile = expected_profile(event_type)
    scale = max(1, round(profile.default_scale * max(float(scale_ratio), 0.0)))
    return target_mbps(event_type, scale)


def sla_mbps_for_ratio(event_type: str, scale_ratio: float) -> float:
    profile = expected_profile(event_type)
    return round(max(0.1, target_mbps_for_ratio(event_type, scale_ratio) * profile.sla_fraction), 3)


__all__ = [
    "ScaleProfile",
    "expected_profile",
    "all_profiles",
    "target_mbps",
    "target_mbps_for_ratio",
    "sla_mbps_for_ratio",
    "EVENT_CONFIG",
    "IPERF3_ARGS",
]
