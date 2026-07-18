from __future__ import annotations

from typing import Any

from tests.conftest import StubFree5gc


EXPECTED_TOP_LEVEL_KEYS = {
    "intent",
    "baseline",
    "planner",
    "executor",
    "verification",
    "adaptation",
    "validationReport",
    "agentDecision",
    "finalMetrics",
    "finalSlices",
}


def test_run_success_path_completes_without_adaptation(make_loop, valid_event_config, healthy_metrics, baseline_slices):
    """(a) Normal completion: all executor tool calls succeed, SLA verification passes,
    and no adaptation round is needed."""
    loop = make_loop()

    result = loop.run(
        execution_id="exec-1",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=healthy_metrics,
        baseline_slices=baseline_slices,
    )

    assert EXPECTED_TOP_LEVEL_KEYS.issubset(result.keys())
    assert result["executor"]["status"] == "success"
    assert all(action["status"] == "success" for action in result["executor"]["actions"])
    assert result["verification"]["status"] == "passed"
    assert result["verification"]["adaptationRequired"] is False
    assert result["adaptation"]["executed"] is False

    # No QoS actuator action was planned in this healthy fixture.  That is not
    # an observation outage, so every evidence level must say not-applicable
    # instead of the misleading unavailable placeholder.
    decision_checks = result["agentDecision"]["verification"]
    assert decision_checks, "expected at least one verification check"
    assert all(check["pfcpEvidence"]["status"] == "not-applicable" for check in decision_checks)
    assert all(check["kernelEvidence"]["status"] == "not-applicable" for check in decision_checks)
    assert all(check["effectEvidence"]["status"] == "not-applicable" for check in decision_checks)
    assert result["agentDecision"]["dataPlaneActuation"]["status"] == "not-requested"


def test_run_blocking_tool_failure_returns_degraded_early(make_loop, valid_event_config, healthy_metrics, baseline_slices):
    """(b) Blocking failure path: list_subscribers (a blocking tool per
    NetworkExecutor.is_blocking_tool) raises, so ToolGateway.call catches the
    exception and returns status "failed". Executor.execute then short-circuits
    with status "failed" before any post-action SLA verification runs, and
    loop.run() takes the early-return branch (loop.py:91-137)."""
    failing_free5gc = StubFree5gc(list_subscribers_error=RuntimeError("free5gc NRF unreachable"))
    loop = make_loop(free5gc=failing_free5gc)

    result = loop.run(
        execution_id="exec-2",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=healthy_metrics,
        baseline_slices=baseline_slices,
    )

    assert EXPECTED_TOP_LEVEL_KEYS.issubset(result.keys())
    assert result["executor"]["status"] == "failed"

    verification = result["verification"]
    assert verification["status"] == "degraded"
    assert verification["adaptationRequired"] is False
    assert "reason" in verification
    assert "failedActions" in verification
    assert any(entry.get("tool") == "list_subscribers" for entry in verification["failedActions"])

    adaptation = result["adaptation"]
    assert adaptation["executed"] is False
    assert "skipped" in adaptation["reason"].lower()


def test_run_triggers_bounded_adaptation_when_sla_degraded(make_loop, valid_event_config, baseline_slices):
    """(c) Adaptation path: final metrics observed after a successful executor run
    violate the SLA (throughput far below the iot_surge target of 2 Mbps and UPF
    CPU pegged high), so SlaVerifier.verify() sets adaptationRequired True and
    loop.adapt_once_if_needed() fires a single bounded patch_hpa call
    (loop.py:141-146, 192-207), followed by a re-verification."""
    degraded_metrics = {
        "throughputMbps": 0.05,
        "latencyMs": 400.0,
        "upfCpuPercent": 99.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        # scenario match lets ToolGateway.wait_for_sla_metrics (used by the
        # verify_sla executor action) return immediately without sleeping.
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }
    loop = make_loop(current_metrics=lambda *a, **k: dict(degraded_metrics))

    result = loop.run(
        execution_id="exec-3",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=degraded_metrics,
        baseline_slices=baseline_slices,
    )

    assert result["executor"]["status"] == "success"

    adaptation = result["adaptation"]
    assert adaptation["executed"] is True
    assert adaptation["round"] == 1
    assert adaptation["maxRounds"] == 1
    assert "postAdaptationVerification" in adaptation
    assert adaptation["action"]["tool"] == "patch_hpa"

    # The final verification returned to the caller is the re-verification run
    # after adaptation, and it should match the postAdaptationVerification snapshot.
    assert result["verification"] == adaptation["postAdaptationVerification"]


