#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRIVY="${TRIVY:-${ROOT_DIR}/.tools/trivy-0.72.0/trivy.exe}"
KUBE_LINTER="${KUBE_LINTER:-${ROOT_DIR}/.tools/kube-linter-0.8.3/kube-linter.exe}"
KUBECONFORM="${KUBECONFORM:-${ROOT_DIR}/.tools/kubeconform-0.8.0/kubeconform.exe}"
FREE5GC_HELM_DIR="${FREE5GC_HELM_DIR:-/tmp/free5gc-helm}"

K8S_ROOT="${ROOT_DIR}/k8s"
INFRA_ROOT="${ROOT_DIR}/infrastructure"
IGNORE_FILE="${ROOT_DIR}/.trivyignore.yaml"
if [[ "$TRIVY" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
  K8S_ROOT="$(wslpath -w "${ROOT_DIR}/k8s")"
  INFRA_ROOT="$(wslpath -w "${ROOT_DIR}/infrastructure")"
  IGNORE_FILE="$(wslpath -w "${ROOT_DIR}/.trivyignore.yaml")"
fi

"$TRIVY" config --exit-code 1 --severity HIGH,CRITICAL --ignorefile "$IGNORE_FILE" "$K8S_ROOT"
"$TRIVY" config --exit-code 1 --severity HIGH,CRITICAL --ignorefile "$IGNORE_FILE" \
  --skip-dirs "**/.terraform/**" \
  --skip-dirs "**/build/**" \
  --skip-dirs "**/builds/**" \
  --skip-files "**/*.tfplan" \
  --skip-files "**/tfplan*" \
  --skip-files "**/*.tfstate*" \
  "$INFRA_ROOT"

# These exact paths are scanned separately below. The ordinary pass retains
# every default check for every other Kubernetes manifest.
"$KUBE_LINTER" lint "$K8S_ROOT" \
  --ignore-paths "${K8S_ROOT}\\multus-daemonset.yaml" \
  --ignore-paths "${K8S_ROOT}\\gtp5g-installer.yaml" \
  --ignore-paths "${K8S_ROOT}\\gtp5g-metrics-exporter.yaml" \
  --ignore-paths "${K8S_ROOT}\\pfcp-evidence-collector.yaml" \
  --ignore-paths "${K8S_ROOT}\\helm-overlays\\free5gc-nef-internal-nlb.yaml" \
  --ignore-paths "${K8S_ROOT}\\helm-overlays\\free5gc-webui-internal-nlb.yaml"

# These Services select pods rendered by the pinned external free5GC chart, so
# a raw-file-only lint cannot see their workloads. Exclude only dangling-service
# here; selector consistency and rendered schema are checked below.
"$KUBE_LINTER" lint \
  "${K8S_ROOT}\\helm-overlays\\free5gc-nef-internal-nlb.yaml" \
  "${K8S_ROOT}\\helm-overlays\\free5gc-webui-internal-nlb.yaml" \
  --exclude dangling-service

"$KUBE_LINTER" lint "${K8S_ROOT}\\multus-daemonset.yaml" \
  --exclude host-network \
  --exclude host-pid \
  --exclude no-read-only-root-fs \
  --exclude privilege-escalation-container \
  --exclude privileged-container \
  --exclude run-as-non-root \
  --exclude sensitive-host-mounts

"$KUBE_LINTER" lint "${K8S_ROOT}\\gtp5g-installer.yaml" \
  --exclude host-pid \
  --exclude no-read-only-root-fs \
  --exclude privilege-escalation-container \
  --exclude privileged-container \
  --exclude run-as-non-root \
  --exclude sensitive-host-mounts

# These two node-observability workloads are scanned separately so only the
# host permissions documented in k8s/SECURITY-EXCEPTIONS.md are excluded.
"$KUBE_LINTER" lint "${K8S_ROOT}\\gtp5g-metrics-exporter.yaml" \
  --exclude host-pid \
  --exclude run-as-non-root

"$KUBE_LINTER" lint "${K8S_ROOT}\\pfcp-evidence-collector.yaml" \
  --exclude host-pid \
  --exclude privilege-escalation-container \
  --exclude run-as-non-root

# Schema validation is a separate gate from policy linting.  Only files that
# actually declare Kubernetes resources are passed, so Helm values files are
# never mistaken for manifests.  Missing CRD schemas are allowed, but all
# built-in Kubernetes 1.36 resources are checked strictly.
manifest_count=0
while IFS= read -r -d '' manifest; do
  grep -q '^apiVersion:' "$manifest" || continue
  manifest_target="$manifest"
  if [[ "$KUBECONFORM" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
    manifest_target="$(wslpath -w "$manifest")"
  fi
  "$KUBECONFORM" -strict -ignore-missing-schemas -kubernetes-version 1.36.0 "$manifest_target"
  manifest_count=$((manifest_count + 1))
done < <(find "${ROOT_DIR}/k8s" -type f \( -name '*.yaml' -o -name '*.yml' \) -print0)
if [[ "$manifest_count" -eq 0 ]]; then
  echo "kubeconform gate found no Kubernetes manifests." >&2
  exit 1
fi

if [[ ! -f "${FREE5GC_HELM_DIR}/charts/free5gc/charts/free5gc-upf/Chart.yaml" ]]; then
  echo "Pinned free5GC chart is missing at ${FREE5GC_HELM_DIR}; run sync_free5gc_chart before this gate." >&2
  exit 1
fi
command -v helm >/dev/null 2>&1 || { echo "helm is required for the rendered UPF security gate." >&2; exit 1; }

RENDER_DIR="$(mktemp -d "${ROOT_DIR}/artifacts/security-upf.XXXXXX")"
trap 'rm -rf "$RENDER_DIR"' EXIT
for slice in embb urllc mmtc v2x; do
  helm template "upf-${slice}" "${FREE5GC_HELM_DIR}/charts/free5gc/charts/free5gc-upf" \
    --namespace free5gc \
    -f "${ROOT_DIR}/k8s/slice-data-plane/upf-${slice}-values.yaml" \
    >"${RENDER_DIR}/upf-${slice}.yaml"
done

helm template free5gc "${FREE5GC_HELM_DIR}/charts/free5gc/charts/free5gc-nef" \
  --namespace free5gc >"${RENDER_DIR}/nef.yaml"
helm template free5gc "${FREE5GC_HELM_DIR}/charts/free5gc/charts/free5gc-webui" \
  --namespace free5gc \
  --set webui.service.type=NodePort \
  --set webui.service.port=5000 \
  --set webui.service.nodePort=30500 \
  >"${RENDER_DIR}/webui.yaml"

# Fail if a future pinned chart changes the selectors used by either internal
# NLB overlay. Helm omits metadata.namespace because release namespace is
# applied by Helm itself, which is why this is an explicit label contract gate.
for rendered in nef webui; do
  grep -q 'app.kubernetes.io/instance: free5gc' "${RENDER_DIR}/${rendered}.yaml"
  grep -q "app.kubernetes.io/name: free5gc-${rendered}" "${RENDER_DIR}/${rendered}.yaml"
  grep -q 'project: free5gc' "${RENDER_DIR}/${rendered}.yaml"
  grep -q "nf: ${rendered}" "${RENDER_DIR}/${rendered}.yaml"
done
RENDER_SCAN_DIR="$RENDER_DIR"
if [[ "$TRIVY" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
  RENDER_SCAN_DIR="$(wslpath -w "$RENDER_DIR")"
fi
# The upstream NEF/WebUI subcharts have separately tracked lint debt. Keep the
# repository-owned security policy gates scoped to the four UPF renders; the
# two internal NLB overlays were already scanned above with no exclusions.
for slice in embb urllc mmtc v2x; do
  "$TRIVY" config --exit-code 1 --severity HIGH,CRITICAL \
    "${RENDER_SCAN_DIR}/upf-${slice}.yaml"
  "$KUBE_LINTER" lint "${RENDER_SCAN_DIR}/upf-${slice}.yaml" \
    --exclude run-as-non-root \
    --exclude unsafe-sysctls
done
"$KUBECONFORM" -strict -ignore-missing-schemas -kubernetes-version 1.36.0 "$RENDER_SCAN_DIR"
