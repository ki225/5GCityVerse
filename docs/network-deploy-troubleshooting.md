# destroy/deploy 網路排查筆記：EKS、Multus、free5GC

本文整理 2026-06-26 `./scripts/deploy.sh` 卡在 Kubernetes/free5GC 網路部署時的完整排查。這份筆記的目的不是只記錄一次修 bug，而是把背後的 Kubernetes、EKS、CNI、Multus、Helm 原理寫清楚，避免之後 `destroy` 後重新 `deploy` 又踩同一組問題。

## 1. 背景問題

部署流程大致如下：

```text
terraform apply
  -> update kubeconfig
  -> install Multus
  -> install gtp5g
  -> helm install free5GC
  -> helm install UERANSIM
  -> resolve free5GC WebUI LoadBalancer URL
  -> update Lambda/frontend outputs
```

最初看到的錯誤是：

```text
Waiting for daemon set "kube-multus-ds" rollout to finish: 0 of 3 updated pods are available...
error: timed out waiting for the condition
```

後續又看到 Helm 停在：

```text
Release "free5gc" does not exist. Installing it now.
```

這句 Helm 訊息本身不是錯誤。它只表示 release 不存在，所以 Helm 正在走初次安裝。真正問題藏在 Helm 等待期間建立出的 pod/events。

## 2. 排查時間線

### 2.1 kubeconfig 指向舊的 EKS API endpoint

第一層錯誤不是 Multus，而是本機 kubeconfig 還指向前一個 EKS cluster endpoint：

```text
Unable to connect to the server:
dial tcp: lookup 61BD...yl4.ap-northeast-1.eks.amazonaws.com: no such host
```

用 AWS 查目前 cluster endpoint 後發現實際 endpoint 已變成：

```text
https://CE06...gr7.ap-northeast-1.eks.amazonaws.com
```

原因是 EKS cluster destroy 後再 deploy，cluster 名稱可以相同，但 API server endpoint DNS name 會重新產生。若 kubeconfig 沒有被強制更新並切到正確 context，`kubectl` 仍會打舊 DNS。

修正：

- `scripts/lib.sh:update_kubeconfig` 會呼叫 `aws eks update-kubeconfig --alias "$EKS_CLUSTER_NAME" --user-alias "$EKS_CLUSTER_NAME"`。
- 更新後強制 `kubectl config use-context "$EKS_CLUSTER_NAME"`。
- 讀取目前 kubeconfig endpoint，必須等於 `aws eks describe-cluster --query cluster.endpoint`。
- `check_kubernetes_api` 會用 `/version` 驗證 API server 真的可達。

背後原理：

- kubeconfig 是本機檔案，不會因 Terraform destroy 自動清除舊 endpoint。
- EKS API endpoint 是 cluster 的 control-plane DNS，重建 cluster 會換 hostname。
- `kubectl config current-context` 正確不代表 server endpoint 正確，兩者都要檢查。

### 2.2 Multus DaemonSet image tag 不存在

kubeconfig 修好後，Multus DaemonSet 仍卡在：

```text
Init:ImagePullBackOff
failed to resolve reference ".../eks/multus-cni:v4.1.2-eksbuild.1": not found
```

原因是 manifest pin 到不存在的 AWS ECR image tag：

```text
602401143452.dkr.ecr.ap-northeast-1.amazonaws.com/eks/multus-cni:v4.1.2-eksbuild.1
```

修正：

- 預設 image 改為可拉取且含 daemon binary 的 upstream thick image：

```text
ghcr.io/k8snetworkplumbingwg/multus-cni:v4.1.2-thick
```

- `scripts/lib.sh` 使用 `MULTUS_CNI_IMAGE` render `k8s/multus-daemonset.yaml`，需要 private mirror 時可用環境變數覆寫。

背後原理：

- EKS managed nodes 可以直接拉公開 registry image，但 image tag 必須存在。
- Multus 有 thin/thick 模式。現有 DaemonSet 使用 `multus-daemon`，必須使用 thick image。

### 2.3 Multus thick image 啟動參數與 volume 不完整

改成 thick image 後，image pull 成功，但 pod 變成短暫 Ready 後 crash。原因是原 DaemonSet 只做了簡單 `cp multus`，但 thick daemon 需要：

- `daemon-config.json`
- `install_multus -t thick`
- host `/run` socket path
- host root、CNI config dir、kubelet/netns/CNI state volumes
- RBAC `list/watch pods`

