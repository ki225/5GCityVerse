import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { admissionLimit, allocationAdmitted, parseScenarioSnapshot, selectedActiveScenario, validWorkload, type RoundWorkload, type ScenarioSnapshot } from '../onpremScenarios'
import { OnpremRoundEvidence, OnpremPolicyPanel } from '../../components/OnpremRuntime/ScenarioPanels'
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext'

const caps = { start: true, staticPolicy: false, aiPolicy: false, verifiedMaxUes: 0,
  commissioningEnabled: true, candidateMaxUes: 5, admissionMaxUes: 5, commissioningProfile: 'scenario-offered-load-v1' }
const allocation = { baselineUes: 0, events: { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 1 }, total: 1 }
function snapshot(): ScenarioSnapshot { return { roundId: null, generation: null, sequence: 0, policyRevision: 0,
  observedAt: null, stage: 'idle', executionOutcome: null, cleanupStatus: 'not_started',
  allocation: { ...allocation, events: { ...allocation.events, factory_iot: 0 }, total: 0 },
  counts: { allocated: null, registered: null, pduEstablished: null, trafficVerified: null },
  capabilities: { ...caps }, actions: [], perUe: [], measurements: [], errors: [] } }
function workload(): RoundWorkload { return { schemaVersion: 'cityverse.round-workload/v1', profileId: 'scenario-offered-load-v1',
  budgetKbpsPerDirection: 10000, perUe: [1, 2, 3].map((i) => ({ supi: `imsi-20893010000000${i}`, scenario: 'video_streaming', slice: 'embb', requestedKbps: 4000, appliedKbps: i === 1 ? 3334 : 3333 })),
  requestedUplinkKbps: 12000, requestedDownlinkKbps: 12000, requestedCombinedKbps: 24000,
  appliedUplinkKbps: 10000, appliedDownlinkKbps: 10000, appliedCombinedKbps: 20000,
  capReason: 'requested_per_direction_load_exceeds_budget', comparisonFingerprint: 'a'.repeat(64), qosEffectClaimed: false } }

it('keeps verified0 separate from explicit candidate and rejects missing/malformed capability', () => {
  expect(admissionLimit(caps)).toBe(5)
  expect(admissionLimit({ ...caps, candidateMaxUes: 500, admissionMaxUes: 500 })).toBe(500)
  expect(admissionLimit({ ...caps, applicationMaxUes: 500, candidateMaxUes: 500, admissionMaxUes: 500 })).toBeNull()
  expect(admissionLimit({ ...caps, applicationMaxUes: 501 })).toBeNull()
  expect(admissionLimit({ start: true })).toBeNull()
  expect(admissionLimit({ ...caps, commissioningEnabled: false, aiPolicy: true, verifiedMaxUes: 100, candidateMaxUes: null, admissionMaxUes: 100, commissioningProfile: null })).toBe(100)
  expect(caps.verifiedMaxUes).toBe(0)
  expect(admissionLimit({ ...caps, verifiedMaxUes: 1 })).toBeNull()
  expect(admissionLimit({ ...caps, verifiedMaxUesScope: 'scenario-offered-load-v1' })).toBe(5)
  expect(admissionLimit({ ...caps, verifiedMaxUesScope: 'native-connectivity' as 'scenario-offered-load-v1' })).toBeNull()
})
it('accepts explicit summary schema without inventing observation freshness', () => {
  const s = { ...snapshot(), viewSchema: 'cityverse.round-summary/v1' as const }
  const original = structuredClone(s)
  expect(parseScenarioSnapshot(s)).toEqual(original)
  expect(s).toEqual(original)
  expect(s.observedAt).toBeNull()
  expect(() => parseScenarioSnapshot({ ...s, viewSchema: 'untrusted-view' })).toThrow()
})
it.each([0, 1, 5, 6, 50, 500, 501])('submission uses the candidate admission limit for total %i', (total) => {
  expect(allocationAdmitted(caps, { ...allocation, events: { ...allocation.events, factory_iot: total }, total })).toBe(total >= 1 && total <= 5)
  if (total !== 1) expect(allocationAdmitted(caps, { ...allocation, total })).toBe(false)
  expect(allocationAdmitted(undefined, { ...allocation, total })).toBe(false)
})
it.each([{ verifiedMaxUes: 1 }, { verifiedMaxUes: 5 }, { admissionMaxUes: 50 }, { candidateMaxUes: 6 },
  { commissioningEnabled: 'true' }, { commissioningProfile: 'custom' }, { staticPolicy: true }, { aiPolicy: true }])('fails closed mutated capability %j', (change) => {
  const s = snapshot(); Object.assign(s.capabilities, change)
  expect(admissionLimit(s.capabilities)).toBeNull()
  expect(() => parseScenarioSnapshot(s)).toThrow('commissioning')
})
it('selects actual nonzero event but preserves valid explicit selections', () => {
  expect(selectedActiveScenario('web_browsing', allocation)).toBe('factory_iot')
  const both = { ...allocation, events: { ...allocation.events, realtime_control: 1 }, total: 2 }
  expect(selectedActiveScenario('factory_iot', both)).toBe('factory_iot')
  expect(selectedActiveScenario('realtime_control', both)).toBe('realtime_control')
  expect(selectedActiveScenario('realtime_control', snapshot().allocation)).toBe('realtime_control')
})
it.each([{ appliedCombinedKbps: 1 }, { qosEffectClaimed: true }, { profileId: 'custom' },
  { requestedUplinkKbps: true }, { budgetKbpsPerDirection: 20000 }, { comparisonFingerprint: 'bad' }, { capReason: null }])('rejects malformed offered-load metadata %j', (change) => {
  expect(validWorkload({ ...workload(), ...change })).toBe(false)
  const s = snapshot(); s.actions = [{ id: 'traffic', kind: 'traffic_window', requested: true, accepted: false, installed: false, effectVerified: false, workload: { ...workload(), ...change } as RoundWorkload }]
  expect(() => parseScenarioSnapshot(s)).toThrow('workload')
})
it('renders candidate and requested/applied workloads without claiming QoS effect', () => {
  const s = snapshot(); s.actions = [{ id: 'traffic', kind: 'traffic_window', requested: true, accepted: false, installed: false, effectVerified: false, workload: workload() }]
  const adapter: ScenarioAdapter = { snapshot: s, error: '', pending: false, uncertain: false, now: Date.now(), fresh: false,  }
  const admission = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter} />)
  expect(admission).toContain('data-verified-max-ues="0"')
  expect(admission).toContain('data-candidate-max-ues="5"')
  expect(admission).toContain('data-commissioning-enabled="true"')
  const html = renderToStaticMarkup(<ScenarioAdapterProvider value={adapter}><OnpremPolicyPanel /></ScenarioAdapterProvider>)
  expect(html).toContain('data-requested-uplink-kbps="12000"')
  expect(html).toContain('data-applied-uplink-kbps="10000"')
  expect(html).toContain('data-qos-effect-claimed="false"')
  expect(html).not.toContain('data-testid="application-policy-audit"')
})

