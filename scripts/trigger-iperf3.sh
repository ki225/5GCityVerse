#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"

if [[ -z "$SCENARIO" ]]; then
  echo "Usage: $0 <concert|medical|typhoon|iot_surge|accident>" >&2
  exit 1
fi

case "$SCENARIO" in
  er_surge) SCENARIO="medical" ;;
  iot) SCENARIO="iot_surge" ;;
esac

JOB_SUFFIX="${SCENARIO//_/-}"
JOB_FILE="$ROOT_DIR/k8s/iperf3-jobs/${JOB_SUFFIX}.yaml"

if [[ ! -f "$JOB_FILE" ]]; then
  echo "No iperf3 job manifest for scenario: $SCENARIO ($JOB_FILE)" >&2
  exit 1
fi

echo "[1/3] Ensuring iperf3 server is running"
kubectl apply -f "$ROOT_DIR/k8s/iperf3-server.yaml"
kubectl rollout status deployment/iperf3-server -n "$NAMESPACE" --timeout=120s

echo "[2/3] Recreating iperf3 job: iperf3-$JOB_SUFFIX"
kubectl delete job "iperf3-$JOB_SUFFIX" -n "$NAMESPACE" --ignore-not-found
kubectl apply -f "$JOB_FILE"

echo "[3/3] Job launched"
echo "Monitor with:"
echo "  kubectl logs -n $NAMESPACE job/iperf3-$JOB_SUFFIX -f"
