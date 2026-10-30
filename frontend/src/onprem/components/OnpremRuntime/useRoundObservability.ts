import { useEffect, useState } from 'react'
import { startScenarioPolling } from '../../services/scenarioPolling'
import { mergeObservationRows, readAgentEvents, readObservationView, type ObservationState } from '../../services/roundObservability'

const empty: ObservationState = { data: null, events: [], samples: [], error: '', gap: false, truncated: false }
export function useRoundObservability(roundId: string | null, generation: number | null): ObservationState {
  const owner = `${roundId ?? ''}:${generation ?? ''}`
  const [state, setState] = useState<ObservationState & { owner: string }>({ ...empty, owner: '' })
  useEffect(() => {
    setState({ ...empty, owner })
    if (!roundId || generation === null) return
    let eventCursor = 0, measurementCursor = 0
    return startScenarioPolling(async (signal) => {
      const results = await Promise.allSettled([
        readObservationView(roundId, generation, measurementCursor, signal), readAgentEvents(roundId, eventCursor, signal),
      ])
      const [view, events] = results
      for (const result of results) if (result.status === 'rejected') throw result.reason
      if (view.status !== 'fulfilled' || events.status !== 'fulfilled') throw new Error('Observation read failed')
      return { data: view.value.observability, events: events.value, samples: view.value.measurements }
    }, ({ data, events, samples }) => {
      eventCursor = events.nextCursor; measurementCursor = samples.nextCursor
      setState((current) => ({ owner, data, events: mergeObservationRows(current.events, events.events, 300),
        samples: mergeObservationRows(current.samples, samples.samples, 1200), error: '',
        gap: current.gap || events.gap || samples.gap,
        truncated: current.truncated || current.events.length + events.events.length > 300 || current.samples.length + samples.samples.length > 1200,
      }))
    }, (cause) => setState((current) => ({ ...current, error: String(cause) })), {
      intervalMs: (page) => page.events.hasMore || page.samples.hasMore ? 0 : 1000,
    })
  }, [roundId, generation, owner])
  return state.owner === owner ? state : empty
}
