import type { ScenarioRequest } from '../onprem/services/onpremScenarios'

/** Stable public v2 payload: counts are derived by the backend from the groups. */
export function profileRequest(request: ScenarioRequest) {
  return {
    schemaVersion: 'cityverse.city-traffic/v2',
    requestId: request.requestId, strategy: request.strategy, trafficMode: request.trafficMode,
    topologyRevision: request.topologyRevision,
    demandGroups: structuredClone(request.demandGroups),
    serviceRequirements: structuredClone(request.serviceRequirements),
  }
}
