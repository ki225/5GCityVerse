import { SCENARIO_EVENTS, type ScenarioGroup } from '../../domain/scenarios'
export type DemandEvent = ScenarioGroup
export type SpatialDemand = {
  groupId: string; profileId: DemandEvent; ueCount: number; serviceId?: string
  sourceLocation: { type: 'point'; buildingId?: string; position: { x: number; y: number } }
}
const groups: DemandEvent[] = ['web_browsing', ...SCENARIO_EVENTS]
const id = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,100}$/.test(value)
const exact = (value: object, keys: string[]) => Object.keys(value).every(key => keys.includes(key))

/** Mirrors the backend's disjoint point allocation, including baseline demand. */
export function validSpatialDemand(request: {
  topologyRevision?: number; demandGroups?: SpatialDemand[]; baselineUes: number; events: Record<string, number>
}): boolean {
  if (request.topologyRevision === undefined && request.demandGroups === undefined) return true
  if (!Number.isSafeInteger(request.topologyRevision) || request.topologyRevision! < 1
    || !Array.isArray(request.demandGroups) || !request.demandGroups.length || request.demandGroups.length > 100) return false
  const seen = new Set<string>(), counts = Object.fromEntries(groups.map(group => [group, 0]))
  for (const row of request.demandGroups) {
    if (!row || !exact(row, ['groupId', 'profileId', 'ueCount', 'serviceId', 'sourceLocation'])
      || !id(row.groupId) || seen.has(row.groupId) || !groups.includes(row.profileId)
      || !Number.isSafeInteger(row.ueCount) || row.ueCount < 1 || row.serviceId !== undefined && !id(row.serviceId)) return false
    const location = row.sourceLocation, point = location?.position
    if (!location || !exact(location, ['type', 'position', 'buildingId']) || location.type !== 'point'
      || location.buildingId !== undefined && !id(location.buildingId) || !point || !exact(point, ['x', 'y'])
      || ![point.x, point.y].every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 24)) return false
    seen.add(row.groupId); counts[row.profileId] += row.ueCount
  }
  return groups.every(group => counts[group] === (group === 'web_browsing' ? request.baselineUes : request.events[group]))
}
