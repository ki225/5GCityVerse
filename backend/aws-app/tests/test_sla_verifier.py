from __future__ import annotations

import pytest

from agent_runtime.sla_verifier import SlaVerifier


class TestThroughputBearerOnlyFailed:
    """Group 1: throughput bearer-only判 failed (sla_verifier.py:56-63)"""

    def test_throughput_bearer_only_failed_no_capacity_source(self):
        """Bearer active but no capacity source → should fail with 'no capacity' message."""
        # Metrics: no uplinkMbps, downlinkMbps, iperf3Mbps; dataSource not in valid set
        metrics = {
            "throughputMbps": 0,
            "ueTunProbe": {"ready": True, "latencyMs": 12.0, "receivedPackets": 10},
            "dataSource": "unknown",  # not in {"prometheus", "eks+prometheus", "eks+iperf3", "free5gc-oam+iperf3"}
            "latencyMs": 10,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
        }
        intent = {
            "sla": {"minThroughputMbps": 5.0, "latencyMsMax": 50, "minPduSessions": 1, "maxUpfCpuPercent": 75},
            "targetSlice": {"sst": 3},
        }
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, [])
        checks = result["checks"]
        throughput_check = next((c for c in checks if c["metric"] == "throughputMbps"), None)
        assert throughput_check is not None, "throughputMbps check should exist"
        assert throughput_check["status"] == "failed", "Should fail when bearer active but no capacity source"
        assert "no capacity throughput source" in throughput_check["passCondition"], \
            f"passCondition should mention 'no capacity', got: {throughput_check['passCondition']}"

    def test_throughput_with_iperf3_avoids_bearer_only_failed(self):
        """With iperf3Mbps present → should not fail via bearer-only path; throughput check runs normally."""
        # Same metrics but add iperf3Mbps (capacity source)
        metrics = {
            "throughputMbps": 0,
            "iperf3Mbps": 4.0,  # Capacity source present
            "ueTunProbe": {"ready": True, "latencyMs": 12.0, "receivedPackets": 10},
            "dataSource": "unknown",
            "latencyMs": 10,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
        }
        intent = {
            "sla": {"minThroughputMbps": 5.0, "latencyMsMax": 50, "minPduSessions": 1, "maxUpfCpuPercent": 75},
            "targetSlice": {"sst": 3},
        }
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, [])
        checks = result["checks"]
        throughput_check = next((c for c in checks if c["metric"] == "throughputMbps"), None)
        assert throughput_check is not None
        # With iperf3Mbps, throughput_check should fail but NOT because of bearer-only
        # actual=0 < target=5, so it fails, but passCondition should be min_check style
        assert "no capacity" not in throughput_check["passCondition"], \
            f"Should not mention 'no capacity', got: {throughput_check['passCondition']}"
        assert "must be at least" in throughput_check["passCondition"], \
            f"Should use min_check message, got: {throughput_check['passCondition']}"


class TestFourKeysInChecks:
    """Group 2: 四件套欄位 (E3) - checks must have source, timestamp, window, passCondition"""

    def test_verify_returns_checks_with_four_required_keys(self):
        """Every check dict must contain: source, timestamp, window, passCondition (all non-empty strings)."""
        metrics = {
            "throughputMbps": 10.0,
            "latencyMs": 20.0,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
            "dataSource": "prometheus",
        }
        intent = {
            "sla": {
                "minThroughputMbps": 5.0,
                "latencyMsMax": 50,
                "minPduSessions": 1,
                "maxUpfCpuPercent": 75,
            },
            "targetSlice": {"sst": 3},
        }
        slices = [{"sst": 3, "load": 50}]
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, slices)

        # Top level should have checkedAt and dataSource
        assert "checkedAt" in result
        assert "dataSource" in result
        assert result["dataSource"] == "prometheus"

        checks = result["checks"]
        assert len(checks) > 0, "checks should not be empty"

        # Each check must have the four required keys
        required_keys = {"source", "timestamp", "window", "passCondition"}
        for check in checks:
            for key in required_keys:
                assert key in check, f"Check {check.get('metric')} missing key '{key}'"
                assert isinstance(check[key], str), f"Check {check.get('metric')}.{key} must be string, got {type(check[key])}"
                assert len(check[key]) > 0, f"Check {check.get('metric')}.{key} must be non-empty string"


