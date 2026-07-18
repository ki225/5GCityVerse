import { describe, expect, it } from 'vitest'
import type { AgentDecision, NetworkMetrics, PacketFlow, SliceStatus } from '../../types'
import {
  hasPassedSla,
  INITIAL_AI_ALLOCATION,
  isSliceStrategyLocked,
  formatExperienceMetric,
  describeCitizenUeExperience,
  finiteMetric,
  formatOptionalGtp,
  loadSourceBadgeContent,
  measureCitizenUeExperience,
  measureActiveScenarioExperiences,
  measureScenarioExperience,
  resolveAppliedAiAllocation,
  resolveScenarioTrafficState,
  selectionStageBadgeContent,
  shouldShowCitizenUeComparison,
} from './SliceDashboard'

describe('optional network metrics and localization', () => {
  it('does not coerce unavailable GTP telemetry to raw null or zero', () => {
    const text = (_zh: string, en: string) => en
    expect(finiteMetric(null)).toBeNull()
    expect(finiteMetric(undefined)).toBeNull()
    expect(formatOptionalGtp(null, text)).toBe('unavailable')
    expect(formatOptionalGtp(12, text)).toBe('12.0')
  })

  it('localizes runtime slice badges', () => {
    expect(loadSourceBadgeContent('eks-runtime-logs', 'en')?.label).toBe('measured (UE log)')
    expect(selectionStageBadgeContent('active-session', 'en')?.label).toBe('in session')
    expect(selectionStageBadgeContent('configured', 'en')?.label).toBe('configured')
  })
})

describe('isSliceStrategyLocked', () => {
  it('allows changes only before a scenario round starts', () => {
    expect(isSliceStrategyLocked('idle')).toBe(false)
    expect(isSliceStrategyLocked('queued')).toBe(true)
    expect(isSliceStrategyLocked('runtime_priming')).toBe(true)
    expect(isSliceStrategyLocked('planning')).toBe(true)
    expect(isSliceStrategyLocked('complete')).toBe(true)
  })
})

describe('resolveAppliedAiAllocation', () => {
  const baseline = { shares: { ...INITIAL_AI_ALLOCATION }, decisionKey: null }
  const observedSlices = [
    { type: 'eMBB', load: 80 },
    { type: 'URLLC', load: 10 },
    { type: 'mMTC', load: 5 },
    { type: 'V2X', load: 5 },
  ] as SliceStatus[]

  it('keeps the applied allocation locked while live load changes before AI execution', () => {
    expect(resolveAppliedAiAllocation(baseline, observedSlices, null)).toBe(baseline)
    expect(resolveAppliedAiAllocation(baseline, observedSlices, {
      executionId: 'round-pending',
      completedAt: undefined,
      actions: [{ type: 'nef_qos', status: 'success' }],
    } as AgentDecision)).toBe(baseline)
  })

  it('does not change allocation when a completed round only observed the network', () => {
    expect(resolveAppliedAiAllocation(baseline, observedSlices, {
      executionId: 'round-observe-only',
      completedAt: '2026-07-14T02:00:00Z',
      actions: [{ type: 'prometheus', status: 'success' }],
    } as AgentDecision)).toBe(baseline)
  })

  it('commits one allocation snapshot after a successful network-policy action', () => {
    const applied = resolveAppliedAiAllocation(baseline, observedSlices, {
      executionId: 'round-applied',
      completedAt: '2026-07-14T02:00:00Z',
      actions: [{ type: 'nef_qos', status: 'success' }],
    } as AgentDecision)

    expect(applied).toEqual({
      shares: { eMBB: 80, URLLC: 10, mMTC: 5, V2X: 5 },
      decisionKey: 'round-applied',
    })
    expect(resolveAppliedAiAllocation(applied, [
      { type: 'eMBB', load: 5 },
      { type: 'URLLC', load: 80 },
      { type: 'mMTC', load: 10 },
      { type: 'V2X', load: 5 },
    ] as SliceStatus[], {
      executionId: 'round-applied',
      completedAt: '2026-07-14T02:00:00Z',
      actions: [{ type: 'nef_qos', status: 'success' }],
    } as AgentDecision)).toBe(applied)
  })
})

describe('measureCitizenUeExperience', () => {
  it('uses the real UE TUN probe for everyday citizen experience', () => {
    expect(measureCitizenUeExperience({
      throughputMbps: 120,
      latencyMs: 1,
      ueTunProbe: {
        ready: true,
        throughputMbps: 7.5,
        latencyMs: 28,
        packetLossPercent: 0.5,
        receivedPackets: 4,
      },
    } as NetworkMetrics)).toEqual({
      throughputMbps: 7.5,
      latencyMs: 28,
      packetLossPercent: 0.5,
      observed: true,
      source: 'ue-tun-probe',
    })
  })

  it('never substitutes aggregate iperf throughput for a citizen UE measurement', () => {
    expect(measureCitizenUeExperience({ throughputMbps: 120, latencyMs: 1 } as NetworkMetrics)).toEqual({
      throughputMbps: 0,
      latencyMs: 0,
      packetLossPercent: 0,
      observed: false,
      source: 'ue-tun-probe',
    })
  })

  it('keeps low-rate UE probe evidence visible instead of rounding it to zero', () => {
    expect(formatExperienceMetric(0.001)).toBe('0.001')
  })

  it('distinguishes an established bearer from a missing bearer while the probe starts', () => {
    const text = (zh: string) => zh
    const missing = measureCitizenUeExperience(null)
    expect(describeCitizenUeExperience(missing, text, 1).label).toBe('TUN bearer 已建立，品質探測啟動中')
    expect(describeCitizenUeExperience(missing, text, 0).label).toBe('等待市民手機建立 TUN bearer')
  })

  it('does not treat low-rate ping probe traffic as evidence of congestion', () => {
    const text = (zh: string) => zh
    expect(describeCitizenUeExperience({
      throughputMbps: 0.001,
      latencyMs: 4.9,
      packetLossPercent: 0,
      observed: true,
      source: 'ue-tun-probe',
    }, text, 1, 5).label).toBe('TUN bearer 正常，探測封包低延遲且無丟包')
  })
})

