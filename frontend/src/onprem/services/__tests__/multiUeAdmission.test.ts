import { describe, expect, it } from 'vitest'
import { admissionLimit, allocationAdmitted } from '../onpremScenarios'

describe('backend-authorized multi-UE population limits', () => {
  const capability = { applicationMaxUes: 100, verifiedMaxUes: 1,
    verifiedMaxUesScope: 'scenario-offered-load-v1' as const, commissioningEnabled: false,
    candidateMaxUes: null, admissionMaxUes: 1, commissioningProfile: null,
    staticPolicy: false, aiPolicy: false, start: false }
  const demand = (total: number) => ({ baselineUes: 0, total,
    events: { video_streaming: total,  realtime_control: 0, connected_traffic: 0, factory_iot: 0 } })

  it('reads the actual native one-UE limit without claiming AI or write availability', () => {
    expect(admissionLimit(capability)).toBe(1)
    expect(allocationAdmitted(capability, demand(1))).toBe(true)
    expect(allocationAdmitted(capability, demand(2))).toBe(false)
    expect(capability.start).toBe(false)
    expect(capability.aiPolicy).toBe(false)
  })

  it('rejects mismatched counts, profiles and false capacity promotion', () => {
    for (const change of [{ admissionMaxUes: 100 }, { verifiedMaxUes: 0 }, { candidateMaxUes: 100 },
      { commissioningProfile: 'other-profile' }, { staticPolicy: true }]) {
      expect(admissionLimit({ ...capability, ...change })).toBeNull()
    }
  })

  it.each([2, 5, 10, 25, 50, 100])('uses the declared verified %i UE limit without requiring a legacy AI flag', limit => {
    const caps = { ...capability, verifiedMaxUes: limit, admissionMaxUes: limit }
    expect(admissionLimit(caps)).toBe(limit)
    expect(allocationAdmitted(caps, demand(limit))).toBe(true)
    expect(allocationAdmitted(caps, demand(limit + 1))).toBe(false)
  })

  it.each([5, 10, 25, 50, 100])('keeps an authorized %i UE trial distinct from verified capacity', limit => {
    const caps = { ...capability, verifiedMaxUes: 0, admissionMaxUes: limit, candidateMaxUes: limit,
      commissioningEnabled: true, commissioningProfile: 'scenario-offered-load-v1' }
    expect(admissionLimit(caps)).toBe(limit)
    expect(allocationAdmitted(caps, demand(limit))).toBe(true)
    expect(caps.verifiedMaxUes).toBe(0)
  })

  it('sums building and event demand across the city instead of applying a one-UE limit per group', () => {
    const caps = { ...capability, verifiedMaxUes: 100, admissionMaxUes: 100 }
    const mixed = { baselineUes: 30, total: 100, events: { video_streaming: 40,  realtime_control: 15, connected_traffic: 5, factory_iot: 10 } }
    expect(allocationAdmitted(caps, mixed)).toBe(true)
    expect(allocationAdmitted({ ...caps, admissionMaxUes: 50 }, mixed)).toBe(false)
  })
})
