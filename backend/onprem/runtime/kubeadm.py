"""Explicit-target Kubernetes UE driver; never uses the current kube context."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import base64
import shlex
import hashlib
import math
import inspect
from uuid import uuid4
from dataclasses import dataclass
from enum import Enum
from contextvars import ContextVar

from backend.onprem.runtime.ue import EvidenceUnavailable, SLICES, shards, seconds, timestamp
from backend.onprem.runtime.traffic_manager import traffic_image, manager_container

LABEL = "cityverse.io/managed-by=onprem-ue-controller"
INIT_IMAGE = "python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"
TRAFFIC_IMAGE = traffic_image()
TRAFFIC_CLIENT_MEMORY_LIMIT = "256Mi"
TRAFFIC_RECEIVER_MEMORY_LIMIT = "2Gi"
TERMINAL_SHUTDOWN = 'cityverse.io/terminal-shutdown'
PROCESS_READ = "readlink /proc/1/exe; awk '{print $22}' /proc/1/stat; cat /proc/sys/kernel/random/boot_id; awk '/^SigCgt:/ {print $2}' /proc/1/status"


def terminal_intent_digest(intent):
    return hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_terminal_recovery_receipt(receipt, *, now):
    """Trusted startup authorization, not self-authenticating public JSON."""
    if (not isinstance(receipt, dict) or receipt.get('schemaVersion') != 'cityverse.terminal-recovery/v1'
            or not isinstance(receipt.get('approvalId'), str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', receipt['approvalId'])
            or any(type(receipt.get(k)) not in (int, float) or not math.isfinite(receipt[k]) for k in ('approvedAt', 'expiresAt'))
            or not receipt['approvedAt'] <= now < receipt['expiresAt'] <= receipt['approvedAt'] + 900
            or not isinstance(receipt.get('intentDigest'), str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['intentDigest'])
            or type(receipt.get('generation')) is not int or receipt['generation'] <= 0
            or any(not isinstance(receipt.get(k), str) or not receipt[k] for k in ('podUid', 'containerID', 'roundId'))
            or not isinstance(receipt.get('process'), dict)):
        raise EvidenceUnavailable('Terminal recovery receipt invalid or expired')
    audit = receipt.get('audit', {})
    if (not isinstance(audit, dict) or audit.get('reason') != 'kubectl_patch_non_json_pre_dispatch'
            or audit.get('reviewed') is not True or not isinstance(audit.get('helperIdentity'), str) or not audit['helperIdentity']
            or not isinstance(audit.get('sourceSha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', audit['sourceSha256'])):
        raise EvidenceUnavailable('Terminal recovery pre-dispatch review missing')
    return receipt

# Credentials are read only inside an init container from a Kubernetes Secret.
# JSON is a YAML subset accepted by yaml-cpp; no shell interpolation of secrets.
CONFIG_INIT = """import json, os, socket
groups = json.load(open('/credentials/groups.json'))
credential = groups[int(os.environ['GROUP'])]
slice = {'sst': int(os.environ['SST']), 'sd': os.environ['SD']}
config = {'supi': os.environ['SUPI'], 'mcc': '208', 'mnc': '93',
 'key': credential['key'], 'op': credential['opc'], 'opType': 'OPC', 'amf': '8000',
 'imei': '356938035643803', 'imeiSv': '4370816125816151',
 'gnbSearchList': [socket.gethostbyname(os.environ['GNB'])],
 'uacAic': {'mps': False, 'mcs': False}, 'uacAcc': {'normalClass': 0, 'class11': False, 'class12': False, 'class13': False, 'class14': False, 'class15': False},
 'sessions': [{'type': 'IPv4', 'apn': 'internet', 'slice': slice}],
 'configured-nssai': [slice], 'default-nssai': [slice],
 'integrity': {'IA1': True, 'IA2': True, 'IA3': True},
 'ciphering': {'EA1': True, 'EA2': True, 'EA3': True},
 'integrityMaxRate': {'uplink': 'full', 'downlink': 'full'}}
with open('/run/ue/config.yaml', 'w') as f: json.dump(config, f)
os.chmod('/run/ue/config.yaml', 0o600)
"""



class KubeadmReadKind(Enum):
    METADATA = 'metadata'
    NATIVE_PROBE = 'native_probe'
    PROCESS = 'process'


class KubeadmMutationKind(Enum):
    POD_APPLY = 'pod_apply'
    POD_CREATE = 'pod_create'
    POD_DELETE = 'pod_delete'
    NAS_DEREGISTER = 'nas_deregister'
    TERMINAL_PATCH = 'terminal_patch'
    TERMINAL_SIGNAL = 'terminal_signal'


@dataclass(frozen=True)
class KubeadmMutation:
    """Exact prepared dispatch. No timestamps, grants or ownership are invented."""
    kind: KubeadmMutationKind
    context: str
    namespace: str
    target_json: str
    argv: tuple[str, ...]
    input_text: str | None
    request_id: str

    @property
    def body_sha256(self):
        return hashlib.sha256((self.input_text or '').encode()).hexdigest()

    @property
    def request_sha256(self):
        return hashlib.sha256(json.dumps([self.kind.value, self.context, self.namespace,
            self.target_json, self.argv, self.input_text, self.request_id], separators=(',', ':'),
            allow_nan=False).encode()).hexdigest()


_NOT_SENT_PROOF = object()


class NativeDispatchNotSent(EvidenceUnavailable):
    """Only this module's final guard can prove zero native-process entry."""
    def __init__(self, mutation, *, _proof=None, reason='Native dispatch rejected before process entry'):
        if _proof is not _NOT_SENT_PROOF or type(mutation) is not KubeadmMutation:
            raise ValueError('Trusted exact pre-entry proof required')
        self.mutation=mutation
        self.request_id=mutation.request_id
        self._proof=_proof
        super().__init__(reason)

    def matches(self, mutation):
        return (self._proof is _NOT_SENT_PROOF and type(mutation) is KubeadmMutation
                and self.mutation==mutation and self.request_id==mutation.request_id)


