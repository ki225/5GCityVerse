export type Health = 'healthy' | 'degraded' | 'error' | 'unknown' | 'reserved' | 'inactive'
export type EvidenceState = 'verified' | 'configured' | 'unknown'
export interface WorkloadHealth {
  id: string; function: string; required: boolean; health: Health; reason: string
  desiredReplicas: number | null; readyReplicas: number | null
}
export interface SiteHealth {
  id: string; kind: string; health: Health; reason: string; source: string
  desiredReplicas: number | null; readyReplicas: number | null; workloads: WorkloadHealth[]
  observedAt: string | null; validUntil: string | null
}
export interface PathSegment {
  fromId: string | null; toId: string | null; interface: 'access' | 'N3' | 'N6'; state: EvidenceState
  observedAt: string | null; validUntil: string | null
}
export interface ServicePath {
  id: string; demandGroupId: string; profileId: string; slice: string; state: string; reason: string
  sourceLocation: { buildingId?: string; position: { x: number; y: number } }
  deviceCount: number | null; sessionCount: number | null; requestedUes: number | null
  segments: PathSegment[]; application: { id: string; siteId: string } | null
  traffic: null | { ulMbps: number | null; dlMbps: number | null; source: string; observedAt: string; validUntil: string; windowStart: string; windowEnd: string }
}
export interface CityObservation {
  schemaVersion: 'cityverse.city-observability/v1'; topologyRevision: number | null; topologyDigest: string
  scope: null | { roundId: string; generation: number; populationRevision: number; sequence: number; topologyRevision: number }
  infra: SiteHealth[]; paths: ServicePath[]; pathStatus: string; usageAvailable: false
}
const healthStates = ['healthy', 'degraded', 'error', 'unknown', 'reserved', 'inactive']
const timestamp = (v: unknown) => v === null || (typeof v === 'string' && Number.isFinite(Date.parse(v)))
const count = (v: unknown) => v === null || (Number.isSafeInteger(v) && Number(v) >= 0)
const finite = (v: unknown) => v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0)

/** Reject malformed display data before it reaches projection or animation code. */
export function parseCityObservation(raw: unknown): CityObservation {
  const v = raw as CityObservation
  if (!v || v.schemaVersion !== 'cityverse.city-observability/v1' || typeof v.topologyDigest !== 'string'
    || !count(v.topologyRevision) || typeof v.pathStatus !== 'string' || v.usageAvailable !== false
    || (v.scope !== null && (!v.scope || typeof v.scope.roundId !== 'string' || !count(v.scope.generation) || !count(v.scope.populationRevision) || !count(v.scope.sequence)))
    || !Array.isArray(v.infra) || v.infra.length > 128 || !Array.isArray(v.paths) || v.paths.length > 200
    || new Set(v.infra.map(s => s.id)).size !== v.infra.length || new Set(v.paths.map(p => p.id)).size !== v.paths.length
    || !v.infra.every(s => s && typeof s.id === 'string' && healthStates.includes(s.health) && typeof s.reason === 'string'
      && timestamp(s.observedAt) && timestamp(s.validUntil) && count(s.desiredReplicas) && count(s.readyReplicas)
      && Array.isArray(s.workloads) && s.workloads.length <= 256 && s.workloads.every(w => w && typeof w.id === 'string'
        && typeof w.function === 'string' && healthStates.includes(w.health) && typeof w.reason === 'string' && count(w.desiredReplicas) && count(w.readyReplicas)))
    || !v.paths.every(p => p && typeof p.id === 'string' && typeof p.profileId === 'string' && typeof p.demandGroupId === 'string'
      && count(p.deviceCount) && count(p.sessionCount) && count(p.requestedUes) && p.sourceLocation?.position
      && ['x', 'y'].every(k => { const n = p.sourceLocation.position[k as 'x' | 'y']; return typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 24 })
      && Array.isArray(p.segments) && p.segments.length <= 3 && p.segments.every(s => s && ['access', 'N3', 'N6'].includes(s.interface)
        && ['verified', 'configured', 'unknown'].includes(s.state) && timestamp(s.observedAt) && timestamp(s.validUntil)
        && (s.fromId === null || typeof s.fromId === 'string') && (s.toId === null || typeof s.toId === 'string'))
      && (p.application === null || (typeof p.application?.id === 'string' && typeof p.application?.siteId === 'string'))
      && (p.traffic === null || (p.traffic?.source === 'native-iperf3-receiver' && finite(p.traffic.ulMbps) && finite(p.traffic.dlMbps)
        && [p.traffic.observedAt, p.traffic.validUntil, p.traffic.windowStart, p.traffic.windowEnd].every(t => typeof t === 'string' && timestamp(t)))))) {
    throw new Error('城市監看資料格式不正確')
  }
  return v
}
export const freshUntil = (until: string | null, now: number) => until !== null && Date.parse(until) > now
export function siteState(site: SiteHealth | undefined, now: number, unavailable = false): Health {
  return !site || unavailable || !freshUntil(site.validUntil, now) ? 'unknown' : site.health
}
export function segmentState(segment: PathSegment, now: number, unavailable = false): EvidenceState {
  return segment.state === 'configured' ? 'configured' : unavailable || !freshUntil(segment.validUntil, now) ? 'unknown' : segment.state
}
export const healthLabels: Record<Health, string> = { healthy: '正常', degraded: '降級', error: '不可用', unknown: '未知', reserved: '預留', inactive: '未啟用' }
export const healthSymbols: Record<Health, string> = { healthy: '✓', degraded: '!', error: '×', unknown: '?', reserved: '–', inactive: '○' }
export const reasonLabels: Record<string, string> = {
  replicas_not_ready: '部分副本未就緒', rollout_pending: '更新／啟動中', required_replicas_ready: '必要副本已就緒',
  required_replicas_unavailable: '必要副本尚未就緒', container_restarted: '容器曾異常重啟', configured_workload_missing: '缺少配置的工作負載',
  observation_unavailable: '無法取得觀測資料', not_deployed: '尚未部署', scaled_to_zero: '已縮減為零副本', unsupported_selector: '無法確認 Pod 關聯',
}
