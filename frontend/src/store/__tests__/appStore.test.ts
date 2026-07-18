import { describe, it, expect, beforeEach } from 'vitest'
import { useAppStore } from '../appStore'

const DEFAULT_SLICES_SST = [1, 2, 3, 4]

beforeEach(() => {
  useAppStore.getState().reset()
})

describe('updateMetrics normalization', () => {
  it('fills missing fields with defaults and keeps provided field', () => {
    useAppStore.getState().updateMetrics({ upfCpuPercent: 5 } as any)
    const metrics = useAppStore.getState().metrics!
    expect(metrics.upfCpuPercent).toBe(5)
    expect(metrics.upfPodCount).toBe(0)
    expect(metrics.amfPodCount).toBe(0)
    expect(metrics.gtpPacketsPerSec).toBe(0)
    expect(metrics.pduSessionCount).toBe(0)
    expect(metrics.latencyMs).toBe(0)
    expect(metrics.throughputMbps).toBe(0)
  })

  it('converts numeric strings to numbers', () => {
    useAppStore.getState().updateMetrics({ throughputMbps: '12' } as any)
    const metrics = useAppStore.getState().metrics!
    expect(metrics.throughputMbps).toBe(12)
    expect(typeof metrics.throughputMbps).toBe('number')
  })

  it('does not throw on null/undefined and falls back to all defaults', () => {
    expect(() => useAppStore.getState().updateMetrics(null as any)).not.toThrow()
    let metrics = useAppStore.getState().metrics!
    expect(metrics.upfCpuPercent).toBe(0)
    expect(metrics.upfPodCount).toBe(0)
    expect(metrics.amfPodCount).toBe(0)
    expect(metrics.gtpPacketsPerSec).toBe(0)
    expect(metrics.pduSessionCount).toBe(0)
    expect(metrics.latencyMs).toBe(0)
    expect(metrics.throughputMbps).toBe(0)

    expect(() => useAppStore.getState().updateMetrics(undefined as any)).not.toThrow()
    metrics = useAppStore.getState().metrics!
    expect(metrics.upfCpuPercent).toBe(0)
  })
})

describe('setFree5gcStatus slice normalization', () => {
  it('falls back to 4 default slices when slices is not an array', () => {
    useAppStore.getState().setFree5gcStatus({ slices: null } as any)
    const slices = useAppStore.getState().slices
    expect(slices).toHaveLength(4)
    expect(slices.map((s) => s.sst).sort()).toEqual(DEFAULT_SLICES_SST)
    slices.forEach((s) => expect(s.load).toBe(0))
  })

  it('overrides only the matching slice and keeps others default', () => {
    useAppStore.getState().setFree5gcStatus({ slices: [{ sst: 1, load: 77 }] } as any)
    const slices = useAppStore.getState().slices
    const sst1 = slices.find((s) => s.sst === 1)!
    expect(sst1.load).toBe(77)
    const others = slices.filter((s) => s.sst !== 1)
    others.forEach((s) => expect(s.load).toBe(0))
    expect(slices).toHaveLength(4)
  })

  it('preserves loadSource from the incoming slice (estimated or prometheus)', () => {
    useAppStore.getState().setFree5gcStatus({
      slices: [
        { sst: 1, load: 40, loadSource: 'estimated-from-registered-ues' },
        { sst: 2, load: 60, loadSource: 'prometheus' },
      ],
    } as any)
    const slices = useAppStore.getState().slices
    expect(slices.find((s) => s.sst === 1)!.loadSource).toBe('estimated-from-registered-ues')
    expect(slices.find((s) => s.sst === 2)!.loadSource).toBe('prometheus')
    // slices without a loadSource in the payload stay undefined, not dropped/errored
    expect(slices.find((s) => s.sst === 3)!.loadSource).toBeUndefined()
  })

  it('preserves selectionStage from the incoming slice (configured or active-session)', () => {
    useAppStore.getState().setFree5gcStatus({
      slices: [
        { sst: 1, load: 40, selectionStage: 'active-session' },
        { sst: 2, load: 0, selectionStage: 'configured' },
      ],
    } as any)
    const slices = useAppStore.getState().slices
    expect(slices.find((s) => s.sst === 1)!.selectionStage).toBe('active-session')
    expect(slices.find((s) => s.sst === 2)!.selectionStage).toBe('configured')
    // slices without a selectionStage in the payload stay undefined, not dropped/errored
    expect(slices.find((s) => s.sst === 3)!.selectionStage).toBeUndefined()
  })
})

describe('mergePodComponents (via updateMetrics)', () => {
  it('keeps all 11 default components, retains unknown extras, and filters UNKNOWN', () => {
    useAppStore.getState().updateMetrics({
      podComponents: [
        { component: 'UPF', pods: [{ name: 'upf-0', phase: 'Running' }], desired: 1 },
        { component: 'CUSTOM_EXTRA', pods: [{ name: 'extra-0', phase: 'Running' }], desired: 1 },
        { component: 'UNKNOWN', pods: [{ name: 'unknown-0', phase: 'Running' }], desired: 1 },
      ],
    } as any)

    const pods = useAppStore.getState().pods
    const components = pods.map((p) => p.component)

    const expectedDefaults = ['UPF', 'AMF', 'SMF', 'NEF', 'PCF', 'NSSF', 'NRF', 'UDR', 'AUSF', 'UERANSIM', 'IPERF3']
    expectedDefaults.forEach((c) => expect(components).toContain(c))

    expect(components).toContain('CUSTOM_EXTRA')
    expect(components).not.toContain('UNKNOWN')
  })
})

describe('setFree5gcStatus networkSnapshot without embedded metrics/slices', () => {
  it('falls back to top-level metrics/slices when networkSnapshot omits them (backend dedup)', () => {
    useAppStore.getState().setFree5gcStatus({
      connected: true,
      metrics: { throughputMbps: 42, upfCpuPercent: 10 },
      slices: [{ sst: 1, load: 55 }],
      networkSnapshot: {
        id: 'snap-dedup',
        timestamp: 1234,
        source: 'eks',
        edges: [],
        // metrics/slices intentionally omitted, as the backend no longer embeds them
      },
    } as any)

    const state = useAppStore.getState()
    expect(state.networkSnapshot?.id).toBe('snap-dedup')
    expect(state.networkSnapshot?.metrics.throughputMbps).toBe(42)
    expect(state.networkSnapshot?.slices.find((s) => s.sst === 1)?.load).toBe(55)
    expect(state.metrics?.throughputMbps).toBe(42)
    expect(state.slices.find((s) => s.sst === 1)?.load).toBe(55)
  })
})

describe('ring buffers', () => {
  it('caps metricsHistory at 60 entries', () => {
    for (let i = 0; i < 65; i++) {
      useAppStore.getState().updateMetrics({ upfCpuPercent: i } as any)
    }
    expect(useAppStore.getState().metricsHistory.length).toBe(60)
  })

  it('caps agentLog at 100 entries', () => {
    for (let i = 0; i < 105; i++) {
      useAppStore.getState().appendAgentLog(`msg-${i}`)
    }
    expect(useAppStore.getState().agentLog.length).toBe(100)
  })
})
