# 5GCityVerse

> 以 free5GC + EKS 為基礎，打造真實可觀測的 5G 智慧城市數位分身平台。
> 使用者觸發城市事件 → 產生真實網路流量 → 驅動 K8s HPA 擴縮 → 前端即時呈現真實 Pod 狀態。
> **每一個動畫背後，都是真實發生的基礎設施行為。**

---

## 專案理念

大多數人以為：

```
5G = 更快的網路
```

5G 真正的核心是：

- **Network Slicing** — 不同服務共用實體網路，卻彼此隔離
- **Service-Based Architecture (SBA)** — 網路功能如微服務互相溝通
- **Cloud-native UPF** — User Plane 可動態擴縮，如同 K8s workload
- **QoS 保護** — URLLC 流量在壅塞時仍被保護
- **Edge Computing** — 延遲敏感服務在邊緣節點處理

本專案的核心主張：

> **讓看不見的網路行為，透過真實基礎設施變化被看見。**

城市裡每一個事件，都驅動真實的 free5GC 流量、真實的 K8s HPA，前端呈現的是真實狀態，而非預設動畫。

---

## 專案目標

- 讓一般民眾直觀理解 5G 核心特性
- 展示 Network Slicing 的實際隔離效果
- 真實呈現 Cloud-native 5GC 的動態擴縮行為
- 建立可複用的 5G 教育展示平台
- 作為 free5GC + EKS 的參考架構

---

## 核心設計原則：真實因果鏈

本專案刻意避免「假 scaling」——前端動畫不由計時器或按鈕直接驅動，而是由真實的基礎設施事件驅動：

```
使用者按下 [AR 演唱會]
        │
        ▼
Event Engine 觸發 UERANSIM
產生大量 iperf3 eMBB traffic
        │
        ▼
free5GC UPF Pod
真實 CPU / Network throughput 上升
        │
        ▼
K8s HPA 偵測 metrics 超閾值
觸發真實 scale out
        │
        ▼
Prometheus 抓到新 Pod 狀態
        │
        ▼
State Bridge 推送 WebSocket 事件
        │
        ▼
前端城市地圖
UPF-2 節點亮起（真實 Pod 上線）
```

每一層都是真實發生的，沒有任何一層是模擬數字。

---

## 城市事件系統

### 🚗 自駕車增加（URLLC）

| 項目 | 說明 |
|---|---|
| Slice 類型 | URLLC — Ultra-Reliable Low Latency |
| UERANSIM 行為 | 產生低延遲、小封包、高頻率 traffic |
| 系統反應 | Edge UPF 優先處理，QoS priority 提升 |
| 前端呈現 | 紅色 packet flow，latency dashboard 改變 |
| K8s 行為 | Edge UPF HPA 可能觸發 |

### 🎮 AR 演唱會（eMBB）

| 項目 | 說明 |
|---|---|
| Slice 類型 | eMBB — Enhanced Mobile Broadband |
| UERANSIM 行為 | iperf3 UDP 大流量，模擬影音串流 |
| 系統反應 | UPF CPU/network throughput 暴增，HPA 觸發 |
| 前端呈現 | 藍色 traffic 爆炸，UPF-2 真實上線 |
| K8s 行為 | **主要 scaling showcase**，UPF 從 1 擴至 4 |

### 🏭 智慧工廠模式（Industrial URLLC / TSN）

| 項目 | 說明 |
|---|---|
| Slice 類型 | Industrial URLLC — deterministic low latency |
| UERANSIM 行為 | 固定週期小封包，模擬工業控制訊號 |
| 系統反應 | 專屬 Slice 隔離，不受其他流量影響 |
| 前端呈現 | 工廠路徑保護動畫，latency 維持低值 |
| K8s 行為 | 工廠 UPF 獨立 namespace，不共用資源 |

### 🏠 居民下班時間（General eMBB）

| 項目 | 說明 |
|---|---|
| Slice 類型 | General eMBB |
| UERANSIM 行為 | 多 UE 同時產生 YouTube / Gaming 流量 |
| 系統反應 | throughput 上升，residential traffic 密度增加 |
| 前端呈現 | packet flow 密度提升，throughput dashboard 變化 |

### 📡 IoT 裝置爆量（mMTC）

| 項目 | 說明 |
|---|---|
| Slice 類型 | mMTC — Massive Machine-Type Communication |
| UERANSIM 行為 | 大量 UE 同時發起 session，小封包高頻率 |
| 系統反應 | AMF session 數暴增，signaling traffic 增加 |
| 前端呈現 | 綠色 packet 大量出現，AMF session counter 爆增 |
| K8s 行為 | AMF HPA 可能觸發（依 session count metrics） |

---

## 系統架構

### 整體架構圖

