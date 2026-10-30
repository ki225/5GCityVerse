export type RuntimeProfile = 'onprem'

export interface RuntimeCapabilityStatus {
  status?: string
  configured?: boolean
  verified?: boolean
  provider?: string
  model?: string
  reason?: string
  detail?: string
  error?: string
}

export interface RuntimeStatus {
  mode?: RuntimeProfile | string
  ai?: RuntimeCapabilityStatus
  kubernetes?: RuntimeCapabilityStatus
  free5gc?: RuntimeCapabilityStatus
  checkedAt?: string
}

const LOCAL_API_URL = ''
const LOCAL_WS_URL = 'ws://127.0.0.1:8000/ws'

export function runtimeProfile(): RuntimeProfile {
  return 'onprem'
}

export function apiBaseUrl(): string {
  const value = import.meta.env.VITE_API_URL
  if (value) return String(value).replace(/\/$/, '')
  return LOCAL_API_URL
}

export function webSocketUrl(): string {
  const value = import.meta.env.VITE_WS_URL
  if (value) return String(value)
  const origin = typeof location !== 'undefined' ? location.origin : undefined
  return origin ? origin.replace(/^http/, 'ws') + '/ws' : LOCAL_WS_URL
}

export function runtimeTargetLabel(_status?: RuntimeStatus | null): string {
  return 'On-prem'
}

export function capabilityIsAvailable(capability: RuntimeCapabilityStatus | undefined): boolean {
  const status = capability?.status?.toLowerCase()
  return status === 'available' || status === 'connected' || status === 'ok' || capability?.verified === true
}

export function capabilityLabel(capability: RuntimeCapabilityStatus | undefined, fallback = 'unknown'): string {
  if (!capability) return fallback
  if (capability.status) return capability.status
  if (capability.configured === true) return 'configured'
  if (capability.configured === false) return 'not_configured'
  return fallback
}
