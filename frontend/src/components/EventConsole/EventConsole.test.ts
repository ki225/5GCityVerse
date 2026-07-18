import { describe, it, expect } from 'vitest'
import { EVENT_SCALE_STEP, EVENTS, extendPollingDeadline, isBaselineFlow, scenarioRuntimeLabel, selectSessionBearerFlows, SERVICE_NEEDS, shouldEarlyExitOnWsSignal, STATUS_POLL_GRACE_MS } from './EventConsole'
import type { AgentDecision, PacketFlow } from '../../types'
import { useAppStore } from '../../store/appStore'

describe('novice 5G content', () => {
  it('defines four service-need categories in plain language', () => {
    expect(SERVICE_NEEDS.map((need) => need.type)).toEqual(['eMBB', 'URLLC', 'mMTC', 'V2X'])
    expect(SERVICE_NEEDS.find((need) => need.type === 'eMBB')?.needZh).toContain('很多資料')
    expect(SERVICE_NEEDS.find((need) => need.type === 'URLLC')?.needZh).toContain('不能等')
    expect(SERVICE_NEEDS.find((need) => need.type === 'mMTC')?.titleZh).toContain('MIoT')
    expect(SERVICE_NEEDS.find((need) => need.type === 'V2X')?.needZh).toContain('車輛')
  })

  it('gives every scenario a primary category and a reason without presenting fixed SST or 5QI values', () => {
    expect(Object.fromEntries(EVENTS.map((event) => [event.type, event.slice]))).toEqual({
      concert: 'eMBB',
      typhoon: 'URLLC',
      accident: 'V2X',
      medical: 'URLLC',
      iot_surge: 'mMTC / MIoT',
    })
    expect(EVENTS.every((event) => event.whyZh.length > 10 && event.whyEn.length > 10)).toBe(true)
    expect(EVENTS.map((event) => event.slice).join(' ')).not.toMatch(/SST|5QI/)
  })

  it('states that typhoon and emergency-room traffic can include other service needs', () => {
    const typhoon = EVENTS.find((event) => event.type === 'typhoon')!
    const medical = EVENTS.find((event) => event.type === 'medical')!
    expect(typhoon.alsoZh).toContain('mMTC')
    expect(typhoon.alsoZh).toContain('eMBB')
    expect(medical.alsoZh).toContain('eMBB')
    expect(medical.alsoZh).toContain('mMTC')
  })
})

describe('event scale controls', () => {
  it('uses one shared integer step so number and range inputs accept identical values', () => {
    expect(EVENT_SCALE_STEP).toBe(1)
  })
})

describe('session bearer filtering', () => {
  const flows = [
    { id: 'baseline', active: true, plane: 'user', scenario: 'baseline' },
    { id: 'concert', active: true, plane: 'user', scenario: 'concert' },
    { id: 'typhoon', active: true, plane: 'user', scenario: 'typhoon' },
    { id: 'control', active: true, plane: 'control', scenario: 'concert' },
  ] as PacketFlow[]

  it('keeps baseline but excludes global scenarios not selected in this session', () => {
    expect(selectSessionBearerFlows(flows, ['concert']).map((flow) => flow.id)).toEqual(['baseline', 'concert'])
    expect(selectSessionBearerFlows(flows, []).map((flow) => flow.id)).toEqual(['baseline'])
  })
})

describe('polling deadline', () => {
  it('extends through the backend traffic end plus inference grace', () => {
    expect(extendPollingDeadline(10_000, 20_000)).toBe(20_000 + STATUS_POLL_GRACE_MS)
    expect(extendPollingDeadline(200_000, 20_000)).toBe(200_000)
  })
})

describe('beginAgentRound', () => {
  it('clears the completed AI center when a newly accepted round starts', () => {
    useAppStore.setState({
      agentDecision: { agentName: 'old-round' } as AgentDecision,
      agentDecisionHistory: [{ executionId: 'old-execution' }] as ReturnType<typeof useAppStore.getState>['agentDecisionHistory'],
      roundReportReady: true,
      agentLog: ['old evidence'],
      runtimePrime: { status: 'success' } as ReturnType<typeof useAppStore.getState>['runtimePrime'],
      orchestrationStage: 'complete',
      isReportGenerating: true,
      reportRequestId: 7,
    })

    useAppStore.getState().beginAgentRound()

    const state = useAppStore.getState()
    expect(state.agentDecision).toBeNull()
    expect(state.agentDecisionHistory).toEqual([])
    expect(state.roundReportReady).toBe(false)
    expect(state.agentLog).toEqual([])
    expect(state.runtimePrime).toBeNull()
    expect(state.orchestrationStage).toBe('queued')
    expect(state.isReportGenerating).toBe(false)
    expect(state.reportRequestId).toBe(7)
  })

  it('locks the strategy selected when the accepted round begins', () => {
    useAppStore.getState().reset()
    useAppStore.getState().setSliceStrategy('ai')
    useAppStore.getState().beginAgentRound()

    expect(useAppStore.getState().submittedSliceStrategy).toBe('ai')
    useAppStore.getState().setSliceStrategy('static')
    expect(useAppStore.getState().sliceStrategy).toBe('ai')

    useAppStore.getState().reset()
    expect(useAppStore.getState().sliceStrategy).toBe('none')
    expect(useAppStore.getState().submittedSliceStrategy).toBeNull()
  })
})

