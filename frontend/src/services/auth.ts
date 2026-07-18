const ACCESS_TOKEN_KEY = '5gcityverse.apiAccessToken'
export const AUTH_REQUIRED_EVENT = '5gcityverse:auth-required'
let nonBrowserToken = ''

export class MissingAccessTokenError extends Error {
  constructor() {
    super('API access token is required')
    this.name = 'MissingAccessTokenError'
  }
}

export function accessToken(): string {
  if (typeof window === 'undefined') return nonBrowserToken
  return window.sessionStorage.getItem(ACCESS_TOKEN_KEY)?.trim() ?? ''
}

export function hasAccessToken(): boolean {
  return accessToken().length > 0
}

export function setAccessToken(value: string): boolean {
  const token = value.trim()
  if (!token) return false
  if (typeof window === 'undefined') {
    nonBrowserToken = token
    return true
  }
  window.sessionStorage.setItem(ACCESS_TOKEN_KEY, token)
  return true
}

export function clearAccessToken(): void {
  if (typeof window === 'undefined') nonBrowserToken = ''
  else window.sessionStorage.removeItem(ACCESS_TOKEN_KEY)
}

export function requireAccessToken(): string {
  const token = accessToken()
  if (!token) throw new MissingAccessTokenError()
  return token
}

export function notifyAuthorizationRequired(): void {
  clearAccessToken()
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT))
}
