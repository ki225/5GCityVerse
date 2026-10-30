import { SERVICE_PROFILE_IDS } from '../../domain/scenarios'
import { profileRequest } from '../../services/profileRequest'
import { apiBaseUrl } from '../../services/runtimeConfig'
import { sessionHeaders } from '../../services/browserSession'
import type { ScenarioAllocation, ScenarioGroup, ScenarioRequest } from './onpremScenarios'

export type MetricKey = 'receivedUplinkMbps' | 'receivedDownlinkMbps' | 'rttMs' | 'lossPercent'
export type SlaStatus = 'met' | 'not_met' | 'unknown'
export type ServiceSlaTargets = { scope: 'per-ue' | 'service-total' | 'slice-total'; minUplinkMbps?: number | null;
  minDownlinkMbps?: number | null; maxMeanRttMs?: number | null; maxLossPercent?: number | null }
export type Evaluation = { windowSeconds: number; sampleIntervalSeconds: number; minTimeCompliance: number; minUeCompliance: number }
export type ServiceRequirement = { traffic: { protocol: 'udp'; offeredUplinkMbpsPerUe: number; offeredDownlinkMbpsPerUe: number };
  sla?: ServiceSlaTargets; evaluation?: Evaluation }
export type ServiceRequirements = { schemaVersion: 'cityverse.service-requirements/v2'; services: Partial<Record<ScenarioGroup, ServiceRequirement>>;
  sliceObjectives?: Partial<Record<string, { sla: ServiceSlaTargets; evaluation: Evaluation }>> }
export type MetricAssessment = { status: SlaStatus; reason?: string; target: number; coverageRatio: number; lowerBound: number; upperBound: number;
  observedMean?: number | null; observedMin?: number | null; observedMax?: number | null; requiredTimeCompliance?: number; requiredUeCompliance?: number; expectedSeconds?: number; unknownSeconds?: number }
export type ServiceAssessmentRow = { scenario?: string; slice?: string; scope: string; status: SlaStatus; reason: string;
  expectedUes: number; observedUes: number; passedUes?: number; failedUes?: number; unknownUes?: number;
  startedAt: number; finishedAt: number; complete: boolean; sla: ServiceSlaTargets; evaluation: Evaluation;
  metrics: Partial<Record<MetricKey, MetricAssessment>>;
  perUe?: Array<{ ueRef: string; status: SlaStatus; metrics: Partial<Record<MetricKey, MetricAssessment>> }> }
export type ServiceAssessmentWindow = { status: SlaStatus; reason: string; scope: string; historical: true; complete: boolean;
  startedAt: number | null; finishedAt: number | null; services: ServiceAssessmentRow[]; slices: ServiceAssessmentRow[] }
export type ServiceSlaAssessment = { schemaVersion: 'cityverse.service-sla/v1'; revision: string; status: SlaStatus; reason: string;
  scope: 'latest-complete-evaluation-window'; historical: true; latestWindow: ServiceAssessmentWindow; wholeRound: ServiceAssessmentWindow }
export type CityAccessCatalog = { source: 'operator-configuration'; topologyRevision: number; spatialRevision: string; coordinateUnit: 'km'; nativeConnectivityVerified: false }
export type ServiceCapabilities = { cityAccessCatalog?: CityAccessCatalog; schemaVersion: 'cityverse.service-requirements/v2'; protocols: string[];
  maxOfferedKbpsPerUePerDirection: number; maxEvaluationWindowSeconds: number; minSampleIntervalSeconds: number;
  rtt: { statistic: string; sameTrafficWindowAvailable: boolean; reason: string };
  trafficDefaults: Record<ScenarioGroup, { slice: string; protocol: 'udp'; offeredUplinkMbpsPerUe: number; offeredDownlinkMbpsPerUe: number }> }
export type SpatialResolution = { topologyRevision: number; spatialRevision: string; resolvedGroups: Array<{ groupId: string; gnbId: string; cellId: string; endpoint: string; distanceKm: number }> }
export type ServicePreview = { spatialResolution?: SpatialResolution; admissible: boolean; networkCapacityVerified: false; resolvedRequirements: { revision: string;
  services: Record<string, unknown>; slices: Record<string, { ueCount: number; offeredUplinkKbps: number; offeredDownlinkKbps: number; requiredUplinkKbps: number; requiredDownlinkKbps: number }> } | null;
  capabilities: Omit<ServiceCapabilities, 'trafficDefaults'>; warnings: Array<{ code: string; detail?: string; scenario?: string }> }

