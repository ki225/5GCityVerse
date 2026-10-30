import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const harness = vi.hoisted(() => ({ effects: [] as Array<() => void | (() => void)>, state: undefined as unknown,
  view: vi.fn(), events: vi.fn() }))
vi.mock('react', async original => ({ ...await original<typeof import('react')>(),
  useState: (initial: unknown) => [initial, (value: unknown) => {
    harness.state = typeof value === 'function' ? (value as (v: unknown) => unknown)(harness.state) : value
  }], useEffect: (effect: () => void | (() => void)) => { harness.effects.push(effect) } }))
vi.mock('../roundObservability', async original => ({ ...await original<typeof import('../roundObservability')>(),
  readObservationView: harness.view, readAgentEvents: harness.events }))
import { useRoundObservability } from '../../components/OnpremRuntime/useRoundObservability'
import type { ObservationState } from '../roundObservability'
beforeEach(() => { vi.useFakeTimers(); harness.effects.length = 0; harness.view.mockReset(); harness.events.mockReset() })
afterEach(() => vi.useRealTimers())

it('drains13 real-shaped cursor pages, signals truncation, preserves source clocks and cancels GET polling', async () => {
  const at = '2026-10-29T11:00:00Z'
  const data = { roundId: 'city100', generation: 9, observedAt: at, latestQos: [] }
  harness.events.mockResolvedValue({ events: [], hasMore: false, nextCursor: 0, gap: false })
  harness.view.mockImplementation(async (_round, _generation, cursor) => ({ observability: data,
    measurements: { samples: Array.from({ length: cursor < 1300 ? 100 : 0 }, (_, i) => ({ sequence: cursor + i + 1,
      roundId: 'city100', generation: 9, observedAt: at, receivedUplinkMbps: 1, receivedDownlinkMbps: 1 })),
      nextCursor: Math.min(cursor + 100, 1300), hasMore: cursor + 100 < 1300, gap: cursor === 600 } }))
  useRoundObservability('city100', 9)
  const cleanup = harness.effects[0]()
  await vi.advanceTimersByTimeAsync(50)
  const state = harness.state as ObservationState
  expect(harness.view.mock.calls.slice(0, 13).map(c => c[2])).toEqual(Array.from({ length: 13 }, (_, i) => i * 100))
  expect(state.samples).toHaveLength(1200)
  expect(state.samples[0].sequence).toBe(101)
  expect(state.samples[state.samples.length - 1].sequence).toBe(1300)
  expect(state.truncated).toBe(true)
  expect(state.gap).toBe(true)
  expect(state.data!.observedAt).toBe(at)
  const reads = harness.view.mock.calls.length
  if (typeof cleanup === 'function') cleanup()
  await vi.advanceTimersByTimeAsync(5000)
  expect(harness.view).toHaveBeenCalledTimes(reads)
})

it('does not convert a failed next page into fresh complete history', async () => {
  harness.events.mockResolvedValue({ events: [], hasMore: false, nextCursor: 0, gap: false })
  harness.view.mockResolvedValueOnce({ observability: { observedAt: '2026-10-29T11:00:00Z' },
    measurements: { samples: [{ sequence: 1 }], nextCursor: 1, hasMore: true, gap: false } })
    .mockRejectedValue(new Error('receiver page unavailable'))
  useRoundObservability('city100', 9)
  const cleanup = harness.effects[0]()
  await vi.advanceTimersByTimeAsync(50)
  const state = harness.state as ObservationState
  expect(state.samples).toHaveLength(1)
  expect(state.data!.observedAt).toBe('2026-10-29T11:00:00Z')
  expect(state.error).toContain('receiver page unavailable')
  if (typeof cleanup === 'function') cleanup()
})
