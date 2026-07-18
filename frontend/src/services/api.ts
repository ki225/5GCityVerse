import type { CityEventType, EventBatchTriggerOptions, EventTriggerOptions } from '../types'
import type { Free5gcStatus } from '../types'
import { sessionHeaders } from './browserSession'
import { currentLocale } from '../i18n'
import { notifyAuthorizationRequired } from './auth'

export async function triggerCityEvent(eventType: CityEventType, options: EventTriggerOptions): Promise<{ executionId: string }> {
  const res = await fetch(`${apiBaseUrl()}/api/scenario/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
    body: JSON.stringify({
      event_type: eventType,
      event_scale: options.eventScale,
      city_residents: options.cityResidents,
      locale: currentLocale(),
    }),
  })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<{ executionId: string }>
}

export async function triggerCityEvents(options: EventBatchTriggerOptions): Promise<{ executionId: string; executionIds: string[]; events: Array<{ executionId: string; eventType: CityEventType; eventScale: number; eventDurationSeconds: number }> }> {
  const res = await fetch(`${apiBaseUrl()}/api/scenario/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
    body: JSON.stringify({
      city_residents: options.cityResidents,
      slice_strategy: options.sliceStrategy,
      locale: currentLocale(),
      scenarios: options.scenarios.map((scenario) => ({
        event_type: scenario.eventType,
        event_scale: scenario.eventScale,
      })),
    }),
  })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<{ executionId: string; executionIds: string[]; events: Array<{ executionId: string; eventType: CityEventType; eventScale: number; eventDurationSeconds: number }> }>
}

export async function getSimulationStatus(executionId: string) {
  const res = await fetch(`${apiBaseUrl()}/events/status/${executionId}`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

export async function acknowledgeTrafficRendered(executionId: string) {
  const res = await fetch(`${apiBaseUrl()}/events/status/${executionId}/traffic-rendered`, {
    method: 'POST',
    headers: sessionHeaders(),
  })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<{ executionId: string; trafficRenderedAt: string; measuredEdgeCount: number }>
}

export async function getSliceStatus() {
  const res = await fetch(`${apiBaseUrl()}/network/slices`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

export async function getMetrics() {
  const res = await fetch(`${apiBaseUrl()}/metrics/current`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

export async function getFree5gcStatus(): Promise<Free5gcStatus> {
  const res = await fetch(`${apiBaseUrl()}/free5gc/status`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<Free5gcStatus>
}

export interface ResetJobStatus {
  resetId: string
  status: 'queued' | 'running' | 'success' | 'failed'
  progressStage: string
  progressPercent: number
  message?: string
  error?: string
  statusUrl?: string
  idempotentReplay?: boolean
}

export async function resetSimulation(): Promise<ResetJobStatus> {
  const res = await fetch(`${apiBaseUrl()}/events/reset`, { method: 'POST', headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<ResetJobStatus>
}

export async function getResetStatus(resetId: string): Promise<ResetJobStatus> {
  const res = await fetch(`${apiBaseUrl()}/events/reset/${encodeURIComponent(resetId)}`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<ResetJobStatus>
}

export async function waitForResetCompletion(
  initial: ResetJobStatus,
  onProgress?: (status: ResetJobStatus) => void,
  options: { timeoutMs?: number; pollMs?: number } = {},
): Promise<ResetJobStatus> {
  const timeoutMs = options.timeoutMs ?? 930_000
  const pollMs = options.pollMs ?? 2_000
  const deadline = Date.now() + timeoutMs
  let current = initial
  let lastPollError: unknown
  onProgress?.(current)
  while (current.status !== 'success' && current.status !== 'failed') {
    if (Date.now() >= deadline) {
      const detail = lastPollError ? ` Last polling error: ${String(lastPollError)}` : ''
      throw new Error(`Reset status timed out after ${Math.ceil(timeoutMs / 1000)} seconds.${detail}`)
    }
    await new Promise((resolve) => globalThis.setTimeout(resolve, Math.min(pollMs, Math.max(0, deadline - Date.now()))))
    try {
      current = await getResetStatus(initial.resetId)
      lastPollError = undefined
      onProgress?.(current)
    } catch (error) {
      // A brief API/network interruption must not hide a reset that is still
      // running in Lambda. Keep polling until the explicit client deadline.
      lastPollError = error
    }
  }
  if (current.status === 'failed') {
    throw new Error(current.error || current.message || 'Simulation runtime reset failed')
  }
  return current
}

async function errorMessage(res: Response): Promise<string> {
  if (res.status === 401 || res.status === 403) notifyAuthorizationRequired()
  try {
    const body = await res.json() as { error?: string; detail?: string | Record<string, unknown> }
    const detail = humanDetail(body.detail)
    return [body.error, detail].filter(Boolean).join(': ') || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

function humanDetail(detail: string | Record<string, unknown> | undefined): string | undefined {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const humanField = detail.detail ?? detail.error ?? detail.reason
    if (typeof humanField === 'string') return humanField
    return JSON.stringify(detail)
  }
  return undefined
}

function apiBaseUrl(): string {
  const value = import.meta.env.VITE_API_URL
  if (!value && import.meta.env.MODE === 'test') return ''
  if (!value) {
    throw new Error('VITE_API_URL is required. Run the Terraform deployment script so the frontend is built with cloud endpoints.')
  }
  return String(value).replace(/\/$/, '')
}
