import { create } from 'zustand'
import type {
  AgentDecision,
  CityEventType,
  ComponentPods,
  Free5gcStatus,
  NetworkMetrics,
  PacketFlow,
  PodEvent,
  SliceStatus,
} from '../types'

interface AppState {
  // Active city event
  activeEvent: CityEventType | null
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
  agentLog: string[]

  // D3 packet flows
  packetFlows: PacketFlow[]

  // free5GC live status
  free5gcStatus: Free5gcStatus | null

  // Actions
  setActiveEvent: (event: CityEventType | null) => void
  setSimulating: (v: boolean) => void
  updateSlices: (slices: SliceStatus[]) => void
  applyPodEvent: (e: PodEvent) => void
  updateMetrics: (m: NetworkMetrics) => void
  setAgentDecision: (d: AgentDecision) => void
  updateAgentAction: (agentName: string, actionIndex: number, status: AgentDecision['actions'][0]['status'], httpStatus?: number) => void
  appendAgentLog: (msg: string) => void
  setPacketFlows: (flows: PacketFlow[]) => void
  setFree5gcStatus: (status: Free5gcStatus) => void
  reset: () => void
}

const DEFAULT_SLICES: SliceStatus[] = [
  { sst: 1, type: 'eMBB',  sd: '000001', load: 20, sessions: 120,  trend: 'stable' },
  { sst: 2, type: 'URLLC', sd: '000002', load: 10, sessions: 34,   trend: 'stable' },
  { sst: 3, type: 'mMTC',  sd: '000003', load: 15, sessions: 2400, trend: 'stable' },
  { sst: 4, type: 'V2X',   sd: '000004', load: 5,  sessions: 18,   trend: 'stable' },
]

const DEFAULT_PODS: ComponentPods[] = [
  { component: 'UPF',  pods: [{ name: 'upf-0', phase: 'Running' }], desired: 1 },
  { component: 'AMF',  pods: [{ name: 'amf-0', phase: 'Running' }], desired: 1 },
  { component: 'SMF',  pods: [{ name: 'smf-0', phase: 'Running' }], desired: 1 },
  { component: 'NEF',  pods: [{ name: 'nef-0', phase: 'Running' }], desired: 1 },
  { component: 'PCF',  pods: [{ name: 'pcf-0', phase: 'Running' }], desired: 1 },
  { component: 'NSSF', pods: [{ name: 'nssf-0', phase: 'Running' }], desired: 1 },
]

export const useAppStore = create<AppState>((set) => ({
  activeEvent: null,
  isSimulating: false,
  slices: DEFAULT_SLICES,
  pods: DEFAULT_PODS,
  metrics: null,
  metricsHistory: [],
  agentDecision: null,
  agentLog: [],
  packetFlows: [],
  free5gcStatus: null,

  setActiveEvent: (event) => set({ activeEvent: event }),
  setSimulating: (v) => set({ isSimulating: v }),

  updateSlices: (slices) => set({ slices }),

  applyPodEvent: (e) =>
    set((state) => {
      const pods = state.pods.map((c) => {
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
    set((state) => ({
      metrics: m,
      metricsHistory: [...state.metricsHistory.slice(-59), m],
    })),

  setAgentDecision: (d) => set({ agentDecision: d }),

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

  setPacketFlows: (flows) => set({ packetFlows: flows }),

  setFree5gcStatus: (status) =>
    set((state) => ({
      free5gcStatus: status,
      metrics: status.metrics,
      metricsHistory: [...state.metricsHistory.slice(-59), status.metrics],
      slices: status.slices,
    })),

  reset: () =>
    set({
      activeEvent: null,
      isSimulating: false,
      slices: DEFAULT_SLICES,
      pods: DEFAULT_PODS,
      metrics: null,
      metricsHistory: [],
      agentDecision: null,
      agentLog: [],
      packetFlows: [],
      free5gcStatus: null,
    }),
}))
