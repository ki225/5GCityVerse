import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { accessToken, clearAccessToken, hasAccessToken, requireAccessToken, setAccessToken } from '../auth'

describe('out-of-band access token', () => {
  beforeEach(() => clearAccessToken())
  afterEach(() => vi.unstubAllGlobals())

  it('rejects whitespace without creating usable authorization', () => {
    expect(setAccessToken('   ')).toBe(false)
    expect(hasAccessToken()).toBe(false)
    expect(() => requireAccessToken()).toThrow('API access token is required')
  })

  it('trims and returns an in-memory token in the test runtime', () => {
    expect(setAccessToken('  secret-value  ')).toBe(true)
    expect(accessToken()).toBe('secret-value')
    expect(hasAccessToken()).toBe(true)
  })

  it('never logs the token', () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    setAccessToken('not-for-console')
    requireAccessToken()
    expect(log).not.toHaveBeenCalled()
    log.mockRestore()
  })

  it('stores browser credentials in sessionStorage only', () => {
    const values = new Map<string, string>()
    const sessionStorage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    }
    const localSet = vi.fn()
    vi.stubGlobal('window', { sessionStorage, localStorage: { setItem: localSet } })
    expect(setAccessToken('browser-secret')).toBe(true)
    expect(accessToken()).toBe('browser-secret')
    expect(localSet).not.toHaveBeenCalled()
  })
})
