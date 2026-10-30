import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { connectWebSocket } from '../websocket'
import { useAppStore } from '../../store/appStore'

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
    useAppStore.getState().reset()
    originalWebSocket = (globalThis as any).WebSocket
    ;(globalThis as any).WebSocket = FakeWebSocket
  })

  afterEach(() => {
    cleanup?.()
    cleanup = null
    vi.useRealTimers()
    ;(globalThis as any).WebSocket = originalWebSocket
    vi.unstubAllEnvs()
  })

  it('sets wsConnected true when the socket opens', () => {
    expect(useAppStore.getState().wsConnected).toBe(false)

    cleanup = connectWebSocket()
    const fake = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    fake.triggerOpen()

    expect(useAppStore.getState().wsConnected).toBe(true)
    const connectedUrl = FakeWebSocket.urls[FakeWebSocket.urls.length - 1]
    expect(connectedUrl).not.toContain('token=')
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

  it('retries when the backend handshake is unavailable', () => {
    vi.useFakeTimers()
    cleanup = connectWebSocket()
    FakeWebSocket.instances[FakeWebSocket.instances.length - 1].rejectAuthorization()
    const count = FakeWebSocket.instances.length
    vi.advanceTimersByTime(3000)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    const reconnected = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    cleanup?.()
    expect(reconnected.readyState).toBe(FakeWebSocket.CLOSED)
    vi.advanceTimersByTime(6000)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    vi.useRealTimers()
  })

  it('stops retrying after an explicit backend policy rejection', () => {
    vi.useFakeTimers()
    cleanup = connectWebSocket()
    const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    socket.onclose?.({ code: 4403 })
    const count = FakeWebSocket.instances.length
    vi.advanceTimersByTime(6000)
    expect(FakeWebSocket.instances.length).toBe(count)
  })

  it('uses the local on-prem WebSocket without a token query when no token exists', () => {

    useAppStore.getState().reset()
    cleanup = connectWebSocket()

    const connectedUrl = FakeWebSocket.urls[FakeWebSocket.urls.length - 1]
    expect(connectedUrl).toContain('ws://127.0.0.1:8000/ws')
    expect(connectedUrl).toContain('sessionId=test-session')
    expect(connectedUrl).not.toContain('token=')
  })
})