修正：

- `k8s/multus-daemonset.yaml` 補上 `multus-daemon-config` ConfigMap。
- init container 改用 `install_multus -d /host/opt/cni/bin -t thick`。
- 補齊 `/host/run`、`/hostroot`、`/var/lib/kubelet`、`/run/netns` 等 mount。
- RBAC 增加 pod `list/watch`。

背後原理：

- thin mode 的 Multus CNI binary 直接在 kubelet CNI call path 中執行。
- thick mode 使用 shim + daemon，CNI request 會透過 socket 交給 daemon 處理。
- daemon 要看到 host network namespace、CNI config、kubelet pod network state，否則即使 image 正確也會退出。

### 2.4 free5GC Helm 卡住的真正原因：host 沒有 eth1

free5GC 安裝進入 `pending-install`，pod 狀態顯示：

```text
free5gc-free5gc-amf-amf   Init:0/1
free5gc-free5gc-upf-upf   ContainerCreating
```

events 顯示關鍵錯誤：

```text
plugin type="ipvlan" failed (add):
failed to lookup master "eth1": Link not found
```

當時 `k8s/free5gc-eks-values.yaml` 啟用了：

```yaml
global:
  amf:
    multus:
      enabled: true
      n2network:
        type: ipvlan
        masterIf: eth1
  upf:
    multus:
      enabled: true
      n3network:
        type: ipvlan
        masterIf: eth1
```

free5GC Helm chart 因此自動產生 NAD 和 pod annotation：

```yaml
k8s.v1.cni.cncf.io/networks: n2network-free5gc-free5gc-amf
k8s.v1.cni.cncf.io/networks: n3network-free5gc-free5gc-upf, n4network..., n6network...
```

但目前 Terraform 只建立了 N2/N3/N4/N6 subnet，沒有把一張穩定、未被 AWS VPC CNI 管理的 ENI attach 到每個目標 node，node OS 裡自然沒有可給 ipvlan 使用的 `eth1`。

修正：

- `k8s/free5gc-eks-values.yaml` 將 chart-level Multus 關閉：

```yaml
global:
  amf:
    multus:
      enabled: false
  upf:
    multus:
      enabled: false
```

- AMF 保留 NGAP NodePort service，UERANSIM 透過 service 連 AMF。
- UPF 保留在 `plane=user-plane` node 上，gtp5g kernel module 仍由 `gtp5g-installer` 安裝。
- `scripts/lib.sh` 增加 guard：如果 values 又把 `global.amf.multus.enabled` 或 `global.upf.multus.enabled` 設為 `true`，deploy 會在 Helm 前停止，除非明確設定 `ALLOW_FREE5GC_CHART_MULTUS=true`。

背後原理：

- Multus 只負責讓 pod 可以有第二張、第三張網卡。
- ipvlan 的 `master` 必須是 host namespace 已存在的 Linux interface。
- EKS managed node 的 AWS VPC CNI 會管理 pod primary network 與 secondary ENI/IP。它不等於「提供一張可任意拿來做 Multus ipvlan master 的 eth1」。
- 只建立 subnet 不會讓 EC2 instance 多出網卡；還需要 ENI attach、OS 命名穩定化、路由與安全群組規則。

### 2.5 Helm pending release 造成重跑部署卡住

第一次 Helm install timeout 後，release 狀態留在：

```text
STATUS: pending-install
```

這會讓後續 `helm upgrade --install` 行為變得不乾淨，像是 release lock 或半套資源還在。

修正：

- `scripts/lib.sh:recover_pending_helm_release` 會偵測：

```text
pending-install
pending-upgrade
pending-rollback
```

- 若命中，就先：

```bash
helm -n free5gc uninstall free5gc --wait --timeout 10m
```

再重新安裝。

背後原理：

- Helm release 狀態儲存在 cluster secret/configmap 中。
- `--wait` timeout 不代表 Kubernetes 資源完全消失；它常留下 pending release metadata。
- 清掉 pending release 比直接反覆 upgrade 更可預期。

## 3. 現在的穩定部署模式

目前預設模式是：

```text
EKS managed node
  -> AWS VPC CNI for pod eth0
  -> Multus CNI installed but not used by free5GC chart
  -> AMF NGAP exposed through NodePort service
  -> UERANSIM gNB reaches AMF through service
  -> UPF runs on user-plane node with gtp5g kernel module
```

