#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/infrastructure/terraform"

AWS_PROFILE="${AWS_PROFILE:-kiki}"
AWS_REGION="${AWS_REGION:-ap-northeast-1}"
AWS_EKS_ENDPOINT_URL="${AWS_EKS_ENDPOINT_URL:-https://eks.${AWS_REGION}.api.aws}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-5gcityverse-prod-eks}"
FREE5GC_NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"
HELM_RELEASE="${HELM_RELEASE:-free5gc}"
FREE5GC_HELM_REPO="${FREE5GC_HELM_REPO:-https://github.com/free5gc/free5gc-helm.git}"
FREE5GC_HELM_DIR="${FREE5GC_HELM_DIR:-/tmp/free5gc-helm}"
FREE5GC_VALUES="${FREE5GC_VALUES:-${ROOT_DIR}/k8s/free5gc-eks-values.yaml}"
FREE5GC_HELM_TIMEOUT="${FREE5GC_HELM_TIMEOUT:-30m}"
UERANSIM_RELEASE="${UERANSIM_RELEASE:-ueransim-city}"
UERANSIM_VALUES="${UERANSIM_VALUES:-${ROOT_DIR}/k8s/ueransim-eks-values.yaml}"
UERANSIM_HELM_TIMEOUT="${UERANSIM_HELM_TIMEOUT:-10m}"

log() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $*"
}

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

aws_eks_cli() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" --endpoint-url "$AWS_EKS_ENDPOINT_URL" eks "$@"
}

retry() {
  local max_attempts="$1"
  local delay_seconds="$2"
  shift 2

  local attempt=1
  until "$@"; do
    if [[ "$attempt" -ge "$max_attempts" ]]; then
      return 1
    fi

    echo "Command failed; retrying in ${delay_seconds}s (${attempt}/${max_attempts}): $*" >&2
    sleep "$delay_seconds"
    attempt=$((attempt + 1))
  done
}

update_kubeconfig() {
  local expected_endpoint
  local current_endpoint

  log "Updating kubeconfig for EKS cluster ${EKS_CLUSTER_NAME} in ${AWS_REGION} with profile ${AWS_PROFILE}..."
  retry 6 10 aws_eks_cli describe-cluster --name "$EKS_CLUSTER_NAME" --query 'cluster.status' --output text >/dev/null
  expected_endpoint="$(aws_eks_cli describe-cluster --name "$EKS_CLUSTER_NAME" --query 'cluster.endpoint' --output text)"
  retry 6 10 aws_eks_cli update-kubeconfig --name "$EKS_CLUSTER_NAME"

  current_endpoint="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  if [[ "$current_endpoint" != "$expected_endpoint" ]]; then
    echo "Kubeconfig endpoint mismatch after update." >&2
    echo "  expected: ${expected_endpoint}" >&2
    echo "  current:  ${current_endpoint:-<empty>}" >&2
    return 1
  fi

  if ! kubectl --request-timeout=15s get --raw=/version >/dev/null; then
    echo "Kubernetes API is not reachable at ${expected_endpoint}." >&2
    echo "Check network/DNS access to the EKS public endpoint, AWS profile ${AWS_PROFILE}, and region ${AWS_REGION}." >&2
    return 1
  fi

  log "Kubeconfig is using the current EKS endpoint: ${expected_endpoint}"
}

sync_free5gc_chart() {
  if [[ -d "$FREE5GC_HELM_DIR/.git" ]]; then
    if ! retry 3 10 git -C "$FREE5GC_HELM_DIR" pull --ff-only; then
      echo "Could not update ${FREE5GC_HELM_DIR}; using the cached free5GC chart." >&2
    fi
  else
    retry 6 10 git clone "$FREE5GC_HELM_REPO" "$FREE5GC_HELM_DIR"
  fi
}

install_gtp5g() {
  kubectl apply -f "$ROOT_DIR/k8s/gtp5g-installer.yaml"
  kubectl -n kube-system rollout status daemonset/gtp5g-installer --timeout=600s
}

dump_free5gc_debug() {
  echo
  echo "free5GC install did not become ready in time. Current Kubernetes diagnostics:"
  echo
  kubectl -n "$FREE5GC_NAMESPACE" get pods -o wide || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" get pvc,svc,deploy,statefulset,job || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" get events --sort-by=.lastTimestamp | tail -80 || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" describe pods || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" logs --all-containers --prefix --tail=80 -l app.kubernetes.io/instance="$HELM_RELEASE" || true
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
    --timeout "$FREE5GC_HELM_TIMEOUT" \
    --wait || {
      dump_free5gc_debug
      return 1
    }
  kubectl -n "$FREE5GC_NAMESPACE" rollout status statefulset/mongodb --timeout=600s
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment --all --timeout=600s
}

install_ueransim() {
  helm upgrade --install "$UERANSIM_RELEASE" "$FREE5GC_HELM_DIR/charts/ueransim" \
    --namespace "$FREE5GC_NAMESPACE" \
    -f "$UERANSIM_VALUES" \
    --timeout "$UERANSIM_HELM_TIMEOUT" \
    --wait
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/"$UERANSIM_RELEASE"-gnb --timeout=300s
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/"$UERANSIM_RELEASE"-ue --timeout=300s
}

install_real_simulation_assets() {
  kubectl apply -f "$ROOT_DIR/k8s/ue-config/embb.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config/urllc.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config/typhoon.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config/mmtc.yaml"
  kubectl apply -f "$ROOT_DIR/k8s/ue-config/v2x.yaml"
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