describe('active scenario lifecycle', () => {
  it('keeps scenarios visible while backend orchestration is still running', () => {
    useAppStore.setState({
      isSimulating: true,
      activeScenarios: [{
        type: 'concert',
        label: 'AR Concert',
        startedAt: 1_000,
        endsAt: 2_000,
        eventScale: 10,
        cityResidents: 100,
        executionId: 'exec-1',
      }],
    })

    useAppStore.getState().pruneActiveScenarios(3_000)

    expect(useAppStore.getState().activeScenarios).toHaveLength(1)
  })

  it('synchronizes all scenarios in a batch to the backend traffic window', () => {
    useAppStore.setState({
      isSimulating: true,
      activeScenarios: ['concert', 'typhoon'].map((type) => ({
        type: type as 'concert' | 'typhoon',
        label: type,
        startedAt: 1_000,
        endsAt: 2_000,
        eventScale: 10,
        cityResidents: 100,
        executionId: 'exec-1',
      })),
    })

    useAppStore.getState().syncActiveScenarioWindow('exec-1', 50_000, 170_000)

    expect(useAppStore.getState().activeScenarios.map(({ startedAt, endsAt }) => [startedAt, endsAt])).toEqual([
      [50_000, 170_000],
      [50_000, 170_000],
    ])
  })
})

describe('scenario runtime label', () => {
  const text = (zh: string) => zh

  it('shows preparation instead of a countdown before traffic is measured', () => {
    expect(scenarioRuntimeLabel({ trafficObserved: false, endsAt: 5_000, now: 1_000, isRunning: true, decisionReady: false }, text)).toBe('流量準備中')
  })

  it('keeps the scenario visibly alive when its nominal timer expires during AI reasoning', () => {
    expect(scenarioRuntimeLabel({ trafficObserved: true, endsAt: 2_000, now: 3_000, isRunning: true, decisionReady: false }, text)).toBe('AI 推理中 · 流量持續')
  })

  it('shows the remaining seconds while the traffic window still has time', () => {
    expect(scenarioRuntimeLabel({ trafficObserved: true, endsAt: 62_000, now: 1_500, isRunning: true, decisionReady: false }, text)).toBe('61s')
  })
})

describe('shouldEarlyExitOnWsSignal', () => {
  it('does not early-exit for a multi-scenario batch even when the global WS signal advanced (verificationSummary)', () => {
    const agentDecision = { verificationSummary: { verdict: 'pass' } } as unknown as AgentDecision
    expect(shouldEarlyExitOnWsSignal(2, agentDecision, 'action')).toBe(false)
  })

  it('does not early-exit for a multi-scenario batch even when the global WS signal advanced (blocked stage)', () => {
    expect(shouldEarlyExitOnWsSignal(3, null, 'blocked')).toBe(false)
  })

  it('keeps polling a single scenario after the decision arrives', () => {
    const agentDecision = { verificationSummary: { verdict: 'pass' } } as unknown as AgentDecision
    expect(shouldEarlyExitOnWsSignal(1, agentDecision, 'action')).toBe(false)
  })

  it('lets the terminal REST status end a blocked scenario', () => {
    expect(shouldEarlyExitOnWsSignal(1, null, 'blocked')).toBe(false)
  })

  it('does not early-exit for a single scenario when neither signal is present', () => {
    expect(shouldEarlyExitOnWsSignal(1, null, 'planning')).toBe(false)
  })
})

describe('isBaselineFlow', () => {
  it('treats an undefined scenario as baseline', () => {
    expect(isBaselineFlow(undefined)).toBe(true)
  })

  it('treats an unrecognized scenario string as baseline', () => {
    expect(isBaselineFlow('some_unknown_scenario')).toBe(true)
  })

  it('does not treat a known burst-event scenario as baseline', () => {
    expect(isBaselineFlow('typhoon')).toBe(false)
    expect(isBaselineFlow('concert')).toBe(false)
  })
})
