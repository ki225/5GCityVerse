import { sessionHeaders } from './browserSession'
import { apiBaseUrl } from './runtimeConfig'
import type { ScenarioRequest } from '../onprem/services/onpremScenarios'
import { validateCityIntent } from './cityIntent'
import { profileRequest } from './profileRequest'

let controlToken = ''
export function setIntentControlToken(value: string): void { controlToken = value.trim() }
export class IntentRequestError extends Error {
  constructor(readonly status: number, readonly code: string) { super(code || `HTTP ${status}`) }
  get definitive() { return [400, 401, 403, 404, 409, 422].includes(this.status) || this.code === 'INTENT_WRITE_NOT_CONFIGURED' }
}

export interface IntentOperation {
  operation_id: string
  intent_id: string
  status: string
  stage?: string
  error?: unknown
  receipts?: unknown[]
  plan?: Record<string, unknown>
  effect?: Record<string, unknown>
  before?: Record<string, unknown>
  after?: Record<string, unknown>
  compensation?: unknown[]
  released?: unknown[]
  model_provenance?: Record<string, unknown>
  installation_verdict?: string
  routing_verdict?: string
  sla_verdict?: string
  restore_status?: string
  pending?: boolean
  recovery_required?: boolean
  control?: {
    nodes?: string[]
    role?: string
    phase?: string
    comparison_eligible?: boolean
    restoration_verified?: boolean
  }
}

export interface ServiceIntentView {
  intent_id: string
  revision: number
  source_event_id?: string
  status?: string
  state?: string
  latest_operation?: IntentOperation | null
  definition_issues?: unknown[]
  report?: Record<string, unknown> | null
  [key: string]: unknown
}

async function boundedJson(response: Response, maximum: number, signal?: AbortSignal | null): Promise<unknown> {
  if (!response.body) throw new Error('operation_response_stream_missing')
  const declared = response.headers.get('content-length')
  if (declared && /^\d+$/.test(declared) && Number(declared) > maximum) {
    void response.body.cancel().catch(() => undefined)
    throw new Error('operation_response_too_large')
  }
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let bytes = 0
  const cancel = () => { void reader.cancel().catch(() => undefined) }
  signal?.addEventListener('abort', cancel, { once: true })
  try {
    while (true) {
      if (signal?.aborted) throw new Error('operation_query_cancelled')
      const { done, value } = await reader.read()
      if (signal?.aborted) throw new Error('operation_query_cancelled')
      if (done) break
      bytes += value.byteLength
      if (bytes > maximum) { cancel(); throw new Error('operation_response_too_large') }
      chunks.push(value)
    }
    const body = new Uint8Array(bytes)
    let offset = 0
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body)) as unknown
  } finally { signal?.removeEventListener('abort', cancel); reader.releaseLock() }
}

async function request<T>(path: string, options: RequestInit = {}, responseLimit?: number): Promise<T> {
  if (options.signal?.aborted) throw new Error('operation_query_cancelled')
  const mutation = options.method && options.method !== 'GET'
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json', ...sessionHeaders(),
      ...(controlToken ? { Authorization: `Bearer ${controlToken}` } : {}),
      ...(mutation ? { 'Idempotency-Key': crypto.randomUUID() } : {}), ...options.headers,
    },
  })
  if (options.signal?.aborted) { void response.body?.cancel().catch(() => undefined); throw new Error('operation_query_cancelled') }
  if (!response.ok) {
    if (responseLimit !== undefined) {
      void response.body?.cancel().catch(() => undefined)
      throw new Error(`operation_http_${response.status}`)
    }
    const body = await response.json().catch(() => ({})) as Record<string, unknown>
    throw new IntentRequestError(response.status, intentDetail(body.error ?? body.detail ?? body.reason))
  }
  return responseLimit === undefined ? response.json() as Promise<T> : await boundedJson(response, responseLimit, options.signal) as T
}

export interface OperationDetailView {
  operation_id: string
  intent_id: string
  status: string
  stage: string | null
  kind: string | null
  error_code: string | null
  category: string | null
  readback_reason: string | null
}

const OPERATION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function machineCode(value: unknown): string | null {
  if (typeof value !== 'string' || !/^[a-z][a-z0-9_]{0,95}$/.test(value)
    || /^(sk_|pk_|key_|token_|secret_|password_|bearer_|authorization_)/.test(value)
    || /(api_key|access_token|client_secret)/.test(value)) return null
  return value
}
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

