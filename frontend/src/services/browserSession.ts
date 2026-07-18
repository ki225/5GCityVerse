import { requireAccessToken } from './auth'

const SESSION_KEY = '5gcityverse.browserSessionId'

export function browserSessionId(): string {
  if (typeof window === 'undefined') return 'test-session'
  const existing = window.sessionStorage.getItem(SESSION_KEY)
  if (existing) return existing
  const value = globalThis.crypto?.randomUUID?.() ?? `session-${Date.now()}-${Math.random().toString(16).slice(2)}`
  window.sessionStorage.setItem(SESSION_KEY, value)
  return value
}

export function sessionHeaders(): Record<string, string> {
  const token = requireAccessToken()
  return { 'X-Session-Id': browserSessionId(), Authorization: `Bearer ${token}` }
}
