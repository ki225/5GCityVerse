#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"
UE_DEPLOYMENT="${UERANSIM_UE_DEPLOYMENT:-ueransim-city-ue}"

if [[ -z "$SCENARIO" ]]; then
  echo "Usage: $0 <concert|medical|typhoon|iot_surge|accident>" >&2
  exit 1
fi

case "$SCENARIO" in
  concert)
    CONFIG_FILE="$ROOT_DIR/k8s/ue-config/embb.yaml"
    CONFIG_MAP="ueransim-ue-config-embb"
    SLICE_LABEL="eMBB SST=1 SD=000001"
    ;;
  medical|er_surge)
    CONFIG_FILE="$ROOT_DIR/k8s/ue-config/urllc.yaml"
    CONFIG_MAP="ueransim-ue-config-urllc"
    SLICE_LABEL="URLLC SST=2 SD=000002"
    ;;
  typhoon)
    CONFIG_FILE="$ROOT_DIR/k8s/ue-config/typhoon.yaml"
    CONFIG_MAP="ueransim-ue-config-typhoon"
    SLICE_LABEL="URLLC SST=2 SD=000003"
    ;;
  iot_surge|iot)
    CONFIG_FILE="$ROOT_DIR/k8s/ue-config/mmtc.yaml"
    CONFIG_MAP="ueransim-ue-config-mmtc"
    SLICE_LABEL="mMTC SST=3 SD=000004"
    ;;
  accident)
    CONFIG_FILE="$ROOT_DIR/k8s/ue-config/v2x.yaml"
    CONFIG_MAP="ueransim-ue-config-v2x"
    SLICE_LABEL="V2X SST=4 SD=000005"
    ;;
  *)
    echo "Unknown scenario: $SCENARIO" >&2
    exit 1
    ;;
esac

echo "[1/4] Applying UE ConfigMap: $CONFIG_MAP"
kubectl apply -f "$CONFIG_FILE"

echo "[2/4] Patching $UE_DEPLOYMENT to use $CONFIG_MAP"
kubectl patch deployment "$UE_DEPLOYMENT" -n "$NAMESPACE" --type='strategic' -p "{
  \"spec\": {
    \"template\": {
      \"metadata\": {
        \"annotations\": {
          \"5gcityverse.io/slice-switched-at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
        }
      },
      \"spec\": {
        \"volumes\": [{
          \"name\": \"ue-volume\",
          \"configMap\": {
            \"name\": \"$CONFIG_MAP\",
            \"items\": [{\"key\": \"ue-config.yaml\", \"path\": \"ue-config.yaml\"}]
          }
        }]
      }
    }
  }
}"

echo "[3/4] Waiting for UE rollout"
kubectl rollout status deployment/"$UE_DEPLOYMENT" -n "$NAMESPACE" --timeout=120s

echo "[4/4] UE slice switched to $SLICE_LABEL"
UE_POD="$(kubectl get pod -n "$NAMESPACE" -l app.kubernetes.io/name=ueransim -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="ue")].metadata.name}' 2>/dev/null | awk '{print $1}')"
if [[ -n "$UE_POD" ]]; then
  kubectl exec -n "$NAMESPACE" "$UE_POD" -- ip addr show uesimtun0 2>/dev/null || true
fi
