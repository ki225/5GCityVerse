import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { scenarioPollInterval, startScenarioPolling } from '../scenarioPolling'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

it('StrictMode stop/start cannot revive an aborted loop or show its AbortError', async () => {
  const received = vi.fn(), failed = vi.fn()
  const read = vi.fn((signal: AbortSignal) => new Promise<number>((resolve, reject) => {
    if (read.mock.calls.length === 1) signal.addEventListener('abort', () => reject(new DOMException('signal is aborted without reason', 'AbortError')))
    else resolve(2)
  }))
  const stopOld = startScenarioPolling(read, received, failed)
  stopOld()
  const stopNew = startScenarioPolling(read, received, failed)
  await vi.advanceTimersByTimeAsync(9000)
  expect(failed).not.toHaveBeenCalled()
  expect(read).toHaveBeenCalledTimes(5) // first aborted + one active loop
  expect(read.mock.calls.slice(1).every(([signal]) => !signal.aborted)).toBe(true)
  expect(received).toHaveBeenCalledTimes(4)
  stopNew()
  await vi.advanceTimersByTimeAsync(20000)
  expect(read).toHaveBeenCalledTimes(5)
})

it('real GET timeout is visible and a later successful poll recovers', async () => {
  const received = vi.fn(), failed = vi.fn()
  const read = vi.fn((signal: AbortSignal) => new Promise<number>((resolve, reject) => {
    if (read.mock.calls.length === 1) signal.addEventListener('abort', () => reject(signal.reason))
    else resolve(7)
  }))
  const stop = startScenarioPolling(read, received, failed, { timeoutMs: 1000, intervalMs: 100 })
  await vi.advanceTimersByTimeAsync(1000)
  expect(String(failed.mock.calls[0][0])).toContain('timed out; retrying')
  expect(received).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(100)
  expect(received).toHaveBeenCalledWith(7)
  stop()
})

it('does not suppress unexpected AbortErrors or ordinary network failures', async () => {
  const failure = vi.fn()
  const read = vi.fn().mockRejectedValueOnce(new DOMException('unexpected', 'AbortError')).mockRejectedValueOnce(new Error('network down'))
  const stop = startScenarioPolling(read, vi.fn(), failure, { intervalMs: 100 })
  await vi.advanceTimersByTimeAsync(100)
  expect(failure).toHaveBeenCalledTimes(2)
  expect(String(failure.mock.calls[0][0])).toContain('unexpected')
  expect(String(failure.mock.calls[1][0])).toContain('network down')
  stop()
})

it('late resolution after disposal cannot publish stale observations', async () => {
  let resolve!: (value: number) => void
  const receive = vi.fn()
  const stop = startScenarioPolling(() => new Promise<number>((done) => { resolve = done }), receive, vi.fn())
  stop()
  expect(vi.getTimerCount()).toBe(0)
  resolve(1)
  await vi.advanceTimersByTimeAsync(20000)
  expect(receive).not.toHaveBeenCalled()
})

it.each([0, 1200])('observes a two-second result window with %ims GET latency without overlapping requests', async (latency) => {
  const beginning = Date.now()
  let active = 0, peak = 0
  const received = vi.fn(), failed = vi.fn()
  const read = vi.fn(() => new Promise<{ stage: string }>((resolve) => {
    active += 1; peak = Math.max(peak, active)
    setTimeout(() => {
      active -= 1
      const elapsed = Date.now() - beginning
      resolve({ stage: elapsed < 2300 ? 'measuring_result' : elapsed < 4300 ? 'results_ready' : 'completed' })
    }, latency)
  }))
  const stop = startScenarioPolling(read, received, failed, { intervalMs: scenarioPollInterval })
  await vi.advanceTimersByTimeAsync(7000)
  expect(received.mock.calls.some(([value]) => value.stage === 'results_ready')).toBe(true)
  expect(peak).toBe(1)
  expect(failed).not.toHaveBeenCalled()
  expect(scenarioPollInterval({ stage: 'completed' })).toBe(3000)
  expect(scenarioPollInterval({ stage: 'idle' })).toBe(3000)
  stop()
})

it('starts owned status reads immediately, then every two seconds, and uses fresh terminal state for the next interval', async () => {
  let stage = 'measuring_result'
  const read = vi.fn(async () => ({ stage }))
  const receive = vi.fn(), failed = vi.fn()
  const stop = startScenarioPolling(read, receive, failed, { intervalMs: scenarioPollInterval })
  expect(read).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(6000)
  expect(read).toHaveBeenCalledTimes(4) // starts at 0, 2, 4 and 6 seconds
  expect(receive).toHaveBeenCalledTimes(4)
  stage = 'completed'
  await vi.advanceTimersByTimeAsync(2000)
  expect(read).toHaveBeenCalledTimes(5)
  expect(receive).toHaveBeenLastCalledWith({ stage: 'completed' })
  await vi.advanceTimersByTimeAsync(2999)
  expect(read).toHaveBeenCalledTimes(5)
  await vi.advanceTimersByTimeAsync(1)
  expect(read).toHaveBeenCalledTimes(6)
  expect(failed).not.toHaveBeenCalled()
  stop()
  expect(vi.getTimerCount()).toBe(0)
  await vi.advanceTimersByTimeAsync(10000)
  expect(read).toHaveBeenCalledTimes(6)
})

it('does not overlap or defer a slow owned status read beyond its existing two-second start cadence', async () => {
  const complete: (() => void)[] = []
  let active = 0, peak = 0
  const read = vi.fn(() => new Promise<{ stage: string }>((resolve) => {
    active += 1; peak = Math.max(peak, active)
    complete.push(() => { active -= 1; resolve({ stage: 'measuring_result' }) })
  }))
  const failed = vi.fn()
  const stop = startScenarioPolling(read, vi.fn(), failed, { intervalMs: scenarioPollInterval })
  await vi.advanceTimersByTimeAsync(3000)
  expect(read).toHaveBeenCalledTimes(1)
  complete[0]()
  await vi.advanceTimersByTimeAsync(1)
  expect(read).toHaveBeenCalledTimes(2) // The slow request already consumed the interval.
  await vi.advanceTimersByTimeAsync(3000)
  expect(read).toHaveBeenCalledTimes(2)
  expect(peak).toBe(1)
  expect(failed).not.toHaveBeenCalled()
  stop()
  complete[1]()
  await vi.advanceTimersByTimeAsync(10000)
  expect(read).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})
