import { describe, expect, it } from 'vitest'
import fixture from './fixtures/scopedTrafficCapability.json'
import { decodeScopedTrafficCapabilities } from '../scopedTrafficCapabilities'

describe('scoped capacity display only', () => {
  it('keeps exact 100 scope separate from global capacity and live readiness', () => {
    expect(decodeScopedTrafficCapabilities([fixture], 100)).toEqual([fixture])
    const verified = { ...fixture, verifiedDevices: 100, validUntil: 110, evidenceId: 'a'.repeat(64) }
    expect(decodeScopedTrafficCapabilities([verified], 100)).toEqual([verified])
    for (const patch of [{ policyWritesAllowed: true }, { uePduReady: true }, { validUntil: 99 }, { evidenceId: null }, { verifiedDevices: 99 }])
      expect(decodeScopedTrafficCapabilities([{ ...verified, ...patch }], 100)).toEqual([])
    for (const patch of [{ devices: 99 }, { pdusPerDevice: true }, { offeredDownlinkKbpsPerDevice: 101 }, { demandGroups: fixture.scope.demandGroups.slice(0, -1) }])
      expect(decodeScopedTrafficCapabilities([{ ...verified, scope: { ...verified.scope, ...patch } }], 100)).toEqual([])
    expect(decodeScopedTrafficCapabilities(undefined, 100)).toEqual([])
  })
})