class KubeadmDriver:
    def __init__(self, context: str, namespace: str = "free5gc-onprem", *, batch_size: int = 50,
                 batch_interval: float = 30, image: str = "free5gc/ueransim:v4.0.1@sha256:4a6745b0c9f0c60173833f8bef89816324e84636e220917bdc682555a299e8ba",
                 init_image: str = INIT_IMAGE, tempo_ms: int = 500, runner=subprocess.run,
                 traffic_enabled: bool = False, terminal_authorizer=None, device_profile=None, final_mutation_guard=None,
                 typed_launch_burst: int = 1, typed_launch_max_pending: int = 8):
        if not context or any(term in context.lower() for term in ("arn:", "eks", "kind", "docker-desktop", "minikube")):
            raise ValueError("An explicit on-prem kubeadm context is required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace) or namespace in {"default", "kube-system"}:
            raise ValueError("A dedicated on-prem namespace is required")
        shards([], batch_size)
        if batch_interval < 2:
            raise ValueError("batch_interval must be at least two seconds")
        if type(tempo_ms) is not int or not 1 <= tempo_ms <= 60000:
            raise ValueError('tempo_ms must be an integer between 1 and 60000')
        if (type(typed_launch_burst) is not int or not 1 <= typed_launch_burst <= 10
                or type(typed_launch_max_pending) is not int or not 1 <= typed_launch_max_pending <= 20
                or typed_launch_burst > typed_launch_max_pending):
            raise ValueError('Typed launch requires burst 1..10 within pending ceiling 1..20')
        self.typed_launch_burst, self.typed_launch_max_pending = typed_launch_burst, typed_launch_max_pending
        self.context, self.namespace = context, namespace
        self.batch_size, self.batch_interval = batch_size, batch_interval
        self.image, self.init_image = image, init_image
        self._transport_runner = runner
        self._final_mutation_guard = None
        self._dispatch_port = ContextVar("kubeadm_dispatch_port_" + str(id(self)), default=None)
        if final_mutation_guard is not None:
            self.bind_final_mutation_guard(final_mutation_guard)
        self.device_profile = device_profile
        if device_profile is not None:
            from .device_sessions import DeviceDriverProfile, DEVICE_CONFIG_PATCH, FREE_RAN_DEVICE_CONFIG_PATCH
            vendor = 'free-ran-ue' if getattr(self, 'backend', None) == 'free-ran-ue' else 'ueransim'
            if type(device_profile) is not DeviceDriverProfile or device_profile.driver!=vendor or image.split('@')[-1]!=device_profile.image_digest:
                raise ValueError('Explicit matching native device driver profile required')
            if vendor == 'free-ran-ue':
                from .free_ran_ue import CONFIG_INIT as native_init
                from dataclasses import asdict
                # The server-owned RAN allowlist and source receipt are part
                # of the launch capability consumed by admission, not mutable
                # request fields hidden behind the same configuration digest.
                patch = FREE_RAN_DEVICE_CONFIG_PATCH + json.dumps(asdict(device_profile), sort_keys=True)
            else:
                native_init, patch = CONFIG_INIT, DEVICE_CONFIG_PATCH
            self.reservation_configuration_digest=hashlib.sha256((native_init+patch).encode()).hexdigest()
        self.tempo_ms = tempo_ms
        self.traffic_enabled = traffic_enabled
        self.terminal_authorizer = terminal_authorizer
        self.owned_native_teardown = None  # Installed only by the typed pre-stop controller path.
        self.owned_native_nas = None
        self.owned_native_nas_ack = None
        self.last_launch = 0.0
        self._deregistered_pod_uids = {}
        self._quiescent_pod_uids = {}
        self._deregister_attempts = {}
        self._retirement_cursor = 0
        metadata_transport = os.environ.get('CITYVERSE_ONPREM_K8S_POD_READ_TRANSPORT', 'kubectl')
        if metadata_transport not in {'kubectl', 'api-https'}:
            raise ValueError('Unsupported trusted Kubernetes Pod read transport')
        self._metadata_pod_reader = None
        if metadata_transport == 'api-https':
            # Local import avoids a cycle: the fixed reader shares LABEL.
            from backend.onprem.evidence.kubernetes_pod_read import KubernetesPodRead
            self._metadata_pod_reader = KubernetesPodRead(context, namespace)

    @property
    def runtime_policy_digest(self):
        """Pacing/resources/authority scope seal, separate from PDU initializer compatibility."""
        return hashlib.sha256(json.dumps(dict(schema='cityverse.ue-runtime-policy/v1',
            typed_launch_burst=self.typed_launch_burst, typed_launch_max_pending=self.typed_launch_max_pending,
            batch_interval=self.batch_interval, context=self.context, namespace=self.namespace,
            image=self.image, init_image=self.init_image,
            pdu_configuration_digest=getattr(self, 'reservation_configuration_digest', None),
            resource_policy_sha256=getattr(self, 'resource_policy_digest', None)),
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def bind_final_mutation_guard(self, guard):
        """One instance, before lifespan. A recovery owner cannot silently unbind."""
        if not callable(guard) or inspect.iscoroutinefunction(guard) or self._final_mutation_guard is not None:
            raise ValueError('Final Kubernetes mutation guard must bind exactly once')
        self._final_mutation_guard = guard

    @property
    def runner(self):
        # Capture the transport so an existing bounded wrapper can safely wrap
        # this port before binding. Captured ports still consult the live guard.
        transport = self._transport_runner
        def unclassified(*args, **kwargs):
            if self._final_mutation_guard is not None and self._dispatch_port.get() is None:
                raise EvidenceUnavailable('Unclassified Kubernetes dispatch forbidden during owned recovery')
            return transport(*args, **kwargs)
        return unclassified

    @runner.setter
    def runner(self, transport):
        if not callable(transport) or self._final_mutation_guard is not None:
            raise ValueError('Kubernetes transport must be configured before final guard binding')
        self._transport_runner = transport

    def read_runner(self, kind):
        """Explicit trusted read-callsite DI; never classifies arbitrary argv."""
        if type(kind) is not KubeadmReadKind:
            raise ValueError('Typed Kubernetes read kind required')
        def read(*args, **kwargs):
            token = self._dispatch_port.set(kind)
            try:
                return self.runner(*args, **kwargs)
            finally:
                self._dispatch_port.reset(token)
        return read

    def _prepare_mutation(self, kind, target, argv, *, input_text=None):
        if type(kind) is not KubeadmMutationKind or type(target) is not dict or not target:
            raise EvidenceUnavailable('Typed exact Kubernetes mutation target required')
        if not isinstance(argv, (list, tuple)) or not all(type(a) is str for a in argv):
            raise EvidenceUnavailable('Prepared Kubernetes argv required')
        if input_text is not None and type(input_text) is not str:
            raise EvidenceUnavailable('Prepared Kubernetes body must be text')
        return KubeadmMutation(kind, self.context, self.namespace,
            json.dumps(target, sort_keys=True, separators=(',', ':'), allow_nan=False),
            tuple(argv), input_text, str(uuid4()))

    def _write_runner(self, kind, target=None, argv=None, *, on_enter=None, **kwargs):
        mutation=kind if type(kind) is KubeadmMutation else self._prepare_mutation(kind,target,argv,input_text=kwargs.get('input'))
        if type(mutation) is not KubeadmMutation or kwargs.get('input')!=mutation.input_text:
            raise EvidenceUnavailable('Prepared Kubernetes dispatch bytes changed')
        if self._final_mutation_guard is not None:
            try:
                checked = self._final_mutation_guard(mutation)
                if checked is not None:
                    if inspect.iscoroutine(checked):checked.close()
                    raise EvidenceUnavailable('Final Kubernetes guard must complete synchronously')
            except Exception as error:
                # No runner has been invoked. Do not wrap any exception after
                # entry, even when that runner reports zero or an unknown ACK.
                reason=str(error)
                if not re.fullmatch(r'[A-Za-z0-9_ ]{1,120}',reason):
                    reason='Native dispatch rejected before process entry'
                raise NativeDispatchNotSent(mutation,_proof=_NOT_SENT_PROOF,reason=reason) from None
        token = self._dispatch_port.set(mutation)
        try:
            if on_enter is not None:on_enter()
            return self.runner(list(mutation.argv), **kwargs)
        finally:
            self._dispatch_port.reset(token)

    def _dispatch_prepared(self, mutation, *, on_enter=None, **kwargs):
        return self._write_runner(mutation,on_enter=on_enter,**kwargs)

    @staticmethod
    def _kubectl_result(result):
        if result.returncode:
            raise EvidenceUnavailable('Explicit-target Kubernetes operation failed')
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def _read_kubectl(self, *args):
        if self._final_mutation_guard is None:
            return self.kubectl(*args)  # Preserve the ordinary legacy injection seam.
        command = ['kubectl', '--context', self.context, '--namespace', self.namespace, *args]
        return self._kubectl_result(self.read_runner(KubeadmReadKind.METADATA)(command,
            input=None, text=True, capture_output=True, timeout=30, check=False))

    def _write_kubectl(self, kind, target, *args, body=None):
        if self._final_mutation_guard is None:
            return self.kubectl(*args, body=body)
        command = ['kubectl', '--context', self.context, '--namespace', self.namespace, *args]
        prepared = json.dumps(body) if body is not None else None
        return self._kubectl_result(self._write_runner(kind, target, command,
            input=prepared, text=True, capture_output=True, timeout=30, check=False))

    def kubectl(self, *args: str, body=None):
        command = ["kubectl", "--context", self.context, "--namespace", self.namespace, *args]
        result = self.runner(command, input=json.dumps(body) if body is not None else None,
                             text=True, capture_output=True, timeout=30, check=False)
        if result.returncode:
            # Do not leak command output (which can include Secret/config data).
            raise EvidenceUnavailable("Explicit-target Kubernetes operation failed")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def _reconcile_metadata(self, *, namespace=False):
        reader = getattr(self, '_metadata_pod_reader', None)
        if reader is None:
            return (self._read_kubectl('get', 'namespace', self.namespace, '-o', 'json') if namespace
                    else self._read_kubectl('get', 'pods', '-l', LABEL, '-o', 'json'))
        deadline = time.monotonic() + 5
        try:
            return (reader.namespace_record(deadline=deadline) if namespace
                    else reader.pods(deadline=deadline, managed=True))
        except EvidenceUnavailable:
            raise EvidenceUnavailable('Explicit-target Kubernetes operation failed') from None

    def read_pods(self):
        reader = getattr(self, '_metadata_pod_reader', None)
        if reader is None:
            return self._read_kubectl('get', 'pods', '-o', 'json')
        return reader.pods(deadline=time.monotonic() + 5)

    def read_pod(self, name):
        reader = getattr(self, '_metadata_pod_reader', None)
        if reader is None:
            return self._read_kubectl('get', 'pod', name, '-o', 'json')
        return reader.pod(name, deadline=time.monotonic() + 5)

    def _gnb_identity(self, pod: dict, name: str):
        meta = pod.get('metadata', {})
        pod_name, uid = meta.get('name'), meta.get('uid')
        container_name = 'gnb-' + name
        specs = [c for c in pod.get('spec', {}).get('containers', []) if c.get('name') == container_name]
        statuses = [c for c in pod.get('status', {}).get('containerStatuses', []) if c.get('name') == container_name]
        if (not isinstance(pod_name, str) or not pod_name.startswith(container_name + '-')
                or meta.get('namespace') != self.namespace or not isinstance(uid, str) or not uid
                or meta.get('deletionTimestamp') or pod.get('status', {}).get('phase') != 'Running'
                or len(specs) != 1 or len(statuses) != 1
                or type(statuses[0].get('restartCount')) is not int
                or statuses[0]['restartCount'] < 0
                or not re.fullmatch(r'[^:]+://\S+', statuses[0].get('containerID', ''))):
            raise EvidenceUnavailable('gNB context observation unavailable')
        return {'pod': pod_name, 'uid': uid, 'container': container_name,
                'containerID': statuses[0]['containerID'],
                'restartCount': statuses[0]['restartCount']}

    def _check_gnb_identity(self, name: str, expected: dict):
        fresh = self.read_pod(expected['pod'])
        current = self._gnb_identity(fresh, name)
        if current != expected:
            raise EvidenceUnavailable('gNB context observation unavailable')
        return current

    def observe_gnb_contexts(self):
        """Read four gNB UE context counts without mutating RAN state."""
        if self.device_profile is not None:
            from backend.onprem.evidence.device_ran import NativeDeviceRanReader
            reader=getattr(self,'device_ran_reader',None)
            if not isinstance(reader,NativeDeviceRanReader) or reader.driver is not self:
                raise EvidenceUnavailable('Commissioned isolated gNB context reader unavailable')
            return reader.context_proof()
        try:
            pods = self._read_kubectl('get', 'pods', '-o', 'json').get('items', [])
            result = {}
            for name in SLICES:
                matches = [p for p in pods if p.get('metadata', {}).get('name', '').startswith('gnb-' + name + '-')]
                if len(matches) != 1:
                    raise EvidenceUnavailable('gNB context observation unavailable')
                identity = self._gnb_identity(matches[0], name)
                self._check_gnb_identity(name, identity)
                base = ['kubectl', '--context', self.context, '--namespace', self.namespace,
                        'exec', identity['pod'], '-c', identity['container'], '--',
                        'timeout', '5', '/ueransim/nr-cli']
                dumped = self.read_runner(KubeadmReadKind.NATIVE_PROBE)(base + ['--dump'], text=True, capture_output=True, timeout=12, check=False)
                node = dumped.stdout.strip() if dumped.returncode == 0 else ''
                if not re.fullmatch(r'\S+', node):
                    raise EvidenceUnavailable('gNB context observation unavailable')
                counted = self.read_runner(KubeadmReadKind.NATIVE_PROBE)(base + [node, '--exec', 'ue-count'],
                                      text=True, capture_output=True, timeout=12, check=False)
                count = counted.stdout.strip() if counted.returncode == 0 else ''
                if not re.fullmatch(r'[0-9]+', count):
                    raise EvidenceUnavailable('gNB context observation unavailable')
                self._check_gnb_identity(name, identity)
                result[name] = {**identity, 'node': node, 'ueCount': int(count),
                                'observedAt': timestamp(time.time())}
            return {'source': 'native-gnb-nr-cli-ue-count',
                    'scope': 'four-kubeadm-gnb-pods',
                    'observedAt': timestamp(min(seconds(row['observedAt']) for row in result.values())),
                    'readouts': result}
        except (subprocess.TimeoutExpired, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise EvidenceUnavailable('gNB context observation unavailable') from None

    def pod_manifest(self, generation: int, index: int, shard: dict):
        if 'device' in shard:
            from .device_sessions import DeviceAllocation,UeransimDeviceCompiler,DEVICE_SPATIAL_FIELDS
            if self.device_profile is None:
                raise EvidenceUnavailable('Device generation requires explicit multi-session driver profile')
            return UeransimDeviceCompiler(self.device_profile).pod_manifest(
                self,generation,index,DeviceAllocation.from_dict(shard['device']),
                spatial_context={k:shard[k] for k in DEVICE_SPATIAL_FIELDS if k in shard})
        name = shard["slice"]
        manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": f"cv-ue-g{generation}-{index}", "namespace": self.namespace,
            "labels": {"cityverse.io/managed-by": "onprem-ue-controller", "cityverse.io/generation": str(generation), "cityverse.io/slice": name},
            "annotations": {"cityverse.io/supis": json.dumps(shard["supis"])}},
            "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 30,
                     "automountServiceAccountToken": False, "nodeSelector": {"cityverse.io/role": "ran"},
                     "initContainers": [{"name": "config", "image": self.init_image,
                                         "command": ["python", "-c", CONFIG_INIT],
                                         "env": [{"name": key, "value": str(value)} for key, value in {
                                             "GROUP": shard["credentialGroup"], "SST": SLICES[name]["sst"], "SD": SLICES[name]["sd"],
                                             "SUPI": shard["supis"][0], "GNB": shard.get("spatialAccess", {}).get("endpoint", f"gnb-{name}")}.items()],
                                         "volumeMounts": [{"name": "credentials", "mountPath": "/credentials", "readOnly": True}, {"name": "config", "mountPath": "/run/ue"}]}],
                     "containers": [{"name": "ue", "image": self.image,
                                     "command": ["/ueransim/nr-ue", "-c", "/run/ue/config.yaml", "-t", str(self.tempo_ms), "-n", str(len(shard["supis"]))],
                                     "securityContext": {"privileged": True},
                                     "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "1Gi"}},
                                     "volumeMounts": [{"name": "config", "mountPath": "/run/ue", "readOnly": True}, {"name": "tun", "mountPath": "/dev/net/tun"}]}],
                     "volumes": [{"name": "credentials", "secret": {"secretName": "cityverse-ue-credentials", "defaultMode": 256}},
                                 {"name": "config", "emptyDir": {"medium": "Memory"}},
                                 {"name": "tun", "hostPath": {"path": "/dev/net/tun", "type": "CharDevice"}}]}}
        if self.traffic_enabled:
            # Shared Pod network namespace exposes actual UE TUNs without a
            # device mount or NET_ADMIN; workers never alter interfaces/routes.
            manifest["spec"]["containers"].append({"name": "traffic", "image": TRAFFIC_IMAGE,
                # Keep Bash as PID 1 to reap orphaned exec-worker descendants.
                "command": ["bash", "-c", "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"], "add": ["NET_RAW"]}},
                # A 50 UE bidirectional batch starts 100 bounded client
                # process trees in this sidecar, matching receiver capacity.
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"memory": TRAFFIC_CLIENT_MEMORY_LIMIT}},
                "volumeMounts": [{"name": "traffic-tmp", "mountPath": "/tmp"}]})
            manager_container(manifest["spec"]["containers"][-1], image=TRAFFIC_IMAGE)
            # Detached batches retain one sealed aggregate per token.  64 MiB
            # covers several measured five-shard rounds plus the bounded
            # in-flight duplicate set; it is a limit, not a reservation.
            manifest["spec"]["volumes"].append({"name": "traffic-tmp", "emptyDir": {"sizeLimit": "64Mi"}})
        if shard.get('spatialAccess'):
            manifest['metadata']['annotations']['cityverse.io/spatial-access'] = json.dumps(shard['spatialAccess'], sort_keys=True)
        return manifest

    def retirement_required(self, generation):
        pods = self._reconcile_metadata().get('items', [])
        if not sum(generation['desired'].values()):
            # A zero intent needs release-only evidence while old Pods remain.
            # Once they are gone, resume full RAN observation so first-run
            # bootstrap can verify gNB association before creating a new UE.
            return bool(pods)
        return any(pod['metadata']['labels'].get('cityverse.io/generation') != str(generation['generation']) for pod in pods)

    def _spatial_device_gnb_gate(self, record, evidence):
        """Validate the selected vendor's commissioned native gNB incarnation."""
        from .device_sessions import DEVICE_SPATIAL_FIELDS, validate_device_spatial_context
        if self.device_profile is None:
            raise EvidenceUnavailable('Spatial device requires commissioned native driver profile')
        context = validate_device_spatial_context({k:record[k] for k in DEVICE_SPATIAL_FIELDS if k in record}, self.device_profile)
        if self.device_profile.driver == 'free-ran-ue':
            from .spatial_ran import configured_registry, native_readouts
            registry = configured_registry()
            access = context['spatialAccess']
            candidates = [r for r in registry['gnbs'] if r['gnbId']==access['gnbId']
                and r['cellId']==access['cellId'] and r['endpoint']==access['endpoint']
                and record['slice'] in r['slices']]
            if len(candidates) != 1:
                raise EvidenceUnavailable('Spatial FreeRAN device gNB is not commissioned for slice')
            gate = native_readouts(self, registry).get(access['gnbId'], {})
            if (gate.get('source') != 'free-ran-ue-api+native-ngap-sctp/v1'
                    or gate.get('available') is not True or gate.get('ngapAssociated') is not True
                    or gate.get('podUid') != access['gnbPodUid'] or gate.get('endpoint') != access['endpoint']
                    or not -1 <= time.time()-seconds(gate.get('observedAt')) <= 15):
                raise EvidenceUnavailable('Spatial FreeRAN native gNB differs from commissioned UID pin')
            return gate
        gate = evidence.get('gnb', {}).get(record['slice'], {})
        if context and (gate.get('source') != 'native-ngap-sctp/v2' or gate.get('available') is not True
                or gate.get('gnbSearchEndpoint') != self.device_profile.gnb_search_endpoint
                or gate.get('podUid') != context['spatialAccess']['gnbPodUid']
                or gate.get('sourceRevision') != self.device_profile.source_revision
                or gate.get('imageDigest') != self.device_profile.image_digest
                or not gate.get('processEpoch') or not gate.get('n3NetworkPolicy')):
            raise EvidenceUnavailable('Spatial device native gNB differs from commissioned profile or UID pin')
        return gate

    def reconcile(self, generation: dict, evidence: dict):
        now = time.time()
        if evidence.get("generation") != generation["generation"] or not -1 <= now - seconds(evidence.get("observedAt")) <= 15:
            raise EvidenceUnavailable("Fresh generation evidence is required before Kubernetes mutations")
        for name in ("ue", "amf", "smf", "upf"):
            source = evidence.get("sources", {}).get(name, {})
            # UPF and UE probe evidence is sampled on separate VMs. Match the
            # collector's bounded five-second cross-host skew allowance;
            # AMF/SMF receipt timestamps originate on this host.
            future_skew = 5 if name in {"ue", "upf"} else 1
            if source.get("available") is not True or not -future_skew <= now - seconds(source.get("observedAt")) <= 15:
                raise EvidenceUnavailable(f"{name} source is required before Kubernetes mutations")
        # An explicit namespace opt-in prevents a context alias pointing at EKS.
        namespace = self._reconcile_metadata(namespace=True)
        if namespace.get("metadata", {}).get("labels", {}).get("cityverse.io/platform") != "kubeadm":
            raise EvidenceUnavailable("Namespace has not passed kubeadm platform preflight")
        pods = self._reconcile_metadata().get("items", [])
        for pod in pods:
            label = pod.get('metadata', {}).get('labels', {}).get('cityverse.io/generation', '')
            if not isinstance(label, str) or not label.isdigit() or int(label) > generation['generation']:
                raise EvidenceUnavailable('Managed UE Pod generation is malformed or newer than current intent')
        old = [pod for pod in pods if int(pod['metadata']['labels']['cityverse.io/generation']) < generation['generation']]
        if old:
            if len(old) != len(pods):
                raise EvidenceUnavailable('Old and current UE Pod allocations coexist; retirement ownership is ambiguous')
            self._retire_cohort(old, evidence, maximum_generation=generation['generation'] - 1)
            return  # Do not reassign any SUPI before old contexts disappear.
        current_pods = {pod["metadata"]["name"]: pod for pod in pods}
        # Collector must scope rows to this runtime, including residual older UEs.
        for row in evidence.get("ues", []):
            if row.get("generation") != generation["generation"]:
                raise EvidenceUnavailable("Waiting for previous generation registration/session/tunnel release")
        if not sum(generation['desired'].values()):
            return  # Zero has no new launch; subscriber/gNB preflight is irrelevant to release.
        if evidence.get("subscriberPool", {}).get("verifiedCount") != 1000 or evidence["subscriberPool"].get("credentialsMatched") is not True:
            raise EvidenceUnavailable("Subscriber pool has not been verified")
        typed = any('device' in row for row in generation['allocation'])
        launch_limit = 1
        if typed:
            if len(generation['allocation']) > 100 or any('device' not in row for row in generation['allocation']):
                raise EvidenceUnavailable('Typed launch requires a complete bounded 100-device allocation')
            if any(row.get('runtimePolicyDigest') != self.runtime_policy_digest for row in generation['allocation']):
                raise EvidenceUnavailable('Typed launch runtime policy differs from the original held reservation')
            pending = sum(pod.get('metadata', {}).get('deletionTimestamp') is not None
                          or not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                                     for c in pod.get('status', {}).get('conditions', ())) for pod in pods)
            launch_limit = min(self.typed_launch_burst, self.typed_launch_max_pending - pending)
            if launch_limit <= 0:
                return
        launched = 0
        previous_launch = self.last_launch
        for index, shard in enumerate(shards(generation["allocation"], self.batch_size)):
            index = shard.get('podIndex', index)
            pod_name = f"cv-ue-g{generation['generation']}-{index}"
            if pod_name in current_pods:
                continue
            if shard.get('spatialAccess') and 'device' in shard:
                gate = self._spatial_device_gnb_gate(shard, evidence)
            elif shard.get('spatialAccess'):
                from .spatial_ran import configured_registry, native_readouts
                registry = configured_registry()
                access = shard['spatialAccess']
                rows = native_readouts(self, registry)
                gate = rows.get(access['gnbId'], {})
                if (gate.get('endpoint') != access['endpoint'] or gate.get('cellId') != access['cellId']
                        or not access.get('gnbPodUid') or gate.get('podUid') != access['gnbPodUid']):
                    raise EvidenceUnavailable('Commissioned launch gNB binding changed')
            else:
                gate = evidence.get("gnb", {}).get(shard["slice"], {})
            # A spatial native read occurs after entry. Compare its timestamp
            # with the current clock, never the pre-read timestamp (which can
            # make a valid newly observed gNB appear to be future-dated).
            now = time.time()
            if (not -1 <= now - seconds(evidence.get('observedAt')) <= 15
                    or any(not -(5 if name in {'ue', 'upf'} else 1) <=
                        now - seconds(evidence['sources'][name]['observedAt']) <= 15
                        for name in ('ue', 'amf', 'smf', 'upf'))):
                raise EvidenceUnavailable('Native source evidence expired during launch preparation')
            if gate.get("ngapAssociated") is not True or not -1 <= now - seconds(gate.get("observedAt")) <= 15:
                raise EvidenceUnavailable("gNB NGAP association is not verified")
            if not launched and now - previous_launch < self.batch_interval:
                return
            self._write_kubectl(KubeadmMutationKind.POD_APPLY, {"generation": generation["generation"], "allocation": generation["allocation"], "pod_name": pod_name},
                "apply", "-f", "-", "-o", "json", body=self.pod_manifest(generation["generation"], index, shard))
            self.last_launch = now
            launched += 1
            if launched >= launch_limit:
                return  # Every Pod still has its own guarded apply and immutable manifest.

    def population_create(self, generation, record, evidence):
        """One journalled device create. Caller never replays this dispatch."""
        now=time.time()
        if self.device_profile is None or evidence.get('generation')!=generation or not -1<=now-seconds(evidence.get('observedAt'))<=15:
            raise EvidenceUnavailable('Population create requires current device evidence')
        for name in ('ue','amf','smf','upf'):
            source=evidence.get('sources',{}).get(name,{})
            if source.get('available') is not True or not -1<=now-seconds(source.get('observedAt'))<=15:
                raise EvidenceUnavailable('Population create native gate unavailable')
        namespace=self._reconcile_metadata(namespace=True)
        if namespace.get('metadata',{}).get('labels',{}).get('cityverse.io/platform')!='kubeadm':
            raise EvidenceUnavailable('Population namespace preflight missing')
        if evidence.get('subscriberPool',{}).get('verifiedCount')!=1000 or evidence['subscriberPool'].get('credentialsMatched') is not True:
            raise EvidenceUnavailable('Population subscriber pool not commissioned')
        gate=(self._spatial_device_gnb_gate(record,evidence) if record.get('spatialAccess')
              else evidence.get('gnb',{}).get(record['slice'],{}))
        if gate.get('ngapAssociated') is not True or not -1<=now-seconds(gate.get('observedAt'))<=15:
            raise EvidenceUnavailable('Population access gNB not associated')
        unit=shards([record],self.batch_size)[0]
        self._write_kubectl(KubeadmMutationKind.POD_CREATE, {'generation':generation,'record':record},
            'create','-f','-','-o','json',body=self.pod_manifest(generation,record['podIndex'],unit))

    def owned_failed_drain_pods(self,current,owned_snapshot):
        """Read exact terminal owned Pods; no native mutation or running bypass."""
        expected={}
        for fallback,unit in enumerate(shards(current['allocation'],self.batch_size)):
            manifest=self.pod_manifest(current['generation'],unit.get('podIndex',fallback),unit)
            expected[manifest['metadata']['name']]=manifest
        pins={}
        for row in [*owned_snapshot.get('perUe',[]),*owned_snapshot.get('devices',[])]:
            if row.get('supi') and row.get('podUid'):
                if row['supi'] in pins and pins[row['supi']]!=row['podUid']:
                    raise EvidenceUnavailable('Owned drain original Pod identities conflict')
                pins[row['supi']]=row['podUid']
        records=[]
        for pod in self.read_pods().get('items',[]):
            meta=pod.get('metadata',{})
            if meta.get('labels',{}).get('cityverse.io/managed-by')!='onprem-ue-controller':
                continue
            reference=expected.get(meta.get('name'))
            if (reference is None or not meta.get('uid') or meta.get('namespace')!=self.namespace
                    or any(meta.get('labels',{}).get(k)!=v for k,v in reference['metadata']['labels'].items())
                    or any(meta.get('annotations',{}).get(k)!=v for k,v in reference['metadata'].get('annotations',{}).items())):
                raise EvidenceUnavailable('Owned drain residual Pod ownership differs')
            spec,status=pod.get('spec',{}),pod.get('status',{})
            if (any(spec.get(k)!=reference['spec'].get(k) for k in ('volumes','nodeSelector'))
                    or (spec.get('securityContext') or {})!=(reference['spec'].get('securityContext') or {})
                    or any(spec.get(k) for k in ('hostNetwork','hostPID','hostIPC','shareProcessNamespace','ephemeralContainers'))):
                raise EvidenceUnavailable('Owned drain residual Pod namespace or volume specification changed')
            quiescent_device=False
            if self.device_profile is not None and status.get('phase')=='Running':
                states={c['name']:c for c in status.get('containerStatuses',[])}
                ue=states.get('ue',{})
                if (set(states)=={'ue','traffic'} and ue.get('state',{}).get('terminated',{}).get('exitCode')==0
                        and ue.get('ready') is False and states['traffic'].get('restartCount')==0
                        and len(status.get('initContainerStatuses',[]))==len(spec.get('initContainers',[]))
                        and all(row.get('state',{}).get('terminated',{}).get('exitCode')==0
                                for row in status.get('initContainerStatuses',[]))):
                    probe=self.read_runner(KubeadmReadKind.PROCESS)(['kubectl','--context',self.context,'--namespace',self.namespace,'exec',meta['name'],'-c','traffic','--','sh','-c',
                        'printf "SELF %s\\n" "$$"; readlink /proc/1/exe; for p in /proc/[0-9]*; do read -r comm < "$p/comm" || exit 1; printf "%s %s\\n" "${p##*/}" "$comm"; done'],text=True,capture_output=True,timeout=8,check=False)
                    lines=probe.stdout.splitlines()
                    if probe.returncode==0 and len(lines)==4 and lines[0].startswith('SELF ') and lines[1]=='/usr/local/bin/cityverse-traffic-manager':
                        self_pid=lines[0].split()[1]
                        process_rows=[line.split(' ',1) for line in lines[2:]]
                        quiescent_device=(set(row[0] for row in process_rows)=={'1',self_pid}
                            and next(row[1] for row in process_rows if row[0]==self_pid)=='sh'
                            and next(row[1] for row in process_rows if row[0]=='1').startswith('cityverse-traff'))
            if (not quiescent_device and (status.get('phase') not in {'Failed','Succeeded'}
                    or any(s.get('state',{}).get('running') or s.get('ready') is True
                           for category in ('containerStatuses','initContainerStatuses') for s in status.get(category,[]))) or spec.get('restartPolicy')!='Never'):
                raise EvidenceUnavailable('Owned quarantine drain refuses a potentially running Pod')
            for category in ('containers','initContainers'):
                observed={c['name']:c for c in spec.get(category,[])}
                wanted={c['name']:c for c in reference['spec'].get(category,[])}
                from .sealed_container import sealed_container_fields
                if set(observed)!=set(wanted) or any(not sealed_container_fields(observed[name],row,
                        ('image','command','args','env','envFrom','volumeMounts','securityContext')) for name,row in wanted.items()):
                    raise EvidenceUnavailable('Owned drain residual Pod launch specification changed')
            supis=json.loads(meta['annotations']['cityverse.io/supis'])
            if any(s in pins and pins[s]!=meta['uid'] for s in supis):
                raise EvidenceUnavailable('Owned drain residual Pod UID differs from original round')
            records.append({'name':meta['name'],'uid':meta['uid'],'generation':current['generation'],
                'supis':supis,'phase':status['phase'],'containerStates':status.get('containerStatuses',[]),
                'initStates':status.get('initContainerStatuses',[])})
        return sorted(records,key=lambda r:r['name'])

    def _quiescent_device_sidecar(self,pod):
        """A terminated native UE cannot send NAS; manager must have no child."""
        if self.device_profile is None or pod.get('status',{}).get('phase')!='Running':return False
        states={r['name']:r for r in pod['status'].get('containerStatuses',[])}
        if (set(states)!={'ue','traffic'} or states['ue'].get('state',{}).get('terminated',{}).get('exitCode')!=0
                or states['ue'].get('ready') is not False or states['traffic'].get('restartCount')!=0
                or len(pod['status'].get('initContainerStatuses',[]))!=len(pod['spec'].get('initContainers',[]))
                or any(r.get('state',{}).get('terminated',{}).get('exitCode')!=0 for r in pod['status'].get('initContainerStatuses',[]))):return False
        probe=self.read_runner(KubeadmReadKind.PROCESS)(['kubectl','--context',self.context,'--namespace',self.namespace,'exec',pod['metadata']['name'],'-c','traffic','--','sh','-c',
            'printf "SELF %s\\n" "$$"; readlink /proc/1/exe; for p in /proc/[0-9]*; do read -r comm < "$p/comm" || exit 1; printf "%s %s\\n" "${p##*/}" "$comm"; done'],text=True,capture_output=True,timeout=8,check=False)
        lines=probe.stdout.splitlines()
        if probe.returncode or len(lines)!=4 or not lines[0].startswith('SELF ') or lines[1]!='/usr/local/bin/cityverse-traffic-manager':return False
        rows=[line.split(' ',1) for line in lines[2:]];self_pid=lines[0].split()[1]
        return (all(len(r)==2 for r in rows) and {r[0] for r in rows}=={'1',self_pid}
            and next(r[1] for r in rows if r[0]==self_pid)=='sh' and next(r[1] for r in rows if r[0]=='1').startswith('cityverse-traff'))

    def population_deregister(self,pod,supi):
        """Dispatch NAS disable-5g once, never infer release from its ACK."""
        meta=pod['metadata']
        current=self.read_pod(meta['name'])
        if (not meta.get('uid') or current['metadata'].get('uid')!=meta['uid']
                or json.loads(current['metadata'].get('annotations',{}).get('cityverse.io/supis','[]'))!=[supi]
                or current['metadata'].get('labels',{}).get('cityverse.io/managed-by')!='onprem-ue-controller'):
            raise EvidenceUnavailable('Population deregistration Pod identity changed')
        result=self._write_runner(KubeadmMutationKind.NAS_DEREGISTER, {'pod':current,'supis':[supi]}, ['kubectl','--context',self.context,'--namespace',self.namespace,'exec',meta['name'],
            '-c','ue','--','/ueransim/nr-cli',supi,'--exec','deregister disable-5g'],text=True,capture_output=True,timeout=12,check=False)
        if result.returncode or result.stdout.strip()!='De-registration procedure triggered':
            raise EvidenceUnavailable('Population deregistration acknowledgement unavailable')

    @staticmethod
    def ue_quiescent(state):
        return state.get('mm-state') == 'MM-NULL' or (state.get('mm-state') == 'MM-DEREGISTERED/NO-SUPI'
            and state.get('sim-inserted') is False and state.get('has-emergency') is False)

    def ue_states(self, name, supis):
        import yaml
        lines = ['set -eu']
        for index, supi in enumerate(supis):
            lines.append(f"(value=$(/ueransim/nr-cli {shlex.quote(supi)} --exec status); printf '%s\\t%s\\n' {shlex.quote(supi)} \"$(printf '%s' \"$value\" | base64 | tr -d '\\n')\") &")
            if (index + 1) % 32 == 0: lines.append('wait')
        lines.append('wait')
        result = self.read_runner(KubeadmReadKind.PROCESS)(['kubectl', '--context', self.context, '--namespace', self.namespace,
                              'exec', name, '-c', 'ue', '--', 'sh', '-c', '\n'.join(lines)],
                             text=True, capture_output=True, timeout=12, check=False)
        states = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.split('\t')
                if len(fields) != 2 or fields[0] in states:
                    raise EvidenceUnavailable('Malformed UE quiescence observation')
                value = yaml.safe_load(base64.b64decode(fields[1], validate=True))
                if not isinstance(value, dict): raise EvidenceUnavailable('Malformed UE state')
                states[fields[0]] = value
        if set(states) != set(supis): raise EvidenceUnavailable('Incomplete UE state observation')
        return states

    @staticmethod
    def _nas_script(supis, command):
        lines=['set -eu']
        for index,supi in enumerate(supis):
            lines.append(f"(value=$(/ueransim/nr-cli {shlex.quote(supi)} --exec '{command}'); printf '%s\\t%s\\n' {shlex.quote(supi)} \"$(printf '%s' \"$value\" | base64 | tr -d '\\n')\") &")
            if (index+1)%32==0:lines.append('wait')
        lines.append('wait')
        return '\n'.join(lines)

    def retire_owned_pod(self, pod: dict, evidence: dict):
        """Ask real UEs to deregister, then wait for independently observed release.

        A bounded cohort per reconciliation limits subprocess work. A command ACK
        is never treated as proof that AMF/SMF/kernel state has been released.
        """
        metadata = pod['metadata']
        name, uid = metadata['name'], metadata.get('uid')
        fresh = self._read_kubectl('get', 'pod', name, '-o', 'json')
        current = fresh.get('metadata', {})
        if (not uid or current.get('uid') != uid
                or current.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'
                or current.get('labels', {}).get('cityverse.io/generation') != metadata.get('labels', {}).get('cityverse.io/generation')):
            raise EvidenceUnavailable('Owned UE Pod identity changed before deregistration')
        supis = json.loads(current.get('annotations', {}).get('cityverse.io/supis', '[]'))
        if supis != json.loads(metadata.get('annotations', {}).get('cityverse.io/supis', '[]')):
            raise EvidenceUnavailable('Owned UE Pod SUPI allocation changed before deregistration')
        if not isinstance(supis, list) or not 1 <= len(supis) <= self.batch_size or len(set(supis)) != len(supis) or any(not isinstance(supi, str) or not re.fullmatch(r'imsi-[0-9]{15}', supi) for supi in supis):
            raise EvidenceUnavailable('Owned UE Pod SUPI list is invalid')
        if current.get('annotations', {}).get(TERMINAL_SHUTDOWN):
            if self.terminal_authorizer is None:
                raise EvidenceUnavailable('Terminal shutdown recovery authority unavailable')
            return self.retire_terminal_owned_pod(pod, evidence, lambda: self.terminal_authorizer(pod))
        terminated_device=self._quiescent_device_sidecar(fresh)
        if fresh.get('status', {}).get('phase') == 'Running' and not terminated_device:
            states = self.ue_states(name, supis)
            registered = [supi for supi, state in states.items() if state.get('mm-state') == 'MM-REGISTERED/NORMAL-SERVICE']
            quiescent = all(self.ue_quiescent(state) for state in states.values())
            if not registered and not quiescent:
                if self.terminal_authorizer is not None:
                    return self.retire_terminal_owned_pod(pod, evidence, lambda: self.terminal_authorizer(pod))
                raise EvidenceUnavailable('Waiting for UE registration state before normal deregistration')
            if not registered and uid not in self._deregistered_pod_uids:
                self._deregistered_pod_uids[uid] = time.time()
                return  # First actual quiescence observation; require later native release evidence.
        else:
            registered = []
        if registered:
            before_command = self._read_kubectl('get', 'pod', name, '-o', 'json').get('metadata', {})
            if (before_command.get('uid') != uid
                    or before_command.get('labels', {}).get('cityverse.io/generation') != metadata['labels'].get('cityverse.io/generation')
                    or before_command.get('annotations', {}).get('cityverse.io/supis') != current.get('annotations', {}).get('cityverse.io/supis')):
                raise EvidenceUnavailable('Owned UE Pod changed before normal deregistration command')
            for supi in registered:
                count, previous = self._deregister_attempts.get((uid, supi), (0, 0))
                if count >= 3 or (count and time.time() - previous < 30):
                    raise EvidenceUnavailable('Normal UE deregistration retry is bounded; wait or inspect protocol failure')
            owned_nas=callable(self.owned_native_nas)
            nas_command='deregister switch-off' if owned_nas else 'deregister disable-5g'
            nas_script=self._nas_script(registered,nas_command)
            prepared=self._prepare_mutation(KubeadmMutationKind.NAS_DEREGISTER,
                {'pod':fresh,'supis':registered}, ['kubectl','--context',self.context,'--namespace',self.namespace,
                    'exec',name,'-c','ue','--','sh','-c',nas_script])
            def dispatch():
                # Bookkeeping advances only after the final guard succeeds.
                def entered():
                    for supi in registered:
                        count, _ = self._deregister_attempts.get((uid, supi), (0, 0))
                        self._deregister_attempts[(uid, supi)] = (count + 1, time.time())
                return self._dispatch_prepared(prepared,on_enter=entered,
                    text=True,capture_output=True,timeout=12,check=False)
            dispatch.native_dispatch_request=prepared
            nas_target=None
            if owned_nas:nas_target,result=self.owned_native_nas(fresh,dispatch)
            else:result=dispatch()
            acknowledgements = {}
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    fields = line.split('\t')
                    if len(fields) != 2 or fields[0] in acknowledgements:
                        raise EvidenceUnavailable('Malformed UE deregistration acknowledgement')
                    acknowledgements[fields[0]] = base64.b64decode(fields[1], validate=True).decode().strip()
            if set(acknowledgements) != set(registered) or any(value != 'De-registration procedure triggered' for value in acknowledgements.values()):
                raise EvidenceUnavailable('UE deregistration acknowledgement incomplete')
            after = self._read_kubectl('get', 'pod', name, '-o', 'json')
            if after.get('metadata', {}).get('uid') != uid:
                raise EvidenceUnavailable('UE Pod changed during deregistration')
            if owned_nas:
                # The native adapter journals dispatch before IO. ACK remains
                # diagnostic: only later exact terminal/native census advances.
                # A lost ACK is never permission to send a second command.
                self.owned_native_nas_ack(nas_target)
            self._deregistered_pod_uids[uid] = time.time()
            return
        if terminated_device and callable(self.owned_native_teardown):
            # The adapter checks every pinned UE terminal/idle, restores exact
            # orphan core contexts, and never deletes Pods or replaces NAS.
            # The existing evidence below remains mandatory; a fresh next poll
            # must independently observe core disappearance before Pod deletion.
            self.owned_native_teardown()
        for row in evidence.get('ues', []):
            if str(row.get('supi', '')).startswith('residual-upf-'):
                raise EvidenceUnavailable('Waiting for unattributed native UPF residual release')
            if row.get('supi') in supis and (row.get('amf', {}).get('registered') is True
                    or row.get('smf', {}).get('sessionCount', 0) > 0
                    or row.get('upf', {}).get('pfcpSeid')):
                raise EvidenceUnavailable('Waiting for native AMF/SMF/UPF release after UE deregistration')
        acknowledged = self._deregistered_pod_uids.get(uid)
        if any(not evidence.get('sources', {}).get(source, {}).get('observedAt') for source in ('amf', 'smf', 'upf')):
            raise EvidenceUnavailable('Waiting for native post-deregistration source observations')
        if acknowledged is not None and any(seconds(evidence.get('sources', {}).get(source, {}).get('observedAt')) <= acknowledged for source in ('amf', 'smf', 'upf')):
            raise EvidenceUnavailable('Waiting for native post-deregistration source observations')
        now = time.time()
        if any(evidence.get('sources', {}).get(source, {}).get('available') is not True
               or not -(5 if source == 'upf' else 1) <= now - seconds(evidence.get('sources', {}).get(source, {}).get('observedAt')) <= 15
               for source in ('amf', 'smf', 'upf')):
            raise EvidenceUnavailable('Waiting for fresh native AMF/SMF/UPF release evidence')
        # nr-cli ACK only means the command was queued. UERANSIM can cancel
        # deregistration while authenticating and later register again. MM-NULL
        # is the actual disabled NAS state; RM-REGISTERED may remain stale.
        if fresh.get('status', {}).get('phase') == 'Running' and not terminated_device:
            quiescent_at = self._quiescent_pod_uids.get(uid)
            if quiescent_at is None:
                states = self.ue_states(name, supis)
                if not all(self.ue_quiescent(state) for state in states.values()):
                    raise EvidenceUnavailable('Waiting for actual UE MM-NULL quiescence; command ACK is not shutdown proof')
                # Recollect AMF/SMF/UPF after this observation.  Reusing the
                # pre-quiescence snapshot can expire during a slow kubectl exec
                # and cannot prove that release followed MM-NULL.
                self._quiescent_pod_uids[uid] = time.time()
                return
            if any(seconds(evidence['sources'][source]['observedAt']) <= quiescent_at
                   for source in ('amf', 'smf', 'upf')):
                raise EvidenceUnavailable('Waiting for post-quiescence native release evidence')
            # Quiescence is not sticky: UERANSIM may re-register after the
            # earlier MM-NULL observation.  Never delete from cached proof.
            states = self.ue_states(name, supis)
            if not all(self.ue_quiescent(state) for state in states.values()):
                self._quiescent_pod_uids.pop(uid, None)
                raise EvidenceUnavailable('UE re-registered after quiescence; normal deregistration retry required')
            current = self._read_kubectl('get', 'pod', name, '-o', 'json').get('metadata', {})
            if current.get('uid') != uid or current.get('annotations', {}).get('cityverse.io/supis') != fresh['metadata'].get('annotations', {}).get('cityverse.io/supis'):
                raise EvidenceUnavailable('UE Pod changed during quiescence observation')
        if terminated_device:
            actual=self._read_kubectl('get','pod',name,'-o','json')
            if actual.get('metadata',{}).get('uid')!=uid or not self._quiescent_device_sidecar(actual):
                raise EvidenceUnavailable('Owned terminated device or idle manager changed before retirement')
        self.delete_owned_pod(fresh)
        self._quiescent_pod_uids.pop(uid, None)

    def _retire_cohort(self, pods: list, evidence: dict, *, maximum_generation: int, watchdog_ack_only=False):
        """Advance up to four independent Pods, retaining ordinary delete grace.

        Called under UEController's generation lock, sequentially (no shared
        SQLite/thread mutations). Validate the entire cohort before any work.
        A terminating Pod still blocks reallocation, but cannot monopolize the
        next Pod's NAS release while kubelet completes its standard grace.
        """
        seen_uids, seen_names, seen_supis = set(), set(), set()
        for pod in pods:
            meta = pod.get('metadata', {})
            labels = meta.get('labels', {})
            label = labels.get('cityverse.io/generation', '')
            uid, name = meta.get('uid'), meta.get('name')
            if (labels.get('cityverse.io/managed-by') != 'onprem-ue-controller'
                    or not isinstance(label, str) or not re.fullmatch(r'[1-9][0-9]*', label)
                    or int(label) > maximum_generation or not isinstance(uid, str) or not uid
                    or not isinstance(name, str) or not re.fullmatch(r'cv-ue-g' + label + r'-[0-9]+', name)
                    or uid in seen_uids or name in seen_names):
                raise EvidenceUnavailable('Retirement cohort ownership or generation is inconsistent')
            try:
                supis = json.loads(meta.get('annotations', {}).get('cityverse.io/supis', '[]'))
            except (TypeError, ValueError):
                raise EvidenceUnavailable('Retirement cohort SUPI allocation is malformed') from None
            if (not isinstance(supis, list) or not 1 <= len(supis) <= self.batch_size
                    or any(not isinstance(s, str) or not re.fullmatch(r'imsi-[0-9]{15}', s) for s in supis)
                    or len(set(supis)) != len(supis) or seen_supis.intersection(supis)):
                raise EvidenceUnavailable('Retirement cohort SUPI allocations overlap or are malformed')
            seen_uids.add(uid)
            seen_names.add(name)
            seen_supis.update(supis)
        pending = sorted((p for p in pods if not p['metadata'].get('deletionTimestamp')),
                         key=lambda p: p['metadata']['name'])
        if not pending:
            return  # No completion claim: caller still observes old Pod presence.
        offset = self._retirement_cursor % len(pending)
        selected = (pending[offset:] + pending[:offset])[:4]
        self._retirement_cursor = (offset + len(selected)) % len(pending)
        failures = []
        for pod in selected:
            now = time.time()
            try:
                try:
                    fresh_sources = all(evidence.get('sources', {}).get(source, {}).get('available') is True
                        and -(5 if source in {'ue', 'upf'} else 1) <= now - seconds(evidence['sources'][source].get('observedAt')) <= 15
                        for source in ('ue', 'amf', 'smf', 'upf'))
                except (TypeError, ValueError):
                    fresh_sources = False
                if not fresh_sources:
                    if not watchdog_ack_only:
                        raise EvidenceUnavailable('Retirement cohort source evidence expired')
                    # Preserve the explicit watchdog's owned NAS shutdown path.
                    # Strip source proof: it may ACK, but cannot pass any DELETE
                    # branch without fresh post-ACK AMF/SMF/UPF observations.
                    self.retire_owned_pod(pod, {'ues': evidence.get('ues', [])})
                else:
                    self.retire_owned_pod(pod, evidence)
            except Exception as exc:
                failures.append(exc)
        if failures:
            # Other independent Pods were still advanced; never hide a failed
            # member or report this cohort as released based on partial success.
            raise failures[0]

    def delete_owned_pod(self, pod: dict):
        metadata = pod.get('metadata', {})
        name, uid = metadata.get('name', ''), metadata.get('uid')
        if (not uid or not re.fullmatch(r'cv-ue-g[0-9]+-[0-9]+', name)
                or metadata.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'):
            raise EvidenceUnavailable('Cannot delete a Pod without exact managed identity')
        # Normal kubectl delete does not accept -o json or enforce UID
        # preconditions. Raw DELETE retains kubeconfig transport and atomically
        # rejects a replacement Pod with the same name. Default grace is kept.
        self._write_kubectl(KubeadmMutationKind.POD_DELETE, {'pod':pod}, 'delete', '--raw', f'/api/v1/namespaces/{self.namespace}/pods/{name}', '-f', '-',
                     body={'apiVersion': 'v1', 'kind': 'DeleteOptions',
                           'preconditions': {'uid': uid}, 'propagationPolicy': 'Background'})

    def terminal_process(self, name):
        result = self.read_runner(KubeadmReadKind.PROCESS)(['kubectl', '--context', self.context, '--namespace', self.namespace,
                              'exec', name, '-c', 'ue', '--', 'sh', '-ec', PROCESS_READ],
                             text=True, capture_output=True, timeout=8, check=False)
        fields = result.stdout.strip().splitlines()
        if (result.returncode or len(fields) != 4 or fields[0] != '/ueransim/nr-ue'
                or not fields[1].isdigit() or int(fields[1]) <= 0
                or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', fields[2])
                or not re.fullmatch(r'[0-9a-fA-F]{16}', fields[3])
                or not int(fields[3], 16) & (1 << 14)):
            raise EvidenceUnavailable('Terminal shutdown process identity or TERM handler unavailable')
        return {'pid': 1, 'exe': fields[0], 'startTicks': int(fields[1]), 'bootId': fields[2], 'sigCgt': fields[3]}

    @staticmethod
    def _terminal_native_absent(evidence, supis, *, after=0):
        now = time.time()
        for source in ('amf', 'smf', 'upf'):
            value = evidence.get('sources', {}).get(source, {})
            at = seconds(value.get('observedAt'))
            if value.get('available') is not True or not after < at <= now + 1 or not -1 <= now - at <= 15:
                raise EvidenceUnavailable('Terminal shutdown requires fresh post-event native absence')
        if not isinstance(evidence.get('ues'), list):
            raise EvidenceUnavailable('Terminal shutdown native inventory missing')
        for row in evidence['ues']:
            if str(row.get('supi', '')).startswith('residual-upf-') or (row.get('supi') in supis and
                    (row.get('amf', {}).get('registered') is True or row.get('smf', {}).get('sessionCount', 0) > 0
                     or row.get('upf', {}).get('pfcpSeid'))):
                raise EvidenceUnavailable('Terminal shutdown native resource remains')

    def _terminal_denied_states(self, name, supis):
        # Eligibility for guarded PROCESS termination, not NAS quiescence.
        # LIMITED-SERVICE/5U1 is a separate failed-round case, never a 5U3
        # rejection or a successful deregistration ACK. All sibling/native,
        # authority, process and post-termination guards remain mandatory.
        states = self.ue_states(name, supis)
        if set(states) != set(supis):
            raise EvidenceUnavailable('Terminal shutdown exact NAS population missing')
        denied = []
        for supi, state in states.items():
            if state.get('has-emergency') is not False:
                raise EvidenceUnavailable('Terminal shutdown emergency state not safely absent')
            if self.ue_quiescent(state):
                continue
            rejected = (state.get('5u-state') == '5U3-ROAMING-NOT-ALLOWED'
                        and state.get('mm-state') == 'MM-DEREGISTERED/NO-CELL-AVAILABLE')
            limited = (state.get('5u-state') == '5U1-UPDATED'
                       and state.get('mm-state') == 'MM-DEREGISTERED/LIMITED-SERVICE'
                       and state.get('sim-inserted') is True)
            attempting = (state.get('5u-state') == '5U2-NOT-UPDATED'
                          and state.get('mm-state') == 'MM-DEREGISTERED/ATTEMPTING-REGISTRATION'
                          and state.get('sim-inserted') is True)
            if (state.get('rm-state') != 'RM-DEREGISTERED' or state.get('cm-state') != 'CM-IDLE'
                    or not (rejected or limited or attempting)):
                raise EvidenceUnavailable('Terminal shutdown requires normal sibling deregistration first')
            denied.append(supi)
        if not denied:
            raise EvidenceUnavailable('Terminal shutdown denied UE proof missing')
        return states, denied

    def _terminal_signal(self, name, process, *, target=None):
        expected_lines = [process['exe'], str(process['startTicks']), process['bootId'], process['sigCgt']]
        script = 'set -eu\nactual="$(' + PROCESS_READ + ')"\n[ "$actual" = ' + shlex.quote('\n'.join(expected_lines)) + ' ]\nkill -TERM 1'
        try:
            self._write_runner(KubeadmMutationKind.TERMINAL_SIGNAL, target or {'pod_name':name,'process':process}, ['kubectl', '--context', self.context, '--namespace', self.namespace, 'exec', name,
                         '-c', 'ue', '--', 'sh', '-ec', script], text=True, capture_output=True, timeout=8, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass  # Durable claim is uncertain, never resend.
        return 'signal_outcome_unverified'

    def _recover_terminal_reservation(self, name, supis, evidence, intent, stored, container, fence):
        # A running process is NOT evidence of no prior dispatch. Only the
        # independently reviewed, startup-pinned pre-dispatch receipt permits
        # this one narrow migration. Retain original intent and append a claim.
        fresh, checked, auth = fence()
        receipt = validate_terminal_recovery_receipt(auth.get('preDispatchRecovery'), now=time.time())
        if (set(intent) != {'version', 'roundId', 'generation', 'podUid', 'supis', 'deniedSupis', 'containerID', 'process', 'reservedAt'}
                or intent.get('version') != 1 or checked != container
                or not container.get('state', {}).get('running')
                or any(receipt.get(k) != intent.get(k) for k in ('roundId', 'generation', 'podUid', 'containerID', 'process'))
                or receipt['approvedAt'] < intent['reservedAt']
                or receipt['intentDigest'] != terminal_intent_digest(intent)
                or fresh['metadata']['annotations'].get(TERMINAL_SHUTDOWN) != stored):
            raise EvidenceUnavailable('Terminal recovery legacy intent does not match reviewed receipt')
        states, denied = self._terminal_denied_states(name, supis)
        if denied != intent['deniedSupis']:
            raise EvidenceUnavailable('Terminal recovery denied population changed')
        self._terminal_native_absent(evidence, supis)
        if self.terminal_process(name) != intent['process']:
            raise EvidenceUnavailable('Terminal recovery exact process changed')
        fresh, checked, current_auth = fence()
        if checked != container or current_auth.get('preDispatchRecovery') != receipt:
            raise EvidenceUnavailable('Terminal recovery authority changed')
        validate_terminal_recovery_receipt(receipt, now=time.time())
        self._terminal_native_absent(evidence, supis)
        updated = {**intent, 'recoveryDispatchClaim': {'approvalId': receipt['approvalId'],
                   'receiptDigest': terminal_intent_digest(receipt), 'originalIntentDigest': receipt['intentDigest'],
                   'claimedAt': time.time(), 'audit': receipt['audit']}}
        self._write_kubectl(KubeadmMutationKind.TERMINAL_PATCH, {'pod':fresh,'intent':updated}, 'patch', 'pod', name, '--type=json', '-o', 'json', '-p', json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': intent['podUid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': fresh['metadata']['resourceVersion']},
            {'op': 'test', 'path': '/metadata/annotations/cityverse.io~1terminal-shutdown', 'value': stored},
            {'op': 'replace', 'path': '/metadata/annotations/cityverse.io~1terminal-shutdown', 'value': json.dumps(updated, sort_keys=True)}]))
        final, checked, current_auth = fence()
        if (checked != container or current_auth.get('preDispatchRecovery') != receipt
                or json.loads(final['metadata']['annotations'].get(TERMINAL_SHUTDOWN, '{}')) != updated
                or self.ue_states(name, supis) != states):
            raise EvidenceUnavailable('Terminal recovery post-claim fence changed')
        final, checked, current_auth = fence()
        if (checked != container or current_auth.get('preDispatchRecovery') != receipt
                or json.loads(final['metadata']['annotations'].get(TERMINAL_SHUTDOWN, '{}')) != updated):
            raise EvidenceUnavailable('Terminal recovery fence changed during NAS observation')
        validate_terminal_recovery_receipt(receipt, now=time.time())
        self._terminal_native_absent(evidence, supis)
        return self._terminal_signal(name, intent['process'], target={'pod':final,'intent':updated})

    def retire_terminal_owned_pod(self, pod, evidence, authorization):
        """Retirement recovery requires explicit trusted failed-round authority.

        authorization is a trusted live durable-ledger reader, not HTTP data.
        Signal reservation persists on the exact Pod BEFORE exec. An interrupted
        reservation is unknown and cannot be replayed, even after backend restart.
        """
        expected = pod.get('metadata', {})
        name, uid = expected.get('name'), expected.get('uid')
        allocation = expected.get('annotations', {}).get('cityverse.io/supis')
        generation = expected.get('labels', {}).get('cityverse.io/generation')
        try:
            supis = json.loads(allocation)
        except (TypeError, ValueError):
            raise EvidenceUnavailable('Terminal shutdown allocation invalid') from None
        if (not isinstance(supis, list) or not 1 <= len(supis) <= self.batch_size
                or len(set(supis)) != len(supis) or any(not isinstance(s, str) or not re.fullmatch(r'imsi-[0-9]{15}', s) for s in supis)
                or not isinstance(generation, str) or not re.fullmatch(r'[1-9][0-9]*', generation)
                or not isinstance(name, str) or not re.fullmatch(r'cv-ue-g' + generation + r'-[0-9]+', name)
                or not isinstance(uid, str) or not uid
                or not callable(authorization)):
            raise EvidenceUnavailable('Terminal shutdown ownership invalid')

        authority_identity = None
        def fence():
            nonlocal authority_identity
            auth = authorization()
            if (not isinstance(auth, dict) or auth.get('executionOutcome') not in {'failed', 'cancelled'}
                    or auth.get('stage') != 'cleaning' or not isinstance(auth.get('roundId'), str) or not auth['roundId']
                    or auth.get('generation') != int(generation) or auth.get('podUid') != uid
                    or auth.get('supis') != supis):
                raise EvidenceUnavailable('Terminal shutdown failed-round authority changed')
            identity = {k: auth[k] for k in ('roundId', 'generation', 'podUid', 'supis')}
            if authority_identity is not None and identity != authority_identity:
                raise EvidenceUnavailable('Terminal shutdown durable round identity changed')
            authority_identity = identity
            fresh = self._read_kubectl('get', 'pod', name, '-o', 'json')
            meta = fresh.get('metadata', {})
            specs = [c for c in fresh.get('spec', {}).get('containers', []) if c.get('name') == 'ue']
            statuses = [c for c in fresh.get('status', {}).get('containerStatuses', []) if c.get('name') == 'ue']
            if (meta.get('uid') != uid or meta.get('deletionTimestamp')
                    or meta.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'
                    or meta.get('labels', {}).get('cityverse.io/generation') != generation
                    or meta.get('annotations', {}).get('cityverse.io/supis') != allocation
                    or fresh.get('spec', {}).get('restartPolicy') != 'Never'
                    or fresh.get('spec', {}).get('hostPID') is True or fresh.get('spec', {}).get('shareProcessNamespace') is True
                    or len(specs) != 1 or specs[0].get('image') != self.image
                    or specs[0].get('command') != ['/ueransim/nr-ue', '-c', '/run/ue/config.yaml', '-t', str(self.tempo_ms), '-n', str(len(supis))]
                    or len(statuses) != 1 or type(statuses[0].get('restartCount')) is not int or statuses[0]['restartCount'] != 0
                    or not re.fullmatch(r'containerd://[0-9a-f]{64}', statuses[0].get('containerID', ''))):
                raise EvidenceUnavailable('Terminal shutdown Pod/container fence changed')
            return fresh, statuses[0], auth

        fresh, container, auth = fence()
        stored = fresh['metadata'].get('annotations', {}).get(TERMINAL_SHUTDOWN)
        if stored:
            try:
                intent = json.loads(stored)
            except (ValueError, TypeError):
                raise EvidenceUnavailable('Terminal shutdown durable intent malformed') from None
            if (intent.get('podUid') != uid or intent.get('supis') != supis or intent.get('generation') != int(generation)
                    or intent.get('roundId') != auth['roundId'] or intent.get('containerID') != container['containerID']):
                raise EvidenceUnavailable('Terminal shutdown intent ownership changed')
            terminated = container.get('state', {}).get('terminated', {})
            if not terminated and auth.get('preDispatchRecovery') is not None and 'recoveryDispatchClaim' not in intent:
                return self._recover_terminal_reservation(name, supis, evidence, intent, stored, container, fence)
            stop_bound = intent.get('recoveryDispatchClaim', {}).get('claimedAt', intent['reservedAt'])
            if (terminated.get('containerID') != intent['containerID'] or type(terminated.get('exitCode')) is not int or terminated['exitCode'] != 0
                    or not terminated.get('finishedAt') or seconds(terminated['finishedAt']) < int(stop_bound)):
                raise EvidenceUnavailable('Terminal shutdown outcome unknown; no signal replay')
            if 'terminatedObservedAt' not in intent:
                # Kubelet finishedAt may have only second precision. Persist the
                # actual time this exact terminated container was read, then wait
                # for independently collected native observations AFTER it.
                updated = {**intent, 'terminatedObservedAt': time.time(), 'terminatedState': terminated}
                self._write_kubectl(KubeadmMutationKind.TERMINAL_PATCH, {'pod':fresh,'intent':updated}, 'patch', 'pod', name, '--type=json', '-o', 'json', '-p', json.dumps([
                    {'op': 'test', 'path': '/metadata/uid', 'value': uid},
                    {'op': 'test', 'path': '/metadata/resourceVersion', 'value': fresh['metadata']['resourceVersion']},
                    {'op': 'test', 'path': '/metadata/annotations/cityverse.io~1terminal-shutdown', 'value': stored},
                    {'op': 'replace', 'path': '/metadata/annotations/cityverse.io~1terminal-shutdown', 'value': json.dumps(updated, sort_keys=True)}]))
                return 'termination_observed_waiting_native'
            if intent.get('terminatedState') != terminated:
                raise EvidenceUnavailable('Terminal shutdown terminated state changed')
            self._terminal_native_absent(evidence, supis, after=intent['terminatedObservedAt'])
            final, final_container, _ = fence()
            if final_container != container:
                raise EvidenceUnavailable('Terminal shutdown termination observation changed')
            self._terminal_native_absent(evidence, supis, after=intent['terminatedObservedAt'])
            self.delete_owned_pod(final)
            return 'delete_requested'
        if not container.get('state', {}).get('running'):
            raise EvidenceUnavailable('Terminal shutdown requires exact running process')
        states, denied = self._terminal_denied_states(name, supis)
        self._terminal_native_absent(evidence, supis)
        process = self.terminal_process(name)
        fresh, checked, _ = fence()
        if checked != container:
            raise EvidenceUnavailable('Terminal shutdown container changed during process read')
        self._terminal_native_absent(evidence, supis)
        intent = {'version': 1, 'roundId': auth['roundId'], 'generation': int(generation), 'podUid': uid,
                  'supis': supis, 'deniedSupis': [s for s in denied if states[s].get('5u-state') == '5U3-ROAMING-NOT-ALLOWED'],
                  **({'limitedServiceSupis': [s for s in denied if states[s].get('5u-state') == '5U1-UPDATED']}
                     if any(states[s].get('5u-state') == '5U1-UPDATED' for s in denied) else {}),
                  'containerID': container['containerID'],
                  'process': process, 'reservedAt': time.time()}
        self._write_kubectl(KubeadmMutationKind.TERMINAL_PATCH, {'pod':fresh,'intent':intent}, 'patch', 'pod', name, '--type=json', '-o', 'json', '-p', json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': uid},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': fresh['metadata']['resourceVersion']},
            {'op': 'add', 'path': '/metadata/annotations/cityverse.io~1terminal-shutdown', 'value': json.dumps(intent, sort_keys=True)}]))
        final, checked, _ = fence()
        if checked != container or json.loads(final['metadata']['annotations'].get(TERMINAL_SHUTDOWN, '{}')) != intent:
            raise EvidenceUnavailable('Terminal shutdown reservation changed')
        if self.ue_states(name, supis) != states:
            raise EvidenceUnavailable('Terminal shutdown NAS state changed after reservation')
        final, checked, _ = fence()
        try:
            final_raw = final['metadata']['annotations'].get(TERMINAL_SHUTDOWN, '{}')
            final_intent = json.loads(final_raw)
        except (KeyError, TypeError, ValueError):
            raise EvidenceUnavailable('Terminal shutdown reservation malformed before signal') from None
        if (checked != container or final_intent != intent
                or final_raw != json.dumps(intent, sort_keys=True)):
            raise EvidenceUnavailable('Terminal shutdown container or reservation changed before signal')
        self._terminal_native_absent(evidence, supis)
        # PID 1 cannot be reused within this same living container. Recheck every
        # process field in the very exec that sends TERM; never KILL or killall.
        return self._terminal_signal(name, process, target={'pod':final,'intent':intent})

    def cleanup(self, generation: int, evidence=None):
        namespace = self._read_kubectl("get", "namespace", self.namespace, "-o", "json")
        if namespace.get("metadata", {}).get("labels", {}).get("cityverse.io/platform") != "kubeadm":
            raise EvidenceUnavailable("Namespace is not an approved kubeadm target")
        pods = self._read_kubectl("get", "pods", "-l", f"{LABEL},cityverse.io/generation={generation}", "-o", "json").get("items", [])
        if pods:
            if any(p.get('metadata', {}).get('labels', {}).get('cityverse.io/generation') != str(generation) for p in pods):
                raise EvidenceUnavailable('Cleanup Pod generation differs from exact requested generation')
            self._retire_cohort(pods, evidence or {}, maximum_generation=generation, watchdog_ack_only=True)
        return not pods


