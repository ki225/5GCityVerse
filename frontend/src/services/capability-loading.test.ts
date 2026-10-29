import { afterEach, expect, it, vi } from 'vitest'
import { getIntentCapabilities } from './intents'
const valid = { actions: [], write_configured: true, planner_available: true, observation_available: true }
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
it('allows a slow capability read beyond 35 seconds but bounds it at 185 seconds', async () => {
  vi.useFakeTimers()
  const fetch = vi.fn(() => new Promise<Response>(() => undefined)); vi.stubGlobal('fetch', fetch)
  const result = expect(getIntentCapabilities()).rejects.toThrow('capabilities_timeout')
  await vi.advanceTimersByTimeAsync(35001)
  expect((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].signal?.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(150000); await result
  expect(vi.getTimerCount()).toBe(0)
})
it('cancels an unresolved read on unmount, removes its deadline and allows an independent retry', async () => {
  vi.useFakeTimers(); const fetch = vi.fn(() => new Promise<Response>(() => undefined)); vi.stubGlobal('fetch', fetch)
  const abort = new AbortController(), pending = expect(getIntentCapabilities(abort.signal)).rejects.toThrow('capabilities_cancelled')
  abort.abort(); await pending; expect(vi.getTimerCount()).toBe(0)
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(valid))))
  expect(await getIntentCapabilities()).toEqual(valid)
  expect(vi.getTimerCount()).toBe(0)
})
it.each([[502, 'capabilities_connection'], [504, 'capabilities_timeout'], [500, 'capabilities_http']])
  ('classifies HTTP %s without accepting an old grant', async (status, code) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: Number(status) })))
    await expect(getIntentCapabilities()).rejects.toThrow(String(code))
  })
it.each(['not json', JSON.stringify({ ...valid, planner_available: 'true' }), '{}'])('rejects invalid capability response %s', async body => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
  await expect(getIntentCapabilities()).rejects.toThrow('capabilities_invalid_response')
})
