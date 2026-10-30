import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { compileCityDemand, type CityDemand } from '../../../city-map/domain/demand'
import { parseScenarioSnapshot, receiverEvidenceCoverage, allocationAdmitted, verifiedReceiverMeasurements, type ScenarioSnapshot } from '../onpremScenarios'
import { ScenarioAdapterProvider } from '../../components/OnpremRuntime/ScenarioContext'
import nativeFixture from './fixtures/nativeSessionBinding.json'
import { NetworkAiPanel } from '../../components/OnpremRuntime/NetworkAiPanel'
import { OnpremRoundEvidence } from '../../components/OnpremRuntime/ScenarioPanels'
import { mergeObservationRows } from '../roundObservability'

const clock = Date.parse('2026-10-29T11:00:00Z'), now = new Date(clock).toISOString()
const categories = ['web_browsing', 'video_streaming', 'factory_iot', 'connected_traffic'] as const
const slices = ['embb', 'embb', 'mmtc', 'v2x'] as const
const service = { traffic: { protocol: 'udp' as const, offeredUplinkMbpsPerUe: 1, offeredDownlinkMbpsPerUe: 1 } }
const groups: CityDemand[] = categories.map((profileId, i) => ({ groupId: `district-${i}`, label: `District ${i}`,
  temporary: true, profileId, ueCount: 25, sourceLocation: { type: 'point', position: { x: i % 2 ? 18 : 6, y: i > 1 ? 18 : 6 } } }))
function nativeSummary() {
  const demand = compileCityDemand(groups, Object.fromEntries(categories.map(c => [c, service])), 7)
  const perUe = Array.from({ length: 100 }, (_, i) => ({ supi: `imsi-${208930100000001 + i}`,
    scenario: categories[Math.floor(i / 25)], slice: slices[Math.floor(i / 25)],
    generation: 9, podUid: `native-device-pod-${i}`, sessionId: 4, ueIp: `10.101.0.${i + 2}`,
    tun: 'ueTun0', upf: `upf-${slices[Math.floor(i / 25)]}` }))
  return { ...demand, viewSchema: 'cityverse.round-summary/v1', roundId: 'city-100', generation: 9, sequence: 42,
    resolvedRequirements: { revision: 'a'.repeat(64), source: 'user-request' },
    policyRevision: 0, observedAt: now, stage: 'measuring_result', executionOutcome: null, cleanupStatus: 'not_started',
    allocation: { baselineUes: demand.baselineUes, events: demand.events, total: 100 },
    capabilities: { start: true, staticPolicy: false, aiPolicy: false, applicationMaxUes: 100,
      verifiedMaxUes: 0, verifiedMaxUesScope: 'scenario-offered-load-v1', candidateMaxUes: 100,
      admissionMaxUes: 100, commissioningEnabled: true, commissioningProfile: 'scenario-offered-load-v1' },
    counts: { allocated: 100, registered: 100, pduEstablished: 100, trafficVerified: 100 }, actions: [], errors: [], perUe,
    trafficCounts: { scope: 'selected-round-ues', requested: 100, selected: 100, registered: 100,
      pduEstablished: 100, connectivityVerified: 100, observedTransmitting: 100,
      measurementScope: 'last-receiver-window', observedAt: now },
    measurements: perUe.map(row => ({ ...row, roundId: 'city-100', policyRevision: 0, observedAt: now,
      source: 'receiver', receivedUplinkMbps: 1, receivedDownlinkMbps: 1, rttMs: null })) }
}
const render = (snapshot: ScenarioSnapshot, fresh = true, at = clock) => renderToStaticMarkup(<OnpremRoundEvidence
  adapter={{ snapshot, error: '', pending: false, uncertain: false, now: at, fresh }} />)

function typedSummary() {
  const raw = nativeSummary()
  const bindings = raw.perUe.map((owner, i) => {
    const row = { ...structuredClone(nativeFixture.binding), ...owner, deviceId: `device-${i}`,
      nonceDigest: i.toString(16).padStart(64, '0'),
      demandGroupId: groups[Math.floor(i / 25)].groupId, sourceLocation: groups[Math.floor(i / 25)].sourceLocation }
    const key = JSON.parse(row.sessionKey)
    key[1] = row.supi; key[2] = row.sessionId
    row.sessionKey = JSON.stringify(key); row.pduSessionId = Number(row.sessionId)
    row.proof = { ...row.proof, sessionKey: row.sessionKey, podUid: row.podUid,
      containerId: row.containerId, processEpoch: row.processEpoch, upfEpoch: row.upfEpoch,
      spatialRan: { ...row.proof.spatialRan, supi: row.supi, demandGroupId: row.demandGroupId } }
    return row
  })
  return { ...raw, observedAt: nativeFixture.wrapper.observedAt,
    perUe: raw.perUe.map(({ supi, scenario, slice, podUid, generation }) => ({ supi, scenario, slice, podUid, generation })),
    nativeSessionBindings: { ...nativeFixture.wrapper, generation: raw.generation, roundId: raw.roundId,
      populationRevision: 0, bindings },
    measurements: bindings.map(owner => ({ ...owner, roundId: raw.roundId, policyRevision: 0,
      source: 'receiver', observedAt: nativeFixture.wrapper.observedAt,
      receivedUplinkMbps: 1 as number | null, receivedDownlinkMbps: 1 as number | null, rttMs: null,
      windows: (['ul', 'dl'] as const).map(direction => ({ direction, binding: { ...owner },
        source: 'native-iperf3-receiver', receivedMbps: 1, finishedAt: 110, receiverFinishedAt: 110 })) })) }
}

