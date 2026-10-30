/** One effect lifetime owns one serial polling loop. StrictMode cleanup cannot
 * be revived by a different lifetime's shared mounted ref. GET timeouts are
 * errors (and retried); intentional disposal is never a visible failure. */
export function startScenarioPolling<T>(read: (signal: AbortSignal) => Promise<T>,
  receive: (value: T) => void, failure: (error: unknown) => void,
  options: { intervalMs?: number | ((value: T) => number); timeoutMs?: number } = {}) {
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let activeDeadline: ReturnType<typeof setTimeout> | undefined
  let active: AbortController | undefined
  let interval = typeof options.intervalMs === 'number' ? options.intervalMs : 3000
  async function poll() {
    if (stopped) return
    const started = Date.now()
    const controller = new AbortController()
    active = controller
    let timedOut = false
    const deadline = setTimeout(() => {
      timedOut = true
      controller.abort(new Error('Scenario snapshot request timed out; retrying'))
    }, options.timeoutMs ?? 10000)
    activeDeadline = deadline
    try {
      const value = await read(controller.signal)
      if (!stopped && !timedOut) {
        if (typeof options.intervalMs === 'function') interval = options.intervalMs(value)
        receive(value)
      }
    } catch (error) {
      if (!stopped) failure(timedOut ? new Error('Scenario snapshot request timed out; retrying') : error)
    } finally {
      clearTimeout(deadline)
      activeDeadline = undefined
      active = undefined
      // Target request-start cadence, not response duration plus cadence.
      // Slow GETs remain serial; they never create overlapping requests.
      if (!stopped) timer = setTimeout(poll, Math.max(0, interval - (Date.now() - started)))
    }
  }
  void poll()
  return () => { stopped = true; clearTimeout(timer); clearTimeout(activeDeadline); active?.abort() }
}

export function scenarioPollInterval(value: { stage: string }) {
  return value.stage === 'idle' || value.stage === 'completed' ? 3000 : 2000
}
