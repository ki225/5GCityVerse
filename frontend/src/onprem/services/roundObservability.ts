import { SCENARIO_EVENTS, scenarioRead, freshTimestamp, type ScenarioGroup } from './onpremScenarios'
import { parseActionSummary, type ActionSummary } from './actionEvidence'

export type QosSample = {
  id: string; sequence: number; roundId: string; generation: number; scenario: ScenarioGroup; slice: string; stage: string | null
  startedAt: string | null; finishedAt: string | null; observedAt: string | null
  receivedUplinkMbps: number | null; receivedDownlinkMbps: number | null; rttMs: number | null; rttP95Ms: number | null
  lossPercent: number | null; packets: number | null; lostPackets: number | null; sampleCount: number
  source: string; quality: 'observed' | 'missing'; comparisonFingerprint: string | null; policyRevision: number | null
  targets?: QosTargets | null; assessment?: QosAssessment
  rtt?: { status: 'observed' | 'missing' | 'timeout' | 'unsupported'; source: string; statistic: 'mean'; sampleCount: number
    transmitted: number; received: number; startedAt: string | null; finishedAt: string | null; reason: string | null
    validUeCount?: number; expectedUeCount?: number; relation?: 'adjacent-probe-window'; sameTrafficWindow?: boolean } | null
}
export type QosMetric = 'receivedUplinkMbps' | 'receivedDownlinkMbps' | 'rttMs' | 'lossPercent'
export type AgentRole = 'planner' | 'validator' | 'executor'
export type TargetRef = { mapNodeId: 'gnb' | 'amf' | 'smf' | 'pcf' | 'nssf' | 'udm-ausf' | 'nrf' | 'upf' | 'dn'
  scenario: string | null; slice: string | null; workloadRef: string | null; podUid: string | null; shared: boolean }
export function validTargetRefs(value: unknown): value is TargetRef[] {
  return Array.isArray(value) && value.length <= 100 && value.every((v) => v &&
    ['gnb', 'amf', 'smf', 'pcf', 'nssf', 'udm-ausf', 'nrf', 'upf', 'dn'].includes(v.mapNodeId) && typeof v.shared === 'boolean'
    && [v.scenario, v.slice, v.workloadRef, v.podUid].every((s) => s === null || (typeof s === 'string' && s.length <= 512)))
}
export type MetricVerdict = 'met' | 'not_met' | 'unknown'
export type QosTargets = { revision: string; source: string; scenario: string; slice: string; scope: 'scenario-total'
  throughputStatistic: 'sum'; rttStatistic: 'mean'; lossStatistic: 'packet-ratio'
  minUplinkMbps: number | null; minDownlinkMbps: number | null; maxRttMs: number | null; maxLossPercent: number | null }
export type QosAssessment = { status: MetricVerdict; reason: string; metrics: Record<QosMetric, MetricVerdict> }
export type AgentEvent = {
  sequence: number; callId: string | null; cycle: number | null; role: string | null; phase: string | null
  tool: string | null; status?: string | null; at: string | number | null; startedAt: string | number | null
  endedAt: string | number | null; durationMs?: number | null; inputSummary: unknown; outputSummary: unknown
  actionId?: string | null; relation?: string; evidenceRef?: string | null; targetRefs?: TargetRef[]
}
export type RoundDecision = {
  id: string; cycle: number; action: string | null; reason: string | null; outcome: string | null; improvementPercent: number | null
  beforeRef: string | null; afterRef: string | null; actionRef: string | null
  at?: string | null; beforeSampleIds?: string[]; afterSampleIds?: string[]; scenario?: string | null
}
export type RoundChange = {
  id: string; kind: string; requested: boolean | null; accepted: boolean | null; installed: boolean | null; effectVerified: boolean | null
  profileId: string | null; policyRevision: number | null; podChange: 'unknown'; scenario?: string | null
  before?: QerEvidenceSummary | null; after?: QerEvidenceSummary | null
  decisionId?: string | null; cycle?: number | null; at?: string | null
  beforeSampleIds?: string[]; afterSampleIds?: string[]
  state?: 'installed' | 'restored' | 'unknown'; restorationVerified?: boolean | null; restoredAt?: string | null
}
export type QerEvidenceSummary = {
  profileId: string | null; uplinkKbps: { min: number; max: number } | null; downlinkKbps: { min: number; max: number } | null
  ueCount: number | null; source: 'native-qer-before' | 'installed-policy-receipt'; state: 'observed' | 'installed'
}
export type RoundObservability = {
  schemaVersion: 'cityverse.round-observability/v1'; roundId: string; generation: number; observedAt: string | null
  agent: { role?: string | null; phase?: string | null; tool?: string | null; status?: string | null; cycle?: number | null
    callId?: string | null; startedAt?: string | number | null; updatedAt?: string | number | null; targetRefs?: TargetRef[] } | null
  decisions: RoundDecision[]; changes: RoundChange[]; latestQos: QosSample[]
  actions?: ActionSummary[]
  nodeStates?: unknown[]
  capabilities: { podScaling: boolean; qerPolicy: boolean }
}
export type CursorPage<T> = { nextCursor: number; hasMore: boolean; gap: boolean } & T
export type RoundObservationView = {
  schemaVersion: 'cityverse.round-observation-view/v1'; roundId: string; generation: number; sequence: number
  observability: RoundObservability; measurements: CursorPage<{ samples: QosSample[] }>
}

