import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { connectWebSocket } from '../websocket'
import { useAppStore } from '../../store/appStore'
import { hasAccessToken, setAccessToken } from '../auth'

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSED = 3
  static instances: FakeWebSocket[] = []
  static urls: string[] = []
  readyState = FakeWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((evt: { data: string }) => void) | null = null
  constructor(url: string) {
    FakeWebSocket.instances.push(this)
    FakeWebSocket.urls.push(url)
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code: 1000 })
  }
  rejectAuthorization() {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code: 1006 })
  }
  triggerOpen() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }
}

describe('wsConnected tracking', () => {
  let originalWebSocket: unknown
  let cleanup: (() => void) | null = null

  beforeEach(() => {
    FakeWebSocket.instances = []
    FakeWebSocket.urls = []
    setAccessToken('ws token/+?')
    useAppStore.getState().reset()
    originalWebSocket = (globalThis as any).WebSocket
    ;(globalThis as any).WebSocket = FakeWebSocket
  })

  afterEach(() => {
    cleanup?.()
    cleanup = null
    ;(globalThis as any).WebSocket = originalWebSocket
  })

  it('sets wsConnected true when the socket opens', () => {
    expect(useAppStore.getState().wsConnected).toBe(false)

    cleanup = connectWebSocket()
    const fake = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    fake.triggerOpen()

    expect(useAppStore.getState().wsConnected).toBe(true)
    const connectedUrl = FakeWebSocket.urls[FakeWebSocket.urls.length - 1]
    expect(connectedUrl).toContain('token=ws+token%2F%2B%3F')
    expect(connectedUrl).not.toContain('ws token/+?')
  })

  it('sets wsConnected false when the socket closes', () => {
    cleanup = connectWebSocket()
    const fake = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    fake.triggerOpen()
    expect(useAppStore.getState().wsConnected).toBe(true)

    fake.close()
    expect(useAppStore.getState().wsConnected).toBe(false)
  })

  it('sets wsConnected false when the socket errors (which triggers close)', () => {
    cleanup = connectWebSocket()
    const fake = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    fake.triggerOpen()
    expect(useAppStore.getState().wsConnected).toBe(true)

    fake.onerror?.()
    expect(useAppStore.getState().wsConnected).toBe(false)
  })

  it('clears an invalid token when the handshake closes before opening', () => {
    cleanup = connectWebSocket()
    FakeWebSocket.instances[FakeWebSocket.instances.length - 1].rejectAuthorization()
    expect(hasAccessToken()).toBe(false)
  })
})
