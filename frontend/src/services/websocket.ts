import type { WsMessage, PodEvent, AgentDecision, SliceStatus, Free5gcStatus, NetworkSnapshot, RuntimePrimeStatus } from '../types'
import { useAppStore } from '../store/appStore'
import { normalizeNetworkSnapshot } from './networkSnapshot'
import { browserSessionId } from './browserSession'
import { currentLocale } from '../i18n'
import { notifyAuthorizationRequired, requireAccessToken } from './auth'

let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectEnabled = false
let socketGeneration = 0
const seenEventSignals = new Set<string>()

export function handleMessage(msg: WsMessage) {
  const store = useAppStore.getState()
  const zh = currentLocale() === 'zh-TW'

  // Event-specific WebSocket messages are broadcast by API Gateway to every
  // connected browser. Only accept executions started by this tab. Global
  // infrastructure telemetry remains visible because the EKS/free5GC runtime
  // is intentionally shared, while another tab's Agent workflow stays private.
  if (typeof window !== 'undefined' && isForeignExecutionMessage(msg, store.activeScenarios.map((item) => item.executionId))) return

  switch (msg.type) {
    case 'pod_event':
      store.applyPodEvent(msg.payload as PodEvent)
      break
    case 'metrics_update':
      // Prometheus-only metrics from State Bridge do not carry the canonical
      // NetworkSnapshot contract. Avoid synthesizing topology flows here; the
      // REST status poll / network_snapshot messages provide actual edges.
      break
    case 'agent_decision':
      store.setAgentDecision(msg.payload as AgentDecision)
      break
    case 'agent_action': {
      const p = msg.payload as { agentName: string; index: number; status: AgentDecision['actions'][0]['status']; httpStatus?: number }
      store.updateAgentAction(p.agentName, p.index, p.status, p.httpStatus)
      break
    }
    case 'slice_update':
      store.updateSlices(msg.payload as SliceStatus[])
      break
    case 'event_started': {
      const p = msg.payload as { executionId?: string; eventType?: string; status?: string }
      const status = p.status || 'event_started'
      const key = `${p.executionId || p.eventType || 'unknown'}:${status}`
      if (seenEventSignals.has(key)) break
      seenEventSignals.add(key)
      if (seenEventSignals.size > 80) {
        const first = seenEventSignals.values().next().value
        if (first) seenEventSignals.delete(first)
      }
      const label = status === 'AGENT_QUEUED'
        ? (zh ? '已排入佇列' : 'Queued')
        : status === 'AGENT_RUNNING'
          ? (zh ? '流量環境啟動中' : 'Runtime starting')
          : `Signal ${status}`
      if (status === 'AGENT_RUNNING') store.setOrchestrationStage('runtime_priming')
      store.appendAgentLog(`[Event Engine] ${label}: ${p.eventType || 'unknown'} (${shortExecutionId(p.executionId)})`)
      break
    }
    case 'runtime_priming': {
      const p = msg.payload as RuntimePrimeStatus
      store.setRuntimePrime({ ...p, status: 'running' })
      store.appendAgentLog(zh ? `[流量環境] 正在送出情境流量，AI 尚未規劃：${p.eventType || 'unknown'} (${shortExecutionId(p.executionId)})` : `[Runtime] Sending scenario traffic before planner starts: ${p.eventType || 'unknown'} (${shortExecutionId(p.executionId)})`)
      break
    }
    case 'runtime_primed': {
      const p = msg.payload as RuntimePrimeStatus
      // Batch orchestration holds the planner until the browser has painted a
      // measured bearer frame and acknowledged it through the status API.
      if (!p.awaitingTrafficRenderAck) store.setRuntimePrime(p)
      if (p.observedBeforePlanning) {
        store.appendAgentLog(zh ? `[流量環境] 已觀測：${(p.observedScenarios || []).join('、') || p.eventType || 'scenario'}；現在才允許 AI 規劃` : `[Runtime] Traffic observed: ${(p.observedScenarios || []).join(', ') || p.eventType || 'scenario'}; planner may start`)
      } else {
        store.appendAgentLog(`[Runtime] Traffic evidence missing: ${(p.missingScenarios || []).join(', ') || p.eventType || 'scenario'}`)
      }
      break
    }
    case 'event_blocked': {
      const p = msg.payload as { executionId?: string; detail?: string; error?: string }
      store.setOrchestrationStage('blocked')
      store.appendAgentLog(`[Event Engine] ${p.error || 'Event blocked'}${p.detail ? `: ${p.detail}` : ''}`)
      store.setActiveEvent(null)
      store.setSimulating(false)
      break
    }
    case 'event_reset': {
      const p = msg.payload as { executionId?: string; eventType?: string; status?: string; reason?: string }
      store.setOrchestrationStage('idle')
      store.appendAgentLog(`[Event Engine] Scenario cancelled: ${p.eventType || 'unknown'}${p.reason ? ` (${p.reason})` : ''} (${shortExecutionId(p.executionId)})`)
      store.setActiveEvent(null)
      store.setSimulating(false)
      break
    }
    case 'free5gc_status':
      store.setFree5gcStatus(msg.payload as Free5gcStatus)
      break
    case 'network_snapshot': {
      const metrics = store.metrics ?? {
        upfCpuPercent: 0,
        upfPodCount: 0,
        amfPodCount: 0,
        gtpPacketsPerSec: 0,
        pduSessionCount: 0,
        latencyMs: 0,
        throughputMbps: 0,
        timestamp: Date.now(),
      }
      store.setNetworkSnapshot(normalizeNetworkSnapshot(msg.payload as NetworkSnapshot, metrics, store.slices))
      break
    }
  }
}

