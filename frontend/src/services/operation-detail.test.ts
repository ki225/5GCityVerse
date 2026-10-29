import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getIntentOperationDetail } from './intents'
const ID = '82ebb343-2690-43ee-a740-6607d1efaa2a'
const value = () => ({ operation_id: ID, intent_id: 'intent', status: 'needs_definition', kind: 'reconcile', stage: 'needs_definition', error: { code: 'model_proposal_unavailable', category: 'model', reason: 'provider_unavailable', message: 'never-render-private-body' }, request: { token: 'never-render-private-token' } })
beforeEach(() => {

  vi.stubEnv('VITE_API_URL', 'http://127.0.0.1:8000')
  vi.stubEnv('VITE_WS_URL', 'ws://127.0.0.1:8000/ws')

})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.unstubAllEnvs() })
describe('one bounded session-owned operation read', () => {
  it('GETs only the exact operation and returns a bounded safe projection, not private payload', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(value())))
    vi.stubGlobal('fetch', fetch)
    const result = await getIntentOperationDetail(ID, 'intent')
    expect(result.error_code).toBe('model_proposal_unavailable'); expect(result.readback_reason).toBe('provider_unavailable')
    expect(JSON.stringify(result)).not.toContain('never-render')
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(call[0]).toBe('http://127.0.0.1:8000/api/operations/' + ID)
    expect(call[1].method).toBeUndefined(); expect(call[1].headers).not.toHaveProperty('Authorization')
    expect(call[1].headers).toHaveProperty('X-CityVerse-Session-Id', 'test-session')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it.each(['wrong_intent', 'wrong_operation'])('rejects a returned %s without exposing it', async damage => {
    const response = value(); if (damage === 'wrong_intent') response.intent_id = 'foreign'; else response.operation_id = '00000000-0000-4000-8000-000000000000'
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(response))))
    await expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_not_in_selected_intent')
  })
  it.each(['', '../other', 'x'.repeat(129), 'not-an-operation'])('refuses malformed ID before fetch', async id => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(getIntentOperationDetail(id, 'intent')).rejects.toThrow('invalid_operation_lookup'); expect(fetch).not.toHaveBeenCalled()
  })
  it('rejects declared and actual chunked oversized bodies and cancels the reader', async () => {
    const cancelled = vi.fn()
    const body = () => new ReadableStream<Uint8Array>({ start(c) { c.enqueue(new Uint8Array(256 * 1024 + 1)) }, cancel: cancelled })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body(), { headers: { 'Content-Length': String(256 * 1024 + 1) } })))
    await expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_response_too_large')
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body())))
    await expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_response_too_large')
    expect(cancelled).toHaveBeenCalledTimes(2)
  })
  it('exposes only safe machine codes, never free text, markup, JWT or secret-looking properties', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...value(), stage: '<script>bad</script>', error: { code: 'sk_private_secret', category: 'Bearer credential', reason: 'eyJ.a.b', message: 'private-body' } }))))
    const result = await getIntentOperationDetail(ID, 'intent')
    expect(result.error_code).toBeNull(); expect(result.category).toBeNull(); expect(result.stage).toBeNull(); expect(result.readback_reason).toBeNull()
  })
  it('limits an unresolved fetch to the whole operation deadline and cleans its timer', async () => {
    vi.useFakeTimers(); const fetch = vi.fn(() => new Promise<Response>(() => undefined)); vi.stubGlobal('fetch', fetch)
    const pending = expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_query_timeout')
    await vi.advanceTimersByTimeAsync(10_001); await pending
    expect(vi.getTimerCount()).toBe(0)
  })
  it('cancels an unresolved body read at the deadline', async () => {
    vi.useFakeTimers(); const cancelled = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(new ReadableStream({ cancel: cancelled }))))
    const pending = expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_query_timeout')
    await vi.advanceTimersByTimeAsync(10_001); await pending
    expect(cancelled).toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0)
  })
  it('propagates cancellation promptly even when the transport double never resolves', async () => {
    vi.useFakeTimers(); vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))
    const controller = new AbortController()
    const pending = expect(getIntentOperationDetail(ID, 'intent', controller.signal)).rejects.toThrow('operation_query_cancelled')
    controller.abort(); await pending; expect(vi.getTimerCount()).toBe(0)
  })
  it('never reads or renders a denied HTTP response body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('private-auth-diagnostic', { status: 403 })))
    await expect(getIntentOperationDetail(ID, 'intent')).rejects.toThrow('operation_http_403')
  })
})
