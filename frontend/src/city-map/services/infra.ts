import config from '../data/infra-config.json'
import { Application, DataCenter, Gnb, NetworkFunction } from '../domain/model'
import type { CityMap, CityObject } from '../domain/model'

export const infraConfig = config
export type QuantityPair = { cpu: string | null; memory: string | null }
export interface InfraContainer { name: string; image: string | null; requests: QuantityPair; limits: QuantityPair; ready: boolean; restarts: number }
export interface InfraPod {
  name: string; uid: string; namespace: string; siteId: string; siteBinding: 'pod-metadata' | 'configured-workload'
  workload: string; function: string; category: string; phase: string; ready: boolean
  node: string | null; podIp: string | null; containers: InfraContainer[]
}
export interface InfraSnapshot { schemaVersion: 'cityverse.city-infra/v1'; source: 'kubernetes-pod-spec'; observedAt: string; namespace: string; pods: InfraPod[]; usageAvailable: false }
export interface InfraSource { read(signal: AbortSignal): Promise<InfraSnapshot> }

export function infraTarget(map: CityMap, object: CityObject | null): DataCenter | Gnb | null {
  if (object instanceof DataCenter || object instanceof Gnb) return object
  if (object instanceof NetworkFunction || object instanceof Application) {
    const host = map.get(object.siteId)
    return host instanceof DataCenter ? host : null
  }
  return null
}

export function parseInfraSnapshot(raw: unknown): InfraSnapshot {
  const data = raw as InfraSnapshot
  const text = (v: unknown) => v === null || typeof v === 'string'
  const quantities = (v: QuantityPair) => v && text(v.cpu) && text(v.memory)
  if (!data || data.schemaVersion !== 'cityverse.city-infra/v1' || data.source !== 'kubernetes-pod-spec'
    || typeof data.observedAt !== 'string' || !Number.isFinite(Date.parse(data.observedAt)) || typeof data.namespace !== 'string' || data.usageAvailable !== false
    || !Array.isArray(data.pods) || data.pods.length > 4096
    || !data.pods.every(p => p && ['name', 'uid', 'namespace', 'siteId', 'function', 'phase', 'workload'].every(k => typeof p[k as keyof InfraPod] === 'string')
      && p.namespace === data.namespace && ['pod-metadata', 'configured-workload'].includes(p.siteBinding)
      && typeof p.ready === 'boolean' && text(p.node) && text(p.podIp) && Array.isArray(p.containers)
      && p.containers.every(c => c && typeof c.name === 'string' && text(c.image) && quantities(c.requests) && quantities(c.limits)
        && typeof c.ready === 'boolean' && Number.isInteger(c.restarts) && c.restarts >= 0))) {
    throw new Error('站點資料格式不正確')
  }
  return data
}

/** Fetch on selection / refresh only. An unavailable source never becomes zero Pods. */
export class HttpInfraSource implements InfraSource {
  constructor(private url: string, private headers: () => Record<string, string> = () => ({})) {}
  async read(signal: AbortSignal) {
    const response = await fetch(this.url, { signal, headers: this.headers(), cache: 'no-store' })
    if (!response.ok) throw new Error(`無法讀取即時 Pod 資訊（HTTP ${response.status}）`)
    const body = await response.text()
    if (body.length > 4 * 1024 * 1024) throw new Error('Pod 資訊超過顯示上限')
    return parseInfraSnapshot(JSON.parse(body))
  }
}
