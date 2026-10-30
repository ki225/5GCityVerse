import { scenarioRead, type ScenarioGroup } from './onpremScenarios'
import { validTargetRefs, type AgentEvent, type AgentRole, type RoundObservability, type TargetRef } from './roundObservability'

export type ActionSummary = { id: string; decisionId: string | null; cycle: number | null; actionId: string | null; callId: string | null
  at: string | null; type: 'decision' | 'tool'; action: string | null; target: string | null; tool?: string | null
  scenario: string | null; slice?: string | null; evidenceRef?: string | null; role?: AgentRole | null; targetRefs?: TargetRef[]; relation: 'action' | 'cycle' | 'call' }
type RateRange = { min: number; max: number } | null
type Readback = { ueCount: number; uplinkKbps: RateRange; downlinkKbps: RateRange } | null
export type ActionResource = { callId?: string | null; role?: AgentRole | null; tool?: string | null; source?: string; phase?: string; status: string
  observedAt: string | null; collectedAt?: string | null; relation?: string; bindings?: Array<{ slice: string; podUid: string | null; workloadRef: string | null; status: string; sharing: string }>
  workloads: Array<{ workloadRef: string | null; podUid: string | null; nfType: string | null; sharing: string; ready: boolean | null
    readyPods: number | null; desiredPods: number | null; ownerReadyPods?: number | null; replicaScope?: 'controller' | 'unknown'; restarts: number | null; phase: string | null }>
  metrics: Array<{ workloadRef: string | null; podUid: string | null; nfType: string | null; sharing: string; observedAt: string | null
    identityStatus: string; status: string; containers: Array<{ name: string | null; cpuCores: number | null; memoryBytes: number | null }> }> }
export type ActionEvidence = { schemaVersion: 'cityverse.action-evidence/v1'; roundId: string; generation: number; cycle: number | null
  actionId: string | null; callId?: string | null; qer: { status: string; consistency: string; reason?: string; observedAt?: string | null
    capturedAt?: string | null; receiptId?: string | null; requested?: { profileId: string; uplinkKbps: number; downlinkKbps: number } | null
    before?: Readback; readback?: Readback }
  resources: ActionResource[]; diagnostics: Array<{ callId?: string | null; role?: AgentRole | null; tool?: string; source?: string; observedAt?: string | null
    diagnostics: Array<{ workloadRef?: string | null; categories?: Record<string, number>; category?: string; count?: number | null; observedAt?: string | null }> }>
  toolEvidence: unknown[] }

const finite = (v: unknown) => v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0)
const time = (v: unknown) => v === null || (typeof v === 'string' && Number.isFinite(Date.parse(v)))
const textOrNull = (v: unknown) => v === null || (typeof v === 'string' && v.length <= 512)
export function parseActionSummary(value: unknown): ActionSummary {
  const a = value as ActionSummary
  if (!a || typeof a.id !== 'string' || !a.id || !['decision', 'tool'].includes(a.type)
    || !['action', 'cycle', 'call'].includes(a.relation) || !time(a.at)
    || (a.cycle !== null && (!Number.isSafeInteger(a.cycle) || a.cycle < 0))
    || (a.role != null && !['planner', 'validator', 'executor'].includes(a.role))
    || (a.targetRefs !== undefined && !validTargetRefs(a.targetRefs))
    || [a.decisionId, a.actionId, a.callId, a.action, a.target, a.scenario].some((s) => s !== null && (typeof s !== 'string' || s.length > 512))) throw new Error('Invalid action summary')
  return a
}

