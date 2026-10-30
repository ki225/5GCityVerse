import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach,expect,it,vi } from 'vitest'
import { OnpremRoundOutcome } from '../../components/OnpremRuntime/ScenarioPanels'
import {
parseScenarioSnapshot,
validSlaAssessment,
validWorkload,
type RoundWorkload,type ScenarioSnapshot
} from '../onpremScenarios'

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

function qos(): RoundWorkload {
  return { schemaVersion: 'cityverse.round-workload/v1', profileId: 'scenario-qos-offered-load-v1',
    trafficMode: 'qos', budgetKbpsPerDirection: 400000,
    perUe: Array.from({ length: 5 }, (_, i) => ({ supi: `imsi-${208930100000001 + i}`,
      scenario: 'video_streaming', slice: 'embb', requestedKbps: 4000, appliedKbps: 4000 })),
    requestedUplinkKbps: 20000, requestedDownlinkKbps: 20000, requestedCombinedKbps: 40000,
    appliedUplinkKbps: 20000, appliedDownlinkKbps: 20000, appliedCombinedKbps: 40000,
    capReason: null, comparisonFingerprint: 'a'.repeat(64), qosEffectClaimed: false }
}

function snapshot(): ScenarioSnapshot {
  return { roundId: 'round', requestId: 'request', generation: 1, sequence: 1, policyRevision: 0,
    stage: 'completed', cleanupStatus: 'verified', executionOutcome: 'passed', observedAt: null,
    trafficMode: 'qos', allocation: { baselineUes: 0,
      events: { video_streaming: 5,  connected_traffic: 0, realtime_control: 0, factory_iot: 0 }, total: 5 },
    counts: { allocated: 5, registered: 5, pduEstablished: 5, trafficVerified: 5 },
    capabilities: {}, perUe: qos().perUe.map((r) => ({ ...r, upf: 'upf-embb', sessionId: '1',
      podUid: 'pod', ueIp: '10.101.0.1', tun: 'ueTun0' })), measurements: [], errors: [],
    actions: [{ id: 'window', kind: 'traffic_window', requested: true, accepted: true,
      installed: true, effectVerified: true, workload: qos() }] }
}

it('accepts unscaled QoS load above 10 Mbps and rejects mode/rate tampering', () => {
  expect(validWorkload(qos())).toBe(true)
  const scaled = qos(); scaled.perUe[0].appliedKbps = 200
  expect(validWorkload(scaled)).toBe(false)
  expect(validWorkload({ ...qos(), trafficMode: 'functional' })).toBe(false)
  expect(validWorkload({ ...qos(), budgetKbpsPerDirection: 10000 })).toBe(false)
  expect(parseScenarioSnapshot(snapshot()).trafficMode).toBe('qos')
  expect(() => parseScenarioSnapshot({ ...snapshot(), trafficMode: 'functional' })).toThrow('Workload')
})

it('shows independent unmet SLA even when execution and traffic effect passed', () => {
  const s = snapshot()
  s.sla = { status: 'not_met', reason: 'criteria_evaluated', scope: 'last-result-window', historical: true,
    windowId: 'window', scenarios: [{ scenario: 'video_streaming', sampleId: 'window:video_streaming', status: 'not_met', reason: 'criteria_evaluated' }] }
  expect(validSlaAssessment(s.sla)).toBe(true)
  const html = renderToStaticMarkup(<OnpremRoundOutcome snapshot={s} />)
  expect(html).toContain('data-execution-outcome="passed"')
  expect(html).toContain('data-sla-status="not_met"')
  expect(validSlaAssessment({ ...s.sla, status: 'met' })).toBe(false)
})
