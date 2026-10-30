"""Render the internal AF callback service. Secrets must be provisioned separately."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

PYTHON_IMAGE = "python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"
CALLBACK_IMAGE = "docker.io/cityverse/af-callback-deps@sha256:6a8615abc96e044348cf57a4b9ff32dd40a41742bdef85a141225b5dc88ef6df"


def objects(pcf_ip, namespace="free5gc-onprem", node="cv5g-cp", resource_base=None, *, recovery_fence=False):
    ipaddress.ip_address(pcf_ip)  # No CIDR, wildcard, or inferred trusted peer.
    if not isinstance(recovery_fence, bool):
        raise ValueError("Recovery fence opt-in must be boolean")
    if resource_base is not None:
        parsed = urlsplit(resource_base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/npcf-policyauthorization/v1"):
            raise ValueError("Exact trusted PCF Policy Authorization resource base required")
    labels = {"app.kubernetes.io/name": "cityverse-af-callback", "app.kubernetes.io/part-of": "cityverse",
              "cityverse.io/runtime": "kubeadm"}
    def resource(kind, name, spec=None, **extra):
        return {"apiVersion": "apps/v1" if kind == "Deployment" else "v1", "kind": kind,
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                **({"spec": spec} if spec is not None else {}), **extra}
    source = Path(__file__).resolve().parents[4] / "backend/onprem/storage/callbacks.py"
    cm = resource("ConfigMap", "cityverse-af-callback-source", data={
        "onprem__policy__policy_callback.py": source.read_text(encoding="utf-8"),
        "onprem__api__policy_callback_asgi.py": (source.parents[1] / "api/policy_callback_asgi.py").read_text(encoding="utf-8")})
    source_checksum = hashlib.sha256(json.dumps(cm["data"], sort_keys=True).encode()).hexdigest()
    if recovery_fence:
        cm["metadata"]["name"] = "cityverse-af-callback-source-gen263-" + source_checksum[:12]
        cm["immutable"] = True
    pv = resource("PersistentVolume", "cityverse-af-callback", {
        "capacity": {"storage": "1Gi"}, "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "cityverse-local",
        "local": {"path": "/var/lib/cityverse-af-callback"},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}]}}})
    pv["metadata"].pop("namespace")
    pvc = resource("PersistentVolumeClaim", "cityverse-af-callback", {
        "accessModes": ["ReadWriteOnce"], "storageClassName": "cityverse-local", "volumeName": "cityverse-af-callback",
        "resources": {"requests": {"storage": "1Gi"}}})
    env = [{"name": key, "valueFrom": {"secretKeyRef": {"name": "cityverse-af-callback-auth", "key": key}}}
           for key in ("CITYVERSE_AF_CALLBACK_SECRET", "CITYVERSE_AF_ADMIN_TOKEN")]
    env += [{"name": "CITYVERSE_AF_PCF_IPS", "value": pcf_ip}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    if resource_base is not None:
        env.append({"name": "CITYVERSE_AF_PCF_RESOURCE_BASE", "value": resource_base})
    if recovery_fence:
        env += [{"name": "CITYVERSE_AF_RECOVERY_FENCE", "value": "1"},
                {"name": "CITYVERSE_AF_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}]
    container = {"name": "callback", "image": CALLBACK_IMAGE, "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "backend.onprem.api.policy_callback_asgi", "--host", "0.0.0.0", "--port", "9082", "--state", "/state/callback.sqlite"],
        "workingDir": "/opt/cityverse", "env": env, "ports": [{"name": "http", "containerPort": 9082}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1337, "runAsGroup": 1337,
                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
        "volumeMounts": [{"name": "source", "mountPath": "/opt/cityverse/backend", "readOnly": True},
                         {"name": "state", "mountPath": "/state"}],
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 5},
        "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10, "failureThreshold": 3}}
    if recovery_fence:
        container["readinessProbe"] = {"exec": {"command": ["python", "-c",
            "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:9082/internal/recovery-fence',timeout=2)); assert r['readWriteFence']=='active' and r['inFlight']==0 and r['actions']==0 and r['receipts']==0"]},
            "periodSeconds": 5}
    dep = resource("Deployment", "cityverse-af-callback", {"replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels,
            "annotations": {"checksum/callback-source": source_checksum}}, "spec": {
            "automountServiceAccountToken": False, "nodeSelector": {"kubernetes.io/hostname": node},
            "containers": [container], "volumes": [{"name": "source", "configMap": {"name": cm["metadata"]["name"], "items": [
                {"key": "onprem__policy__policy_callback.py", "path": "onprem/policy/policy_callback.py"},
                {"key": "onprem__api__policy_callback_asgi.py", "path": "onprem/api/policy_callback_asgi.py"}]}},
            {"name": "state", "persistentVolumeClaim": {"claimName": "cityverse-af-callback"}}]}}})
    svc = resource("Service", "cityverse-af-callback", {"type": "ClusterIP", "selector": labels,
        "ports": [{"name": "http", "port": 9082, "targetPort": "http"}]})
    return [pv, pvc, cm, dep, svc]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcf-ip", required=True)
    parser.add_argument("--namespace", default="free5gc-onprem")
    parser.add_argument("--node", default="cv5g-cp")
    parser.add_argument("--pcf-resource-base", required=True)
    parser.add_argument("--recovery-fence", action="store_true")
    args = parser.parse_args()
    json.dump({"apiVersion": "v1", "kind": "List", "items": objects(args.pcf_ip, args.namespace, args.node, args.pcf_resource_base,
                                                                      recovery_fence=args.recovery_fence)}, sys.stdout)