def test_adaptation_invalidates_metrics_cache_before_reread(make_loop, valid_event_config, baseline_slices):
    """(d) Regression for the postAdaptationVerification staleness bug: once
    adapt_once_if_needed() executes a non-failed patch_hpa action, loop.run()
    must invalidate the caller's metrics cache *before* re-reading
    current_metrics()/current_slices(), otherwise postAdaptationVerification
    would observe the pre-adaptation (stale, TTL-cached) values instead of the
    post-adaptation state."""
    degraded_metrics = {
        "throughputMbps": 0.05,
        "latencyMs": 400.0,
        "upfCpuPercent": 99.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }
    healthy_metrics_after_adaptation = {
        "throughputMbps": 5.0,
        "latencyMs": 20.0,
        "upfCpuPercent": 40.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }

    invalidate_calls: list[int] = []
    # Cache stand-in: current_metrics returns degraded values until the cache
    # is invalidated, mirroring app.py's TTL-cached current_metrics().
    cache_state = {"invalidated": False}

    def fake_current_metrics(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(healthy_metrics_after_adaptation if cache_state["invalidated"] else degraded_metrics)

    def fake_invalidate_metrics() -> None:
        cache_state["invalidated"] = True
        invalidate_calls.append(1)

    loop = make_loop(current_metrics=fake_current_metrics, invalidate_metrics=fake_invalidate_metrics)

    result = loop.run(
        execution_id="exec-4",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=degraded_metrics,
        baseline_slices=baseline_slices,
    )

    assert result["adaptation"]["executed"] is True
    # invalidate_metrics must have fired exactly once, as part of the adaptation.
    assert len(invalidate_calls) == 1
    # The post-adaptation reread must observe the fresh (non-cached) values.
    assert result["finalMetrics"]["throughputMbps"] == healthy_metrics_after_adaptation["throughputMbps"]
    assert result["adaptation"]["postAdaptationVerification"]["status"] == "passed"


def test_adaptation_marks_ineffective_when_patch_hpa_is_skipped(make_loop, valid_event_config, baseline_slices):
    """F3: when patch_hpa is the only adaptation lever and it comes back "skipped"
    (HPA is runtime-managed; no separate patch_hpa Lambda configured, per
    ToolGateway.patch_hpa/tool_gateway.py:140-149), the adaptation payload must not
    claim a normal executed round. It must honestly report effective=False with a
    reason, and the re-verification must be annotated as reflecting unchanged state."""
    degraded_metrics = {
        "throughputMbps": 0.05,
        "latencyMs": 400.0,
        "upfCpuPercent": 99.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }
    # Default make_loop fixture has no lambda_function_names configured, so patch_hpa
    # takes the "skipped" (runtime_managed_hpa) branch.
    loop = make_loop(current_metrics=lambda *a, **k: dict(degraded_metrics))

    result = loop.run(
        execution_id="exec-skip",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=degraded_metrics,
        baseline_slices=baseline_slices,
    )

    adaptation = result["adaptation"]
    assert adaptation["executed"] is True
    assert adaptation["action"]["status"] == "skipped"
    assert adaptation["effective"] is False
    assert "no actionable lever available" in adaptation["reason"]
    assert adaptation["postAdaptationVerification"]["note"] == (
        "state unchanged; re-verification reflects pre-adaptation state"
    )


def test_adaptation_skips_invalidate_hook_when_not_provided(make_loop, valid_event_config, baseline_slices):
    """Backward compatibility: when no invalidate_metrics callback is injected
    (the default via make_loop), adaptation still runs without error."""
    degraded_metrics = {
        "throughputMbps": 0.05,
        "latencyMs": 400.0,
        "upfCpuPercent": 99.0,
        "pduSessionCount": 5,
        "dataSource": "prometheus",
        "iperf3": {"scenario": "iot_surge", "source": "server-log"},
    }
    loop = make_loop(current_metrics=lambda *a, **k: dict(degraded_metrics))

    result = loop.run(
        execution_id="exec-5",
        event_type="iot_surge",
        cfg=valid_event_config,
        baseline_metrics=degraded_metrics,
        baseline_slices=baseline_slices,
    )

    assert result["adaptation"]["executed"] is True
