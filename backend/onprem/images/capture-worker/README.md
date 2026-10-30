# Opt-in capture worker packaging (not deployed)

This packages the fixed authenticated Kubernetes-exec worker, not a policy
controller, HTTP endpoint, or commissioned QoS capability. Default rendering
is unchanged. Only `upf-embb` can receive the sidecar.

## Build and pin

From the repository root:

```sh
docker build -f backend/onprem/images/capture-worker/Dockerfile -t cityverse-policy-capture:reviewed .
```

The base digest is the existing resolved `python:3.11-slim` entry in
`the pinned FROM instruction in Dockerfile`. No new worker digest is asserted before an actual build
and registry/content-store resolution. Set inventory
`enablePolicyCaptureWorker: true` plus `policyCaptureImage: <actual-repository>@sha256:<actual-manifest-digest>`
only after building, inspecting and distributing that exact image. This text
is a placeholder description, not a usable manifest pin. The renderer rejects
an enabled worker without a digest-pinned image. No Secret is generated.

## Security and lifecycle

- Main container: reviewed UID/GID0 exception, read-only root, drop ALL and add NET_RAW only,
  RuntimeDefault seccomp, no privilege escalation, no ports/listeners.
- A narrow root init drops ALL capabilities (no CHOWN needed). It copies the fixed 32-byte
  `cityverse-policy-capture-key` Secret key `key` and Downward API Pod UID into
  fixed private-volume files, mode0400 owned by0; creates mode0700 claims owned by0.
  This avoids projected-volume symlinks rejected by worker path validation.
  It has no NET_RAW, no HTTP, no arbitrary paths, and no secret stdout.
- Main mounts only claims read/write; key/UID use read-only subPath mounts.
  Init refuses any nonempty state, never overwriting an existing claim.
- Bounded8Mi emptyDir survives **same Pod container restart only**. Pod loss
  destroys local claims; the external durable journal must retain quarantine
  and must NOT automatically re-arm on a replacement Pod or rotated key.
- The key is commissioned explicitly with `kvm_lab.py capture-key`. That action
  creates one immutable 32-byte CSPRNG key through stdin, verifies API readback,
  and reports only UID plus SHA-256. It retains a valid existing Secret and
  refuses malformed or mutable state; ordinary configure/render never creates,
  replaces, rotates, logs, or guesses this credential.
- Fixed idle PID1 handles TERM; a separate authenticated exec launches capture.
  Readiness opens/closes the claim-directory descriptor and validates files;
  current ClaimStore constructor creates no file and takes no flock. This is
  not capture capability/effect proof. Worker/status operations do take bounded
  directory locks; status never creates a claim. No liveness probe
  intentionally restarts a worker during a capture. No Docker HEALTHCHECK or
  EXPOSE: it is an idle exec target with Kubernetes readiness, not a service.
- Immutable image contains only three stdlib Python modules; no dependencies,
  source ConfigMap override, package installation, host mount or NET_ADMIN.

## Validation status

Packaging tests: 9 passed, including disabled-vs-absent object/serialized equality.
Actual Docker build passed: image configuration ID
`sha256:7f590f4aebf6d8cf79a57637ae44e14c31f7fb923b9b756b4f1ce9227cae962f`.
This is NOT a registry manifest digest; RepoDigests is empty, so a deployable
digest pin and containerd import verification remain pending.
Isolated network-none Docker smoke passed: actual exec UID0, CapEff/Prm/Bnd
`0x2000` (NET_RAW only), NoNewPrivs1, raw socket open/close, fixed init/private
files/readiness and claims retained across same-container restart. The exact
test container was removed. This is not same-Pod Kubernetes/CRI validation.
Skill validator scripts failed because installed scripts have CRLF; available
fallback is actual build plus structural tests. Dedicated lint/security scan,
YAML syntax/schema/server dry-run remain pending; no false scanner pass claimed.
No apply, UPF restart, PCF action, packet capture or native policy gate performed.
Root main/init exceptions were approved for this fixed exec target: an actual
isolated Docker non-root10001 + NET_RAW + no-new-privileges run failed AF_PACKET
with EPERM. This does not independently prove the deployed CRI behaves identically.
Root with only NET_RAW avoids adding SETUID/SETGID/SETPCAP or a privileged
launcher. It still enlarges UID authority within the container; pinned image,
read-only root, private mounts, no service/host mounts and parent identity fences
are mandatory compensating controls. Review the exception before deployment.
Image size/build performance are not yet measured; no optimization percentage
is claimed. Parent PID/container/image/native-epoch fences remain mandatory.
