import { afterEach, describe, expect, it, vi } from 'vitest'
import { readObservationView, readObservability, readQosMeasurements, type QosSample, type RoundObservationView } from '../roundObservability'

const observedAt = '2026-10-18T00:00:20Z'
function fixture(): RoundObservationView {
  const sample: QosSample = { id: 'window-1:video_streaming', sequence: 1, roundId: 'round1', generation: 7,
    scenario: 'video_streaming', slice: 'embb', stage: 'measuring_result', startedAt: observedAt,
    finishedAt: observedAt, observedAt, receivedUplinkMbps: 1, receivedDownlinkMbps: 2,
    rttMs: null, rttP95Ms: null, lossPercent: null, packets: null, lostPackets: null,
    sampleCount: 0, source: 'receiver', quality: 'observed', comparisonFingerprint: 'load', policyRevision: null }
  return { schemaVersion: 'cityverse.round-observation-view/v1', roundId: 'round1', generation: 7, sequence: 20,
    observability: { schemaVersion: 'cityverse.round-observability/v1', roundId: 'round1', generation: 7,
      observedAt, agent: null, decisions: [], changes: [], latestQos: [sample], nodeStates: [],
      capabilities: { podScaling: false, qerPolicy: false } },
    measurements: { samples: [sample], nextCursor: 1, hasMore: true, gap: false } }
}
function respond(body: unknown) { return { ok: true, status: 200, json: async () => structuredClone(body) } }

afterEach(() => vi.unstubAllGlobals())

describe('combined fresh observation display read', () => {
  it('uses one request with the original abort signal and preserves source clocks, missing QoS and cursor', async () => {
    const fetch = vi.fn().mockResolvedValue(respond(fixture()))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    const view = await readObservationView('round1', 7, 0, signal)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(String(fetch.mock.calls[0][0])).toContain('/round1/observation-view?after=0&limit=100')
    expect(fetch.mock.calls[0][1].signal).toBe(signal)
    expect(view.observability.observedAt).toBe(observedAt)
    expect(view.measurements.samples[0].rttMs).toBeNull()
    expect(view.measurements.hasMore).toBe(true)
    expect(view.measurements.nextCursor).toBe(1)
    expect('observedAt' in view).toBe(false)
  })

  it('keeps both old readers and their returned bodies available', async () => {
    const view = fixture()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(respond(view.observability))
      .mockResolvedValueOnce(respond(view.measurements)))
    const signal = new AbortController().signal
    expect(await readObservability('round1', 7, signal)).toEqual(view.observability)
    expect(await readQosMeasurements('round1', 7, 0, signal)).toEqual(view.measurements)
  })

  it.each(['owner', 'generation', 'sequence', 'nested-owner', 'cursor', 'sample-scope', 'missing-page'])(
    'rejects invalid %s before updating UI data', async (damage) => {
      const view = fixture()
      if (damage === 'owner') view.roundId = 'other'
      if (damage === 'generation') view.generation = 8
      if (damage === 'sequence') view.sequence = Number.NaN
      if (damage === 'nested-owner') view.observability.roundId = 'other'
      if (damage === 'cursor') view.measurements.nextCursor = 0
      if (damage === 'sample-scope') view.measurements.samples[0].generation = 8
      if (damage === 'missing-page') (view as unknown as Record<string, unknown>).measurements = null
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond(view)))
      await expect(readObservationView('round1', 7, 0, new AbortController().signal)).rejects.toThrow()
    },
  )

  it('passes cancellation through without issuing a fallback or replay', async () => {
    const cancellation = new DOMException('Disposed', 'AbortError')
    const fetch = vi.fn().mockRejectedValue(cancellation)
    vi.stubGlobal('fetch', fetch)
    await expect(readObservationView('round1', 7, 9, new AbortController().signal)).rejects.toBe(cancellation)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(String(fetch.mock.calls[0][0])).toContain('after=9')
  })
})

vi.mock('../../../services/browserSession', () => ({ sessionHeaders: () => ({ Authorization: 'Bearer fixture-only', 'X-CityVerse-Session-Id': 'fixture-owner' }) }))