const integer = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0
const numberOrNull = (v: unknown) => v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0)
const timestamp = (v: unknown) => v === null || (typeof v === 'string' && Number.isFinite(Date.parse(v)))
const validSampleRefs = (refs: unknown) => refs === undefined || (Array.isArray(refs) && refs.length <= 6 && refs.every((ref) => typeof ref === 'string' && ref.length <= 512))
const qosMetrics: QosMetric[] = ['receivedUplinkMbps', 'receivedDownlinkMbps', 'rttMs', 'lossPercent']
const verdicts = ['met', 'not_met', 'unknown']
function validQosTargets(t: QosTargets | null | undefined, sample: QosSample): boolean {
  return t == null || Boolean(t && typeof t.revision === 'string' && typeof t.source === 'string'
    && t.scenario === sample.scenario && t.slice === sample.slice && t.scope === 'scenario-total'
    && t.throughputStatistic === 'sum' && t.rttStatistic === 'mean' && t.lossStatistic === 'packet-ratio'
    && [t.minUplinkMbps, t.minDownlinkMbps, t.maxRttMs, t.maxLossPercent].every(numberOrNull)
    && (t.maxLossPercent === null || t.maxLossPercent <= 100))
}

function validQerSummary(summary: QerEvidenceSummary | null | undefined): boolean {
  return summary == null || Boolean(summary && (summary.profileId === null || (typeof summary.profileId === 'string' && summary.profileId.length <= 160))
    && [summary.uplinkKbps, summary.downlinkKbps].every((range) => range === null || (range && numberOrNull(range.min) && numberOrNull(range.max)
      && range.min !== null && range.max !== null && range.min <= range.max && range.max <= 1e12))
    && (summary.ueCount === null || (integer(summary.ueCount) && summary.ueCount <= 500))
    && ['native-qer-before', 'installed-policy-receipt'].includes(summary.source) && ['observed', 'installed'].includes(summary.state))
}

export function parseQosSample(value: unknown, roundId: string, generation: number): QosSample {
  const s = value as QosSample
  if (!s || typeof s.id !== 'string' || !s.id || !integer(s.sequence) || s.roundId !== roundId || s.generation !== generation
    || !(['web_browsing', ...SCENARIO_EVENTS] as string[]).includes(s.scenario) || typeof s.slice !== 'string'
    || (s.stage !== null && typeof s.stage !== 'string') || typeof s.source !== 'string' || !['observed', 'missing'].includes(s.quality)
    || ![s.startedAt, s.finishedAt, s.observedAt].every(timestamp)
    || ![s.receivedUplinkMbps, s.receivedDownlinkMbps, s.rttMs, s.rttP95Ms, s.lossPercent, s.packets, s.lostPackets].every(numberOrNull)
    || !integer(s.sampleCount) || (s.policyRevision !== null && !integer(s.policyRevision))
    || (s.lossPercent !== null && s.lossPercent > 100)
    || (s.rttP95Ms !== null && s.sampleCount < 20)
    || (s.comparisonFingerprint !== null && typeof s.comparisonFingerprint !== 'string')
    || !validQosTargets(s.targets, s)
    || (s.assessment !== undefined && (!s.assessment || !verdicts.includes(s.assessment.status)
      || typeof s.assessment.reason !== 'string' || !s.assessment.metrics || qosMetrics.some((k) => !verdicts.includes(s.assessment!.metrics[k]))))
    || (s.rtt != null && (!['observed', 'missing', 'timeout', 'unsupported'].includes(s.rtt.status) || s.rtt.statistic !== 'mean'
      || typeof s.rtt.source !== 'string' || ![s.rtt.sampleCount, s.rtt.transmitted, s.rtt.received].every(integer)
      || s.rtt.received > s.rtt.transmitted || s.rtt.sampleCount > s.rtt.received || ![s.rtt.startedAt, s.rtt.finishedAt].every(timestamp)
      || [s.rtt.validUeCount, s.rtt.expectedUeCount].some((n) => n !== undefined && (!integer(n) || n > 500))
      || (s.rtt.sameTrafficWindow !== undefined && typeof s.rtt.sameTrafficWindow !== 'boolean')
      || (s.rtt.relation !== undefined && s.rtt.relation !== 'adjacent-probe-window')))) throw new Error('Invalid QoS observation')
  return s
}

