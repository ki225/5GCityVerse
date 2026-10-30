import { afterEach,beforeEach,describe,expect,it,vi } from 'vitest'
import { getDailyNativeObservation,getFree5gcStatus,getMetrics } from '../api'



function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

describe('core status daily observation option', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, {}))))
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

  it('keeps the default status URL and explicitly requests the core projection', async () => {
    await getFree5gcStatus()
    await getFree5gcStatus({ includeDaily: false })
    await getFree5gcStatus({ includeDaily: true })
    expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
      '/free5gc/status', '/free5gc/status?includeDaily=false', '/free5gc/status?includeDaily=true',
    ])
  })

  it('preserves the dedicated on-prem observation request', async () => {

    await getFree5gcStatus({ includeDaily: false })
    await getDailyNativeObservation()
    expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
      '/free5gc/status?includeDaily=false',
      '/api/onprem/daily-observation',
    ])
  })
})

describe('errorMessage shape handling (via getMetrics)', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
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
          profileId: 'video_streaming',
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
        detail: { executionId: 'exec-1', profileId: 'video_streaming' },
      }),
    )

    await expect(getMetrics()).rejects.toThrow(
      'EVENT_BLOCKED: {"executionId":"exec-1","profileId":"video_streaming"}',
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
