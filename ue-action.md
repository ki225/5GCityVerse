# UE 行為模擬開發文件

> **核心概念：** UE 需要的頻寬由「跑什麼 App」決定。不同情境 = 不同 App = 不同流量特質。  
> 5GC 根據 UE 申請的 QoS，分配對應的網路資源（Slice / AMBR / GBR / 5QI）。

---

## 目錄

1. [各情境的 App 模擬邏輯](#1-各情境的-app-模擬邏輯)
2. [UE 與 5GC 的互動流程](#2-ue-與-5gc-的互動流程)
3. [各情境實作規格](#3-各情境實作規格)
4. [UERANSIM 設定](#4-ueransim-設定)
5. [App 流量模擬（iperf3）](#5-app-流量模擬iperf3)
6. [free5GC subscriber 設定](#6-free5gc-subscriber-設定)
7. [5GC 資源分配的可觀測指標](#7-5gc-資源分配的可觀測指標)
8. [部署與執行](#8-部署與執行)

---

## 1. 各情境的 App 模擬邏輯

### 為什麼不同情境需要不同 UE 行為

現實中決定頻寬的層次：

```
使用者在做什麼（App）
        ↓
App 產生的流量特質（封包大小 / 頻率 / 頻寬）
        ↓
UE 向 5GC 申請的 QoS（PDU Session Request）
        ↓
5GC 根據 Slice Policy 分配資源
        ↓
UPF 執行 QoS Enforcement
```

### 各情境對應的現實 App

| 情境 | 現實 App | 流量特質 | UE 數量 |
|------|---------|---------|---------|
| **Concert** | 8 萬人看直播、上傳短影音 | 大封包、持續高頻寬、上行為主 | 1（展示頻寬上限）|
| **ER Surge** | 救護車即時影像、遠端醫療指令 | 小封包、低延遲、雙向穩定 | 1（展示延遲保證）|
| **Typhoon** | 緊急廣播、救災語音通話 | 小封包、保證頻寬、抗壅塞 | 3（少量緊急設備）|
| **IoT Surge** | 感測器回報溫度/位置/狀態 | 極小封包、間歇性、大量並發 | 50（本質是數量）|
| **Accident** | 車輛 V2X 廣播、事故影像上傳 | 短暫大流量爆發後釋放 | 1（展示爆發特性）|

---

## 2. UE 與 5GC 的互動流程

### 完整信令流程

```
UE 啟動
  │
  ├─[1] Registration Request → AMF
  │       包含：SUPI (IMSI)、requestedNSSAI (想要哪個 Slice)
  │
  ├─[2] AMF 驗證 UE 身份
  │       查 UDM：這個 IMSI 有沒有訂閱這個 Slice？
  │       查 subscriber profile：AMBR 上限是多少？
  │
  ├─[3] AMF 選擇對應的 SMF
  │       根據 NSSAI 選擇管理該 Slice 的 SMF
  │
  ├─[4] PDU Session Establishment Request → SMF
  │       UE 告訴 SMF：我要建立連線、我要跑什麼 APN/DNN
  │       SMF 查 PCF：這個 UE 的 QoS Policy 是什麼？
  │
  ├─[5] SMF 設定 UPF
  │       告訴 UPF：幫這個 UE 的流量套用以下規則
  │       - AMBR：最多給他 X Mbps
  │       - GBR：保留 Y Mbps 給他（URLLC 才有）
  │       - 5QI：封包排程優先級
  │
  └─[6] UE 取得 IP（來自 UPF）
          開始傳資料，UPF 執行 QoS Enforcement
```

### 應用程式如何影響 5GC 資源分配

```
App 開始傳大量資料
        ↓
UE 的 buffer 開始填滿
        ↓
UE 向 gNB 回報 Buffer Status Report (BSR)
        ↓
gNB 根據 5QI 決定排程優先級
（5QI=1 的 URLLC 先排，5QI=9 的 eMBB 後排）
        ↓
UPF 收到封包，套用 AMBR / GBR 限速
        ↓
Prometheus 可以看到：
  - UPF bytes rate 上升
  - 各 5QI 的封包數變化
  - Session 資源使用率
```

---

## 3. 各情境實作規格

### 3.1 Concert — eMBB 高頻寬

**現實場景：** 演唱會場地 8 萬人同時用手機上傳短影音、觀看直播。  
**App 行為：** 持續大量上行（上傳影片），下行串流（看直播）。

```
Slice:    SST=1, SD=000001 (eMBB)
5QI:      9  → Non-GBR, Packet Delay Budget=300ms, 優先級低但頻寬大
AMBR:     UL 1 Gbps / DL 1 Gbps（天花板高，盡力而為）
GBR:      無（不保證，但給最大資源）
UE 數量:  1
iperf3:   大封包 UDP，模擬影音 codec 輸出
```

**5GC 資源分配結果：**
- UPF 分配大量 downlink/uplink buffer 給此 UE
- gNB 排程時給予大量 PRB（Physical Resource Block）
- Prometheus 顯示：Throughput 大幅上升，Packets/sec 相對少（大封包）

---

### 3.2 ER Surge — URLLC 低延遲保證

**現實場景：** 救護車上的醫療設備即時傳送病患生命跡象影像給醫院。  
**App 行為：** 雙向穩定小流量，任何一個封包延遲都可能造成醫療誤判。

```
Slice:    SST=2, SD=000002 (URLLC)
5QI:      1  → GBR, Packet Delay Budget=100ms, 最高優先級
AMBR:     UL 50 Mbps / DL 50 Mbps
GBR:      UL 10 Mbps / DL 10 Mbps（保證這個頻寬永遠可用）
UE 數量:  1
iperf3:   小封包 UDP + 量測 RTT，模擬醫療影像壓縮幀
```

**5GC 資源分配結果：**
- SMF 在建立 PDU Session 時，要求 UPF 為此 UE **預留** 10 Mbps 資源
- 即使 Concert 情境同時存在，URLLC UE 的 GBR 不受影響
- Prometheus 顯示：Throughput 低但穩定，Jitter 極小

---

### 3.3 Typhoon — URLLC 緊急通訊

**現實場景：** 颱風期間救災人員的通訊設備、緊急廣播系統。  
**App 行為：** 語音通話品質必須保證，不能因為網路壅塞而斷線。

```
Slice:    SST=2, SD=000003 (URLLC)
5QI:      2  → GBR, Mission-critical voice, Packet Delay Budget=150ms
AMBR:     UL 20 Mbps / DL 20 Mbps
GBR:      UL 5 Mbps / DL 5 Mbps
UE 數量:  3（指揮中心 + 2 個救災隊伍）
iperf3:   小封包 UDP，模擬語音 codec (AMR-WB = 約 23.85 kbps/人)
```

**5GC 資源分配結果：**
- 3 個 UE 各自有獨立 GBR 保證
- 網路壅塞時，這 3 個 UE 的流量優先通過 UPF
- Prometheus 顯示：Session Count=3，每個 session 流量穩定

---

### 3.4 IoT Surge — mMTC 大量小設備

**現實場景：** 城市裡 50 個感測器（溫度、空氣品質、交通流量）同時回報數據。  
**App 行為：** 每個設備每 10 秒傳一次極小的資料包，單一設備流量微不足道，但同時連線數很多。

```
Slice:    SST=3, SD=000004 (mMTC)
5QI:      79 → Non-GBR, small data, 最低優先級
AMBR:     UL 1 Mbps / DL 1 Mbps（每個 UE 上限極低）
GBR:      無
UE 數量:  50（每個代表一個 IoT 設備）
iperf3:   極小封包 UDP，多個並發 stream
```

**5GC 資源分配結果：**
- AMF 要處理 50 個 Registration（連線數是壓力來源）
- SMF 建立 50 個 PDU Session
- UPF：每個 session 流量極小，但 session table 很大
- Prometheus 顯示：Throughput 低，但 Session Count 和 Packets/sec 數量多

---

### 3.5 Accident — V2X 爆發流量

**現實場景：** 車禍發生瞬間，車輛廣播緊急訊號 + 行車紀錄器影像上傳。  
**App 行為：** 極短時間內大量上傳，完成後立即釋放資源。

```
Slice:    SST=4, SD=000005 (V2X)
5QI:      75 → Mission-critical V2X, Packet Delay Budget=50ms
AMBR:     UL 200 Mbps / DL 200 Mbps
GBR:      無（但優先級高）
UE 數量:  1
iperf3:   大封包 UDP，持續 30 秒後停止（模擬事故影像上傳）
```

**5GC 資源分配結果：**
- UPF 短暫看到高流量爆發，30 秒後歸零
- Prometheus 顯示：Throughput 曲線呈現明顯的「脈衝」形狀

---

## 4. UERANSIM 設定

### 目錄結構

```
k8s/ueransim/
  ├── configs/
  │   ├── ue-embb.yaml        # Concert / Accident
  │   ├── ue-urllc-er.yaml    # ER Surge
  │   ├── ue-urllc-typhoon.yaml  # Typhoon (3 UE)
  │   ├── ue-mmtc.yaml        # IoT Surge (50 UE)
  │   └── ue-v2x.yaml         # Accident
  └── deployments/
      ├── ueransim-embb.yaml
      ├── ueransim-urllc.yaml
      ├── ueransim-mmtc.yaml
      └── ueransim-v2x.yaml
```

---

### 4.1 eMBB UE 設定（Concert）

**`k8s/ueransim/configs/ue-embb.yaml`**

```yaml
supi: "imsi-208930000000001"
mcc: "208"
mnc: "93"
key: "8baf473f2f8fd09487cccbd7097c6862"
op: "8e27b6af0e692e750f32667a3b14605d"
opType: "OP"
amf:
  value: "8000"
  region: "128"
  set: "1"

# 向 5GC 申請 eMBB Slice
requestedNssai:
  - sst: 1
    sd: "000001"

# PDU Session 設定
sessions:
  - type: "IPv4"
    apn: "internet"
    slice:
      sst: 1
      sd: "000001"

configured-nssai:
  - sst: 1
    sd: "000001"
```

---

### 4.2 URLLC UE 設定（ER Surge）

**`k8s/ueransim/configs/ue-urllc-er.yaml`**

```yaml
supi: "imsi-208930000000002"
mcc: "208"
mnc: "93"
key: "8baf473f2f8fd09487cccbd7097c6862"
op: "8e27b6af0e692e750f32667a3b14605d"
opType: "OP"

# 向 5GC 申請 URLLC Slice
requestedNssai:
  - sst: 2
    sd: "000002"

sessions:
  - type: "IPv4"
    apn: "internet"
    slice:
      sst: 2
      sd: "000002"

configured-nssai:
  - sst: 2
    sd: "000002"
```

---

### 4.3 Typhoon UE 設定（3 個 UE）

**`k8s/ueransim/configs/ue-urllc-typhoon.yaml`**

```yaml
# UERANSIM 支援單一設定檔跑多個 UE
# 每個 UE 自動遞增 IMSI

supi: "imsi-208930000000010"   # 從 010 開始，與其他情境區隔
mcc: "208"
mnc: "93"
key: "8baf473f2f8fd09487cccbd7097c6862"
op: "8e27b6af0e692e750f32667a3b14605d"
opType: "OP"

requestedNssai:
  - sst: 2
    sd: "000003"

sessions:
  - type: "IPv4"
    apn: "emergency"          # 使用不同 DNN 區隔緊急通訊
    slice:
      sst: 2
      sd: "000003"

configured-nssai:
  - sst: 2
    sd: "000003"
```

**`k8s/ueransim/deployments/ueransim-urllc-typhoon.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ueransim-typhoon
  namespace: free5gc
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ueransim-typhoon
  template:
    metadata:
      labels:
        app: ueransim-typhoon
    spec:
      containers:
        - name: ueransim-ue
          image: free5gc/ueransim:latest
          command: ["nr-ue"]
          args:
            - "-c"
            - "/etc/ueransim/ue.yaml"
            - "-n"
            - "3"              # 啟動 3 個 UE instance，IMSI 自動遞增
          volumeMounts:
            - name: ue-config
              mountPath: /etc/ueransim
      volumes:
        - name: ue-config
          configMap:
            name: ueransim-ue-config-urllc-typhoon
```

---

### 4.4 mMTC UE 設定（50 個 IoT 設備）

**`k8s/ueransim/configs/ue-mmtc.yaml`**

```yaml
supi: "imsi-208930000000100"   # 從 100 開始，與其他情境完全區隔
mcc: "208"
mnc: "93"
key: "8baf473f2f8fd09487cccbd7097c6862"
op: "8e27b6af0e692e750f32667a3b14605d"
opType: "OP"

requestedNssai:
  - sst: 3
    sd: "000004"

sessions:
  - type: "IPv4"
    apn: "iot"                 # IoT 專屬 DNN
    slice:
      sst: 3
      sd: "000004"

configured-nssai:
  - sst: 3
    sd: "000004"
```

**`k8s/ueransim/deployments/ueransim-mmtc.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ueransim-iot
  namespace: free5gc
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ueransim-iot
  template:
    metadata:
      labels:
        app: ueransim-iot
    spec:
      containers:
        - name: ueransim-ue
          image: free5gc/ueransim:latest
          command: ["nr-ue"]
          args:
            - "-c"
            - "/etc/ueransim/ue.yaml"
            - "-n"
            - "50"             # 50 個 UE，模擬 50 個 IoT 設備
          resources:
            requests:
              memory: "512Mi"
              cpu: "500m"
            limits:
              memory: "1Gi"
              cpu: "1"
          volumeMounts:
            - name: ue-config
              mountPath: /etc/ueransim
      volumes:
        - name: ue-config
          configMap:
            name: ueransim-ue-config-mmtc
```

---

## 5. App 流量模擬（iperf3）

### 5.1 為什麼用 iperf3 模擬 App

```
真實 App（Netflix / 醫療影像 / IoT SDK）太複雜，無法在 Demo 環境部署
iperf3 可以精確控制：
  - 封包大小  → 模擬不同 codec / protocol
  - 頻寬      → 模擬 App 的實際需求
  - 並發數    → 模擬多個 App stream 或多個設備
  - 持續時間  → 模擬不同使用行為
```

### 5.2 各情境 iperf3 參數對照

**Concert（影音直播）**

```bash
# 模擬：H.264 1080p 直播上行串流
# 封包大小 1400 bytes = 接近 MTU，影音 codec 輸出特性
# 800 Mbps = 大型演唱會直播所需上行頻寬
iperf3 \
  -c $IPERF3_SERVER \
  -u \              # UDP：影音串流不重傳，即時性優先
  -b 800M \         # 目標頻寬 800 Mbps
  -l 1400 \         # 封包大小 1400 bytes（大封包）
  -t 120 \          # 持續 2 分鐘
  --json
```

**ER Surge（醫療即時影像）**

```bash
# 模擬：醫療影像壓縮幀 + 生命跡象資料
# 封包大小 200 bytes = 壓縮後的生命跡象資料
# 10 Mbps = 高畫質醫療影像最低需求
# --trip-times = 量測 RTT，展示低延遲
iperf3 \
  -c $IPERF3_SERVER \
  -u \
  -b 10M \          # 穩定 10 Mbps（GBR 保證值）
  -l 200 \          # 小封包（醫療資料壓縮後）
  -t 120 \
  --trip-times \    # 量測來回延遲，展示 URLLC 低延遲特性
  --json
```

**Typhoon（緊急語音通話）**

```bash
# 模擬：AMR-WB 語音 codec（約 24 kbps/通話）x 3 設備
# 封包大小 60 bytes = RTP over UDP 語音封包典型大小
# 5 Mbps = 多路語音 + 控制訊號
iperf3 \
  -c $IPERF3_SERVER \
  -u \
  -b 5M \           # 保證頻寬（對應 GBR 設定）
  -l 60 \           # 極小封包，模擬語音 RTP 封包
  -t 120 \
  --trip-times \
  --json
```

**IoT Surge（感測器資料）**

```bash
# 模擬：50 個感測器同時回報資料
# 封包大小 64 bytes = 典型 IoT sensor payload（溫度+位置+狀態）
# -P 50 = 50 個並發 stream，每個代表一個感測器
# 每個 stream 只有 200 Kbps，但 50 個加起來展示連線數
iperf3 \
  -c $IPERF3_SERVER \
  -u \
  -b 200K \         # 每個感測器極低頻寬
  -l 64 \           # 極小封包（IoT payload）
  -P 50 \           # 50 個並發 stream
  -t 120 \
  --json
```

**Accident（事故影像爆發上傳）**

```bash
# 模擬：行車紀錄器事故片段緊急上傳（30 秒後自動停止）
# 大封包高頻寬，但只持續 30 秒
iperf3 \
  -c $IPERF3_SERVER \
  -u \
  -b 150M \         # 事故瞬間爆發上傳
  -l 1400 \
  -t 30 \           # 只持續 30 秒，模擬短暫爆發
  --json
```

---

### 5.3 iperf3 Job 範本（所有情境共用）

**`k8s/iperf3/job-template.yaml`**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: iperf3-${SCENARIO}
  namespace: free5gc
spec:
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: iperf3-client
          image: networkstatic/iperf3:latest
          command: ["sh", "-c"]
          args:
            - |
              # 等待 UE tunnel 介面準備好
              until ip link show uesimtun0 > /dev/null 2>&1; do
                echo "Waiting for UE tunnel interface..."
                sleep 2
              done
              echo "UE tunnel ready, starting iperf3..."
              
              # 透過 UE tunnel 介面傳送（確保流量經過 UPF）
              iperf3 -c $IPERF3_SERVER \
                     -B $(ip addr show uesimtun0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1) \
                     ${IPERF3_ARGS} \
                     --json | tee /tmp/iperf3-result.json
              
              echo "=== iperf3 Result ==="
              cat /tmp/iperf3-result.json
          env:
            - name: IPERF3_SERVER
              value: "iperf3-server.free5gc.svc.cluster.local"
            - name: IPERF3_ARGS
              value: "${SCENARIO_SPECIFIC_ARGS}"
          # 共用 UE Pod 的 network namespace，才能存取 uesimtun0
          # （需要與 UERANSIM UE Pod 在同一 Node 或使用 hostNetwork）
```

> **重點：** `-B $(uesimtun0 IP)` 強制 iperf3 從 UE tunnel 介面送出流量，  
> 確保封包走 GTP Tunnel → UPF，而非直接走 K8s Pod 網路。

---

### 5.4 iperf3 Server 部署

**`k8s/iperf3/server.yaml`**

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
          command: ["iperf3", "-s", "-p", "5201", "--json"]
          ports:
            - containerPort: 5201
              protocol: UDP
            - containerPort: 5201
              protocol: TCP
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
    - port: 5201
      protocol: UDP
    - port: 5201
      protocol: TCP
```

---

## 6. free5GC subscriber 設定

### 6.1 為什麼 subscriber 設定很重要

```
UE 向 AMF 說：「我要連 SST=2 的 Slice」
AMF 去問 UDM：「這個 IMSI 有沒有訂閱 SST=2？」
UDM 查 MongoDB subscriber 資料
  → 有：繼續，並取得 AMBR / GBR / 5QI 設定
  → 沒有：Registration Reject
```

subscriber 設定決定了 5GC 最終給這個 UE 多少資源。

---

### 6.2 預建 subscriber 腳本

**`scripts/seed-subscribers.py`**

```python
#!/usr/bin/env python3
"""
預先建立所有情境需要的 subscriber
在 free5GC WebUI 部署後執行一次即可
"""

import requests
import json

FREE5GC_WEBUI = "http://localhost:5000"
PLMN_ID = "20893"

def login():
    resp = requests.post(f"{FREE5GC_WEBUI}/api/login",
                         json={"username": "admin", "password": "free5gc"})
    return resp.json()["token"]

def create_subscriber(token: str, imsi: str, profile: dict):
    headers = {"Token": token, "Content-Type": "application/json"}
    url = f"{FREE5GC_WEBUI}/api/subscriber/{imsi}/{PLMN_ID}"
    resp = requests.post(url, headers=headers, json=profile)
    if resp.status_code == 201:
        print(f"  ✓ Created: {imsi}")
    elif resp.status_code == 409:
        requests.put(url, headers=headers, json=profile)
        print(f"  ↻ Updated: {imsi}")
    else:
        print(f"  ✗ Failed:  {imsi} → {resp.status_code}")

# ============================================================
# Subscriber Profile 定義
# ============================================================

def embb_profile(imsi: str) -> dict:
    """Concert / Accident 用的 eMBB Profile"""
    return {
        "plmnID": PLMN_ID,
        "ueId": imsi,
        "AuthenticationSubscription": {
            "authenticationMethod": "5G_AKA",
            "permanentKey": {"permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862"},
            "sequenceNumber": "000000000023",
            "authenticationManagementField": "8000",
            "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [f"msisdn-{imsi[-10:]}"],
            "subscribedUeAmbr": {
                "uplink": "1 Gbps",
                "downlink": "1 Gbps"
            },
            "nssai": {
                "defaultSingleNssais": [{"sst": 1, "sd": "000001"}],
                "singleNssais": [{"sst": 1, "sd": "000001"}]
            }
        },
        "SessionManagementSubscriptionData": [{
            "singleNssai": {"sst": 1, "sd": "000001"},
            "dnnConfigurations": {
                "internet": {
                    "pduSessionTypes": {"defaultSessionType": "IPV4"},
                    "sscModes": {"defaultSscMode": "SSC_MODE_1"},
                    "5gQosProfile": {
                        "5qi": 9,
                        "arp": {"priorityLevel": 8, "preemptCap": "NOT_PREEMPT",
                                "preemptVuln": "NOT_PREEMPTABLE"},
                        "priorityLevel": 8
                    },
                    "sessionAmbr": {
                        "uplink": "1 Gbps",
                        "downlink": "1 Gbps"
                    },
                    # 無 GBR，盡力而為
                    "staticIpAddress": []
                }
            }
        }]
    }

def urllc_er_profile(imsi: str) -> dict:
    """ER Surge 用的 URLLC Profile（GBR 保證）"""
    return {
        "plmnID": PLMN_ID,
        "ueId": imsi,
        "AuthenticationSubscription": {
            "authenticationMethod": "5G_AKA",
            "permanentKey": {"permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862"},
            "sequenceNumber": "000000000023",
            "authenticationManagementField": "8000",
            "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [f"msisdn-{imsi[-10:]}"],
            "subscribedUeAmbr": {
                "uplink": "50 Mbps",
                "downlink": "50 Mbps"
            },
            "nssai": {
                "defaultSingleNssais": [{"sst": 2, "sd": "000002"}],
                "singleNssais": [{"sst": 2, "sd": "000002"}]
            }
        },
        "SessionManagementSubscriptionData": [{
            "singleNssai": {"sst": 2, "sd": "000002"},
            "dnnConfigurations": {
                "internet": {
                    "pduSessionTypes": {"defaultSessionType": "IPV4"},
                    "sscModes": {"defaultSscMode": "SSC_MODE_1"},
                    "5gQosProfile": {
                        "5qi": 1,              # URLLC 最高優先級
                        "arp": {"priorityLevel": 1, "preemptCap": "MAY_PREEMPT",
                                "preemptVuln": "NOT_PREEMPTABLE"},
                        "priorityLevel": 1
                    },
                    "sessionAmbr": {
                        "uplink": "50 Mbps",
                        "downlink": "50 Mbps"
                    },
                    # GBR 保證頻寬
                    "gbrQosFlowInfo": {
                        "maxFbrUplink": "10 Mbps",
                        "maxFbrDownlink": "10 Mbps",
                        "guaranteedFbrUplink": "10 Mbps",
                        "guaranteedFbrDownlink": "10 Mbps"
                    }
                }
            }
        }]
    }

def mmtc_profile(imsi: str) -> dict:
    """IoT Surge 用的 mMTC Profile"""
    return {
        "plmnID": PLMN_ID,
        "ueId": imsi,
        "AuthenticationSubscription": {
            "authenticationMethod": "5G_AKA",
            "permanentKey": {"permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862"},
            "sequenceNumber": "000000000023",
            "authenticationManagementField": "8000",
            "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [f"msisdn-{imsi[-10:]}"],
            "subscribedUeAmbr": {
                "uplink": "1 Mbps",       # IoT 每個設備頻寬極低
                "downlink": "1 Mbps"
            },
            "nssai": {
                "defaultSingleNssais": [{"sst": 3, "sd": "000004"}],
                "singleNssais": [{"sst": 3, "sd": "000004"}]
            }
        },
        "SessionManagementSubscriptionData": [{
            "singleNssai": {"sst": 3, "sd": "000004"},
            "dnnConfigurations": {
                "iot": {
                    "pduSessionTypes": {"defaultSessionType": "IPV4"},
                    "sscModes": {"defaultSscMode": "SSC_MODE_1"},
                    "5gQosProfile": {
                        "5qi": 79,         # Small data，最低優先級
                        "arp": {"priorityLevel": 15, "preemptCap": "NOT_PREEMPT",
                                "preemptVuln": "PREEMPTABLE"},
                        "priorityLevel": 15
                    },
                    "sessionAmbr": {
                        "uplink": "1 Mbps",
                        "downlink": "1 Mbps"
                    }
                }
            }
        }]
    }

# ============================================================
# 主程式：建立所有 subscriber
# ============================================================

if __name__ == "__main__":
    print("Logging in to free5GC WebUI...")
    token = login()

    print("\n[1/4] eMBB Subscribers (Concert / Accident)")
    create_subscriber(token, "imsi-208930000000001", embb_profile("imsi-208930000000001"))

    print("\n[2/4] URLLC Subscribers (ER Surge)")
    create_subscriber(token, "imsi-208930000000002", urllc_er_profile("imsi-208930000000002"))

    print("\n[3/4] URLLC Subscribers (Typhoon, 3 UE)")
    for i in range(10, 13):   # imsi ...010, 011, 012
        imsi = f"imsi-20893000000{str(i).zfill(4)}"
        # Typhoon 用類似 ER 的 URLLC profile，但 SD 不同
        profile = urllc_er_profile(imsi)
        profile["AccessAndMobilitySubscriptionData"]["nssai"] = {
            "defaultSingleNssais": [{"sst": 2, "sd": "000003"}],
            "singleNssais": [{"sst": 2, "sd": "000003"}]
        }
        profile["SessionManagementSubscriptionData"][0]["singleNssai"] = {
            "sst": 2, "sd": "000003"
        }
        create_subscriber(token, imsi, profile)

    print("\n[4/4] mMTC Subscribers (IoT Surge, 50 UE)")
    for i in range(100, 150):   # imsi ...0100 ~ 0149
        imsi = f"imsi-20893000000{str(i).zfill(4)}"
        create_subscriber(token, imsi, mmtc_profile(imsi))

    print("\nDone. All subscribers created.")
```

---

## 7. 5GC 資源分配的可觀測指標

### 7.1 各情境預期展示的 5GC 指標

| 情境 | AMF registered UE | SMF PDU Sessions | UPF Throughput | UPF Packets/sec | Jitter |
|------|:-----------------:|:----------------:|:--------------:|:---------------:|:------:|
| Concert | 1 | 1 | 高（600+ Mbps）| 少（大封包）| 不重要 |
| ER Surge | 1 | 1 | 低（~10 Mbps）| 少 | **< 5ms** |
| Typhoon | 3 | 3 | 低（~15 Mbps）| 少 | **< 10ms** |
| IoT Surge | 50 | 50 | 低（~10 Mbps）| **極多** | 不重要 |
| Accident | 1 | 1 | 短暫高（150 Mbps）→ 0 | 短暫多 | 不重要 |

### 7.2 Prometheus 查詢對照

```promql
# UPF 總 Throughput（Mbps）
rate(free5gc_upf_bytes_total[10s]) * 8 / 1000000

# 各 Slice 的 PDU Session 數
free5gc_smf_pdu_session_active

# AMF 已註冊 UE 數
free5gc_amf_registered_ue_total

# UPF 每秒封包數（展示 IoT 小封包特性）
rate(free5gc_upf_packets_total[10s])

# iperf3 量測的 RTT（從 Job log 解析）
# 用於展示 URLLC 低延遲保證
```

---

## 8. 部署與執行

### 8.1 初始化（只需執行一次）

```bash
# 1. 部署 iperf3 server
kubectl apply -f k8s/iperf3/server.yaml

# 2. 建立所有 UERANSIM ConfigMap
kubectl create configmap ueransim-ue-config-embb \
  --from-file=ue.yaml=k8s/ueransim/configs/ue-embb.yaml \
  -n free5gc

kubectl create configmap ueransim-ue-config-urllc-er \
  --from-file=ue.yaml=k8s/ueransim/configs/ue-urllc-er.yaml \
  -n free5gc

kubectl create configmap ueransim-ue-config-urllc-typhoon \
  --from-file=ue.yaml=k8s/ueransim/configs/ue-urllc-typhoon.yaml \
  -n free5gc

kubectl create configmap ueransim-ue-config-mmtc \
  --from-file=ue.yaml=k8s/ueransim/configs/ue-mmtc.yaml \
  -n free5gc

# 3. 預建所有 subscriber
python3 scripts/seed-subscribers.py
```

### 8.2 事件觸發流程

```bash
# Concert
./scripts/switch-scenario.sh concert
# → 切換 UE 到 eMBB ConfigMap
# → 等待 UE Re-attach (SST=1)
# → 啟動 iperf3: -u -b 800M -l 1400

# ER Surge
./scripts/switch-scenario.sh er_surge
# → 切換 UE 到 URLLC ConfigMap
# → 等待 UE Re-attach (SST=2, GBR=10Mbps)
# → 啟動 iperf3: -u -b 10M -l 200 --trip-times

# IoT Surge
./scripts/switch-scenario.sh iot_surge
# → 啟動 ueransim-iot Deployment（50 UE）
# → 等待 50 個 UE 全部 Attach (SST=3)
# → 啟動 iperf3: -u -b 200K -l 64 -P 50
```

### 8.3 驗證各情境

```bash
# 確認 UE 使用正確 Slice
kubectl exec -n free5gc deploy/free5gc-amf -- \
  wget -qO- http://localhost:8000/api/registered-ue-context \
  | python3 -c "
import sys, json
ues = json.load(sys.stdin)
for ue in (ues if isinstance(ues, list) else []):
    supi = ue.get('Supi', '?')
    nssai = ue.get('Nssai', {}).get('singleNssai', [{}])
    sst = nssai[0].get('sst', '?') if nssai else '?'
    print(f'UE: {supi}  SST: {sst}')
"

# 確認 UPF 有真實流量
kubectl exec -n free5gc deploy/free5gc-upf -- \
  wget -qO- http://localhost:2112/metrics \
  | grep free5gc_upf_bytes_total

# 確認 iperf3 RTT（URLLC 情境）
kubectl logs -n free5gc job/iperf3-er-surge \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
intervals = data.get('intervals', [])
for i in intervals[-3:]:  # 最後 3 個量測
    rtt = i['sum'].get('mean_rtt', 0) / 1000  # us to ms
    print(f'RTT: {rtt:.2f} ms')
"
```

---

*文件版本：v1.0 | 對應專案：5GCityVerse*