import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ReactElement } from 'react'

const h = vi.hoisted(() => ({ cursor: 0, values: [] as unknown[], ready: true, admitted: true,
  city: {} as any, adapter: {} as any, formal: {} as any, released: true, effects: [] as Array<() => void | (() => void)>, preview: vi.fn() }))
vi.mock('react', async importOriginal => ({ ...await importOriginal<typeof import('react')>(),
  useState: (initial: unknown) => { const i = h.cursor++; if (!(i in h.values)) h.values[i] = initial;
    return [h.values[i], (next: unknown) => { h.values[i] = typeof next === 'function' ? next(h.values[i]) : next }] },
  useEffect: (effect: () => void | (() => void)) => h.effects.push(effect),
  useRef: (value: unknown) => ({ current: value }),
}))
vi.mock('../context/CityDemandContext', () => ({ useCityDemand: () => h.city }))
vi.mock('../context/FormalIntentContext', () => ({ useFormalIntent: () => h.formal }))
vi.mock('../../onprem/components/OnpremRuntime/ScenarioContext', () => ({ useOnpremScenario: () => h.adapter }))
vi.mock('../../onprem/components/OnpremRuntime/CityBootstrapGate', () => ({ useNativeExecutionReady: () => h.ready }))
vi.mock('../../onprem/components/OnpremRuntime/ScenarioPanels', () => ({ OnpremRoundEvidence: () => null, ScenarioLifecyclePanel: () => null }))
vi.mock('../../onprem/services/onpremScenarios', () => ({ admissionLimit: () => 100,
  allocationAdmitted: () => h.admitted, roundIsReleased: () => h.released }))
vi.mock('../../onprem/services/serviceRequirements', () => ({ previewServiceRequest: h.preview }))
import { CityDemandConsole } from './CityDemandConsole'

