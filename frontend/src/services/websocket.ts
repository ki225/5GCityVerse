import type { WsMessage, PodEvent, NetworkMetrics, AgentDecision, SliceStatus, Free5gcStatus } from '../types'
import { useAppStore } from '../store/appStore'

const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_URL = import.meta.env.VITE_WS_URL || `${WS_PROTOCOL}//${window.location.host}/ws`

let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function handleMessage(msg: WsMessage) {
  const store = useAppStore.getState()

  switch (msg.type) {
    case 'pod_event':
      store.applyPodEvent(msg.payload as PodEvent)
      break
    case 'metrics_update':
      store.updateMetrics(msg.payload as NetworkMetrics)
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
    case 'event_started':
      store.appendAgentLog(`[${new Date().toLocaleTimeString()}] Event started`)
      break
    case 'free5gc_status':
      store.setFree5gcStatus(msg.payload as Free5gcStatus)
      break
  }
}

export function connectWebSocket(): () => void {
  if (socket && socket.readyState === WebSocket.OPEN) return () => {}

  socket = new WebSocket(WS_URL)

  socket.onopen = () => {
    useAppStore.getState().appendAgentLog('[WebSocket] Connected to State Bridge')
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  socket.onmessage = (evt) => {
    try {
      const msg: WsMessage = JSON.parse(evt.data as string)
      handleMessage(msg)
    } catch {
      // ignore malformed messages
    }
  }

  socket.onclose = () => {
    useAppStore.getState().appendAgentLog('[WebSocket] Disconnected — reconnecting in 3s')
    reconnectTimer = setTimeout(() => connectWebSocket(), 3000)
  }

  socket.onerror = () => {
    socket?.close()
  }

  return () => {
    socket?.close()
  }
}