export function roundActions(data: RoundObservability | null | undefined, events: AgentEvent[]): ActionSummary[] {
  const recorded = data?.actions?.map((action) => ({ ...action, role: action.role ??
    (action.callId ? events.find((e) => e.callId === action.callId && ['planner', 'validator', 'executor'].includes(e.role ?? ''))?.role as AgentRole | undefined
      : action.type === 'decision' ? 'planner' : null) ?? null }))
  // Older v1 servers already expose durable decision/change/call identities.
  const decisions = (data?.decisions ?? []).map((d): ActionSummary => {
    const c = data?.changes.find((c) => c.decisionId === d.id)
    return { id: d.id, decisionId: d.id, cycle: d.cycle, actionId: c?.id ?? null, callId: null, at: d.at ?? null,
      type: 'decision', role: 'planner', action: d.action, target: c?.profileId ?? null, scenario: c?.scenario ?? d.scenario ?? null, relation: c ? 'action' : 'cycle' }
  })
  const calls = [...new Map(events.filter((e) => e.callId).map((e) => [e.callId, e])).values()].map((e): ActionSummary => ({
    id: `call:${e.callId}`, decisionId: data?.decisions.find((d) => d.cycle === e.cycle)?.id ?? null, cycle: e.cycle ?? null,
    actionId: null, callId: e.callId, at: typeof e.at === 'string' ? e.at : null, type: 'tool', action: null,
    target: e.tool ?? null, tool: e.tool, scenario: null, role: ['planner', 'validator', 'executor'].includes(e.role ?? '') ? e.role as AgentRole : null, targetRefs: e.targetRefs, relation: 'call',
  }))
  // A retained completion may outlive its start/artifact or a lagging summary.
  // Keep that observed call selectable without inventing evidence or targets.
  const known = new Set(recorded?.map((a) => a.callId).filter(Boolean))
  return recorded ? [...recorded, ...calls.filter((a) => !known.has(a.callId))].slice(-200) : [...decisions, ...calls].slice(-200)
}

export function scopedActions(actions: ActionSummary[], data: RoundObservability | null | undefined, scenario: ScenarioGroup,
  sampleId?: string | null, decisionId?: string | null, role?: AgentRole | null): ActionSummary[] {
  return actions.filter((a) => {
    if (role && a.role !== role) return false
    if (a.scenario !== null && a.scenario !== scenario) return false
    const d = data?.decisions.find((d) => d.id === a.decisionId || d.cycle === a.cycle)
    if (decisionId) return a.decisionId === decisionId || d?.id === decisionId
    return !sampleId || Boolean(d?.beforeSampleIds?.includes(sampleId) || d?.afterSampleIds?.includes(sampleId))
  })
}

export function selectedActivityAction(actions: ActionSummary[], selectedId?: string | null, currentCallId?: string | null): ActionSummary | null {
  if (selectedId) return actions.find((a) => a.id === selectedId) ?? null
  return actions.find((a) => a.callId && a.callId === currentCallId)
    ?? actions.filter((a) => a.type === 'decision').slice(-1)[0] ?? actions.slice(-1)[0] ?? null
}

/** Compact presentation labels only; never infer target identity from a tool. */
export function shortActivityLabel(name: string | null | undefined): [string, string] {
  const labels: Record<string, [string, string]> = {
    apply: ['套用', 'Apply'], hold: ['維持', 'Hold'], restore: ['還原', 'Restore'],
    get_nf_resource_usage: ['資源', 'Resources'], get_k8s_resource_usage: ['資源', 'Resources'],
    get_nf_inventory: ['盤點', 'Inventory'], get_k8s_workloads: ['Pod', 'Pods'], get_nf_health: ['健康', 'Health'],
    get_environment_overview: ['網路', 'Network'], get_slice_status: ['切片', 'Slice'], get_slice_topology: ['路徑', 'Path'],
    get_slice_qos: ['QoS', 'QoS'], get_upf_dataplane_state: ['UPF', 'UPF'], get_nf_log_excerpt: ['日誌', 'Logs'],
    get_k8s_events: ['事件', 'Events'], get_traffic_metrics: ['流量', 'Traffic'], get_metric_trend: ['趨勢', 'Trend'],
    read_native_qer: ['QER', 'QER'], verify_effect: ['驗證', 'Verify'], verify_installation: ['安裝', 'Install'],
    apply_validated_plan: ['套用', 'Apply'], restore_owned_policy: ['還原', 'Restore'], validate_plan: ['檢查', 'Check'],
    measure_result_window: ['量測', 'Measure'], compare_traffic_windows: ['比較', 'Compare'],
    get_observation_capabilities: ['能力', 'Capabilities'], get_k8s_cluster_health: ['叢集', 'Cluster'], get_nf_peer_status: ['對等', 'Peers'],
    get_control_plane_failures: ['失敗', 'Failures'], get_ue_session_summary: ['連線', 'Sessions'], get_ran_status: ['RAN', 'RAN'],
    get_n6_status: ['N6', 'N6'], get_packet_path_metrics: ['路徑', 'Path'], search_lessons: ['經驗', 'Lessons'],
    get_previous_decision: ['前次', 'Previous'], get_current_policy: ['政策', 'Policy'], get_traffic_demand: ['需求', 'Demand'],
    get_network_snapshot: ['快照', 'Snapshot'], list_policy_capabilities: ['能力', 'Capabilities'], inspect_plan: ['方案', 'Plan'],
    check_round_scope: ['範圍', 'Scope'], check_observation_freshness: ['時效', 'Freshness'], check_policy_capability: ['能力', 'Capabilities'],
    get_validated_plan: ['方案', 'Plan'], get_action_status: ['狀態', 'Status'], confirm_validated_hold: ['維持', 'Hold'],
    verify_restoration: ['還原', 'Restore'], decline_stage: ['婉拒', 'Decline'],
    get_round_context: ['本輪', 'Round'], get_policy_capabilities: ['能力', 'Capabilities'],
    observe_network: ['網路', 'Network'], observe_traffic: ['流量', 'Traffic'],
    get_validated_decision: ['決策', 'Decision'], execute_decision: ['執行', 'Execute'],
    check_proposal: ['檢查', 'Check'], check_execution: ['安裝', 'Install'],
    check_effect: ['驗證', 'Verify'], check_restoration: ['還原', 'Restore'], record_lesson: ['記憶', 'Memory'],
  }
  return labels[name ?? ''] ?? ['操作', 'Action']
}

