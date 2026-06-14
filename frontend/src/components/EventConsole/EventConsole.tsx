import { useState } from 'react'
import { useAppStore } from '../../store/appStore'
import { triggerCityEvent, resetSimulation } from '../../services/api'
import { EVENT_FLOWS } from '../CityMap/cityData'
import type { CityEventType } from '../../types'

interface EventConfig {
  type: CityEventType
  label: string
  icon: string
  description: string
  slice: string
  color: string
}

const EVENTS: EventConfig[] = [
  {
    type: 'concert',
    label: 'AR Concert',
    icon: '🎤',
    description: 'World stadium 80k attendees — 4K stream + AR interaction',
    slice: 'eMBB (SST=1)',
    color: 'border-blue-600 hover:bg-blue-900/30',
  },
  {
    type: 'typhoon',
    label: 'Typhoon',
    icon: '🌀',
    description: 'Category 5 — 1.2M affected, emergency IoT monitoring',
    slice: 'mMTC + URLLC',
    color: 'border-red-700 hover:bg-red-900/30',
  },
  {
    type: 'accident',
    label: 'Traffic Accident',
    icon: '🚗',
    description: 'Highway 5km congestion — V2X rerouting',
    slice: 'V2X (SST=4)',
    color: 'border-orange-600 hover:bg-orange-900/30',
  },
  {
    type: 'medical',
    label: 'ER Surge',
    icon: '🏥',
    description: 'ER utilization 95% — URLLC medical priority',
    slice: 'URLLC (SST=2)',
    color: 'border-red-600 hover:bg-red-900/30',
  },
  {
    type: 'iot_surge',
    label: 'IoT Surge',
    icon: '📡',
    description: 'Massive sensor registration — AMF signaling storm',
    slice: 'mMTC (SST=3)',
    color: 'border-green-700 hover:bg-green-900/30',
  },
]

export function EventConsole() {
  const { activeEvent, isSimulating, setActiveEvent, setSimulating, setPacketFlows, reset, appendAgentLog } = useAppStore()
  const [loading, setLoading] = useState<CityEventType | null>(null)

  async function handleTrigger(ev: EventConfig) {
    if (isSimulating) return
    setLoading(ev.type)
    setActiveEvent(ev.type)
    setSimulating(true)
    setPacketFlows(EVENT_FLOWS[ev.type] ?? [])
    appendAgentLog(`[${new Date().toLocaleTimeString()}] Triggering: ${ev.label}`)

    try {
      const { executionId } = await triggerCityEvent(ev.type)
      appendAgentLog(`[Event Engine] Execution started: ${executionId}`)
    } catch (err) {
      appendAgentLog(`[Error] ${String(err)}`)
    } finally {
      setLoading(null)
    }
  }

  async function handleReset() {
    try {
      await resetSimulation()
    } catch {
      // best-effort
    }
    reset()
  }

  return (
    <div className="panel shrink-0">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-bold text-slate-300 tracking-wider uppercase">
          City Event Console
        </h2>
        {isSimulating && (
          <button onClick={handleReset} className="btn text-xs bg-slate-700 hover:bg-slate-600">
            Reset
          </button>
        )}
      </div>

      <div className="flex gap-2 flex-wrap">
        {EVENTS.map((ev) => (
          <button
            key={ev.type}
            onClick={() => handleTrigger(ev)}
            disabled={isSimulating}
            className={`
              group relative flex items-center gap-2 px-3 py-2 rounded-lg border
              text-sm font-semibold transition-all duration-200 disabled:opacity-40
              disabled:cursor-not-allowed active:scale-95
              ${activeEvent === ev.type ? 'ring-2 ring-white/20' : ''}
              ${ev.color}
            `}
          >
            {loading === ev.type ? (
              <span className="animate-spin text-base">⟳</span>
            ) : (
              <span className="text-base">{ev.icon}</span>
            )}
            <span>{ev.label}</span>
            {/* Tooltip */}
            <div className="absolute bottom-full left-0 mb-2 w-48 bg-slate-900 border border-slate-700 rounded p-2 text-xs text-slate-300 opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-10">
              <p className="font-bold mb-0.5">{ev.slice}</p>
              <p className="text-slate-400">{ev.description}</p>
            </div>
          </button>
        ))}
      </div>

      {activeEvent && (
        <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
          <span className="w-2 h-2 rounded-full bg-sky-500 animate-pulse" />
          Simulation active — {EVENTS.find((e) => e.type === activeEvent)?.label}
        </div>
      )}
    </div>
  )
}
