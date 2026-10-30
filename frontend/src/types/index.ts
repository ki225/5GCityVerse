import type { ServiceProfileId } from '../domain/scenarios';
// City Events
export type CityEventType = ServiceProfileId;
export interface ActiveScenario {
    type: CityEventType;
    label: string;
    startedAt: number;
    endsAt: number;
    eventScale: number;
    cityResidents: number;
    executionId?: string;
}
// Network Slices
export type SliceType = 'eMBB' | 'URLLC' | 'mMTC' | 'V2X';
export interface SliceStatus {
    sst: 1 | 2 | 3 | 4;
    type: SliceType;
    sd: string;
    load: number; // 0–100 %
    sessions: number;
    trend: 'up' | 'down' | 'stable';
    throughputMbps?: number;
    dataSource?: NetworkMetrics['dataSource'];
    loadSource?: string;
    evidenceLevel?: string;
    selectionStage?: string;
}
// K8s / Pod State
export type PodPhase = 'Pending' | 'Running' | 'Succeeded' | 'Failed' | 'Unknown' | 'Terminating';
export interface PodEvent {
    event: 'ADDED' | 'MODIFIED' | 'DELETED';
    pod: string;
    phase: PodPhase;
    component: 'UPF' | 'AMF' | 'SMF' | 'NEF' | 'PCF' | 'NSSF' | 'NRF' | 'UDR' | 'AUSF' | 'UERANSIM' | 'IPERF3' | 'UNKNOWN';
    namespace: string;
    timestamp: string;
}
export interface ComponentPods {
    component: string;
    pods: {
        name: string;
        phase: PodPhase;
        reason?: string;
    }[];
    desired: number;
}
// Metrics 
export interface NetworkMetrics {
    upfCpuPercent: number;
    upfPodCount: number;
    amfPodCount: number;
    amfCpuPercent?: number;
    registeredUeCount?: number;
    ueransimActivePods?: number;
    staleRegistrations?: number;
    gtpPacketsPerSec: number;
    pduSessionCount: number;
    latencyMs: number;
    throughputMbps: number;
    uplinkMbps?: number;
    downlinkMbps?: number;
    timestamp: number;
    dataSource?: 'prometheus' | 'eks+prometheus' | 'eks' | 'kubernetes' | 'free5gc' | 'free5gc-oam' | 'eks+iperf3' | 'kubernetes+iperf3' | 'free5gc-oam+iperf3' | 'eks+ueransim-logs' | 'eks+ue-tun-probe' | 'unavailable';
    evidenceLevel?: string;
    unavailableMetrics?: Array<'upfCpuPercent' | 'gtpPacketsPerSec' | 'latencyMs' | string>;
    scenarioTraffic?: Array<{
        scenario: string;
        executionId?: string;
        pod?: string;
        bearerReady?: boolean;
        supi?: string | null;
        throughputMbps?: number | null;
        observedAt?: number;
        transport?: string;
    }>;
    ueTunProbe?: {
        ready: boolean;
        interface?: string;
        target?: string;
        throughputMbps?: number;
        latencyMs?: number;
        packetLossPercent?: number;
        receivedPackets?: number;
        measurementSource?: 'ue-tun-ping' | 'resident-tun-iperf3';
    };
    podComponents?: ComponentPods[];
    componentCpuPercent?: Record<string, number>;
}
export type FlowDirection = 'forward' | 'reverse' | 'idle';
export type FlowPlane = 'user' | 'control';
export interface FlowEdgeState {
    id: string;
    sourceNodeId: string;
    targetNodeId: string;
    sliceType: SliceType;
    plane?: FlowPlane;
    protocol?: string;
    scenario?: string;
    active: boolean;
    throughputMbps: number;
    uplinkMbps: number;
    downlinkMbps?: number;
    latencyMs?: number;
    packetLossPercent?: number;
    upfCongestionPercent?: number;
    fiveQi?: number;
    evidenceCount?: number;
    lastObservedAt?: string;
    lastObservedEpochMillis?: number;
    qosFlows?: Array<{
        fiveQi: number;
        bitrateMbps: number;
        priority: number;
    }>;
}
export interface NetworkSnapshot {
    id: string;
    timestamp: number;
    source: NetworkMetrics['dataSource'] | string;
    metrics: NetworkMetrics;
    slices: SliceStatus[];
    edges: FlowEdgeState[];
}
export interface Free5gcStatus {
    connected: boolean;
    source: string;
    subscriberInventoryStatus?: 'available' | 'unavailable';
    subscriberCount?: number;
    eventSubscriberCount?: number;
    registeredUeCount?: number;
    profileCount?: number;
    subscribers: {
        plmnID: string;
        ueId: string;
        gpsi?: string;
    }[];
    eventSubscribers: {
        plmnID: string;
        ueId: string;
        gpsi?: string;
    }[];
    registeredUes?: {
        Supi?: string;
        CmState?: string;
        AccessType?: string;
        PduSessions?: unknown;
    }[];
    profiles?: string[];
    metrics: NetworkMetrics;
    slices: SliceStatus[];
    networkSnapshot?: NetworkSnapshot;
    podComponents?: ComponentPods[];
    checkedAt: string;
    error?: string;
}
export interface RuntimeCapabilityStatus {
    status?: string;
    configured?: boolean;
    provider?: string;
    model?: string;
    reason?: string;
    detail?: string;
    error?: string;
}
export interface RuntimeStatus {
    mode?: 'cloud' | 'onprem' | string;
    ai?: RuntimeCapabilityStatus;
    kubernetes?: RuntimeCapabilityStatus;
    free5gc?: RuntimeCapabilityStatus;
    checkedAt?: string;
}
// AI Agent
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical';
export type AgentStatus = 'idle' | 'analyzing' | 'executing' | 'done' | 'error';
export interface AgentAction {
    type: 'nef_pfd' | 'nef_traffic_influence' | 'nef_qos' | 'k8s_hpa' | 'ueransim' | 'iperf3' | 'prometheus' | 'free5gc_subscriber' | 'free5gc_qer';
    tool?: string;
    description: string;
    api?: string;
    status: 'pending' | 'running' | 'success' | 'failed' | 'skipped';
    httpStatus?: number;
    because?: string;
    expectedImpact?: string;
    verificationMetric?: string;
    result?: unknown;
}
export interface AgentObservation {
    label: string;
    value: string;
    severity: RiskLevel;
    source: string;
}
export interface AgentPlan {
    name: string;
    rationale: string;
    expectedImpact: string;
    status?: string;
}
export interface RejectedAgentPlan {
    name: string;
    reason: string;
}
export interface AgentVerification {
    metric: string;
    before: string | number;
    target: string | number;
    status: 'pending' | 'pass' | 'fail' | 'passed' | 'failed' | 'degraded';
    passCondition: string;
    pfcpEvidence?: string;
    mechanism?: string;
}
export interface AgentIntent {
    executionId: string;
    profileId: CityEventType;
    intentType: 'one_shot' | 'continuous_control';
    eventScale?: number;
    cityResidents?: number;
    controlLoop?: {
        pollIntervalSeconds: number;
        eventDurationSeconds: number;
        cooldownSeconds: number;
    };
    riskLevel: RiskLevel;
    targetSlice: {
        sst: number;
        sd: string;
        name: SliceType;
        fiveQi: number;
        dnn: string;
    };
    sla: {
        latencyMsMax: number;
        minThroughputMbps: number;
        minPduSessions: number;
        maxUpfCpuPercent: number;
        baselineProtection?: {
            floorMbps: number;
            floorSource: string;
        };
    };
    trafficProfile: string;
    ueIds: string[];
    runtimePrime?: RuntimePrimeStatus;
}
export interface VerificationSummary {
    status: 'passed' | 'failed' | 'degraded' | 'fallback';
    checks?: AgentVerification[];
    adaptationRequired?: boolean;
    checkedAt?: string;
    dataSource?: string;
    error?: string;
}
export interface RuntimePrimeStatus {
    executionId?: string;
    profileId?: CityEventType | string;
    status: 'idle' | 'running' | 'success' | 'traffic_not_observed' | 'error' | 'skipped' | 'cancelled';
    startedAt?: string;
    primedAt?: string;
    primedEpochMillis?: number;
    trafficStartedEpochMillis?: number;
    trafficEndsEpochMillis?: number;
    observedBeforePlanning?: boolean;
    awaitingTrafficRenderAck?: boolean;
    expectedScenarios?: string[];
    observedScenarios?: string[];
    missingScenarios?: string[];
    actions?: string[];
    requestedTrafficProfile?: {
        rateMbps: number;
        aggregateRateMbps?: number;
        estimatedAggregatePps?: number;
        packetBytes: number;
        parallel?: number;
        durationSeconds?: number;
    };
    appliedTrafficProfile?: {
        rateMbps: number;
        aggregateRateMbps?: number;
        estimatedAggregatePps?: number;
        packetBytes: number;
        parallel?: number;
        durationSeconds?: number;
        args?: string[];
    };
    localTrafficCapMbps?: number;
    localTrafficCapPps?: number;
    reason?: string;
    error?: string;
    detail?: string;
}
export interface AgentAdaptation {
    round: number;
    maxRounds: number;
    executed: boolean;
    reason: string;
    action?: unknown;
    postAdaptationVerification?: VerificationSummary;
}
export interface AgentDecision {
    executionId?: string;
    agentName: string;
    riskLevel: RiskLevel;
    decision: string;
    intent?: AgentIntent;
    observations?: AgentObservation[];
    hypotheses?: string[];
    selectedPlan?: AgentPlan;
    rejectedPlans?: RejectedAgentPlan[];
    actions: AgentAction[];
    verification?: AgentVerification[];
    verificationSummary?: VerificationSummary;
    validationReport?: ScenarioValidationReport;
    adaptation?: AgentAdaptation;
    expectedOutcome: string;
    startedAt: string;
    completedAt?: string;
}
export interface AgentDecisionRecord {
    executionId: string;
    profileId: CityEventType;
    status: string;
    decision: AgentDecision;
    updatedAt: number;
}
export interface ScenarioValidationReport {
    scenario: string;
    phase: string;
    baseline_captured: {
        source: string;
        per_slice_throughput_mbps: Record<string, number>;
        total_pdu_sessions: number;
        upf_cpu_percent: number;
    };
    steps_completed: string;
    required_steps: string[];
    nef_apis_called: string[];
    nef_apis_required: string[];
    sla_result: {
        latency_ms: {
            value: number;
            threshold: number;
            passed: boolean;
        };
        throughput_mbps: {
            value: number;
            threshold: number;
            passed: boolean;
            delta_from_baseline: number;
        };
        isolation_check: {
            max_degradation_percent: number;
            passed: boolean;
        };
        status: string;
        data_source: string;
    };
    k8s_scaling_observed: Record<string, number>;
    improvements_vs_previous: string[];
    remaining_issues: string[];
}
// Packet Flow (D3 animation)
export interface PacketFlow {
    id: string;
    sourceNodeId: string;
    targetNodeId: string;
    sliceType: SliceType;
    plane?: FlowPlane;
    protocol?: string;
    scenario?: string;
    active: boolean;
    direction?: FlowDirection;
    // Flow characteristics for multi-dimensional animation
    bandwidthMbps?: number; // 0-1000+ (controls line width & particle density)
    throughputMbps?: number;
    uplinkMbps?: number;
    downlinkMbps?: number;
    latencyMs?: number; // 0-500 (controls particle speed & jitter)
    fiveQi?: number; // 1-9 (controls priority & flicker)
    evidenceCount?: number;
    lastObservedAt?: string;
    lastObservedEpochMillis?: number;
    packetLossPercent?: number; // 0-100 (controls visibility & ghost particles)
    upfCongestionPercent?: number; // 0-100 (controls path color shift)
    qosFlows?: Array<{
        fiveQi: number;
        bitrateMbps: number;
        priority: number;
    }>;
}
// City Nodes (SVG)
export interface CityNode {
    id: string;
    label: string;
    x: number;
    y: number;
    type: 'district' | 'core' | 'upf' | 'gnb';
    activeSlices: SliceType[];
}
// WebSocket Messages
export type WsMessageType = 'pod_event' | 'metrics_update' | 'agent_decision' | 'agent_action' | 'slice_update' | 'event_started' | 'event_blocked' | 'event_reset' | 'runtime_priming' | 'runtime_primed' | 'free5gc_status' | 'network_snapshot';
export interface WsMessage {
    type: WsMessageType | 'ue_status';
    payload: unknown;
}