export function actionRelatedPanels(action: ActionSummary, evidence?: ActionEvidence | null) {
  const tool = action.tool ?? action.target ?? ''
  const qer = action.action === 'apply' || action.action === 'restore' || /qer|policy/.test(tool)
  const resources = /^get_(k8s_workloads|nf_inventory|nf_health|k8s_resource_usage|nf_resource_usage|n6_status|environment_overview)$/.test(tool)
    || /^(scale|restart|deploy)_/.test(tool) || Boolean(evidence?.resources.length)
  return { qer: action.action !== 'hold' && qer, resources, logs: true }
}

export function parseActionEvidence(value: unknown, roundId: string, generation: number, action: ActionSummary): ActionEvidence {
  const e = value as ActionEvidence
  const validReadback = (r: Readback | undefined) => r == null || Boolean(Number.isSafeInteger(r.ueCount) && r.ueCount >= 0 && r.ueCount <= 500
    && [r.uplinkKbps, r.downlinkKbps].every((range) => range === null || (range && finite(range.min) && finite(range.max) && range.min <= range.max)))
  if (!e || e.schemaVersion !== 'cityverse.action-evidence/v1' || e.roundId !== roundId || e.generation !== generation
    || (action.cycle !== null && e.cycle !== action.cycle) || (action.actionId !== null && e.actionId !== action.actionId)
    || (action.callId !== null && e.callId !== action.callId) || !e.qer || typeof e.qer.status !== 'string' || typeof e.qer.consistency !== 'string'
    || !validReadback(e.qer.before) || !validReadback(e.qer.readback)
    || (e.qer.observedAt !== undefined && !time(e.qer.observedAt))
    || (e.qer.requested != null && (typeof e.qer.requested.profileId !== 'string'
      || ![e.qer.requested.uplinkKbps, e.qer.requested.downlinkKbps].every((n) => typeof n === 'number' && Number.isFinite(n) && n > 0 && n <= 500000)))
    || !Array.isArray(e.resources) || e.resources.length > 100 || !Array.isArray(e.diagnostics) || e.diagnostics.length > 100
    || !Array.isArray(e.toolEvidence) || e.toolEvidence.length > 400
    || e.resources.some((r) => action.callId !== null && r.callId !== action.callId)
    || e.resources.some((r) => !r || typeof r.status !== 'string' || !time(r.observedAt) || !Array.isArray(r.workloads) || r.workloads.length > 100
      || r.workloads.some((w) => !w || (w.ready !== null && typeof w.ready !== 'boolean') || ![w.readyPods, w.desiredPods, w.restarts].every(finite)
        || ![w.workloadRef, w.podUid, w.nfType, w.sharing, w.phase].every(textOrNull))
      || !Array.isArray(r.metrics) || r.metrics.length > 100 || r.metrics.some((m) => !m || !time(m.observedAt) || !Array.isArray(m.containers)
        || ![m.workloadRef, m.podUid, m.nfType, m.sharing, m.identityStatus, m.status].every(textOrNull)
        || m.containers.length > 100 || m.containers.some((c) => !c || !textOrNull(c.name) || !finite(c.cpuCores) || !finite(c.memoryBytes)))
      || (r.bindings !== undefined && (!Array.isArray(r.bindings) || r.bindings.length > 32 || r.bindings.some((b) => !b || ![b.slice, b.podUid, b.workloadRef, b.status, b.sharing].every(textOrNull)))))
    || e.diagnostics.some((d) => !d || (action.callId !== null && d.callId !== action.callId) || !Array.isArray(d.diagnostics) || d.diagnostics.length > 100)) throw new Error('Invalid action evidence scope or payload')
  return e
}

