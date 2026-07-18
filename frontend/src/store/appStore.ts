import { create } from 'zustand'
import type {
  AgentDecision,
  AgentDecisionRecord,
  ActiveScenario,
  CityEventType,
  ComponentPods,
  Free5gcStatus,
  NetworkMetrics,
  NetworkSnapshot,
  PacketFlow,
  PodEvent,
  RuntimePrimeStatus,
  SliceStatus,
} from '../types'
import { normalizeNetworkSnapshot, snapshotFromMetrics } from '../services/networkSnapshot'

export type SliceStrategy = 'none' | 'static' | 'ai'

interface AppState {
  // Active city event
  activeEvent: CityEventType | null
  activeScenarios: ActiveScenario[]
  isSimulating: boolean

  // Network slices
  slices: SliceStatus[]

  // Pod / K8s state
  pods: ComponentPods[]

  // Metrics
  metrics: NetworkMetrics | null
  metricsHistory: NetworkMetrics[]

  // AI Agent
  agentDecision: AgentDecision | null
  agentDecisionHistory: AgentDecisionRecord[]
  roundReportReady: boolean
  agentLog: string[]
  runtimePrime: RuntimePrimeStatus | null
  orchestrationStage: string
  isReportGenerating: boolean
  reportRequestId: number

  // D3 packet flows
  packetFlows: PacketFlow[]
  networkSnapshot: NetworkSnapshot | null
  previousNetworkSnapshot: NetworkSnapshot | null
  snapshotTransitionStartedAt: number
  snapshotTransitionDurationMs: number

  // free5GC live status
  free5gcStatus: Free5gcStatus | null

  // WebSocket connectivity
  wsConnected: boolean
  runtimeBusy: boolean

  // Slicing strategy selected while configuring the scenario. Once the
  // backend accepts a round, submittedSliceStrategy is the immutable source
  // of truth for that round.
  sliceStrategy: SliceStrategy
  submittedSliceStrategy: SliceStrategy | null

  // Actions
  setActiveEvent: (event: CityEventType | null) => void
  addActiveScenario: (scenario: ActiveScenario) => void
  syncActiveScenarioWindow: (executionId: string, startedAt: number, endsAt: number) => void
  removeActiveScenario: (event: CityEventType) => void
  pruneActiveScenarios: (now?: number) => void
  setSimulating: (v: boolean) => void
  updateSlices: (slices: SliceStatus[]) => void
  applyPodEvent: (e: PodEvent) => void
  updateMetrics: (m: NetworkMetrics) => void
  setNetworkSnapshot: (snapshot: NetworkSnapshot) => void
  setAgentDecision: (d: AgentDecision | null) => void
  beginAgentRound: () => void
  recordAgentDecision: (record: AgentDecisionRecord) => void
  clearAgentDecisionHistory: () => void
  setRoundReportReady: (ready: boolean) => void
  setRuntimePrime: (status: RuntimePrimeStatus | null) => void
  setOrchestrationStage: (stage: string) => void
  setReportGenerating: (v: boolean) => void
  requestReport: () => void
  updateAgentAction: (agentName: string, actionIndex: number, status: AgentDecision['actions'][0]['status'], httpStatus?: number) => void
  appendAgentLog: (msg: string) => void
  setFree5gcStatus: (status: Free5gcStatus) => void
  setWsConnected: (v: boolean) => void
  setRuntimeBusy: (v: boolean) => void
  setSliceStrategy: (strategy: SliceStrategy) => void
  reset: () => void
}

const DEFAULT_SLICES: SliceStatus[] = [
  { sst: 1, type: 'eMBB',  sd: '000001', load: 0, sessions: 0, trend: 'stable' },
  { sst: 2, type: 'URLLC', sd: '000002', load: 0, sessions: 0, trend: 'stable' },
  { sst: 3, type: 'mMTC',  sd: '000004', load: 0, sessions: 0, trend: 'stable' },
  { sst: 4, type: 'V2X',   sd: '000005', load: 0, sessions: 0, trend: 'stable' },
]

const DEFAULT_PODS: ComponentPods[] = [
  { component: 'UPF',  pods: [], desired: 0 },
  { component: 'AMF',  pods: [], desired: 0 },
  { component: 'SMF',  pods: [], desired: 0 },
  { component: 'NEF',  pods: [], desired: 0 },
  { component: 'PCF',  pods: [], desired: 0 },
  { component: 'NSSF', pods: [], desired: 0 },
  { component: 'NRF',  pods: [], desired: 0 },
  { component: 'UDR',  pods: [], desired: 0 },
  { component: 'AUSF', pods: [], desired: 0 },
  { component: 'UERANSIM', pods: [], desired: 0 },
  { component: 'IPERF3', pods: [], desired: 0 },
]

