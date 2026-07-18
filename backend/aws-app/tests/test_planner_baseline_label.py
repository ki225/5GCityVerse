from __future__ import annotations

from typing import Any

from agent_runtime.planner import NetworkPlanner


def _intent(batch_scenarios: list[str] | None = None, event_type: str = "iot_surge") -> dict[str, Any]:
    return {
        "eventType": event_type,
        "eventScale": 10,
        "cityResidents": 1000,
        "riskLevel": "high",
        "controlLoop": {"pollIntervalSeconds": 30},
        "targetSlice": {"sst": 3, "name": "mMTC", "fiveQi": 79},
        "batchScenarios": batch_scenarios or [],
    }


def _metrics() -> dict[str, Any]:
    return {"throughputMbps": 924.2, "latencyMs": 12.0, "upfCpuPercent": 40.0, "dataSource": "eks+iperf3"}


def _baseline_observation(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item for item in observations if "Baseline" in item["label"] or "Pre-event" in item["label"])


def test_solo_scenario_keeps_baseline_metrics_label() -> None:
    """A single, non-batch trigger has no concurrent scenarios, so the plain
    "Baseline metrics" label (unambiguous) is preserved."""
    observations = NetworkPlanner.observations(_intent(batch_scenarios=["iot_surge"]), _metrics(), 86)

    observation = _baseline_observation(observations)
    assert observation["label"] == "Baseline metrics"
    assert observation["concurrentScenarios"] == []


def test_batch_scenario_relabels_baseline_as_pre_event_snapshot() -> None:
    """Regression test: when a batch trigger runs multiple sub-scenarios back to
    back, the "Baseline metrics" figure actually includes traffic from earlier
    sub-scenarios in the same batch and is misleading if labeled as a clean
    baseline. It must be relabeled and the concurrent scenarios listed.
    """
    observations = NetworkPlanner.observations(
        _intent(batch_scenarios=["concert", "iot_surge"], event_type="iot_surge"), _metrics(), 86
    )

    observation = _baseline_observation(observations)
    assert observation["label"] == "Pre-event snapshot (includes concurrent scenarios)"
    assert observation["concurrentScenarios"] == ["concert"]
    assert "924.2 Mbps" in observation["value"]


def test_network_round_plan_changes_with_observed_pressure() -> None:
    planner = NetworkPlanner()
    low = planner.network_round_plan(_intent(event_type="network_round"), {"latencyMs": 2, "upfCpuPercent": 10}, [{"load": 20}], "en")
    high = planner.network_round_plan(_intent(event_type="network_round"), {"latencyMs": 18, "upfCpuPercent": 80}, [{"load": 92}], "en")

    assert [item["tool"] for item in low] == ["get_network_analytics", "list_subscribers", "upsert_subscriber_profile", "verify_sla"]
    assert "activate_qos_policy" in [item["tool"] for item in high]
    assert "request_traffic_influence" in [item["tool"] for item in high]
    assert planner.capacity_target({"upfCpuPercent": 72}, 40) == 3
