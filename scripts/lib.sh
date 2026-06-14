#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/infrastructure/terraform"

AWS_PROFILE="${AWS_PROFILE:-kiki}"
AWS_REGION="${AWS_REGION:-ap-northeast-1}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-5gcityverse-prod-eks}"
FREE5GC_NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"
HELM_RELEASE="${HELM_RELEASE:-free5gc}"
FREE5GC_HELM_REPO="${FREE5GC_HELM_REPO:-https://github.com/free5gc/free5gc-helm.git}"
FREE5GC_HELM_DIR="${FREE5GC_HELM_DIR:-/tmp/free5gc-helm}"
FREE5GC_VALUES="${FREE5GC_VALUES:-${ROOT_DIR}/k8s/free5gc-eks-values.yaml}"
UERANSIM_RELEASE="${UERANSIM_RELEASE:-ueransim-city}"
UERANSIM_VALUES="${UERANSIM_VALUES:-${ROOT_DIR}/k8s/ueransim-eks-values.yaml}"

require_cmd() {
  local missing=0
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "Missing command: $cmd" >&2
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}

tf_output() {
  terraform -chdir="$TF_DIR" output -raw "$1"
}

aws_cli() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

update_kubeconfig() {
  aws_cli eks update-kubeconfig --name "$EKS_CLUSTER_NAME"
}

sync_free5gc_chart() {
  if [[ -d "$FREE5GC_HELM_DIR/.git" ]]; then
    git -C "$FREE5GC_HELM_DIR" pull --ff-only
  else
    git clone "$FREE5GC_HELM_REPO" "$FREE5GC_HELM_DIR"
  fi
}

install_gtp5g() {
  kubectl apply -f "$ROOT_DIR/k8s/gtp5g-installer.yaml"
  kubectl -n kube-system rollout status daemonset/gtp5g-installer --timeout=600s
}

install_free5gc() {
  kubectl get namespace "$FREE5GC_NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$FREE5GC_NAMESPACE"
  cp "$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-smf/smf-configmap-single-upf.yaml" \
    "$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-smf/templates/smf-configmap.yaml"

  # The upstream free5GC chart vendors its subcharts under charts/. Its
  # Chart.yaml dependency entries omit version constraints, which newer Helm
  # releases reject during `helm dependency update` even though install works
  # with the bundled charts.
  if [[ ! -d "$FREE5GC_HELM_DIR/charts/free5gc/charts/mongodb-15.6.0" ]]; then
    if ! helm dependency update "$FREE5GC_HELM_DIR/charts/free5gc"; then
      echo "Helm dependency update failed; continuing with bundled free5GC subcharts." >&2
    fi
  else
    echo "Using bundled free5GC Helm subcharts; skipping dependency update."
  fi

  helm upgrade --install "$HELM_RELEASE" "$FREE5GC_HELM_DIR/charts/free5gc" \
    --namespace "$FREE5GC_NAMESPACE" \
    -f "$FREE5GC_VALUES" \
    --set-json 'free5gc-upf.upf.podSecurityContext.sysctls=[]' \
    --timeout 15m \
    --wait
  kubectl -n "$FREE5GC_NAMESPACE" rollout status statefulset/mongodb --timeout=600s
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment --all --timeout=600s
}

install_ueransim() {
  helm upgrade --install "$UERANSIM_RELEASE" "$FREE5GC_HELM_DIR/charts/ueransim" \
    --namespace "$FREE5GC_NAMESPACE" \
    -f "$UERANSIM_VALUES" \
    --timeout 10m \
    --wait
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/"$UERANSIM_RELEASE"-gnb --timeout=300s
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/"$UERANSIM_RELEASE"-ue --timeout=300s
}

install_real_simulation_assets() {
  kubectl apply -f "$ROOT_DIR/k8s/ue-config-embb.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config-urllc.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config-typhoon.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config-mmtc.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config-v2x.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/iperf3-server.yaml"
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/iperf3-server --timeout=300s
}

free5gc_webui_url() {
  local hostname
  hostname="$(kubectl -n "$FREE5GC_NAMESPACE" get svc webui-service -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  if [[ -n "$hostname" ]]; then
    echo "http://${hostname}:5000"
  fi
}
