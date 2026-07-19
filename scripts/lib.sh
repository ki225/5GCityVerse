#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/infrastructure/terraform"

AWS_PROFILE="${AWS_PROFILE:-kiki}"
AWS_REGION="${AWS_REGION:-ap-northeast-1}"
AWS_EKS_ENDPOINT_URL="${AWS_EKS_ENDPOINT_URL:-}"
KUBECTL_VALIDATE="${KUBECTL_VALIDATE:-true}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-5gcityverse-prod-eks}"
EKS_KUBECONFIG_CONTEXT="${EKS_KUBECONFIG_CONTEXT:-$EKS_CLUSTER_NAME}"
MULTUS_CNI_IMAGE="${MULTUS_CNI_IMAGE:-ghcr.io/k8snetworkplumbingwg/multus-cni@sha256:c8bfe5bad3b5371a5677feb9e8e162da91b61bcac409c244f6f1b18c801ad006}"
ALLOW_FREE5GC_CHART_MULTUS="${ALLOW_FREE5GC_CHART_MULTUS:-false}"
FREE5GC_NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"
HELM_RELEASE="${HELM_RELEASE:-free5gc}"
FREE5GC_HELM_REPO="${FREE5GC_HELM_REPO:-https://github.com/free5gc/free5gc-helm.git}"
FREE5GC_HELM_DIR="${FREE5GC_HELM_DIR:-/tmp/free5gc-helm}"
FREE5GC_HELM_REF="${FREE5GC_HELM_REF:-0d0b4b392bbb1b099acb9a1b37c39e0647ff6d4c}"
FREE5GC_VALUES="${FREE5GC_VALUES:-${ROOT_DIR}/k8s/free5gc-eks-values.yaml}"
SLICE_DATA_PLANE_DIR="${SLICE_DATA_PLANE_DIR:-${ROOT_DIR}/k8s/slice-data-plane}"
FREE5GC_HELM_TIMEOUT="${FREE5GC_HELM_TIMEOUT:-30m}"
SMF_QER_IMAGE_DIGEST="${SMF_QER_IMAGE_DIGEST:-d47063101e6ae2897ab1dd4455b3ebecd1a467bbbce561acb6a56dff02143057}"
SMF_QER_SECRET_NAME="${SMF_QER_SECRET_NAME:-smf-qer-actuator-token}"
UERANSIM_RELEASE="${UERANSIM_RELEASE:-ueransim-city}"
UERANSIM_VALUES="${UERANSIM_VALUES:-${ROOT_DIR}/k8s/ueransim-eks-values.yaml}"
UERANSIM_HELM_TIMEOUT="${UERANSIM_HELM_TIMEOUT:-10m}"
UERANSIM_IMAGE_DIGEST="${UERANSIM_IMAGE_DIGEST:-sha256:58909d22fe2b1d24893fe26eb9502dac1056c85e4135fa87902bf3a1d1eb3e0b}"
METRICS_SERVER_MANIFEST_URL="${METRICS_SERVER_MANIFEST_URL:-https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml}"
METRICS_SERVER_MANIFEST_SHA256="${METRICS_SERVER_MANIFEST_SHA256:-f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441}"

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

