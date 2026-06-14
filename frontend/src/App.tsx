import { useEffect } from 'react'
import { CanvasCityMap } from './components/CityMap/CanvasCityMap'
import { AgentPanel } from './components/AgentPanel/AgentPanel'
import { SliceDashboard } from './components/Dashboard/SliceDashboard'
import { EventConsole } from './components/EventConsole/EventConsole'
import { getFree5gcStatus } from './services/api'
import { connectWebSocket } from './services/websocket'
import { useAppStore } from './store/appStore'

export default function App() {
  useEffect(() => {
    const cleanup = connectWebSocket()
    return cleanup
  }, [])

  useEffect(() => {
    let stopped = false
    const store = useAppStore.getState()

    async function syncFree5gcStatus() {
      try {
        const status = await getFree5gcStatus()
        if (!stopped) store.setFree5gcStatus(status)
      } catch (err) {
        if (!stopped) store.appendAgentLog(`[free5GC] status sync failed: ${String(err)}`)
      }
    }

    syncFree5gcStatus()
    const timer = window.setInterval(syncFree5gcStatus, 5000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [])

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-city-bg text-slate-100">
      {/* Header */}
      <header className="shrink-0 border-b border-city-border px-6 py-2 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-sky-400 font-bold text-lg tracking-tight">5GCityVerse</span>
          <span className="text-slate-600 text-sm">AI-Native B5G Smart City Simulator</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          <span>free5GC v4.x</span>
          <span className="w-px h-3 bg-slate-700" />
          <span>AWS Bedrock Claude Sonnet 4</span>
          <span className="w-px h-3 bg-slate-700" />
          <span>EKS</span>
        </div>
      </header>

      {/* Main 3-column layout */}
      <main className="flex-1 flex gap-3 p-3 overflow-hidden min-h-0">
        {/* Left: City Map */}
        <div className="flex-1 min-w-0">
          <CanvasCityMap />
        </div>

        {/* Center: Agent Panel */}
        <div className="w-72 shrink-0">
          <AgentPanel />
        </div>

        {/* Right: Dashboard */}
        <div className="w-56 shrink-0">
          <SliceDashboard />
        </div>
      </main>

      {/* Bottom: Event Console */}
      <div className="shrink-0 px-3 pb-3">
        <EventConsole />
      </div>
    </div>
  )
}