這個模式刻意不依賴 host `eth1`。原因是 destroy/deploy 後 EKS managed node、ENI attachment、interface naming 都可能改變；沒有自動化前，把 Helm chart Multus 打開就是不穩定的。

## 4. destroy 後重新 deploy 的防呆點

目前防呆包含：

1. kubeconfig endpoint guard
   - 每次 `deploy.sh/start.sh/stop.sh` 都更新 kubeconfig。
   - 使用穩定 context alias。
   - 驗證 kubeconfig endpoint 等於 AWS 當前 endpoint。

2. Kubernetes API reachability guard
   - `kubectl get --raw=/version` 失敗時直接報明 endpoint 和網路提示。

3. Multus image guard
   - 預設使用存在且支援 daemon 的 `ghcr.io/k8snetworkplumbingwg/multus-cni:v4.1.2-thick`。
   - `MULTUS_CNI_IMAGE` 可覆寫，但 manifest 不再 hard-code 不存在的 ECR tag。

4. free5GC Multus guard
   - 預設 chart-level Multus disabled。
   - 若有人又改成 enabled，`install_free5gc` 會在 Helm 前失敗並說明缺少 stable unmanaged host `eth1`。

5. Helm pending recovery
   - 自動清理 `pending-install/pending-upgrade/pending-rollback` release。

## 5. 若未來真的要啟用 Multus N2/N3/N4/N6

不要只把 `global.*.multus.enabled` 改回 `true`。需要先補齊下列能力：

1. Terraform 建立或管理每個目標 node 所需的 dedicated ENI。
2. ENI attach 到正確 AZ 的 target nodes。
3. OS 內 interface name 固定，例如確保 Multus master 一定叫 `eth1` 或改 values 指到實際名稱。
4. node bootstrap 驗證 interface 存在：

```bash
ip link show eth1
```

5. 路由與 security group 允許 N2 SCTP、N3 GTP-U UDP/2152、N4 PFCP UDP/8805、N6 egress。
6. 明確處理 AWS VPC routing：pod 內 ipvlan IP 是否能被 VPC 路由識別，或是否需要 secondary IP/route/controller。
7. Helm 前加入 preflight，確認所有 AMF/UPF 可排程 node 都有目標 master interface。

完成上述後，才設定：

```bash
ALLOW_FREE5GC_CHART_MULTUS=true
```

並把 values 中 chart-level Multus 打開。

## 6. 常用排查命令

```bash
# kubeconfig 是否指向最新 EKS endpoint
aws eks describe-cluster --name 5gcityverse-prod-eks --query 'cluster.endpoint' --output text
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
kubectl get --raw=/version

# Multus 狀態
kubectl -n kube-system get ds kube-multus-ds -o wide
kubectl -n kube-system get pods -l name=multus -o wide
kubectl -n kube-system describe pod -l name=multus

# free5GC Helm/pod 狀態
helm -n free5gc status free5gc
kubectl -n free5gc get pods -o wide
kubectl -n free5gc get events --sort-by=.lastTimestamp | tail -80

# 檢查 AMF/UPF 是否又被掛上 Multus annotation
kubectl -n free5gc get deploy free5gc-free5gc-amf-amf free5gc-free5gc-upf-upf \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.metadata.annotations.k8s\.v1\.cni\.cncf\.io/networks}{"\n"}{end}'
```

## 7. 判斷問題層的方法

| 現象 | 層級 | 代表意義 |
| --- | --- | --- |
| `no such host` on `*.eks.amazonaws.com` | kubeconfig / DNS | kubeconfig 指到舊 EKS endpoint 或 DNS/network 不通 |
| `ImagePullBackOff` | image registry | image registry/tag 不存在或無權限 |
| Multus pod crash | DaemonSet spec | thick daemon 參數、config、host mounts、RBAC 不完整 |
| `failed to lookup master "eth1"` | host networking / NAD | NAD 要求的 host interface 不存在 |
| Helm `pending-install` | Helm release state | 前次 install/upgrade timeout 留下半套 release metadata |

核心判斷原則：`helm install` 卡住時，不要只看 Helm 的最後一行，應立即看 `kubectl get pods` 與 `kubectl get events`。Kubernetes event 通常會直接指出 CNI、image、scheduler、volume 或 readiness 的真正根因。