```
┌─────────────────────────────────────────────────────────────┐
│                        使用者瀏覽器                           │
│               React Frontend (S3 + CloudFront)               │
│         城市地圖 │ 流量動畫 │ Pod 狀態 │ Metrics Dashboard    │
└──────────────────────────┬──────────────────────────────────┘
                           │ WebSocket + REST
                           ▼
              ┌────────────────────────┐
              │      API Gateway        │
              └────────────┬───────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
  │ Event Engine │ │ State Bridge │ │ Metrics Proxy     │
  │ FastAPI/ECS  │ │ K8s Watcher  │ │ Prometheus → API  │
  │              │ │ → WebSocket  │ │                  │
  └──────┬───────┘ └──────────────┘ └──────────────────┘
         │
         │ 觸發流量
         ▼
┌────────────────────────────────────────────────────────────┐
│                    EKS Cluster                              │
│                                                            │
│  ┌─────────────── free5gc namespace ──────────────────┐   │
│  │                                                     │   │
│  │  AMF ──── SMF ──── NRF                             │   │
│  │   │        │                                       │   │
│  │  AUSF     PCF                                      │   │
│  │            │                                       │   │
│  │           UPF (HPA: 1~4 pods) ◄── 主要 scale 點   │   │
│  │            │                                       │   │
│  └────────────┼───────────────────────────────────────┘   │
│               │                                            │
│  ┌─────────── ueransim namespace ─────────────────────┐   │
│  │  gNB Pod  ──►  UE Pods (Job-based, 按需啟動)       │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌─────────── monitoring namespace ───────────────────┐   │
│  │  Prometheus ── Prometheus Adapter ── Grafana        │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌─────────── platform namespace ─────────────────────┐   │
│  │  Event Engine ── State Bridge ── Metrics Proxy      │   │
│  └─────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

### HPA Metrics 策略

| 階段 | Metrics 來源 | Scale Target | 說明 |
|---|---|---|---|
| MVP | CPU Utilization | UPF | 最簡單，先讓整條鏈跑通 |
| V2 | Network throughput (custom metrics) | UPF | 語意正確，流量大 → UPF scale |
| V3 | free5GC session count | AMF / UPF | 最符合電信語意 |

### State Bridge（關鍵元件）

State Bridge 負責將 K8s 真實事件推送給前端，是「基礎設施狀態 → 視覺動畫」的橋樑：

```python
# 核心邏輯示意
from kubernetes import client, watch
import asyncio, websockets, json

async def watch_pods(websocket):
    v1 = client.CoreV1Api()
    w = watch.Watch()
    for event in w.stream(v1.list_namespaced_pod, namespace="free5gc"):
        pod_name = event['object'].metadata.name
        phase    = event['object'].status.phase
        await websocket.send(json.dumps({
            "event":    event['type'],   # ADDED / MODIFIED / DELETED
            "pod":      pod_name,
            "phase":    phase,
            "component": extract_component(pod_name)  # UPF / AMF / SMF
        }))
