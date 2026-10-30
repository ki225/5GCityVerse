import { useEffect, useRef, useState } from 'react'
import { actionReadKey, evidenceForRole, readActionEvidence, readActionLogs, type ActionEvidence, type ActionSummary, type ActionLogs, type ActionLogLine, type ActionLogSource } from '../../services/actionEvidence'
import { readAgentEvents, mergeObservationRows, type AgentEvent, type AgentRole } from '../../services/roundObservability'
import { startScenarioPolling } from '../../services/scenarioPolling'

export type ActionDetailState = { evidence: ActionEvidence | null; events: AgentEvent[]; loading: boolean; error: string; gap: boolean; hasMore: boolean }
const empty: ActionDetailState = { evidence: null, events: [], loading: false, error: '', gap: false, hasMore: false }
/** Same scoped cache/abort lifetime for evidence and its bounded log pages. */
export function startActionEvidenceRead(roundId: string, generation: number, action: ActionSummary, after: number,
  receive: (evidence: ActionEvidence, events: AgentEvent[], cursor: number, hasMore: boolean, gap: boolean) => void, fail: (cause: unknown) => void, role: AgentRole | null = null, includeEvents = true) {
  const stop = startScenarioPolling(async (signal) => {
    const results = await Promise.allSettled([readActionEvidence(roundId, generation, action, signal),
      includeEvents ? readAgentEvents(roundId, after, signal, { cycle: action.cycle, actionId: action.actionId, callId: action.callId, role })
        : Promise.resolve({ events: [] as AgentEvent[], nextCursor: after, hasMore: false, gap: false })])
    const [evidence, events] = results
    for (const r of results) if (r.status === 'rejected') throw r.reason
    if (evidence.status !== 'fulfilled' || events.status !== 'fulfilled') throw new Error('Action detail read failed')
    return { evidence: evidenceForRole(evidence.value, role), page: events.value }
  }, ({ evidence, page }) => { receive(evidence, page.events, page.nextCursor, page.hasMore, page.gap); stop() },
  (cause) => { fail(cause); stop() })
  return stop
}

export function useActionEvidence(roundId: string | null, generation: number | null, action: ActionSummary | null, open: boolean, revision: number, role: AgentRole | null = null, includeEvents = true) {
  const owner = `${roundId ?? ''}:${generation ?? ''}`
  const key = `${actionReadKey(roundId, generation, action, role)}:${revision}:${includeEvents}`
  const cache = useRef(new Map<string, ActionDetailState & { cursor: number }>())
  const [state, setState] = useState<ActionDetailState & { key: string }>({ ...empty, key: '' })
  const [pageRequest, setPageRequest] = useState(0)
  useEffect(() => { cache.current.clear() }, [owner])
  useEffect(() => {
    const cached = cache.current.get(key)
    setState({ ...(cached ?? empty), key, loading: open && (!cached || pageRequest > 0) })
    if (!open || !roundId || generation === null || !action) return
    if (cached && pageRequest === 0) return
    const after = cached?.cursor ?? 0
    return startActionEvidenceRead(roundId, generation, action, after, (evidence, events, cursor, hasMore, gap) => {
      const next = { evidence, events: mergeObservationRows(cached?.events ?? [], events, 300), cursor, hasMore,
        gap: Boolean(cached?.gap || gap || (cached?.events.length ?? 0) + events.length > 300), loading: false, error: '' }
      cache.current.set(key, next)
      if (cache.current.size > 20) cache.current.delete(cache.current.keys().next().value!)
      setState({ ...next, key })
    }, (cause) => setState((current) => current.key === key ? { ...current, error: String(cause), loading: false } : current), role, includeEvents)
  }, [key, open, pageRequest])
  useEffect(() => setPageRequest(0), [key])
  return { ...(state.key === key ? state : empty), loadMore: () => setPageRequest((n) => n + 1) }
}

export function startActionLogRead(roundId: string, generation: number, action: ActionSummary, after: number, role: AgentRole | null,
  receive: (page: ActionLogs) => void, fail: (cause: unknown) => void) {
  const stop = startScenarioPolling((signal) => readActionLogs(roundId, generation, action, after, signal, role),
    (page) => { receive(page); stop() }, (cause) => { fail(cause); stop() })
  return stop
}

type RawLogState = { lines: ActionLogLine[]; sources: ActionLogSource[]; status: ActionLogs['status'] | null; reason: string | null
  loading: boolean; error: string; hasMore: boolean; gap: boolean; cursor: number }
const noLogs: RawLogState = { lines: [], sources: [], status: null, reason: null, loading: false, error: '', hasMore: false, gap: false, cursor: 0 }
/** A raw log disclosure has its own lifetime; evidence/event reads never open it. */
export function useActionLogs(roundId: string | null, generation: number | null, action: ActionSummary, open: boolean, role: AgentRole | null, revision = 0) {
  const owner = `${roundId ?? ''}:${generation ?? ''}`
  const key = `${actionReadKey(roundId, generation, action, role)}:${revision}`
  const cache = useRef(new Map<string, RawLogState>())
  const [state, setState] = useState<RawLogState & { key: string }>({ ...noLogs, key: '' })
  const [pageRequest, setPageRequest] = useState(0)
  useEffect(() => { cache.current.clear() }, [owner])
  useEffect(() => {
    const cached = cache.current.get(key)
    setState({ ...(cached ?? noLogs), key, loading: open && (!cached || pageRequest > 0) })
    if (!open || !roundId || generation === null || (cached && pageRequest === 0)) return
    return startActionLogRead(roundId, generation, action, cached?.cursor ?? 0, role, (page) => {
      const lines = mergeObservationRows(cached?.lines ?? [], page.lines, 500)
      const sources = [...new Map([...(cached?.sources ?? []), ...page.sources].map((source) => [source.sourceRef, source])).values()]
      const next: RawLogState = { lines, sources, status: page.status, reason: page.reason, cursor: page.nextCursor,
        loading: false, error: '', hasMore: page.hasMore, gap: Boolean(cached?.gap || page.gap || (cached?.lines.length ?? 0) + page.lines.length > 500) }
      cache.current.set(key, next)
      if (cache.current.size > 20) cache.current.delete(cache.current.keys().next().value!)
      setState({ ...next, key })
    }, (cause) => setState((current) => current.key === key ? { ...current, loading: false, error: String(cause) } : current))
  }, [key, open, pageRequest])
  useEffect(() => setPageRequest(0), [key])
  return { ...(state.key === key ? state : noLogs), loadMore: () => setPageRequest((n) => n + 1) }
}