ensure_ecr_image_digest() {
  local repository_url="$1"
  local expected_digest="$2"
  local dockerfile="$3"
  local build_context="$4"
  local repository_name image_id restore_tag push_ref actual_digest

  [[ "$expected_digest" == sha256:* ]] || expected_digest="sha256:${expected_digest}"
  if [[ ! "$expected_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Invalid reviewed image digest: ${expected_digest}" >&2
    return 1
  fi
  repository_name="${repository_url##*/}"

  actual_digest="$(aws_cli ecr describe-images \
    --repository-name "$repository_name" \
    --image-ids "imageDigest=${expected_digest}" \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
  if [[ "$actual_digest" == "$expected_digest" ]]; then
    log "Verified reviewed ECR image ${repository_name}@${expected_digest}"
    return 0
  fi

  log "ECR repository ${repository_name} is missing ${expected_digest}; restoring the reviewed image"
  aws_cli ecr get-login-password |
    docker login --username AWS --password-stdin "${repository_url%%/*}" >/dev/null

  image_id="$(docker image ls --digests --no-trunc \
    --format '{{.Digest}} {{.ID}}' | awk -v digest="$expected_digest" '$1 == digest { print $2; exit }')"
  restore_tag="restore-${expected_digest#sha256:}"
  restore_tag="${restore_tag:0:20}"
  push_ref="${repository_url}:${restore_tag}"
  if [[ -n "$image_id" ]]; then
    log "Reusing local image ${image_id} for ${repository_name}@${expected_digest}"
    docker tag "$image_id" "$push_ref"
  else
    log "No local copy of ${expected_digest}; rebuilding from pinned ${dockerfile}"
    docker build --provenance=false -f "$dockerfile" -t "$push_ref" "$build_context"
  fi
  docker push "$push_ref"

  actual_digest="$(aws_cli ecr describe-images \
    --repository-name "$repository_name" \
    --image-ids "imageTag=${restore_tag}" \
    --query 'imageDetails[0].imageDigest' --output text)"
  if [[ "$actual_digest" != "$expected_digest" ]]; then
    echo "Restored image digest mismatch for ${repository_name}." >&2
    echo "  expected: ${expected_digest}" >&2
    echo "  actual:   ${actual_digest:-<missing>}" >&2
    echo "Refusing to deploy an unreviewed image; update the reviewed digest explicitly." >&2
    return 1
  fi
  log "Restored and verified ${repository_name}@${actual_digest}"
}

is_wsl() {
  [[ -r /proc/version ]] && grep -qiE 'microsoft|wsl' /proc/version
}

frontend_npm_ci() {
  local frontend_dir="${ROOT_DIR}/frontend"
  local backup_dir

  (
    cd "$frontend_dir"

    if is_wsl && [[ -e node_modules/@esbuild/win32-x64/esbuild.exe ]]; then
      backup_dir="node_modules.windows-$(date +%Y%m%d%H%M%S)"
      log "Detected Windows frontend node_modules under WSL; moving it to ${backup_dir}"
      mv node_modules "$backup_dir" || {
        echo "Could not move frontend/node_modules out of the way." >&2
        echo "Stop any running Windows/Vite node.exe processes that may be holding esbuild.exe, then rerun." >&2
        return 1
      }
    fi

    npm ci
  )
}

frontend_dependencies_ready() {
  (
    cd "$ROOT_DIR/frontend"
    [[ -d node_modules ]] || return 1
    node -e "require('rollup')" >/dev/null 2>&1 || return 1
    if is_wsl && [[ -e node_modules/@esbuild/win32-x64/esbuild.exe ]]; then
      return 1
    fi
  )
}

ensure_frontend_dependencies() {
  if frontend_dependencies_ready; then
    return 0
  fi

  log "Frontend dependencies are missing or not native to this environment; reinstalling"
  frontend_npm_ci
}

tf_output() {
  terraform -chdir="$TF_DIR" output -raw "$1"
}

aws_cli() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

aws_eks_cli() {
  if [[ -n "$AWS_EKS_ENDPOINT_URL" ]]; then
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" --endpoint-url "$AWS_EKS_ENDPOINT_URL" eks "$@"
  else
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks "$@"
  fi
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

check_kubernetes_api() {
  local endpoint="${1:-}"

  if [[ -z "$endpoint" ]]; then
    endpoint="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  fi

  if ! kubectl --request-timeout=15s get --raw=/version >/dev/null; then
    echo "Kubernetes API is not reachable at ${endpoint:-<unknown endpoint>}." >&2
    echo "Check network/DNS access to the EKS public endpoint, AWS profile ${AWS_PROFILE}, and region ${AWS_REGION}." >&2
    if is_wsl; then
      echo "This is running under WSL; if Windows can reach the endpoint but WSL cannot, restart WSL networking or run from a network/VPN path that can route to EKS." >&2
    fi
    echo "Note: KUBECTL_VALIDATE=false only skips OpenAPI schema validation; it will not fix a no-route-to-host API connection." >&2
    return 1
  fi
}

kubectl_apply() {
  local manifest="$1"

  retry 4 15 kubectl --request-timeout=30s apply --validate="$KUBECTL_VALIDATE" -f "$manifest" || {
    check_kubernetes_api || true
    echo "Failed to apply ${manifest}." >&2
    return 1
  }
}

kubectl_rollout_status() {
  local namespace="$1"
  local resource="$2"
  local timeout="$3"

  retry 6 20 kubectl --request-timeout=30s -n "$namespace" rollout status "$resource" --timeout="$timeout" || {
    check_kubernetes_api || true
    echo "Failed waiting for rollout: ${namespace}/${resource}." >&2
    return 1
  }
}

kubectl_create_namespace_if_missing() {
  local namespace="$1"

  for attempt in 1 2 3 4 5 6; do
    if kubectl --request-timeout=30s get namespace "$namespace" >/dev/null 2>&1; then
      return 0
    fi

    if kubectl --request-timeout=30s create namespace "$namespace"; then
      return 0
    fi

    if [[ "$attempt" -lt 6 ]]; then
      echo "Could not confirm/create namespace ${namespace}; retrying in 15s (${attempt}/6)" >&2
      sleep 15
    fi
  done

  check_kubernetes_api || true
  echo "Failed to confirm/create namespace ${namespace}." >&2
  return 1
}

rollout_status_all_deployments() {
  local deployments
  local output

  output="$(retry 6 20 kubectl --request-timeout=30s -n "$FREE5GC_NAMESPACE" get deployment -o name)" || {
    check_kubernetes_api || true
    echo "Failed to list deployments in namespace ${FREE5GC_NAMESPACE}." >&2
    return 1
  }
  mapfile -t deployments <<<"$output"
  for deployment in "${deployments[@]}"; do
    [[ -n "$deployment" ]] || continue
    kubectl_rollout_status "$FREE5GC_NAMESPACE" "$deployment" 600s
  done
}

recover_pending_helm_release() {
  local status

  status="$(helm -n "$FREE5GC_NAMESPACE" status "$HELM_RELEASE" 2>/dev/null | awk -F': ' '/^STATUS:/ {print $2; exit}' || true)"
  case "$status" in
    pending-install|pending-upgrade|pending-rollback)
      log "Helm release ${HELM_RELEASE} is ${status}; uninstalling the incomplete release before retrying"
      helm -n "$FREE5GC_NAMESPACE" uninstall "$HELM_RELEASE" --wait --timeout 10m || {
        echo "Failed to uninstall pending Helm release ${HELM_RELEASE}." >&2
        return 1
      }
      ;;
  esac
}

recover_ueransim_helm_release() {
  local status

  status="$(helm -n "$FREE5GC_NAMESPACE" status "$UERANSIM_RELEASE" 2>/dev/null | awk -F': ' '/^STATUS:/ {print $2; exit}' || true)"
  case "$status" in
    failed|pending-install|pending-upgrade|pending-rollback)
      log "Helm release ${UERANSIM_RELEASE} is ${status}; uninstalling it before retrying"
      helm -n "$FREE5GC_NAMESPACE" uninstall "$UERANSIM_RELEASE" --wait --timeout 10m || {
        echo "Failed to uninstall unhealthy Helm release ${UERANSIM_RELEASE}." >&2
        return 1
      }
      ;;
  esac
}

free5gc_chart_multus_enabled() {
  awk '
    /^global:/ { in_global = 1; next }
    in_global && /^[^[:space:]]/ { in_global = 0 }
    in_global && /^  (amf|upf):/ { in_nf = 1; next }
    in_nf && /^    multus:/ { in_multus = 1; next }
    in_multus && /^      enabled:[[:space:]]*true[[:space:]]*$/ { found = 1 }
    in_multus && /^  [^[:space:]]/ { in_nf = 0; in_multus = 0 }
    END { exit found ? 0 : 1 }
  ' "$FREE5GC_VALUES"
}

validate_free5gc_values_for_eks() {
  if ! free5gc_chart_multus_enabled; then
    return 0
  fi

  if [[ "$ALLOW_FREE5GC_CHART_MULTUS" == "true" ]]; then
    echo "ALLOW_FREE5GC_CHART_MULTUS=true; skipping EKS eth1 safety guard." >&2
    return 0
  fi

  cat >&2 <<EOF
free5GC chart-level Multus is enabled in ${FREE5GC_VALUES}, but this EKS
deployment does not provision a stable, unmanaged host eth1 for AMF/UPF
ipvlan attachments.

Leave global.amf.multus.enabled and global.upf.multus.enabled set to false
for the managed-node deployment path. Only set ALLOW_FREE5GC_CHART_MULTUS=true
after adding node bootstrap/Terraform automation that creates and verifies the
required host interface before Helm installs free5GC.
EOF
  return 1
}

update_kubeconfig() {
  local expected_endpoint
  local api_endpoint
  local tls_server_name
  local cluster_entry
  local current_endpoint
  local kube_context="${EKS_KUBECONFIG_CONTEXT:-$EKS_CLUSTER_NAME}"

  log "Updating kubeconfig for EKS cluster ${EKS_CLUSTER_NAME} in ${AWS_REGION} with profile ${AWS_PROFILE}..."
  retry 6 10 aws_eks_cli describe-cluster --name "$EKS_CLUSTER_NAME" --query 'cluster.status' --output text >/dev/null
  expected_endpoint="$(aws_eks_cli describe-cluster --name "$EKS_CLUSTER_NAME" --query 'cluster.endpoint' --output text)"
  retry 6 10 aws_eks_cli update-kubeconfig \
    --name "$EKS_CLUSTER_NAME" \
    --alias "$kube_context" \
    --user-alias "$kube_context"
  kubectl config use-context "$kube_context" >/dev/null

  api_endpoint="${EKS_API_TUNNEL_ENDPOINT:-$expected_endpoint}"
  if [[ -n "${EKS_API_TUNNEL_ENDPOINT:-}" ]]; then
    tls_server_name="${EKS_API_TLS_SERVER_NAME:-${expected_endpoint#https://}}"
    cluster_entry="$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')"
    kubectl config set-cluster "$cluster_entry" \
      --server="$api_endpoint" \
      --tls-server-name="$tls_server_name" >/dev/null
    log "Routing Kubernetes API access through ${api_endpoint} with TLS SNI ${tls_server_name}"
  fi

  current_endpoint="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  if [[ "$current_endpoint" != "$api_endpoint" ]]; then
    echo "Kubeconfig endpoint mismatch after update." >&2
    echo "  expected: ${api_endpoint}" >&2
    echo "  current:  ${current_endpoint:-<empty>}" >&2
    return 1
  fi

  check_kubernetes_api "$api_endpoint"

  log "Kubeconfig is using the current EKS endpoint: ${api_endpoint}"
}

sync_free5gc_chart() {
  if [[ -d "$FREE5GC_HELM_DIR/.git" ]]; then
    retry 3 10 git -C "$FREE5GC_HELM_DIR" fetch --tags --force origin "$FREE5GC_HELM_REF"
  else
    retry 6 10 git clone --filter=blob:none --no-checkout "$FREE5GC_HELM_REPO" "$FREE5GC_HELM_DIR"
  fi
  git -C "$FREE5GC_HELM_DIR" checkout --detach --force "$FREE5GC_HELM_REF"
  if [[ "$(git -C "$FREE5GC_HELM_DIR" rev-parse HEAD)" != "$FREE5GC_HELM_REF" ]]; then
    echo "free5GC Helm checkout did not resolve to the reviewed commit ${FREE5GC_HELM_REF}." >&2
    return 1
  fi
  log "Using pinned free5GC Helm ref ${FREE5GC_HELM_REF} ($(git -C "$FREE5GC_HELM_DIR" rev-parse HEAD))"
}

patch_free5gc_smf_actuator_template() {
  local patch_file="${ROOT_DIR}/k8s/helm-overlays/free5gc/smf-qer-actuator.patch"
  if git -C "$FREE5GC_HELM_DIR" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    return 0
  fi
  git -C "$FREE5GC_HELM_DIR" apply --check "$patch_file"
  git -C "$FREE5GC_HELM_DIR" apply "$patch_file"
}

install_smf_qer_secret() {
  local token_file
  [[ -n "${SMF_QER_ACTUATOR_TOKEN:-}" ]] || {
    echo "SMF_QER_ACTUATOR_TOKEN is required before installing free5GC." >&2
    return 1
  }
  kubectl_create_namespace_if_missing "$FREE5GC_NAMESPACE"
  token_file="$(mktemp)"
  trap 'rm -f "$token_file"; trap - RETURN' RETURN
  chmod 0600 "$token_file"
  printf '%s' "$SMF_QER_ACTUATOR_TOKEN" >"$token_file"
  kubectl -n "$FREE5GC_NAMESPACE" create secret generic "$SMF_QER_SECRET_NAME" \
    --from-file="token=${token_file}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

install_multus() {
  local manifest

  manifest="$(mktemp)"
  trap 'rm -f "$manifest"; trap - RETURN' RETURN
  sed "s#ghcr.io/k8snetworkplumbingwg/multus-cni@sha256:c8bfe5bad3b5371a5677feb9e8e162da91b61bcac409c244f6f1b18c801ad006#${MULTUS_CNI_IMAGE}#g" \
    "$ROOT_DIR/k8s/multus-daemonset.yaml" >"$manifest"

  log "Installing Multus CNI with image ${MULTUS_CNI_IMAGE}"
  kubectl_apply "$manifest"
  retry 6 10 kubectl --request-timeout=30s wait \
    --for=condition=Established \
    crd/network-attachment-definitions.k8s.cni.cncf.io \
    --timeout=60s || {
      check_kubernetes_api || true
      echo "Failed waiting for Multus NetworkAttachmentDefinition CRD." >&2
      return 1
  }
  kubectl_rollout_status kube-system daemonset/kube-multus-ds 600s
}

install_gtp5g() {
  kubectl_apply "$ROOT_DIR/k8s/gtp5g-installer.yaml"
  kubectl_rollout_status kube-system daemonset/gtp5g-installer 600s
}

install_gtp5g_metrics_exporter() {
  kubectl_create_namespace_if_missing "$FREE5GC_NAMESPACE"
  kubectl_apply "$ROOT_DIR/k8s/gtp5g-metrics-exporter.yaml"
  kubectl_rollout_status "$FREE5GC_NAMESPACE" daemonset/gtp5g-metrics-exporter 300s
}

install_metrics_server() {
  local manifest actual_sha
  manifest="$(mktemp)"
  trap 'rm -f "$manifest"; trap - RETURN' RETURN
  log "Installing checksum-pinned Kubernetes metrics-server from ${METRICS_SERVER_MANIFEST_URL}"
  retry 4 15 curl --fail --silent --show-error --location "$METRICS_SERVER_MANIFEST_URL" --output "$manifest"
  actual_sha="$(sha256sum "$manifest" | awk '{print $1}')"
  if [[ "$actual_sha" != "$METRICS_SERVER_MANIFEST_SHA256" ]]; then
    echo "metrics-server manifest checksum mismatch: expected ${METRICS_SERVER_MANIFEST_SHA256}, got ${actual_sha}" >&2
    return 1
  fi
  kubectl_apply "$manifest" || {
    check_kubernetes_api || true
    echo "Failed to apply metrics-server manifest." >&2
    return 1
  }
  if ! kubectl -n kube-system get deployment metrics-server \
      -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- '--kubelet-insecure-tls'; then
    kubectl -n kube-system patch deployment metrics-server --type='json' -p='[
      {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}
    ]' >/dev/null
  fi
  kubectl_rollout_status kube-system deployment/metrics-server 300s
  retry 12 10 kubectl --request-timeout=15s get --raw /apis/metrics.k8s.io/v1beta1/nodes >/dev/null
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

dump_ueransim_debug() {
  echo
  echo "UERANSIM install did not become ready in time. Current Kubernetes diagnostics:"
  echo
  helm -n "$FREE5GC_NAMESPACE" status "$UERANSIM_RELEASE" || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" get deploy,rs,pod,svc -l app.kubernetes.io/instance="$UERANSIM_RELEASE" -o wide || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" get events --sort-by=.lastTimestamp | tail -80 || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" describe pod -l app.kubernetes.io/instance="$UERANSIM_RELEASE" || true
  echo
  kubectl -n "$FREE5GC_NAMESPACE" logs --all-containers --prefix --tail=120 -l app.kubernetes.io/instance="$UERANSIM_RELEASE" || true
}

patch_free5gc_upf_wrapper_template() {
  local template="$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-upf/templates/upf/upf-configmap.yaml"
  local deployment="$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-upf/templates/upf/upf-deployment.yaml"
  [[ -f "$template" ]] || return 0
  python3 - "$template" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = """    # Wait for upfgtp interface to be created by gtp5g module
    echo "[wrapper] Waiting for upfgtp interface..."
    for i in $(seq 1 30); do
      if ip link show upfgtp > /dev/null 2>&1; then
        echo "[wrapper] upfgtp interface is ready"
        break
      fi
      sleep 1
    done

    # Setup iptables for NAT (single interface mode - eth0)
    iptables -A FORWARD -j ACCEPT
    iptables -t nat -A POSTROUTING -s {{ $.Values.global.uesubnet }} -o eth0 -j MASQUERADE
"""
ready_anchor = """    # free5GC UPF creates the gtp5g link itself. A stale upfgtp link can remain
    # after CrashLoop/restart on EKS nodes and makes UPF fail with "file exists".
"""
wait_for_module = """    # The gtp5g installer compiles and loads the patched kernel module on every
    # replacement user-plane node. Do not let UPF race that bootstrap: an early
    # open attempt exits with \"operation not supported\" and drops PFCP/GTP state.
    echo "[wrapper] Waiting for patched gtp5g QoS module..."
    for i in $(seq 1 180); do
      if grep -q "QoS Enable: 1" /proc/gtp5g/qos 2>/dev/null; then
        echo "[wrapper] patched gtp5g QoS module is ready"
        break
      fi
      if [ "$i" -eq 180 ]; then
        echo "[wrapper] patched gtp5g QoS module was not ready after 360 seconds" >&2
        exit 1
      fi
      sleep 2
    done

"""
new = wait_for_module + ready_anchor + """    if ip link show upfgtp >/dev/null 2>&1; then
      echo "[wrapper] Removing stale upfgtp interface before UPF starts"
      ip link delete upfgtp || true
    fi

    # Setup iptables for NAT (single interface mode - eth0)
    iptables -C FORWARD -j ACCEPT 2>/dev/null || iptables -A FORWARD -j ACCEPT
    iptables -t nat -C POSTROUTING -s {{ $.Values.global.uesubnet }} -o eth0 -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s {{ $.Values.global.uesubnet }} -o eth0 -j MASQUERADE
"""
if old in text:
    path.write_text(text.replace(old, new))
elif "Waiting for patched gtp5g QoS module" in text:
    pass
elif ready_anchor in text:
    path.write_text(text.replace(ready_anchor, wait_for_module + ready_anchor))
else:
    raise SystemExit(f"Could not patch UPF wrapper template: {path}")
PY

  [[ -f "$deployment" ]] || return 0
  python3 - "$deployment" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
mount_anchor = """        - mountPath: {{ .volume.tlsmount }}
          name: {{ include "free5gc-upf.fullname" $ }}-{{ .name }}-empty-volume
"""
mount_replacement = mount_anchor + """        - mountPath: /tmp
          name: {{ include "free5gc-upf.fullname" $ }}-{{ .name }}-tmp-volume
"""
volume_anchor = """      - name: {{ include "free5gc-upf.fullname" $ }}-{{ .name }}-empty-volume
        emptyDir: {}
"""
volume_replacement = volume_anchor + """      - name: {{ include "free5gc-upf.fullname" $ }}-{{ .name }}-tmp-volume
        emptyDir:
          sizeLimit: 64Mi
"""
if "- mountPath: /tmp" not in text:
    if mount_anchor not in text or volume_anchor not in text:
        raise SystemExit(f"Could not add UPF /tmp emptyDir to: {path}")
    text = text.replace(mount_anchor, mount_replacement).replace(volume_anchor, volume_replacement)
    path.write_text(text)
PY
}

# The upstream NSSF configmap template hardcodes an example PLMN (466/92),
# fake NF UUIDs, and nsiList/amfSetList S-NSSAI combos that don't match this
# project's PLMN (208/93) or its five configured S-NSSAIs (see the nssf
# section of k8s/free5gc-eks-values.yaml). Chart values can't override this
# because nsiList/amfSetList are hardcoded in the template, not sourced from
# .Values.
patch_free5gc_nssf_template() {
  local template="$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-nssf/templates/nssf-configmap.yaml"
  [[ -f "$template" ]] || return 0
  python3 - "$template" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = """      nsiList:
        - snssai:
            sst: 1
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 10
        - snssai:
            sst: 1
            sd: 1
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 11
        - snssai:
            sst: 1
            sd: 2
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 12
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 12
        - snssai:
            sst: 1
            sd: 3
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 13
        - snssai:
            sst: 2
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 20
        - snssai:
            sst: 2
            sd: 1
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 21
        - snssai:
            sst: 1
            sd: 010203
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 22
      amfSetList:
        - amfSetId: 1
          amfList:
            - ffa2e8d7-3275-49c7-8631-6af1df1d9d26
            - 0e8831c3-6286-4689-ab27-1e2161e15cb1
            - a1fba9ba-2e39-4e22-9c74-f749da571d0d
          nrfAmfSet: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:8081/nnrf-nfm/v1/nf-instances
          supportedNssaiAvailabilityData:
            - tai:
                plmnId:
                  mcc: 466
                  mnc: 92
                tac: 33456
              supportedSnssaiList:
                - sst: 1
                  sd: 1
                - sst: 1
                  sd: 2
                - sst: 2
                  sd: 1
            - tai:
                plmnId:
                  mcc: 466
                  mnc: 92
                tac: 33457
              supportedSnssaiList:
                - sst: 1
                - sst: 1
                  sd: 1
                - sst: 1
                  sd: 2
        - amfSetId: 2
          nrfAmfSet: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:8084/nnrf-nfm/v1/nf-instances
          supportedNssaiAvailabilityData:
            - tai:
                plmnId:
                  mcc: 466
                  mnc: 92
                tac: 33456
              supportedSnssaiList:
                - sst: 1
                - sst: 1
                  sd: 1
                - sst: 1
                  sd: 3
                - sst: 2
                  sd: 1
            - tai:
                plmnId:
                  mcc: 466
                  mnc: 92
                tac: 33458
              supportedSnssaiList:
                - sst: 1
                - sst: 1
                  sd: 1
                - sst: 2
"""
new = """      nsiList:
        - snssai:
            sst: 1
            sd: 000001
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 10
        - snssai:
            sst: 2
            sd: 000002
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 11
        - snssai:
            sst: 2
            sd: 000003
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 12
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 12
        - snssai:
            sst: 3
            sd: 000004
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 13
        - snssai:
            sst: 4
            sd: 000005
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 20
        - snssai:
            sst: 1
            sd: 000001
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 21
        - snssai:
            sst: 2
            sd: 000002
          nsiInformationList:
            - nrfId: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:{{ $.Values.global.nrf.service.port }}/nnrf-nfm/v1/nf-instances
              nsiId: 22
      amfSetList:
        - amfSetId: 1
          amfList:
            - ffa2e8d7-3275-49c7-8631-6af1df1d9d26
            - 0e8831c3-6286-4689-ab27-1e2161e15cb1
            - a1fba9ba-2e39-4e22-9c74-f749da571d0d
          nrfAmfSet: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:8081/nnrf-nfm/v1/nf-instances
          supportedNssaiAvailabilityData:
            - tai:
                plmnId:
                  mcc: 208
                  mnc: 93
                tac: 000001
              supportedSnssaiList:
                - sst: 1
                  sd: 000001
                - sst: 2
                  sd: 000002
                - sst: 2
                  sd: 000003
            - tai:
                plmnId:
                  mcc: 208
                  mnc: 93
                tac: 000001
              supportedSnssaiList:
                - sst: 3
                  sd: 000004
                - sst: 4
                  sd: 000005
                - sst: 1
                  sd: 000001
        - amfSetId: 2
          nrfAmfSet: {{ $.Values.global.sbi.scheme }}://{{ $.Values.global.nrf.service.name }}:8084/nnrf-nfm/v1/nf-instances
          supportedNssaiAvailabilityData:
            - tai:
                plmnId:
                  mcc: 208
                  mnc: 93
                tac: 000001
              supportedSnssaiList:
                - sst: 1
                  sd: 000001
                - sst: 2
                  sd: 000002
                - sst: 2
                  sd: 000003
                - sst: 3
                  sd: 000004
            - tai:
                plmnId:
                  mcc: 208
                  mnc: 93
                tac: 000001
              supportedSnssaiList:
                - sst: 4
                  sd: 000005
                - sst: 1
                  sd: 000001
                - sst: 2
                  sd: 000002
"""
if old in text:
    path.write_text(text.replace(old, new))
elif "mcc: 466" in text or "mnc: 92" in text:
    raise SystemExit(f"Could not patch NSSF nsiList/amfSetList template: {path}")
PY
}

install_free5gc() {
  local smf_image_repository
  kubectl_create_namespace_if_missing "$FREE5GC_NAMESPACE"
  validate_free5gc_values_for_eks
  recover_pending_helm_release

  cp "$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-smf/smf-configmap-single-upf.yaml" \
    "$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-smf/templates/smf-configmap.yaml"
  patch_free5gc_upf_wrapper_template
  patch_free5gc_nssf_template
  patch_free5gc_smf_actuator_template
  install_smf_qer_secret
  smf_image_repository="${SMF_QER_IMAGE_REPOSITORY:-$(tf_output smf_qer_actuator_ecr_repository_url)}"

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

  # Deploy one independently addressable UPF per teaching slice.  The parent
  # chart's single UPF is disabled so the UI cannot accidentally claim
  # isolation while every S-NSSAI still traverses the same user plane.
  local slice release values_file
  for slice in embb urllc mmtc v2x; do
    release="upf-${slice}"
    values_file="${SLICE_DATA_PLANE_DIR}/upf-${slice}-values.yaml"
    helm upgrade --install "$release" \
      "$FREE5GC_HELM_DIR/charts/free5gc/charts/free5gc-upf" \
      --namespace "$FREE5GC_NAMESPACE" \
      -f "$values_file" \
      --set-json 'upf.podSecurityContext.sysctls=[]' \
      --timeout "$FREE5GC_HELM_TIMEOUT" \
      --wait || {
        dump_free5gc_debug
        return 1
    }
  done

  helm upgrade --install "$HELM_RELEASE" "$FREE5GC_HELM_DIR/charts/free5gc" \
    --namespace "$FREE5GC_NAMESPACE" \
    -f "$FREE5GC_VALUES" \
    -f "${SLICE_DATA_PLANE_DIR}/smf-multi-slice-values.yaml" \
    --set deployUpf=false \
    --set-string "free5gc-smf.smf.image.name=${smf_image_repository}" \
    --set-string 'free5gc-smf.smf.image.tag=' \
    --set-string "free5gc-smf.smf.image.digest=${SMF_QER_IMAGE_DIGEST#sha256:}" \
    --set free5gc-smf.smf.actuator.enabled=true \
    --set-string "free5gc-smf.smf.actuator.secretName=${SMF_QER_SECRET_NAME}" \
    --timeout "$FREE5GC_HELM_TIMEOUT" \
    --wait || {
      dump_free5gc_debug
      return 1
  }
  kubectl_rollout_status "$FREE5GC_NAMESPACE" statefulset/mongodb 600s
  rollout_status_all_deployments
  install_nef_internal_nlb
  install_webui_internal_nlb

  for slice in embb urllc mmtc v2x; do
    release="upf-${slice}"
    kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment \
      -l "app.kubernetes.io/instance=${release}" --timeout=600s
    if [[ "$(kubectl -n "$FREE5GC_NAMESPACE" get endpoints \
      "${release}-free5gc-upf-upf-${slice}-service" \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" == "" ]]; then
      echo "Dedicated ${slice} UPF has no ready PFCP endpoint." >&2
      return 1
    fi
  done

  # Force SMF to resolve all four headless UPF services and establish fresh
  # PFCP associations after the data-plane releases are ready.
  kubectl -n "$FREE5GC_NAMESPACE" rollout restart deployment \
    -l 'app.kubernetes.io/name=free5gc-smf'
  kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment \
    -l 'app.kubernetes.io/name=free5gc-smf' --timeout=600s

  # If AMF was restarted by this upgrade, any already-running gNB keeps a dead
  # SCTP association to the old AMF pod and UERANSIM never re-runs NGSetup —
  # every UE registration then fails with "AMF selection failed" until the gNB
  # is bounced (observed 2026-07-05). Restart gNB/UE deployments if they exist.
  if kubectl -n "$FREE5GC_NAMESPACE" get deploy "$UERANSIM_RELEASE"-gnb >/dev/null 2>&1; then
    log "Restarting UERANSIM gNB/UE to re-establish NGAP after free5GC upgrade"
    kubectl -n "$FREE5GC_NAMESPACE" rollout restart deploy "$UERANSIM_RELEASE"-gnb || true
    kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/"$UERANSIM_RELEASE"-gnb 300s || true
    sleep 5
    for dep in "$UERANSIM_RELEASE"-ue ueransim-city-mmtc; do
      kubectl -n "$FREE5GC_NAMESPACE" get deploy "$dep" >/dev/null 2>&1 && \
        kubectl -n "$FREE5GC_NAMESPACE" rollout restart deploy "$dep" || true
    done
  fi
}

install_nef_internal_nlb() {
  local service_name="free5gc-nef-private"

  log "Installing private NEF NLB overlay for VPC-attached Lambda clients"
  kubectl_apply "$ROOT_DIR/k8s/helm-overlays/free5gc-nef-internal-nlb.yaml"
  retry 60 10 internal_nlb_ready "$service_name"
}

nef_internal_nlb_ready() {
  internal_nlb_ready free5gc-nef-private
}

install_webui_internal_nlb() {
  local service_name="free5gc-webui-private"

  log "Installing private WebUI NLB overlay for VPC-attached Lambda clients"
  kubectl_apply "$ROOT_DIR/k8s/helm-overlays/free5gc-webui-internal-nlb.yaml"
  retry 60 10 internal_nlb_ready "$service_name"
}

webui_internal_nlb_ready() {
  internal_nlb_ready free5gc-webui-private
}

service_load_balancer_hostname() {
  local service_name="$1"
  local hostname
  hostname="$(kubectl --request-timeout=15s -n "$FREE5GC_NAMESPACE" get service "$service_name" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"

  # Keep hostname syntax strict so an IP, cluster-local name, or arbitrary
  # text is never copied into Terraform state. NLB DNS names do not reliably
  # carry an "internal-" prefix; the ELBv2 Scheme check below is authoritative.
  if [[ ! "$hostname" =~ ^[a-z0-9][a-z0-9.-]*\.elb\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$ ]]; then
    return 1
  fi
  printf '%s\n' "$hostname"
}

internal_nlb_ready() {
  local service_name="$1"
  local service_type scheme_annotation legacy_internal hostname actual_scheme endpoint

  service_type="$(kubectl --request-timeout=15s -n "$FREE5GC_NAMESPACE" get service "$service_name" \
    -o jsonpath='{.spec.type}' 2>/dev/null || true)"
  scheme_annotation="$(kubectl --request-timeout=15s -n "$FREE5GC_NAMESPACE" get service "$service_name" \
    -o jsonpath='{.metadata.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-scheme}' 2>/dev/null || true)"
  legacy_internal="$(kubectl --request-timeout=15s -n "$FREE5GC_NAMESPACE" get service "$service_name" \
    -o jsonpath='{.metadata.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-internal}' 2>/dev/null || true)"
  hostname="$(service_load_balancer_hostname "$service_name")" || return 1
  endpoint="$(kubectl --request-timeout=15s -n "$FREE5GC_NAMESPACE" get endpoints "$service_name" \
    -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null || true)"

  [[ "$service_type" == "LoadBalancer" ]] || return 1
  [[ "$scheme_annotation" == "internal" || "$legacy_internal" == "true" ]] || return 1
  [[ -n "$endpoint" ]] || return 1

  # Kubernetes annotations express intent; ELBv2 is the authoritative check
  # that AWS actually provisioned an internal load balancer with this DNS name.
  actual_scheme="$(aws_cli elbv2 describe-load-balancers \
    --query "LoadBalancers[?DNSName=='${hostname}'].Scheme | [0]" \
    --output text 2>/dev/null || true)"
  [[ "$actual_scheme" == "internal" ]]
}

internal_nlb_url() {
  local service_name="$1"
  local port="$2"
  local hostname
  internal_nlb_ready "$service_name" || return 1
  hostname="$(service_load_balancer_hostname "$service_name")" || return 1
  printf 'http://%s:%s\n' "$hostname" "$port"
}

install_ueransim() {
  recover_ueransim_helm_release
  local resident_baseline_overlay="$ROOT_DIR/k8s/helm-overlays/ueransim/ue-deployment.yaml"
  local resident_baseline_target="$FREE5GC_HELM_DIR/charts/ueransim/templates/ue/ue-deployment.yaml"
  local ueransim_image_repository
  if [[ ! -f "$resident_baseline_overlay" || ! -f "$resident_baseline_target" ]]; then
    echo "Resident UE baseline Helm overlay or upstream target is missing." >&2
    echo "overlay=$resident_baseline_overlay target=$resident_baseline_target" >&2
    return 1
  fi
  # The upstream chart has no extraContainers value. Keep the repository-owned
  # overlay explicit instead of mutating the live Deployment after Helm runs.
  cp "$resident_baseline_overlay" "$resident_baseline_target"
  ueransim_image_repository="${UERANSIM_IMAGE_REPOSITORY:-$(tf_output ueransim_ecr_repository_url)}"
  helm upgrade --install "$UERANSIM_RELEASE" "$FREE5GC_HELM_DIR/charts/ueransim" \
    --namespace "$FREE5GC_NAMESPACE" \
    -f "$UERANSIM_VALUES" \
    --set-string "gnb.image.name=${ueransim_image_repository}@sha256" \
    --set-string "gnb.image.tag=${UERANSIM_IMAGE_DIGEST#sha256:}" \
    --set-string "ue.image.name=${ueransim_image_repository}@sha256" \
    --set-string "ue.image.tag=${UERANSIM_IMAGE_DIGEST#sha256:}" \
    --force \
    --timeout "$UERANSIM_HELM_TIMEOUT" \
    --wait || {
      dump_ueransim_debug
      return 1
  }
  kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/"$UERANSIM_RELEASE"-gnb 300s
  kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/"$UERANSIM_RELEASE"-ue 300s
  # `helm --force` can start gNB and UE at the same time. Deployment readiness
  # precedes NG Setup, so explicitly reconnect gNB first and only then restart
  # the UEs to avoid registering against an empty AMF context.
  kubectl -n "$FREE5GC_NAMESPACE" rollout restart deploy "$UERANSIM_RELEASE"-gnb
  kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/"$UERANSIM_RELEASE"-gnb 300s
  sleep 5
  for dep in "$UERANSIM_RELEASE"-ue ueransim-city-mmtc; do
    if kubectl -n "$FREE5GC_NAMESPACE" get deploy "$dep" >/dev/null 2>&1; then
      kubectl -n "$FREE5GC_NAMESPACE" rollout restart deploy "$dep"
      kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/"$dep" 300s
    fi
  done
}

install_real_simulation_assets() {
  install_metrics_server
  kubectl_apply "$ROOT_DIR/k8s/ue-config/embb.yaml"
  kubectl_apply "$ROOT_DIR/k8s/ue-config/urllc.yaml"
  kubectl_apply "$ROOT_DIR/k8s/ue-config/typhoon.yaml"
  kubectl_apply "$ROOT_DIR/k8s/ue-config/mmtc.yaml"
  kubectl_apply "$ROOT_DIR/k8s/ue-config/mmtc-baseline.yaml"
  kubectl_apply "$ROOT_DIR/k8s/ue-config/v2x.yaml"
  kubectl_apply "$ROOT_DIR/k8s/iperf3-server.yaml"
  kubectl_apply "$ROOT_DIR/k8s/upf-hpa.yaml"
  kubectl_apply "$ROOT_DIR/k8s/pfcp-evidence-collector.yaml"
  kubectl_rollout_status "$FREE5GC_NAMESPACE" deployment/iperf3-server 300s
}

free5gc_webui_url() {
  internal_nlb_url free5gc-webui-private 5000
}

free5gc_nef_url() {
  internal_nlb_url free5gc-nef-private 8080
}
