import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { apiBaseUrl } from '../../services/runtimeConfig'
import { sessionHeaders } from '../../services/browserSession'
import { parseCityObservation, type CityObservation } from '../services/observability'

function useMonitorState() {
  const [mode, setMode] = useState<'demand' | 'monitor'>('demand')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pathId, setPathId] = useState<string | null>(null)
  const [showHealth, setShowHealth] = useState(true), [showPaths, setShowPaths] = useState(true)
  const [snapshot, setSnapshot] = useState<CityObservation | null>(null)
  const [error, setError] = useState(''), [now, setNow] = useState(Date.now)
  useEffect(() => {
    let stopped = false, pending = false, request: AbortController | null = null
    async function poll() {
      if (document.hidden || pending || stopped) return
      pending = true
      const controller = new AbortController(); request = controller
      const timeout = setTimeout(() => controller.abort(), 25000)
      try {
        const response = await fetch(`${apiBaseUrl()}/api/onprem/city-observability`, { signal: controller.signal, headers: sessionHeaders(), cache: 'no-store' })
        if (!response.ok) throw new Error(`城市監看暫不可用（HTTP ${response.status}）`)
        const body = await response.text()
        if (body.length > 1024 * 1024) throw new Error('監看資料超過顯示上限')
        const data = parseCityObservation(JSON.parse(body))
        if (!stopped) { setSnapshot(data); setPathId(id => id && data.paths.some(p => p.id === id) ? id : null); setError(''); setNow(Date.now()) }
      } catch (e) {
        if (!stopped && !document.hidden) setError(e instanceof Error ? e.message : '城市監看暫不可用')
      } finally { clearTimeout(timeout); pending = false }
    }
    const visibility = () => { setNow(Date.now()); if (document.hidden) request?.abort(); else void poll() }
    const timer = setInterval(() => { if (!document.hidden) { setNow(Date.now()); void poll() } }, 5000)
    const expiry = setInterval(() => { if (!document.hidden) setNow(Date.now()) }, 1000)
    document.addEventListener('visibilitychange', visibility)
    void poll()
    return () => { stopped = true; request?.abort(); clearInterval(timer); clearInterval(expiry); document.removeEventListener('visibilitychange', visibility) }
  }, [])
  return { mode, setMode, selectedId, setSelectedId, pathId, setPathId, showHealth, setShowHealth, showPaths, setShowPaths, snapshot, error, now }
}
const Context = createContext<ReturnType<typeof useMonitorState> | null>(null)
export function CityMonitorProvider({ children }: { children: ReactNode }) {
  return <Context.Provider value={useMonitorState()}>{children}</Context.Provider>
}
export const useCityMonitor = () => useContext(Context)
