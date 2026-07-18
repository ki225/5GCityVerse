import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { getMetrics, getResetStatus, resetSimulation, triggerCityEvents, waitForResetCompletion } from '../api'
import { clearAccessToken, setAccessToken } from '../auth'

beforeEach(() => setAccessToken('unit-test-token'))

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

describe('errorMessage shape handling (via getMetrics)', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders a string detail as "error: detail"', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(400, { error: 'INVALID_REQUEST', detail: 'Unknown event_type: bogus' }),
    )

    await expect(getMetrics()).rejects.toThrow('INVALID_REQUEST: Unknown event_type: bogus')
  })

  it('extracts a human-readable field from a dict detail', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(503, {
        error: 'EVENT_BLOCKED',
        detail: {
          executionId: 'exec-1',
          eventType: 'concert',
          status: 'AGENT_BLOCKED',
          error: 'free5GC is offline; event trigger blocked',
          detail: 'connection refused',
        },
      }),
    )

    await expect(getMetrics()).rejects.toThrow('EVENT_BLOCKED: connection refused')
  })

  it('falls back to JSON.stringify when a dict detail has no human field', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(503, {
        error: 'EVENT_BLOCKED',
        detail: { executionId: 'exec-1', eventType: 'concert' },
      }),
    )

    await expect(getMetrics()).rejects.toThrow(
      'EVENT_BLOCKED: {"executionId":"exec-1","eventType":"concert"}',
    )
  })

  it('falls back to "HTTP <status>" when the body is not JSON', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)

    await expect(getMetrics()).rejects.toThrow('HTTP 500')
  })
})

describe('resetSimulation', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns the durable reset job from a 202 response', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(202, {
      resetId: 'reset-1', status: 'queued', progressStage: 'queued', progressPercent: 0,
    }))

    await expect(resetSimulation()).resolves.toMatchObject({ resetId: 'reset-1', status: 'queued' })
  })

  it('throws using the shared error handling when the response is not ok', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(500, { error: 'INTERNAL_ERROR', detail: 'Internal server error' }),
    )

    await expect(resetSimulation()).rejects.toThrow('INTERNAL_ERROR: Internal server error')
  })
})

describe('reset status polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('polls the session-scoped status URL until success and reports progress', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, {
      resetId: 'reset 1', status: 'success', progressStage: 'complete', progressPercent: 100,
    }))
    const observed: number[] = []
    const completion = waitForResetCompletion(
      { resetId: 'reset 1', status: 'running', progressStage: 'runtime_cleanup', progressPercent: 10 },
      (status) => observed.push(status.progressPercent),
      { timeoutMs: 100, pollMs: 10 },
    )

    await vi.advanceTimersByTimeAsync(10)
    await expect(completion).resolves.toMatchObject({ status: 'success', progressPercent: 100 })
    expect(observed).toEqual([10, 100])
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toContain('/events/reset/reset%201')
  })

  it('surfaces a persisted failed terminal state', async () => {
    await expect(waitForResetCompletion({
      resetId: 'reset-1', status: 'failed', progressStage: 'failed', progressPercent: 100, error: 'SMF rollout failed',
    })).rejects.toThrow('SMF rollout failed')
  })

  it('keeps transient polling failures observable and eventually times out', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('temporary network failure'))
    const completion = waitForResetCompletion(
      { resetId: 'reset-1', status: 'running', progressStage: 'session_recycle', progressPercent: 50 },
      undefined,
      { timeoutMs: 20, pollMs: 10 },
    )
    const rejection = expect(completion).rejects.toThrow(/timed out.*temporary network failure/i)

    await vi.advanceTimersByTimeAsync(20)
    await rejection
  })

  it('gets reset status with browser session headers', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, {
      resetId: 'reset-1', status: 'running', progressStage: 'session_recycle', progressPercent: 50,
    }))
    await getResetStatus('reset-1')
    const init = vi.mocked(fetch).mock.calls[0]?.[1] as RequestInit
    expect(init.headers).toMatchObject({ Authorization: 'Bearer unit-test-token' })
  })
})

describe('triggerCityEvents contract', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the slicing strategy selected in Event Setup', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, { executionId: 'exec', executionIds: [], events: [] }))

    await triggerCityEvents({
      cityResidents: 180_000,
      sliceStrategy: 'static',
      scenarios: [{ eventType: 'concert', eventScale: 42 }],
    })

    const init = vi.mocked(fetch).mock.calls[0]?.[1] as RequestInit
    expect(init.headers).toMatchObject({ Authorization: 'Bearer unit-test-token' })
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).not.toContain('unit-test-token')
    expect(JSON.parse(String(init.body))).toMatchObject({
      city_residents: 180_000,
      slice_strategy: 'static',
      scenarios: [{ event_type: 'concert', event_scale: 42 }],
    })
  })

  it('does not issue a request when the access token is blank', async () => {
    clearAccessToken()
    await expect(getMetrics()).rejects.toThrow('API access token is required')
    expect(fetch).not.toHaveBeenCalled()
  })
})
