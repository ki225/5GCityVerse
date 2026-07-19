#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

require_cmd aws curl terraform kubectl helm git npm python3 sha256sum docker
export AWS_PROFILE AWS_REGION
aws sts --profile "$AWS_PROFILE" --region "$AWS_REGION" get-caller-identity --no-cli-pager >/dev/null || {
  echo "AWS STS preflight failed for profile ${AWS_PROFILE} in ${AWS_REGION}; refusing to plan with fallback credentials." >&2
  exit 1
}

PLAN_DIR="${ROOT_DIR}/artifacts/terraform-plans"
mkdir -p "$PLAN_DIR"

terraform_reviewed_apply() {
  local terraform_dir="$1"
  local phase="$2"
  shift 2
  local stamp plan_file plan_text plan_sha apply_sha answer reviewer review_file
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  plan_file="${PLAN_DIR}/${stamp}-${phase}.tfplan"
  plan_text="${plan_file}.txt"
  review_file="${plan_file}.reviewed"

  terraform -chdir="$terraform_dir" plan -lock-timeout=10m -out="$plan_file" "$@"
  terraform -chdir="$terraform_dir" show -no-color "$plan_file" | tee "$plan_text"
  plan_sha="$(sha256sum "$plan_file" | awk '{print $1}')"
  printf '%s  %s\n' "$plan_sha" "$(basename "$plan_file")" >"${plan_file}.sha256"
  log "Reviewed-plan artifact: ${plan_file} (sha256 ${plan_sha})"

  reviewer="${DEPLOY_REVIEWER:-}"
  if [[ -z "$reviewer" ]]; then
    read -r -p "Reviewer name/identity for this plan artifact: " reviewer
  fi
  [[ -n "$reviewer" ]] || { echo "A reviewer identity is required." >&2; exit 1; }
  if [[ "${DEPLOY_APPROVED:-false}" != "true" ]]; then
    read -r -p "Apply this exact saved plan? Type 'apply ${phase}': " answer
    if [[ "$answer" != "apply ${phase}" ]]; then
      echo "Apply cancelled; saved plan retained at ${plan_file}." >&2
      exit 1
    fi
  fi
  printf 'reviewer=%s\nreviewed_at=%s\nphase=%s\nsha256=%s\nplan_text=%s\n' \
    "$reviewer" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase" "$plan_sha" "$plan_text" >"$review_file"

  apply_sha="$(sha256sum "$plan_file" | awk '{print $1}')"
  if [[ "$apply_sha" != "$plan_sha" ]]; then
    echo "Saved plan changed after review; refusing apply (${plan_sha} != ${apply_sha})." >&2
    exit 1
  fi
  terraform -chdir="$terraform_dir" apply -lock-timeout=10m "$plan_file"
}

