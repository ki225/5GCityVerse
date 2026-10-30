import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { parseScenarioSnapshot, validWorkload, type RoundWorkload, type ScenarioGroup, type ScenarioSnapshot } from '../onpremScenarios'
import { OnpremPolicyPanel } from '../../components/OnpremRuntime/ScenarioPanels'
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext'

const slices: Record<ScenarioGroup, string> = { web_browsing: 'embb', video_streaming: 'embb',  connected_traffic: 'v2x', realtime_control: 'urllc', factory_iot: 'mmtc' }
function probe(n = 100, rate = 100): RoundWorkload {
  const groups = Object.keys(slices) as ScenarioGroup[]
  return { schemaVersion: 'cityverse.round-workload/v1', profileId: 'controlled_qer_probe_v1',
    workloadKind: 'diagnostic_probe', probeKbpsPerDirection: rate, budgetKbpsPerDirection: 10000,
    perUe: Array.from({ length: n }, (_, i) => ({ supi: `imsi-${String(i + 1).padStart(15, '0')}`,
      scenario: groups[i % groups.length], slice: slices[groups[i % groups.length]], requestedKbps: rate, appliedKbps: rate })),
    requestedUplinkKbps: n * rate, requestedDownlinkKbps: n * rate, requestedCombinedKbps: n * rate * 2,
    appliedUplinkKbps: n * rate, appliedDownlinkKbps: n * rate, appliedCombinedKbps: n * rate * 2,
    capReason: null, comparisonFingerprint: 'a'.repeat(64), qosEffectClaimed: false }
}
function legacy(): RoundWorkload {
  const w = probe(1); delete w.workloadKind; delete w.probeKbpsPerDirection
  return { ...w, profileId: 'scenario-offered-load-v1', perUe: [{ ...w.perUe[0], requestedKbps: 1000, appliedKbps: 1000 }],
    requestedUplinkKbps: 1000, requestedDownlinkKbps: 1000, requestedCombinedKbps: 2000,
    appliedUplinkKbps: 1000, appliedDownlinkKbps: 1000, appliedCombinedKbps: 2000 }
}
function snapshot(w = probe()): ScenarioSnapshot {
  return { roundId: 'round', generation: 1, sequence: 1, policyRevision: 0, observedAt: null,
    stage: 'measuring_result', executionOutcome: null, cleanupStatus: 'pending',
    allocation: { baselineUes: w.perUe.filter((r) => r.scenario === 'web_browsing').length,
      events: { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 0,
        ...Object.fromEntries(['video_streaming', 'realtime_control', 'connected_traffic', 'realtime_control', 'factory_iot'].map((name) =>
          [name, w.perUe.filter((r) => r.scenario === name).length])) }, total: w.perUe.length },
    counts: { allocated: null, registered: null, pduEstablished: null, trafficVerified: null },
    capabilities: { start: true, applicationMaxUes: 100, staticPolicy: false, aiPolicy: true, verifiedMaxUes: 0,
      commissioningEnabled: false, candidateMaxUes: 100, admissionMaxUes: 100, commissioningProfile: null, capacityStatus: 'unverified_ai_trial' },
    perUe: w.perUe.map((r, i) => ({ supi: r.supi, scenario: r.scenario, sessionId: '1', podUid: 'pod',
      ueIp: `10.101.0.${i + 1}`, tun: `tun${i}`, upf: `upf-${r.slice}` })),
    actions: ['web_browsing', 'result'].map((id) => ({ id, kind: 'traffic_window', requested: true, accepted: false,
      installed: false, effectVerified: false, workload: structuredClone(w) })), measurements: [], errors: [] }
}

