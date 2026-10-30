import type { ScenarioRequest } from '../../onprem/services/onpremScenarios'
import { SCENARIO_EVENTS, type ScenarioGroup } from '../../domain/scenarios'
import { validSpatialDemand, type SpatialDemand } from '../../onprem/services/spatialDemand'
import { validServiceRequirements, type ServiceRequirement } from '../../onprem/services/serviceRequirements'
import type { Building, Point } from './model'

export type CityDemand = SpatialDemand & { label: string; temporary: boolean }
export type ServiceSettings = Partial<Record<ScenarioGroup, ServiceRequirement>>
export { demandNames } from '../../domain/scenarios'
export function buildingEvent(building: Building): ScenarioGroup {
  return building.buildingType === 'arena' ? 'video_streaming' : building.buildingType === 'station' ? 'connected_traffic'
    : building.buildingType === 'warehouse' ? 'realtime_control'
    : building.buildingType === 'factory' ? 'factory_iot' : 'web_browsing'
}
export function pointInCity(point: Point) {
  return [point.x, point.y].every(value => Number.isFinite(value) && value >= 0 && value <= 24)
}
export function compileCityDemand(rows: CityDemand[], services: ServiceSettings, topologyRevision: number): Omit<ScenarioRequest, 'requestId' | 'strategy'> & { strategy: 'none' } {
  const events = Object.fromEntries(SCENARIO_EVENTS.map(key => [key, 0])) as Record<typeof SCENARIO_EVENTS[number], number>
  let baselineUes = 0
  for (const row of rows) { if (row.profileId === 'web_browsing') baselineUes += row.ueCount; else events[row.profileId] += row.ueCount }
  const request = { strategy: 'none' as const, trafficMode: 'qos' as const, baselineUes, events, topologyRevision,
    demandGroups: rows.map(({ label: _label, temporary: _temporary, ...wire }) => structuredClone(wire)),
    serviceRequirements: { schemaVersion: 'cityverse.service-requirements/v2' as const,
      services: Object.fromEntries([...new Set(rows.map(row => row.profileId))].map(key => [key, services[key]])) } }
  const total = baselineUes + Object.values(events).reduce((sum, value) => sum + value, 0)
  if (total < 1 || total > 100 || !validSpatialDemand(request) || !validServiceRequirements(request.serviceRequirements, request)) {
    throw new Error('請設定有效的位置、1–100 個 UE 與服務需求 / Invalid city demand')
  }
  return request
}