/** One authorized exact-operation read; never enumerate history or supply current SLA evidence. */
export async function getIntentOperationDetail(id: string, intentId: string, signal?: AbortSignal): Promise<OperationDetailView> {
  if (!OPERATION_ID.test(id) || !intentId || intentId.length > 128) throw new Error('invalid_operation_lookup')
  if (signal?.aborted) throw new Error('operation_query_cancelled')
  const controller = new AbortController()
  const relay = () => controller.abort()
  if (signal?.aborted) relay()
  signal?.addEventListener('abort', relay, { once: true })
  let timeout = false
  const timer = setTimeout(() => { timeout = true; controller.abort() }, 10_000)
  let stop: () => void = () => undefined
  const cancelled = new Promise<never>((_resolve, reject) => {
    stop = () => reject(new Error(timeout ? 'operation_query_timeout' : 'operation_query_cancelled'))
    controller.signal.addEventListener('abort', stop, { once: true })
    if (controller.signal.aborted) stop()
  })
  try {
    const payload = await Promise.race([request<unknown>(`/api/operations/${encodeURIComponent(id)}`, { signal: controller.signal }, 256 * 1024), cancelled])
    const row = object(payload)
    if (row.operation_id !== id || row.intent_id !== intentId) throw new Error('operation_not_in_selected_intent')
    const error = object(row.error), readback = object(row.readback)
    return { operation_id: id, intent_id: intentId, status: machineCode(row.status) ?? 'unknown',
      stage: machineCode(row.stage), kind: machineCode(row.kind), error_code: machineCode(error.code ?? row.error),
      category: machineCode(error.category ?? row.category), readback_reason: machineCode(readback.reason ?? row.readback_reason ?? error.reason) }
  } catch (failure) {
    if (controller.signal.aborted) throw new Error(timeout ? 'operation_query_timeout' : 'operation_query_cancelled')
    if (failure instanceof Error && /^operation_[a-z0-9_]+$/.test(failure.message)) throw failure
    throw new Error('operation_response_unavailable')
  } finally {
    clearTimeout(timer); controller.signal.removeEventListener('abort', stop); signal?.removeEventListener('abort', relay)
  }
}

export function intentDetail(value: unknown): string {
  if (typeof value === 'string') return value
  if (value instanceof Error) return value.message
  if (Array.isArray(value)) return value.map(intentDetail).filter(Boolean).join('; ')
  if (value && typeof value === 'object') {
    const row = value as Record<string, unknown>
    return intentDetail(row.message ?? row.detail ?? row.reason ?? row.code)
  }
  return ''
}

export const MAX_INTENT_PAGE_OFFSET = 10_000
function pageOffset(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_INTENT_PAGE_OFFSET) throw new Error('Invalid bounded page offset')
  return value
}

export const listServiceIntents = (signal?: AbortSignal, offset = 0) =>
  request<{ items: ServiceIntentView[] }>(`/api/intents?limit=20&offset=${pageOffset(offset)}`, { signal })
export const getServiceIntent = (id: string, signal?: AbortSignal) =>
  request<ServiceIntentView>(`/api/intents/${encodeURIComponent(id)}`, { signal })
export const getIntentOperation = (id: string, signal?: AbortSignal) =>
  request<IntentOperation>(`/api/operations/${encodeURIComponent(id)}`, { signal })
export const getIntentMembers = (id: string, signal?: AbortSignal, offset = 0) =>
  request<{ items: unknown[]; total: number; rejected: unknown[] }>(`/api/intents/${encodeURIComponent(id)}/members?limit=100&offset=${pageOffset(offset)}`, { signal })
export const getIntentCapabilities = (signal?: AbortSignal) =>
  request<IntentCapabilities>('/api/capabilities', { signal })
export interface IntentCapabilities { actions: Array<{ action: string; status: string; reason: string }>; write_configured: boolean; planner_available: boolean; observation_available: boolean }
export type IntentCityTraffic = Omit<ScenarioRequest, 'strategy'> & { strategy: 'none' }
export interface IntentSubmission { intent_id: string; operation_id: string; status: string; traffic_start_mode?: string; traffic_request_id?: string }

export function submitServiceIntent(text: string, cityTraffic: IntentCityTraffic, key: string, signal?: AbortSignal) {
  validateCityIntent({ text, city_traffic: cityTraffic })
  if (typeof key !== 'string' || !/^[A-Za-z0-9:-]{1,256}$/.test(key)) throw new Error('invalid_idempotency_key')
  return request<IntentSubmission>('/api/intents', {
    method: 'POST', body: JSON.stringify({ text, ...(cityTraffic ? { city_traffic: profileRequest(cityTraffic) } : {}) }),
    headers: { 'Idempotency-Key': key }, signal,
  })
}
export const reconcileServiceIntent = (id: string) =>
  request<{ operation_id: string }>(`/api/intents/${encodeURIComponent(id)}/reconcile`, { method: 'POST', body: '{}' })
export const releaseServiceIntent = (id: string) =>
  request<{ operation_id: string }>(`/api/intents/${encodeURIComponent(id)}/release`, { method: 'POST', body: '{}' })
