#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"

if [[ -z "$SCENARIO" ]]; then
  echo "Usage: $0 <concert|medical|er_surge|typhoon|iot_surge|accident>" >&2
  exit 1
fi

case "$SCENARIO" in
  er_surge) SCENARIO="medical" ;;
  iot) SCENARIO="iot_surge" ;;
esac

echo "=== 5GCityVerse scenario: $SCENARIO ==="

case "$SCENARIO" in
  concert|medical|accident)
    echo "[1/3] Switching the primary UERANSIM UE"
    "$ROOT_DIR/scripts/switch-ue-slice.sh" "$SCENARIO"
    ;;
  typhoon)
    echo "[1/3] Starting Typhoon UERANSIM deployment (3 UE)"
    kubectl apply -f "$ROOT_DIR/k8s/ue-config-typhoon.yaml"
    kubectl apply -f "$ROOT_DIR/k8s/ueransim/deployments/ueransim-typhoon.yaml"
    kubectl rollout status deployment/ueransim-typhoon -n "$NAMESPACE" --timeout=180s
    ;;
  iot_surge)
    echo "[1/3] Starting IoT UERANSIM deployment (50 UE)"
    kubectl apply -f "$ROOT_DIR/k8s/ue-config-mmtc.yaml"
    kubectl apply -f "$ROOT_DIR/k8s/ueransim/deployments/ueransim-iot.yaml"
    kubectl rollout status deployment/ueransim-iot -n "$NAMESPACE" --timeout=180s
    ;;
  *)
    echo "Unknown scenario: $SCENARIO" >&2
    exit 1
    ;;
esac

echo "[2/3] Launching matching iperf3 traffic"
"$ROOT_DIR/scripts/trigger-iperf3.sh" "$SCENARIO"

echo "[3/3] Current scenario resources"
kubectl get pod,job -n "$NAMESPACE" -l app.kubernetes.io/part-of=5gcityverse 2>/dev/null || true
