import { beforeEach, expect, it, vi } from 'vitest'
const h = vi.hoisted(() => ({ effects: [] as Array<() => void | (() => void)>, setters: [] as ReturnType<typeof vi.fn>[], read: vi.fn() }))
vi.mock('react', async importOriginal => ({ ...await importOriginal<typeof import('react')>(),
  useState: (initial: unknown) => { const setter = vi.fn(); h.setters.push(setter); return [initial, setter] },
  useEffect: (fn: () => void | (() => void)) => h.effects.push(fn),
  useRef: (value: unknown) => ({ current: value }), useCallback: (fn: unknown) => fn,
}))
vi.mock('../../services/intents', async importOriginal => ({ ...await importOriginal<typeof import('../../services/intents')>(), getIntentCapabilities: h.read }))
import { FormalIntentProvider } from './FormalIntentContext'
const caps = { actions: [], write_configured: true, planner_available: true, observation_available: true }
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }
beforeEach(() => { h.effects.length = h.setters.length = 0; h.read.mockReset() })
it('starts loading on the first render and performs only one background capability read', async () => {
  h.read.mockResolvedValue(caps)
  const tree = FormalIntentProvider({ children: null })
  expect(tree.props.value.capLoading).toBe(true); expect(tree.props.value.caps).toBeNull()
  const cleanup = h.effects[1](); await flush()
  expect(h.read).toHaveBeenCalledTimes(1)
  expect(h.setters[0]).toHaveBeenLastCalledWith(caps)
  expect(h.setters[2]).toHaveBeenLastCalledWith(false)
  if (typeof cleanup === 'function') cleanup()
})
it('aborts cleanup and ignores a stale result, then accepts a separate manual recheck', async () => {
  let resolve!: (value: unknown) => void
  h.read.mockReturnValueOnce(new Promise(done => { resolve = done })).mockResolvedValueOnce(caps)
  const tree = FormalIntentProvider({ children: null })
  const cleanup = h.effects[1]()
  if (typeof cleanup === 'function') cleanup()
  expect(h.read.mock.calls[0][0].aborted).toBe(true)
  resolve(caps); await flush()
  expect(h.setters[0]).not.toHaveBeenCalledWith(caps)
  tree.props.value.refresh()
  expect(h.setters[0]).toHaveBeenLastCalledWith(null)
  expect(h.setters[2]).toHaveBeenLastCalledWith(true)
  const secondCleanup = h.effects[1](); await flush()
  expect(h.read).toHaveBeenCalledTimes(2)
  expect(h.setters[0]).toHaveBeenLastCalledWith(caps)
  if (typeof secondCleanup === 'function') secondCleanup()
})
it('keeps transport failure distinct and stops loading without an automatic retry', async () => {
  h.read.mockRejectedValue(new Error('capabilities_connection'))
  FormalIntentProvider({ children: null }); const cleanup = h.effects[1](); await flush()
  expect(h.setters[1]).toHaveBeenLastCalledWith('capabilities_connection')
  expect(h.setters[2]).toHaveBeenLastCalledWith(false)
  expect(h.read).toHaveBeenCalledTimes(1)
  expect(h.setters[0]).toHaveBeenLastCalledWith(null)
  if (typeof cleanup === 'function') cleanup()
})
