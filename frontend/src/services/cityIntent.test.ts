import { afterEach, describe, expect, it, vi } from 'vitest'
import { compileCityDemand } from '../city-map/domain/demand'
import { parseIntentSubmission, parsePendingCityIntent, validateCityIntent } from './cityIntent'
import { submitServiceIntent } from './intents'
import { profileRequest } from './profileRequest'
vi.mock('./browserSession', () => ({ sessionHeaders: () => ({ 'X-CityVerse-Session-Id': 'same-owner', Authorization: 'Bearer fixture-only' }) }))
afterEach(() => vi.unstubAllGlobals())
function draft() {
  const request = compileCityDemand([{ groupId: 'arena', label: 'Arena', temporary: false, ueCount: 2, profileId: 'video_streaming',
    sourceLocation: { type: 'point', buildingId: 'arena', position: { x: 18.5, y: 4.5 } } }],
  { video_streaming: { traffic: { protocol: 'udp', offeredUplinkMbpsPerUe: 1, offeredDownlinkMbpsPerUe: 2 },
    sla: { scope: 'per-ue', minDownlinkMbps: 1 }, evaluation: { windowSeconds: 5, sampleIntervalSeconds: 1, minTimeCompliance: .95, minUeCompliance: 1 } } }, 3)
  return { text: 'Protect the video_streaming service for 60 seconds using authorized resource budgets.', city_traffic: { ...request, requestId: 'same-request' } }
}
describe('single formal city intent contract', () => {
  it('sends location, population, offered load and SLA together to the formal endpoint', async () => {
    const request = draft(), fetch = vi.fn(async () => new Response(JSON.stringify({ intent_id: 'i', operation_id: 'o', status: 'queued', traffic_start_mode: 'runtime' })))
    vi.stubGlobal('fetch', fetch)
    await submitServiceIntent(request.text, request.city_traffic, 'same-key')
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/intents'); expect(JSON.parse(String(init.body))).toEqual({ text:request.text,city_traffic:profileRequest(request.city_traffic) })
    expect(init.headers).toMatchObject({ 'Idempotency-Key': 'same-key', 'X-CityVerse-Session-Id': 'same-owner', Authorization: 'Bearer fixture-only' })
    expect(request.city_traffic.strategy).toBe('none')
  })
  it('preserves the exact original payload and idempotency key across an uncertain retry', async () => {
    const original = draft(), saved = JSON.stringify({ key: 'same-key', payload: original })
    original.city_traffic.demandGroups![0].ueCount = 99
    const restored = parsePendingCityIntent(saved)
    const fetch = vi.fn(async () => new Response('{}')); vi.stubGlobal('fetch', fetch)
    await submitServiceIntent(restored.payload.text, restored.payload.city_traffic, restored.key)
    await submitServiceIntent(restored.payload.text, restored.payload.city_traffic, restored.key)
    expect(fetch.mock.calls[0]).toEqual(fetch.mock.calls[1])
    expect(restored.payload.city_traffic.demandGroups![0].ueCount).toBe(2)
  })
  it('rejects count, strategy and spatial-contract tampering before dispatch', () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    for (const mutate of [(d: ReturnType<typeof draft>) => { d.city_traffic.events.video_streaming++ },
      (d: ReturnType<typeof draft>) => { delete d.city_traffic.topologyRevision },
      (d: ReturnType<typeof draft>) => { Object.assign(d.city_traffic, { strategy: 'ai' }) }]) {
      const value = draft(); mutate(value)
      expect(() => submitServiceIntent(value.text, value.city_traffic, 'same-key')).toThrow('invalid_city_intent')
    }
    expect(fetch).not.toHaveBeenCalled()
  })
  it('requires explicit intent text and a confirmed runtime-owned acceptance', () => {
    expect(() => validateCityIntent({ ...draft(), text: '' })).toThrow()
    expect(() => parseIntentSubmission({ intent_id: 'i', operation_id: 'o', status: 'queued' })).toThrow()
    expect(() => parsePendingCityIntent('{"key":"same-key","payload":{}}')).toThrow()
  })
})
