from __future__ import annotations

from typing import Any

from time_utils import TimeUtils


class SlaVerifier:
    def verify(self, intent: dict[str, Any], metrics: dict[str, Any], slices: list[dict[str, Any]]) -> dict[str, Any]:
        sla = intent.get("sla") or {}
        target_slice = intent.get("targetSlice") or {}
        throughput_check = self.throughput_check(metrics, sla.get("minThroughputMbps", 0))
        pdu_session_check = self.pdu_session_check(intent, metrics, sla.get("minPduSessions", 0))
        checks = [
            self.max_check("latencyMs", metrics.get("latencyMs", 0), sla.get("latencyMsMax", 0)),
            throughput_check,
            pdu_session_check,
            self.max_check("upfCpuPercent", metrics.get("upfCpuPercent", 0), sla.get("maxUpfCpuPercent", 75)),
        ]
        target_load = self.find_slice_load(slices, target_slice.get("sst"))
        if target_load is not None:
            checks.append(self.max_check("sliceLoad", target_load, 90))
        baseline_protection = sla.get("baselineProtection")
        if isinstance(baseline_protection, dict):
            checks.append(self.baseline_preservation_check(metrics, baseline_protection))

        failed = [check for check in checks if check["status"] == "failed"]
        degraded = [
            check
            for check in checks
            if check["status"] == "degraded"
        ]
        status = "failed" if failed else "degraded" if degraded else "passed"
        return {
            "status": status,
            "checks": checks,
            "adaptationRequired": status in {"failed", "degraded"},
            "checkedAt": TimeUtils.now(),
            "dataSource": metrics.get("dataSource", "unknown"),
        }

    @staticmethod
    def ue_attach_in_progress(intent: dict[str, Any]) -> bool:
        """True when runtime priming's UE bearer wait timed out rather than confirming a bearer.

        `scenario_environment.wait_for_ue_bearer` returns the literal string
        "UE bearer verification timed out; continuing with async status/probe polling"
        (see scenario_environment.py) as one of its `actions` entries when it never saw
        the "PDU Session establishment is successful" log line within the deadline.
        """
        runtime_prime = intent.get("runtimePrime") if isinstance(intent.get("runtimePrime"), dict) else {}
        actions = runtime_prime.get("actions") if isinstance(runtime_prime.get("actions"), list) else []
        return any("UE bearer verification timed out" in str(action) for action in actions)

    def pdu_session_check(self, intent: dict[str, Any], metrics: dict[str, Any], target: Any) -> dict[str, Any]:
        actual_value = self.number(metrics.get("pduSessionCount", 0))
        target_value = self.number(target)
        check = self.min_check("pduSessionCount", actual_value, target_value)
        if check["status"] != "failed" or not self.ue_attach_in_progress(intent):
            return check
        expected_ues = len(intent.get("ueIds") or [])
        attached = int(actual_value)
        check["status"] = "inconclusive"
        check["reason"] = (
            f"UE attach in progress ({attached} of {expected_ues} attached)"
            if expected_ues
            else f"UE attach in progress ({attached} attached)"
        )
        return check

    @staticmethod
    def throughput_check(metrics: dict[str, Any], target: Any) -> dict[str, Any]:
        actual_value = SlaVerifier.number(metrics.get("throughputMbps", 0))
        target_value = SlaVerifier.number(target)
        probe = metrics.get("ueTunProbe") if isinstance(metrics.get("ueTunProbe"), dict) else {}
        data_source = str(metrics.get("dataSource") or "")
        has_probe_bearer = (
            bool(probe.get("ready"))
            and (
                actual_value > 0
                or SlaVerifier.number(probe.get("latencyMs")) > 0
                or SlaVerifier.number(probe.get("receivedPackets")) > 0
            )
        )
        has_capacity_source = (
            any(key in metrics for key in ("uplinkMbps", "downlinkMbps", "iperf3Mbps"))
            or data_source in {"prometheus", "eks+prometheus", "eks+iperf3", "free5gc-oam+iperf3"}
        )
        if target_value > 0 and has_probe_bearer and not has_capacity_source:
            return {
                "metric": "throughputMbps",
                "actual": actual_value,
                "target": f">= {target_value:g}",
                "status": "failed",
                "passCondition": "A UE bearer is active, but no capacity throughput source is available; throughput SLA is not verified",
                "source": "ue-bearer-probe",
                "timestamp": TimeUtils.now(),
                "window": "post-action 30s",
            }
        return SlaVerifier.min_check("throughputMbps", actual_value, target_value)

    @staticmethod
    def baseline_preservation_check(metrics: dict[str, Any], baseline_protection: dict[str, Any]) -> dict[str, Any]:
        """Verify citizen (eMBB) baseline traffic is not starved by the event's slicing.

        Reads the current baseline sample from scenarioTraffic. A missing sample is
        genuinely inconclusive (we cannot claim protection held or broke), while a
        sample below the floor is a real breach signal that may trigger adaptation.
        """
        floor_value = SlaVerifier.number(baseline_protection.get("floorMbps"))
        mechanism = (
            "slice isolation (S-NSSAI) + per-slice DNN + subscriber AMBR/5QI via PCF->SMF; "
            "PFCP QER enforcement unverified"
        )
        traffic = metrics.get("scenarioTraffic")
        sample = None
        if isinstance(traffic, list):
            sample = next(
                (item for item in traffic if isinstance(item, dict) and str(item.get("scenario") or "") == "baseline"),
                None,
            )
        common = {
            "metric": "baselinePreservation",
            "target": f">= {floor_value:g}",
            "passCondition": f"citizen baseline (eMBB) throughput must stay at or above {floor_value:g} Mbps",
            "source": "scenarioTraffic[scenario=baseline]",
            "timestamp": TimeUtils.now(),
            "window": "post-action 30s",
            "mechanism": mechanism,
        }
        if sample is None:
            return {
                **common,
                "actual": None,
                "status": "inconclusive",
                "reason": "baseline sample unavailable",
            }
        actual_value = SlaVerifier.number(sample.get("throughputMbps"))
        status = "passed" if actual_value >= floor_value else "failed"
        return {
            **common,
            "actual": actual_value,
            "status": status,
        }

    @staticmethod
    def min_check(metric: str, actual: Any, target: Any, source: str = "runtime-metrics") -> dict[str, Any]:
        actual_value = SlaVerifier.number(actual)
        target_value = SlaVerifier.number(target)
        if target_value <= 0:
            status = "passed"
        elif actual_value >= target_value:
            status = "passed"
        elif actual_value >= target_value * 0.7:
            status = "degraded"
        else:
            status = "failed"
        return {
            "metric": metric,
            "actual": actual_value,
            "target": f">= {target_value:g}",
            "status": status,
            "passCondition": f"{metric} must be at least {target_value:g}",
            "source": source,
            "timestamp": TimeUtils.now(),
            "window": "post-action 30s",
        }

    @staticmethod
    def max_check(metric: str, actual: Any, target: Any, source: str = "runtime-metrics") -> dict[str, Any]:
        actual_value = SlaVerifier.number(actual)
        target_value = SlaVerifier.number(target)
        if target_value <= 0:
            status = "passed"
        elif actual_value <= target_value:
            status = "passed"
        elif actual_value <= target_value * 1.25:
            status = "degraded"
        else:
            status = "failed"
        return {
            "metric": metric,
            "actual": actual_value,
            "target": f"<= {target_value:g}",
            "status": status,
            "passCondition": f"{metric} must stay at or below {target_value:g}",
            "source": source,
            "timestamp": TimeUtils.now(),
            "window": "post-action 30s",
        }

    @staticmethod
    def find_slice_load(slices: list[dict[str, Any]], sst: int | None) -> float | None:
        for item in slices:
            if item.get("sst") == sst:
                return SlaVerifier.number(item.get("load", 0))
        return None

    @staticmethod
    def number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
