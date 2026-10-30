"""Pinned free-ran-ue UE Pod contract used by the native kubeadm controller.

This module only constructs a manifest. It never reads credentials or mutates
Kubernetes; the caller must retain generation, evidence and retirement fences.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
from threading import Lock
import time

from backend.onprem.runtime.kubeadm import KubeadmDriver, KubeadmReadKind
from backend.onprem.runtime.ue import SLICES, seconds, timestamp
from backend.onprem.runtime.ue import EvidenceUnavailable

UE_IMAGE = "docker.io/cityverse/free-ran-ue@sha256:1b6d966a0016df5f543f3b132d9ae704946feb87bc79fd5540bde37f6d5af55c"
INIT_IMAGE = "python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"

# The init container alone reads Secret material. It writes a 0600 JSON config
# to a memory-backed volume; neither the Pod spec nor logs contain AKA keys.
CONFIG_INIT = r'''import json, os, socket
groups = json.load(open('/credentials/groups.json'))
credential = groups[int(os.environ['GROUP'])]
config = {
 'ue': {
  'ranControlPlaneIp': socket.gethostbyname(os.environ['GNB_HOST']),
  'ranDataPlaneIp': socket.gethostbyname(os.environ['GNB_HOST']),
  'ranControlPlanePort': 31413, 'ranDataPlanePort': 31414,
  'plmnId': {'mcc': '208', 'mnc': '93'}, 'msin': os.environ['MSIN'],
  'authenticationSubscription': {
   'encPermanentKey': credential['key'], 'encOpcKey': credential['opc'],
   'authenticationManagementField': '8000', 'sequenceNumber': '000000000000'},
  'integrityAlgorithm': {'nia0': False, 'nia1': False, 'nia2': True, 'nia3': False},
  'cipheringAlgorithm': {'nea0': True, 'nea1': False, 'nea2': False, 'nea3': False},
  'pduSession': {'dnn': 'internet', 'snssai': {'sst': os.environ['SST'], 'sd': os.environ['SD']}},
  'accessType': '3GPP_ACCESS', 'nrdc': {'enable': False},
  'ueTunnelDevice': 'ueTun', 'ignoreSetupTunnel': False},
 'logger': {'level': 'info'}}
with open('/run/fru/ue.json', 'w') as out: json.dump(config, out)
os.chmod('/run/fru/ue.json', 0o600)
'''


def pod_manifest(generation: int, index: int, shard: dict, namespace: str,
                 *, gnb_host: str | None = None, image: str = UE_IMAGE,
                 init_image: str = INIT_IMAGE) -> dict:
    """Build one fixed-count, sequential-start UE Pod from an owned shard."""
    name = shard.get("slice")
    supis = shard.get("supis")
    group = shard.get("credentialGroup")
    if (name not in SLICES or type(generation) is not int or generation <= 0
            or type(index) is not int or index < 0
            or type(group) is not int or not 0 <= group <= 3
            or not isinstance(supis, list) or not 1 <= len(supis) <= 250
            or any(not isinstance(supi, str) or not re.fullmatch(r"imsi-[0-9]{15}", supi)
                   for supi in supis)
            or len(set(supis)) != len(supis)
            or any(int(supi[5:]) != int(supis[0][5:]) + offset for offset, supi in enumerate(supis))
            or any((int(supi[5:]) - 208930100000001) // 250 != group for supi in supis)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", gnb_host or "gnb-" + name)
            or any(not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", ref)
                   for ref in (image, init_image))):
        raise ValueError("Invalid owned free-ran-ue shard or immutable image")
    pod_name = f"cv-ue-g{generation}-{index}"
    env = {"GROUP": group, "SST": SLICES[name]["sst"], "SD": SLICES[name]["sd"],
           "MSIN": supis[0][5:][5:], "GNB_HOST": gnb_host or "gnb-" + name}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": pod_name, "namespace": namespace,
        "labels": {"cityverse.io/managed-by": "onprem-ue-controller",
                   "cityverse.io/generation": str(generation), "cityverse.io/slice": name},
        "annotations": {"cityverse.io/supis": json.dumps(supis)}},
        "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 180,
                 "automountServiceAccountToken": False,
                 "nodeSelector": {"cityverse.io/role": "ran"},
                 "initContainers": [{"name": "config", "image": init_image,
                                     "command": ["python", "-c", CONFIG_INIT],
                                     "env": [{"name": key, "value": str(value)} for key, value in env.items()],
                                     "volumeMounts": [
                                         {"name": "credentials", "mountPath": "/credentials", "readOnly": True},
                                         {"name": "config", "mountPath": "/run/fru"}]}],
                 "containers": [{"name": "ue", "image": image,
                                 "command": ["/bin/sh", "-c"],
                                 "args": [f"exec ./free-ran-ue ue -c /run/fru/ue.json -n {len(supis)} -p 1"],
                                 "securityContext": {"privileged": True},
                                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                               "limits": {"memory": "384Mi"}},
                                 "volumeMounts": [{"name": "config", "mountPath": "/run/fru", "readOnly": True},
                                                  {"name": "tun", "mountPath": "/dev/net/tun"}]}],
                 "volumes": [{"name": "credentials", "secret": {"secretName": "cityverse-ue-credentials", "defaultMode": 256}},
                             {"name": "config", "emptyDir": {"medium": "Memory"}},
                             {"name": "tun", "hostPath": {"path": "/dev/net/tun", "type": "CharDevice"}}]}}


class FreeRanKubeadmDriver(KubeadmDriver):
    """Opt-in driver. Pod TERM is free-ran-ue's normal NAS deregistration path."""

    backend = "free-ran-ue"

    def __init__(self, *args, image: str = UE_IMAGE, init_image: str = INIT_IMAGE, resource_profile=None, **kwargs):
        super().__init__(*args, image=image, init_image=init_image, **kwargs)
        from .ue_resources import UERequestsProfile, configured_ue_requests
        self.resource_profile = configured_ue_requests() if resource_profile is None else resource_profile
        if self.resource_profile is not None and (type(self.resource_profile) is not UERequestsProfile or not self.traffic_enabled):
            raise ValueError('Typed UE request profile requires native traffic-enabled driver')
        self.ran_evidence_transport = os.environ.get('CITYVERSE_ONPREM_RAN_EVIDENCE_TRANSPORT', 'kubectl')
        if self.ran_evidence_transport not in {'kubectl', 'pod-http'}:
            raise ValueError('Unsupported trusted RAN evidence transport')

    @property
    def resource_policy_digest(self):
        from .ue_resources import resource_policy_digest
        from .kubeadm import TRAFFIC_CLIENT_MEMORY_LIMIT
        return resource_policy_digest(getattr(self, 'resource_profile', None),
            traffic_enabled=self.traffic_enabled, traffic_memory_limit=TRAFFIC_CLIENT_MEMORY_LIMIT)

    def pod_manifest(self, generation: int, index: int, shard: dict):
        if 'device' in shard:
            from .device_sessions import DeviceAllocation, FreeRanDeviceCompiler, DEVICE_SPATIAL_FIELDS
            if self.device_profile is None:
                raise EvidenceUnavailable('FreeRAN device requires commissioned single-session profile')
            return FreeRanDeviceCompiler(self.device_profile).pod_manifest(self, generation, index,
                DeviceAllocation.from_dict(shard['device']),
                spatial_context={k:shard[k] for k in DEVICE_SPATIAL_FIELDS if k in shard})
        manifest = pod_manifest(generation, index, shard, self.namespace,
                                gnb_host=shard.get("spatialAccess", {}).get("endpoint"), image=self.image, init_image=self.init_image)
        if shard.get('spatialAccess'):
            manifest['metadata']['annotations']['cityverse.io/spatial-access'] = json.dumps(shard['spatialAccess'], sort_keys=True)
        if self.traffic_enabled:
            # The existing traffic worker only reads the shared network namespace.
            legacy = super().pod_manifest(generation, index, shard)
            manifest["spec"]["containers"].append(legacy["spec"]["containers"][1])
            manifest["spec"]["volumes"].append(legacy["spec"]["volumes"][-1])
        if self.resource_profile is not None:
            self.resource_profile.apply(manifest)
        manifest['metadata'].setdefault('annotations', {})['cityverse.io/runtime-policy-sha256'] = self.runtime_policy_digest
        return manifest

    def population_deregister(self, pod, supi):
        """Single-device TERM uses the deployed binary's normal NAS Stop path.

        The population journal dispatches this exactly once. Acknowledgement is
        never proof of release: AMF/SMF/PFCP absence remains mandatory.
        """
        try:
            supis = json.loads(pod.get('metadata', {}).get('annotations', {}).get('cityverse.io/supis', '[]'))
        except (TypeError, ValueError):
            raise EvidenceUnavailable('FreeRAN population allocation malformed') from None
        if supis != [supi] or self.device_profile is None or self.device_profile.driver != 'free-ran-ue':
            raise EvidenceUnavailable('FreeRAN population requires one owned device')
        return self.retire_owned_pod(pod, {})

    def ue_states(self, name, supis):
        # free-ran-ue has no nr-cli equivalent. Never invent NAS state from
        # creation annotations or historical logs.
        raise EvidenceUnavailable("free-ran-ue has no per-UE NAS status API")

    def observe_gnb_contexts(self, *, exec_timeout: int = 12, parallel: bool = False):
        """Count current UE contexts in all four gNB APIs with Pod UID fences."""
        if (os.environ.get('CITYVERSE_ONPREM_SPATIAL_RAN_CONFIG')
                or os.environ.get('CITYVERSE_ONPREM_CITY_VALUES')):
            from .spatial_ran import configured_registry, native_readouts
            registry = configured_registry()
            native = native_readouts(self, registry)
            readouts = {}
            for name in SLICES:
                rows = [native[r['gnbId']] for r in registry['gnbs'] if name in r['slices']]
                if not rows:
                    raise EvidenceUnavailable('Commissioned slice RAN context unavailable')
                supis = sorted({s for row in rows for s in row['supis']})
                readouts[name] = dict(ueCount=len(supis), supis=supis, spatialReadouts=rows,
                    observedAt=min(row['observedAt'] for row in rows))
            # Retain the four-slice release envelope. spatialReadouts explicitly
            # enumerate every commissioned gNB; the count includes all seven.
            return dict(source='free-ran-ue-gnb-api-ue-count', scope='four-kubeadm-gnb-pods',
                commissionedGnbCount=len(native), observedAt=min(r['observedAt'] for r in native.values()),
                readouts=readouts)
        if type(exec_timeout) is not int or not 5 <= exec_timeout <= 30:
            raise ValueError("gNB exec timeout must be 5..30 seconds")
        self.last_gnb_diagnostic = None
        self.last_gnb_diagnostics = []
        failures = {}
        failure_lock = Lock()

        def publish_failures():
            with failure_lock:
                self.last_gnb_diagnostics = [failures[name] for name in (*SLICES, "all") if name in failures]
            self.last_gnb_diagnostic = self.last_gnb_diagnostics[0] if self.last_gnb_diagnostics else None

        def record_failure(name, phase, error, reason_code):
            if isinstance(error, EvidenceUnavailable):
                error_type = "EvidenceUnavailable"
            elif isinstance(error, subprocess.TimeoutExpired):
                error_type = "TimeoutExpired"
            elif isinstance(error, json.JSONDecodeError):
                error_type = "JSONDecodeError"
            elif isinstance(error, OSError):
                error_type = "OSError"
            elif isinstance(error, KeyError):
                error_type = "KeyError"
            elif isinstance(error, TypeError):
                error_type = "TypeError"
            elif isinstance(error, ValueError):
                error_type = "ValueError"
            else:
                error_type = "AttributeError"
            with failure_lock:
                failures[name] = {"name": name, "phase": phase,
                                  "errorType": error_type, "reasonCode": reason_code}

        try:
            try:
                pods = self.read_pods().get("items", [])
            except (EvidenceUnavailable, subprocess.TimeoutExpired, OSError,
                    KeyError, TypeError, ValueError, json.JSONDecodeError, AttributeError) as error:
                record_failure("all", "podGet", error, "pod_query_failed")
                raise

            def read_one(name):
                phase, reason_code = "podGet", "pod_identity_unavailable"
                try:
                    matches = [pod for pod in pods if pod.get("metadata", {}).get("name", "").startswith("gnb-" + name + "-")]
                    if len(matches) != 1:
                        raise EvidenceUnavailable("Free-RAN gNB context observation unavailable")
                    identity = self._gnb_identity(matches[0], name)
                    phase, reason_code = "fence", "pod_uid_fence_unavailable"
                    self._check_gnb_identity(name, identity)
                    phase, reason_code = "infoExec", "info_exec_unavailable"
                    if self.ran_evidence_transport == 'pod-http':
                        from backend.onprem.evidence.ran_http import RanEvidenceClient
                        envelope = json.loads(RanEvidenceClient().read(matches[0], time.monotonic() + exec_timeout))
                        if (envelope.get('schemaVersion') != 'cityverse.ran-evidence/v1'
                                or envelope.get('role') != 'gnb'
                                or not -1 <= time.time() - seconds(envelope.get('observedAt')) <= 15):
                            raise EvidenceUnavailable('Free-RAN gNB evidence unavailable or stale')
                        info_body = envelope['info']
                    else:
                        script = ("import json,urllib.request;"
                                  "print(json.dumps(json.load(urllib.request.urlopen("
                                  "'http://127.0.0.1:40104/api/gnb/info',timeout=5))))")
                        result = self.read_runner(KubeadmReadKind.NATIVE_PROBE)(["kubectl", "--context", self.context, "--namespace", self.namespace,
                                              "exec", identity["pod"], "-c", "gnb-evidence", "--",
                                              "python", "-c", script],
                                              text=True, capture_output=True, timeout=exec_timeout, check=False)
                        if result.returncode or len(result.stdout.encode()) > 1024 * 1024:
                            raise EvidenceUnavailable("Free-RAN gNB context API unavailable")
                        info_body = json.loads(result.stdout)
                    phase, reason_code = "schema", "info_schema_invalid"
                    info = info_body["gnbInfo"]
                    if (info.get("gnbName") != "cv-fru-" + name
                            or info.get("plmnId") != "20893"
                            or info.get("snssai") != {"sst": str(SLICES[name]["sst"]), "sd": SLICES[name]["sd"]}
                            or not isinstance(info.get("ranUeList"), list)):
                        raise EvidenceUnavailable("Free-RAN gNB context identity mismatch")
                    if any(not isinstance(row, dict) for row in info["ranUeList"]):
                        raise EvidenceUnavailable("Free-RAN gNB context list malformed")
                    imsis = [row.get("imsi") for row in info["ranUeList"]]
                    if any(not isinstance(value, str) or not re.fullmatch(r"(?:imsi-)?[0-9]{15}", value) for value in imsis):
                        raise EvidenceUnavailable("Free-RAN gNB context list malformed")
                    if len({value.removeprefix("imsi-") for value in imsis}) != len(imsis):
                        raise EvidenceUnavailable("Free-RAN gNB context list malformed")
                    phase, reason_code = "fence", "pod_uid_fence_unavailable"
                    self._check_gnb_identity(name, identity)
                    return name, {**identity, "ueCount": len(imsis),
                                  "supis": sorted("imsi-" + value.removeprefix("imsi-") for value in imsis),
                                  "observedAt": timestamp(time.time())}
                except (EvidenceUnavailable, subprocess.TimeoutExpired, OSError,
                        KeyError, TypeError, ValueError, json.JSONDecodeError, AttributeError) as error:
                    if isinstance(error, subprocess.TimeoutExpired):
                        reason_code = "operation_timeout"
                    elif isinstance(error, json.JSONDecodeError):
                        reason_code = "invalid_json"
                    record_failure(name, phase, error, reason_code)
                    raise
            if parallel:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    readouts = dict(executor.map(read_one, SLICES))
            else:
                readouts = dict(read_one(name) for name in SLICES)
            from .spatial_ran import augment_context_readouts
            readouts = augment_context_readouts(self, readouts)
            return {"source": "free-ran-ue-gnb-api-ue-count",
                    "scope": "four-kubeadm-gnb-pods",
                    "observedAt": timestamp(min(seconds(row["observedAt"]) for row in readouts.values())),
                    "readouts": readouts}
        except EvidenceUnavailable:
            publish_failures()
            raise
        except (subprocess.TimeoutExpired, OSError, KeyError, TypeError, ValueError,
                json.JSONDecodeError, AttributeError):
            publish_failures()
            raise EvidenceUnavailable("Free-RAN gNB context observation unavailable") from None

    def retire_owned_pod(self, pod: dict, evidence: dict):
        """TERM only the exact owned binary; kubelet grace runs its NAS Stop().

        This is a shutdown *request*, not release proof. The base reconcile
        loop retains the terminating Pod fence and independently collected
        AMF/SMF/UPF residuals must disappear before any new allocation.
        """
        meta = pod.get("metadata", {})
        name, uid = meta.get("name"), meta.get("uid")
        if (not isinstance(name, str) or not re.fullmatch(r"cv-ue-g[1-9][0-9]*-[0-9]+", name)
                or not isinstance(uid, str) or not uid
                or meta.get("labels", {}).get("cityverse.io/managed-by") != "onprem-ue-controller"
                or meta.get("deletionTimestamp")):
            raise EvidenceUnavailable("Free-RAN UE retirement ownership invalid")
        fresh = self._read_kubectl("get", "pod", name, "-o", "json")
        current = fresh.get("metadata", {})
        try:
            supis = json.loads(current.get("annotations", {}).get("cityverse.io/supis", "[]"))
        except (TypeError, ValueError):
            raise EvidenceUnavailable("Free-RAN UE retirement allocation malformed") from None
        specs = [item for item in fresh.get("spec", {}).get("containers", []) if item.get("name") == "ue"]
        if (current.get("uid") != uid or current.get("deletionTimestamp")
                or current.get("namespace") != self.namespace
                or current.get("labels", {}).get("cityverse.io/managed-by") != "onprem-ue-controller"
                or current.get("labels", {}).get("cityverse.io/generation")
                   != meta.get("labels", {}).get("cityverse.io/generation")
                or current.get("annotations", {}).get("cityverse.io/supis")
                   != meta.get("annotations", {}).get("cityverse.io/supis")
                or not isinstance(supis, list) or not 1 <= len(supis) <= self.batch_size
                or len(set(supis)) != len(supis)
                or any(not isinstance(s, str) or not re.fullmatch(r"imsi-[0-9]{15}", s) for s in supis)
                or len(specs) != 1 or specs[0].get("image") != self.image
                or specs[0].get("command") != ["/bin/sh", "-c"]
                or specs[0].get("args") != [f"exec ./free-ran-ue ue -c /run/fru/ue.json -n {len(supis)} -p 1"]
                or fresh.get("spec", {}).get("restartPolicy") != "Never"
                or fresh.get("spec", {}).get("terminationGracePeriodSeconds", 0) < 180):
            raise EvidenceUnavailable("Free-RAN UE Pod/process fence changed before normal deregistration")
        if fresh.get("status", {}).get("phase") != "Running":
            for row in evidence.get("ues", []):
                if row.get("supi") in supis and (row.get("amf", {}).get("registered") is True
                        or row.get("smf", {}).get("sessionCount", 0) > 0
                        or row.get("upf", {}).get("pfcpSeid")):
                    raise EvidenceUnavailable("Non-running UE retains native contexts; normal deregistration unavailable")
        # Raw DELETE uses UID preconditions and the Pod's default 180s grace.
        # No --force and no zero grace: the Go binary receives SIGTERM and
        # performs Stop() for every started UE before it exits.
        self.delete_owned_pod(fresh)
        return "graceful_deregistration_requested"
