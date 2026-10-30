import { describe, expect, it } from 'vitest'
import { parseScenarioSnapshot, validSessionWorkloadSummary, verifiedReceiverMeasurements } from '../onpremScenarios'

function snapshot() {
  const now = new Date().toISOString()
  const workload = { schemaVersion: 'cityverse.session-workload/v2', profileId: 'session-service-offered-load-v2',
    budgetKbpsPerDirection: 400000, requestedUplinkKbps: 1000, requestedDownlinkKbps: 1000,
    requestedCombinedKbps: 2000, appliedUplinkKbps: 1000, appliedDownlinkKbps: 1000,
    appliedCombinedKbps: 2000, capReason: null, comparisonFingerprint: 'a'.repeat(64),
    qosEffectClaimed: false, trafficMode: 'qos' }
  const device = { scenario: 'web_browsing', supi: 'imsi-208930100000001', slice: 'embb', podUid: 'owned-pod', generation: 3 }
  return { roundId: 'native-round', generation: 3, sequence: 1, policyRevision: 0, observedAt: now,
    stage: 'measuring_result', executionOutcome: null, cleanupStatus: 'not_started', trafficMode: 'qos',
    allocation: { baselineUes: 1, total: 1, events: { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 0 } },
    capabilities: { start: true, staticPolicy: false, aiPolicy: false },
    counts: { allocated: 1, registered: 1, pduEstablished: 1, trafficVerified: 1 }, errors: [],
    perUe: [device], measurements: [{ ...device, sessionId: 4, ueIp: '10.101.0.2', tun: 'ueTun0', upf: 'embb',
      roundId: 'native-round', policyRevision: 0, observedAt: now, source: 'receiver',
      receivedUplinkMbps: null, receivedDownlinkMbps: 1, rttMs: null }],
    actions: [{ id: 'native-window', kind: 'traffic_window', requested: true, accepted: true,
      installed: false, effectVerified: false, workload }] }
}

describe('native V2 scenario summary DTO', () => {
  it('counts the unified control profile once when reading requested slice UEs', () => {
    const raw = { ...snapshot(), perUe: [], measurements: [], actions: [],
      allocation: { baselineUes: 0, total: 1,
        events: { video_streaming: 0, realtime_control: 1, connected_traffic: 0, factory_iot: 0 } },
      requestedSliceUes: { embb: 0, urllc: 1, v2x: 0, mmtc: 0 } }
    expect(parseScenarioSnapshot(raw).requestedSliceUes?.urllc).toBe(1)
    expect(() => parseScenarioSnapshot({ ...raw,
      requestedSliceUes: { ...raw.requestedSliceUes, urllc: 2 } })).toThrow('slice counts')
  })
  it('retains device identities, numeric native PDU ID and session-workload summary unchanged', () => {
    const raw = snapshot(), parsed = parseScenarioSnapshot(raw)
    expect(parsed.perUe).toEqual(raw.perUe)
    expect(parsed.measurements[0].sessionId).toBe(4)
    expect(parsed.measurements[0].upf).toBe('embb')
    expect(parsed.actions[0].workload).toEqual(raw.actions[0].workload)
    // A device identity is not enough to invent its full per-session ownership.
    expect(verifiedReceiverMeasurements(parsed)).toEqual([])
  })
  it('rejects wrong-generation devices, invalid PDU IDs, malformed totals and fictional per-UE summary data', () => {
    const wrongDevice = snapshot(); wrongDevice.perUe[0].generation = 2
    expect(() => parseScenarioSnapshot(wrongDevice)).toThrow('identities')
    const wrongPdu = snapshot(); wrongPdu.measurements[0].sessionId = 0
    expect(() => parseScenarioSnapshot(wrongPdu)).toThrow('identities')
    const wrongTotal = snapshot(); wrongTotal.actions[0].workload.appliedCombinedKbps = 1
    expect(() => parseScenarioSnapshot(wrongTotal)).toThrow('workload')
    expect(validSessionWorkloadSummary({ ...snapshot().actions[0].workload, perUe: [] })).toBe(false)
  })
})
