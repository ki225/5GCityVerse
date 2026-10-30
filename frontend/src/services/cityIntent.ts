import { validSpatialDemand } from '../onprem/services/spatialDemand'
import { SCENARIO_EVENTS } from '../onprem/services/onpremScenarios'
import { validServiceRequirements } from '../onprem/services/serviceRequirements'
import type { IntentCityTraffic, IntentSubmission } from './intents'

export interface CityIntentDraft { text: string; city_traffic: IntentCityTraffic }
export interface PendingCityIntent { key: string; payload: CityIntentDraft }
export const PENDING_CITY_INTENT = 'cityverse:formal-intent:pending:v2'
export const ACTIVE_CITY_INTENT = 'cityverse:formal-intent:active:v2'
export function validateCityIntent(value: unknown): asserts value is CityIntentDraft {
  const draft = value as CityIntentDraft, traffic = draft?.city_traffic
  if (!draft || Object.keys(draft).sort().join(',') !== 'city_traffic,text'
    || typeof draft.text !== 'string' || !draft.text.trim() || draft.text.length > 8000
    || !traffic || traffic.strategy !== 'none' || traffic.trafficMode !== 'qos'
    || typeof traffic.requestId !== 'string' || !traffic.requestId || traffic.requestId.length > 128
    || !traffic.events || Object.keys(traffic.events).sort().join(',') !== [...SCENARIO_EVENTS].sort().join(',')
    || Object.keys(traffic).some(key => !['requestId', 'baselineUes', 'events', 'strategy', 'trafficMode', 'topologyRevision', 'demandGroups', 'serviceRequirements'].includes(key))
    || !traffic.demandGroups || !validSpatialDemand(traffic)
    || !traffic.serviceRequirements || !validServiceRequirements(traffic.serviceRequirements, traffic)) throw new Error('invalid_city_intent')
  const counts = [traffic.baselineUes, ...Object.values(traffic.events)]
  if (counts.some(count => !Number.isSafeInteger(count) || count < 0) || counts.reduce((sum, count) => sum + count, 0) < 1
    || counts.reduce((sum, count) => sum + count, 0) > 100
    || new TextEncoder().encode(JSON.stringify(draft)).length > 65536) throw new Error('invalid_city_intent')
}
export function parsePendingCityIntent(raw: string): PendingCityIntent {
  const pending = JSON.parse(raw) as PendingCityIntent
  if (!pending || typeof pending.key !== 'string' || !/^[A-Za-z0-9:-]{1,256}$/.test(pending.key)) throw new Error('invalid_pending_city_intent')
  validateCityIntent(pending.payload)
  return pending
}
export function parseIntentSubmission(raw: unknown): IntentSubmission {
  const value = raw as IntentSubmission
  if (!value || ![value.intent_id, value.operation_id, value.status].every(field => typeof field === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(field))
    || value.traffic_start_mode !== 'runtime') throw new Error('intent_submission_not_confirmed')
  return value
}
