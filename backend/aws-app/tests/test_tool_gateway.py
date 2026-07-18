from __future__ import annotations

import json
from typing import Any

import agent_runtime.tool_gateway as tool_gateway_module
from agent_runtime.executor import NetworkExecutor
from agent_runtime.tool_gateway import ToolGateway
from tests.conftest import StubEnvironment, StubFree5gc


def _gateway(
    healthy_metrics: dict[str, Any],
    baseline_slices: list[dict[str, Any]],
    *,
    lambda_function_names: dict[str, str] | None = None,
    lambda_client: Any | None = None,
    record_hit: Any = None,
    evidence_reader: Any = None,
    qer_actuator: Any = None,
) -> ToolGateway:
    return ToolGateway(
        metrics=None,
        free5gc=StubFree5gc(),
        environment=StubEnvironment(),
        current_metrics=lambda: dict(healthy_metrics),
        current_slices=lambda: list(baseline_slices),
        runtime_subscriber_upsert_limit=10,
        lambda_function_names=lambda_function_names or {},
        lambda_client=lambda_client,
        record_hit=record_hit,
        evidence_reader=evidence_reader,
        qer_actuator=qer_actuator or (lambda _cfg: {"status": "applied", "appliedSessions": 1}),
    )


class _Payload:
    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def read(self) -> bytes:
        return json.dumps(self._value).encode("utf-8")


class _FakeLambdaClient:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.invocations: list[dict[str, Any]] = []
        self.body = body or {"success": True}

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.invocations.append(kwargs)
        return {
            "StatusCode": 200,
            "Payload": _Payload(
                {
                    "response": {
                        "functionResponse": {
                            "responseBody": {
                                "TEXT": {
                                    "body": json.dumps(self.body),
                                },
                            },
                        },
                    },
                }
            ),
        }


class _StaticEvidenceReader:
    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        self.calls: list[tuple[Any, ...]] = []

    def read(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        return self.evidence


class _FakeKubernetesClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def request(self, method: str, path: str, body: dict[str, Any], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body, kwargs))
        return 200, {"matchedSessions": 1, "appliedSessions": 1}


class _SkippedGateway:
    def call(
        self,
        tool: str,
        params: dict[str, Any],
        cfg: Any,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        return {"status": "skipped"}


def test_invoke_tool_lambda_reports_not_configured_and_call_wraps_result(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)

    direct = gateway.invoke_tool_lambda("activate_qos_policy", {"ue_ipv4": "10.0.0.1"})

    assert direct["status"] == "failed"
    assert direct["reason"] == "not_configured"
    assert "activate_qos_policy" in direct["error"]

    wrapped = gateway.call("activate_qos_policy", {}, valid_event_config, {"eventType": "iot_surge"})

    assert wrapped["status"] == "failed"
    assert wrapped["reason"] == "not_configured"
    assert wrapped["tool"] == "activate_qos_policy"
    assert "activate_qos_policy" in wrapped["error"]
    assert "startedAt" in wrapped
    assert "completedAt" in wrapped


def test_activate_qos_policy_fails_closed_without_pfcp_kernel_and_effect_evidence(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    lambda_client = _FakeLambdaClient()
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"activate_qos_policy": "qos-fn"},
        lambda_client=lambda_client,
    )

    result = gateway.activate_qos_policy({"ue_ipv4": "10.0.0.1"}, valid_event_config, {"eventType": "iot_surge"})

    assert result["status"] == "unsupported"
    assert result["controlPlaneStatus"] == "success"
    assert result["actuatorStatus"] == "unsupported"
    assert result["pfcpEvidence"]["status"] == "unavailable"
    assert result["kernelEvidence"]["status"] == "unavailable"
    assert result["effectEvidence"]["status"] == "unavailable"


