import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiBaseUrl, capabilityIsAvailable, capabilityLabel, runtimeProfile, webSocketUrl } from '../runtimeConfig'
afterEach(() => vi.unstubAllEnvs())
describe('one production onprem configuration', () => {
  it.each(['production', 'development', 'test'])('uses identical onprem defaults in %s', mode => {
    vi.stubEnv('MODE', mode); vi.stubEnv('VITE_RUNTIME_PROFILE', 'cloud'); vi.stubEnv('VITE_DEPLOYMENT_PROFILE', 'aws')
    expect(runtimeProfile()).toBe('onprem')
    expect(apiBaseUrl()).toBe('')
    expect(webSocketUrl()).toBe('ws://127.0.0.1:8000/ws')
  })
  it('accepts explicit operator endpoints independently of build mode', () => {
    vi.stubEnv('VITE_API_URL', 'http://localhost:9000/'); vi.stubEnv('VITE_WS_URL', 'ws://localhost:9000/ws')
    expect(apiBaseUrl()).toBe('http://localhost:9000'); expect(webSocketUrl()).toBe('ws://localhost:9000/ws')
  })
  it('does not promote configured credentials into verified capabilities', () => {
    expect(capabilityIsAvailable({ status: 'unverified', configured: true })).toBe(false)
    expect(capabilityIsAvailable({ status: 'available', verified: true })).toBe(true)
    expect(capabilityLabel({ configured: false })).toBe('not_configured')
  })
})
