import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSessionState, loadSessionState, saveSessionState } from '../sessionState'

function stubSessionStorage() {
  const values = new Map<string, string>()
  vi.stubGlobal('window', {
    sessionStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  })
  return values
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('submitted round session state', () => {
  it('persists execution ids and submitted strategy for reload recovery', () => {
    stubSessionStorage()

    saveSessionState({
      activeEvent: 'realtime_control',
      activeScenarios: [{
        type: 'realtime_control',
        label: 'ER Surge',
        startedAt: 100,
        endsAt: 200,
        eventScale: 650,
        cityResidents: 180_000,
        executionId: 'exec-realtime_control',
      }],
      isSimulating: true,
      sliceStrategy: 'ai',
      submittedSliceStrategy: 'ai',
      agentDecision: null,
      agentDecisionHistory: [],
      roundReportReady: false,
      agentLog: ['accepted'],
      runtimePrime: { status: 'running', executionId: 'exec-realtime_control', profileId: 'realtime_control' },
      orchestrationStage: 'runtime_priming',
    })

    expect(loadSessionState()).toMatchObject({
      activeEvent: 'realtime_control',
      activeScenarios: [{ executionId: 'exec-realtime_control', type: 'realtime_control' }],
      isSimulating: true,
      sliceStrategy: 'ai',
      submittedSliceStrategy: 'ai',
      orchestrationStage: 'runtime_priming',
    })
  })

  it('drops malformed entries that cannot be polled', () => {
    const values = stubSessionStorage()
    values.set('5gcityverse.sessionState', JSON.stringify({
      version: 1,
      savedAt: Date.now(),
      activeScenarios: [{ type: 'video_streaming' }],
      isSimulating: true,
      sliceStrategy: 'ai',
    }))

    expect(loadSessionState()?.activeScenarios).toEqual([])
  })

  it('clears stale persisted rounds', () => {
    const values = stubSessionStorage()
    vi.setSystemTime(new Date('2026-09-07T00:00:00Z'))
    values.set('5gcityverse.sessionState', JSON.stringify({
      version: 1,
      savedAt: Date.now() - 25 * 60 * 60 * 1000,
      activeScenarios: [{ type: 'video_streaming', executionId: 'old' }],
      isSimulating: true,
      sliceStrategy: 'ai',
    }))

    expect(loadSessionState()).toBeNull()
    expect(values.has('5gcityverse.sessionState')).toBe(false)
  })

  it('removes persisted state on clear', () => {
    const values = stubSessionStorage()
    values.set('5gcityverse.sessionState', '{}')

    clearSessionState()

    expect(values.has('5gcityverse.sessionState')).toBe(false)
  })
})
