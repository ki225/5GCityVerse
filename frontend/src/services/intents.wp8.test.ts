import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getIntentMembers, listServiceIntents, MAX_INTENT_PAGE_OFFSET } from './intents'

beforeEach(() => {

  vi.stubEnv('VITE_API_URL', 'http://127.0.0.1:8000')
  vi.stubEnv('VITE_WS_URL', 'ws://127.0.0.1:8000/ws')

})
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })
describe('bounded current intent/member page API client', () => {
  it('performs only exact GET page reads and preserves the caller AbortSignal', async () => {
    const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [], total: 102 }) }))
    vi.stubGlobal('fetch', fetch)
    const controller = new AbortController()
    await listServiceIntents(controller.signal, 20)
    await getIntentMembers('intent/owned', controller.signal, 100)
    expect(fetch.mock.calls).toHaveLength(2)
    const calls = fetch.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls[0][0]).toBe('http://127.0.0.1:8000/api/intents?limit=20&offset=20')
    expect(calls[1][0]).toBe('http://127.0.0.1:8000/api/intents/intent%2Fowned/members?limit=100&offset=100')
    for (const [, options] of calls) {
      expect(options.method).toBeUndefined(); expect(options.signal).toBe(controller.signal)
      expect(options.headers).not.toHaveProperty('Idempotency-Key')
    }
  })
  it.each([-1, NaN, Infinity, 0.5, MAX_INTENT_PAGE_OFFSET + 1])('rejects invalid or unbounded offset %s before any fetch', offset => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    expect(() => listServiceIntents(undefined, offset)).toThrow('Invalid bounded page offset')
    expect(() => getIntentMembers('i', undefined, offset)).toThrow('Invalid bounded page offset')
    expect(fetch).not.toHaveBeenCalled()
  })
})