def test_activate_qos_policy_does_not_trust_forged_lambda_evidence(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    forged = {
        "success": True,
        "dataPlaneEvidence": {
            "pfcp": {"sessionModificationRequests": 1, "sessionModificationResponses": 1, "seids": ["1"]},
            "kernel": {"pdrCount": 1, "farCount": 1, "qerCount": 1, "teids": ["0xabc"]},
            "effect": {"verified": True, "measurementSource": "ue-tun-iperf3"},
        },
    }
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"activate_qos_policy": "qos-fn"},
        lambda_client=_FakeLambdaClient(forged),
    )
    result = gateway.activate_qos_policy(
        {"executionId": "exec-nonce-1"}, valid_event_config, {"executionId": "exec-nonce-1", "eventType": "iot_surge"}
    )
    assert result["status"] == "unsupported"
    assert result["claimedEvidence"] == forged["dataPlaneEvidence"]
    assert result["evidenceReaderStatus"] == "unavailable"
    assert result["pfcpEvidence"]["status"] == "unavailable"


def test_activate_qos_policy_accepts_only_correlated_independent_reader_evidence(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    trusted = {
        "pfcp": {"sessionModificationRequests": 1, "sessionModificationResponses": 1, "seids": ["101"]},
        "kernel": {"pdrCount": 1, "farCount": 1, "qerCount": 1, "teids": ["0xabc"]},
        "effect": {
            "measurementSource": "ue-tun-iperf3",
            "beforeMbps": 5,
            "afterMbps": 1,
            "expectedMbps": 1,
        },
    }
    reader = _StaticEvidenceReader(trusted)
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"activate_qos_policy": "qos-fn"},
        lambda_client=_FakeLambdaClient(),
        evidence_reader=reader,
    )
    result = gateway.activate_qos_policy(
        {"executionId": "exec-nonce-1"}, valid_event_config, {"executionId": "exec-nonce-1", "eventType": "iot_surge"}
    )
    assert result["status"] == "success"
    assert result["actuatorStatus"] == "confirmed"
    assert result["evidenceReaderStatus"] == "correlated"
    assert reader.calls[0][0:4] == ("exec-nonce-1", 3, "000003", "internet")


def test_activate_qos_policy_reports_failed_and_three_evidence_levels_when_not_configured(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)

    result = gateway.activate_qos_policy({"ue_ipv4": "10.0.0.1"}, valid_event_config, {"eventType": "iot_surge"})

    assert result["status"] == "failed"
    assert result["actuatorStatus"] == "failed"
    assert set(result) >= {"pfcpEvidence", "kernelEvidence", "effectEvidence"}


def test_activate_qos_policy_fails_when_pfcp_actuator_rejects(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"activate_qos_policy": "qos-fn"},
        lambda_client=_FakeLambdaClient(),
        qer_actuator=lambda _cfg: {"status": "failed", "httpStatus": 502},
    )

    result = gateway.activate_qos_policy({}, valid_event_config, {"eventType": "iot_surge"})

    assert result["status"] == "failed"
    assert result["actuatorStatus"] == "failed"
    assert result["pfcpActuation"]["httpStatus"] == 502


