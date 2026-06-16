from __future__ import annotations

import traceback
import uuid
from typing import Any

from config import IPERF3_ARGS, UE_CONFIG_MAPS
from eks_kubernetes_client import EksKubernetesClient
from models import EventConfig


class ScenarioEnvironmentService:
    def __init__(self, cluster_name: str, namespace: str, primary_deployment: str) -> None:
        self.cluster_name = cluster_name
        self.namespace = namespace
        self.primary_deployment = primary_deployment

    def trigger(self, event_type: str, cfg: EventConfig) -> dict[str, Any]:
        if not self.cluster_name:
            return {"status": "skipped", "reason": "EKS_CLUSTER_NAME is not configured"}

        actions = []
        try:
            k8s = EksKubernetesClient(self.cluster_name)
            self.ensure_iperf3_server(k8s)
            self.apply_ue_configmap(k8s, event_type, cfg)

            if event_type in ("typhoon", "iot_surge"):
                self.scale_primary_ueransim(k8s, 0)
                self.delete_deployment(k8s, "ueransim-typhoon" if event_type == "iot_surge" else "ueransim-iot")
                self.create_or_patch_deployment(k8s, self.scenario_deployment_manifest(event_type))
                actions.append(f"started {event_type} UERANSIM deployment")
            else:
                self.delete_deployment(k8s, "ueransim-typhoon")
                self.delete_deployment(k8s, "ueransim-iot")
                self.create_or_patch_deployment(k8s, self.primary_ueransim_manifest(event_type, UE_CONFIG_MAPS[event_type]))
                actions.append(f"started {self.primary_deployment} with {UE_CONFIG_MAPS[event_type]}")

            self.recreate_iperf3_job(k8s, event_type)
            actions.append(f"launched iperf3-{event_type.replace('_', '-')}")
            return {"status": "success", "actions": actions, "httpStatus": 200}
        except Exception as exc:
            error = traceback.format_exc()
            print(error)
            return {"status": "error", "error": str(exc), "trace": error[-2000:], "actions": actions, "httpStatus": 500}

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
                "ports": [{"name": "iperf3", "port": 5201, "targetPort": 5201, "protocol": "TCP"}],
            },
        }
        k8s.create_or_patch(f"/apis/apps/v1/namespaces/{self.namespace}/deployments", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/iperf3-server", deployment)
        k8s.create_or_patch(f"/api/v1/namespaces/{self.namespace}/services", f"/api/v1/namespaces/{self.namespace}/services/iperf3-server", service)

    def apply_ue_configmap(self, k8s: EksKubernetesClient, event_type: str, cfg: EventConfig) -> None:
        name = UE_CONFIG_MAPS[event_type]
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace},
            "data": {"ue-config.yaml": self.ue_config_yaml(event_type, cfg)},
        }
        k8s.create_or_patch(
            f"/api/v1/namespaces/{self.namespace}/configmaps",
            f"/api/v1/namespaces/{self.namespace}/configmaps/{name}",
            manifest,
        )

    def ue_config_yaml(self, event_type: str, cfg: EventConfig) -> str:
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
  - ueransim-gnb.{self.namespace}.svc.cluster.local
uacAic:
  mps: false
  mcs: false
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

    def primary_ueransim_manifest(self, event_type: str, config_map_name: str) -> dict[str, Any]:
        return self.ueransim_deployment_manifest(self.primary_deployment, config_map_name, "1", event_type, {})

    def create_or_patch_deployment(self, k8s: EksKubernetesClient, manifest: dict[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        k8s.create_or_patch(
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments",
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            manifest,
        )

    def delete_deployment(self, k8s: EksKubernetesClient, name: str) -> None:
        k8s.delete(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}")

    def scenario_deployment_manifest(self, event_type: str) -> dict[str, Any]:
        if event_type == "typhoon":
            return self.ueransim_deployment_manifest("ueransim-typhoon", "ueransim-ue-config-typhoon", "3", event_type, {})
        resources = {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
        return self.ueransim_deployment_manifest("ueransim-iot", "ueransim-ue-config-mmtc", "50", event_type, resources)

    def ueransim_deployment_manifest(
        self,
        name: str,
        config_map: str,
        count: str,
        event_type: str,
        resources: dict[str, Any],
    ) -> dict[str, Any]:
        container = {
            "name": "ueransim-ue",
            "image": "free5gc/ueransim:v4.0.1",
            "imagePullPolicy": "IfNotPresent",
            "command": ["./nr-ue"],
            "args": ["-c", "/etc/ueransim/ue-config.yaml", "-n", count],
            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            "volumeMounts": [{"name": "ue-config", "mountPath": "/etc/ueransim"}],
        }
        if resources:
            container["resources"] = resources
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {"app": name, "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type}},
                    "spec": {
                        "containers": [container],
                        "volumes": [{"name": "ue-config", "configMap": {"name": config_map, "items": [{"key": "ue-config.yaml", "path": "ue-config.yaml"}]}}],
                    },
                },
            },
        }

    def recreate_iperf3_job(self, k8s: EksKubernetesClient, event_type: str) -> None:
        job_name = f"iperf3-{event_type.replace('_', '-')}-{uuid.uuid4().hex[:6]}"
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/component": "iperf3", "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type},
            },
            "spec": {
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {"labels": {"app.kubernetes.io/component": "iperf3", "app.kubernetes.io/part-of": "5gcityverse", "5gcityverse.io/scenario": event_type}},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "iperf3-client",
                                "image": "networkstatic/iperf3:latest",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["iperf3", "-c", f"iperf3-server.{self.namespace}.svc.cluster.local"] + IPERF3_ARGS[event_type],
                            }
                        ],
                    },
                },
            },
        }
        k8s.request("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", job)