const groups = SERVICE_PROFILE_IDS
const slices = ['embb', 'urllc', 'mmtc', 'v2x']
const metricKeys: MetricKey[] = ['receivedUplinkMbps', 'receivedDownlinkMbps', 'rttMs', 'lossPercent']
const targets = ['minUplinkMbps', 'minDownlinkMbps', 'maxMeanRttMs', 'maxLossPercent'] as const
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0
const ratio = (v: unknown): v is number => finite(v) && v <= 1
const status = (v: unknown): v is SlaStatus => ['met', 'not_met', 'unknown'].includes(String(v))
const object = (v: unknown): v is Record<string, any> => Boolean(v && typeof v === 'object' && !Array.isArray(v))
const exact = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).every((k) => keys.includes(k))
function validEvaluation(e: any): e is Evaluation {
  return object(e) && exact(e, ['windowSeconds', 'sampleIntervalSeconds', 'minTimeCompliance', 'minUeCompliance'])
    && Number.isSafeInteger(e.windowSeconds) && Number.isSafeInteger(e.sampleIntervalSeconds)
    && e.sampleIntervalSeconds >= 1 && e.windowSeconds <= 3600 && e.windowSeconds >= e.sampleIntervalSeconds
    && e.windowSeconds % e.sampleIntervalSeconds === 0 && ratio(e.minTimeCompliance) && e.minTimeCompliance > 0
    && ratio(e.minUeCompliance) && e.minUeCompliance > 0
}
function validTargets(s: any, allowed: string[]) {
  return object(s) && allowed.includes(s.scope) && exact(s, ['scope', ...targets])
    && targets.some((k) => s[k] != null) && targets.every((k) => s[k] == null || finite(s[k]) && s[k] <= (k === 'maxLossPercent' ? 100 : 1000000))
}
export function validServiceRequirements(value: unknown, allocation: Omit<ScenarioAllocation, 'total'>): value is ServiceRequirements {
  if (!object(value) || !exact(value, ['schemaVersion', 'services', 'sliceObjectives']) || value.schemaVersion !== 'cityverse.service-requirements/v2'
    || !object(value.services) || !Object.keys(value.services).length || !exact(value.services, groups)) return false
  const counts: Record<string, number> = { web_browsing: allocation.baselineUes, ...allocation.events }
  for (const [group, s] of Object.entries(value.services)) {
    if (!counts[group] || !object(s) || !exact(s, ['traffic', 'sla', 'evaluation']) || !object(s.traffic)
      || !exact(s.traffic, ['protocol', 'offeredUplinkMbpsPerUe', 'offeredDownlinkMbpsPerUe']) || s.traffic.protocol !== 'udp') return false
    for (const key of ['offeredUplinkMbpsPerUe', 'offeredDownlinkMbpsPerUe']) {
      const n = s.traffic[key]
      if (!finite(n) || n < .001 || n > 4 || Math.abs(n * 1000 - Math.round(n * 1000)) > 1e-8) return false
    }
    if (('sla' in s) !== ('evaluation' in s)) return false
    if ('sla' in s) {
      if (!validTargets(s.sla, ['per-ue', 'service-total']) || !validEvaluation(s.evaluation)) return false
      const factor = s.sla.scope === 'per-ue' ? 1 : counts[group]
      if ((s.sla.minUplinkMbps ?? 0) > s.traffic.offeredUplinkMbpsPerUe * factor
        || (s.sla.minDownlinkMbps ?? 0) > s.traffic.offeredDownlinkMbpsPerUe * factor) return false
    }
  }
  if (value.sliceObjectives !== undefined) {
    if (!object(value.sliceObjectives) || !exact(value.sliceObjectives, slices)) return false
    for (const item of Object.values(value.sliceObjectives)) {
      if (!object(item) || !exact(item, ['sla', 'evaluation']) || !validTargets(item.sla, ['slice-total']) || !validEvaluation(item.evaluation)) return false
    }
  }
  return true
}