class TestMinCheckBoundaries:
    """Group 3: min_check邊界值"""

    def test_min_check_actual_equals_target_passed(self):
        """actual == target → 'passed'"""
        result = SlaVerifier.min_check("testMetric", 10, 10)
        assert result["status"] == "passed"

    def test_min_check_actual_at_seventy_percent_degraded(self):
        """actual == target * 0.7 → 'degraded'"""
        result = SlaVerifier.min_check("testMetric", 7, 10)
        assert result["status"] == "degraded"

    def test_min_check_actual_below_seventy_percent_failed(self):
        """actual < target * 0.7 → 'failed'"""
        result = SlaVerifier.min_check("testMetric", 6.9, 10)
        assert result["status"] == "failed"

    def test_min_check_target_zero_always_passed(self):
        """target == 0 → 'passed' (no matter actual)"""
        result1 = SlaVerifier.min_check("testMetric", 0, 0)
        assert result1["status"] == "passed"
        result2 = SlaVerifier.min_check("testMetric", 100, 0)
        assert result2["status"] == "passed"

    def test_min_check_actual_above_target_passed(self):
        """actual > target → 'passed'"""
        result = SlaVerifier.min_check("testMetric", 15, 10)
        assert result["status"] == "passed"


class TestMaxCheckBoundaries:
    """Group 4: max_check邊界值"""

    def test_max_check_actual_equals_target_passed(self):
        """actual == target → 'passed'"""
        result = SlaVerifier.max_check("testMetric", 10, 10)
        assert result["status"] == "passed"

    def test_max_check_actual_at_one_point_two_five_degraded(self):
        """actual == target * 1.25 → 'degraded'"""
        result = SlaVerifier.max_check("testMetric", 12.5, 10)
        assert result["status"] == "degraded"

    def test_max_check_actual_above_one_point_two_five_failed(self):
        """actual > target * 1.25 → 'failed'"""
        result = SlaVerifier.max_check("testMetric", 12.6, 10)
        assert result["status"] == "failed"

    def test_max_check_target_zero_always_passed(self):
        """target == 0 → 'passed' (no matter actual)"""
        result1 = SlaVerifier.max_check("testMetric", 0, 0)
        assert result1["status"] == "passed"
        result2 = SlaVerifier.max_check("testMetric", 100, 0)
        assert result2["status"] == "passed"

    def test_max_check_actual_below_target_passed(self):
        """actual < target → 'passed'"""
        result = SlaVerifier.max_check("testMetric", 5, 10)
        assert result["status"] == "passed"


class TestVerifyStatusAggregation:
    """Group 5: verify整體狀態聚合"""

    def test_all_checks_passed_returns_passed_status(self):
        """All checks passed → status='passed', adaptationRequired=False"""
        metrics = {
            "throughputMbps": 10.0,
            "latencyMs": 20.0,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
            "dataSource": "prometheus",
        }
        intent = {
            "sla": {
                "minThroughputMbps": 5.0,
                "latencyMsMax": 50,
                "minPduSessions": 1,
                "maxUpfCpuPercent": 75,
            },
            "targetSlice": {"sst": 3},
        }
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, [])
        assert result["status"] == "passed"
        assert result["adaptationRequired"] is False

    def test_one_failed_check_returns_failed_status(self):
        """At least one check failed → status='failed', adaptationRequired=True"""
        metrics = {
            "throughputMbps": 2.0,  # Below target of 5.0
            "latencyMs": 20.0,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
            "dataSource": "prometheus",
        }
        intent = {
            "sla": {
                "minThroughputMbps": 5.0,
                "latencyMsMax": 50,
                "minPduSessions": 1,
                "maxUpfCpuPercent": 75,
            },
            "targetSlice": {"sst": 3},
        }
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, [])
        assert result["status"] == "failed"
        assert result["adaptationRequired"] is True

    def test_degraded_check_no_failed_returns_degraded_status(self):
        """At least one degraded, no failed → status='degraded', adaptationRequired=True"""
        # throughputMbps at 3.5 (70% of 5.0 = degraded, not failed)
        metrics = {
            "throughputMbps": 3.5,
            "latencyMs": 20.0,
            "pduSessionCount": 5,
            "upfCpuPercent": 30,
            "dataSource": "prometheus",
        }
        intent = {
            "sla": {
                "minThroughputMbps": 5.0,
                "latencyMsMax": 50,
                "minPduSessions": 1,
                "maxUpfCpuPercent": 75,
            },
            "targetSlice": {"sst": 3},
        }
        verifier = SlaVerifier()
        result = verifier.verify(intent, metrics, [])
        assert result["status"] == "degraded"
        assert result["adaptationRequired"] is True