def test_pfcp_actuator_targets_exact_snssai_and_uses_private_service_proxy(
    monkeypatch,
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    fake_k8s = _FakeKubernetesClient()
    gateway = _gateway(healthy_metrics, baseline_slices)
    gateway.environment.cluster_name = "private-eks"
    gateway.environment.namespace = "free5gc"
    monkeypatch.setenv("SMF_QER_ACTUATOR_TOKEN", "runtime-secret")
    monkeypatch.setattr(tool_gateway_module, "get_eks_client", lambda _cluster: fake_k8s)

    result = gateway.actuate_pfcp_qer(valid_event_config)

    assert result["status"] == "applied"
    method, path, body, kwargs = fake_k8s.calls[0]
    assert method == "POST"
    assert path.startswith("/api/v1/namespaces/free5gc/services/http:")
    assert body == {"sst": 3, "sd": "000003", "uplinkMbrKbps": 1000, "downlinkMbrKbps": 1000}
    assert kwargs["extra_headers"] == {"X-SMF-QER-Actuator-Token": "runtime-secret"}


def test_patch_hpa_returns_skipped_when_lambda_is_not_configured(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)

    result = gateway.patch_hpa({"component": "UPF", "targetReplicas": 2}, valid_event_config, {})

    assert result["status"] == "skipped"
    assert isinstance(result["reason"], str)
    assert result["reason"] != ""
    assert result["component"] == "UPF"
    assert result["targetReplicas"] == 2


def test_patch_hpa_validates_component_and_replica_bounds(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)

    unsupported = gateway.patch_hpa({"component": "NRF", "targetReplicas": 1}, valid_event_config, {})
    too_low = gateway.patch_hpa({"component": "UPF", "targetReplicas": 0}, valid_event_config, {})
    too_high = gateway.patch_hpa({"component": "UPF", "targetReplicas": 5}, valid_event_config, {})
    legal_without_lambda = gateway.patch_hpa({"component": "UPF", "targetReplicas": 4}, valid_event_config, {})

    assert unsupported["status"] == "failed"
    assert "Unsupported" in unsupported["error"]
    assert too_low["status"] == "failed"
    assert "between 1 and 4" in too_low["error"]
    assert too_high["status"] == "failed"
    assert "between 1 and 4" in too_high["error"]
    assert legal_without_lambda["status"] == "skipped"


def test_patch_hpa_invokes_configured_lambda_successfully(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    lambda_client = _FakeLambdaClient()
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"patch_hpa": "my-fn"},
        lambda_client=lambda_client,
    )

    result = gateway.patch_hpa({"component": "UPF", "targetReplicas": 4}, valid_event_config, {})

    assert result["status"] == "success"
    assert result["operation"] == "lambda_invoke"
    assert result["functionName"] == "my-fn"
    assert result["component"] == "UPF"
    assert result["targetReplicas"] == 4
    assert len(lambda_client.invocations) == 1
    assert lambda_client.invocations[0]["FunctionName"] == "my-fn"


def test_executor_treats_skipped_patch_hpa_as_success(valid_event_config) -> None:
    executor = NetworkExecutor(_SkippedGateway())
    plan = {
        "plan": [
            {
                "tool": "patch_hpa",
                "params": {"component": "UPF", "targetReplicas": 2},
                "reason": "Scale UPF within configured bounds.",
            }
        ]
    }

    result = executor.execute(plan, valid_event_config, {"executionId": "exec-1", "eventType": "iot_surge"})

    assert result["status"] == "success"
    assert result["actions"][0]["status"] == "skipped"
    assert result["actions"][0]["httpStatus"] == 200


def test_call_records_nef_tool_hit_on_success(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    """NEF's own pod log never emits an access-log line for northbound calls, so
    control_plane_runtime_metrics (app.py) reads ToolGateway.nef_tool_hits instead.
    A successful NEF-backed tool call must be recorded there."""
    lambda_client = _FakeLambdaClient()
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"create_pfd_rule": "pfd-fn"},
        lambda_client=lambda_client,
    )

    result = gateway.call("create_pfd_rule", {"app_id": "app-1"}, valid_event_config, {"eventType": "iot_surge"})

    assert result["status"] == "success"
    assert len(gateway.nef_tool_hits) == 1
    hit = gateway.nef_tool_hits[0]
    assert hit["tool"] == "create_pfd_rule"
    assert hit["protocol"] == "Nnef PFD management"


def test_call_invokes_record_hit_callback_with_execution_id_and_timestamp_on_success(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    """A2: the record_hit callback (wired to EventRepository.record_nef_tool_hit
    in app.py) is how a hit crosses from the async event-execution Lambda
    container into DynamoDB, so control_plane_runtime_metrics in the API
    container can read it back."""
    recorded: list[dict[str, Any]] = []
    lambda_client = _FakeLambdaClient()
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"create_pfd_rule": "pfd-fn"},
        lambda_client=lambda_client,
        record_hit=recorded.append,
    )

    result = gateway.call("create_pfd_rule", {"app_id": "app-1"}, valid_event_config, {"eventType": "iot_surge", "executionId": "exec-42"})

    assert result["status"] == "success"
    assert len(recorded) == 1
    hit = recorded[0]
    assert hit["protocol"] == "Nnef PFD management"
    assert hit["executionId"] == "exec-42"
    assert isinstance(hit["at"], int) and hit["at"] > 0