```

前端收到 `ADDED` 事件後，城市地圖上對應的節點才亮起，不是計時器，是真實 Pod 上線。

---

## 已知技術挑戰

### free5GC UPF 在 K8s 的關鍵問題

| 問題 | 說明 | 解法方向 |
|---|---|---|
| gtp5g kernel module | EKS node 預設不含，UPF 無法建立 GTP tunnel | 自訂 EKS AMI 或 DaemonSet 動態載入 |
| UPF scale out 後舊 session | 新 Pod 不知道舊 GTP tunnel 狀態 | SMF 重新建立 N4 session，或 session persistence |
| N4 interface 動態重連 | SMF 需知道新 UPF 地址 | 透過 NRF 動態服務發現，需驗證 free5GC 行為 |

> 這三個問題建議先在本地 kind / minikube 驗證，再上 EKS。

### HPA Trigger 驗證

CPU-based HPA 需要確認 iperf3 流量能真實將 UPF CPU 推至閾值以上，這取決於：
- EC2 worker node 的 instance type（建議 `t3.medium` 以上）
- HPA `averageUtilization` 閾值設定（建議從 50% 開始調整）
- iperf3 並行數量與持續時間

---

## UI 設計規格

### 前端佈局

```
┌────────────────┬──────────────────┬──────────────────┐
│                │                  │                  │
│   虛擬城市      │   流量動畫層      │  5GC Dashboard   │
│   (SVG 俯視)   │   (D3.js)        │                  │
│                │                  │  Latency         │
│  商場  工廠     │  ●─────────►●   │  Bandwidth       │
│  醫院  居民區   │  (colored        │  UPF Load        │
│  Data Center   │   packets)       │  Session Count   │
│                │                  │  Pod 狀態列表    │
└────────────────┴──────────────────┴──────────────────┘
│                    城市事件控制台                       │
│  [+自駕車] [AR演唱會] [工廠模式] [居民下班] [IoT爆量]  │
└────────────────────────────────────────────────────────┘
```

### Slice 顏色對應

| 顏色 | Slice | 服務類型 |
|---|---|---|
| 🔴 紅色 | URLLC | 自駕車、工業控制、醫療 |
| 🔵 藍色 | eMBB | AR演唱會、影音串流 |
| 🟢 綠色 | mMTC | IoT 裝置、感測器 |

> **注意**：前端採用 2D SVG 城市俯視圖（非 Three.js 3D），降低開發複雜度，聚焦於流量行為的可視化，而非城市渲染。Three.js 留作 V2 選項。

---

## 技術棧（Tech Stack）

### Frontend

| 工具 | 用途 |
|---|---|
| React | UI 框架 |
| D3.js | Packet flow 動畫、即時 dashboard |
| SVG | 城市俯視地圖 |
| TailwindCSS | UI Styling |
| WebSocket | 接收 State Bridge 即時事件 |

### Backend / Platform

| 工具 | 用途 |
|---|---|
| FastAPI | Event Engine API、Metrics Proxy |
| Python kubernetes client | State Bridge（K8s Pod watcher） |
| WebSocket | 即時狀態推送至前端 |

### 5G Networking

| 工具 | 用途 |
|---|---|
| free5GC | 5G Core Network（AMF / SMF / UPF / NRF / AUSF / PCF） |
| UERANSIM | 虛擬 gNB + UE（不需實體硬體） |
| iperf3 | Traffic 產生（由 Event Engine 觸發） |
| gtp5g | GTP kernel module（UPF 依賴） |

### Cloud / Infra

| 工具 | 用途 |
|---|---|
| AWS EKS | 主要 K8s 環境，承載 free5GC |
| AWS EC2 (worker nodes) | EKS node，需自訂 AMI 載入 gtp5g |
| AWS S3 + CloudFront | React 前端靜態托管 |
| AWS API Gateway | API + WebSocket entry point |
| AWS CloudWatch | Node / EKS 層 metrics |
| Terraform | Infrastructure as Code |
| Helm | free5GC + monitoring stack 部署 |

### Observability

| 工具 | 用途 |
|---|---|
| Prometheus | Metrics 收集（UPF CPU / network / session） |
| Prometheus Adapter | 將 custom metrics 暴露給 K8s HPA |
| Grafana | 內部 metrics dashboard |
| kube-state-metrics | Pod / HPA 狀態 metrics |

---

## 開發路線圖

### Phase 1：環境驗證（2 週）

目標：確認整條技術鏈可行

- [ ] 本地 kind / minikube 跑通 free5GC Helm Chart
- [ ] UERANSIM gNB + UE 成功連上 free5GC
- [ ] iperf3 產生流量，確認 UPF 有 GTP 封包通過
- [ ] 驗證 gtp5g module 在非生產 K8s 環境的載入方式
- [ ] HPA CPU-based 設定，確認 iperf3 能觸發 scale

### Phase 2：State Bridge + 基礎前端（2 週）

目標：真實 K8s 事件能到達前端

- [ ] State Bridge 實作（K8s watcher → WebSocket）
- [ ] React 基礎框架，SVG 城市地圖骨架
- [ ] WebSocket 接收 Pod 事件，節點狀態更新
- [ ] D3.js 基礎 packet flow 動畫（三色）

### Phase 3：事件系統整合（2 週）

目標：按下按鈕 → 真實流量 → 真實 scaling → 前端更新

- [ ] Event Engine API（FastAPI）
- [ ] 五種城市事件對應 UERANSIM 流量腳本
- [ ] Prometheus metrics → 前端 dashboard
- [ ] 完整端對端測試

### Phase 4：上 EKS + 穩定化（2 週）

目標：生產環境部署

- [ ] 自訂 EKS AMI（含 gtp5g kernel module）
- [ ] Terraform EKS 基礎設施
- [ ] 解決 UPF scale out 的 N4 重連問題
- [ ] 壓力測試，調整 HPA 閾值
- [ ] CloudFront + API Gateway 部署

### Phase 5：V2 優化（之後）

- [ ] Custom metrics HPA（network throughput-based）
- [ ] 工廠 Slice 獨立 namespace 隔離展示
- [ ] 多 gNB 展示（地理分散）
- [ ] Three.js 3D 城市（選項）
- [ ] EKS 換 EKS Anywhere 展示 Edge 概念

---

## 專案願景

> 「讓看不見的網路，變成能被觀察的城市。」

5G CityVerse 不是動畫模擬器。

每一個在城市地圖上亮起的節點，都對應一個真實啟動的 K8s Pod。
每一條在螢幕上流動的 packet，都來自 UERANSIM 真實產生的流量。
每一次 UPF 擴縮，都是 K8s HPA 根據真實 metrics 做出的決策。

這是 5G Cloud-native 網路行為的真實數位分身，而不是概念示意圖。