export function validServiceSla(value: unknown): value is ServiceSlaAssessment {
  if (!object(value) || value.schemaVersion !== 'cityverse.service-sla/v1' || !/^[a-f0-9]{64}$/.test(value.revision)
    || value.historical !== true || value.scope !== 'latest-complete-evaluation-window' || !status(value.status)) return false
  const metric = (m: any) => object(m) && status(m.status) && finite(m.target) && ratio(m.coverageRatio) && ratio(m.lowerBound) && ratio(m.upperBound)
    && m.lowerBound <= m.upperBound && (m.observedMean == null || finite(m.observedMean))
  for (const w of [value.latestWindow, value.wholeRound]) {
    if (!object(w) || !status(w.status) || typeof w.reason !== 'string' || typeof w.complete !== 'boolean'
      || !Array.isArray(w.services) || w.services.length > SERVICE_PROFILE_IDS.length || !Array.isArray(w.slices) || w.slices.length > 4) return false
    const rows = [...w.services, ...w.slices]
    for (const row of rows) {
      if (!object(row) || !status(row.status) || !Number.isSafeInteger(row.expectedUes) || row.expectedUes < 1 || row.expectedUes > 100
        || !Number.isSafeInteger(row.observedUes) || row.observedUes < 0 || row.observedUes > 100
        || !object(row.metrics) || !exact(row.metrics, metricKeys) || !Object.values(row.metrics).every(metric)
        || !validTargets(row.sla, ['per-ue', 'service-total', 'slice-total']) || !validEvaluation(row.evaluation)) return false
      if (row.status === 'met' && row.complete !== true) return false
      if (row.perUe !== undefined && (!Array.isArray(row.perUe) || row.perUe.length > 100 || row.perUe.some((r: any) =>
        !object(r) || !/^[a-f0-9]{12}$/.test(r.ueRef) || !status(r.status) || !object(r.metrics) || !Object.values(r.metrics).every(metric)))) return false
    }
    if (w.status === 'met' && (!w.complete || !rows.length || rows.some((r) => r.status !== 'met'))) return false
    if (w.status === 'not_met' && !rows.some((r) => r.status === 'not_met')) return false
  }
  return value.status === value.latestWindow.status
}

export function validServiceWorkload(w: any, profiles: Record<string, readonly [string, number]>): boolean {
  const integer = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 1 && n <= 4000
  if (!w || w.schemaVersion !== 'cityverse.round-workload/v1' || w.trafficMode !== 'qos' || w.budgetKbpsPerDirection !== 400000
    || w.qosEffectClaimed !== false || w.capReason !== null || !/^[a-f0-9]{64}$/.test(w.comparisonFingerprint)
    || !/^[a-f0-9]{64}$/.test(w.serviceContractRevision) || !Array.isArray(w.perUe) || !w.perUe.length || w.perUe.length > 100
    || new Set(w.perUe.map((r: any) => r?.supi)).size !== w.perUe.length) return false
  for (const r of w.perUe) {
    if (!object(r) || !exact(r, ['supi', 'scenario', 'slice', 'requestedUplinkKbps', 'requestedDownlinkKbps', 'appliedUplinkKbps', 'appliedDownlinkKbps'])
      || !/^imsi-[0-9]{15}$/.test(r.supi) || !profiles[r.scenario] || r.slice !== profiles[r.scenario][0]
      || ![r.requestedUplinkKbps, r.requestedDownlinkKbps, r.appliedUplinkKbps, r.appliedDownlinkKbps].every(integer)
      || r.requestedUplinkKbps !== r.appliedUplinkKbps || r.requestedDownlinkKbps !== r.appliedDownlinkKbps) return false
  }
  const ul = w.perUe.reduce((n: number, r: any) => n + r.appliedUplinkKbps, 0)
  const dl = w.perUe.reduce((n: number, r: any) => n + r.appliedDownlinkKbps, 0)
  return w.requestedUplinkKbps === ul && w.appliedUplinkKbps === ul && w.requestedDownlinkKbps === dl && w.appliedDownlinkKbps === dl
    && w.requestedCombinedKbps === ul + dl && w.appliedCombinedKbps === ul + dl && Math.max(ul, dl) <= 400000
}

export async function readServiceCapabilities(signal?: AbortSignal): Promise<ServiceCapabilities> {
  const response = await fetch(`${apiBaseUrl()}/api/onprem/service-profiles`, { signal, headers: sessionHeaders() })
  if (!response.ok) throw new Error(`Service requirement capabilities unavailable (HTTP ${response.status})`)
  const value = await response.json()
  if (value.schemaVersion !== 'cityverse.service-requirements/v2' || !object(value.trafficDefaults) || !finite(value.maxOfferedKbpsPerUePerDirection)) throw new Error('Invalid service requirement capabilities')
  return value
}
export async function previewServiceRequest(request: ScenarioRequest, signal?: AbortSignal): Promise<ServicePreview> {
  const response = await fetch(`${apiBaseUrl()}/api/onprem/scenario-rounds/preview`, { method: 'POST', headers: { ...sessionHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify(profileRequest(request)), signal })
  if (!response.ok) throw new Error(`Service preview rejected (HTTP ${response.status})`)
  const value = await response.json()
  if (value.schemaVersion !== 'cityverse.service-preview/v1' || typeof value.admissible !== 'boolean' || value.networkCapacityVerified !== false || !Array.isArray(value.warnings)) throw new Error('Invalid service preview')
  return value
}
