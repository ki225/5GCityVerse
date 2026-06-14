import type { CityEventType } from '../types'
import type { Free5gcStatus } from '../types'

const BASE = import.meta.env.VITE_API_URL || '/api'

export async function triggerCityEvent(eventType: CityEventType): Promise<{ executionId: string }> {
  const res = await fetch(`${BASE}/events/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: eventType }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<{ executionId: string }>
}

export async function getSimulationStatus(executionId: string) {
  const res = await fetch(`${BASE}/events/status/${executionId}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getSliceStatus() {
  const res = await fetch(`${BASE}/network/slices`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getMetrics() {
  const res = await fetch(`${BASE}/metrics/current`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getFree5gcStatus(): Promise<Free5gcStatus> {
  const res = await fetch(`${BASE}/free5gc/status`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<Free5gcStatus>
}

export async function resetSimulation(): Promise<void> {
  await fetch(`${BASE}/events/reset`, { method: 'POST' })
}