describe('measureScenarioExperience', () => {
  it('uses only the scenario user-plane path without summing repeated topology edges', () => {
    const flows = [
      { id: 'a', active: true, plane: 'user', scenario: 'concert', throughputMbps: 32, latencyMs: 19 },
      { id: 'b', active: true, plane: 'user', scenario: 'concert', throughputMbps: 30, latencyMs: 18 },
      { id: 'c', active: true, plane: 'control', scenario: 'concert', throughputMbps: 999, latencyMs: 99 },
      { id: 'd', active: true, plane: 'user', scenario: 'medical', throughputMbps: 8, latencyMs: 12 },
    ] as PacketFlow[]

    expect(measureScenarioExperience(flows, 'concert')).toEqual({
      throughputMbps: 30,
      latencyMs: 19,
      observed: true,
    })
  })

  it('does not invent measurements without an observed scenario bearer', () => {
    expect(measureScenarioExperience([], 'medical')).toEqual({ throughputMbps: 0, latencyMs: 0, observed: false })
  })

  it('keeps measurements separate when a batch shares one execution id', () => {
    const scenarios = [
      { type: 'concert', executionId: 'shared-round', startedAt: 1 },
      { type: 'typhoon', executionId: 'shared-round', startedAt: 1 },
      { type: 'iot_surge', executionId: 'shared-round', startedAt: 1 },
    ] as Parameters<typeof measureActiveScenarioExperiences>[0]
    const flows = [
      { id: 'concert', active: true, scenario: 'concert', throughputMbps: 800, latencyMs: 12 },
      { id: 'typhoon', active: true, scenario: 'typhoon', throughputMbps: 5, latencyMs: 8 },
      { id: 'iot', active: true, scenario: 'iot_surge', throughputMbps: 2.4, latencyMs: 16 },
    ] as PacketFlow[]

    expect(measureActiveScenarioExperiences(scenarios, flows)).toEqual({
      'shared-round:concert': { throughputMbps: 800, latencyMs: 12, observed: true },
      'shared-round:typhoon': { throughputMbps: 5, latencyMs: 8, observed: true },
      'shared-round:iot_surge': { throughputMbps: 2.4, latencyMs: 16, observed: true },
    })
  })
})

describe('resolveScenarioTrafficState', () => {
  it('distinguishes a sent request from a measured user-plane flow', () => {
    expect(resolveScenarioTrafficState(
      { throughputMbps: 0, latencyMs: 0, observed: false },
      'concert',
      { status: 'running', expectedScenarios: ['concert'] },
    )).toBe('pending')
  })

  it('surfaces a scenario that the backend failed to observe', () => {
    expect(resolveScenarioTrafficState(
      { throughputMbps: 0, latencyMs: 0, observed: false },
      'concert',
      { status: 'traffic_not_observed', missingScenarios: ['concert'] },
    )).toBe('missing')
  })

  it('prefers actual bearer evidence over an earlier missing status', () => {
    expect(resolveScenarioTrafficState(
      { throughputMbps: 30, latencyMs: 18, observed: true },
      'concert',
      { status: 'traffic_not_observed', missingScenarios: ['concert'] },
    )).toBe('measured')
  })
})

describe('hasPassedSla', () => {
  it('requires both reported latency and throughput checks to pass', () => {
    const decision = {
      validationReport: {
        sla_result: {
          status: 'passed',
          latency_ms: { passed: true },
          throughput_mbps: { passed: false },
        },
      },
    } as unknown as AgentDecision
    expect(hasPassedSla(decision)).toBe(false)
  })

  it('accepts a passed verification summary when no validation report exists', () => {
    expect(hasPassedSla({ verificationSummary: { status: 'passed' } } as AgentDecision)).toBe(true)
  })
})

describe('shouldShowCitizenUeComparison', () => {
  const measurement = {
    throughputMbps: 8,
    latencyMs: 30,
    packetLossPercent: 1,
    observed: true,
    source: 'ue-tun-probe',
  } as const

  it('requires both a successful slicing action and a passed SLA', () => {
    expect(shouldShowCitizenUeComparison(measurement, measurement, {
      executionId: 'round-applied',
      completedAt: '2026-07-14T02:00:00Z',
      actions: [{ type: 'nef_qos', status: 'success' }],
      verificationSummary: { status: 'passed' },
    } as AgentDecision)).toBe(true)

    expect(shouldShowCitizenUeComparison(measurement, measurement, {
      executionId: 'round-observed',
      completedAt: '2026-07-14T02:00:00Z',
      actions: [{ type: 'prometheus', status: 'success' }],
      verificationSummary: { status: 'passed' },
    } as AgentDecision)).toBe(false)
  })
})