def test_call_does_not_invoke_record_hit_callback_on_failure(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    recorded: list[dict[str, Any]] = []
    gateway = _gateway(healthy_metrics, baseline_slices, record_hit=recorded.append)  # no lambda configured -> failed

    gateway.call("request_traffic_influence", {}, valid_event_config, {"eventType": "iot_surge"})

    assert recorded == []


def test_call_does_not_record_nef_tool_hit_on_failure(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)  # no lambda configured -> failed

    result = gateway.call("request_traffic_influence", {}, valid_event_config, {"eventType": "iot_surge"})

    assert result["status"] == "failed"
    assert gateway.nef_tool_hits == []


def test_call_does_not_record_nef_tool_hit_for_non_nef_tools(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    gateway = _gateway(healthy_metrics, baseline_slices)

    gateway.call("list_subscribers", {}, valid_event_config, {})

    assert gateway.nef_tool_hits == []


def test_nef_tool_hits_buffer_is_bounded(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    lambda_client = _FakeLambdaClient()
    gateway = _gateway(
        healthy_metrics,
        baseline_slices,
        lambda_function_names={"create_pfd_rule": "pfd-fn"},
        lambda_client=lambda_client,
    )

    for _ in range(ToolGateway.NEF_TOOL_HITS_LIMIT + 5):
        gateway.call("create_pfd_rule", {"app_id": "app-1"}, valid_event_config, {"eventType": "iot_surge"})

    assert len(gateway.nef_tool_hits) == ToolGateway.NEF_TOOL_HITS_LIMIT


def test_get_network_analytics_labels_analytics_source_as_in_app_heuristic(
    valid_event_config,
    healthy_metrics,
    baseline_slices,
) -> None:
    """D: get_network_analytics must not imply a 3GPP NWDAF NF is deployed."""
    gateway = _gateway(healthy_metrics, baseline_slices)

    result = gateway.get_network_analytics({}, valid_event_config, {})

    assert result["analyticsSource"] == "in-app heuristic (Prometheus-derived); not a 3GPP NWDAF NF"
    assert "NWDAF" in result["analyticsSource"]


class TestWaitForSlaMetricsSliceSessionsReadiness:
    """A3: wait_for_sla_metrics must treat the scenario's own SST appearing in
    sliceSessions as a readiness signal, so verify_sla doesn't always burn the
    full timeout while a UE bearer is still being verified."""

    def _gateway_with_metrics_sequence(self, sequence: list[dict[str, Any]]) -> ToolGateway:
        state = {"calls": 0}

        def _current_metrics(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            index = min(state["calls"], len(sequence) - 1)
            state["calls"] += 1
            return dict(sequence[index])

        return ToolGateway(
            metrics=None,
            free5gc=StubFree5gc(),
            environment=StubEnvironment(),
            current_metrics=_current_metrics,
            current_slices=lambda: [],
            runtime_subscriber_upsert_limit=10,
        )

    def test_returns_immediately_when_target_sst_has_sessions(self, valid_event_config) -> None:
        metrics_with_sst_sessions = {
            "throughputMbps": 0,
            "dataSource": "unknown",
            "sliceSessions": {3: 2},
        }
        gateway = self._gateway_with_metrics_sequence([metrics_with_sst_sessions])
        intent = {"eventType": "iot_surge", "sla": {"minThroughputMbps": 2.0}, "targetSlice": {"sst": 3}}

        result = gateway.wait_for_sla_metrics(intent, valid_event_config, timeout_seconds=45)

        assert result == metrics_with_sst_sessions

    def test_scenario_slice_sessions_present_false_when_sst_missing_or_zero(self) -> None:
        assert ToolGateway.scenario_slice_sessions_present({"sliceSessions": {1: 5}}, 3) is False
        assert ToolGateway.scenario_slice_sessions_present({"sliceSessions": {3: 0}}, 3) is False
        assert ToolGateway.scenario_slice_sessions_present({}, 3) is False
        assert ToolGateway.scenario_slice_sessions_present({"sliceSessions": {3: 1}}, None) is False

    def test_scenario_slice_sessions_present_true_when_target_sst_has_sessions(self) -> None:
        assert ToolGateway.scenario_slice_sessions_present({"sliceSessions": {3: 1}}, 3) is True
        # sliceSessions keys may be strings depending on the JSON round-trip.
        assert ToolGateway.scenario_slice_sessions_present({"sliceSessions": {"3": 4}}, 3) is True