function defaultMetrics(): NetworkMetrics {
  return {
    upfCpuPercent: 0,
    upfPodCount: 0,
    amfPodCount: 0,
    gtpPacketsPerSec: 0,
    pduSessionCount: 0,
    latencyMs: 0,
    throughputMbps: 0,
    timestamp: Date.now(),
    dataSource: 'unavailable',
    podComponents: DEFAULT_PODS,
  }
}

function normalizeMetrics(metrics: Partial<NetworkMetrics> | undefined): NetworkMetrics {
  const fallback = defaultMetrics()
  const merged = { ...fallback, ...(metrics || {}) }
  return {
    ...merged,
    upfCpuPercent: Number(merged.upfCpuPercent || 0),
    upfPodCount: Number(merged.upfPodCount || 0),
    amfPodCount: Number(merged.amfPodCount || 0),
    gtpPacketsPerSec: Number(merged.gtpPacketsPerSec || 0),
    pduSessionCount: Number(merged.pduSessionCount || 0),
    latencyMs: Number(merged.latencyMs || 0),
    throughputMbps: Number(merged.throughputMbps || 0),
    timestamp: Number(merged.timestamp || Date.now()),
  }
}

function normalizeSlices(slices: SliceStatus[] | undefined): SliceStatus[] {
  if (!Array.isArray(slices)) return DEFAULT_SLICES
  const bySst = new Map(slices.map((slice) => [slice.sst, slice]))
  return DEFAULT_SLICES.map((fallback) => ({ ...fallback, ...(bySst.get(fallback.sst) || {}) }))
}

function syncComponentCount(pods: ComponentPods[], component: string, count: number): ComponentPods[] {
  const safeCount = Math.max(Math.floor(count || 0), 0)
  return pods.map((c) => {
    if (c.component !== component) return c
    return { ...c, desired: safeCount }
  })
}

function syncPodsFromMetrics(pods: ComponentPods[], metrics: NetworkMetrics): ComponentPods[] {
  return syncComponentCount(
    syncComponentCount(pods, 'UPF', metrics.upfPodCount),
    'AMF',
    metrics.amfPodCount
  )
}

function mergePodComponents(podComponents: ComponentPods[] | undefined, metrics: NetworkMetrics): ComponentPods[] {
  if (!podComponents) return syncPodsFromMetrics(DEFAULT_PODS, metrics)
  const byComponent = new Map(podComponents.map((component) => [component.component, component]))
  const defaults = DEFAULT_PODS.map((fallback) => {
    const actual = byComponent.get(fallback.component)
    if (!actual) return { ...fallback }
    return {
      component: actual.component,
      pods: actual.pods,
      desired: actual.pods.filter((pod) => pod.phase === 'Running').length,
    }
  })
  const extras = podComponents
    .filter((component) => component.component !== 'UNKNOWN' && !DEFAULT_PODS.some((fallback) => fallback.component === component.component))
    .map((component) => ({
      component: component.component,
      pods: component.pods,
      desired: component.pods.filter((pod) => pod.phase === 'Running').length,
    }))
  return [...defaults, ...extras]
}

function packetFlowsFromSnapshot(snapshot: NetworkSnapshot | null): PacketFlow[] {
  return snapshot?.edges.map((edge) => ({
    id: edge.id,
    sourceNodeId: edge.sourceNodeId,
    targetNodeId: edge.targetNodeId,
    sliceType: edge.sliceType,
    plane: edge.plane,
    protocol: edge.protocol,
    scenario: edge.scenario,
    active: edge.active,
    bandwidthMbps: edge.throughputMbps,
    throughputMbps: edge.throughputMbps,
    uplinkMbps: edge.uplinkMbps,
    downlinkMbps: edge.downlinkMbps,
    latencyMs: edge.latencyMs,
    fiveQi: edge.fiveQi,
    evidenceCount: edge.evidenceCount,
    lastObservedAt: edge.lastObservedAt,
    lastObservedEpochMillis: edge.lastObservedEpochMillis,
    packetLossPercent: edge.packetLossPercent,
    upfCongestionPercent: edge.upfCongestionPercent,
    qosFlows: edge.qosFlows,
  })) ?? []
}

