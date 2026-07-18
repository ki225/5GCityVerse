from __future__ import annotations

import re

import pytest

from config import EVENT_CONFIG, IPERF3_ARGS
from scale_model import all_profiles, expected_profile, target_mbps


def _bandwidth_from_traffic_profile(traffic_profile: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?[KMG])", traffic_profile)
    assert match, f"no bandwidth token found in traffic_profile={traffic_profile!r}"
    return match.group(1)


def _parallel_streams_from_traffic_profile(traffic_profile: str) -> int:
    match = re.search(r"x (\d+) parallel streams", traffic_profile)
    return int(match.group(1)) if match else 1


@pytest.mark.parametrize("event_type", list(EVENT_CONFIG.keys()))
def test_scale_model_ue_count_matches_config(event_type: str) -> None:
    profile = expected_profile(event_type)
    assert profile.ue_count == EVENT_CONFIG[event_type].ue_count


@pytest.mark.parametrize("event_type", list(EVENT_CONFIG.keys()))
def test_scale_model_bandwidth_matches_config_traffic_profile(event_type: str) -> None:
    profile = expected_profile(event_type)
    assert profile.bandwidth == _bandwidth_from_traffic_profile(EVENT_CONFIG[event_type].traffic_profile)


@pytest.mark.parametrize("event_type", list(EVENT_CONFIG.keys()))
def test_scale_model_parallel_streams_matches_config_traffic_profile(event_type: str) -> None:
    profile = expected_profile(event_type)
    assert profile.parallel_streams == _parallel_streams_from_traffic_profile(EVENT_CONFIG[event_type].traffic_profile)


@pytest.mark.parametrize("event_type", list(EVENT_CONFIG.keys()))
def test_scale_model_matches_iperf3_args_bandwidth_and_parallel(event_type: str) -> None:
    """IPERF3_ARGS is config.py's other (currently unused elsewhere) statement of the same
    per-event iperf3 invocation; scale_model must agree with it too so it can't silently
    drift from either of config.py's two representations."""
    args = IPERF3_ARGS[event_type]
    bandwidth = args[args.index("-b") + 1]
    parallel = int(args[args.index("-P") + 1]) if "-P" in args else 1
    packet_length = int(args[args.index("-l") + 1])

    profile = expected_profile(event_type)
    assert profile.bandwidth == bandwidth
    assert profile.parallel_streams == parallel
    assert profile.packet_length == packet_length


def test_all_profiles_covers_every_event_config_entry() -> None:
    assert set(all_profiles().keys()) == set(EVENT_CONFIG.keys())


def test_expected_profile_unknown_event_raises_key_error() -> None:
    with pytest.raises(KeyError):
        expected_profile("not_a_real_event")


def test_iot_surge_uses_runtime_executed_value() -> None:
    """iot_surge's parallel_streams matches config.py's 12 (k8s/iperf3-jobs/iot-surge.yaml
    was aligned to this value on 2026-07-05, so no discrepancy remains)."""
    profile = expected_profile("iot_surge")
    assert profile.parallel_streams == 12
    assert profile.discrepancy is None
    assert "12" in EVENT_CONFIG["iot_surge"].traffic_profile


def test_no_profiles_have_discrepancy() -> None:
    for profile in all_profiles().values():
        assert profile.discrepancy is None


@pytest.mark.parametrize(
    ("event_type", "small_scale", "large_scale", "small_mbps", "large_mbps"),
    [
        ("concert", 6_000, 12_000, 6.0, 12.0),
        ("typhoon", 100_000, 200_000, 1.0, 2.0),
        ("accident", 500, 1_000, 10.0, 20.0),
        ("medical", 250, 500, 5.0, 10.0),
        ("iot_surge", 10_000, 20_000, 1.0, 2.0),
    ],
)
def test_population_doubling_doubles_bounded_iperf_target(
    event_type: str,
    small_scale: int,
    large_scale: int,
    small_mbps: float,
    large_mbps: float,
) -> None:
    assert target_mbps(event_type, small_scale) == small_mbps
    assert target_mbps(event_type, large_scale) == large_mbps


def test_all_maximum_scenarios_stay_below_one_gigabit() -> None:
    total = sum(target_mbps(name, profile.max_scale) for name, profile in all_profiles().items())
    assert total == 285.0
    assert all(target_mbps(name, profile.max_scale) < 1000 for name, profile in all_profiles().items())
