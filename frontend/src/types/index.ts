// City Events
export type CityEventType =
  | 'concert'
  | 'typhoon'
  | 'accident'
  | 'medical'
  | 'iot_surge'

export interface CityEvent {
  type: CityEventType
  label: string
  description: string
  severity: 1 | 2 | 3 | 4 | 5
  sliceType: SliceType
  icon: string
}

// Network Slices
export type SliceType = 'eMBB' | 'URLLC' | 'mMTC' | 'V2X'

export interface SliceStatus {
  sst: 1 | 2 | 3 | 4
  type: SliceType
  sd: string
  load: number          // 0–100 %
  sessions: number
  trend: 'up' | 'down' | 'stable'
}

// K8s / Pod State
export type PodPhase = 'Pending' | 'Running' | 'Terminating'

export interface PodEvent {
  event: 'ADDED' | 'MODIFIED' | 'DELETED'
  pod: string
  phase: PodPhase
  component: 'UPF' | 'AMF' | 'SMF' | 'NEF' | 'PCF' | 'NSSF'
  namespace: string
  timestamp: string
}

export interface ComponentPods {
  component: string
  pods: { name: string; phase: PodPhase }[]
  desired: number
}

// Metrics 
export interface NetworkMetrics {
  upfCpuPercent: number
  upfPodCount: number
  amfPodCount: number
  amfCpuPercent?: number
  registeredUeCount?: number
  gtpPacketsPerSec: number
  pduSessionCount: number
  latencyMs: number
  throughputMbps: number
  uplinkMbps?: number
  downlinkMbps?: number
  timestamp: number
  dataSource?: 'prometheus' | 'estimated' | 'simulated'
}

export interface Free5gcStatus {
  connected: boolean
  source: string
  subscriberCount?: number
  eventSubscriberCount?: number
  registeredUeCount?: number
  profileCount?: number
  subscribers: { plmnID: string; ueId: string; gpsi?: string }[]
  eventSubscribers: { plmnID: string; ueId: string; gpsi?: string }[]
  registeredUes?: { Supi?: string; CmState?: string; AccessType?: string; PduSessions?: unknown }[]
  profiles?: string[]
  metrics: NetworkMetrics
  slices: SliceStatus[]
  checkedAt: string
  error?: string
}

// AI Agent
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'
export type AgentStatus = 'idle' | 'analyzing' | 'executing' | 'done' | 'error'

export interface AgentAction {
  type: 'nef_pfd' | 'nef_traffic_influence' | 'nef_qos' | 'k8s_hpa' | 'ueransim' | 'iperf3' | 'prometheus' | 'free5gc_subscriber'
  description: string
  api?: string
  status: 'pending' | 'running' | 'success' | 'failed'
  httpStatus?: number
}

export interface AgentDecision {
  agentName: string
  riskLevel: RiskLevel
  decision: string
  actions: AgentAction[]
  expectedOutcome: string
  score: number          // 0–100
  startedAt: string
  completedAt?: string
}

// Packet Flow (D3 animation)
export interface PacketFlow {
  id: string
  sourceNodeId: string
  targetNodeId: string
  sliceType: SliceType
  active: boolean
  // Flow characteristics for multi-dimensional animation
  bandwidthMbps?: number     // 0-1000+ (controls line width & particle density)
  latencyMs?: number          // 0-500 (controls particle speed & jitter)
  fiveQi?: number             // 1-9 (controls priority & flicker)
  packetLossPercent?: number  // 0-100 (controls visibility & ghost particles)
  upfCongestionPercent?: number // 0-100 (controls path color shift)
  qosFlows?: Array<{          // For multi-layer flow visualization
    fiveQi: number
    bitrateMbps: number
    priority: number
  }>
}

// City Nodes (SVG)
export interface CityNode {
  id: string
  label: string
  x: number
  y: number
  type: 'district' | 'core' | 'upf' | 'gnb'
  activeSlices: SliceType[]
}

// WebSocket Messages
export type WsMessageType =
  | 'pod_event'
  | 'metrics_update'
  | 'agent_decision'
  | 'agent_action'
  | 'slice_update'
  | 'event_started'
  | 'free5gc_status'

export interface WsMessage {
  type: WsMessageType
  payload: unknown
}