export const useAppStore = create<AppState>((set) => ({
  activeEvent: null,
  activeScenarios: [],
  isSimulating: false,
  slices: DEFAULT_SLICES,
  pods: DEFAULT_PODS,
  metrics: null,
  metricsHistory: [],
  agentDecision: null,
  agentDecisionHistory: [],
  roundReportReady: false,
  agentLog: [],
  runtimePrime: null,
  orchestrationStage: 'idle',
  isReportGenerating: false,
  reportRequestId: 0,
  packetFlows: [],
  networkSnapshot: null,
  previousNetworkSnapshot: null,
  snapshotTransitionStartedAt: 0,
  snapshotTransitionDurationMs: 900,
  free5gcStatus: null,
  wsConnected: false,
  runtimeBusy: false,
  sliceStrategy: 'none',
  submittedSliceStrategy: null,

  setActiveEvent: (event) => set({ activeEvent: event }),
  addActiveScenario: (scenario) =>
    set((state) => {
      const activeScenarios = [
        ...state.activeScenarios.filter((item) => item.type !== scenario.type),
        scenario,
      ].sort((a, b) => a.startedAt - b.startedAt)
      return {
        activeScenarios,
        activeEvent: scenario.type,
      }
    }),
  syncActiveScenarioWindow: (executionId, startedAt, endsAt) =>
    set((state) => {
      if (!Number.isFinite(startedAt) || !Number.isFinite(endsAt) || endsAt <= startedAt) return state
      return {
        activeScenarios: state.activeScenarios.map((item) =>
          item.executionId === executionId ? { ...item, startedAt, endsAt } : item
        ),
      }
    }),
  removeActiveScenario: (event) =>
    set((state) => {
      const activeScenarios = state.activeScenarios.filter((item) => item.type !== event)
      const activeEvent = activeScenarios.length > 0 ? activeScenarios[activeScenarios.length - 1].type : null
      return {
        activeScenarios,
        activeEvent,
      }
    }),
  pruneActiveScenarios: (now = Date.now()) =>
    set((state) => {
      // The backend owns the event lifecycle while orchestration is running.
      // Keeping the scenario visible avoids racing a client-side timer against
      // runtime priming, AI planning, and the terminal status poll.
      if (state.isSimulating) return state
      const activeScenarios = state.activeScenarios.filter((item) => item.endsAt > now)
      const activeEvent = activeScenarios.length > 0 ? activeScenarios[activeScenarios.length - 1].type : null
      return {
        activeScenarios,
        activeEvent,
      }
    }),
  setSimulating: (v) => set({ isSimulating: v }),

  updateSlices: (slices) =>
    set({ slices }),

  applyPodEvent: (e) =>
    set((state) => {
      const existingPods = state.pods.some((c) => c.component === e.component)
        ? state.pods
        : [...state.pods, { component: e.component, pods: [], desired: 0 }]
      const pods = existingPods.map((c) => {
        if (c.component !== e.component) return c
        let updated = [...c.pods]
        if (e.event === 'ADDED') {
          if (!updated.find((p) => p.name === e.pod)) {
            updated.push({ name: e.pod, phase: e.phase })
          }
        } else if (e.event === 'MODIFIED') {
          updated = updated.map((p) => p.name === e.pod ? { ...p, phase: e.phase } : p)
        } else if (e.event === 'DELETED') {
          updated = updated.filter((p) => p.name !== e.pod)
        }
        return { ...c, pods: updated }
      })
      return { pods }
    }),

  updateMetrics: (m) =>
    set((state) => {
      const metrics = normalizeMetrics(m)
      const networkSnapshot = snapshotFromMetrics(metrics, state.slices)
      return {
        metrics,
        metricsHistory: [...state.metricsHistory.slice(-59), metrics],
        pods: metrics.podComponents ? mergePodComponents(metrics.podComponents, metrics) : syncPodsFromMetrics(state.pods, metrics),
        previousNetworkSnapshot: state.networkSnapshot,
        networkSnapshot,
        snapshotTransitionStartedAt: Date.now(),
        packetFlows: packetFlowsFromSnapshot(networkSnapshot),
      }
    }),

  setNetworkSnapshot: (snapshot) =>
    set((state) => ({
      previousNetworkSnapshot: state.networkSnapshot,
      networkSnapshot: snapshot,
      snapshotTransitionStartedAt: Date.now(),
      metrics: snapshot.metrics,
      metricsHistory: [...state.metricsHistory.slice(-59), snapshot.metrics],
      slices: snapshot.slices,
      pods: snapshot.metrics.podComponents ? mergePodComponents(snapshot.metrics.podComponents, snapshot.metrics) : state.pods,
      packetFlows: packetFlowsFromSnapshot(snapshot),
    })),

  setAgentDecision: (d) => set({ agentDecision: d, orchestrationStage: d ? 'planning' : 'idle' }),
  beginAgentRound: () => set((state) => ({
    agentDecision: null,
    agentDecisionHistory: [],
    roundReportReady: false,
    agentLog: [],
    runtimePrime: null,
    orchestrationStage: 'queued',
    isReportGenerating: false,
    submittedSliceStrategy: state.sliceStrategy,
  })),
  recordAgentDecision: (record) => set((state) => ({
    agentDecision: record.decision,
    agentDecisionHistory: [
      ...state.agentDecisionHistory.filter((item) => item.executionId !== record.executionId),
      record,
    ].sort((a, b) => a.updatedAt - b.updatedAt),
  })),
  clearAgentDecisionHistory: () => set({ agentDecisionHistory: [], roundReportReady: false }),
  setRoundReportReady: (ready) => set({ roundReportReady: ready }),
  setRuntimePrime: (status) =>
    set({
      runtimePrime: status,
      orchestrationStage: !status
        ? 'idle'
        : status.status === 'success'
        ? 'traffic_observed'
        : status.status === 'running'
          ? 'runtime_priming'
          : status.status,
    }),
  setOrchestrationStage: (stage) => set({ orchestrationStage: stage }),
  setReportGenerating: (v) => set({ isReportGenerating: v }),
  requestReport: () => set((state) => ({ reportRequestId: state.reportRequestId + 1 })),

  updateAgentAction: (agentName, actionIndex, status, httpStatus) =>
    set((state) => {
      if (!state.agentDecision || state.agentDecision.agentName !== agentName) return {}
      const actions = state.agentDecision.actions.map((a, i) =>
        i === actionIndex ? { ...a, status, httpStatus } : a
      )
      return { agentDecision: { ...state.agentDecision, actions } }
    }),

  appendAgentLog: (msg) =>
    set((state) => ({ agentLog: [...state.agentLog.slice(-99), msg] })),

  setFree5gcStatus: (status) =>
    set((state) => {
      if (status?.connected === false) {
        const metrics = defaultMetrics()
        return {
          free5gcStatus: { ...status, metrics, slices: DEFAULT_SLICES },
          metrics,
          metricsHistory: [...state.metricsHistory.slice(-59), metrics],
          slices: DEFAULT_SLICES,
          pods: DEFAULT_PODS,
          packetFlows: [],
        }
      }
      const metrics = normalizeMetrics(status?.metrics)
      const slices = normalizeSlices(status?.slices)
      const networkSnapshot = status?.networkSnapshot
        ? normalizeNetworkSnapshot(status.networkSnapshot, metrics, slices)
        : snapshotFromMetrics(metrics, slices)
      const podComponents = status?.podComponents || metrics.podComponents
      return {
        free5gcStatus: { ...status, metrics, slices },
        metrics,
        metricsHistory: [...state.metricsHistory.slice(-59), metrics],
        slices,
        pods: podComponents
          ? mergePodComponents(podComponents, metrics)
          : syncPodsFromMetrics(state.pods, metrics),
        previousNetworkSnapshot: state.networkSnapshot,
        networkSnapshot,
        snapshotTransitionStartedAt: Date.now(),
        packetFlows: packetFlowsFromSnapshot(networkSnapshot),
      }
    }),

  setWsConnected: (v) => set({ wsConnected: v }),
  setRuntimeBusy: (v) => set({ runtimeBusy: v }),
  setSliceStrategy: (sliceStrategy) => set((state) =>
    state.orchestrationStage === 'idle' ? { sliceStrategy } : state
  ),

  reset: () =>
    set({
      activeEvent: null,
      activeScenarios: [],
      isSimulating: false,
      slices: DEFAULT_SLICES,
      pods: DEFAULT_PODS,
      metrics: null,
      metricsHistory: [],
      agentDecision: null,
      agentDecisionHistory: [],
      roundReportReady: false,
      agentLog: [],
      runtimePrime: null,
      orchestrationStage: 'idle',
      isReportGenerating: false,
      reportRequestId: 0,
      packetFlows: [],
      networkSnapshot: null,
      previousNetworkSnapshot: null,
      snapshotTransitionStartedAt: 0,
      free5gcStatus: null,
      wsConnected: false,
      sliceStrategy: 'none',
      submittedSliceStrategy: null,
    }),
}))