export function connectWebSocket(): () => void {
  reconnectEnabled = true
  const generation = ++socketGeneration
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return () => {
      if (generation === socketGeneration) {
        reconnectEnabled = false
      }
    }
  }

  const wsUrl = new URL(webSocketUrl())
  wsUrl.searchParams.set('sessionId', browserSessionId())
  wsUrl.searchParams.set('token', requireAccessToken())
  const ws = new WebSocket(wsUrl.toString())
  socket = ws
  let opened = false

  ws.onopen = () => {
    if (generation !== socketGeneration) return
    opened = true
    useAppStore.getState().appendAgentLog('[WebSocket] Connected to State Bridge')
    useAppStore.getState().setWsConnected(true)
  }

  ws.onmessage = (evt) => {
    if (generation !== socketGeneration) return
    try {
      const msg: WsMessage = JSON.parse(evt.data as string)
      handleMessage(msg)
    } catch {
      // ignore malformed messages
    }
  }

  ws.onclose = (event) => {
    if (generation !== socketGeneration) return
    useAppStore.getState().setWsConnected(false)
    if (!opened || event.code === 4401 || event.code === 4403 || event.code === 1008) {
      reconnectEnabled = false
      notifyAuthorizationRequired()
      return
    }
    if (!reconnectEnabled) return
    useAppStore.getState().appendAgentLog('[WebSocket] Disconnected — reconnecting in 3s')
    reconnectTimer = setTimeout(() => connectWebSocket(), 3000)
  }

  ws.onerror = () => {
    ws.close()
  }

  return () => {
    if (generation !== socketGeneration) return
    reconnectEnabled = false
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    ws.close()
    if (socket === ws) socket = null
  }
}

function webSocketUrl(): string {
  const value = import.meta.env.VITE_WS_URL
  if (!value && import.meta.env.MODE === 'test') return 'about:blank'
  if (!value) {
    throw new Error('VITE_WS_URL is required. Run the Terraform deployment script so the frontend is built with cloud endpoints.')
  }
  return String(value)
}

function isForeignExecutionMessage(msg: WsMessage, executionIds: Array<string | undefined>): boolean {
  const eventTypes = new Set(['event_started', 'runtime_priming', 'runtime_primed', 'event_blocked', 'event_reset', 'agent_decision', 'agent_action'])
  if (!eventTypes.has(msg.type)) return false
  const payload = msg.payload as { executionId?: string }
  if (!payload.executionId) return executionIds.length === 0
  const executionId = payload.executionId
  return !executionIds.some((id) => id && (executionId === id || executionId.startsWith(`${id}-`) || id.startsWith(`${executionId}-`)))
}

function shortExecutionId(executionId?: string): string {
  return executionId ? executionId.slice(0, 8) : 'unknown'
}
