import { describe, it, expect } from 'vitest'
import { parseCityObservation, segmentState, siteState, type SiteHealth, type PathSegment } from './observability'

const now = Date.now()
const site: SiteHealth = { id: 'central', kind: 'central', source: 'kubernetes-workload-readiness', health: 'healthy',
  reason: 'required_replicas_ready', readyReplicas: 2, desiredReplicas: 2, workloads: [],
  observedAt: new Date(now - 1000).toISOString(), validUntil: new Date(now + 1000).toISOString() }
const segment: PathSegment = { fromId: 'gnb-0-0', toId: 'central', interface: 'N3', state: 'verified', observedAt: site.observedAt, validUntil: site.validUntil }
describe('city observability display contract', () => {
  it('expires health and routes independently without renewing the evidence', () => {
    expect(siteState(site, now)).toBe('healthy')
    expect(siteState(site, now + 1001)).toBe('unknown')
    expect(siteState(site, now, true)).toBe('unknown')
    expect(segmentState(segment, now)).toBe('verified')
    expect(segmentState(segment, now + 1001)).toBe('unknown')
    expect(segmentState({ ...segment, state: 'configured', validUntil: null }, now)).toBe('configured')
  })
  it('rejects invalid inventories instead of displaying a successful empty fleet', () => {
    const base = { schemaVersion: 'cityverse.city-observability/v1', topologyRevision: 1, topologyDigest: 'digest',
      scope: null, infra: [site], paths: [], pathStatus: 'no_active_round', usageAvailable: false }
    expect(parseCityObservation(base).infra).toHaveLength(1)
    expect(() => parseCityObservation({ ...base, infra: [site, site] })).toThrow()
    expect(() => parseCityObservation({ ...base, infra: [{ ...site, readyReplicas: -1 }] })).toThrow()
    expect(() => parseCityObservation({ ...base, infra: [{ ...site, validUntil: 'invalid' }] })).toThrow()
  })
})