export function parseObservation(value: unknown, roundId: string, generation: number): RoundObservability {
  const v = value as RoundObservability
  if (!v || v.schemaVersion !== 'cityverse.round-observability/v1' || v.roundId !== roundId || v.generation !== generation
    || !Array.isArray(v.latestQos) || !Array.isArray(v.decisions) || !Array.isArray(v.changes)
    || !v.capabilities || typeof v.capabilities.podScaling !== 'boolean' || typeof v.capabilities.qerPolicy !== 'boolean'
    || !timestamp(v.observedAt) || v.decisions.length > 1000 || v.changes.length > 2000
    || (v.actions !== undefined && (!Array.isArray(v.actions) || v.actions.length > 400))
    || (v.agent !== null && (!v.agent || (v.agent.phase != null && typeof v.agent.phase !== 'string') || (v.agent.status != null && typeof v.agent.status !== 'string')))
    || (v.agent?.targetRefs !== undefined && !validTargetRefs(v.agent.targetRefs))
    || (v.nodeStates !== undefined && (!Array.isArray(v.nodeStates) || v.nodeStates.length > 256))
    || v.decisions.some((d) => !d || typeof d.id !== 'string' || (d.reason !== null && typeof d.reason !== 'string') || !integer(d.cycle) || (d.action !== null && typeof d.action !== 'string')
      || !validSampleRefs(d.beforeSampleIds) || !validSampleRefs(d.afterSampleIds))
    || v.changes.some((c) => !c || typeof c.id !== 'string' || typeof c.kind !== 'string'
      || ![c.requested, c.accepted, c.installed, c.effectVerified].every((n) => n === null || typeof n === 'boolean')
      || !validQerSummary(c.before) || !validQerSummary(c.after) || !validSampleRefs(c.beforeSampleIds) || !validSampleRefs(c.afterSampleIds)
      || (c.after != null && (c.installed !== true || c.after.state !== 'installed' || c.after.source !== 'installed-policy-receipt'))
      || (c.state !== undefined && !['installed', 'restored', 'unknown'].includes(c.state))
      || (c.restorationVerified != null && typeof c.restorationVerified !== 'boolean')
      || (c.restoredAt !== undefined && !timestamp(c.restoredAt)))) throw new Error('Invalid round observability')
  v.latestQos.forEach((s) => parseQosSample(s, roundId, generation))
  v.actions?.forEach(parseActionSummary)
  return v
}

function parsePage<T>(value: CursorPage<T>, cursor: number): CursorPage<T> {
  if (!value || !integer(value.nextCursor) || value.nextCursor < cursor || typeof value.hasMore !== 'boolean'
    || typeof value.gap !== 'boolean' || (value.hasMore && value.nextCursor <= cursor)) throw new Error('Invalid observation cursor')
  return value
}
export async function readObservability(roundId: string, generation: number, signal: AbortSignal) {
  return parseObservation(await scenarioRead(`/${encodeURIComponent(roundId)}/observability`, signal), roundId, generation)
}
export async function readAgentEvents(roundId: string, after: number, signal: AbortSignal, filter: { cycle?: number | null; actionId?: string | null; callId?: string | null; role?: AgentRole | null } = {}) {
  const params = new URLSearchParams({ after: String(after), limit: '100' })
  if (filter.cycle != null) params.set('cycle', String(filter.cycle))
  if (filter.actionId) params.set('actionId', filter.actionId)
  if (filter.callId) params.set('callId', filter.callId)
  if (filter.role) params.set('role', filter.role)
  const page = parsePage(await scenarioRead<CursorPage<{ events: AgentEvent[] }>>(`/${encodeURIComponent(roundId)}/events?${params}`, signal), after)
  if (!Array.isArray(page.events) || page.events.length > 100 || page.events.some((e) => !e || !integer(e.sequence)
    || e.sequence <= after || e.sequence > page.nextCursor || (e.status != null && typeof e.status !== 'string') || (e.durationMs !== undefined && !numberOrNull(e.durationMs))
    || (e.targetRefs !== undefined && !validTargetRefs(e.targetRefs)))) throw new Error('Invalid agent events')
  if (page.events.some((e) => (filter.cycle != null && e.cycle !== filter.cycle) || (filter.callId && e.callId !== filter.callId)
    || (filter.role && e.role !== filter.role))) throw new Error('Foreign action log')
  return page
}
export async function readQosMeasurements(roundId: string, generation: number, after: number, signal: AbortSignal) {
  return parseMeasurements(await scenarioRead<CursorPage<{ samples: QosSample[] }>>(`/${encodeURIComponent(roundId)}/measurements?after=${after}&limit=100`, signal), roundId, generation, after)
}
function parseMeasurements(value: CursorPage<{ samples: QosSample[] }>, roundId: string, generation: number, after: number) {
  const page = parsePage(value, after)
  if (!Array.isArray(page.samples) || page.samples.length > 100) throw new Error('Invalid measurement page')
  if (page.samples.some((s) => !s || s.sequence <= after || s.sequence > page.nextCursor)) throw new Error('Invalid measurement cursor')
  page.samples.forEach((s) => parseQosSample(s, roundId, generation))
  return page
}

