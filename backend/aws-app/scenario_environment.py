from __future__ import annotations

import traceback
import uuid
import time
import urllib.parse
import re
from typing import Any

from config import UE_CONFIG_MAPS
from eks_kubernetes_client import EksKubernetesClient, get_eks_client
from models import EventConfig
from time_utils import TimeUtils


class ScenarioEnvironmentService:
    REQUIRED_CORE_COMPONENTS = ("AMF", "SMF", "UPF", "NEF", "PCF", "NSSF", "CHF")
    LEGACY_BASELINE_DEPLOYMENTS = ("ueransim-baseline-mmtc", "ueransim-iot")
    BASELINE_MMTC_DEPLOYMENT = "ueransim-city-mmtc"
    DEDICATED_UPF_DEPLOYMENTS = (
        "upf-embb-free5gc-upf-upf-embb",
        "upf-urllc-free5gc-upf-upf-urllc",
        "upf-mmtc-free5gc-upf-upf-mmtc",
        "upf-v2x-free5gc-upf-upf-v2x",
    )
    DEDICATED_UPF_SERVICES = (
        "upf-embb-free5gc-upf-upf-embb-service",
        "upf-urllc-free5gc-upf-upf-urllc-service",
        "upf-mmtc-free5gc-upf-upf-mmtc-service",
        "upf-v2x-free5gc-upf-upf-v2x-service",
    )
    NRF_DISCOVERY_TARGETS = {
        "AMF": ("SMF", "free5gc-free5gc-amf-amf"),
        "AUSF": ("AMF", "free5gc-free5gc-ausf-ausf"),
        "UDM": ("AUSF", "free5gc-free5gc-udm-udm"),
        "UDR": ("UDM", "free5gc-free5gc-udr-udr"),
        "SMF": ("AMF", "free5gc-free5gc-smf-smf"),
        "CHF": ("SMF", "free5gc-free5gc-chf-chf"),
        "NSSF": ("AMF", "free5gc-free5gc-nssf-nssf"),
        "PCF": ("SMF", "free5gc-free5gc-pcf-pcf"),
    }

    def __init__(
        self,
        cluster_name: str,
        namespace: str,
        primary_deployment: str,
        ueransim_image: str = "free5gc/ueransim@sha256:4a6745b0c9f0c60173833f8bef89816324e84636e220917bdc682555a299e8ba",
        iperf3_image: str = "networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f",
    ) -> None:
        self.cluster_name = cluster_name
        self.namespace = namespace
        self.primary_deployment = primary_deployment
        self.ueransim_image = ueransim_image
        self.iperf3_image = iperf3_image

    def trigger(
        self,
        event_type: str,
        cfg: EventConfig,
        execution_id: str | None = None,
        wait_for_bearer: bool = True,
    ) -> dict[str, Any]:
        if not self.cluster_name:
            return {"status": "skipped", "reason": "EKS_CLUSTER_NAME is not configured"}

        actions = []
        try:
            k8s = get_eks_client(self.cluster_name)
            core_state = self.ensure_core_network(k8s)
            actions.extend(core_state["actions"])
            if self.ensure_primary_ue_tun_probe(k8s):
                actions.append("attached UE-TUN quality probe to the resident citizen UE")
            self.ensure_upf_singleton(k8s)
            actions.append("verified four dedicated slice UPFs at one replica before bearer setup")
            nrf_state = self.ensure_nrf_discovery(k8s)
            actions.extend(nrf_state["actions"])
            upf_state = self.ensure_smf_uses_current_upf(k8s)
            actions.extend(upf_state["actions"])
            self.ensure_iperf3_server(k8s)
            scenario_server = self.ensure_scenario_iperf3_server(k8s, event_type)
            scenario_server_ip = self.service_cluster_ip(k8s, scenario_server)
            self.ensure_nf_hpas(k8s)
            actions.append("ensured iperf3 servers and safe NF HPA bounds (UPF singleton)")
            self.apply_ue_configmap(k8s, event_type, cfg)

            self.scale_primary_ueransim(k8s, 1)
            self.create_or_patch_deployment(k8s, self.scenario_deployment_manifest(event_type, cfg, execution_id, scenario_server_ip))
            actions.append(f"started {event_type} UERANSIM deployment alongside resident baseline")
            actions.append(f"launched TUN-bound iperf3 sidecar for {event_type}")
            if wait_for_bearer:
                actions.append(self.wait_for_ue_bearer(k8s, event_type, timeout_seconds=self.ue_bearer_timeout_seconds(cfg.ue_count)))
            else:
                actions.append("delegated bearer verification to the shared batch traffic observer")
            return {"status": "success", "actions": actions, "httpStatus": 200}
        except Exception as exc:
            error = traceback.format_exc()
            print(error)
            return {"status": "error", "error": str(exc), "trace": error[-2000:], "actions": actions, "httpStatus": 500}

    @staticmethod
    def sanitize_execution_id(execution_id: str) -> str:
        """Sanitize a value for use as a Kubernetes label value (<=63 chars, [A-Za-z0-9_.-])."""
        sanitized = re.sub(r"[^A-Za-z0-9_.-]", "-", execution_id).strip("-_.")
        return sanitized[:63].rstrip("-_.")

    def ensure_baseline_traffic(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        actions: list[str] = []
        core_state = self.ensure_core_network(k8s)
        actions.extend(core_state["actions"])
        self.ensure_upf_singleton(k8s)
        actions.append("pinned discovered Multus UPF deployment to one replica before baseline setup")
        nrf_state = self.ensure_nrf_discovery(k8s)
        actions.extend(nrf_state["actions"])
        upf_state = self.ensure_smf_uses_current_upf(k8s)
        actions.extend(upf_state["actions"])
        self.ensure_iperf3_server(k8s)
        self.ensure_nf_hpas(k8s)
        self.cleanup_iperf3_jobs(k8s)
        for name in self.LEGACY_BASELINE_DEPLOYMENTS:
            self.delete_deployment(k8s, name)
            actions.append(f"removed legacy non-resident baseline {name}")
        for event_type in UE_CONFIG_MAPS:
            deployment_name = f"ueransim-{event_type.replace('_', '-')}"
            server_name = f"iperf3-server-{event_type.replace('_', '-')}"
            self.delete_deployment(k8s, deployment_name)
            self.delete_deployment(k8s, server_name)
            k8s.delete(f"/api/v1/namespaces/{self.namespace}/services/{server_name}", ignore_404=True)
            actions.append(f"removed stale event runtime {deployment_name}/{server_name}")
        self.scale_primary_ueransim(k8s, 1)
        actions.append(f"ensured resident baseline {self.primary_deployment} count=1")
        if self.ensure_primary_ue_tun_probe(k8s):
            actions.append("attached UE-TUN quality probe to the resident citizen UE")
        self.ensure_baseline_mmtc_configmap(k8s)
        self.create_or_patch_deployment(k8s, self.baseline_mmtc_deployment_manifest())
        actions.append(f"ensured resident baseline {self.BASELINE_MMTC_DEPLOYMENT} count=1")
        # The resident UE sidecar is the only authoritative citizen baseline.
        # A standalone pod-to-Service Job races for iperf3's single test slot and
        # can both starve the TUN-bound client and misrepresent cluster traffic as
        # UE -> gNB -> UPF traffic. Remove any legacy Jobs instead of recreating
        # them during each metrics reconciliation.
        removed_baseline_jobs = self.cleanup_baseline_iperf3_jobs(k8s)
        actions.append(
            f"resident UE-TUN sidecar is authoritative; removed {removed_baseline_jobs} legacy baseline job(s)"
        )
        return {"status": "success", "actions": actions}

    def cleanup_baseline_iperf3_jobs(self, k8s: EksKubernetesClient) -> int:
        selector = urllib.parse.quote(
            "app.kubernetes.io/component=iperf3,5gcityverse.io/scenario=baseline",
            safe="",
        )
        status, data = k8s.request(
            "GET",
            f"/apis/batch/v1/namespaces/{self.namespace}/jobs?labelSelector={selector}",
            ignore_404=True,
        )
        removed = 0
        if status < 300 and isinstance(data, dict):
            for job in data.get("items", []):
                name = ((job.get("metadata") or {}).get("name") or "")
                if name:
                    k8s.delete(
                        f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}?propagationPolicy=Background",
                        ignore_404=True,
                    )
                    removed += 1
        return removed

    def ensure_baseline_mmtc_configmap(self, k8s: EksKubernetesClient) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ueransim-ue-config-mmtc-baseline", "namespace": self.namespace},
            "data": {"ue-config.yaml": self.baseline_mmtc_config_yaml(k8s)},
        }
        k8s.create_or_patch(
            f"/api/v1/namespaces/{self.namespace}/configmaps",
            f"/api/v1/namespaces/{self.namespace}/configmaps/ueransim-ue-config-mmtc-baseline",
            manifest,
        )

    def baseline_mmtc_config_yaml(self, k8s: EksKubernetesClient) -> str:
        gnb_address = self.gnb_address(k8s)
        return f"""supi: 'imsi-208930000000200'
mcc: '208'
mnc: '93'
protectionScheme: 0
homeNetworkPublicKey: '5a8d38864820197c3394b92613b20b91633cbd897119273bf8e4a6f4eec0a650'
homeNetworkPublicKeyId: 1
routingIndicator: '0000'
key: '8baf473f2f8fd09487cccbd7097c6862'
op: '8e27b6af0e692e750f32667a3b14605d'
opType: 'OPC'
amf: '8000'
imei: '356938035643920'
imeiSv: '4370816125816200'
gnbSearchList:
  - {gnb_address}
uacAic:
  mps: false
  mcs: false
uacAcc:
  normalClass: 0
  class11: false
  class12: false
  class13: false
  class14: false
  class15: false
sessions:
  - type: 'IPv4'
    apn: 'iot'
    slice:
      sst: 3
      sd: '000004'
configured-nssai:
  - sst: 3
    sd: '000004'
default-nssai:
  - sst: 3
    sd: '000004'
integrity:
  IA1: true
  IA2: true
  IA3: true
ciphering:
  EA1: true
  EA2: true
  EA3: true
integrityMaxRate:
  uplink: "full"
  downlink: "full"
"""

    def baseline_mmtc_deployment_manifest(self) -> dict[str, Any]:
        manifest = self.ueransim_deployment_manifest(
            self.BASELINE_MMTC_DEPLOYMENT,
            "ueransim-ue-config-mmtc-baseline",
            "1",
            "baseline-mmtc",
            {},
        )
        annotations = manifest["spec"]["template"]["metadata"].setdefault("annotations", {})
        annotations["5gcityverse.io/config-revision"] = "baseline-mmtc-iot-v3"
        return manifest

    def ensure_core_network(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        status, data = k8s.request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments")
        if status >= 300:
            raise RuntimeError(f"Kubernetes deployment discovery failed: HTTP {status} {data}")

        deployments = data.get("items", []) if isinstance(data, dict) else []
        by_component: dict[str, list[dict[str, Any]]] = {component: [] for component in self.REQUIRED_CORE_COMPONENTS}
        for deployment in deployments:
            component = self.component_from_resource(deployment)
            if component in by_component:
                by_component[component].append(deployment)

        missing = [component for component, items in by_component.items() if not items]
        if missing:
            raise RuntimeError(
                "free5GC runtime is not installed or is missing deployments: "
                + ", ".join(missing)
                + f". Run scripts/start.sh or scripts/deploy.sh before triggering real traffic."
            )

        actions: list[str] = []
        for component, items in by_component.items():
            for deployment in items:
                metadata = deployment.get("metadata") or {}
                spec = deployment.get("spec") or {}
                name = metadata.get("name")
                replicas = int(spec.get("replicas") or 0)
                if not name or replicas >= 1:
                    continue
                k8s.patch(
                    f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
                    {"spec": {"replicas": 1}},
                )
                actions.append(f"scaled {component} deployment {name} to 1 replica")

        pod_status, pod_data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods")
        if pod_status < 300 and isinstance(pod_data, dict):
            running = self.running_core_counts(pod_data.get("items", []))
            zero = [component for component in self.REQUIRED_CORE_COMPONENTS if running.get(component, 0) == 0]
            if zero:
                actions.append("waiting for core pods to become running: " + ", ".join(zero))

        return {"actions": actions or ["free5GC core deployments already have replicas >= 1"]}

    def ensure_nrf_discovery(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        actions: list[str] = []
        repaired: list[str] = []
        for target, (requester, deployment) in self.NRF_DISCOVERY_TARGETS.items():
            if self.nrf_has_instances(k8s, target, requester):
                continue
            self.restart_deployment(k8s, deployment)
            repaired.append(target)
            actions.append(f"restarted {deployment}; NRF had no {target} profile for {requester}")

        for target in repaired:
            deployment = self.NRF_DISCOVERY_TARGETS[target][1]
            self.wait_for_deployment_available(k8s, deployment)

        profile_repairs = self.ensure_nrf_profile_documents(k8s)
        actions.extend(profile_repairs)

        for target, (requester, _) in self.NRF_DISCOVERY_TARGETS.items():
            if not self.nrf_has_instances(k8s, target, requester):
                raise RuntimeError(f"NRF discovery still has no {target} profile for requester {requester}")

        return {"actions": actions or ["NRF discovery has AMF/AUSF/UDM/UDR/SMF/NSSF/PCF profiles"]}

    def ensure_nrf_profile_documents(self, k8s: EksKubernetesClient) -> list[str]:
        actions: list[str] = []
        for target, requester in (("UDM", "AUSF"), ("UDR", "UDM")):
            if self.nrf_has_instances(k8s, target, requester):
                continue
            instance_id = self.nrf_instance_id(k8s, target) or str(uuid.uuid4())
            profile = self.nrf_profile_document(target, instance_id)
            status, data = k8s.request(
                "PUT",
                f"/api/v1/namespaces/{self.namespace}/services/http:nrf-nnrf:8000/proxy"
                f"/nnrf-nfm/v1/nf-instances/{instance_id}",
                profile,
            )
            if status >= 300:
                raise RuntimeError(f"Failed to repair NRF {target} profile: HTTP {status} {data}")
            actions.append(f"repaired NRF {target} profile document {instance_id}")
        return actions

    def nrf_instance_id(self, k8s: EksKubernetesClient, target: str) -> str | None:
        status, data = k8s.request(
            "GET",
            f"/api/v1/namespaces/{self.namespace}/services/http:nrf-nnrf:8000/proxy"
            f"/nnrf-nfm/v1/nf-instances?nf-type={target}&limit=20",
        )
        if status >= 300 or not isinstance(data, dict):
            return None
        for item in ((data.get("_link") or {}).get("item") or []):
            href = item.get("href") if isinstance(item, dict) else None
            if href:
                return str(href).rstrip("/").split("/")[-1]
        return None

    def nrf_profile_document(self, target: str, instance_id: str) -> dict[str, Any]:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))
        service_by_target = {
            "UDM": ("free5gc-free5gc-udm-service", ["nudm-sdm", "nudm-uecm", "nudm-ueau", "nudm-ee", "nudm-pp"]),
            "UDR": ("free5gc-free5gc-udr-service", ["nudr-dr"]),
        }
        host, service_names = service_by_target[target]

        def service(name: str) -> dict[str, Any]:
            return {
                "serviceInstanceId": instance_id + name,
                "serviceName": name,
                "versions": [
                    {
                        "apiVersionInUri": "v1",
                        "apiFullVersion": f"http://{host}:8080/{name}/v1",
                        "expiry": expires_at,
                    }
                ],
                "scheme": "http",
                "nfServiceStatus": "REGISTERED",
                "ipEndPoints": [{"ipv4Address": host, "port": 8080, "transport": "TCP"}],
                "apiPrefix": f"http://{host}:8080",
            }

        profile: dict[str, Any] = {
            "nfInstanceId": instance_id,
            "nfType": target,
            "nfStatus": "REGISTERED",
            "heartBeatTimer": 60,
            "ipv4Addresses": [host],
            "plmnList": [{"mcc": "208", "mnc": "93"}],
            "customInfo": {"oauth2": False},
            "nfServices": [service(name) for name in service_names],
        }
        if target == "UDM":
            profile["udmInfo"] = {"supiRanges": [{"start": "208930000000000", "end": "208930000000999"}]}
        if target == "UDR":
            profile["udrInfo"] = {
                "groupId": "udrGroup001",
                "supiRanges": [{"start": "208930000000000", "end": "208930000000999"}],
            }
        return profile

    def ensure_smf_uses_current_upf(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        missing: list[str] = []
        for service in self.DEDICATED_UPF_SERVICES:
            status, data = k8s.request(
                "GET", f"/api/v1/namespaces/{self.namespace}/endpoints/{service}"
            )
            addresses = []
            if status < 300 and isinstance(data, dict):
                for subset in data.get("subsets") or []:
                    addresses.extend(subset.get("addresses") or [])
            if not addresses:
                missing.append(service)
        if missing:
            raise RuntimeError("Dedicated UPF PFCP endpoints unavailable: " + ", ".join(missing))
        return {"actions": ["SMF multi-slice topology has four ready PFCP service endpoints"]}

    def current_pod_ip(self, k8s: EksKubernetesClient, component: str) -> str | None:
        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods")
        if status >= 300 or not isinstance(data, dict):
            return None
        candidates: list[dict[str, Any]] = []
        for pod in data.get("items", []):
            metadata = pod.get("metadata") or {}
            if metadata.get("deletionTimestamp"):
                continue
            pod_status = pod.get("status") or {}
            if pod_status.get("phase") != "Running":
                continue
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod_status.get("conditions") or []
            )
            if ready and self.component_from_resource(pod) == component and pod_status.get("podIP"):
                candidates.append(pod)
        candidates.sort(key=lambda pod: str((pod.get("metadata") or {}).get("creationTimestamp") or ""), reverse=True)
        if candidates:
            return str((candidates[0].get("status") or {}).get("podIP"))
        return None

    def nrf_has_instances(self, k8s: EksKubernetesClient, target: str, requester: str) -> bool:
        path = (
            f"/api/v1/namespaces/{self.namespace}/services/http:nrf-nnrf:8000/proxy"
            f"/nnrf-disc/v1/nf-instances?target-nf-type={target}&requester-nf-type={requester}"
        )
        status, data = k8s.request("GET", path)
        if status >= 300 or not isinstance(data, dict):
            return False
        instances = data.get("nfInstances")
        return isinstance(instances, list) and len(instances) > 0

    def restart_deployment(self, k8s: EksKubernetesClient, name: str) -> None:
        k8s.patch(
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "5gcityverse.io/nrf-repair-restarted-at": TimeUtils.now(),
                                "5gcityverse.io/nrf-repair-revision": str(uuid.uuid4()),
                            }
                        }
                    }
                }
            },
        )

    def wait_for_deployment_available(self, k8s: EksKubernetesClient, name: str, timeout_seconds: int = 45) -> None:
        deadline = time.time() + timeout_seconds
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"
        while time.time() < deadline:
            status, data = k8s.request("GET", path)
            if status < 300 and isinstance(data, dict):
                metadata = data.get("metadata") or {}
                spec = data.get("spec") or {}
                deployment_status = data.get("status") or {}
                desired = int(spec.get("replicas") or 1)
                generation = int(metadata.get("generation") or 0)
                observed_generation = int(deployment_status.get("observedGeneration") or 0)
                replicas = int(deployment_status.get("replicas") or 0)
                available = int(deployment_status.get("availableReplicas") or 0)
                ready = int(deployment_status.get("readyReplicas") or 0)
                updated = int(deployment_status.get("updatedReplicas") or 0)
                unavailable = int(deployment_status.get("unavailableReplicas") or 0)
                # During a rolling restart the old replica can remain Available while
                # the new ReplicaSet already counts as Updated but is not Ready yet.
                # Treating that overlap as complete lets the next scenario restart
                # UPF/SMF again and launches UEs against a transient PFCP/NAS state.
                # Require the controller to observe the requested generation and the
                # rollout to converge to exactly the desired, ready replica set.
                if (
                    observed_generation >= generation
                    and replicas == desired
                    and available == desired
                    and ready == desired
                    and updated == desired
                    and unavailable == 0
                ):
                    return
            time.sleep(2)
        raise RuntimeError(f"Deployment {name} rollout did not converge before timeout")

    def ensure_iperf3_server(self, k8s: EksKubernetesClient) -> None:
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "iperf3-server", "namespace": self.namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "iperf3-server"}},
                "template": {
                    "metadata": {"labels": {"app": "iperf3-server"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "iperf3",
                                "image": "networkstatic/iperf3:latest",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["iperf3", "-s", "-p", "5201"],
                                "ports": [
                                    {"name": "iperf3-tcp", "containerPort": 5201, "protocol": "TCP"},
                                    {"name": "iperf3-udp", "containerPort": 5201, "protocol": "UDP"},
                                ],
                            }
                        ]
                    },
                },
            },
        }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "iperf3-server", "namespace": self.namespace},
            "spec": {
                "selector": {"app": "iperf3-server"},
                "ports": [
                    {"name": "iperf3-tcp", "port": 5201, "targetPort": 5201, "protocol": "TCP"},
                    {"name": "iperf3-udp", "port": 5201, "targetPort": 5201, "protocol": "UDP"},
                ],
            },
        }
        k8s.create_or_patch(f"/apis/apps/v1/namespaces/{self.namespace}/deployments", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/iperf3-server", deployment)
        k8s.create_or_patch(f"/api/v1/namespaces/{self.namespace}/services", f"/api/v1/namespaces/{self.namespace}/services/iperf3-server", service)

    def ensure_scenario_iperf3_server(self, k8s: EksKubernetesClient, event_type: str) -> str:
        safe_event = event_type.replace("_", "-")
        name = f"iperf3-server-{safe_event}"
        labels = {
            "app": name,
            "app.kubernetes.io/component": "iperf3-server",
            "app.kubernetes.io/part-of": "5gcityverse",
            "5gcityverse.io/scenario": event_type,
        }
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "iperf3-server",
                                "image": "networkstatic/iperf3:latest",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["iperf3", "-s", "-p", "5201"],
                                "ports": [
                                    {"name": "iperf3-tcp", "containerPort": 5201, "protocol": "TCP"},
                                    {"name": "iperf3-udp", "containerPort": 5201, "protocol": "UDP"},
                                ],
                            }
                        ]
                    },
                },
            },
        }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "spec": {
                "selector": {"app": name},
                "ports": [
                    {"name": "iperf3-tcp", "port": 5201, "targetPort": 5201, "protocol": "TCP"},
                    {"name": "iperf3-udp", "port": 5201, "targetPort": 5201, "protocol": "UDP"},
                ],
            },
        }
        k8s.create_or_patch(
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments",
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            deployment,
        )
        k8s.create_or_patch(f"/api/v1/namespaces/{self.namespace}/services", f"/api/v1/namespaces/{self.namespace}/services/{name}", service)
        return name

    def service_cluster_ip(self, k8s: EksKubernetesClient, name: str) -> str:
        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/services/{name}")
        address = ((data.get("spec") or {}).get("clusterIP") if isinstance(data, dict) else None)
        if status >= 300 or not address or address == "None":
            raise RuntimeError(f"Kubernetes service {name} has no routable ClusterIP")
        return str(address)

    def ensure_nf_hpas(self, k8s: EksKubernetesClient) -> None:
        targets = {
            # AMF/SMF keep UE and PDU session context in-process in this lab.
            "AMF": ("free5gc-free5gc-amf-amf", "amf-hpa", "25m", 1),
            "SMF": ("free5gc-free5gc-smf-smf", "smf-hpa", "25m", 1),
            "PCF": ("free5gc-free5gc-pcf-pcf", "pcf-hpa", "20m", 3),
            "NEF": ("free5gc-free5gc-nef-nef", "nef-hpa", "20m", 3),
        }
        for component, (deployment, hpa_name, cpu_target, max_replicas) in targets.items():
            self.ensure_cpu_hpa(k8s, component, deployment, hpa_name, cpu_target, max_replicas)
        for deployment in self.DEDICATED_UPF_DEPLOYMENTS:
            suffix = deployment.split("-")[1]
            self.ensure_cpu_hpa(k8s, "UPF", deployment, f"upf-{suffix}-hpa", "50m", 1)

    def ensure_upf_hpa(self, k8s: EksKubernetesClient) -> None:
        self.ensure_upf_singleton(k8s)

    def ensure_upf_singleton(self, k8s: EksKubernetesClient) -> None:
        for deployment in self.DEDICATED_UPF_DEPLOYMENTS:
            suffix = deployment.split("-")[1]
            self.ensure_cpu_hpa(k8s, "UPF", deployment, f"upf-{suffix}-hpa", "50m", 1)
            k8s.patch(
                f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{deployment}",
                {"spec": {"replicas": 1}},
            )
            self.wait_for_deployment_available(k8s, deployment)

    def ensure_cpu_hpa(self, k8s: EksKubernetesClient, component: str, deployment: str, hpa_name: str, cpu_target: str, max_replicas: int) -> None:
        hpa = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": hpa_name,
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/component": component.lower(),
                    "app.kubernetes.io/part-of": "5gcityverse",
                },
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment,
                },
                "minReplicas": 1,
                "maxReplicas": max_replicas,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "AverageValue",
                                "averageValue": cpu_target,
                            },
                        },
                    }
                ],
                "behavior": {
                    "scaleUp": {
                        "stabilizationWindowSeconds": 30,
                        "policies": [
                            {"type": "Percent", "value": 100, "periodSeconds": 60},
                            {"type": "Pods", "value": 1, "periodSeconds": 60},
                        ],
                        "selectPolicy": "Max",
                    },
                    "scaleDown": {
                        "stabilizationWindowSeconds": 300,
                        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 120}],
                    },
                },
            },
        }
        k8s.create_or_patch(
            f"/apis/autoscaling/v2/namespaces/{self.namespace}/horizontalpodautoscalers",
            f"/apis/autoscaling/v2/namespaces/{self.namespace}/horizontalpodautoscalers/{hpa_name}",
            hpa,
        )

    def apply_ue_configmap(self, k8s: EksKubernetesClient, event_type: str, cfg: EventConfig) -> None:
        name = UE_CONFIG_MAPS[event_type]
        gnb_address = self.gnb_address(k8s)
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace},
            "data": {"ue-config.yaml": self.ue_config_yaml(event_type, cfg, gnb_address)},
        }
        k8s.create_or_patch(
            f"/api/v1/namespaces/{self.namespace}/configmaps",
            f"/api/v1/namespaces/{self.namespace}/configmaps/{name}",
            manifest,
        )

    def gnb_address(self, k8s: EksKubernetesClient) -> str:
        service_status, service_data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/services/gnb-service")
        if service_status < 300 and isinstance(service_data, dict):
            return f"gnb-service.{self.namespace}.svc.cluster.local"

        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods")
        if status >= 300:
            raise RuntimeError(f"Kubernetes gNB discovery failed: HTTP {status} {data}")

        for pod in data.get("items", []) if isinstance(data, dict) else []:
            metadata = pod.get("metadata") or {}
            status_data = pod.get("status") or {}
            name = (metadata.get("name") or "").lower()
            labels = metadata.get("labels") or {}
            label_text = " ".join(str(value).lower() for value in labels.values())
            is_gnb = "gnb" in f"{name} {label_text}"
            pod_ip = status_data.get("podIP")
            if is_gnb and status_data.get("phase") == "Running" and pod_ip:
                return pod_ip

        raise RuntimeError("UERANSIM gNB pod is not running; cannot build real UE config")

    def ue_config_yaml(self, event_type: str, cfg: EventConfig, gnb_address: str) -> str:
        supi = cfg.ue_ids[0]
        suffix = cfg.imsi_suffix[-1:]
        imei = f"35693803564380{suffix}"
        imeisv = f"437081612581615{suffix}"
        return f"""supi: '{supi}'
mcc: '208'
mnc: '93'
protectionScheme: 0
homeNetworkPublicKey: '5a8d38864820197c3394b92613b20b91633cbd897119273bf8e4a6f4eec0a650'
homeNetworkPublicKeyId: 1
routingIndicator: '0000'
key: '8baf473f2f8fd09487cccbd7097c6862'
op: '8e27b6af0e692e750f32667a3b14605d'
opType: 'OPC'
amf: '8000'
imei: '{imei}'
imeiSv: '{imeisv}'
gnbSearchList:
  - {gnb_address}
uacAic:
  mps: false
  mcs: false
uacAcc:
  normalClass: 0
  class11: false
  class12: false
  class13: false
  class14: false
  class15: false
sessions:
  - type: 'IPv4'
    apn: '{cfg.dnn}'
    slice:
      sst: {cfg.slice_sst}
      sd: '{cfg.slice_sd}'
configured-nssai:
  - sst: {cfg.slice_sst}
    sd: '{cfg.slice_sd}'
default-nssai:
  - sst: {cfg.slice_sst}
    sd: '{cfg.slice_sd}'
integrity:
  IA1: true
  IA2: true
  IA3: true
ciphering:
  EA1: true
  EA2: true
  EA3: true
integrityMaxRate:
  uplink: "full"
  downlink: "full"
"""

    def scale_primary_ueransim(self, k8s: EksKubernetesClient, replicas: int) -> None:
        k8s.patch(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.primary_deployment}", {"spec": {"replicas": replicas}}, ignore_404=True)

    def ensure_primary_ue_tun_probe(self, k8s: EksKubernetesClient) -> bool:
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.primary_deployment}"
        status, deployment = k8s.request("GET", path)
        if status >= 300 or not isinstance(deployment, dict):
            raise RuntimeError(f"Resident UE deployment {self.primary_deployment} is unavailable")
        template = ((deployment.get("spec") or {}).get("template") or {})
        pod_spec = template.get("spec") or {}
        containers = [dict(container) for container in (pod_spec.get("containers") or [])]
        annotations = dict((template.get("metadata") or {}).get("annotations") or {})
        probe_version = "2"
        if any(container.get("name") == "ue-tun-probe" for container in containers) and annotations.get("5gcityverse.io/ue-tun-probe-version") == probe_version:
            return False
        containers = [container for container in containers if container.get("name") != "ue-tun-probe"]
        containers.append({
            "name": "ue-tun-probe",
            "image": "busybox:1.36",
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c"],
            "args": [self.ue_tun_probe_script()],
        })
        annotations["5gcityverse.io/ue-tun-probe-version"] = probe_version
        k8s.patch(path, {"spec": {"template": {"metadata": {"annotations": annotations}, "spec": {"containers": containers}}}})
        self.wait_for_deployment_available(k8s, self.primary_deployment)
        return True

    def primary_ueransim_manifest(self, event_type: str, config_map_name: str) -> dict[str, Any]:
        return self.ueransim_deployment_manifest(self.primary_deployment, config_map_name, "1", event_type, {})

    def create_or_patch_deployment(self, k8s: EksKubernetesClient, manifest: dict[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        create_path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments"
        patch_path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"
        status, data = k8s.request("POST", create_path, manifest)
        if status == 409:
            existing_status, existing = k8s.request("GET", patch_path)
            labels = ((existing.get("metadata") or {}).get("labels") or {}) if existing_status < 300 and isinstance(existing, dict) else {}
            if labels.get("app.kubernetes.io/managed-by") == "Helm":
                raise RuntimeError(
                    f"Refusing to patch Helm-managed deployment {name}; use a scenario-specific "
                    "5GCityVerse deployment name instead."
                )
            k8s.patch(patch_path, self.deployment_runtime_patch(manifest))
            return
        if status >= 300:
            raise RuntimeError(f"Kubernetes deployment create failed: HTTP {status} {data}")

    @staticmethod
    def deployment_runtime_patch(manifest: dict[str, Any]) -> dict[str, Any]:
        spec = manifest.get("spec") or {}
        template = spec.get("template") or {}
        return {
            "metadata": {"labels": (manifest.get("metadata") or {}).get("labels") or {}},
            "spec": {
                "replicas": spec.get("replicas", 1),
                "template": {
                    "metadata": {"annotations": ((template.get("metadata") or {}).get("annotations") or {})},
                    "spec": (template.get("spec") or {}),
                },
            },
        }

    def delete_deployment(self, k8s: EksKubernetesClient, name: str) -> None:
        k8s.delete(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}")

    def scenario_deployment_manifest(self, event_type: str, cfg: EventConfig, execution_id: str | None = None, server_address: str | None = None) -> dict[str, Any]:
        deployment_name = f"ueransim-{event_type.replace('_', '-')}"
        if event_type == "typhoon":
            manifest = self.ueransim_deployment_manifest(deployment_name, "ueransim-ue-config-typhoon", "1", event_type, {}, execution_id)
        elif event_type == "iot_surge":
            resources = {"requests": {"cpu": "125m", "memory": "256Mi"}, "limits": {"cpu": "750m", "memory": "768Mi"}}
            # One real representative UE carries the scale-adjusted aggregate
            # traffic. Starting 50 identical UEs with a single SUPI caused SM
            # context collisions and made the measured round nondeterministic.
            manifest = self.ueransim_deployment_manifest(deployment_name, "ueransim-ue-config-mmtc", "1", event_type, resources, execution_id)
        else:
            resources = {"requests": {"cpu": "125m", "memory": "256Mi"}, "limits": {"cpu": "750m", "memory": "768Mi"}}
            manifest = self.ueransim_deployment_manifest(deployment_name, UE_CONFIG_MAPS[event_type], "1", event_type, resources, execution_id)
        evidence_annotations = {
            "5gcityverse.io/execution-id": str(execution_id or ""),
            "5gcityverse.io/sst": str(cfg.slice_sst),
            "5gcityverse.io/sd": str(cfg.slice_sd),
            "5gcityverse.io/dnn": str(cfg.dnn),
        }
        manifest["metadata"]["annotations"] = evidence_annotations
        manifest["spec"]["template"]["metadata"]["annotations"].update(evidence_annotations)
        return self.attach_tun_traffic(manifest, event_type, cfg, server_address)

    def attach_tun_traffic(self, manifest: dict[str, Any], event_type: str, cfg: EventConfig, server_address: str | None = None) -> dict[str, Any]:
        server = server_address or f"iperf3-server-{event_type.replace('_', '-')}.{self.namespace}.svc.cluster.local"
        args = " ".join(self.iperf_args_for_profile(cfg.traffic_profile))
        command = (
            "until test -e /sys/class/net/uesimtun0; do sleep 1; done; "
            "while true; do "
            f"echo SCENARIO={event_type} TRANSPORT=free5gc-tun INTERFACE=uesimtun0; "
            f"iperf3 --bind-dev uesimtun0 -c {server} {args} || true; sleep 1; done"
        )
        containers = manifest["spec"]["template"]["spec"].setdefault("containers", [])
        containers.append({
            "name": "iperf3-client",
            "image": self.iperf3_image,
            "imagePullPolicy": "IfNotPresent",
            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            "command": ["sh", "-c", command],
        })
        return manifest

    def ueransim_deployment_manifest(
        self,
        name: str,
        config_map: str,
        count: str,
        event_type: str,
        resources: dict[str, Any],
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        container = {
            "name": "ueransim-ue",
            "image": self.ueransim_image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["./nr-ue"],
            "args": ["-c", "/etc/ueransim/ue-config.yaml", "-n", count],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "privileged": False,
                "readOnlyRootFilesystem": True,
                "runAsUser": 0,
                "runAsGroup": 0,
                "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN", "MKNOD"]},
            },
            "volumeMounts": [
                {"name": "ue-config", "mountPath": "/etc/ueransim"},
                {"name": "dev-net-tun", "mountPath": "/dev/net/tun"},
                # UERANSIM keeps its inter-process registry under /tmp. Keep
                # the root filesystem read-only and expose only this scratch
                # location as writable.
                {"name": "ue-tmp", "mountPath": "/tmp"},
                # nr-ue appends a per-session routing-table entry while it
                # creates uesimtun0. An init container seeds a narrow writable
                # copy instead of making the image root filesystem writable.
                {"name": "ue-iproute2", "mountPath": "/etc/iproute2"},
            ],
        }
        probe = {
            "name": "ue-tun-probe",
            "image": "busybox:1.36",
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c"],
            "args": [self.ue_tun_probe_script()],
        }
        if resources:
            container["resources"] = resources
        config_revision = (
            f"{config_map}:{count}:{event_type}"
            if event_type.startswith("baseline-")
            else str(uuid.uuid4())
        )
        labels = {"app": name, "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type}
        sanitized_execution_id = self.sanitize_execution_id(execution_id) if execution_id else ""
        if sanitized_execution_id:
            labels["5gcityverse.io/execution-id"] = sanitized_execution_id
        pod_metadata = {
            "labels": labels,
            "annotations": {"5gcityverse.io/config-revision": config_revision},
        }
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": pod_metadata,
                    "spec": {
                        "initContainers": [
                            {
                                "name": "prepare-iproute2",
                                "image": self.ueransim_image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["sh", "-c", "cp -a /etc/iproute2/. /writable-iproute2/"],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "runAsUser": 0,
                                    "runAsGroup": 0,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [{"name": "ue-iproute2", "mountPath": "/writable-iproute2"}],
                            }
                        ],
                        "containers": [container, probe],
                        "volumes": [
                            {"name": "ue-config", "configMap": {"name": config_map, "items": [{"key": "ue-config.yaml", "path": "ue-config.yaml"}]}},
                            {"name": "dev-net-tun", "hostPath": {"path": "/dev/net/tun", "type": "CharDevice"}},
                            {"name": "ue-tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                            {"name": "ue-iproute2", "emptyDir": {"sizeLimit": "1Mi"}},
                        ],
                    },
                },
            },
        }

    @staticmethod
    def ue_tun_probe_script() -> str:
        return r"""
set -eu
iface=uesimtun0
target=${UE_TUN_PROBE_TARGET:-8.8.8.8}
while true; do
  if [ ! -e "/sys/class/net/$iface/statistics/rx_bytes" ]; then
    echo "UE_TUN_METRICS {\"ready\":false,\"reason\":\"interface_missing\"}"
    sleep 5
    continue
  fi
  rx1=$(cat "/sys/class/net/$iface/statistics/rx_bytes" 2>/dev/null || echo 0)
  tx1=$(cat "/sys/class/net/$iface/statistics/tx_bytes" 2>/dev/null || echo 0)
  t1=$(date +%s)
  ping -I "$iface" -c 5 -W 1 "$target" >/tmp/ue-tun-ping.out 2>&1 || true
  sleep 1
  rx2=$(cat "/sys/class/net/$iface/statistics/rx_bytes" 2>/dev/null || echo "$rx1")
  tx2=$(cat "/sys/class/net/$iface/statistics/tx_bytes" 2>/dev/null || echo "$tx1")
  t2=$(date +%s)
  elapsed=$((t2 - t1))
  [ "$elapsed" -gt 0 ] || elapsed=1
  drx=$((rx2 - rx1))
  dtx=$((tx2 - tx1))
  [ "$drx" -ge 0 ] || drx=0
  [ "$dtx" -ge 0 ] || dtx=0
  total=$((drx + dtx))
  mbps=$(awk "BEGIN { printf \"%.3f\", ($total * 8) / ($elapsed * 1000000) }")
  loss=$(sed -n 's/.*, \([0-9.]*\)% packet loss.*/\1/p' /tmp/ue-tun-ping.out | tail -n1)
  latency=$(sed -n 's/.*= [^/]*\/\([^/]*\)\/.*/\1/p' /tmp/ue-tun-ping.out | tail -n1)
  received=$(sed -n 's/.*transmitted, \([0-9]*\) packets* received.*/\1/p' /tmp/ue-tun-ping.out | tail -n1)
  [ -n "$received" ] || received=$(sed -n 's/.*transmitted, \([0-9]*\) received.*/\1/p' /tmp/ue-tun-ping.out | tail -n1)
  if [ "${received:-0}" -gt 0 ]; then
    echo "UE_TUN_METRICS {\"ready\":true,\"interface\":\"$iface\",\"target\":\"$target\",\"rxBytes\":$rx2,\"txBytes\":$tx2,\"deltaRxBytes\":$drx,\"deltaTxBytes\":$dtx,\"throughputMbps\":$mbps,\"latencyMs\":${latency:-0},\"packetLossPercent\":${loss:-0},\"receivedPackets\":$received}"
  else
    echo "UE_TUN_METRICS {\"ready\":false,\"reason\":\"no_echo_reply\",\"interface\":\"$iface\",\"target\":\"$target\",\"packetLossPercent\":${loss:-100},\"receivedPackets\":0}"
  fi
  sleep 5
done
"""

    def recreate_iperf3_job(self, k8s: EksKubernetesClient, event_type: str, cfg: EventConfig, execution_id: str | None = None) -> None:
        self.cleanup_iperf3_jobs(k8s, event_type)
        server_name = self.ensure_scenario_iperf3_server(k8s, event_type)
        deployment_name = f"iperf3-{event_type.replace('_', '-')}"
        args = " ".join(self.iperf_args_for_profile(cfg.traffic_profile))
        command = (
            "sleep 5; "
            "while true; do "
            f"echo SCENARIO={event_type}; "
            f"iperf3 -c {server_name}.{self.namespace}.svc.cluster.local {args} || true; "
            "sleep 1; "
            "done"
        )
        labels = {"app.kubernetes.io/component": "iperf3", "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type}
        pod_labels = {
            "app": deployment_name,
            "app.kubernetes.io/component": "iperf3",
            "app.kubernetes.io/part-of": "5gcityverse",
            "5gcityverse.io/scenario": event_type,
        }
        sanitized = self.sanitize_execution_id(execution_id) if execution_id else ""
        if sanitized:
            labels["5gcityverse.io/execution-id"] = sanitized
            pod_labels["5gcityverse.io/execution-id"] = sanitized
        evidence_annotations = {
            "5gcityverse.io/execution-id": str(execution_id or ""),
            "5gcityverse.io/sst": str(cfg.slice_sst),
            "5gcityverse.io/sd": str(cfg.slice_sd),
            "5gcityverse.io/dnn": str(cfg.dnn),
        }
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": deployment_name,
                "namespace": self.namespace,
                "labels": labels,
                "annotations": evidence_annotations,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": deployment_name}},
                "template": {
                    "metadata": {
                        "labels": pod_labels,
                        "annotations": {"5gcityverse.io/config-revision": str(uuid.uuid4()), **evidence_annotations},
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "iperf3-client",
                                "image": "networkstatic/iperf3:latest",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["sh", "-c", command],
                            }
                        ],
                    },
                },
            },
        }
        k8s.create_or_patch(
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments",
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{deployment_name}",
            deployment,
        )

    def has_event_runtime(self, k8s: EksKubernetesClient) -> bool:
        selector = urllib.parse.quote("app.kubernetes.io/part-of=5gcityverse", safe="")
        status, data = k8s.request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments?labelSelector={selector}", ignore_404=True)
        if status >= 300 or not isinstance(data, dict):
            return False
        active_scenarios = set(UE_CONFIG_MAPS.keys())
        for deployment in data.get("items", []):
            labels = (deployment.get("metadata") or {}).get("labels") or {}
            scenario = labels.get("5gcityverse.io/scenario")
            if scenario in active_scenarios:
                replicas = int(((deployment.get("spec") or {}).get("replicas") or 0))
                if replicas > 0:
                    return True
        return False

    @staticmethod
    def iperf_args_for_profile(profile: str) -> list[str]:
        profile_text = str(profile or "")
        rate_match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG])", profile_text, re.IGNORECASE)
        rate = "5M"
        if rate_match:
            raw_value = rate_match.group(1)
            value = raw_value.rstrip("0").rstrip(".") if "." in raw_value else raw_value
            rate = f"{value}{rate_match.group(2).upper()}"
        parallel_match = re.search(r"x\s*(\d+)", profile_text, re.IGNORECASE)
        parallel = int(parallel_match.group(1)) if parallel_match else 1
        packet_length_match = re.search(r"(\d+)\s*-?byte", profile_text, re.IGNORECASE)
        packet_length = packet_length_match.group(1) if packet_length_match else "200"
        if not packet_length_match and (rate.endswith("M") or rate.endswith("G")):
            numeric_rate = float(rate[:-1])
            packet_length = "1200" if rate.endswith("G") or numeric_rate >= 100 else "200"
        if parallel > 1:
            packet_length = "64"
        args = ["-u", "-b", rate, "-i", "1", "--forceflush"]
        if parallel > 1:
            args.extend(["-P", str(max(1, min(parallel, 24)))])
        args.extend(["-t", str(ScenarioEnvironmentService.iperf_duration_seconds_from_profile(profile_text)), "-l", packet_length])
        return args

    @staticmethod
    def iperf_duration_seconds(cfg: EventConfig) -> int:
        return ScenarioEnvironmentService.iperf_duration_seconds_from_profile(cfg.traffic_profile)

    @staticmethod
    def iperf_duration_seconds_from_profile(profile: str) -> int:
        # Scenario generators use a three-minute run interval so one iperf process
        # remains active throughout a typical AI planning round. The enclosing
        # loop reconnects after transient server replacement without changing the
        # lifecycle guarantee: traffic continues until the backend removes the
        # scenario deployment after inference and verification finish.
        return 180

    def ensure_baseline_iperf3_job(self, k8s: EksKubernetesClient) -> bool:
        selector = urllib.parse.quote("app.kubernetes.io/component=iperf3,5gcityverse.io/scenario=baseline", safe="")
        status, data = k8s.request("GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for job in data.get("items", []):
                job_status = job.get("status") or {}
                name = ((job.get("metadata") or {}).get("name") or "")
                if int(job_status.get("active") or 0) > 0:
                    return False
                if name:
                    k8s.delete(f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}?propagationPolicy=Background", ignore_404=True)
        job_name = f"iperf3-baseline-{uuid.uuid4().hex[:6]}"
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/component": "iperf3",
                    "app.kubernetes.io/part-of": "5gcityverse",
                    "5gcityverse.io/scenario": "baseline",
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 3660,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/component": "iperf3",
                            "app.kubernetes.io/part-of": "5gcityverse",
                            "5gcityverse.io/scenario": "baseline",
                        }
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "iperf3-client",
                                "image": "networkstatic/iperf3:latest",
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "iperf3",
                                    "-c",
                                    f"iperf3-server.{self.namespace}.svc.cluster.local",
                                    "-u",
                                    "-b",
                                    "120M",
                                    "-t",
                                    "3600",
                                    "-l",
                                    "800",
                                    "-i",
                                    "1",
                                    "--forceflush",
                                ],
                            }
                        ],
                    },
                },
            },
        }
        create_status, create_data = k8s.request("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", job)
        if create_status >= 300:
            raise RuntimeError(f"Kubernetes baseline iperf3 job create failed: HTTP {create_status} {create_data}")
        return True

    def cleanup_iperf3_jobs(self, k8s: EksKubernetesClient, event_type: str | None = None) -> None:
        selector = urllib.parse.quote("app.kubernetes.io/component=iperf3,app.kubernetes.io/part-of=5gcityverse", safe="")
        status, data = k8s.request("GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for job in data.get("items", []):
                labels = (job.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                if event_type and labels.get("5gcityverse.io/scenario") != event_type:
                    continue
                name = ((job.get("metadata") or {}).get("name") or "")
                if name:
                    k8s.delete(f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}?propagationPolicy=Background", ignore_404=True)
        status, data = k8s.request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for deployment in data.get("items", []):
                labels = (deployment.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                if event_type and labels.get("5gcityverse.io/scenario") != event_type:
                    continue
                name = ((deployment.get("metadata") or {}).get("name") or "")
                if name and not name.startswith("iperf3-server"):
                    k8s.delete(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}", ignore_404=True)
        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for pod in data.get("items", []):
                labels = (pod.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                if event_type and labels.get("5gcityverse.io/scenario") != event_type:
                    continue
                name = ((pod.get("metadata") or {}).get("name") or "")
                if name and not name.startswith("iperf3-server"):
                    k8s.delete(f"/api/v1/namespaces/{self.namespace}/pods/{name}", ignore_404=True)

    def cleanup_event_runtime(self, k8s: EksKubernetesClient, event_type: str) -> dict[str, Any]:
        self.cleanup_iperf3_jobs(k8s, event_type)
        deployment_name = f"ueransim-{event_type.replace('_', '-')}"
        server_name = f"iperf3-server-{event_type.replace('_', '-')}"
        self.delete_deployment(k8s, deployment_name)
        self.delete_deployment(k8s, server_name)
        k8s.delete(f"/api/v1/namespaces/{self.namespace}/services/{server_name}", ignore_404=True)
        return {"status": "reset", "cleanup": f"deleted {deployment_name}, {server_name}, and iperf3 jobs for {event_type}"}

    def cleanup_execution(self, k8s: EksKubernetesClient, execution_id: str) -> dict[str, Any]:
        """Delete deployments/pods labeled with the given execution-id, skipping baseline resources.

        Not wired to an API route yet; provided for future precise per-execution cleanup.
        """
        sanitized = self.sanitize_execution_id(execution_id)
        deleted: list[str] = []
        if not sanitized:
            return {"status": "skipped", "reason": "execution_id sanitized to empty", "deleted": deleted}
        selector = urllib.parse.quote(f"5gcityverse.io/execution-id={sanitized}", safe="")

        status, data = k8s.request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for deployment in data.get("items", []):
                labels = (deployment.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                name = ((deployment.get("metadata") or {}).get("name") or "")
                if name:
                    self.delete_deployment(k8s, name)
                    deleted.append(name)

        status, data = k8s.request("GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for job in data.get("items", []):
                labels = (job.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                name = ((job.get("metadata") or {}).get("name") or "")
                if name:
                    k8s.delete(f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}?propagationPolicy=Background", ignore_404=True)
                    deleted.append(name)

        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={selector}", ignore_404=True)
        if status < 300 and isinstance(data, dict):
            for pod in data.get("items", []):
                labels = (pod.get("metadata") or {}).get("labels") or {}
                if labels.get("5gcityverse.io/scenario") == "baseline":
                    continue
                name = ((pod.get("metadata") or {}).get("name") or "")
                if name:
                    k8s.delete(f"/api/v1/namespaces/{self.namespace}/pods/{name}", ignore_404=True)
                    deleted.append(name)

        return {"status": "reset", "cleanup": f"deleted execution-scoped resources for {execution_id}", "deleted": deleted}

    def cleanup_all_event_runtime(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        self.cleanup_iperf3_jobs(k8s)
        deleted: list[str] = []
        for name in self.LEGACY_BASELINE_DEPLOYMENTS:
            self.delete_deployment(k8s, name)
            deleted.append(name)
        for event_type in UE_CONFIG_MAPS:
            deployment_name = f"ueransim-{event_type.replace('_', '-')}"
            server_name = f"iperf3-server-{event_type.replace('_', '-')}"
            self.delete_deployment(k8s, deployment_name)
            self.delete_deployment(k8s, server_name)
            k8s.delete(f"/api/v1/namespaces/{self.namespace}/services/{server_name}", ignore_404=True)
            deleted.append(deployment_name)
        self.scale_primary_ueransim(k8s, 1)
        if self.ensure_baseline_iperf3_job(k8s):
            deleted.append("started iperf3-baseline")
        return {
            "status": "reset",
            "cleanup": "deleted event runtime and restored resident-only baseline",
            "deployments": deleted,
            "baseline": {"deployment": self.primary_deployment, "replicas": 1},
        }

    def recycle_session_state(self, k8s: EksKubernetesClient) -> dict[str, Any]:
        """Clear leaked UE contexts after scenario Pods are removed.

        UERANSIM Pods can disappear before NAS deregistration/PDU release is
        delivered. free5GC then retains SM contexts and allocated UE IPs. A
        bounded SMF -> AMF -> RAN recycle restores an isolated resident-only
        state between simulation rounds.
        """
        resident_ran = (
            "ueransim-city-gnb",
            self.primary_deployment,
            self.BASELINE_MMTC_DEPLOYMENT,
        )
        restarted: list[str] = []
        smf_deployment = "free5gc-free5gc-smf-smf"
        amf_deployment = "free5gc-free5gc-amf-amf"
        for upf_deployment in self.DEDICATED_UPF_DEPLOYMENTS:
            self.restart_deployment(k8s, upf_deployment)
            self.wait_for_deployment_available(k8s, upf_deployment, timeout_seconds=90)
            restarted.append(upf_deployment)
        self.restart_deployment(k8s, smf_deployment)
        self.wait_for_deployment_available(k8s, smf_deployment, timeout_seconds=90)
        restarted.append(smf_deployment)
        self.restart_deployment(k8s, amf_deployment)
        self.wait_for_deployment_available(k8s, amf_deployment, timeout_seconds=90)
        restarted.append(amf_deployment)
        # AMF reports Available before its NGAP startup sequence has fully
        # settled. Reconnecting gNB immediately can receive an unsupported
        # transient initiating message and lose the SCTP association.
        time.sleep(12)
        gnb, *resident_ues = resident_ran
        self.restart_deployment(k8s, gnb)
        self.wait_for_deployment_available(k8s, gnb, timeout_seconds=90)
        restarted.append(gnb)
        # Deployment availability only means the container is ready; allow NG
        # Setup/SCTP to complete before resident UEs begin registration.
        time.sleep(5)
        for deployment in resident_ues:
            self.restart_deployment(k8s, deployment)
            self.wait_for_deployment_available(k8s, deployment, timeout_seconds=90)
            restarted.append(deployment)
        return {"status": "success", "restarted": restarted, "reason": "released scenario SM contexts and restored resident RAN"}

    @staticmethod
    def ue_bearer_timeout_seconds(ue_count: int) -> int:
        """Scale the bearer-verification timeout with UE batch size.

        Base 30s covers a single-UE scenario; each additional UE in the batch
        needs time to attach/register before the first PDU session can appear
        in the pod log, so add 2s/UE up to a 120s cap (e.g. iot_surge's 50 UEs).
        """
        base_seconds = 30
        per_ue_seconds = 2
        cap_seconds = 120
        return min(cap_seconds, base_seconds + per_ue_seconds * max(0, int(ue_count or 0)))

    def wait_for_ue_bearer(self, k8s: EksKubernetesClient, event_type: str, timeout_seconds: int = 28) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            pod_name = self.ueransim_pod_name(k8s, event_type)
            if not pod_name:
                time.sleep(2)
                continue
            status, log_text = k8s.request_text(
                "GET",
                f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}/log?container=ueransim-ue&tailLines=160",
                ignore_404=True,
            )
            if status < 300 and "PDU Session establishment is successful" in log_text:
                return f"verified UE bearer from {pod_name}"
            time.sleep(2)
        return "UE bearer verification timed out; continuing with async status/probe polling"

    def ueransim_pod_name(self, k8s: EksKubernetesClient, event_type: str) -> str | None:
        deployment_name = f"ueransim-{event_type.replace('_', '-')}"
        status, data = k8s.request("GET", f"/api/v1/namespaces/{self.namespace}/pods")
        if status >= 300 or not isinstance(data, dict):
            return None
        for pod in data.get("items", []):
            metadata = pod.get("metadata") or {}
            labels = metadata.get("labels") or {}
            name = metadata.get("name") or ""
            phase = (pod.get("status") or {}).get("phase")
            label_text = " ".join(str(value) for value in labels.values())
            if phase == "Running" and (name.startswith(deployment_name) or deployment_name in label_text):
                return name
        return None

    @classmethod
    def component_from_resource(cls, resource: dict[str, Any]) -> str | None:
        metadata = resource.get("metadata") or {}
        labels = metadata.get("labels") or {}
        name = (metadata.get("name") or "").lower()
        label_text = " ".join(str(value).lower() for value in labels.values())
        haystack = f"{name} {label_text}"
        for component in cls.REQUIRED_CORE_COMPONENTS:
            if component.lower() in haystack:
                return component
        return None

    @classmethod
    def running_core_counts(cls, pods: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pod in pods:
            if ((pod.get("status") or {}).get("phase") or "Unknown") != "Running":
                continue
            component = cls.component_from_resource(pod)
            if component:
                counts[component] = counts.get(component, 0) + 1
        return counts
