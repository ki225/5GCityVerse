import type { ActiveScenario, AgentDecision, AgentDecisionRecord, CityEventType, RuntimePrimeStatus } from '../types'

export type PersistedSliceStrategy = 'none' | 'static' | 'ai'

export interface PersistedSessionState {
  version: 1
  savedAt: number
  activeEvent: CityEventType | null
  activeScenarios: ActiveScenario[]
  isSimulating: boolean
  sliceStrategy: PersistedSliceStrategy
  submittedSliceStrategy: PersistedSliceStrategy | null
  agentDecision: AgentDecision | null
  agentDecisionHistory: AgentDecisionRecord[]
  roundReportReady: boolean
  agentLog: string[]
  runtimePrime: RuntimePrimeStatus | null
  orchestrationStage: string
}

const SESSION_STATE_KEY = '5gcityverse.sessionState'
const MAX_SESSION_AGE_MS = 24 * 60 * 60 * 1000

export function loadSessionState(): PersistedSessionState | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(SESSION_STATE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<PersistedSessionState>
    if (parsed.version !== 1 || !Number.isFinite(parsed.savedAt)) return null
    if (Date.now() - Number(parsed.savedAt) > MAX_SESSION_AGE_MS) {
      clearSessionState()
      return null
    }
    return {
      version: 1,
      savedAt: Number(parsed.savedAt),
      activeEvent: parsed.activeEvent ?? null,
      activeScenarios: Array.isArray(parsed.activeScenarios) ? parsed.activeScenarios.filter(hasExecutionId) : [],
      isSimulating: parsed.isSimulating === true,
      sliceStrategy: normalizeStrategy(parsed.sliceStrategy),
      submittedSliceStrategy: parsed.submittedSliceStrategy ? normalizeStrategy(parsed.submittedSliceStrategy) : null,
      agentDecision: parsed.agentDecision ?? null,
      agentDecisionHistory: Array.isArray(parsed.agentDecisionHistory) ? parsed.agentDecisionHistory : [],
      roundReportReady: parsed.roundReportReady === true,
      agentLog: Array.isArray(parsed.agentLog) ? parsed.agentLog.filter((item): item is string => typeof item === 'string').slice(-100) : [],
      runtimePrime: parsed.runtimePrime ?? null,
      orchestrationStage: typeof parsed.orchestrationStage === 'string' ? parsed.orchestrationStage : 'idle',
    }
  } catch {
    clearSessionState()
    return null
  }
}

export function saveSessionState(state: Omit<PersistedSessionState, 'version' | 'savedAt'>): void {
  if (typeof window === 'undefined') return
  window.sessionStorage.setItem(SESSION_STATE_KEY, JSON.stringify({
    version: 1,
    savedAt: Date.now(),
    ...state,
  }))
}

export function clearSessionState(): void {
  if (typeof window === 'undefined') return
  window.sessionStorage.removeItem(SESSION_STATE_KEY)
}

function hasExecutionId(value: unknown): value is ActiveScenario {
  return Boolean(value && typeof value === 'object' && typeof (value as ActiveScenario).executionId === 'string')
}

function normalizeStrategy(value: unknown): PersistedSliceStrategy {
  return value === 'static' || value === 'ai' ? value : 'none'
}
