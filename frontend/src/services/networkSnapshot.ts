import type { FlowEdgeState, NetworkMetrics, NetworkSnapshot, SliceStatus } from '../types'

export function normalizeNetworkSnapshot(
  value: unknown,
  fallbackMetrics: NetworkMetrics,
  fallbackSlices: SliceStatus[]
): NetworkSnapshot {
  const raw = isRecord(value) ? value : {}
  const metrics = isRecord(raw.metrics) ? raw.metrics as unknown as NetworkMetrics : fallbackMetrics
  const slices = Array.isArray(raw.slices) ? raw.slices as SliceStatus[] : fallbackSlices
  const timestamp = finiteNumber(raw.timestamp, Date.now())
  const edges = Array.isArray(raw.edges)
    ? raw.edges.map((edge, index) => normalizeEdge(edge, index)).filter((edge): edge is FlowEdgeState => Boolean(edge))
    : []

  return {
    id: String(raw.id || `snapshot-${timestamp}`),
    timestamp,
    source: String(raw.source || metrics.dataSource || 'unknown'),
    metrics,
    slices,
    edges,
  }
}

export function snapshotFromMetrics(metrics: NetworkMetrics, slices: SliceStatus[]): NetworkSnapshot {
  const throughput = finiteNumber(metrics.throughputMbps)
  const uplink = finiteNumber(metrics.uplinkMbps, throughput / 2)
  const downlink = finiteNumber(metrics.downlinkMbps, Math.max(throughput - uplink, 0))
  const latency = Math.max(1, finiteNumber(metrics.latencyMs, 1))
  const congestion = finiteNumber(metrics.upfCpuPercent)
  const active = throughput > 0 || finiteNumber(metrics.gtpPacketsPerSec) > 0

  const edgeBase = {
    active,
    throughputMbps: throughput,
    uplinkMbps: uplink,
    downlinkMbps: downlink,
    latencyMs: latency,
    packetLossPercent: 0,
    upfCongestionPercent: congestion,
    fiveQi: dominantFiveQi(slices),
  }

  return {
    id: `metrics-${finiteNumber(metrics.timestamp, Date.now())}`,
    timestamp: finiteNumber(metrics.timestamp, Date.now()),
    source: metrics.dataSource || 'unknown',
    metrics,
    slices,
    edges: active ? [
      { id: 'live-residential-gnb1', sourceNodeId: 'residential', targetNodeId: 'gnb1', sliceType: dominantSlice(slices), ...edgeBase },
      { id: 'live-gnb1-upf', sourceNodeId: 'gnb1', targetNodeId: 'upf', sliceType: dominantSlice(slices), ...edgeBase },
      { id: 'live-upf-dn', sourceNodeId: 'upf', targetNodeId: 'dn', sliceType: dominantSlice(slices), ...edgeBase },
    ] : [],
  }
}

export function interpolateNetworkSnapshots(
  from: NetworkSnapshot | null,
  to: NetworkSnapshot,
  progress: number
): NetworkSnapshot {
  if (!from) return to
  const t = Math.min(Math.max(progress, 0), 1)
  const fromEdges = new Map(from.edges.map((edge) => [edge.id, edge]))
  const toEdges = new Map(to.edges.map((edge) => [edge.id, edge]))
  const ids = Array.from(new Set([...fromEdges.keys(), ...toEdges.keys()]))

  return {
    ...to,
    timestamp: lerp(from.timestamp, to.timestamp, t),
    metrics: interpolateMetrics(from.metrics, to.metrics, t),
    edges: ids.map((id) => interpolateEdge(id, fromEdges.get(id), toEdges.get(id), t)),
  }
}

function interpolateMetrics(from: NetworkMetrics, to: NetworkMetrics, t: number): NetworkMetrics {
  return {
    ...to,
    upfCpuPercent: lerp(finiteNumber(from.upfCpuPercent), finiteNumber(to.upfCpuPercent), t),
    gtpPacketsPerSec: lerp(finiteNumber(from.gtpPacketsPerSec), finiteNumber(to.gtpPacketsPerSec), t),
    pduSessionCount: Math.round(lerp(finiteNumber(from.pduSessionCount), finiteNumber(to.pduSessionCount), t)),
    latencyMs: lerp(finiteNumber(from.latencyMs), finiteNumber(to.latencyMs), t),
    throughputMbps: lerp(finiteNumber(from.throughputMbps), finiteNumber(to.throughputMbps), t),
    uplinkMbps: lerp(finiteNumber(from.uplinkMbps), finiteNumber(to.uplinkMbps), t),
    downlinkMbps: lerp(finiteNumber(from.downlinkMbps), finiteNumber(to.downlinkMbps), t),
  }
}