export async function readActionEvidence(roundId: string, generation: number, action: ActionSummary, signal: AbortSignal) {
  const query = new URLSearchParams()
  if (action.cycle !== null) query.set('cycle', String(action.cycle))
  if (action.actionId) query.set('actionId', action.actionId)
  if (action.callId) query.set('callId', action.callId)
  return parseActionEvidence(await scenarioRead(`/${encodeURIComponent(roundId)}/action-evidence?${query}`, signal), roundId, generation, action)
}

export function evidenceForRole(evidence: ActionEvidence, role: AgentRole | null): ActionEvidence {
  return role ? { ...evidence, resources: evidence.resources.filter((r) => r.role === role),
    diagnostics: evidence.diagnostics.filter((r) => r.role === role),
    toolEvidence: evidence.toolEvidence.filter((r) => r && typeof r === 'object' && 'role' in r && r.role === role) } : evidence
}

export type ActionLogSource = { sourceRef: string; workloadRef: string | null; podUid: string | null; container: string | null; observedAt: string | null; evidenceRef: string | null }
export type ActionLogLine = { sequence: number; at: string | null; text: string; sourceRef: string }
export type ActionLogs = { schemaVersion: 'cityverse.action-logs/v1'; roundId: string; generation: number; cycle: number | null
  actionId: string | null; callId: string | null; role: AgentRole | null; status: 'captured' | 'not_captured' | 'unavailable'
  reason: string | null; relation: 'action-time'; sources: ActionLogSource[]; lines: ActionLogLine[]; nextCursor: number; hasMore: boolean; gap: boolean }

export function actionReadKey(roundId: string | null, generation: number | null, action: ActionSummary | null, role?: AgentRole | null) {
  return JSON.stringify([roundId, generation, action?.id ?? null, action?.cycle ?? null, action?.actionId ?? null, action?.callId ?? null, role ?? null])
}

export function parseActionLogs(value: unknown, roundId: string, generation: number, action: ActionSummary, after = 0, role: AgentRole | null = null): ActionLogs {
  const v = value as ActionLogs
  if (!v || v.schemaVersion !== 'cityverse.action-logs/v1' || v.roundId !== roundId || v.generation !== generation
    || (action.cycle !== null && v.cycle !== action.cycle) || (action.actionId !== null && v.actionId !== action.actionId)
    || (action.callId !== null && v.callId !== action.callId) || v.role !== role || v.relation !== 'action-time'
    || !['captured', 'not_captured', 'unavailable'].includes(v.status) || !textOrNull(v.reason)
    || !Number.isSafeInteger(v.nextCursor) || v.nextCursor < after || typeof v.hasMore !== 'boolean' || typeof v.gap !== 'boolean'
    || (v.hasMore && v.nextCursor <= after) || !Array.isArray(v.sources) || v.sources.length > 100
    || v.sources.some((s) => !s || typeof s.sourceRef !== 'string' || !s.sourceRef || s.sourceRef.length > 512
      || ![s.workloadRef, s.podUid, s.container, s.evidenceRef].every(textOrNull) || (!s.workloadRef && !s.podUid) || !time(s.observedAt))
    || !Array.isArray(v.lines) || v.lines.length > 100 || v.lines.some((line) => !line || !Number.isSafeInteger(line.sequence)
      || line.sequence <= after || line.sequence > v.nextCursor || typeof line.text !== 'string' || line.text.length > 4096
      || !time(line.at) || !v.sources.some((s) => s.sourceRef === line.sourceRef))
    || (v.status !== 'captured' && v.lines.length > 0)) throw new Error('Invalid action log scope or payload')
  return v
}

export async function readActionLogs(roundId: string, generation: number, action: ActionSummary, after: number, signal: AbortSignal, role: AgentRole | null = null) {
  const query = new URLSearchParams({ after: String(after), limit: '100' })
  if (action.cycle !== null) query.set('cycle', String(action.cycle))
  if (action.actionId) query.set('actionId', action.actionId)
  if (action.callId) query.set('callId', action.callId)
  if (role) query.set('role', role)
  return parseActionLogs(await scenarioRead(`/${encodeURIComponent(roundId)}/action-logs?${query}`, signal), roundId, generation, action, after, role)
}
