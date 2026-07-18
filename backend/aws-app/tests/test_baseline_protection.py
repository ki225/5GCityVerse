from __future__ import annotations

from agent_runtime.intent_manager import IntentManager
from agent_runtime.sla_verifier import SlaVerifier


class TestBaselineProtectionFloor:
    """Citizen baseline (eMBB) protection floor: pre-event-observed vs default source."""

    def test_floor_uses_pre_event_observed_baseline_times_point_eight(self, valid_event_config) -> None:
        manager = IntentManager()
        intent = manager.build_intent(
            "exec-1",
            "concert",
            valid_event_config,
            scenario_context={"baselineThroughputMbps": 10.0},
        )

        baseline_protection = intent["sla"]["baselineProtection"]
        assert baseline_protection == {
            "sliceType": "eMBB",
            "floorMbps": 8.0,
            "floorSource": "pre-event-observed",
        }

    def test_floor_falls_back_to_default_when_no_pre_event_sample(self, valid_event_config) -> None:
        manager = IntentManager()
        intent = manager.build_intent("exec-2", "concert", valid_event_config, scenario_context={})

        baseline_protection = intent["sla"]["baselineProtection"]
        assert baseline_protection == {
            "sliceType": "eMBB",
            "floorMbps": 0.95,
            "floorSource": "default",
        }

    def test_floor_falls_back_to_default_when_observed_baseline_is_zero(self, valid_event_config) -> None:
        """A zero/garbage observed sample must not produce a zero (meaningless) floor."""
        manager = IntentManager()
        intent = manager.build_intent(
            "exec-3",
            "concert",
            valid_event_config,
            scenario_context={"baselineThroughputMbps": 0},
        )

        assert intent["sla"]["baselineProtection"]["floorSource"] == "default"
        assert intent["sla"]["baselineProtection"]["floorMbps"] == 0.95


class TestBaselinePreservationCheck:
    """sla_verifier.baseline_preservation_check: passed / inconclusive / failed."""

    def _intent(self, floor_mbps: float = 8.0, floor_source: str = "pre-event-observed") -> dict:
        return {
            "sla": {
                "minThroughputMbps": 0,
                "latencyMsMax": 0,
                "minPduSessions": 0,
                "maxUpfCpuPercent": 0,
                "baselineProtection": {"sliceType": "eMBB", "floorMbps": floor_mbps, "floorSource": floor_source},
            },
            "targetSlice": {"sst": 3},
        }

    def _find(self, result: dict) -> dict:
        return next(c for c in result["checks"] if c["metric"] == "baselinePreservation")

    def test_passed_when_baseline_sample_at_or_above_floor(self) -> None:
        metrics = {
            "dataSource": "eks+iperf3",
            "scenarioTraffic": [{"scenario": "baseline", "throughputMbps": 9.0}],
        }
        verifier = SlaVerifier()

        result = verifier.verify(self._intent(floor_mbps=8.0), metrics, [])

        check = self._find(result)
        assert check["status"] == "passed"
        assert check["actual"] == 9.0
        assert check["source"] == "scenarioTraffic[scenario=baseline]"
        assert check["mechanism"] == (
            "slice isolation (S-NSSAI) + per-slice DNN + subscriber AMBR/5QI via PCF->SMF; "
            "PFCP QER enforcement unverified"
        )
        assert "timestamp" in check and "window" in check and "passCondition" in check
        assert result["status"] == "passed"

    def test_inconclusive_when_baseline_sample_missing(self) -> None:
        metrics = {"dataSource": "eks+iperf3", "scenarioTraffic": [{"scenario": "concert", "throughputMbps": 500.0}]}
        verifier = SlaVerifier()

        result = verifier.verify(self._intent(floor_mbps=8.0), metrics, [])

        check = self._find(result)
        assert check["status"] == "inconclusive"
        assert check["reason"] == "baseline sample unavailable"
        assert check["actual"] is None
        # Inconclusive must not force overall failed/degraded.
        assert result["status"] == "passed"
        assert result["adaptationRequired"] is False

    def test_failed_when_baseline_sample_below_floor(self) -> None:
        """A real breach signal: baseline traffic starved below its protection floor."""
        metrics = {
            "dataSource": "eks+iperf3",
            "scenarioTraffic": [{"scenario": "baseline", "throughputMbps": 2.0}],
        }
        verifier = SlaVerifier()

        result = verifier.verify(self._intent(floor_mbps=8.0), metrics, [])

        check = self._find(result)
        assert check["status"] == "failed"
        assert check["actual"] == 2.0
        assert result["status"] == "failed"
        assert result["adaptationRequired"] is True

    def test_check_omitted_when_intent_has_no_baseline_protection(self) -> None:
        """Callers that never set sla.baselineProtection (e.g. legacy intents) must not
        get a spurious baselinePreservation check."""
        intent = self._intent()
        del intent["sla"]["baselineProtection"]
        metrics = {"dataSource": "eks+iperf3", "scenarioTraffic": [{"scenario": "baseline", "throughputMbps": 9.0}]}
        verifier = SlaVerifier()

        result = verifier.verify(intent, metrics, [])

        assert all(c["metric"] != "baselinePreservation" for c in result["checks"])


class TestBaselineScenarioThroughputHelper:
    """app.CityVerseBackendApp.baseline_scenario_throughput extracts the pre-event sample."""

    def test_extracts_baseline_sample_throughput(self) -> None:
        from app import CityVerseBackendApp

        metrics = {"scenarioTraffic": [{"scenario": "concert", "throughputMbps": 500.0}, {"scenario": "baseline", "throughputMbps": 12.5, "transport": "free5gc-tun"}]}

        assert CityVerseBackendApp.baseline_scenario_throughput(metrics) == 12.5

    def test_returns_none_when_no_baseline_sample(self) -> None:
        from app import CityVerseBackendApp

        metrics = {"scenarioTraffic": [{"scenario": "concert", "throughputMbps": 500.0}]}

        assert CityVerseBackendApp.baseline_scenario_throughput(metrics) is None

    def test_returns_none_when_scenario_traffic_missing(self) -> None:
        from app import CityVerseBackendApp

        assert CityVerseBackendApp.baseline_scenario_throughput({}) is None
