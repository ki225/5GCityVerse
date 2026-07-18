from __future__ import annotations

from agent_runtime.intent_manager import IntentManager


def test_throughput_target_scales_down_with_low_scale_ratio() -> None:
    """The SLA follows the same population-proportional iperf3 target."""
    full_target = IntentManager.throughput_target_for_event("concert", 1.0)
    low_target = IntentManager.throughput_target_for_event("concert", 0.1)

    assert full_target == 64.0
    assert low_target == 6.4
    assert low_target < full_target


def test_throughput_target_equals_base_at_full_scale() -> None:
    assert IntentManager.throughput_target_for_event("medical", 1.0) == 10.4
    assert IntentManager.throughput_target_for_event("iot_surge", 1.0) == 4.0


def test_throughput_target_has_observable_floor() -> None:
    target = IntentManager.throughput_target_for_event("iot_surge", 0.01)
    assert target == 0.16


def test_throughput_target_does_not_scale_above_full_at_high_ratio() -> None:
    """High-scale demand rises linearly until the explicit scenario cap."""
    target = IntentManager.throughput_target_for_event("concert", 2.0)
    assert target == 96.0


def test_build_intent_exposes_sla_scale_ratio_and_scaled_target(valid_event_config) -> None:
    manager = IntentManager()
    intent = manager.build_intent(
        "exec-1",
        "concert",
        valid_event_config,
        scenario_context={"scaleRatio": 0.25, "eventScale": 20000},
    )

    assert intent["sla"]["slaScaleRatio"] == 0.25
    assert intent["sla"]["minThroughputMbps"] == 16.0


def test_build_intent_defaults_scale_ratio_to_one_when_absent(valid_event_config) -> None:
    """Non-scenario callers (e.g. handle_agent_tool) call build_intent without a
    scenario_context; the SLA target must fall back to the full base target."""
    manager = IntentManager()
    intent = manager.build_intent("exec-2", "concert", valid_event_config)

    assert intent["sla"]["slaScaleRatio"] == 1.0
    assert intent["sla"]["minThroughputMbps"] == 64.0


def test_network_round_session_target_matches_real_representative_bearers(valid_event_config) -> None:
    intent = IntentManager().build_intent(
        "round-1",
        "network_round",
        valid_event_config,
        {"batchScenarios": ["concert", "typhoon", "iot_surge"]},
    )

    assert intent["sla"]["minPduSessions"] == 5


def test_build_intent_leaves_latency_target_unaffected_by_scale(valid_event_config) -> None:
    manager = IntentManager()
    intent = manager.build_intent(
        "exec-3",
        "concert",
        valid_event_config,
        scenario_context={"scaleRatio": 0.25},
    )

    assert intent["sla"]["latencyMsMax"] == IntentManager.latency_target_for_slice(
        IntentManager.enum_value(valid_event_config.slice_type)
    )


def test_zero_scale_ratio_still_applies_observable_floor_not_fallback(valid_event_config) -> None:
    manager = IntentManager()
    intent = manager.build_intent(
        "exec-4",
        "concert",
        valid_event_config,
        scenario_context={"scaleRatio": 0.0},
    )

    assert intent["sla"]["slaScaleRatio"] == 0.0
    assert intent["sla"]["minThroughputMbps"] == 0.16
