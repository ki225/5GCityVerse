# On-prem infrastructure as code

## Shutdown and remove the project network

First stop the owned backend/frontend and resource controllers using the
[application shutdown guide](../../scripts/onprem/README.md#stop-applications-controllers-and-owned-helpers).
The following removes the dedicated `free5gc-onprem` deployment and, when
present and explicitly identified, the project's old isolated device canary.
It never resets kubeadm, deletes kube-system, prunes Docker or shuts down Ubuntu.
Run from this repository inside Ubuntu. Reading this deployment's admin
kubeconfig and root-owned state requires `sudo`.

```bash
KUBECONFIG_FILE=/etc/kubernetes/admin.conf
KUBE_CONTEXT="$(sudo kubectl --kubeconfig "$KUBECONFIG_FILE" config current-context)"
MAIN_UID=$(sudo kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$KUBE_CONTEXT" \
  get namespace free5gc-onprem --ignore-not-found -o jsonpath='{.metadata.uid}')
CANARY_UID=$(sudo kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$KUBE_CONTEXT" \
  get namespace cityverse-device-canary --ignore-not-found -o jsonpath='{.metadata.uid}')
CANARY_ARGS=()
if [[ -n "$CANARY_UID" ]]; then CANARY_ARGS=(--canary-uid "$CANARY_UID"); fi
sudo bash infrastructure/onprem/scripts/teardown.sh \
  --kubeconfig "$KUBECONFIG_FILE" --context "$KUBE_CONTEXT" \
  --namespace free5gc-onprem --namespace-uid "${MAIN_UID:-already-absent}" \
  "${CANARY_ARGS[@]}" --delete-retained-pv-definitions --apply
sudo bash infrastructure/onprem/scripts/remove-host-network.sh \
  --kubeconfig "$KUBECONFIG_FILE" --context "$KUBE_CONTEXT" \
  --disable-autostart --apply
```

Without `--apply`, both scripts only check scope. `teardown.sh` validates the
exact namespace UID and `cityverse.io/platform=kubeadm`; the optional canary
also requires `cityverse.io/managed-by=isolated-device-ran`. It uninstalls only
Helm releases in that namespace and normally waits for both namespaces to
disappear. This deployment had no Helm releases; its manually applied resources
were removed by namespace deletion. An already absent namespace is a safe
no-op. You can rerun with the UID saved in the before-inventory; the literal
`already-absent` is accepted only when the main namespace actually does not
exist, and cannot authorize deletion of a present namespace.

Namespace removal deletes its PVCs. `--delete-retained-pv-definitions` additionally
removes only project `Retain` local PV API objects after namespace deletion,
checking PV UID, original claim UID, `Released` state, local path and node
affinity. **It retains the actual `/var/lib/cityverse-mongodb` and
`/var/lib/cityverse-af-callback` data directories**, host SQLite DBs, logs, tokens,
source and test evidence. Recreate the PV/PVC definitions from the deployment
manifests when installing again; old claimRef UIDs are not left blocking new
claims. Omit this flag to retain PV definitions, but then explicitly reconcile
their old claim references before reinstalling.

`remove-host-network.sh` requires both project namespaces absent and refuses
any remaining NAD that uses a target parent. It checks exactly ten `cv-n2`,
`cv-n4`, `cv-n3-*`, `cv-n6-*` dummy parents and four `cv-gw-*` ipvlan gateways,
including gateway parent ifindex, before removal. It leaves primary CNI bridges,
nonproject network namespaces, global `ip_forward`, untagged Docker forwarding
rules and unrelated routes intact. Normal Kubernetes CNI deletion removes the
project Pod networks and their N3 `c170:` qdiscs; the script never erases the
entire host-local lease directory. Missing target links are a safe no-op.

On this Ubuntu host, the dedicated
`5gcityverse-secondary-links.service` recreates those links at boot.
`--disable-autostart` verifies its exact systemd FragmentPath, Description,
ExecStart, root ownership and lack of requiring units, then disables/stops only
that unit. This prevents a host restart from recreating the lab links.
Shared kubelet/containerd/networking units are not masked. To intentionally
rebuild this existing host, first verify the same unit ownership:

```bash
sudo systemctl show 5gcityverse-secondary-links.service \
  -p FragmentPath -p Description -p ExecStart -p RequiredBy
# Only for the root-owned /etc/systemd/system/5gcityverse-secondary-links.service
# running /usr/local/sbin/5gcityverse-secondary-links, with no RequiredBy users:
sudo systemctl enable --now 5gcityverse-secondary-links.service
```

Then follow the installation guide to restore the project namespace/PVs/network
workloads. The retained host unit also ensures the pinned control-plane address
on this particular host; it is not part of a generic physical-site install.
If the shared API is offline, verify Kubernetes after the original control
plane is ready; a refused API connection is not evidence that a namespace was
deleted.

Requirements REQ-T1..T4 and REQ-H1..H3: explicit cluster and UID fencing,
dedicated namespace scope, retained data and shared CNI protection. The scripts
use a minimal CLI pattern; `bash -n` and ShellCheck passed after correcting
SC1007 empty assignments.

This directory contains **Helm and Kustomize declarations, values, Bash deployment/teardown scripts, and a small configuration-only Python node-selector normalizer**. `Chart.yaml` depends directly on the official [free5gc-helm](https://github.com/free5gc/free5gc-helm) version `v4.2.2`, chart `1.1.7`; `upstream.lock.yaml` pins the Git revision. API source, Docker build contexts, and runtime controllers remain under `backend/`.

## Required configuration

| Setting | Location in `values.yaml` |
| --- | --- |
| AMF, AUSF, NRF, PCF, UDM, UDR, and WebUI resources and node selectors | `free5gc.free5gc-<nf>.<nf>` |
| MongoDB resources, storage class, and node selector | `free5gc.mongodb` |
| SMF and NSSF resources and sites | `city.core` |
| Per-slice UPF resources, sites, and subnets | `city.slices` |
| Seven gNB identities, coordinates, slices, and resources | `city.gnbs`, `city.gnbResources` |
| Three Distributed, one Central, and four reserved Edge locations | `city.sites` |
| N3 distance-delay model | `city.transportDelay` |

The standard upstream NF and MongoDB templates are reused. In this version, the Multus SMF, NSSF, and UPF templates have a fixed slice topology, so thin Helm templates define the city's four slices, seven gNBs, N6 endpoints, and NADs. `patches/` stores only AMF Recreate and stable NF identity changes. NFs with fixed network addresses use one replica. Adding UPF replicas still requires new PFCP addresses, SMF topology, and session draining. Coordinates and capacity ratios do not create hardware; Edge remains a reserved placement target. Enabling `city.transportDelay` produces a delay plan; **rendering or installing Helm does not run tc**.

## Environment and rendering

The host environment is Ubuntu with existing kubeadm, primary CNI, Multus, ipvlan/static CNI binaries, compatible gtp5g and SCTP modules, and an available `cityverse-local` StorageClass. UPF hosts need `cityverse.io/gtp5g-ready=true`. Physical deployments must supply corresponding site labels and N2, N3, N4, and N6 networks. MongoDB retains the upstream tini bootstrap; first deployment needs network access for Debian packages.

1. Render the default Ubuntu single-node configuration:

```bash
bash infrastructure/onprem/scripts/render.sh
```

The script reads `value.yaml`, resolves the local `hostname -s` without
querying Kubernetes, and downloads the exact `upstream.lock.yaml` revision into
`${XDG_CACHE_HOME:-$HOME/.cache}/5gcityverse/free5gc-helm/<revision>` on its first
run. Later runs reuse this cache; dirty or mismatched checkouts are rejected,
never reset. Output is printed and saved under a new `.tmp/onprem-render/` run
directory each time; previous output is retained.

2. Optional: override a target hostname or use your own values:

```bash
bash infrastructure/onprem/scripts/render.sh --hostname my-kubernetes-node
bash infrastructure/onprem/scripts/render.sh --upstream /path/to/clean/free5gc-helm \
  --values /path/to/site-values.yaml --output /path/to/new-render-directory
```

`--values` replaces the Ubuntu overlay: precedence is chart `values.yaml`, then
each supplied file in CLI order (last wins). Thus explicit values retain the
existing physical-site deployment workflow. `--hostname` is only for the default
Ubuntu overlay and cannot combine with `--values`. The local hostname must match the
Kubernetes node's `kubernetes.io/hostname` label; use `--hostname` if it differs.
All logical sites are colocated on that one node; no extra physical capacity is
created. Only a single-node simulation may run `sudo bash
infrastructure/onprem/scripts/host-network.sh --apply`; this does not replace
physical VLANs.

The script checks the revision and clean working tree, strict lint, identical
results from two renders, and Kustomize. It outputs `runtime.yaml`, `Chart.lock`,
a chart package, `render-settings.yaml`, and the selected `values-01.yaml`
(additional files numbered in CLI order). Rendering neither connects to the
cluster. It requires Git, Helm, kubectl (local Kustomize only), Python 3 with
PyYAML, and standard Ubuntu shell utilities; rendering alone does not install the
required CNI, kernel modules, storage class, host links or images.

## Installation

Before Helm, provision the native API credentials, UPF/RAN evidence source and retained
host directories using the backend virtualenv described in the startup guide:

```bash
sudo "$HOME/.venvs/5gcityverse-onprem/bin/python" -m backend.onprem.operations.deployment.native_runtime --apply
```

The command defaults to `/etc/kubernetes/admin.conf`, its current context, the
local hostname, and private state `/var/lib/cityverse/operator-native`. It does
not create UEs or traffic. Existing private tokens and MongoDB data are retained;
Helm owns the MongoDB PV definition. For another cluster, pass `--kubeconfig`,
`--context` and `--node` explicitly. Keep its generated private
`bootstrap-values.json` as the last Helm values file so the source revision
triggers an evidence-sidecar rollout after source updates.

On this WSL host, `fs.inotify.max_user_instances=128` exhausted the root user's
watch instances and made the existing kube-proxy fail with `too many open files`.
The commissioned prerequisite is `1024`, retained in
`/etc/sysctl.d/90-5gcityverse-inotify.conf`. Diagnose that exact failure before
changing a different host's limits; a successful Helm install does not prove
the Kubernetes Service IP or Multus networking is working.

Review the diff, then deploy with explicit kubeconfig, context, and namespace:

```bash
sudo kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" -n free5gc-onprem \
  apply --dry-run=server -f /path/to/render/runtime.yaml
sudo helm upgrade --install cityverse /path/to/render/cityverse-onprem-0.2.0.tgz \
  --kubeconfig "$KUBECONFIG" --kube-context "$CONTEXT" -n free5gc-onprem \
  --create-namespace -f /path/to/render/values-01.yaml \
  -f /var/lib/cityverse/operator-native/bootstrap-values.json \
  --post-renderer infrastructure/onprem/scripts/post-render.sh
sudo kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" label namespace free5gc-onprem cityverse.io/platform=kubeadm --overwrite
```

For multiple `--values` files, pass every generated `values-NN.yaml` to Helm in
the same order. Use the release and namespace from `render-settings.yaml` if
you overrode them. The chart package retains the physical base defaults, so
the generated overlay must also be passed at installation.

Before adopting manually deployed NFs or custom images, review Helm ownership, Pod specs, PVCs, and image differences. This directory cleanup did not redeploy or overwrite live NFs. The backend manages its observer, callback, capture, subscriber provisioning, and UE lifecycle; they are not embedded in this chart.

## Backend boundary

- PFCP: `backend/onprem/operations/diagnostics/pfcp_preflight.py`
- Callback and capture helpers: `backend/onprem/operations/deployment/`
- Live UE verification: `backend/onprem/operations/validation/city_smoke.py` with `--context`, `--kubeconfig`, `--values`, `--site-values`, `--scenario`, and `--supi`.
- NF image builds and patches: `backend/onprem/images/`

Legacy KVM, offline recovery and stage inventory, Python renderer, four-gNB cutover tools, and their dedicated tests were removed. Active backend and test references no longer point to `infrastructure/onprem/kubeadm`.

## Script verification and requirements

The scripts use a small handwritten CLI following the Bash skill's `docs/script-patterns.md → Argument Parsing Patterns` guidance.

| Requirement | Implementation |
| --- | --- |
| REQ-001 Pin the upstream source | `render.sh` checks revision and dirty tree |
| REQ-002 Pure IaC | Helm templates and declarative patches in `post-render.sh` |
| REQ-003 Explicit local network action | `host-network.sh` requires root and `--apply` |
| REQ-D1 N3 uplink and downlink distance delay | `transport-delay.yaml` computes each gNB/UPF budget; `n3-delay.sh` installs peer filters |
| REQ-D2 Preserve other shaping | Reject host netns and foreign qdiscs; remove only reserved handle `c170:` |
| REQ-D3 No API logic in infrastructure | Bash consumes only PID and endpoint JSON; Kubernetes operations and verification belong to the backend |

Verification used `bash -n`, system ShellCheck, and an actual Helm render. Helm contract tests live at `backend/onprem/validation/tests/test_infra_chart.py`.

## N3 distance delay

One-way delay is `ceil((abs(dx)+abs(dy)) × routeStretch × fibreUsPerKm + processingUs[tier])` microseconds. The current model uses grid-path length, route stretch 1.3, and fiber propagation 5 µs/km. Additional transport processing budgets are Edge 250 µs, Distributed 1000 µs, and Central 4000 µs. These are adjustable simulation assumptions, not 3GPP requirements or a claim that 100 km always equals a particular number of milliseconds. Physical host latency is not added separately.

`templates/transport-delay.yaml` produces `n3-delay.json` in the `cityverse-transport-delay` ConfigMap. Each endpoint maps to an existing gNB or UPF `n3` interface. `peers` includes the peer IP, site, tier, path distance in km, and one-way `delayUs`. gNB N3 egress delays uplink traffic; UPF N3 egress delays downlink traffic. Different gNBs sharing a UPF get separate bands. Only IPv4 UDP addressed to a named N3 peer enters netem, including every IP fragment of GTP-U; matching only the UDP port would miss later fragments. Other traffic uses an undelayed band. This change injects no delay into N2, N4, SBI, or N6.

Hosts need `tc`, `jq`, `nsenter`, and `sch_netem`, `sch_prio`, and `cls_u32`. If Ubuntu lacks a module, build it against headers or source and `Module.symvers` for the running kernel, install it under `/lib/modules/$(uname -r)/extra/`, then run `depmod -a` and `modprobe`; do not replace the kernel.

```bash
# The backend/operator first verifies Pod UID, sandbox PID, and node bootID for this endpoint.
jq '.endpoints["gnb-concert"]' n3-delay.json > gnb-concert-delay.json
sudo bash infrastructure/onprem/scripts/n3-delay.sh apply "$SANDBOX_PID" gnb-concert-delay.json
sudo bash infrastructure/onprem/scripts/n3-delay.sh show "$SANDBOX_PID" gnb-concert-delay.json
sudo bash infrastructure/onprem/scripts/n3-delay.sh remove "$SANDBOX_PID" gnb-concert-delay.json
```

Apply both gNB and UPF endpoints for the full bidirectional distance effect. `c170:` is this module's reserved root handle; taking over existing shaping is unsupported. Resolve the PID and apply again after a Pod changes namespace, a rollout, or a host restart. There is no resident reconciliation controller, and NF or PDU sessions are not migrated automatically. Disabling `enabled` or deleting the ConfigMap does not clear qdiscs from surviving Pods. Run `remove` on both endpoints with the retained endpoint JSON before disabling the configuration. See [backend transport validation](../../backend/onprem/operations/validation/TRANSPORT.md) for same-host acceptance.

References: [tc-netem](https://man7.org/linux/man-pages/man8/tc-netem.8.html), [3GPP TS 23.501](https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=3144).