it.each([[1, 1], [7, 73], [100, 100]])('accepts exact diagnostic metadata for %i UEs at %i kbps', (n, rate) => {
  expect(validWorkload(probe(n, rate))).toBe(true)
  expect(parseScenarioSnapshot(snapshot(probe(n, rate))).actions[0].workload?.workloadKind).toBe('diagnostic_probe')
})
it('preserves legacy application metadata and rejects diagnostic fields disguised as legacy', () => {
  expect(validWorkload(legacy())).toBe(true)
  expect(validWorkload({ ...legacy(), workloadKind: 'diagnostic_probe', probeKbpsPerDirection: 100 })).toBe(false)
  expect(validWorkload({ ...probe(), profileId: 'scenario-offered-load-v1' })).toBe(false)
})
it.each([
  { profileId: 'custom' }, { schemaVersion: 'cityverse.round-workload/v2' }, { workloadKind: 'application' },
  { probeKbpsPerDirection: undefined }, { probeKbpsPerDirection: true }, { probeKbpsPerDirection: 0 },
  { probeKbpsPerDirection: 101 }, { probeKbpsPerDirection: 1.5 }, { probeKbpsPerDirection: '100' },
  { budgetKbpsPerDirection: 20000 }, { capReason: 'requested_per_direction_load_exceeds_budget' },
  { qosEffectClaimed: true }, { requestedUplinkKbps: 9999 }, { requestedDownlinkKbps: 9999 },
  { requestedCombinedKbps: 10000 }, { appliedUplinkKbps: 9999 }, { appliedDownlinkKbps: 9999 },
  { appliedCombinedKbps: 10000 }, { comparisonFingerprint: 'bad' }, { fixtureCapKbps: 50 },
])('rejects malformed or undeclared diagnostic metadata %j', (change) => {
  const w = { ...probe(), ...change }
  expect(validWorkload(w)).toBe(false)
  expect(() => parseScenarioSnapshot(snapshot(w as RoundWorkload))).toThrow('workload')
})
it.each(['workloadKind', 'probeKbpsPerDirection'])('rejects missing diagnostic field %s', (field) => {
  const w = probe(); delete (w as unknown as Record<string, unknown>)[field]
  expect(validWorkload(w)).toBe(false)
})
it.each([0, 101])('rejects diagnostic population %i', (n) => expect(validWorkload(probe(n))).toBe(false))
it.each([{ requestedKbps: 99 }, { appliedKbps: 99 }, { appliedKbps: 101 }, { slice: 'v2x' },
  { supi: 'not-an-imsi' }, { rateHint: 100 }])('rejects incorrect diagnostic per-UE data %j', (change) => {
  const w = probe(); Object.assign(w.perUe[0], change)
  expect(validWorkload(w)).toBe(false)
})
it('rejects duplicate owners, changed result fingerprint and changed result probe rate', () => {
  const duplicate = probe(); duplicate.perUe[1].supi = duplicate.perUe[0].supi
  expect(validWorkload(duplicate)).toBe(false)
  const s = snapshot(); s.actions[1].workload!.comparisonFingerprint = 'b'.repeat(64)
  expect(() => parseScenarioSnapshot(s)).toThrow('frozen plan')
  s.actions[1].workload = probe(100, 50)
  expect(() => parseScenarioSnapshot(s)).toThrow('frozen plan')
  s.actions[1].workload = probe(); s.perUe[0].upf = 'upf-v2x'
  expect(() => parseScenarioSnapshot(s)).toThrow('ownership')
})
it('labels the diagnostic profile separately without claiming an application improvement', () => {
  const s = snapshot()
  const adapter: ScenarioAdapter = { snapshot: s, error: '', pending: false, uncertain: false, now: Date.now(), fresh: false,  }
  const html = renderToStaticMarkup(<ScenarioAdapterProvider value={adapter}><OnpremPolicyPanel /></ScenarioAdapterProvider>)
  expect(html).toContain('data-profile-id="controlled_qer_probe_v1"')
  expect(html).toContain('data-workload-kind="diagnostic_probe"')
  expect(html).toContain('data-probe-kbps-per-direction="100"')
  expect(html).toContain('data-qos-effect-claimed="false"')
  expect(html).toMatch(/受控 QER 診斷流量|Controlled QER diagnostic traffic/)
  expect(html).toMatch(/不能用來宣稱應用效能改善|does not represent normal application traffic/)
  expect(html).not.toContain('data-testid="application-policy-audit"')
})