it('rejects wrong fixed scenario slice/rate and noncanonical applied shares', () => {
  for (const change of [{ slice: 'v2x' }, { scenario: 'web_browsing' }, { requestedKbps: 20000 }, { appliedKbps: 3333 }]) {
    const w = workload(); Object.assign(w.perUe[0], change)
    expect(validWorkload(w)).toBe(false)
  }
})
it('accepts a bounded 500-UE workload projection without claiming QoS effect', () => {
  const w: RoundWorkload = { ...workload(), perUe: Array.from({ length: 500 }, (_, i) => ({
    supi: `imsi-${String(208930100000001 + i)}`, scenario: 'web_browsing', slice: 'embb',
    requestedKbps: 1000, appliedKbps: 20,
  })), requestedUplinkKbps: 500000, requestedDownlinkKbps: 500000, requestedCombinedKbps: 1000000,
  appliedUplinkKbps: 10000, appliedDownlinkKbps: 10000, appliedCombinedKbps: 20000 }
  expect(validWorkload(w)).toBe(true)
  expect(validWorkload({ ...w, perUe: [...w.perUe, w.perUe[0]] })).toBe(false)
})
it('joins workload to exact native owners and requires identical baseline/result plans', () => {
  const s = snapshot(); const w = workload()
  s.generation = 1; s.stage = 'measuring_result'; s.roundId = 'round'
  s.allocation = { ...allocation, events: { ...allocation.events, factory_iot: 0, video_streaming: 3 }, total: 3 }
  s.perUe = w.perUe.map((r, i) => ({ supi: r.supi, scenario: r.scenario, sessionId: '1', podUid: 'pod',
    ueIp: `10.101.0.${i + 1}`, tun: `uesimtun${i}`, upf: 'upf-embb' }))
  s.actions = ['web_browsing', 'result'].map((id) => ({ id, kind: 'traffic_window', requested: true, accepted: false,
    installed: false, effectVerified: false, workload: structuredClone(w) }))
  const original = structuredClone(s)
  expect(parseScenarioSnapshot(s)).toEqual(original)
  expect(s).toEqual(original)
  s.actions[1].workload!.comparisonFingerprint = 'b'.repeat(64)
  expect(() => parseScenarioSnapshot(s)).toThrow('frozen plan')
  s.actions[1].workload = structuredClone(w)
  s.perUe[0].upf = 'upf-v2x'
  expect(() => parseScenarioSnapshot(s)).toThrow('ownership')
  s.perUe[0].upf = 'upf-embb'; s.perUe.pop()
  expect(() => parseScenarioSnapshot(s)).toThrow('ownership')
})
