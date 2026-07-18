from __future__ import annotations

from typing import Any

import pytest

import scenario_environment as scenario_environment_module
from scenario_environment import ScenarioEnvironmentService


class FakeK8sClient:
    """Minimal in-memory stand-in for EksKubernetesClient used by ScenarioEnvironmentService."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted_paths: list[str] = []
        self._resources: dict[str, dict[str, Any]] = {}

    def request(self, method: str, path: str, body: Any = None, content_type: str = "application/json", ignore_404: bool = False) -> tuple[int, Any]:
        if method == "GET":
            base = path.split("?")[0]
            if base in self._resources:
                return 200, self._resources[base]
            if base.rstrip("/").split("/")[-1] in ("deployments", "jobs", "pods"):
                return 200, {"items": []}
            return 404, {}
        if method == "POST":
            self.created.append({"path": path, "body": body})
            return 201, body
        return 200, {}

    def create_or_patch(self, create_path: str, patch_path: str, manifest: dict[str, Any]) -> tuple[int, Any]:
        self.created.append({"path": create_path, "body": manifest})
        return 201, manifest

    def patch(self, path: str, body: dict[str, Any], ignore_404: bool = False) -> tuple[int, Any]:
        return 200, body

    def delete(self, path: str, ignore_404: bool = True) -> tuple[int, Any]:
        self.deleted_paths.append(path)
        return 200, {}


def _service() -> ScenarioEnvironmentService:
    return ScenarioEnvironmentService(cluster_name="test-cluster", namespace="free5gc", primary_deployment="ueransim-city-ue")


def test_sanitize_execution_id_strips_illegal_characters_and_truncates() -> None:
    dirty = "Exec:2026/07/04_batch#" + "x" * 80
    sanitized = ScenarioEnvironmentService.sanitize_execution_id(dirty)

    assert len(sanitized) <= 63
    assert all(c.isalnum() or c in "-_." for c in sanitized)


def test_sanitize_execution_id_is_stable_for_already_legal_values() -> None:
    assert ScenarioEnvironmentService.sanitize_execution_id("exec-1234") == "exec-1234"


def test_sanitize_execution_id_truncation_never_ends_with_separator() -> None:
    boundary = "a" * 62 + "-" + "b" * 10
    sanitized = ScenarioEnvironmentService.sanitize_execution_id(boundary)

    assert len(sanitized) <= 63
    assert sanitized[-1].isalnum()


def test_all_illegal_execution_id_yields_no_label_instead_of_empty_value(valid_event_config) -> None:
    assert ScenarioEnvironmentService.sanitize_execution_id("***") == ""

    service = _service()
    manifest = service.ueransim_deployment_manifest(
        "ueransim-iot-surge", "ueransim-ue-config-mmtc", "5", "iot_surge", {}, execution_id="***"
    )

    assert "5gcityverse.io/execution-id" not in manifest["metadata"]["labels"]
    assert "5gcityverse.io/execution-id" not in manifest["spec"]["template"]["metadata"]["labels"]


def test_ueransim_deployment_manifest_includes_execution_id_label_when_provided(valid_event_config) -> None:
    service = _service()

    manifest = service.ueransim_deployment_manifest(
        "ueransim-iot-surge", "ueransim-ue-config-mmtc", "5", "iot_surge", {}, execution_id="exec:abc/123"
    )

    expected = ScenarioEnvironmentService.sanitize_execution_id("exec:abc/123")
    assert manifest["metadata"]["labels"]["5gcityverse.io/execution-id"] == expected
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert pod_labels["5gcityverse.io/execution-id"] == expected


def test_ueransim_deployment_manifest_omits_execution_id_label_when_none(valid_event_config) -> None:
    service = _service()

    manifest = service.ueransim_deployment_manifest(
        "ueransim-iot-surge", "ueransim-ue-config-mmtc", "5", "iot_surge", {}
    )

    assert "5gcityverse.io/execution-id" not in manifest["metadata"]["labels"]
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert "5gcityverse.io/execution-id" not in pod_labels


def test_scenario_ue_request_fits_fragmented_control_plane_capacity(valid_event_config) -> None:
    service = _service()

    manifest = service.scenario_deployment_manifest(
        "iot_surge", valid_event_config, execution_id="exec-capacity"
    )
    ue = next(
        container
        for container in manifest["spec"]["template"]["spec"]["containers"]
        if container["name"] == "ueransim-ue"
    )

    assert ue["resources"]["requests"]["cpu"] == "125m"
    assert ue["resources"]["limits"]["cpu"] == "750m"


def test_ueransim_deployment_manifest_mounts_writable_tmp_with_read_only_root(valid_event_config) -> None:
    service = _service()
    manifest = service.ueransim_deployment_manifest(
        "ueransim-concert", "ueransim-ue-config-concert", "1", "concert", {}
    )

    pod_spec = manifest["spec"]["template"]["spec"]
    ue = next(container for container in pod_spec["containers"] if container["name"] == "ueransim-ue")

    assert ue["securityContext"]["readOnlyRootFilesystem"] is True
    assert {"name": "ue-tmp", "mountPath": "/tmp"} in ue["volumeMounts"]
    assert {"name": "ue-iproute2", "mountPath": "/etc/iproute2"} in ue["volumeMounts"]
    assert {"name": "ue-tmp", "emptyDir": {"sizeLimit": "64Mi"}} in pod_spec["volumes"]
    assert {"name": "ue-iproute2", "emptyDir": {"sizeLimit": "1Mi"}} in pod_spec["volumes"]

    init = pod_spec["initContainers"][0]
    assert init["name"] == "prepare-iproute2"
    assert init["image"] == service.ueransim_image
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert init["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert init["volumeMounts"] == [{"name": "ue-iproute2", "mountPath": "/writable-iproute2"}]


def test_recreate_iperf3_job_labels_deployment_with_sanitized_execution_id(valid_event_config) -> None:
    service = _service()
    k8s = FakeK8sClient()

    service.recreate_iperf3_job(k8s, "iot_surge", valid_event_config, execution_id="exec 42")

    iperf_creations = [c for c in k8s.created if "/deployments" in c["path"] and c["body"].get("metadata", {}).get("name") == "iperf3-iot-surge"]
    assert len(iperf_creations) == 1
    manifest = iperf_creations[0]["body"]
    expected = ScenarioEnvironmentService.sanitize_execution_id("exec 42")
    assert manifest["metadata"]["labels"]["5gcityverse.io/execution-id"] == expected
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert pod_labels["5gcityverse.io/execution-id"] == expected


def test_trigger_passes_execution_id_through_to_manifests(monkeypatch, valid_event_config) -> None:
    service = _service()
    k8s = FakeK8sClient()
    # ensure_core_network needs deployments present for all required components
    k8s._resources[f"/apis/apps/v1/namespaces/{service.namespace}/deployments"] = {
        "items": [
            {"metadata": {"name": f"free5gc-free5gc-{c.lower()}-{c.lower()}"}, "spec": {"replicas": 1}}
            for c in ScenarioEnvironmentService.REQUIRED_CORE_COMPONENTS
        ]
    }
    k8s._resources[f"/api/v1/namespaces/{service.namespace}/pods"] = {
        "items": [
            {
                "metadata": {"name": f"free5gc-{c.lower()}-pod"},
                "status": {"phase": "Running", "podIP": "10.0.0.5"},
            }
            for c in ScenarioEnvironmentService.REQUIRED_CORE_COMPONENTS
        ]
    }

    monkeypatch.setattr(scenario_environment_module, "get_eks_client", lambda cluster_name: k8s)
    monkeypatch.setattr(service, "ensure_upf_singleton", lambda k8s_client: None)
    monkeypatch.setattr(service, "ensure_primary_ue_tun_probe", lambda k8s_client: False)
    monkeypatch.setattr(service, "ensure_nrf_discovery", lambda k8s_client: {"actions": []})
    monkeypatch.setattr(service, "ensure_smf_uses_current_upf", lambda k8s_client: {"actions": []})
    monkeypatch.setattr(service, "apply_ue_configmap", lambda k8s_client, event_type, cfg: None)
    monkeypatch.setattr(service, "gnb_address", lambda k8s_client: "10.0.0.9")
    monkeypatch.setattr(service, "service_cluster_ip", lambda k8s_client, name: "172.20.0.55")
    monkeypatch.setattr(service, "wait_for_ue_bearer", lambda k8s_client, event_type, timeout_seconds=28: "skipped for test")

    captured: dict[str, Any] = {}

    def fake_create_or_patch_deployment(k8s_client, manifest):
        captured["scenario_manifest"] = manifest

    monkeypatch.setattr(service, "create_or_patch_deployment", fake_create_or_patch_deployment)

    result = service.trigger("iot_surge", valid_event_config, execution_id="exec:xyz")

    assert result["status"] == "success"
    expected = ScenarioEnvironmentService.sanitize_execution_id("exec:xyz")
    assert captured["scenario_manifest"]["metadata"]["labels"]["5gcityverse.io/execution-id"] == expected
    containers = captured["scenario_manifest"]["spec"]["template"]["spec"]["containers"]
    assert any(container["name"] == "iperf3-client" and "--bind-dev uesimtun0" in container["command"][2] for container in containers)


def test_cleanup_execution_uses_execution_id_label_selector_and_skips_baseline(valid_event_config) -> None:
    service = _service()
    k8s = FakeK8sClient()
    sanitized = ScenarioEnvironmentService.sanitize_execution_id("exec-99")
    selector_path = f"/apis/apps/v1/namespaces/{service.namespace}/deployments?labelSelector=5gcityverse.io%2Fexecution-id%3D{sanitized}"
    k8s._resources[selector_path.split("?")[0]] = {
        "items": [
            {"metadata": {"name": "ueransim-iot-surge", "labels": {"5gcityverse.io/scenario": "iot_surge"}}},
            {"metadata": {"name": "iperf3-server-baseline", "labels": {"5gcityverse.io/scenario": "baseline"}}},
        ]
    }

    result = service.cleanup_execution(k8s, "exec-99")

    assert result["status"] == "reset"
    assert "ueransim-iot-surge" in result["deleted"]
    assert "iperf3-server-baseline" not in result["deleted"]
    deleted_deployment_paths = [p for p in k8s.deleted_paths if "deployments/ueransim-iot-surge" in p]
    assert len(deleted_deployment_paths) == 1
    assert not any("iperf3-server-baseline" in p for p in k8s.deleted_paths)


def test_ensure_baseline_traffic_creates_resident_mmtc_baseline_deployment(monkeypatch) -> None:
    service = _service()
    k8s = FakeK8sClient()

    monkeypatch.setattr(service, "ensure_core_network", lambda k8s_client: {"actions": []})
    monkeypatch.setattr(service, "ensure_upf_singleton", lambda k8s_client: None)
    monkeypatch.setattr(service, "ensure_primary_ue_tun_probe", lambda k8s_client: False)
    monkeypatch.setattr(service, "ensure_nrf_discovery", lambda k8s_client: {"actions": []})
    monkeypatch.setattr(service, "ensure_smf_uses_current_upf", lambda k8s_client: {"actions": []})
    monkeypatch.setattr(service, "ensure_iperf3_server", lambda k8s_client: None)
    monkeypatch.setattr(service, "ensure_nf_hpas", lambda k8s_client: None)
    monkeypatch.setattr(service, "gnb_address", lambda k8s_client: "10.0.0.9")
    monkeypatch.setattr(service, "cleanup_baseline_iperf3_jobs", lambda k8s_client: 0)

    result = service.ensure_baseline_traffic(k8s)

    assert result["status"] == "success"
    mmtc_creations = [
        c for c in k8s.created
        if c["body"].get("metadata", {}).get("name") == ScenarioEnvironmentService.BASELINE_MMTC_DEPLOYMENT
    ]
    assert len(mmtc_creations) == 1
    manifest = mmtc_creations[0]["body"]
    labels = manifest["metadata"]["labels"]
    assert labels["5gcityverse.io/scenario"].startswith("baseline")
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert pod_labels["5gcityverse.io/scenario"].startswith("baseline")

    configmap_creations = [
        c for c in k8s.created
        if c["body"].get("metadata", {}).get("name") == "ueransim-ue-config-mmtc-baseline"
    ]
    assert len(configmap_creations) == 1

    assert any("ensured resident baseline ueransim-city-mmtc" in action for action in result["actions"])
    assert any("resident UE-TUN sidecar is authoritative" in action for action in result["actions"])


def test_ensure_baseline_traffic_mmtc_deployment_not_in_legacy_cleanup_list() -> None:
    assert ScenarioEnvironmentService.BASELINE_MMTC_DEPLOYMENT not in ScenarioEnvironmentService.LEGACY_BASELINE_DEPLOYMENTS


def test_cleanup_all_event_runtime_does_not_delete_resident_mmtc_baseline() -> None:
    service = _service()
    k8s = FakeK8sClient()

    service.cleanup_all_event_runtime(k8s)

    assert not any(
        ScenarioEnvironmentService.BASELINE_MMTC_DEPLOYMENT in path for path in k8s.deleted_paths
    )


def test_wait_for_deployment_available_rejects_old_available_replica_during_rollout(monkeypatch) -> None:
    service = _service()
    deployment = service.DEDICATED_UPF_DEPLOYMENTS[0]
    path = f"/apis/apps/v1/namespaces/{service.namespace}/deployments/{deployment}"

    class RolloutK8sClient:
        def __init__(self) -> None:
            self.responses = [
                # Old pod is still Available while the new ReplicaSet is only
                # scheduled. This was previously (incorrectly) accepted.
                {
                    "metadata": {"generation": 2},
                    "spec": {"replicas": 1},
                    "status": {
                        "observedGeneration": 2,
                        "replicas": 2,
                        "availableReplicas": 1,
                        "readyReplicas": 1,
                        "updatedReplicas": 1,
                        "unavailableReplicas": 1,
                    },
                },
                # Rollout has converged: only the new replica remains and is ready.
                {
                    "metadata": {"generation": 2},
                    "spec": {"replicas": 1},
                    "status": {
                        "observedGeneration": 2,
                        "replicas": 1,
                        "availableReplicas": 1,
                        "readyReplicas": 1,
                        "updatedReplicas": 1,
                    },
                },
            ]
            self.calls = 0

        def request(self, method: str, request_path: str):
            assert method == "GET"
            assert request_path == path
            response = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return 200, response

    k8s = RolloutK8sClient()
    monkeypatch.setattr(scenario_environment_module.time, "sleep", lambda _seconds: None)

    service.wait_for_deployment_available(k8s, deployment, timeout_seconds=1)

    assert k8s.calls == 2


def test_each_dedicated_upf_is_pinned_to_single_replica(monkeypatch) -> None:
    service = _service()
    calls = []

    monkeypatch.setattr(service, "ensure_cpu_hpa", lambda *args: calls.append(args))
    monkeypatch.setattr(service, "wait_for_deployment_available", lambda *args: calls.append(("wait", args[1])))

    class Client:
        def patch(self, path, body):
            calls.append(("patch", path, body))

    service.ensure_upf_singleton(Client())

    assert len(calls) == len(service.DEDICATED_UPF_DEPLOYMENTS) * 3
    for index, deployment in enumerate(service.DEDICATED_UPF_DEPLOYMENTS):
        hpa, patch_call, wait_call = calls[index * 3 : index * 3 + 3]
        assert hpa[-1] == 1
        assert hpa[2] == deployment
        assert patch_call[1].endswith(f"/deployments/{deployment}")
        assert patch_call[2] == {"spec": {"replicas": 1}}
        assert wait_call == ("wait", deployment)


def test_multi_slice_smf_requires_all_dedicated_pfcp_endpoints() -> None:
    service = _service()

    class Client:
        def request(self, method, path):
            assert method == "GET"
            return 200, {"subsets": [{"addresses": [{"ip": "10.0.0.10"}]}]}

    result = service.ensure_smf_uses_current_upf(Client())
    assert result["actions"] == ["SMF multi-slice topology has four ready PFCP service endpoints"]


def test_multi_slice_smf_fails_closed_when_one_pfcp_endpoint_is_missing() -> None:
    service = _service()
    missing = service.DEDICATED_UPF_SERVICES[-1]

    class Client:
        def request(self, method, path):
            if path.endswith(missing):
                return 200, {"subsets": []}
            return 200, {"subsets": [{"addresses": [{"ip": "10.0.0.10"}]}]}

    with pytest.raises(RuntimeError, match=missing):
        service.ensure_smf_uses_current_upf(Client())


def test_resident_helm_ue_gets_a_tun_probe_without_replacing_its_container(monkeypatch) -> None:
    service = _service()
    path = f"/apis/apps/v1/namespaces/{service.namespace}/deployments/{service.primary_deployment}"

    class Client:
        def __init__(self):
            self.patch_body = None

        def request(self, method, request_path):
            assert method == "GET"
            assert request_path == path
            return 200, {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"meta.helm.sh/release-name": "ueransim-city"}},
                        "spec": {"containers": [{"name": "ue", "image": "free5gc/ueransim:v4.0.1"}]},
                    }
                }
            }

        def patch(self, request_path, body):
            assert request_path == path
            self.patch_body = body

    client = Client()
    monkeypatch.setattr(service, "wait_for_deployment_available", lambda *args: None)

    assert service.ensure_primary_ue_tun_probe(client) is True
    containers = client.patch_body["spec"]["template"]["spec"]["containers"]
    assert [container["name"] for container in containers] == ["ue", "ue-tun-probe"]
    assert client.patch_body["spec"]["template"]["metadata"]["annotations"]["meta.helm.sh/release-name"] == "ueransim-city"
    assert "no_echo_reply" in containers[-1]["args"][0]


class TestUeBearerTimeoutScaling:
    """A1: wait_for_ue_bearer timeout must scale with cfg.ue_count (base 30s + 2s/UE, cap 120s)."""

    def test_single_ue_uses_base_timeout(self) -> None:
        assert ScenarioEnvironmentService.ue_bearer_timeout_seconds(1) == 32

    def test_zero_ue_uses_base_timeout_floor(self) -> None:
        assert ScenarioEnvironmentService.ue_bearer_timeout_seconds(0) == 30

    def test_typhoon_three_ue_scales_up(self) -> None:
        assert ScenarioEnvironmentService.ue_bearer_timeout_seconds(3) == 36

    def test_iot_surge_fifty_ue_is_capped_at_120(self) -> None:
        # base 30 + 2*50 = 130, which must be clamped to the 120s cap.
        assert ScenarioEnvironmentService.ue_bearer_timeout_seconds(50) == 120

    def test_trigger_passes_scaled_timeout_to_wait_for_ue_bearer(self, monkeypatch, valid_event_config) -> None:
        service = _service()
        k8s = FakeK8sClient()
        k8s._resources[f"/apis/apps/v1/namespaces/{service.namespace}/deployments"] = {
            "items": [
                {"metadata": {"name": f"free5gc-free5gc-{c.lower()}-{c.lower()}"}, "spec": {"replicas": 1}}
                for c in ScenarioEnvironmentService.REQUIRED_CORE_COMPONENTS
            ]
        }
        k8s._resources[f"/api/v1/namespaces/{service.namespace}/pods"] = {
            "items": [
                {
                    "metadata": {"name": f"free5gc-{c.lower()}-pod"},
                    "status": {"phase": "Running", "podIP": "10.0.0.5"},
                }
                for c in ScenarioEnvironmentService.REQUIRED_CORE_COMPONENTS
            ]
        }
        monkeypatch.setattr(scenario_environment_module, "get_eks_client", lambda cluster_name: k8s)
        monkeypatch.setattr(service, "ensure_upf_singleton", lambda k8s_client: None)
        monkeypatch.setattr(service, "ensure_primary_ue_tun_probe", lambda k8s_client: False)
        monkeypatch.setattr(service, "ensure_nrf_discovery", lambda k8s_client: {"actions": []})
        monkeypatch.setattr(service, "ensure_smf_uses_current_upf", lambda k8s_client: {"actions": []})
        monkeypatch.setattr(service, "apply_ue_configmap", lambda k8s_client, event_type, cfg: None)
        monkeypatch.setattr(service, "gnb_address", lambda k8s_client: "10.0.0.9")
        monkeypatch.setattr(service, "service_cluster_ip", lambda k8s_client, name: "172.20.0.55")
        monkeypatch.setattr(service, "create_or_patch_deployment", lambda k8s_client, manifest: None)
        monkeypatch.setattr(service, "recreate_iperf3_job", lambda k8s_client, event_type, cfg, execution_id=None: None)

        captured: dict[str, Any] = {}

        def fake_wait_for_ue_bearer(k8s_client, event_type, timeout_seconds=28):
            captured["timeout_seconds"] = timeout_seconds
            return "ok"

        monkeypatch.setattr(service, "wait_for_ue_bearer", fake_wait_for_ue_bearer)

        # valid_event_config fixture has ue_count=5 -> base 30 + 2*5 = 40.
        result = service.trigger("iot_surge", valid_event_config, execution_id="exec:scale")

        assert result["status"] == "success"
        assert captured["timeout_seconds"] == ScenarioEnvironmentService.ue_bearer_timeout_seconds(valid_event_config.ue_count)
        assert captured["timeout_seconds"] == 40

        def fail_if_waited(*_args, **_kwargs):
            raise AssertionError("batch launch must not block on per-scenario bearer verification")

        monkeypatch.setattr(service, "wait_for_ue_bearer", fail_if_waited)
        batch_result = service.trigger(
            "iot_surge",
            valid_event_config,
            execution_id="exec:batch",
            wait_for_bearer=False,
        )

        assert batch_result["status"] == "success"
        assert "delegated bearer verification" in batch_result["actions"][-1]