describe('city100 native evidence boundaries (fixtures, not a live capacity claim)', () => {
  it('conserves four geographic groups and decodes100 devices with100 distinct Pod/session bindings', () => {
    const raw = nativeSummary(), parsed = parseScenarioSnapshot(raw)
    expect(parsed.allocation.total).toBe(100)
    expect(raw.demandGroups).toEqual(groups.map(({ label: _label, temporary: _temporary, ...wire }) => wire))
    expect(new Set(parsed.perUe.map(r => r.podUid)).size).toBe(100)
    expect(new Set(parsed.perUe.map(r => `${r.supi}:${r.sessionId}:${r.podUid}`)).size).toBe(100)
    expect(parsed.measurements).toEqual(raw.measurements)
    expect(verifiedReceiverMeasurements(parsed, clock)).toHaveLength(100)
    expect(receiverEvidenceCoverage(parsed, clock)).toMatchObject({ expected: 100, sessionBindings: 100,
      uplink: 100, downlink: 100, duplex: 100, missingUplink: 0, missingDownlink: 0, status: 'complete' })
    const html = render(parsed)
    expect(html).toContain('data-expected="100"')
    expect(html).toContain('data-duplex="100"')
    expect(html).toContain('data-verified-max-ues="0"')
    expect(html).toContain('data-candidate-max-ues="100"')
    expect(html).toContain('data-traffic-visible="true"')
    expect(html).toContain('量測覆蓋不代表 SLA 達標')
  })
  it.each(['session', 'binding', 'ul', 'dl', 'stale', 'owner', 'duplicate'] as const)('retains100 denominator with missing %s evidence', fault => {
    const raw = nativeSummary()
    if (fault === 'session') raw.measurements.pop()
    if (fault === 'binding') {
      const minimal = raw.perUe[99] as Partial<typeof raw.perUe[number]>
      delete minimal.sessionId; delete minimal.ueIp; delete minimal.tun; delete minimal.upf
    }
    if (fault === 'ul') (raw.measurements[99] as { receivedUplinkMbps: number | null }).receivedUplinkMbps = null
    if (fault === 'dl') (raw.measurements[99] as { receivedDownlinkMbps: number | null }).receivedDownlinkMbps = null
    if (fault === 'stale') raw.measurements[99].observedAt = new Date(clock - 16000).toISOString()
    if (fault === 'owner') raw.measurements[99].podUid = 'foreign-pod'
    if (fault === 'duplicate') raw.measurements[99] = { ...raw.measurements[98] }
    const parsed = parseScenarioSnapshot(raw)
    const coverage = receiverEvidenceCoverage(parsed, clock)
    expect(coverage.expected).toBe(100)
    expect(coverage.duplex).toBeLessThan(100)
    expect(coverage.status).toBe('unknown')
    expect(render(parsed)).toContain('證據不完整／未知')
    expect(render(parsed)).toContain('100 / 100 / —')
    expect(render(parsed)).toContain('data-expected="100"')
  })
  it('does not multiply minimal V2 device identity or PDU count into session ownership', () => {
    const raw = nativeSummary()
    const minimal = { ...raw, perUe: raw.perUe.map(({ scenario, slice, supi, podUid, generation }) => ({ scenario, slice, supi, podUid, generation })) }
    const parsed = parseScenarioSnapshot(minimal)
    expect(parsed.counts.pduEstablished).toBe(100)
    expect(receiverEvidenceCoverage(parsed, clock)).toMatchObject({ expected: 100, sessionBindings: 0, duplex: 0, status: 'unknown' })
    expect(verifiedReceiverMeasurements(parsed, clock)).toEqual([])
    expect(render(parsed)).toContain('data-traffic-visible="false"')
  })
  it.each([{ schemaVersion: 'cityverse.current-state/v2' }, { perSession: null }, { devices: [] }])('never downgrades an incomplete V2 view to legacy bindings: %j', marker => {
    const parsed = parseScenarioSnapshot({ ...nativeSummary(), ...marker })
    expect(receiverEvidenceCoverage(parsed, clock)).toMatchObject({ expected: 100, sessionBindings: 0, duplex: 0, status: 'unknown' })
    expect(verifiedReceiverMeasurements(parsed, clock)).toEqual([])
  })
  it('uses the controlled V2 proof for100 devices/sessions and original bidirectional receiver windows', () => {
    const parsed = parseScenarioSnapshot(typedSummary())
    expect(receiverEvidenceCoverage(parsed, 110000)).toMatchObject({ expected: 100, sessionBindings: 100,
      uplink: 100, downlink: 100, duplex: 100, status: 'complete' })
    expect(verifiedReceiverMeasurements(parsed, 110000)).toHaveLength(100)
    expect(render(parsed, true, 110000)).toContain('data-duplex="100"')
  })
  it.each(['last-binding', 'nonce-unsealed', 'unavailable', 'closing', 'expired', 'last-pod', 'direction', 'old-window', 'window-pfcp', 'metric-mismatch'] as const)(
    'V2 proof %s never becomes complete or falls back to legacy evidence', fault => {
      const raw = typedSummary()
      if (fault === 'last-binding') raw.nativeSessionBindings.bindings.pop()
      if (fault === 'nonce-unsealed') raw.nativeSessionBindings.bindings[99].nonceDigest = 'unsealed'
      if (fault === 'unavailable') raw.nativeSessionBindings.available = false
      if (fault === 'closing') raw.nativeSessionBindings.closingCensusDigest = 'f'.repeat(64)
      if (fault === 'expired') raw.nativeSessionBindings.validUntil = '1970-01-01T00:01:49+00:00'
      if (fault === 'last-pod') raw.nativeSessionBindings.bindings[99].podUid = 'foreign'
      if (fault === 'direction') raw.measurements[99].windows.pop()
      if (fault === 'old-window') raw.measurements[99].windows[1].receiverFinishedAt = 90
      if (fault === 'window-pfcp') raw.measurements[99].windows[1].binding.pfcpSeid = 'foreign'
      if (fault === 'metric-mismatch') raw.measurements[99].receivedDownlinkMbps = 999
      const parsed = parseScenarioSnapshot(raw), coverage = receiverEvidenceCoverage(parsed, 110000)
      expect(coverage.expected).toBe(100)
      expect(coverage.duplex).toBeLessThan(100)
      expect(coverage.status).toBe('unknown')
      expect(verifiedReceiverMeasurements(parsed, 110000).length).toBeLessThan(100)
      expect(render(parsed, true, 110000)).toContain('接收端證據不完整／未知')
    })
  it('measured zero is coverage, never positive traffic or SLA success', () => {
    const raw = nativeSummary()
    raw.measurements.forEach(r => { r.receivedUplinkMbps = 0; r.receivedDownlinkMbps = 0 })
    const parsed = parseScenarioSnapshot(raw)
    expect(receiverEvidenceCoverage(parsed, clock).status).toBe('complete')
    expect(verifiedReceiverMeasurements(parsed, clock)).toEqual([])
    expect(render(parsed)).toContain('data-traffic-visible="false"')
    expect(render(parsed)).toContain('data-sla-status="unknown"')
  })
  it('revokes coverage when whole snapshot is stale or a transport read failed', () => {
    const parsed = parseScenarioSnapshot(nativeSummary())
    expect(receiverEvidenceCoverage(parsed, clock + 16000)).toMatchObject({ expected: 100, duplex: 0, status: 'unknown' })
    expect(render(parsed, false)).toContain('data-duplex="0"')
    expect(render(parsed, false)).toContain('data-traffic-visible="false"')
  })
  it('shows declared verified capability separately, rejects101 and does not alter offered rates', () => {
    const raw = nativeSummary(), parsed = parseScenarioSnapshot(raw)
    const verified = { ...parsed.capabilities, verifiedMaxUes: 100, candidateMaxUes: null,
      commissioningEnabled: false, commissioningProfile: null, capacityStatus: 'receipt_verified' as const }
    expect(render({ ...parsed, capabilities: verified })).toContain('data-verified-max-ues="100"')
    expect(allocationAdmitted(verified, { ...parsed.allocation, total: 101, baselineUes: 26 })).toBe(false)
    expect(() => compileCityDemand([{ ...groups[0], ueCount: 101 }], { web_browsing: service }, 7)).toThrow()
    expect(raw.serviceRequirements!.services.web_browsing!.traffic).toEqual(service.traffic)
  })
  it('stage3 keeps100 denominator, unknown traffic and explicit display truncation', () => {
    const raw = nativeSummary(); raw.measurements.pop()
    const parsed = parseScenarioSnapshot(raw)
    const html = renderToStaticMarkup(<ScenarioAdapterProvider value={{ snapshot: parsed, error: '', pending: false, uncertain: false, now: clock, fresh: true,
      observation: { data: null, events: [], samples: [], error: '', gap: true, truncated: true } }}><NetworkAiPanel /></ScenarioAdapterProvider>)
    expect(html).toContain('— / 100')
    expect(html).toContain('歷史紀錄有缺口或已達顯示上限')
  })
  it('bounded history drops oldest display rows without replacing full100 denominator', () => {
    const first = Array.from({ length: 1200 }, (_, i) => ({ sequence: i + 1 }))
    const next = Array.from({ length: 100 }, (_, i) => ({ sequence: i + 1201 }))
    const retained = mergeObservationRows(first, next, 1200)
    expect(retained).toHaveLength(1200)
    expect(retained[0].sequence).toBe(101)
    expect(retained[retained.length - 1].sequence).toBe(1300)
    expect(receiverEvidenceCoverage(parseScenarioSnapshot(nativeSummary()), clock).expected).toBe(100)
  })
})