export async function readObservationView(roundId: string, generation: number, after: number, signal: AbortSignal): Promise<RoundObservationView> {
  const value = await scenarioRead<RoundObservationView>(`/${encodeURIComponent(roundId)}/observation-view?after=${after}&limit=100&view=current`, signal)
  // This sequence is the persisted source, not a global clock for live Agent
  // or topology overlays. Their original timestamps remain in the subviews.
  if (!value || value.schemaVersion !== 'cityverse.round-observation-view/v1' || value.roundId !== roundId
    || value.generation !== generation || !integer(value.sequence)) throw new Error('Invalid observation view')
  parseObservation(value.observability, roundId, generation)
  parseMeasurements(value.measurements, roundId, generation, after)
  return value
}

export function mergeObservationRows<T extends { sequence: number }>(current: T[], incoming: T[], limit: number): T[] {
  return [...new Map([...current, ...incoming].map((row) => [row.sequence, row])).values()]
    .sort((a, b) => a.sequence - b.sequence).slice(-limit)
}
export const qosSampleFresh = (sample: QosSample | null, now: number) => Boolean(sample?.quality === 'observed' && freshTimestamp(sample.observedAt, now))
export const comparableQos = (a: QosSample | null, b: QosSample | null) => Boolean(a && b && a.roundId === b.roundId
  && a.generation === b.generation && a.scenario === b.scenario && a.slice === b.slice && a.quality === 'observed' && b.quality === 'observed'
  && a.comparisonFingerprint && a.comparisonFingerprint === b.comparisonFingerprint)

export function qosMetricTarget(sample: QosSample | null, key: QosMetric): number | null {
  if (!sample?.targets || !validQosTargets(sample.targets, sample)) return null
  return sample.targets[{ receivedUplinkMbps: 'minUplinkMbps', receivedDownlinkMbps: 'minDownlinkMbps', rttMs: 'maxRttMs', lossPercent: 'maxLossPercent' }[key] as 'minUplinkMbps']
}
export function qosMetricVerdict(sample: QosSample | null, key: QosMetric, now: number, historical = false): MetricVerdict {
  const target = qosMetricTarget(sample, key), measured = sample?.[key]
  if (!sample || sample.quality !== 'observed' || (!historical && !qosSampleFresh(sample, now)) || target === null || measured == null
    || (key === 'lossPercent' && (!sample.packets || sample.lostPackets === null))
    || (key === 'rttMs' && (sample.rtt?.status !== 'observed' || sample.rtt.statistic !== 'mean' || !sample.rtt.sampleCount
      || sample.rtt.sameTrafficWindow === false || (!historical && !freshTimestamp(sample.rtt.finishedAt, now))
      || (sample.rtt.expectedUeCount !== undefined && sample.rtt.validUeCount !== sample.rtt.expectedUeCount)))) return 'unknown'
  const recorded = sample.assessment?.metrics[key] ?? 'unknown'
  const meets = key === 'receivedUplinkMbps' || key === 'receivedDownlinkMbps' ? measured >= target : measured <= target
  return (recorded === 'met' && meets) || (recorded === 'not_met' && !meets) ? recorded : 'unknown'
}

export type ObservationState = { data: RoundObservability | null; events: AgentEvent[]; samples: QosSample[]; error: string; gap: boolean; truncated: boolean }
