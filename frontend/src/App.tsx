import { useEffect, useState } from 'react'
import { CanvasCityMap } from './components/CityMap/CanvasCityMap'
import { AgentPanel } from './components/AgentPanel/AgentPanel'
import { SliceDashboard } from './components/Dashboard/SliceDashboard'
import { EventConsole } from './components/EventConsole/EventConsole'
import { getFree5gcStatus, getMetrics } from './services/api'
import { connectWebSocket } from './services/websocket'
import { useAppStore } from './store/appStore'
import type { SliceStrategy } from './store/appStore'
import { hasSelectedLocale, useLocale, type Locale } from './i18n'
import { LearningCenter } from './components/LearningCenter/LearningCenter'
import { AUTH_REQUIRED_EVENT, hasAccessToken, setAccessToken } from './services/auth'

export default function App() {
  const { locale, setLocale, text } = useLocale()
  const [languageChosen, setLanguageChosen] = useState(hasSelectedLocale)
  const [authorized, setAuthorized] = useState(hasAccessToken)
  const [learningOpen, setLearningOpen] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('events')
  const wsConnected = useAppStore((state) => state.wsConnected)
  const runtimeBusy = useAppStore((state) => state.runtimeBusy)
  const sliceStrategy = useAppStore((state) => state.sliceStrategy)
  const submittedSliceStrategy = useAppStore((state) => state.submittedSliceStrategy)
  const aiWorkspaceEnabled = isAiWorkspaceEnabled(sliceStrategy, submittedSliceStrategy)

  useEffect(() => {
    const requireAuthorization = () => setAuthorized(false)
    window.addEventListener(AUTH_REQUIRED_EVENT, requireAuthorization)
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, requireAuthorization)
  }, [])

  useEffect(() => {
    if (!aiWorkspaceEnabled && workspaceTab === 'decision') setWorkspaceTab('events')
  }, [aiWorkspaceEnabled, workspaceTab])

  useEffect(() => {
    if (!languageChosen || !authorized) return
    const cleanup = connectWebSocket()
    return cleanup
  }, [languageChosen, authorized])

  useEffect(() => {
    if (!languageChosen || !authorized) return
    let stopped = false
    let failureCount = 0
    const store = useAppStore.getState()

    async function syncFree5gcStatus() {
      try {
        const [status, metrics] = await Promise.all([getFree5gcStatus(), getMetrics()])
        failureCount = 0
        if (!stopped) {
          store.setFree5gcStatus(status)
          store.setRuntimeBusy(false)
          if (status.connected !== false) {
            store.updateMetrics(metrics)
          }
        }
      } catch (err) {
        if (String(err).includes('SESSION_BUSY')) store.setRuntimeBusy(true)
        failureCount += 1
        if (!stopped && failureCount >= 3) {
          store.appendAgentLog(`[free5GC] status sync degraded after ${failureCount} attempts: ${String(err)}`)
        }
      }
    }

    syncFree5gcStatus()
    const timer = window.setInterval(syncFree5gcStatus, wsConnected ? 60000 : 15000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [wsConnected, languageChosen, authorized])

  if (!languageChosen) {
    return <LanguageStart onChoose={(choice) => { setLocale(choice); setLanguageChosen(true) }} />
  }

  if (!authorized) {
    return <AccessGate locale={locale} onAuthorize={(token) => {
      const accepted = setAccessToken(token)
      if (accepted) setAuthorized(true)
      return accepted
    }} />
  }

  return (
    <div className="min-h-screen flex flex-col bg-city-bg text-slate-900">
      {/* Header */}
      <header className="border-b border-city-border bg-white/90 px-4 py-2 shadow-sm sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="text-blue-700 font-bold text-lg tracking-tight">5GCityVerse</span>
            <span className="text-slate-500 text-sm">{text('AI 原生 B5G 智慧城市模擬平台', 'AI-Native B5G Smart City Simulator')}</span>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>free5GC v4.x</span>
            <span className="w-px h-3 bg-slate-300" />
            <span>{text('AgentxG 規則式決策引擎', 'AgentxG rules-based decision engine')}</span>
            <span className="w-px h-3 bg-slate-300" />
            <span>EKS</span>
            <button type="button" onClick={() => setLearningOpen(true)} className="min-h-11 rounded-full border border-blue-200 bg-blue-50 px-3 py-2 font-bold text-blue-700 transition hover:bg-blue-600 hover:text-white">
              {text('認識 free5GC / 5GC NF', 'Learn free5GC / 5GC NFs')}
            </button>
            <div className="ml-1 flex rounded border border-slate-200 bg-slate-50 p-0.5" aria-label="Language / 語言">
              <button type="button" onClick={() => setLocale('zh-TW')} className={`min-h-11 min-w-11 rounded px-2 py-2 ${locale === 'zh-TW' ? 'bg-blue-600 text-white' : 'text-slate-600'}`}>中文</button>
              <button type="button" onClick={() => setLocale('en')} className={`min-h-11 min-w-11 rounded px-2 py-2 ${locale === 'en' ? 'bg-blue-600 text-white' : 'text-slate-600'}`}>EN</button>
            </div>
          </div>
        </div>
      </header>

      {runtimeBusy && (
        <div className="mx-3 mt-3 rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900" role="status">
          {text('另一個瀏覽器分頁正在使用共享 free5GC 核網。本分頁是獨立 session，不會載入對方的事件流量；請等對方完成後再開始。', 'Another browser tab is using the shared free5GC core. This independent session will not load that tab’s event traffic; wait until it finishes before starting.')}
        </div>
      )}
      <main className="grid flex-1 grid-cols-1 items-start gap-3 p-3 xl:grid-cols-[minmax(620px,1.08fr)_minmax(520px,0.92fr)]">
        <section className="min-w-0" aria-label={text('城市網路地圖', 'City network map')}>
          <CanvasCityMap />
        </section>

        <section className="min-w-0 overflow-hidden rounded-lg border border-city-border bg-white shadow-[0_18px_45px_rgba(15,23,42,0.08)]" aria-label={text('模擬控制與資訊', 'Simulation controls and information')}>
          <div className="overflow-hidden border-b border-slate-200 bg-slate-50/80" role="tablist" aria-label={text('模擬資訊分頁', 'Simulation information tabs')}>
            <div className="grid w-full grid-cols-4 px-1 pt-2 sm:px-2">
              {WORKSPACE_TABS.map((tab) => {
                const selected = workspaceTab === tab.id
                const disabled = tab.id === 'decision' && !aiWorkspaceEnabled
                return (
                  <button
                    key={tab.id}
                    id={`workspace-tab-${tab.id}`}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    aria-disabled={disabled}
                    aria-controls={`workspace-panel-${tab.id}`}
                    tabIndex={disabled ? -1 : selected ? 0 : -1}
                    disabled={disabled}
                    title={disabled ? text('請先在事件設定選擇 AI 動態切片', 'Choose AI dynamic slicing in Event Setup first') : undefined}
                    onClick={() => setWorkspaceTab(tab.id)}
                    className={`group relative flex min-w-0 items-center justify-center gap-1.5 rounded-t-lg border border-b-0 px-1.5 py-3 text-xs font-bold transition sm:px-2 xl:text-[13px] ${
                      selected
                        ? 'border-blue-200 bg-white text-blue-700 shadow-[0_-4px_14px_rgba(37,99,235,0.08)]'
                        : disabled
                          ? 'cursor-not-allowed border-transparent bg-slate-100/70 text-slate-300'
                          : 'border-transparent text-slate-500 hover:bg-white/70 hover:text-slate-800'
                    }`}
                  >
                    <span className={`grid h-6 w-6 place-items-center rounded-md text-xs ${selected ? 'bg-blue-100 text-blue-700' : 'bg-slate-200 text-slate-600 group-hover:bg-blue-50 group-hover:text-blue-700'}`} aria-hidden="true">
                      {tab.icon}
                    </span>
                    <span className="truncate">{text(tab.zh, tab.en)}</span>
                    {disabled && <span className="rounded bg-slate-200 px-1 py-0.5 text-[9px] text-slate-500" aria-hidden="true">🔒</span>}
                    <span className={`absolute inset-x-4 -bottom-px h-0.5 rounded-full bg-blue-600 transition ${selected ? 'opacity-100' : 'opacity-0'}`} />
                  </button>
                )
              })}
            </div>
          </div>

          <div className="min-h-[680px] bg-slate-50/35 p-3 sm:p-4">
            <WorkspacePanel id="events" activeTab={workspaceTab}><EventConsole /></WorkspacePanel>
            <WorkspacePanel id="decision" activeTab={workspaceTab}><AgentPanel /></WorkspacePanel>
            <WorkspacePanel id="dashboard" activeTab={workspaceTab}><SliceDashboard /></WorkspacePanel>
            <WorkspacePanel id="engineering" activeTab={workspaceTab}>
              <div className="space-y-3">
                <div className="panel">
                  <p className="text-xs font-bold uppercase tracking-[0.16em] text-blue-600">{text('工程模式', 'Engineering mode')}</p>
                  <h2 className="mt-1 text-lg font-bold text-slate-900">{text('系統與核心網觀測', 'System and core-network observability')}</h2>
                  <p className="mt-2 text-sm leading-6 text-slate-600">
                    {text('集中查看 free5GC、AgentxG 規則式決策引擎與 EKS 的狀態；更細部的事件執行紀錄可在「事件設定」分頁中檢視。', 'Monitor free5GC, the AgentxG rules-based decision engine, and EKS in one place. Detailed execution records remain available in the Event Setup tab.')}
                  </p>
                  <div className="mt-4 grid gap-3 sm:grid-cols-3">
                    {[
                      { name: 'free5GC', status: text('連線中', 'Connected') },
                      { name: text('AgentxG 規則式決策引擎', 'AgentxG rules-based decision engine'), status: text('已啟用', 'Enabled') },
                      { name: 'EKS', status: text('連線中', 'Connected') },
                    ].map((service) => (
                      <div key={service.name} className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-3">
                        <div className="flex items-center gap-2 text-xs font-bold text-emerald-700"><span className="h-2 w-2 rounded-full bg-emerald-500" />{service.status}</div>
                        <div className="mt-1 font-semibold text-slate-800">{service.name}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </WorkspacePanel>
          </div>
        </section>
      </main>
      {learningOpen && <LearningCenter onClose={() => setLearningOpen(false)} />}
    </div>
  )
}

type WorkspaceTab = 'events' | 'decision' | 'dashboard' | 'engineering'

export function isAiWorkspaceEnabled(strategy: SliceStrategy, submittedStrategy: SliceStrategy | null): boolean {
  return (submittedStrategy ?? strategy) === 'ai'
}

const WORKSPACE_TABS: Array<{ id: WorkspaceTab; zh: string; en: string; icon: string }> = [
  { id: 'events', zh: '事件設定', en: 'Event Setup', icon: 'EV' },
  { id: 'decision', zh: 'AI 決策', en: 'AI Decisions', icon: 'AI' },
  { id: 'dashboard', zh: '5GC 儀表板', en: '5GC Dashboard', icon: '5G' },
  { id: 'engineering', zh: '系統狀態', en: 'System Status', icon: '⚙' },
]

function WorkspacePanel({ id, activeTab, children }: { id: WorkspaceTab; activeTab: WorkspaceTab; children: React.ReactNode }) {
  const active = id === activeTab
  return (
    <div
      id={`workspace-panel-${id}`}
      role="tabpanel"
      aria-labelledby={`workspace-tab-${id}`}
      hidden={!active}
      tabIndex={0}
      className="outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
    >
      {children}
    </div>
  )
}

function LanguageStart({ onChoose }: { onChoose: (locale: Locale) => void }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-950 via-blue-950 to-slate-900 p-6 text-white">
      <section className="w-full max-w-2xl rounded-2xl border border-blue-300/30 bg-white/10 p-8 text-center shadow-2xl backdrop-blur">
        <p className="text-sm font-bold uppercase tracking-[0.3em] text-blue-300">5GCityVerse</p>
        <h1 className="mt-3 text-3xl font-bold">選擇操作語言 · Choose your language</h1>
        <p className="mt-3 text-sm text-slate-300">語言將套用到本分頁的介面、Agent 說明與模擬報告。<br />The selected language applies to this tab’s UI, Agent explanations, and simulation report.</p>
        <div className="mt-8 grid gap-4 sm:grid-cols-2">
          <button type="button" onClick={() => onChoose('zh-TW')} className="rounded-xl border border-blue-300 bg-blue-600 px-6 py-5 text-lg font-bold hover:bg-blue-500">繁體中文<span className="mt-1 block text-xs font-normal text-white">開始 5G Core 城市模擬</span></button>
          <button type="button" onClick={() => onChoose('en')} className="rounded-xl border border-slate-400 bg-slate-800 px-6 py-5 text-lg font-bold hover:bg-slate-700">English<span className="mt-1 block text-xs font-normal text-slate-300">Start the 5G Core city simulation</span></button>
        </div>
      </section>
    </main>
  )
}

function AccessGate({ locale, onAuthorize }: { locale: Locale; onAuthorize: (token: string) => boolean }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState(false)
  const zh = locale === 'zh-TW'
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 p-6 text-white">
      <form
        className="w-full max-w-md rounded-2xl border border-blue-300/30 bg-white/10 p-8 shadow-2xl"
        onSubmit={(event) => {
          event.preventDefault()
          const accepted = onAuthorize(token)
          setError(!accepted)
          if (accepted) setToken('')
        }}
      >
        <p className="text-sm font-bold uppercase tracking-[0.25em] text-blue-300">5GCityVerse</p>
        <h1 className="mt-3 text-2xl font-bold">{zh ? '輸入存取權杖' : 'Enter access token'}</h1>
        <p className="mt-2 text-sm text-slate-300">
          {zh ? '權杖只保留在此瀏覽器分頁，關閉分頁後即清除。' : 'The token stays only in this browser tab and is cleared when the tab closes.'}
        </p>
        <label className="mt-6 block text-sm font-bold" htmlFor="api-access-token">{zh ? '存取權杖' : 'Access token'}</label>
        <input
          id="api-access-token"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(event) => { setToken(event.target.value); if (error) setError(false) }}
          aria-invalid={error}
          aria-describedby={error ? 'api-access-token-error' : undefined}
          className="mt-2 min-h-11 w-full rounded-lg border border-slate-400 bg-white px-3 text-slate-950"
        />
        {error && <p id="api-access-token-error" role="alert" className="mt-2 text-sm font-semibold text-red-300">
          {zh ? '請輸入有效的存取權杖；尚未送出任何網路請求。' : 'Enter a valid access token. No network request was sent.'}
        </p>}
        <button type="submit" className="mt-5 min-h-11 w-full rounded-lg bg-blue-600 px-4 font-bold hover:bg-blue-500">
          {zh ? '進入模擬平台' : 'Open simulator'}
        </button>
      </form>
    </main>
  )
}
