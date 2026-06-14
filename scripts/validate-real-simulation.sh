#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${FREE5GC_NAMESPACE:-free5gc}"
SCENARIO="${1:-concert}"

echo "=== UE slice config ==="
"$(dirname "$0")/switch-ue-slice.sh" "$SCENARIO"

echo ""
echo "=== iperf3 traffic job ==="
"$(dirname "$0")/trigger-iperf3.sh" "$SCENARIO"

echo ""
echo "=== Current jobs ==="
kubectl get job -n "$NAMESPACE" -l app.kubernetes.io/component=iperf3

echo ""
echo "=== UE tunnel ==="
UE_POD="$(kubectl get pod -n "$NAMESPACE" -l app.kubernetes.io/name=ueransim -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="ue")].metadata.name}' 2>/dev/null | awk '{print $1}')"
if [[ -n "$UE_POD" ]]; then
  kubectl exec -n "$NAMESPACE" "$UE_POD" -- ip addr show uesimtun0 2>/dev/null || true
else
  echo "UE pod not found"
fi

echo ""
echo "=== Prometheus sample ==="
PROM_SVC="$(kubectl get svc -n "$NAMESPACE" prometheus-server -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
if [[ -n "$PROM_SVC" ]]; then
  curl -fsS "http://$PROM_SVC/api/v1/query?query=free5gc_upf_bytes_total" | head -c 1000 || true
  echo ""
else
  echo "prometheus-server service not found in namespace $NAMESPACE"
fi
