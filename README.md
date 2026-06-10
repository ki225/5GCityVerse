# 5GCityVerse

> An AI-Native B5G Smart City Simulator — Multi-Agent driven network resource orchestration built on free5GC, AWS Bedrock, and EKS.

Every animation on the city map reflects a real infrastructure event: a real K8s Pod scaling, a real free5GC NEF API call, a real HPA decision.

---

## What It Does

Users trigger city events (typhoon, AR concert, traffic accident, medical emergency). The system responds by:

1. **Bedrock Supervisor Agent** analyzes current network state via Prometheus / NWDAF
2. Sub-agents (Traffic / Medical / Disaster) call **free5GC NEF APIs** to reconfigure slices and QoS
3. **UERANSIM** generates real traffic → UPF CPU rises → **K8s HPA** scales pods
4. **Kinesis → WebSocket** pushes live state to the React dashboard

---

## Architecture

```
Browser (React + D3.js + SVG City Map)
        │ WebSocket / REST
        ▼
  AWS API Gateway
        │
   ┌────┴────────────────────────┐
   ▼                             ▼
Event Engine (Lambda)    Bedrock Orchestrator (Lambda)
   │ trigger UERANSIM           │ Tool Calls → Lambda → NEF API
   ▼                             ▼
          EKS Cluster
          ├── free5gc namespace
          │     AMF · SMF · UPF (HPA 1~4) · PCF · NSSF
          │     NEF  ◄── Bedrock Agent (VPC)
          │     NWDAF ──► Bedrock Agent (analytics)
          ├── ueransim namespace  (gNB + UE Jobs)
          └── monitoring namespace (Prometheus · Grafana)
```

---

## City Events

| Event | Slice | Key Behavior |
|---|---|---|
| AR Concert | eMBB (SST=1) | UERANSIM 50 UEs × iperf3 → UPF HPA 1→4 |
| Self-Driving Cars | URLLC (SST=2) | NEF Traffic Influence → Edge UPF priority |
| Smart Factory | Industrial URLLC | Isolated namespace, deterministic latency |
| Typhoon / Disaster | mMTC (SST=3) + URLLC | IoT Core → UERANSIM → AMF HPA scale |
| Traffic Accident | V2X (SST=4) | NEF PFD + Traffic Influence → rerouting |

---

## AI Agent Design

- **Supervisor Agent** — receives city event, queries network analytics, decides slice strategy
- **Traffic Agent** — calls `POST /3gpp-traffic-influence/v1/{afId}/subscriptions`
- **Medical Agent** — calls `POST /3gpp-as-session-with-qos/v1/{afId}/subscriptions`
- **Disaster Agent** — calls `POST /3gpp-pfd-management/v1/{afId}/transactions`

All tools are Lambda functions running inside the VPC, calling **free5GC NEF** directly via standard 3GPP TS 29.522 APIs — no fake endpoints.

---

## Tech Stack

| Layer | Tools |
|---|---|
| Frontend | React · D3.js · SVG · TailwindCSS · WebSocket |
| AI | AWS Bedrock (Claude Sonnet 4) · Bedrock Agents · Lambda |
| Orchestration | Step Functions · EventBridge · Kinesis · DynamoDB |
| 5G Core | free5GC (AMF/SMF/UPF/PCF/NEF/NSSF/NWDAF) · UERANSIM · gtp5g |
| Infra | EKS · EC2 (custom AMI w/ gtp5g) · Terraform · Helm |
| Observability | Prometheus · Prometheus Adapter · Grafana · Timestream |


## License

Apache 2.0

