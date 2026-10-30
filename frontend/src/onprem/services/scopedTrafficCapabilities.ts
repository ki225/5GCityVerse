import { validSpatialDemand, type SpatialDemand } from './spatialDemand'

export type ScopedTrafficCapability = {
  schemaVersion: 'cityverse.scoped-traffic-capabilities/v1'; profileId: 'web_browsing'
  verifiedDevices: 0 | 100; validUntil: number | null; evidenceId: string | null
  policyWritesAllowed: false; uePduReady: false
  scope: {
    profileId: 'web_browsing'; devices: 100; pdusPerDevice: 1; slice: 'embb'; trafficMode: 'qos'
    offeredUplinkKbpsPerDevice: 100; offeredDownlinkKbpsPerDevice: 100
    requirementsSchema: 'cityverse.service-requirements/v2'; topologyRevision: number; demandGroups: SpatialDemand[]
  }
}
const exact = (value: object, keys: string[]) => Object.keys(value).sort().join(',') === [...keys].sort().join(',')
/** Display descriptors only: never changes global capacity, readiness or write authority. */
export function decodeScopedTrafficCapabilities(value: unknown, now = Date.now() / 1000): ScopedTrafficCapability[] {
  if (!Array.isArray(value) || value.length !== 1) return []
  const row = value[0] as ScopedTrafficCapability
  if (!row || typeof row !== 'object' || !exact(row, ['schemaVersion', 'profileId', 'verifiedDevices', 'scope', 'validUntil', 'policyWritesAllowed', 'uePduReady', 'evidenceId'])
    || row.schemaVersion !== 'cityverse.scoped-traffic-capabilities/v1' || row.profileId !== 'web_browsing'
    || ![0, 100].includes(row.verifiedDevices) || row.policyWritesAllowed !== false || row.uePduReady !== false) return []
  const s = row.scope
  if (!s || typeof s !== 'object' || !exact(s, ['profileId', 'devices', 'pdusPerDevice', 'slice', 'trafficMode', 'offeredUplinkKbpsPerDevice', 'offeredDownlinkKbpsPerDevice', 'requirementsSchema', 'topologyRevision', 'demandGroups'])
    || s.profileId !== 'web_browsing' || s.devices !== 100 || s.pdusPerDevice !== 1 || s.slice !== 'embb' || s.trafficMode !== 'qos'
    || s.offeredUplinkKbpsPerDevice !== 100 || s.offeredDownlinkKbpsPerDevice !== 100
    || s.requirementsSchema !== 'cityverse.service-requirements/v2'
    || !validSpatialDemand({ ...s, baselineUes: 100, events: { video_streaming: 0, realtime_control: 0, connected_traffic: 0, factory_iot: 0 } })
    || s.demandGroups.some(group => group.profileId !== 'web_browsing')) return []
  if (row.verifiedDevices === 100) {
    if (typeof row.validUntil !== 'number' || !Number.isFinite(row.validUntil) || row.validUntil <= now
      || typeof row.evidenceId !== 'string' || !/^[a-f0-9]{64}$/.test(row.evidenceId)) return []
  } else if (row.validUntil !== null || row.evidenceId !== null) return []
  return [row]
}
