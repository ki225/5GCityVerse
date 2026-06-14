# 5GCityVerse — 真實流量行為模擬開發文件

> **目標：** 同一組 UE，因應不同事件切換 Slice Profile，展示真實的 QoS 差異（頻寬、延遲、封包特性），而非假造數值。

---

## 目錄

1. [架構總覽](#1-架構總覽)
2. [核心概念：為何不是增加 UE 數量](#2-核心概念為何不是增加-ue-數量)
3. [各場景流量行為定義](#3-各場景流量行為定義)
4. [實作階段規劃](#4-實作階段規劃)
5. [Phase 1：UERANSIM 動態 Slice 切換](#5-phase-1ueransim-動態-slice-切換)
6. [Phase 2：iperf3 真實流量注入](#6-phase-2iperf3-真實流量注入)
7. [Phase 3：Prometheus 真實指標串接](#7-phase-3prometheus-真實指標串接)
8. [Phase 4：Lambda 事件整合](#8-phase-4lambda-事件整合)
9. [Phase 5：Dashboard 替換假資料](#9-phase-5dashboard-替換假資料)
10. [驗證方法](#10-驗證方法)
11. [常見問題](#11-常見問題)

---

## 1. 架構總覽

### 目前架構（Before）

```
事件按鈕
  └─→ Lambda
        ├─→ free5GC WebUI (寫 subscriber)
        └─→ WebSocket 廣播假 metrics 給前端
```

問題：UPF 完全沒有流量，所有數字都是後端產生的模擬值。

---

### 目標架構（After）

```
事件按鈕
  └─→ Lambda
        ├─→ free5GC WebUI (修改 QoS Profile / NSSAI)
        ├─→ K8s Job：觸發 UERANSIM UE PDU Session Re-establishment
        ├─→ K8s Job：iperf3 以對應場景參數打流量
        └─→ WebSocket 廣播（數據來自 Prometheus 真實指標）

Prometheus
  └─→ 抓 free5GC UPF metrics（真實 Throughput / GTP / Latency）
  └─→ 抓 K8s metrics-server（AMF/SMF/UPF CPU）

Dashboard
  └─→ 每 5 秒向 /api/metrics 取得 Prometheus 真實數據
```

---

## 2. 核心概念：為何不是增加 UE 數量

5G 三大 Slice 的本質差異在於**流量特質**，不在於 UE 數量：

| Slice | SST | 真正重要的是 | UE 數量重要嗎 |
|-------|-----|------------|-------------|
| eMBB  | 1   | 高頻寬 AMBR、5QI=9 | 次要 |
| URLLC | 2   | 低延遲保證、GBR、5QI=1 | 不重要 |
| mMTC  | 3   | 大量小封包並發連線 | **是，這個才需要多 UE** |

因此本方案的核心是：

- **同一個 UE**，在不同事件下切換到不同 Slice
- **iperf3** 以不同參數模擬各場景的流量特質
- **Prometheus** 抓 UPF 真實數據，不再用假值

---

## 3. 各場景流量行為定義

### 3.1 Concert（演唱會直播）→ eMBB

```
Slice:  SST=1, SD=000001
5QI:    9  (Non-GBR, large data)
AMBR:   UL 500 Mbps / DL 1 Gbps
iperf3: -u -b 800M -t 300 -l 1400
展示重點: UPF Throughput 大幅攀升
```

**流量解釋：** 8 萬人同時觀看 1080p 直播，單一 UPF 需要承擔上行串流。  
用大封包、高頻寬 UDP 模擬影音串流特性。

---

### 3.2 ER Surge（醫療緊急）→ URLLC

```
Slice:  SST=2, SD=000002
5QI:    1  (GBR, Delay-critical)
GBR:    UL 10 Mbps / DL 10 Mbps (保證)
PDB:    100ms (Packet Delay Budget)
iperf3: -u -b 10M -t 300 -l 100 --trip-times
展示重點: Latency 低且穩定，Jitter 小
```

**流量解釋：** 遠端手術、救護車即時影像，小封包但對延遲極度敏感。  
用小封包 UDP 模擬，並觀察 RTT / Jitter。

---

### 3.3 Typhoon（颱風緊急通訊）→ URLLC

```
Slice:  SST=2, SD=000003
5QI:    2  (GBR, Mission-critical voice)
GBR:    UL 5 Mbps / DL 5 Mbps
iperf3: -u -b 5M -t 300 -l 200
展示重點: 在網路壓力下仍保持 GBR 頻寬
```

---

### 3.4 IoT Surge → mMTC

```
Slice:  SST=3, SD=000004
5QI:    79 (Non-GBR, small data)
AMBR:   UL 1 Mbps / DL 1 Mbps (每個 UE 很小)
iperf3: 多個並發連線，-b 500K -P 50
展示重點: Session 數量多，單一 UE 流量小
```

**這個場景** 才需要搭配多 UE（或多 iperf3 並發 stream）來展示連線數。

---

### 3.5 Accident（V2X 事故）→ V2X / eMBB

```
Slice:  SST=4, SD=000005
5QI:    75 (Mission-critical V2X)
iperf3: -u -b 50M -t 60  (短暫爆發)
展示重點: 短暫高頻寬爆發後快速釋放
```

---

## 4. 實作階段規劃

```
Phase 1（2天）: UERANSIM 動態 Slice 切換
Phase 2（1天）: iperf3 K8s Job 注入真實流量
Phase 3（1天）: Prometheus 串接 free5GC UPF 指標
Phase 4（1天）: Lambda 整合以上三項
Phase 5（1天）: Dashboard 替換假資料為真實 Prometheus 數據
```

總計約 **6 個工作天**。

---

## 5. Phase 1：UERANSIM 動態 Slice 切換

### 5.1 目前問題

`k8s/ueransim-eks-values.yaml` 是靜態設定，Slice 不會變動：

```yaml
# 目前
ues:
  count: 1
  initialMSISDN: "0000000001"
  sessions:
    - type: "IPv4"
      apn: "internet"
      slice:
        sst: 0x01
        sd: 0x000001
```

---

### 5.2 解法：為每個 Slice 準備獨立 ConfigMap

在 `k8s/` 目錄下建立各場景的 UE ConfigMap：

**`k8s/ue-config-embb.yaml`**（Concert / V2X）

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ueransim-ue-config-embb
  namespace: free5gc
data:
  ue.yaml: |
    supi: "imsi-208930000000001"
    mcc: "208"
    mnc: "93"
    key: "8baf473f2f8fd09487cccbd7097c6862"
    op: "8e27b6af0e692e750f32667a3b14605d"
    opType: "OP"
    sessions:
      - type: "IPv4"
        apn: "internet"
        slice:
          sst: 1
          sd: "000001"
    requestedNssai:
      - sst: 1
        sd: "000001"
    defaultNssai:
      - sst: 1
        sd: "000001"
```

**`k8s/ue-config-urllc.yaml`**（ER Surge / Typhoon）

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ueransim-ue-config-urllc
  namespace: free5gc
data:
  ue.yaml: |
    supi: "imsi-208930000000001"
    mcc: "208"
    mnc: "93"
    key: "8baf473f2f8fd09487cccbd7097c6862"
    op: "8e27b6af0e692e750f32667a3b14605d"
    opType: "OP"
    sessions:
      - type: "IPv4"
        apn: "internet"
        slice:
          sst: 2
          sd: "000002"
    requestedNssai:
      - sst: 2
        sd: "000002"
    defaultNssai:
      - sst: 2
        sd: "000002"
```

**`k8s/ue-config-mmtc.yaml`**（IoT Surge）

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ueransim-ue-config-mmtc
  namespace: free5gc
data:
  ue.yaml: |
    supi: "imsi-208930000000001"
    mcc: "208"
    mnc: "93"
    key: "8baf473f2f8fd09487cccbd7097c6862"
    op: "8e27b6af0e692e750f32667a3b14605d"
    opType: "OP"
    sessions:
      - type: "IPv4"
        apn: "internet"
        slice:
          sst: 3
          sd: "000004"
    requestedNssai:
      - sst: 3
        sd: "000004"
    defaultNssai:
      - sst: 3
        sd: "000004"
```

---

### 5.3 切換 Script

建立 `scripts/switch-ue-slice.sh`：

```bash
#!/bin/bash
# Usage: ./switch-ue-slice.sh <concert|medical|typhoon|iot|accident>

set -e

SCENARIO=$1
NAMESPACE="free5gc"
UE_DEPLOYMENT="ueransim-ue"

case "$SCENARIO" in
  concert|accident)
    CONFIG_MAP="ueransim-ue-config-embb"
    SLICE_LABEL="embb"
    ;;
  medical|er_surge|typhoon)
    CONFIG_MAP="ueransim-ue-config-urllc"
    SLICE_LABEL="urllc"
    ;;
  iot_surge)
    CONFIG_MAP="ueransim-ue-config-mmtc"
    SLICE_LABEL="mmtc"
    ;;
  *)
    echo "Unknown scenario: $SCENARIO"
    exit 1
    ;;
esac

echo "[1/4] Applying UE ConfigMap: $CONFIG_MAP"
kubectl apply -f "k8s/ue-config-${SLICE_LABEL}.yaml"

echo "[2/4] Updating UE Deployment to use new ConfigMap"
kubectl patch deployment "$UE_DEPLOYMENT" -n "$NAMESPACE" \
  --type='json' \
  -p="[{\"op\": \"replace\", \"path\": \"/spec/template/spec/volumes/0/configMap/name\", \"value\": \"$CONFIG_MAP\"}]"

echo "[3/4] Waiting for UE Pod restart"
kubectl rollout restart deployment/"$UE_DEPLOYMENT" -n "$NAMESPACE"
kubectl rollout status deployment/"$UE_DEPLOYMENT" -n "$NAMESPACE" --timeout=60s

echo "[4/4] UE slice switched to: $SLICE_LABEL"

# 確認 UE 已 attach
sleep 5
UE_POD=$(kubectl get pod -n "$NAMESPACE" -l app=ueransim-ue -o jsonpath='{.items[0].metadata.name}')
echo "=== UE Status ==="
kubectl exec -n "$NAMESPACE" "$UE_POD" -- nr-cli imsi-208930000000001 --exec "status" 2>/dev/null || true
```

---

### 5.4 free5GC subscriber QoS 同步

事件觸發時，除了已有的 subscriber 寫入，需確保 NSSAI 與 UE 端設定一致。

在 `backend/aws-app/index.py` 的 subscriber 建立函式中，確認 `singleNssai` 欄位正確：

```python
# 確認各場景 subscriber profile 的 NSSAI 設定
SLICE_PROFILES = {
    "concert": {
        "sst": 1, "sd": "000001",
        "5qi": 9,
        "ueAmbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"},
        "flowRules": [{"ipFilter": "*", "precedence": 128, "5qi": 9,
                       "gbrUL": "", "gbrDL": "", "mbrUL": "1 Gbps", "mbrDL": "1 Gbps"}]
    },
    "er_surge": {
        "sst": 2, "sd": "000002",
        "5qi": 1,
        "ueAmbr": {"uplink": "100 Mbps", "downlink": "100 Mbps"},
        "flowRules": [{"ipFilter": "*", "precedence": 128, "5qi": 1,
                       "gbrUL": "10 Mbps", "gbrDL": "10 Mbps",
                       "mbrUL": "10 Mbps", "mbrDL": "10 Mbps"}]
    },
    "typhoon": {
        "sst": 2, "sd": "000003",
        "5qi": 2,
        "ueAmbr": {"uplink": "50 Mbps", "downlink": "50 Mbps"},
        "flowRules": [{"ipFilter": "*", "precedence": 128, "5qi": 2,
                       "gbrUL": "5 Mbps", "gbrDL": "5 Mbps",
                       "mbrUL": "5 Mbps", "mbrDL": "5 Mbps"}]
    },
    "iot_surge": {
        "sst": 3, "sd": "000004",
        "5qi": 79,
        "ueAmbr": {"uplink": "10 Mbps", "downlink": "10 Mbps"},
        "flowRules": [{"ipFilter": "*", "precedence": 128, "5qi": 79,
                       "gbrUL": "", "gbrDL": "", "mbrUL": "1 Mbps", "mbrDL": "1 Mbps"}]
    },
    "accident": {
        "sst": 4, "sd": "000005",
        "5qi": 75,
        "ueAmbr": {"uplink": "200 Mbps", "downlink": "200 Mbps"},
        "flowRules": [{"ipFilter": "*", "precedence": 128, "5qi": 75,
                       "gbrUL": "", "gbrDL": "", "mbrUL": "200 Mbps", "mbrDL": "200 Mbps"}]
    },
}
```

---

### 5.5 驗證 Phase 1

```bash
# 觸發事件後，確認 UE 使用正確 Slice 註冊到 AMF
kubectl exec -n free5gc deploy/free5gc-amf -- \
  wget -qO- http://localhost:8000/api/registered-ue-context | python3 -m json.tool

# 確認 PDU Session 建立在正確 Slice 上
kubectl exec -n free5gc deploy/free5gc-smf -- \
  wget -qO- http://localhost:8000/api/smf-ue-info | python3 -m json.tool
```

預期結果：`singleNssai.sst` 對應到事件的 Slice。

---

## 6. Phase 2：iperf3 真實流量注入

### 6.1 部署 iperf3 Server

**`k8s/iperf3-server.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: iperf3-server
  namespace: free5gc
spec:
  replicas: 1
  selector:
    matchLabels:
      app: iperf3-server
  template:
    metadata:
      labels:
        app: iperf3-server
    spec:
      containers:
        - name: iperf3
          image: networkstatic/iperf3:latest
          command: ["iperf3", "-s", "-p", "5201"]
          ports:
            - containerPort: 5201
              protocol: TCP
            - containerPort: 5201
              protocol: UDP
---
apiVersion: v1
kind: Service
metadata:
  name: iperf3-server
  namespace: free5gc
spec:
  selector:
    app: iperf3-server
  ports:
    - name: iperf3
      port: 5201
      targetPort: 5201
  clusterIP: None  # Headless，讓 UE Pod 可透過 UPF Data Plane 連線
```

```bash
kubectl apply -f k8s/iperf3-server.yaml
```

---

### 6.2 各場景 iperf3 Job 定義

建立 `k8s/iperf3-jobs/` 目錄，各場景一個 Job：

**`k8s/iperf3-jobs/concert.yaml`**（eMBB 高頻寬）

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: iperf3-concert
  namespace: free5gc
spec:
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: iperf3-client
          image: networkstatic/iperf3:latest
          command:
            - iperf3
            - "-c"
            - "$(IPERF3_SERVER_IP)"
            - "-u"           # UDP（模擬影音串流）
            - "-b"
            - "800M"         # 800 Mbps 目標頻寬
            - "-t"
            - "120"          # 持續 2 分鐘
            - "-l"
            - "1400"         # 大封包（接近 MTU）
            - "--json"
          env:
            - name: IPERF3_SERVER_IP
              value: "iperf3-server.free5gc.svc.cluster.local"
```

**`k8s/iperf3-jobs/er-surge.yaml`**（URLLC 低延遲）

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: iperf3-er-surge
  namespace: free5gc
spec:
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: iperf3-client
          image: networkstatic/iperf3:latest
          command:
            - iperf3
            - "-c"
            - "iperf3-server.free5gc.svc.cluster.local"
            - "-u"
            - "-b"
            - "10M"          # 低頻寬，但穩定
            - "-t"
            - "120"
            - "-l"
            - "100"          # 小封包（模擬醫療指令/影像壓縮幀）
            - "--trip-times" # 量測 RTT
            - "--json"
```

**`k8s/iperf3-jobs/iot-surge.yaml`**（mMTC 多並發小流量）

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: iperf3-iot-surge
  namespace: free5gc
spec:
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: iperf3-client
          image: networkstatic/iperf3:latest
          command:
            - iperf3
            - "-c"
            - "iperf3-server.free5gc.svc.cluster.local"
            - "-u"
            - "-b"
            - "500K"         # 每個 stream 小頻寬
            - "-P"
            - "50"           # 50 個並發 stream（模擬 50 個 IoT 設備）
            - "-t"
            - "120"
            - "-l"
            - "64"           # 極小封包（IoT sensor data）
            - "--json"
```

**`k8s/iperf3-jobs/typhoon.yaml`**（URLLC 緊急通訊）

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: iperf3-typhoon
  namespace: free5gc
spec:
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: iperf3-client
          image: networkstatic/iperf3:latest
          command:
            - iperf3
            - "-c"
            - "iperf3-server.free5gc.svc.cluster.local"
            - "-u"
            - "-b"
            - "5M"
            - "-t"
            - "120"
            - "-l"
            - "200"
            - "--json"
```

---

### 6.3 Job 觸發函式

建立 `scripts/trigger-iperf3.sh`：

```bash
#!/bin/bash
# Usage: ./trigger-iperf3.sh <concert|er_surge|typhoon|iot_surge|accident>

SCENARIO=$1
NAMESPACE="free5gc"

# 清除上一個同名 Job
kubectl delete job "iperf3-${SCENARIO//_/-}" -n "$NAMESPACE" --ignore-not-found

echo "Launching iperf3 job for scenario: $SCENARIO"
kubectl apply -f "k8s/iperf3-jobs/${SCENARIO//_/-}.yaml"

echo "Job launched. Monitor with:"
echo "  kubectl logs -n $NAMESPACE job/iperf3-${SCENARIO//_/-} -f"
```

---

### 6.4 ⚠️ 重要：UPF Data Plane 路由確認

iperf3 流量必須真的經過 UPF 的 GTP Tunnel 才有意義。

**確認步驟：**

```bash
# 1. 取得 UE Pod 名稱
UE_POD=$(kubectl get pod -n free5gc -l app=ueransim-ue -o jsonpath='{.items[0].metadata.name}')

# 2. 確認 UE 有取得 UPF 分配的 IP（應是 10.1.0.x 段）
kubectl exec -n free5gc "$UE_POD" -- ip addr show uesimtun0

# 3. 確認 UE Pod 可以 ping 到 iperf3 server（透過 uesimtun0）
kubectl exec -n free5gc "$UE_POD" -- ping -I uesimtun0 -c 3 <iperf3-server-pod-ip>

# 4. 在 UPF Pod 確認有 GTP 封包
UPF_POD=$(kubectl get pod -n free5gc -l app=free5gc-upf -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n free5gc "$UPF_POD" -- tcpdump -i any -c 20 udp port 2152
```

如果 uesimtun0 沒有 IP，代表 PDU Session 沒有建立，需先完成 Phase 1。

---

## 7. Phase 3：Prometheus 真實指標串接

### 7.1 部署 Prometheus

**`k8s/monitoring/prometheus.yaml`**（若尚未部署）

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
  namespace: free5gc
data:
  prometheus.yml: |
    global:
      scrape_interval: 5s

    scrape_configs:
      # free5GC UPF metrics（需 free5GC 開啟 metrics endpoint）
      - job_name: 'free5gc-upf'
        static_configs:
          - targets: ['free5gc-upf:2112']

      # free5GC AMF
      - job_name: 'free5gc-amf'
        static_configs:
          - targets: ['free5gc-amf:2112']

      # free5GC SMF
      - job_name: 'free5gc-smf'
        static_configs:
          - targets: ['free5gc-smf:2112']

      # K8s Node / Pod metrics
      - job_name: 'kubernetes-pods'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: ['free5gc']
```

```bash
# 部署 Prometheus（若使用 Helm）
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/prometheus \
  -n free5gc \
  -f k8s/monitoring/prometheus-values.yaml
```

---

### 7.2 free5GC metrics 端點

free5GC 各 NF 預設會在 `:2112/metrics` 暴露 Prometheus 格式的指標。

**關鍵指標清單：**

```
# UPF
free5gc_upf_bytes_total{direction="uplink"}
free5gc_upf_bytes_total{direction="downlink"}
free5gc_upf_packets_total{direction="uplink"}
free5gc_upf_sessions_total

# AMF
free5gc_amf_registered_ue_total
free5gc_amf_registration_requests_total

# SMF
free5gc_smf_pdu_session_total
free5gc_smf_pdu_session_active

# K8s Pod CPU（來自 metrics-server）
container_cpu_usage_seconds_total{pod=~"free5gc-.*"}
```

確認端點是否可用：

```bash
UPF_POD=$(kubectl get pod -n free5gc -l app=free5gc-upf -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n free5gc "$UPF_POD" -- wget -qO- http://localhost:2112/metrics | head -30
```

---

### 7.3 後端 Prometheus 查詢函式

在 `backend/aws-app/index.py` 新增：

```python
import urllib.request
import json

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server.free5gc.svc.cluster.local:80")

def query_prometheus(promql: str) -> float:
    """查詢 Prometheus 並回傳純數值"""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(promql)}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            results = data.get("data", {}).get("result", [])
            if results:
                return float(results[0]["value"][1])
    except Exception as e:
        print(f"Prometheus query failed: {e}")
    return 0.0

def get_real_metrics() -> dict:
    """取得真實指標，不足時 fallback 到 0"""
    uplink_bytes = query_prometheus(
        'rate(free5gc_upf_bytes_total{direction="uplink"}[10s])'
    )
    downlink_bytes = query_prometheus(
        'rate(free5gc_upf_bytes_total{direction="downlink"}[10s])'
    )
    active_sessions = query_prometheus("free5gc_smf_pdu_session_active")
    registered_ues = query_prometheus("free5gc_amf_registered_ue_total")
    gtp_packets = query_prometheus(
        'rate(free5gc_upf_packets_total[10s])'
    )
    amf_cpu = query_prometheus(
        'rate(container_cpu_usage_seconds_total{pod=~"free5gc-amf.*"}[10s]) * 100'
    )
    upf_cpu = query_prometheus(
        'rate(container_cpu_usage_seconds_total{pod=~"free5gc-upf.*"}[10s]) * 100'
    )

    return {
        "throughputMbps": round((uplink_bytes + downlink_bytes) * 8 / 1_000_000, 2),
        "uplinkMbps": round(uplink_bytes * 8 / 1_000_000, 2),
        "downlinkMbps": round(downlink_bytes * 8 / 1_000_000, 2),
        "pduSessionCount": int(active_sessions),
        "registeredUeCount": int(registered_ues),
        "gtpPacketsPerSec": int(gtp_packets),
        "amfCpuPercent": round(amf_cpu, 1),
        "upfCpuPercent": round(upf_cpu, 1),
        "dataSource": "prometheus",  # 標記資料來源，方便 debug
    }
```

---

### 7.4 Slice 負載查詢

```python
def get_slice_metrics() -> dict:
    """各 Slice 的真實流量分布"""
    # 若 free5GC 有提供 per-slice metrics（依版本而定）
    embb_bytes = query_prometheus(
        'rate(free5gc_upf_bytes_total{sst="1"}[10s])'
    )
    urllc_bytes = query_prometheus(
        'rate(free5gc_upf_bytes_total{sst="2"}[10s])'
    )
    mmtc_bytes = query_prometheus(
        'rate(free5gc_upf_bytes_total{sst="3"}[10s])'
    )

    total = embb_bytes + urllc_bytes + mmtc_bytes or 1  # 避免除以 0

    return {
        "slices": {
            "eMBB":  {"throughputMbps": round(embb_bytes * 8 / 1e6, 2),
                      "loadPercent": round(embb_bytes / total * 100, 1)},
            "URLLC": {"throughputMbps": round(urllc_bytes * 8 / 1e6, 2),
                      "loadPercent": round(urllc_bytes / total * 100, 1)},
            "mMTC":  {"throughputMbps": round(mmtc_bytes * 8 / 1e6, 2),
                      "loadPercent": round(mmtc_bytes / total * 100, 1)},
        }
    }
```

> **注意：** 若 free5GC 版本不支援 per-slice label，可改用 SMF session 數量推估，或暫時只顯示總體 UPF 流量。

---

## 8. Phase 4：Lambda 事件整合

### 8.1 事件處理主流程修改

修改 `backend/aws-app/index.py` 的事件處理函式，將三個動作串在一起：

```python
import subprocess
import threading

def handle_event(event_type: str) -> dict:
    """
    整合三步驟：
    1. 修改 free5GC subscriber (QoS/NSSAI) -- 已有
    2. 觸發 UERANSIM Slice 切換
    3. 啟動 iperf3 流量 Job
    """

    # Step 1：已有邏輯，保留
    result = update_free5gc_subscriber(event_type)

    # Step 2：切換 UE Slice（透過 K8s API）
    slice_switch_result = trigger_ue_slice_switch(event_type)

    # Step 3：延遲 10 秒後啟動 iperf3（等 UE Attach 完成）
    def delayed_iperf3():
        import time
        time.sleep(10)
        trigger_iperf3_job(event_type)

    threading.Thread(target=delayed_iperf3, daemon=True).start()

    return {**result, "sliceSwitch": slice_switch_result}


def trigger_ue_slice_switch(event_type: str) -> dict:
    """呼叫 K8s API 切換 UE ConfigMap 並重啟 Deployment"""
    import boto3

    eks_client = boto3.client("eks")
    # 取得 EKS cluster kubeconfig token（Lambda 使用 IAM Role）
    # 實際上透過 kubernetes Python SDK 操作

    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_incluster_config()  # Lambda 在 EKS 內時使用

        apps_v1 = k8s_client.AppsV1Api()

        slice_map = {
            "concert": "ueransim-ue-config-embb",
            "accident": "ueransim-ue-config-embb",
            "er_surge": "ueransim-ue-config-urllc",
            "medical": "ueransim-ue-config-urllc",
            "typhoon": "ueransim-ue-config-urllc",
            "iot_surge": "ueransim-ue-config-mmtc",
        }

        config_map_name = slice_map.get(event_type, "ueransim-ue-config-embb")

        # Patch Deployment volume 指向新 ConfigMap
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{
                            "name": "ue-config",
                            "configMap": {"name": config_map_name}
                        }]
                    },
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt":
                                __import__("datetime").datetime.utcnow().isoformat()
                        }
                    }
                }
            }
        }

        apps_v1.patch_namespaced_deployment(
            name="ueransim-ue",
            namespace="free5gc",
            body=patch_body
        )

        return {"status": "ok", "configMap": config_map_name}

    except Exception as e:
        print(f"K8s slice switch failed: {e}")
        return {"status": "error", "error": str(e)}


def trigger_iperf3_job(event_type: str):
    """建立 iperf3 K8s Job"""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_incluster_config()

        batch_v1 = k8s_client.BatchV1Api()

        # 先刪除同名舊 Job
        job_name = f"iperf3-{event_type.replace('_', '-')}"
        try:
            batch_v1.delete_namespaced_job(
                name=job_name,
                namespace="free5gc",
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground")
            )
        except Exception:
            pass

        # 各場景 iperf3 參數
        IPERF3_PARAMS = {
            "concert":   ["-u", "-b", "800M", "-t", "120", "-l", "1400"],
            "er_surge":  ["-u", "-b", "10M",  "-t", "120", "-l", "100", "--trip-times"],
            "typhoon":   ["-u", "-b", "5M",   "-t", "120", "-l", "200"],
            "iot_surge": ["-u", "-b", "500K", "-t", "120", "-l", "64", "-P", "50"],
            "accident":  ["-u", "-b", "50M",  "-t", "60",  "-l", "1400"],
            "medical":   ["-u", "-b", "10M",  "-t", "120", "-l", "100", "--trip-times"],
        }

        params = IPERF3_PARAMS.get(event_type, ["-u", "-b", "100M", "-t", "60"])
        server = "iperf3-server.free5gc.svc.cluster.local"

        job_manifest = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace="free5gc"
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=300,
                template=k8s_client.V1PodTemplateSpec(
                    spec=k8s_client.V1PodSpec(
                        restart_policy="Never",
                        containers=[k8s_client.V1Container(
                            name="iperf3-client",
                            image="networkstatic/iperf3:latest",
                            command=["iperf3", "-c", server] + params + ["--json"]
                        )]
                    )
                )
            )
        )

        batch_v1.create_namespaced_job(namespace="free5gc", body=job_manifest)
        print(f"iperf3 job launched: {job_name}")

    except Exception as e:
        print(f"iperf3 job failed: {e}")
```

---

### 8.2 Lambda 需要的 IAM 權限

在 Terraform 的 Lambda IAM Role 新增：

```hcl
# terraform/iam.tf（新增至現有 Lambda role）

data "aws_iam_policy_document" "lambda_eks_policy" {
  statement {
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
      "eks:ListClusters",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_eks" {
  name   = "lambda-eks-access"
  role   = aws_iam_role.lambda_role.id
  policy = data.aws_iam_policy_document.lambda_eks_policy.json
}
```

EKS 端需要在 `aws-auth` ConfigMap 加入 Lambda Role：

```bash
# 確認 Lambda Role ARN
LAMBDA_ROLE_ARN=$(terraform output -raw lambda_role_arn)

# 編輯 aws-auth
kubectl edit configmap aws-auth -n kube-system
```

加入：

```yaml
mapRoles:
  - rolearn: <LAMBDA_ROLE_ARN>
    username: lambda
    groups:
      - system:masters  # Demo 用；生產環境應改為最小權限
```

---

### 8.3 /api/metrics 端點替換

修改 `backend/aws-app/index.py` 的 metrics 路由，優先使用 Prometheus：

```python
elif path == "/api/free5gc/status":
    # 優先取真實指標
    real_metrics = get_real_metrics()
    slice_metrics = get_slice_metrics()

    if real_metrics.get("dataSource") == "prometheus":
        # 真實數據路徑
        response_body = {
            **real_metrics,
            **slice_metrics,
            "free5gcStatus": "connected",
        }
    else:
        # Fallback：沿用目前的模擬邏輯
        response_body = get_simulated_metrics()

    return {
        "statusCode": 200,
        "body": json.dumps(response_body),
        "headers": CORS_HEADERS,
    }
```

---

## 9. Phase 5：Dashboard 替換假資料

### 9.1 需要修改的前端欄位

在 `frontend/src/` 中，將以下顯示欄位改為使用真實 API 資料：

| 欄位 | 目前來源 | 改為 |
|------|---------|------|
| `throughputMbps` | event_metrics 假值 | Prometheus UPF bytes rate |
| `pduSessionCount` | event_slices 假值 | Prometheus SMF active sessions |
| `gtpPacketsPerSec` | event_metrics 假值 | Prometheus UPF packets rate |
| `upfCpuPercent` | 假值 70 | K8s metrics-server UPF pod CPU |
| `amfCpuPercent` | 假值 | K8s metrics-server AMF pod CPU |
| `registeredUeCount` | /api/registered-ue-context 推估 | Prometheus AMF registered UE |
| Slice load % | event_slices 假值 | Prometheus per-slice bytes（或 session 數推估）|

---

### 9.2 新增資料來源指示器（建議）

在 Dashboard 右上角加入小標籤，讓評審知道數據來源：

```tsx
// frontend/src/components/Dashboard/MetricCard.tsx
interface MetricCardProps {
  value: number | string;
  label: string;
  dataSource?: "prometheus" | "estimated" | "simulated";
}

const DataSourceBadge = ({ source }: { source: string }) => {
  const colors = {
    prometheus: "bg-green-100 text-green-800",
    estimated:  "bg-yellow-100 text-yellow-800",
    simulated:  "bg-gray-100 text-gray-500",
  };
  return (
    <span className={`text-xs px-1 rounded ${colors[source] ?? colors.simulated}`}>
      {source === "prometheus" ? "● LIVE" : source === "estimated" ? "~ est." : "sim"}
    </span>
  );
};
```

這樣評審問「這是真的嗎」時，可以直接指著 `● LIVE` 說明。

---

## 10. 驗證方法

### 10.1 端到端驗證腳本

建立 `scripts/validate-e2e.sh`：

```bash
#!/bin/bash
# 模擬按下 Concert 按鈕的完整驗證

echo "=== Phase 1：觸發事件 ==="
curl -s -X POST https://<API_GW_URL>/api/events/trigger \
  -H "Content-Type: application/json" \
  -d '{"eventType":"concert"}' | python3 -m json.tool

sleep 15  # 等待 UE re-attach

echo ""
echo "=== Phase 2：確認 UE 使用正確 Slice ==="
kubectl exec -n free5gc deploy/free5gc-amf -- \
  wget -qO- http://localhost:8000/api/registered-ue-context 2>/dev/null \
  | python3 -c "import sys,json; data=json.load(sys.stdin); \
    [print(f'UE: {u.get(\"Supi\",\"?\")} | SST: {u.get(\"Nssai\",{}).get(\"singleNssai\",[{}])[0].get(\"sst\",\"?\")}')\
    for u in (data if isinstance(data, list) else [])]"

echo ""
echo "=== Phase 3：確認 iperf3 Job 執行中 ==="
kubectl get job -n free5gc -l app.kubernetes.io/component=iperf3

echo ""
echo "=== Phase 4：確認 UPF 有真實流量 ==="
UPF_POD=$(kubectl get pod -n free5gc -l app=free5gc-upf -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n free5gc "$UPF_POD" -- \
  cat /proc/net/dev 2>/dev/null | grep -E "upfgtp|lo" || \
  echo "(需要在 UPF Pod 內確認 GTP 介面)"

echo ""
echo "=== Phase 5：確認 Prometheus 有數據 ==="
curl -s "http://$(kubectl get svc -n free5gc prometheus-server -o jsonpath='{.spec.clusterIP}')/api/v1/query?query=free5gc_upf_bytes_total" \
  | python3 -m json.tool | head -20
```

---

### 10.2 各場景預期展示效果

| 場景 | 預期 Throughput | 預期 PDU Sessions | 預期 Jitter |
|------|----------------|-----------------|------------|
| Concert (eMBB) | 600–800 Mbps | 1 (高 AMBR) | 不重要 |
| ER Surge (URLLC) | 8–10 Mbps | 1 (GBR 保證) | < 5ms |
| Typhoon (URLLC) | 4–5 Mbps | 1 (GBR 保證) | < 10ms |
| IoT Surge (mMTC) | 20–30 Mbps | 50 streams | 不重要 |
| Accident (V2X) | 40–50 Mbps (短暫) | 1 (爆發) | 不重要 |

---

## 11. 常見問題

### Q1：UE Attach 後沒有 uesimtun0 介面

代表 PDU Session 建立失敗。

```bash
# 查看 UERANSIM UE log
kubectl logs -n free5gc deploy/ueransim-ue --tail=50

# 查看 SMF log
kubectl logs -n free5gc deploy/free5gc-smf --tail=50 | grep -i "session\|error"
```

常見原因：UE 的 NSSAI 與 free5GC subscriber 設定的 NSSAI 不符。確認兩邊的 `sst` 和 `sd` 完全一致。

---

### Q2：iperf3 流量沒有出現在 Prometheus

```bash
# 確認 iperf3 client 使用 uesimtun0 而非預設介面
kubectl exec -n free5gc <iperf3-job-pod> -- \
  iperf3 -c <server-ip> -B <ue-tunnel-ip> -u -b 10M -t 10
```

若 iperf3 Job 沒有 bind 到 UE 的 tunnel IP，流量就不會經過 UPF。

---

### Q3：Prometheus 查詢回傳空結果

```bash
# 確認 free5GC UPF metrics 端點存在
kubectl exec -n free5gc deploy/free5gc-upf -- wget -qO- http://localhost:2112/metrics

# 若不存在，可能需要在 UPF 的 upfcfg.yaml 開啟 metrics
# metrics:
#   enable: true
#   bindAddr: ":2112"
```

---

### Q4：Lambda 無法連線到 K8s

Lambda 若在 EKS 外部（非 in-cluster），需要改用 EKS API：

```python
import boto3, base64, tempfile, os
from kubernetes import client as k8s_client, config as k8s_config

def get_k8s_client():
    eks = boto3.client("eks", region_name=os.environ["AWS_REGION"])
    cluster = eks.describe_cluster(name=os.environ["EKS_CLUSTER_NAME"])["cluster"]

    # 取得 kubeconfig token
    sts = boto3.client("sts")
    token = sts.get_caller_identity()  # 搭配 aws-iam-authenticator token

    k8s_config.load_kube_config_from_dict({
        "apiVersion": "v1",
        "clusters": [{"cluster": {
            "server": cluster["endpoint"],
            "certificate-authority-data": cluster["certificateAuthority"]["data"]
        }, "name": "eks"}],
        "contexts": [{"context": {"cluster": "eks", "user": "lambda"}, "name": "eks"}],
        "current-context": "eks",
        "users": [{"name": "lambda", "user": {"token": get_eks_token()}}]
    })
```