def configured_driver():
    if os.environ.get("CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED", "false").lower() != "true":
        return None
    backend = os.environ.get("CITYVERSE_ONPREM_RAN_BACKEND", "free-ran-ue")
    device_profile=None
    if backend == 'ueransim-device':
        from backend.onprem.intents.integration import load_configuration
        from .device_sessions import DeviceDriverProfile
        config=load_configuration()
        if not config.get('ue_device_profile') or not config.get('ue_device_image'):
            raise ValueError('not_configured: pinned UERANSIM device profile/image required')
        device_profile=DeviceDriverProfile(**config['ue_device_profile'])
        driver_type,image=KubeadmDriver,config['ue_device_image']
    elif backend != "free-ran-ue":
        raise ValueError("Unsupported on-prem RAN backend; explicit free-ran-ue or ueransim-device profile required")
    else:
        from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver, UE_IMAGE
        driver_type, image = FreeRanKubeadmDriver, os.environ.get("CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE", UE_IMAGE)
        from backend.onprem.intents.integration import load_configuration
        from .device_sessions import DeviceDriverProfile
        config = load_configuration()
        if config.get('ue_device_profile') or config.get('ue_device_image'):
            if not config.get('ue_device_profile') or not config.get('ue_device_image'):
                raise ValueError('not_configured: pinned FreeRAN device profile/image required')
            device_profile = DeviceDriverProfile(**config['ue_device_profile'])
            if (device_profile.driver != 'free-ran-ue'
                    or config['ue_device_image'].split('@')[-1] != device_profile.image_digest
                    or ('CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE' in os.environ
                        and image != config['ue_device_image'])):
                raise ValueError('FreeRAN device vendor/image configuration differs')
            image = config['ue_device_image']
        elif config.get('requires_pre_traffic_admission') is True:
            raise ValueError('not_configured: formal FreeRAN admission requires a commissioned device profile/image')
    return driver_type(os.environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT", ""),
                         os.environ.get("CITYVERSE_ONPREM_NAMESPACE", "free5gc-onprem"),
                         batch_size=int(os.environ.get("CITYVERSE_ONPREM_UE_BATCH_SIZE", "50")),
                         batch_interval=float(os.environ.get("CITYVERSE_ONPREM_UE_BATCH_INTERVAL", "30")),
                         tempo_ms=int(os.environ.get("CITYVERSE_ONPREM_UE_TEMPO_MS", "500")),
                         typed_launch_burst=int(os.environ.get('CITYVERSE_ONPREM_TYPED_UE_LAUNCH_BURST', '1')),
                         typed_launch_max_pending=int(os.environ.get('CITYVERSE_ONPREM_TYPED_UE_MAX_PENDING', '8')),
                         image=image,
                         init_image=os.environ.get("CITYVERSE_ONPREM_UE_INIT_IMAGE", INIT_IMAGE),
                         traffic_enabled=os.environ.get("CITYVERSE_ONPREM_TRAFFIC_ENABLED", "false").lower() == "true",
                         **({'device_profile':device_profile} if device_profile is not None else {}))
