"""Opt-in eMBB capture packaging only; never deploys or enables policy routes."""
import re

# Fixed paths only. On init restart, refuse an existing initialized volume;
# never erase/rewrite a claim/key to make an old action appear new.
INIT = """import os,pathlib
p=pathlib.Path('/state')
if list(p.iterdir()):raise RuntimeError('capture volume already initialized; operator review required')
key=pathlib.Path('/input-key/key').read_bytes()
uid=pathlib.Path('/input-uid/pod-uid').read_bytes()
if len(key)!=32 or not 1<=len(uid.strip())<=128:raise RuntimeError('capture input invalid')
claims=p/'claims';claims.mkdir(mode=0o700)
for name,data in [('key',key),('pod-uid',uid)]:
 fd=os.open(p/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
 try:
  if os.write(fd,data)!=len(data):raise RuntimeError('capture input write incomplete')
  os.fsync(fd)
 finally:os.close(fd)
fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
"""


def add_capture_sidecar(deployment, image):
    if deployment['metadata']['name'] != 'upf-embb':
        raise ValueError('Capture supports only the fixed eMBB profile')
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image):
        raise ValueError('Actual built and resolved capture image digest required')
    pod = deployment['spec']['template']['spec']
    names = {c['name'] for c in pod['containers']}
    if 'policy-capture' in names or 'upf-evidence' not in names:
        raise ValueError('Unique capture and existing native evidence sidecar required')
    pod.setdefault('volumes', []).extend([
        {'name': 'capture-private', 'emptyDir': {'sizeLimit': '8Mi'}},
        {'name': 'capture-key-input', 'secret': {'secretName': 'cityverse-policy-capture-key',
            'defaultMode': 256, 'items': [{'key': 'key', 'path': 'key'}]}},
        {'name': 'capture-uid-input', 'downwardAPI': {'defaultMode': 256,
            'items': [{'path': 'pod-uid', 'fieldRef': {'fieldPath': 'metadata.uid'}}]}}])
    resources = {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '100m', 'memory': '32Mi'}}
    pod.setdefault('initContainers', []).append({'name': 'capture-private-init', 'image': image,
        'imagePullPolicy': 'IfNotPresent', 'command': ['python', '-c', INIT],
        'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'runAsNonRoot': False,
            'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
            'seccompProfile': {'type': 'RuntimeDefault'}, 'capabilities': {'drop': ['ALL']}},
        'resources': resources, 'volumeMounts': [
            {'name': 'capture-private', 'mountPath': '/state'},
            {'name': 'capture-key-input', 'mountPath': '/input-key', 'readOnly': True},
            {'name': 'capture-uid-input', 'mountPath': '/input-uid', 'readOnly': True}]})
    pod['containers'].append({'name': 'policy-capture', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'workingDir': '/opt/cityverse',
        'env': [{'name': 'CITYVERSE_ONPREM_CAPTURE_WORKER_ENABLED', 'value': 'true'}],
        'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'runAsNonRoot': False,
            'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
            'seccompProfile': {'type': 'RuntimeDefault'}, 'capabilities': {'drop': ['ALL'], 'add': ['NET_RAW']}},
        'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}},
        'volumeMounts': [
            {'name': 'capture-private', 'subPath': 'claims', 'mountPath': '/run/cityverse-capture'},
            {'name': 'capture-private', 'subPath': 'key', 'mountPath': '/run/cityverse-capture-key/key', 'readOnly': True},
            {'name': 'capture-private', 'subPath': 'pod-uid', 'mountPath': '/etc/cityverse/pod-uid', 'readOnly': True}],
        'readinessProbe': {'exec': {'command': ['python', '-c',
            "from archive.legacy.evidence.capture_worker import ClaimStore,private_file,KEY_FILE,POD_UID_FILE;s=ClaimStore();s.close();assert len(private_file(KEY_FILE,64))==32;assert private_file(POD_UID_FILE,128).strip()"]},
            'periodSeconds': 10, 'timeoutSeconds': 2}})
    return deployment
