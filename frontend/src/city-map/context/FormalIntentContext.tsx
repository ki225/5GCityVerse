import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { getIntentCapabilities, getServiceIntent, IntentRequestError, submitServiceIntent,
  type IntentCapabilities, type IntentCityTraffic, type IntentSubmission, type ServiceIntentView } from '../../services/intents'
import { ACTIVE_CITY_INTENT, PENDING_CITY_INTENT, parseIntentSubmission, parsePendingCityIntent, validateCityIntent, type PendingCityIntent } from '../../services/cityIntent'

function useFormalIntentState() {
  const [caps, setCaps] = useState<IntentCapabilities | null>(null)
  const [capError, setCapError] = useState('')
  const [capLoading, setCapLoading] = useState(true)
  const [capRevision, setCapRevision] = useState(0)
  const [submission, setSubmission] = useState<IntentSubmission | null>(null)
  const [active, setActive] = useState<ServiceIntentView | null>(null)
  const [activeObservedAt, setActiveObservedAt] = useState(0)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [workspace, setWorkspace] = useState<{ intentId: string; serial: number } | null>(null)
  const busy = useRef(false), mounted = useRef(false), frozen = useRef<PendingCityIntent | null>(null)
  useEffect(() => {
    mounted.current = true
    sessionStorage.removeItem('cityverse:formal-intent:pending:v1')
    sessionStorage.removeItem('cityverse:formal-intent:active:v1')
    try {
      const raw = sessionStorage.getItem(PENDING_CITY_INTENT)
      if (raw) { frozen.current = parsePendingCityIntent(raw); setUncertain(true) }
      const accepted = sessionStorage.getItem(ACTIVE_CITY_INTENT)
      if (accepted) setSubmission(parseIntentSubmission(JSON.parse(accepted)))
    } catch { setError('saved_intent_requires_review'); setUncertain(true) }
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    const abort = new AbortController(); setCaps(null); setCapError(''); setCapLoading(true)
    void getIntentCapabilities(abort.signal).then(value => {
      if (abort.signal.aborted) return
      setCaps(value)
    }).catch(cause => { if (!abort.signal.aborted) setCapError(cause instanceof Error ? cause.message : 'capabilities_connection') })
      .finally(() => { if (!abort.signal.aborted) setCapLoading(false) })
    return () => abort.abort()
  }, [capRevision])
  useEffect(() => {
    if (!submission) return
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const value = await getServiceIntent(submission.intent_id, abort.signal)
        if (!abort.signal.aborted) {
          if (value.intent_id !== submission.intent_id) throw new Error('intent_identity_mismatch')
          setActive(value); setActiveObservedAt(Date.now()); setError('')
        }
      } catch { if (!abort.signal.aborted) { setActive(null); setActiveObservedAt(0); setError('intent_observation_unavailable') } }
      finally { if (!abort.signal.aborted) timer = setTimeout(poll, 3000) }
    }
    void poll(); return () => { abort.abort(); clearTimeout(timer) }
  }, [submission, capRevision])
  const dispatch = async (record: PendingCityIntent) => {
    if (busy.current) return
    busy.current = true; setPending(true); setError('')
    const abort = new AbortController(), timeout = setTimeout(() => abort.abort(), 30000)
    try {
      const result = { ...parseIntentSubmission(await submitServiceIntent(record.payload.text, record.payload.city_traffic, record.key, abort.signal)), traffic_request_id: record.payload.city_traffic.requestId }
      // Persist the accepted identity before removing the exact retry journal.
      sessionStorage.setItem(ACTIVE_CITY_INTENT, JSON.stringify(result))
      sessionStorage.removeItem(PENDING_CITY_INTENT)
      frozen.current = null
      if (mounted.current) { setSubmission(result); setActive(null); setUncertain(false) }
    } catch (cause) {
      if (cause instanceof IntentRequestError && cause.definitive) {
        sessionStorage.removeItem(PENDING_CITY_INTENT); frozen.current = null
        if (mounted.current) { setUncertain(false); setError(cause.code) }
      } else if (mounted.current) { setUncertain(true); setError('intent_submission_outcome_unknown') }
    } finally { clearTimeout(timeout); busy.current = false; if (mounted.current) setPending(false) }
  }
  const openWorkspace = useCallback((intentId = '') => setWorkspace(current => ({ intentId, serial: (current?.serial ?? 0) + 1 })), [])
  return { caps, capError, capLoading, active, activeObservedAt, submission, pending, uncertain, error, workspace, openWorkspace,
    refresh: () => { setCaps(null); setCapError(''); setCapLoading(true); setCapRevision(value => value + 1) },
    submit: async (text: string, traffic: IntentCityTraffic) => {
      if (busy.current || uncertain) return
      const record = { key: `city-intent:${traffic.requestId}`, payload: { text: text.trim(), city_traffic: structuredClone(traffic) } }
      try {
        validateCityIntent(record.payload)
        sessionStorage.setItem(PENDING_CITY_INTENT, JSON.stringify(record))
      } catch { setError('city_intent_cannot_be_saved'); return }
      frozen.current = record; await dispatch(record)
    },
    retry: async () => { if (frozen.current && !busy.current) await dispatch(frozen.current) },
  }
}
type FormalIntentState = ReturnType<typeof useFormalIntentState>
const Context = createContext<FormalIntentState | null>(null)
export const useFormalIntent = () => useContext(Context)
export function FormalIntentProvider({ children }: { children: ReactNode }) {
  return <Context.Provider value={useFormalIntentState()}>{children}</Context.Provider>
}
