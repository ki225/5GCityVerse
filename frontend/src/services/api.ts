import type { Free5gcStatus } from '../types'
import { sessionHeaders } from './browserSession'
import { apiBaseUrl, type RuntimeStatus } from './runtimeConfig'

export async function getMetrics() {
  const res = await fetch(`${apiBaseUrl()}/metrics/current`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

export async function getFree5gcStatus(options?: { includeDaily?: boolean }): Promise<Free5gcStatus> {
  const query = options?.includeDaily === undefined ? '' : `?includeDaily=${options.includeDaily}`
  const res = await fetch(`${apiBaseUrl()}/free5gc/status${query}`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<Free5gcStatus>
}

export interface DailyNativeObservation {
  schemaVersion: 'cityverse.daily-native-observation/v1'
  snapshotId: string
  available: boolean
  reason: string | null
  observedAt: string | null
  source: string
  evidenceTrust: string
  criticalOperationEligible: false
  strongBinding: false
  ueCount?: number
  ues: unknown[]
  slices: Record<string, { ueCount?: number }>
}

export interface DailyTrend {
  snapshotId: string
  observedAt: string
  source: string
  availability: 'available'
  evidenceTrust: string
  criticalOperationEligible: false
  strongBinding: false
  trend: string
  ai: { provider: 'openai'; model: string; status: 'success'; generatedAt: string }
}

export async function getDailyNativeObservation(): Promise<DailyNativeObservation> {
  const res = await fetch(`${apiBaseUrl()}/api/onprem/daily-observation`, { headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<DailyNativeObservation>
}

export async function requestDailyTrend(): Promise<DailyTrend> {
  const res = await fetch(`${apiBaseUrl()}/api/onprem/daily-trend`, { method: 'POST', headers: sessionHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json() as Promise<DailyTrend>
}

export async function getRuntimeStatus(): Promise<RuntimeStatus> {
  const primary = await fetch(`${apiBaseUrl()}/api/runtime/status`)
  if (primary.ok) return primary.json() as Promise<RuntimeStatus>
  if (primary.status !== 404) throw new Error(await errorMessage(primary))

  const fallback = await fetch(`${apiBaseUrl()}/health`)
  if (!fallback.ok) throw new Error(await errorMessage(fallback))
  return fallback.json() as Promise<RuntimeStatus>
}

async function errorMessage(res: Response): Promise<string> {
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

