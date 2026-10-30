import { describe, expect, it } from 'vitest'
import { acceptScenarioSnapshot, parseScenarioSnapshot, type ScenarioSnapshot } from '../onpremScenarios'

function fixture(): ScenarioSnapshot {
  return { roundId: 'round', generation: 4, sequence: 9, policyRevision: 0, observedAt: '2026-10-15T00:00:00Z',
    stage: 'applying_policy', executionOutcome: null, cleanupStatus: 'pending',
    allocation: { baselineUes: 1, events: { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 0 }, total: 1 },
    counts: { allocated: 0, registered: 0, pduEstablished: 0, trafficVerified: 0 }, capabilities: {},
    perUe: [], measurements: [], actions: [], errors: [],
    agentRuntime: { roundId: 'round', generation: 4, phase: 'apply', role: 'executor', status: 'started',
      tool: 'apply_validated_plan', response_id: 'resp-example', model: 'configured-model', cycle: 0, updatedAt: 1000 } }
}

describe('AI progress is separate from native observations', () => {
  it('accepts bounded phase metadata and updates equal-sequence progress only', () => {
    const current = parseScenarioSnapshot(fixture())
    const incoming = fixture()
    incoming.agentRuntime!.updatedAt = 1001
    incoming.agentRuntime!.phase = 'postcheck'
    incoming.observedAt = '2026-10-16T00:00:00Z'
    incoming.counts.registered = 1
    const result = acceptScenarioSnapshot(current, parseScenarioSnapshot(incoming))
    expect(result.agentRuntime!.phase).toBe('postcheck')
    expect(result.observedAt).toBe(current.observedAt)
    expect(result.counts).toBe(current.counts)
    expect(result.sequence).toBe(current.sequence)
  })

  it.each([{ roundId: 'another' }, { generation: 5 }, { phase: 'delete' }, { role: 'admin' },
    { status: 'success' }, { updatedAt: NaN }, { cycle: -1 }, { tool: 'shell; secret' }, { rawPrompt: 'private' }])(
    'rejects invalid or unscoped metadata %j', (patch) => {
      const snapshot = fixture()
      Object.assign(snapshot.agentRuntime!, patch)
      expect(() => parseScenarioSnapshot(snapshot)).toThrow('Invalid AI progress')
    })

  it('older progress and other rounds cannot replace the current state', () => {
    const current = fixture()
    const incoming = fixture()
    incoming.agentRuntime!.updatedAt = 999
    expect(acceptScenarioSnapshot(current, incoming)).toBe(current)
    incoming.agentRuntime!.updatedAt = 1001
    incoming.roundId = 'another'
    expect(acceptScenarioSnapshot(current, incoming)).toBe(current)
  })
})