function interpolateEdge(id: string, from: FlowEdgeState | undefined, to: FlowEdgeState | undefined, t: number): FlowEdgeState {
  const start = from ?? to
  const end = to ?? from
  if (!start || !end) throw new Error(`Missing edge ${id}`)
  return {
    ...end,
    id,
    active: end.active || start.active,
    plane: end.plane ?? start.plane,
    protocol: end.protocol ?? start.protocol,
    scenario: end.scenario ?? start.scenario,
    throughputMbps: lerp(from ? finiteNumber(from.throughputMbps) : 0, to ? finiteNumber(to.throughputMbps) : 0, t),
    uplinkMbps: lerp(from ? finiteNumber(from.uplinkMbps) : 0, to ? finiteNumber(to.uplinkMbps) : 0, t),
    downlinkMbps: lerp(from ? finiteNumber(from.downlinkMbps) : 0, to ? finiteNumber(to.downlinkMbps) : 0, t),
    latencyMs: lerp(from ? finiteNumber(from.latencyMs, 1) : finiteNumber(end.latencyMs, 1), to ? finiteNumber(to.latencyMs, 1) : finiteNumber(start.latencyMs, 1), t),
    packetLossPercent: lerp(from ? finiteNumber(from.packetLossPercent) : 0, to ? finiteNumber(to.packetLossPercent) : 0, t),
    upfCongestionPercent: lerp(from ? finiteNumber(from.upfCongestionPercent) : 0, to ? finiteNumber(to.upfCongestionPercent) : 0, t),
    evidenceCount: end.evidenceCount ?? start.evidenceCount,
    lastObservedAt: end.lastObservedAt ?? start.lastObservedAt,
    lastObservedEpochMillis: end.lastObservedEpochMillis ?? start.lastObservedEpochMillis,
  }
}

function normalizeEdge(value: unknown, index: number): FlowEdgeState | null {
  if (!isRecord(value)) return null
  const sourceNodeId = String(value.sourceNodeId || value.source || '')
  const targetNodeId = String(value.targetNodeId || value.target || '')
  if (!sourceNodeId || !targetNodeId) return null
  const throughputMbps = finiteNumber(value.throughputMbps ?? value.bandwidthMbps)
  const uplinkMbps = finiteNumber(value.uplinkMbps, throughputMbps / 2)
  const downlinkMbps = finiteNumber(value.downlinkMbps, Math.max(throughputMbps - uplinkMbps, 0))
  return {
    id: String(value.id || `${sourceNodeId}-${targetNodeId}-${index}`),
    sourceNodeId,
    targetNodeId,
    sliceType: String(value.sliceType || 'eMBB') as FlowEdgeState['sliceType'],
    plane: value.plane === 'control' ? 'control' : 'user',
    protocol: value.protocol ? String(value.protocol) : undefined,
    scenario: value.scenario ? String(value.scenario) : undefined,
    active: value.active === true || throughputMbps > 0,
    throughputMbps,
    uplinkMbps,
    downlinkMbps,
    latencyMs: finiteNumber(value.latencyMs, 1),
    packetLossPercent: finiteNumber(value.packetLossPercent),
    upfCongestionPercent: finiteNumber(value.upfCongestionPercent),
    fiveQi: finiteNumber(value.fiveQi) || undefined,
    evidenceCount: finiteNumber(value.evidenceCount) || undefined,
    lastObservedAt: value.lastObservedAt ? String(value.lastObservedAt) : undefined,
    lastObservedEpochMillis: finiteNumber(value.lastObservedEpochMillis) || undefined,
    qosFlows: Array.isArray(value.qosFlows) ? value.qosFlows as FlowEdgeState['qosFlows'] : undefined,
  }
}

function dominantSlice(slices: SliceStatus[]): FlowEdgeState['sliceType'] {
  return [...slices].sort((a, b) => (b.throughputMbps ?? b.load ?? 0) - (a.throughputMbps ?? a.load ?? 0))[0]?.type ?? 'eMBB'
}

function dominantFiveQi(slices: SliceStatus[]): number {
  const qiBySlice: Record<FlowEdgeState['sliceType'], number> = { eMBB: 9, URLLC: 1, mMTC: 79, V2X: 79 }
  return qiBySlice[dominantSlice(slices)]
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t
}

function finiteNumber(value: unknown, fallback = 0): number {
  const numberValue = Number(value)
  return Number.isFinite(numberValue) ? numberValue : fallback
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object')
}