class TestPduSessionCountInconclusiveOnBearerTimeout:
    """Group 6 (A2): pduSessionCount must be 'inconclusive', not 'failed', while UE
    attach is still in progress (runtimePrime reports the bearer-verification timeout),
    and inconclusive must not force overall status to failed/degraded (no adaptation)."""

    TIMED_OUT_RUNTIME_PRIME = {
        "actions": ["UE bearer verification timed out; continuing with async status/probe polling"],
    }

    def _intent(self, *, runtime_prime=None, min_pdu_sessions=5, ue_ids=None):
        return {
            "sla": {
                "minThroughputMbps": 0,
                "latencyMsMax": 0,
                "minPduSessions": min_pdu_sessions,
                "maxUpfCpuPercent": 0,
            },
            "targetSlice": {"sst": 3},
            "runtimePrime": runtime_prime or {},
            "ueIds": ue_ids if ue_ids is not None else [f"ue-{i}" for i in range(min_pdu_sessions)],
        }

    def test_pdu_session_count_inconclusive_when_bearer_timed_out_and_below_target(self):
        metrics = {"pduSessionCount": 2, "dataSource": "prometheus"}
        intent = self._intent(runtime_prime=self.TIMED_OUT_RUNTIME_PRIME, min_pdu_sessions=5)
        verifier = SlaVerifier()

        result = verifier.verify(intent, metrics, [])

        pdu_check = next(c for c in result["checks"] if c["metric"] == "pduSessionCount")
        assert pdu_check["status"] == "inconclusive"
        assert pdu_check["reason"] == "UE attach in progress (2 of 5 attached)"

    def test_inconclusive_pdu_session_check_does_not_force_adaptation(self):
        """All other checks pass; only pduSessionCount is short (bearer timed out) ->
        overall status must not become failed/degraded, and adaptation must be skipped."""
        metrics = {
            "throughputMbps": 100.0,
            "latencyMs": 5.0,
            "pduSessionCount": 0,
            "upfCpuPercent": 10,
            "dataSource": "prometheus",
        }
        intent = self._intent(runtime_prime=self.TIMED_OUT_RUNTIME_PRIME, min_pdu_sessions=3, ue_ids=["ue-1", "ue-2", "ue-3"])
        intent["sla"]["minThroughputMbps"] = 1.0
        intent["sla"]["latencyMsMax"] = 50
        intent["sla"]["maxUpfCpuPercent"] = 75
        verifier = SlaVerifier()

        result = verifier.verify(intent, metrics, [])

        pdu_check = next(c for c in result["checks"] if c["metric"] == "pduSessionCount")
        assert pdu_check["status"] == "inconclusive"
        assert result["status"] == "passed"
        assert result["adaptationRequired"] is False

    def test_pdu_session_count_stays_failed_without_bearer_timeout_signal(self):
        """Same shortfall, but runtimePrime shows no timeout -> must remain 'failed' (honest negative)."""
        metrics = {"pduSessionCount": 2, "dataSource": "prometheus"}
        intent = self._intent(runtime_prime={"actions": ["verified UE bearer from pod-xyz"]}, min_pdu_sessions=5)
        verifier = SlaVerifier()

        result = verifier.verify(intent, metrics, [])

        pdu_check = next(c for c in result["checks"] if c["metric"] == "pduSessionCount")
        assert pdu_check["status"] == "failed"
        assert "reason" not in pdu_check

    def test_pdu_session_count_passed_stays_passed_even_with_bearer_timeout(self):
        """If the target is already met, a bearer-timeout signal must not downgrade a passed check."""
        metrics = {"pduSessionCount": 5, "dataSource": "prometheus"}
        intent = self._intent(runtime_prime=self.TIMED_OUT_RUNTIME_PRIME, min_pdu_sessions=5)
        verifier = SlaVerifier()

        result = verifier.verify(intent, metrics, [])

        pdu_check = next(c for c in result["checks"] if c["metric"] == "pduSessionCount")
        assert pdu_check["status"] == "passed"

    def test_ue_attach_in_progress_false_when_runtime_prime_missing(self):
        assert SlaVerifier.ue_attach_in_progress({}) is False

    def test_ue_attach_in_progress_true_when_action_contains_timeout_phrase(self):
        assert SlaVerifier.ue_attach_in_progress({"runtimePrime": self.TIMED_OUT_RUNTIME_PRIME}) is True