function elements(node: any): ReactElement<any>[] {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}
function render(phase: 'launch' | 'review' = 'launch') {
  h.cursor = 0; h.effects.length = 0
  const tree = CityDemandConsole({ phase, onNext: vi.fn() })
  return { tree, submit: elements(tree).find(e => e.props['data-testid'] === 'scenario-submit')! }
}
beforeEach(() => {
  h.ready = h.admitted = h.released = true; h.preview.mockReset()
  const request = { requestId: 'draft', strategy: 'none', baselineUes: 1, events: {}, demandGroups: [], topologyRevision: 3 }
  h.city = { compiled: { request }, rows: [{ groupId: 'group', label: 'House', ueCount: 1,
    profileId: 'web_browsing', sourceLocation: { position: { x: 1, y: 2 } } }], services: {}, caps: { cityAccessCatalog: { spatialRevision: 4 } } }
  h.adapter = { snapshot: { capabilities: { start: true } }, blockingError: false }
  h.formal = { caps: { write_configured: true, planner_available: true, observation_available: true },
    pending: false, uncertain: false, submit: vi.fn(), refresh: vi.fn() }
  h.values = ['Protect browsing for 60 seconds', { key: JSON.stringify(request), at: Date.now(),
    value: { admissible: true, spatialResolution: { resolvedGroups: [] }, warnings: [] } }, false, '']
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('starts the formal intent without a token field or token state', () => {
  const { tree, submit } = render()
  expect(JSON.stringify(tree)).not.toContain('操作授權')
  expect(elements(tree).some(e => e.props.type === 'password')).toBe(false)
  expect(submit.props.disabled).toBe(false)
  submit.props.onClick()
  expect(h.formal.submit).toHaveBeenCalledTimes(1)
  expect(h.formal.submit.mock.calls[0][1]).toMatchObject({ strategy: 'none', baselineUes: 1 })
})
it.each(['network', 'admission', 'write', 'planner', 'observation', 'pending', 'uncertain', 'runtime', 'stale_preview', 'cap_loading', 'cap_error', 'no_preview', 'inadmissible', 'start_disabled', 'no_intent', 'no_request', 'owned_resources'])
  ('still blocks submission when %s is unavailable', damage => {
    if (damage === 'network') h.ready = false
    if (damage === 'admission') h.admitted = false
    if (damage === 'write') h.formal.caps.write_configured = false
    if (damage === 'planner') h.formal.caps.planner_available = false
    if (damage === 'observation') h.formal.caps.observation_available = false
    if (damage === 'pending') h.formal.pending = true
    if (damage === 'uncertain') h.formal.uncertain = true
    if (damage === 'runtime') h.adapter.blockingError = true
    if (damage === 'stale_preview') (h.values[1] as { at: number }).at -= 60001
    if (damage === 'cap_loading') h.formal.capLoading = true
    if (damage === 'cap_error') h.formal.capError = 'capabilities_connection'
    if (damage === 'no_preview') h.values[1] = null
    if (damage === 'inadmissible') (h.values[1] as any).value.admissible = false
    if (damage === 'start_disabled') h.adapter.snapshot.capabilities.start = false
    if (damage === 'no_intent') h.values[0] = ''
    if (damage === 'no_request') h.city.compiled.request = null
    if (damage === 'owned_resources') h.released = false
    const { submit, tree } = render()
    expect(submit.props.disabled).toBe(true)
    const reasons = elements(tree).find(e => e.props['data-testid'] === 'city-submit-reasons')
    expect(reasons).toBeDefined()
    expect(JSON.stringify(reasons)).not.toContain('操作授權')
    submit.props.onClick()
    expect(h.formal.submit).not.toHaveBeenCalled()
  })
it('distinguishes loading and transport errors from genuinely missing configuration', () => {
  h.formal.capLoading = true
  expect(JSON.stringify(render().tree)).toContain('正在確認 AI 能力，可繼續編輯需求。')
  expect(JSON.stringify(render().tree)).not.toContain('後端寫入未配置')
  h.formal.capLoading = false; h.formal.capError = 'capabilities_timeout'
  expect(JSON.stringify(render().tree)).toContain('AI 能力查詢逾時')
  expect(JSON.stringify(render().tree)).not.toContain('後端寫入未配置')
  h.formal.capError = ''; h.formal.caps.write_configured = false
  expect(JSON.stringify(render().tree)).toContain('後端寫入未配置')
})
it('keeps intent and preview through review/launch/back transitions without a new demand', () => {
  const original = structuredClone(h.values)
  const review = render('review').tree
  const next = elements(review).find(e => e.props.children === '下一步：意圖與啟動')!
  next.props.onClick()
  expect(render('launch').submit.props.disabled).toBe(false)
  render('review'); render('launch')
  expect(h.values.slice(0, 4)).toEqual(original)
  expect(h.formal.submit).not.toHaveBeenCalled()
})
it('updates the expired preview reason exactly at its deadline without typing or polling', async () => {
  vi.useFakeTimers(); vi.stubGlobal('window', globalThis)
  ;(h.values[1] as { at: number }).at = Date.now()
  const { submit } = render(); expect(submit.props.disabled).toBe(false)
  const cleanup = h.effects[1]()
  await vi.advanceTimersByTimeAsync(59999)
  expect(render().submit.props.disabled).toBe(false)
  await vi.advanceTimersByTimeAsync(1)
  expect(h.values[4]).toBe(1)
  const expired = render()
  expect(expired.submit.props.disabled).toBe(true)
  expect(JSON.stringify(expired.tree)).toContain('接入預覽已超過 60 秒')
  expect(h.preview).not.toHaveBeenCalled(); expect(h.formal.submit).not.toHaveBeenCalled()
  if (typeof cleanup === 'function') cleanup()
  expect(vi.getTimerCount()).toBe(0)
})
it.each([true, false])('launch-side re-preview preserves the draft and never auto submits; admissible=%s', async admissible => {
  vi.stubGlobal('window', globalThis)
  h.values[1] = null
  h.preview.mockResolvedValue({ admissible, warnings: [], spatialResolution: { topologyRevision: 3, spatialRevision: 4, resolvedGroups: [] } })
  const initial = render()
  elements(initial.tree).find(e => e.props['data-testid'] === 'launch-refresh-preview')!.props.onClick()
  for (let i = 0; i < 6; i++) await Promise.resolve()
  expect(h.preview).toHaveBeenCalledTimes(1)
  expect(h.formal.submit).not.toHaveBeenCalled()
  expect(h.values[0]).toBe('Protect browsing for 60 seconds')
  expect(render().submit.props.disabled).toBe(!admissible)
})
it('shows a re-preview timeout, preserves the draft and never submits on a late response', async () => {
  vi.useFakeTimers(); vi.stubGlobal('window', globalThis)
  h.values[1] = null
  let resolve!: (value: unknown) => void
  h.preview.mockReturnValue(new Promise(done => { resolve = done }))
  elements(render().tree).find(e => e.props['data-testid'] === 'launch-refresh-preview')!.props.onClick()
  await vi.advanceTimersByTimeAsync(30000)
  expect(JSON.stringify(render().tree)).toContain('接入預覽逾時')
  expect(h.values[0]).toBe('Protect browsing for 60 seconds')
  resolve({ admissible: true }); for (let i = 0; i < 6; i++) await Promise.resolve()
  expect(render().submit.props.disabled).toBe(true)
  expect(h.formal.submit).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})
