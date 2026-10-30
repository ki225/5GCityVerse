# On-prem infrastructure as code

This directory contains only **Helm and Kustomize declarations, values, and four Bash scripts**. `Chart.yaml` depends directly on the official [free5gc-helm](https://github.com/free5gc/free5gc-helm) version `v4.2.2`, chart `1.1.7`; `upstream.lock.yaml` pins the Git revision. It contains no Python renderer, API source ConfigMap, Docker build context, tests, or KVM recovery flow.

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

For a single-node deployment, fill every hostname placeholder in `values-single-node.example.yaml`. Only a single-node simulation may run `sudo bash scripts/host-network.sh --apply`; this is no substitute for physical VLANs.

```bash
git clone --depth 1 --branch v4.2.2 https://github.com/free5gc/free5gc-helm.git /tmp/free5gc-helm
bash infrastructure/onprem/scripts/render.sh \
  --upstream /tmp/free5gc-helm --values /path/to/site-values.yaml \
  --output /path/to/new-render-directory
```

Omit `--values` for physical-site mode. The script checks the revision and clean working tree, strict lint, identical results from two renders, and Kustomize. It outputs `runtime.yaml`, `Chart.lock`, and a chart package. Rendering neither connects to the cluster nor runs Python.

## Installation

Review the diff, then deploy with explicit kubeconfig, context, and namespace:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" -n free5gc-onprem \
  apply --dry-run=server -f /path/to/render/runtime.yaml
helm upgrade --install cityverse /path/to/render/cityverse-onprem-0.2.0.tgz \
  --kubeconfig "$KUBECONFIG" --kube-context "$CONTEXT" -n free5gc-onprem \
  --create-namespace -f /path/to/site-values.yaml \
  --post-renderer infrastructure/onprem/scripts/post-render.sh
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" label namespace free5gc-onprem cityverse.io/platform=kubeadm --overwrite
```

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