BOOTSTRAP_DIR="${ROOT_DIR}/infrastructure/bootstrap"
AWS_ACCOUNT_ID="$(aws sts --profile "$AWS_PROFILE" --region "$AWS_REGION" get-caller-identity --query Account --output text --no-cli-pager)"
STATE_BUCKET_NAME="${TF_STATE_BUCKET_NAME:-5gcityverse-prod-tfstate-${AWS_ACCOUNT_ID}-${AWS_REGION}}"
BOOTSTRAP_ENVIRONMENT="${TF_STATE_ENVIRONMENT:-prod}"
if [[ ! "$STATE_BUCKET_NAME" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  echo "TF_STATE_BUCKET_NAME is not a valid S3 bucket name." >&2
  exit 1
fi
BOOTSTRAP_TF_ARGS=(
  "-var=aws_region=${AWS_REGION}"
  "-var=environment=${BOOTSTRAP_ENVIRONMENT}"
  "-var=state_bucket_name=${STATE_BUCKET_NAME}"
)
terraform -chdir="$BOOTSTRAP_DIR" init -input=false
terraform -chdir="$BOOTSTRAP_DIR" fmt -check
terraform -chdir="$BOOTSTRAP_DIR" validate

# A bootstrap root intentionally keeps local state. A fresh checkout may
# therefore see a bucket that belongs to this account but has no local state.
# Recover only after independently verifying owner, region, and the complete
# ownership tag contract. Any unreadable or mismatched attribute is fatal.
STATE_BUCKET_OWNED="$(aws_cli s3api list-buckets \
  --query "contains(Buckets[].Name, '${STATE_BUCKET_NAME}')" --output text)"
if [[ "$STATE_BUCKET_OWNED" == "True" ]]; then
  aws_cli s3api head-bucket --bucket "$STATE_BUCKET_NAME" \
    --expected-bucket-owner "$AWS_ACCOUNT_ID" >/dev/null
  STATE_BUCKET_REGION="$(aws_cli s3api get-bucket-location \
    --bucket "$STATE_BUCKET_NAME" --expected-bucket-owner "$AWS_ACCOUNT_ID" \
    --query 'LocationConstraint' --output text)"
  [[ "$STATE_BUCKET_REGION" == "None" ]] && STATE_BUCKET_REGION="us-east-1"
  if [[ "$STATE_BUCKET_REGION" != "$AWS_REGION" ]]; then
    echo "Existing state bucket region mismatch: expected ${AWS_REGION}, got ${STATE_BUCKET_REGION}." >&2
    exit 1
  fi
  STATE_BUCKET_TAGS="$(aws_cli s3api get-bucket-tagging \
    --bucket "$STATE_BUCKET_NAME" --expected-bucket-owner "$AWS_ACCOUNT_ID" --output json)"
  python3 - "$BOOTSTRAP_ENVIRONMENT" "$STATE_BUCKET_TAGS" <<'PY'
import json
import sys

environment, raw = sys.argv[1:]
actual = {item["Key"]: item["Value"] for item in json.loads(raw)["TagSet"]}
expected = {
    "Project": "5GCityVerse",
    "Environment": environment,
    "Terraform": "bootstrap",
    "DataClass": "terraform-state",
    "ManagedFor": "5gcityverse-main-root",
}
mismatch = {key: {"expected": value, "actual": actual.get(key)} for key, value in expected.items() if actual.get(key) != value}
if mismatch:
    raise SystemExit(f"Existing state bucket tag contract mismatch: {json.dumps(mismatch, sort_keys=True)}")
PY

  bootstrap_import_if_missing() {
    local address="$1"
    local import_id="$2"
    if ! terraform -chdir="$BOOTSTRAP_DIR" state show "$address" >/dev/null 2>&1; then
      log "Recovering bootstrap state address ${address}"
      terraform -chdir="$BOOTSTRAP_DIR" import "${BOOTSTRAP_TF_ARGS[@]}" "$address" "$import_id"
    fi
  }

  STATE_KMS_ALIAS="alias/5gcityverse-tfstate-$(printf '%s' "$STATE_BUCKET_NAME" | sha256sum | cut -c1-12)"
  STATE_KMS_KEY_ID="$(aws_cli kms list-aliases \
    --query "Aliases[?AliasName=='${STATE_KMS_ALIAS}'].TargetKeyId | [0]" --output text)"
  if [[ "$STATE_KMS_KEY_ID" != "None" ]]; then
    aws_cli kms describe-key --key-id "$STATE_KMS_KEY_ID" \
      --query 'KeyMetadata.[Arn,KeyState,Origin]' --output text >/dev/null
    bootstrap_import_if_missing aws_kms_key.terraform_state "$STATE_KMS_KEY_ID"
    bootstrap_import_if_missing aws_kms_alias.terraform_state "$STATE_KMS_ALIAS"
  fi
  bootstrap_import_if_missing aws_s3_bucket.terraform_state "$STATE_BUCKET_NAME"
  bootstrap_import_if_missing aws_s3_bucket_ownership_controls.terraform_state "$STATE_BUCKET_NAME"
  bootstrap_import_if_missing aws_s3_bucket_public_access_block.terraform_state "$STATE_BUCKET_NAME"
  bootstrap_import_if_missing aws_s3_bucket_versioning.terraform_state "$STATE_BUCKET_NAME"
  bootstrap_import_if_missing aws_s3_bucket_server_side_encryption_configuration.terraform_state "$STATE_BUCKET_NAME"
  bootstrap_import_if_missing aws_s3_bucket_policy.terraform_state "$STATE_BUCKET_NAME"
elif [[ "$STATE_BUCKET_OWNED" != "False" ]]; then
  echo "Could not determine whether the state bucket belongs to the authenticated account." >&2
  exit 1
fi

if [[ -f "${BOOTSTRAP_DIR}/terraform.tfstate" ]]; then
  cp "${BOOTSTRAP_DIR}/terraform.tfstate" "${PLAN_DIR}/bootstrap-terraform.tfstate.$(date -u +%Y%m%dT%H%M%SZ).pre-apply.backup"
fi
terraform_reviewed_apply "$BOOTSTRAP_DIR" state-bootstrap "${BOOTSTRAP_TF_ARGS[@]}"
if [[ -f "${BOOTSTRAP_DIR}/terraform.tfstate" ]]; then
  cp "${BOOTSTRAP_DIR}/terraform.tfstate" "${PLAN_DIR}/bootstrap-terraform.tfstate.$(date -u +%Y%m%dT%H%M%SZ).post-apply.backup"
fi

STATE_KMS_KEY_ARN="$(terraform -chdir="$BOOTSTRAP_DIR" output -raw state_kms_key_arn)"
if [[ ! "$STATE_KMS_KEY_ARN" =~ ^arn:aws:kms:${AWS_REGION}:${AWS_ACCOUNT_ID}:key/ ]]; then
  echo "Bootstrap returned an unexpected KMS key ARN; refusing backend initialization." >&2
  exit 1
fi

BACKEND_CONFIG="${PLAN_DIR}/backend-$(date -u +%Y%m%dT%H%M%SZ).hcl"
printf 'bucket = "%s"\nkey = "prod/main.tfstate"\nregion = "%s"\nencrypt = true\nkms_key_id = "%s"\nuse_lockfile = true\n' \
  "$STATE_BUCKET_NAME" "$AWS_REGION" "$STATE_KMS_KEY_ARN" >"$BACKEND_CONFIG"
if [[ -s "${TF_DIR}/terraform.tfstate" ]]; then
  cp "${TF_DIR}/terraform.tfstate" "${PLAN_DIR}/main-local-terraform.tfstate.$(date -u +%Y%m%dT%H%M%SZ).pre-migration.backup"
  terraform -chdir="$TF_DIR" init -migrate-state -force-copy -input=false -backend-config="$BACKEND_CONFIG"
else
  terraform -chdir="$TF_DIR" init -reconfigure -input=false -backend-config="$BACKEND_CONFIG"
fi
terraform -chdir="$TF_DIR" fmt -check -recursive
terraform -chdir="$TF_DIR" validate

if [[ -z "${EKS_PUBLIC_ACCESS_CIDRS_JSON:-}" ]]; then
  echo "EKS_PUBLIC_ACCESS_CIDRS_JSON is required (for example: [\"203.0.113.10/32\"])." >&2
  echo "The EKS API is private by default and will not be exposed to 0.0.0.0/0." >&2
  exit 1
fi
if [[ -z "${EKS_DEPLOYER_PRINCIPAL_ARN:-}" ]]; then
  echo "EKS_DEPLOYER_PRINCIPAL_ARN is required; use a dedicated short-lived deployment role ARN." >&2
  exit 1
fi
if [[ -z "${FREE5GC_WEBUI_PASSWORD:-}" ]]; then
  echo "FREE5GC_WEBUI_PASSWORD must be supplied out-of-band; no deployable default exists." >&2
  exit 1
fi
if [[ "${TF_VAR_api_auth_enabled:-true}" == "true" && -z "${CITYVERSE_API_TOKEN:-}" ]]; then
  echo "CITYVERSE_API_TOKEN is required while API authorization is enabled." >&2
  exit 1
fi
TF_NETWORK_ARGS=(
  "-var=eks_public_access_cidrs=${EKS_PUBLIC_ACCESS_CIDRS_JSON}"
  "-var=eks_deployer_principal_arn=${EKS_DEPLOYER_PRINCIPAL_ARN}"
)

CURRENT_FREE5GC_WEBUI_URL=""
CURRENT_NEF_BASE_URL=""
if EKS_CLUSTER_NAME="$(tf_output eks_cluster_name 2>/dev/null)" && [[ -n "$EKS_CLUSTER_NAME" ]]; then
  update_kubeconfig || true
  CURRENT_FREE5GC_WEBUI_URL="$(free5gc_webui_url 2>/dev/null || true)"
  CURRENT_NEF_BASE_URL="$(free5gc_nef_url 2>/dev/null || true)"
fi
INITIAL_ENDPOINT_ARGS=()
if [[ -n "$CURRENT_FREE5GC_WEBUI_URL" ]]; then
  log "Preserving current free5GC WebUI URL during initial Terraform apply: ${CURRENT_FREE5GC_WEBUI_URL}"
  INITIAL_ENDPOINT_ARGS+=("-var=free5gc_webui_url=${CURRENT_FREE5GC_WEBUI_URL}")
fi
if [[ -n "$CURRENT_NEF_BASE_URL" ]]; then
  log "Preserving current private NEF URL during initial Terraform apply."
  INITIAL_ENDPOINT_ARGS+=("-var=nef_base_url=${CURRENT_NEF_BASE_URL}")
fi
terraform_reviewed_apply "$TF_DIR" initial "${TF_NETWORK_ARGS[@]}" "${INITIAL_ENDPOINT_ARGS[@]}"

# ECR repositories are destroyed with the environment. Re-establish the
# reviewed immutable images from this deployment's Terraform outputs before
# any Kubernetes workload or Lambda environment can reference their digests.
SMF_QER_IMAGE_REPOSITORY="$(tf_output smf_qer_actuator_ecr_repository_url)"
UERANSIM_IMAGE_REPOSITORY="$(tf_output ueransim_ecr_repository_url)"
export SMF_QER_IMAGE_REPOSITORY UERANSIM_IMAGE_REPOSITORY
ensure_ecr_image_digest \
  "$SMF_QER_IMAGE_REPOSITORY" "$SMF_QER_IMAGE_DIGEST" \
  "$ROOT_DIR/custom-smf/Dockerfile" "$ROOT_DIR/custom-smf"
ensure_ecr_image_digest \
  "$UERANSIM_IMAGE_REPOSITORY" "$UERANSIM_IMAGE_DIGEST" \
  "$ROOT_DIR/custom-ueransim/Dockerfile" "$ROOT_DIR/custom-ueransim"

if [[ "${TF_VAR_api_auth_enabled:-true}" == "true" ]]; then
  API_SECRET_ARN="$(tf_output api_access_secret_arn)"
  python3 -c 'import json,os; print(json.dumps({"token": os.environ["CITYVERSE_API_TOKEN"]}))' |
    aws_cli secretsmanager put-secret-value --secret-id "$API_SECRET_ARN" --secret-string file:///dev/stdin >/dev/null
fi

SMF_QER_ACTUATOR_TOKEN="${SMF_QER_ACTUATOR_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export SMF_QER_ACTUATOR_TOKEN
SMF_QER_SECRET_ARN="$(tf_output smf_qer_actuator_secret_arn)"
python3 -c 'import json,os; print(json.dumps({"token": os.environ["SMF_QER_ACTUATOR_TOKEN"]}))' |
  aws_cli secretsmanager put-secret-value --secret-id "$SMF_QER_SECRET_ARN" --secret-string file:///dev/stdin >/dev/null

##################################################################
######## Deploy free5GC and UERANSIM to the EKS cluster ##########
##################################################################

EKS_CLUSTER_NAME="$(tf_output eks_cluster_name)"

update_kubeconfig
sync_free5gc_chart
install_multus
install_gtp5g
install_free5gc
install_gtp5g_metrics_exporter
install_real_simulation_assets

FREE5GC_WEBUI_URL="$(free5gc_webui_url)"
if [[ -z "$FREE5GC_WEBUI_URL" ]]; then
  echo "Could not resolve free5GC WebUI LoadBalancer URL." >&2
  exit 1
fi

log "Rotating and verifying the free5GC WebUI administrator password"
WEBUI_SECRET_ARN="$(tf_output free5gc_webui_secret_arn)"
if [[ -z "${FREE5GC_WEBUI_CURRENT_PASSWORD:-}" ]]; then
  STORED_WEBUI_SECRET="$(aws_cli secretsmanager get-secret-value \
    --secret-id "$WEBUI_SECRET_ARN" --query SecretString --output text 2>/dev/null || true)"
  if [[ -n "$STORED_WEBUI_SECRET" && "$STORED_WEBUI_SECRET" != "None" ]]; then
    FREE5GC_WEBUI_CURRENT_PASSWORD="$(STORED_WEBUI_SECRET="$STORED_WEBUI_SECRET" python3 -c \
      'import json, os; print(json.loads(os.environ["STORED_WEBUI_SECRET"]).get("password", ""))')"
    export FREE5GC_WEBUI_CURRENT_PASSWORD
  fi
fi
(
  ROTATE_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  ROTATE_FORWARD_LOG="$(mktemp)"
  kubectl -n "$FREE5GC_NAMESPACE" port-forward --address 127.0.0.1 \
    service/webui-service "${ROTATE_PORT}:5000" >"$ROTATE_FORWARD_LOG" 2>&1 &
  ROTATE_FORWARD_PID=$!
  trap 'kill "$ROTATE_FORWARD_PID" 2>/dev/null || true; wait "$ROTATE_FORWARD_PID" 2>/dev/null || true; rm -f "$ROTATE_FORWARD_LOG"' EXIT
  retry 30 2 curl --fail --silent --show-error "http://127.0.0.1:${ROTATE_PORT}/" >/dev/null
  FREE5GC_WEBUI_URL="http://127.0.0.1:${ROTATE_PORT}" \
  FREE5GC_WEBUI_CURRENT_PASSWORD="${FREE5GC_WEBUI_CURRENT_PASSWORD:-free5gc}" \
    python3 "$ROOT_DIR/scripts/rotate-free5gc-webui-password.py"
)
python3 -c 'import json,os; print(json.dumps({"username": os.environ.get("FREE5GC_WEBUI_USERNAME", "admin"), "password": os.environ["FREE5GC_WEBUI_PASSWORD"]}))' |
  aws_cli secretsmanager put-secret-value --secret-id "$WEBUI_SECRET_ARN" --secret-string file:///dev/stdin >/dev/null

NEF_BASE_URL="$(free5gc_nef_url)"
if [[ -z "$NEF_BASE_URL" || "$NEF_BASE_URL" == *cluster.local* ]]; then
  echo "Could not resolve a private NEF NLB URL; refusing to configure NEF Lambdas." >&2
  exit 1
fi

log "Updating Lambda service URLs with the resolved WebUI and private NEF endpoints"
terraform_reviewed_apply "$TF_DIR" service-urls "${TF_NETWORK_ARGS[@]}" \
  -var="free5gc_webui_url=${FREE5GC_WEBUI_URL}" \
  -var="nef_base_url=${NEF_BASE_URL}"
TF_FREE5GC_WEBUI_URL="$(tf_output free5gc_webui_url)"
TF_NEF_BASE_URL="$(tf_output nef_base_url)"
if [[ "$TF_FREE5GC_WEBUI_URL" != "$FREE5GC_WEBUI_URL" || "$TF_NEF_BASE_URL" != "$NEF_BASE_URL" ]]; then
  echo "Terraform output did not preserve the Kubernetes-discovered private service URLs." >&2
  echo "WebUI expected=${FREE5GC_WEBUI_URL} actual=${TF_FREE5GC_WEBUI_URL}" >&2
  echo "NEF expected=${NEF_BASE_URL} actual=${TF_NEF_BASE_URL}" >&2
  exit 1
fi
BACKEND_LAMBDA_NAME="$(tf_output backend_lambda_name)"
ACTUAL_FREE5GC_WEBUI_URL="$(aws_cli lambda get-function-configuration --function-name "$BACKEND_LAMBDA_NAME" --query 'Environment.Variables.FREE5GC_WEBUI_URL' --output text)"
NEF_LAMBDA_NAMES=(
  "$(tf_output nef_pfd_lambda_name)"
  "$(tf_output nef_qos_lambda_name)"
  "$(tf_output nef_traffic_influence_lambda_name)"
)
nef_lambda_urls_match() {
  local function_name actual_url
  for function_name in "${NEF_LAMBDA_NAMES[@]}"; do
    actual_url="$(aws_cli lambda get-function-configuration --function-name "$function_name" --query 'Environment.Variables.NEF_BASE_URL' --output text)"
    [[ "$actual_url" == "$NEF_BASE_URL" ]] || return 1
  done
}
if [[ "$ACTUAL_FREE5GC_WEBUI_URL" != "$FREE5GC_WEBUI_URL" ]] || ! nef_lambda_urls_match; then
  log "Retrying Lambda service URL synchronization"
  terraform_reviewed_apply "$TF_DIR" service-urls-retry "${TF_NETWORK_ARGS[@]}" \
    -var="free5gc_webui_url=${FREE5GC_WEBUI_URL}" \
    -var="nef_base_url=${NEF_BASE_URL}"
  ACTUAL_FREE5GC_WEBUI_URL="$(aws_cli lambda get-function-configuration --function-name "$BACKEND_LAMBDA_NAME" --query 'Environment.Variables.FREE5GC_WEBUI_URL' --output text)"
fi
if [[ "$ACTUAL_FREE5GC_WEBUI_URL" != "$FREE5GC_WEBUI_URL" ]]; then
  echo "Backend Lambda FREE5GC_WEBUI_URL mismatch: expected ${FREE5GC_WEBUI_URL}, got ${ACTUAL_FREE5GC_WEBUI_URL}" >&2
  exit 1
fi
if ! nef_lambda_urls_match; then
  echo "One or more NEF Lambdas did not receive the reviewed private NEF URL." >&2
  exit 1
fi

API_URL="$(tf_output api_gateway_url)"
WS_URL="$(tf_output ws_endpoint)"
FRONTEND_BUCKET="$(tf_output frontend_bucket)"
CLOUDFRONT_ID="$(tf_output cloudfront_distribution_id)"
FRONTEND_URL="$(tf_output frontend_url)"

# The WebUI is intentionally private. Seed through an authenticated kubectl
# port-forward instead of trying to reach its internal NLB from the operator's
# workstation or reopening port 5000 publicly.
(
  SEED_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  SEED_FORWARD_LOG="$(mktemp)"
  kubectl -n "$FREE5GC_NAMESPACE" port-forward --address 127.0.0.1 \
    service/webui-service "${SEED_PORT}:5000" >"$SEED_FORWARD_LOG" 2>&1 &
  SEED_FORWARD_PID=$!
  trap 'kill "$SEED_FORWARD_PID" 2>/dev/null || true; wait "$SEED_FORWARD_PID" 2>/dev/null || true; rm -f "$SEED_FORWARD_LOG"' EXIT
  retry 30 2 curl --fail --silent --show-error "http://127.0.0.1:${SEED_PORT}/" >/dev/null
  FREE5GC_WEBUI_URL="http://127.0.0.1:${SEED_PORT}" \
    python3 "$ROOT_DIR/scripts/seed-subscribers.py"
)

# UERANSIM's UE process exits when its SUPI is absent. Seed all subscriber
# profiles before Helm waits for UE readiness, otherwise a clean deployment
# deadlocks waiting for a UE that cannot authenticate yet.
install_ueransim

# Subscriber upserts reset authentication sequence state. Recycle RAN/UE only
# after seeding so UERANSIM authenticates against the new SQN and establishes a
# fresh PDU bearer before the resident UE-TUN verification gate runs.
log "Re-establishing PFCP, gNB, and UE sessions after subscriber synchronization"
for upf in \
  upf-embb-free5gc-upf-upf-embb \
  upf-urllc-free5gc-upf-upf-urllc \
  upf-mmtc-free5gc-upf-upf-mmtc \
  upf-v2x-free5gc-upf-upf-v2x; do
  kubectl -n "$FREE5GC_NAMESPACE" rollout restart "deployment/${upf}"
  kubectl -n "$FREE5GC_NAMESPACE" rollout status "deployment/${upf}" --timeout=180s
done
sleep 3
kubectl -n "$FREE5GC_NAMESPACE" rollout restart deployment/free5gc-free5gc-smf-smf
sleep 3
kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/free5gc-free5gc-smf-smf --timeout=180s
kubectl -n "$FREE5GC_NAMESPACE" rollout restart deployment/ueransim-city-gnb
sleep 3
kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/ueransim-city-gnb --timeout=180s
for ue_deployment in ueransim-city-ue ueransim-city-mmtc; do
  if kubectl -n "$FREE5GC_NAMESPACE" get deployment "$ue_deployment" >/dev/null 2>&1; then
    kubectl -n "$FREE5GC_NAMESPACE" rollout restart "deployment/${ue_deployment}"
    sleep 3
    kubectl -n "$FREE5GC_NAMESPACE" rollout status "deployment/${ue_deployment}" --timeout=180s
  else
    log "Skipping optional UE deployment ${ue_deployment}; it is created on demand by scenario reconciliation"
  fi
done
kubectl -n "$FREE5GC_NAMESPACE" rollout restart deployment/iperf3-server
sleep 3
kubectl -n "$FREE5GC_NAMESPACE" rollout status deployment/iperf3-server --timeout=180s
API_URL="${API_URL%/}" python3 "$ROOT_DIR/scripts/verify-baseline-city.py"

frontend_npm_ci
(
  cd "$ROOT_DIR/frontend"
  # Inline VITE_* env vars proved unreliable under WSL (2026-07-05: stale default
  # endpoints got baked into the bundle). Write .env.production.local so vite
  # picks the current endpoints up deterministically on every deploy.
  printf 'VITE_API_URL=%s\nVITE_WS_URL=%s\nVITE_FREE5GC_WEBUI_URL=%s\n' "${API_URL%/}" "$WS_URL" "$FREE5GC_WEBUI_URL" > .env.production.local
  VITE_API_URL="${API_URL%/}" VITE_WS_URL="$WS_URL" VITE_FREE5GC_WEBUI_URL="$FREE5GC_WEBUI_URL" npm run build
  BUILT_BUNDLE="$(ls dist/assets/index-*.js | head -1)"
  if ! grep -q "$(printf '%s' "${API_URL%/}" | sed 's#https://##;s#/.*##')" "$BUILT_BUNDLE"; then
    echo "Frontend bundle does not contain the live API endpoint; VITE env injection failed." >&2
    exit 1
  fi
  if ! grep -q "$(printf '%s' "$FREE5GC_WEBUI_URL" | sed 's#https\?://##;s#/.*##')" "$BUILT_BUNDLE"; then
    echo "Frontend bundle does not contain the free5GC WebUI endpoint; VITE env injection failed." >&2
    exit 1
  fi
)

# The shared bucket also stores the pinned kubectl binary used by the private
# validation runner. Excluding that prefix keeps frontend garbage collection
# from deleting deployment tooling that Terraform owns.
aws_cli s3 sync "$ROOT_DIR/frontend/dist" "s3://${FRONTEND_BUCKET}" --delete --exclude 'validation-tools/*'
aws_cli cloudfront create-invalidation --distribution-id "$CLOUDFRONT_ID" --paths '/*' >/dev/null

cat <<EOF
Deployment complete.
Frontend: ${FRONTEND_URL}
API: ${API_URL}
WebSocket: ${WS_URL}
free5GC WebUI: ${FREE5GC_WEBUI_URL}
free5GC login credentials: stored out-of-band in Secrets Manager
UERANSIM release: ${UERANSIM_RELEASE}
EOF
