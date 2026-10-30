import type { ServiceIntentView } from '../../../services/intents'

export type EvidenceRow = Record<string, unknown>
export const record = (value: unknown): EvidenceRow => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as EvidenceRow : {}
export const rows = (value: unknown): EvidenceRow[] => Array.isArray(value) ? value.map(record) : []
export const scalar = (value: unknown): string => typeof value === 'string' || typeof value === 'number' ? String(value) : '—'
export const originalTime = (value: unknown): number | null => {
  const n = typeof value === 'number' ? value : typeof value === 'string' ? Date.parse(value) / 1000 : NaN
  return Number.isFinite(n) ? n : null
}

export function rejectedMembers(value: unknown): Array<{ session: string; reason: string }> {
  if (!Array.isArray(value)) return []
  return value.map(item => {
    const row = record(item)
    return Array.isArray(item)
      ? { session: scalar(item[0]), reason: scalar(item[1]) }
      : { session: scalar(row.session_token ?? row.session_key), reason: scalar(row.reason ?? row.message ?? row.code) }
  })
}

export interface Milestone {
  key: 'understood' | 'planned' | 'installed' | 'satisfied'
  state: 'confirmed' | 'historical' | 'unverified' | 'not_required'
  reason: string
}

/** Next original proof/contract boundary; one timer, no network polling or new observation clock. */
export function nextEvidenceDeadline(intent: ServiceIntentView | null, now: number): number | null {
  if (!intent) return null
  const after = record(intent.latest_operation?.after)
  const observed = originalTime(after.observed_at), age = after.maximum_age_seconds
  const candidates = [originalTime(intent.valid_from), originalTime(intent.valid_until)]
  if (observed !== null && typeof age === 'number' && Number.isFinite(age) && age > 0) {
    candidates.push(observed - 1, observed + age)
  }
  const future = candidates.filter((v): v is number => v !== null && Number.isFinite(v) && v > now)
  return future.length ? Math.min(...future) : null
}

/** Present durable server evidence; never turn transport or model invocation into an effect. */
export function intentEvidence(intent: ServiceIntentView, now = Date.now() / 1000) {
  const op = intent.latest_operation
  const plan = record(op?.plan), membership = record(intent.membership), effect = record(op?.effect)
  const after = record(op?.after), objectives = rows(intent.objectives)
  const intentState = intent.status ?? intent.state
  const start = originalTime(intent.valid_from), expiry = originalTime(intent.valid_until)
  const ended = ['expired', 'released'].includes(String(intentState)) || expiry !== null && now >= expiry
  const blocked = ended || ['recovery_required', 'failed', 'compensating', 'compensated'].includes(String(op?.status))
  const contract = objectives.length > 0 && objectives.every(o => typeof o.metric === 'string' && typeof o.target === 'number')
    && Object.keys(record(intent.selector)).length > 0 && typeof intent.provenance === 'string' && !!intent.provenance
    && start !== null && expiry !== null && expiry > start
    && !rows(intent.definition_issues).length && intentState !== 'needs_definition'
  const planned = !!plan.plan_id && plan.intent_id === intent.intent_id && plan.intent_revision === intent.revision && Array.isArray(plan.actions)
  const actions = rows(plan.actions), receipts = rows(op?.receipts)
  const currentInstallStatus = ['applying', 'verifying', 'completed'].includes(String(op?.status))
  const installed = planned && currentInstallStatus && actions.length > 0 && actions.every(action => receipts.some(receipt => {
    const request = record(receipt.request)
    return receipt.action_id === action.action_id && request.operation_id === op?.operation_id
      && request.action === action.kind && request.target_id === action.target_id
      && Object.keys(record(receipt.installed)).length > 0 && ['installation_confirmed', 'effect_verified'].includes(String(receipt.state))
  }))
  const historicalInstall = planned && actions.length > 0 && actions.every(action => receipts.some(receipt => receipt.action_id === action.action_id
    && (typeof receipt.installation_confirmed_at === 'number' || ['installation_confirmed', 'effect_verified'].includes(String(receipt.state)))
    && Object.keys(record(receipt.installed)).length > 0))
  const observed = originalTime(after.observed_at), ageLimit = after.maximum_age_seconds
  const pending = membership.pending_count
  const currentWindow = observed !== null && typeof ageLimit === 'number' && Number.isFinite(ageLimit) && ageLimit > 0 && now - observed >= -1 && now - observed <= ageLimit
  const satisfied = contract && planned && start !== null && now >= start && !blocked && intentState === 'satisfied' && op?.status === 'completed'
    && effect.met === true && effect.protected_services_met === true && effect.same_membership === true && effect.fresh_window === true
    && effect.operation_id === op.operation_id && effect.owner_id === intent.intent_id && effect.source === 'current-native-receiver'
    && !!effect.observation_id && effect.observation_id === after.observation_id && effect.observed_at === after.observed_at
    && rows(effect.objectives).length > 0 && rows(effect.objectives).every(v => v.status === 'passed')
    && membership.effect_invalidated !== true && (pending === 0 || pending === undefined && rows(membership.pending_members).length === 0)
    && currentWindow
  const milestones: Milestone[] = [
    { key: 'understood', state: contract ? ended ? 'historical' : 'confirmed' : 'unverified', reason: contract ? 'contract_recorded' : 'contract_evidence_missing' },
    { key: 'planned', state: planned ? ended ? 'historical' : 'confirmed' : 'unverified', reason: planned ? 'versioned_plan_recorded' : 'matching_plan_missing' },
    { key: 'installed', state: installed ? blocked ? 'historical' : 'confirmed' : historicalInstall ? 'historical' : planned && actions.length === 0 ? 'not_required' : 'unverified', reason: planned && actions.length === 0 ? 'hold_no_resource_change' : installed ? 'native_receipt_recorded' : historicalInstall ? 'prior_installation_current_operation_unverified' : 'native_installation_unverified' },
    { key: 'satisfied', state: satisfied ? 'confirmed' : 'unverified', reason: satisfied ? 'current_receiver_window_verified' : blocked ? 'inactive_or_recovery_current_effect_unverified' : !currentWindow ? 'current_receiver_window_missing_or_stale' : 'current_effect_unverified' },
  ]
  return { milestones, satisfied, currentWindow, model: record(op?.model_provenance), rejected: rejectedMembers(membership.rejected) }
}

/** Count only a complete set of actual unique server session identities, never UE aggregates. */
export function actualSessionCount(perSession: unknown, fresh: boolean): number | null {
  if (!fresh || !Array.isArray(perSession)) return null
  const keys = perSession.map(value => record(value).sessionKey)
  if (keys.some(k => typeof k !== 'string' || !k) || new Set(keys).size !== keys.length) return null
  return keys.length
}
