import { useEffect, useMemo, useState } from 'react'
import { useAppStore } from '../../store/appStore'
import { acknowledgeTrafficRendered, triggerCityEvents, resetSimulation, getSimulationStatus, waitForResetCompletion } from '../../services/api'
import type { ResetJobStatus } from '../../services/api'
import { SLICE_COLOR } from '../CityMap/cityData'
import { useStickToBottom } from '../../hooks/useStickToBottom'
import type { AgentDecision, CityEventType, Free5gcStatus, PacketFlow, RuntimePrimeStatus, ScenarioTriggerConfig, SliceType } from '../../types'
import { useLocale } from '../../i18n'

interface EventConfig {
  type: CityEventType
  label: string
  icon: string
  description: string
  slice: string
  whyZh: string
  whyEn: string
  alsoZh?: string
  alsoEn?: string
  scaleLabel: string
  defaultScale: number
  maxScale: number
  durationSeconds: number
}

const CITY_RESIDENT_DEFAULT = 180_000
const CITY_RESIDENT_MAX = 1_500_000
const FIRST_STATUS_POLL_MS = 500
const STATUS_POLL_MS = 5_000
export const STATUS_POLL_GRACE_MS = 120_000
export const EVENT_SCALE_STEP = 1

// The brighter topology colours are useful as fills but do not meet WCAG
// contrast when rendered as 10px text on white event cards.
const SLICE_TEXT_COLOR: Record<SliceType, string> = {
  eMBB: '#1d4ed8',
  URLLC: '#b91c1c',
  mMTC: '#15803d',
  V2X: '#9a3412',
}

export const SERVICE_NEEDS: ServiceNeed[] = [
  { type: 'eMBB', titleZh: 'eMBB 高速寬頻', titleEn: 'eMBB high-capacity broadband', needZh: '一次要搬很多資料，重點是速度與容量。', needEn: 'Moves lots of data at once; speed and capacity matter most.', exampleZh: '高畫質影音、AR、一般上網', exampleEn: 'HD video, AR, everyday internet' },
  { type: 'URLLC', titleZh: 'URLLC 低延遲高可靠', titleEn: 'URLLC fast and reliable', needZh: '不能等，也不能漏掉重要訊息。', needEn: 'Important messages cannot be late or easily lost.', exampleZh: '關鍵告警、控制、部分遠距醫療', exampleEn: 'Critical alerts, control, some telemedicine' },
  { type: 'mMTC', titleZh: 'mMTC／MIoT 大量物聯網', titleEn: 'mMTC / MIoT massive IoT', needZh: '同時連很多小裝置；每台傳得少，但數量很大。', needEn: 'Connects many small devices; each sends little data, but there are lots of them.', exampleZh: '水表、環境感測器、智慧城市回報', exampleEn: 'Meters, sensors, smart-city reports' },
  { type: 'V2X', titleZh: 'V2X 車聯網', titleEn: 'V2X connected mobility', needZh: '讓車輛、道路設施與用路人交換路況和安全資訊。', needEn: 'Lets vehicles, roads, and road users exchange traffic and safety information.', exampleZh: '事故警示、協同換道、即時改道', exampleEn: 'Crash alerts, coordinated driving, rerouting' },
]

export const EVENTS: EventConfig[] = [
  { type: 'concert', label: 'AR Concert', icon: '🎤', description: '4K stream + AR interaction', slice: 'eMBB', whyZh: '大量觀眾同時看高畫質直播，主要需要較高容量。', whyEn: 'Many viewers stream high-quality video at once, so capacity is the main need.', alsoZh: 'AR 即時互動也可能需要低延遲服務。', alsoEn: 'Real-time AR interaction may also need low latency.', scaleLabel: 'Attendees', defaultScale: 80_000, maxScale: 120_000, durationSeconds: 180 },
  { type: 'typhoon', label: 'Typhoon', icon: '🌀', description: 'low-latency emergency telemetry', slice: 'URLLC', whyZh: '本模擬聚焦緊急遙測：告警要快速、可靠送達。', whyEn: 'This simulation focuses on emergency telemetry: alerts must arrive quickly and reliably.', alsoZh: '同一場颱風也可能有 mMTC 感測器與 eMBB 救災影像。', alsoEn: 'The same typhoon may also include mMTC sensors and eMBB rescue video.', scaleLabel: 'Affected people', defaultScale: 1_200_000, maxScale: 1_500_000, durationSeconds: 180 },
  { type: 'accident', label: 'Traffic Accident', icon: '🚗', description: 'V2X rerouting', slice: 'V2X', whyZh: '車輛與道路設施要快速交換事故警示與改道路況。', whyEn: 'Vehicles and roadside systems must quickly exchange crash alerts and rerouting information.', alsoZh: '影像上傳或安全關鍵訊息可能另需高速寬頻或更嚴格可靠度。', alsoEn: 'Video uploads or safety-critical messages may need broadband or stricter reliability too.', scaleLabel: 'Impacted vehicles', defaultScale: 1_800, maxScale: 5_000, durationSeconds: 180 },
  { type: 'medical', label: 'ER Surge', icon: '🏥', description: 'URLLC medical priority', slice: 'URLLC', whyZh: '本模擬聚焦生命徵象與重要告警，不能延遲或中斷。', whyEn: 'This simulation focuses on vital signs and critical alerts that cannot be delayed or interrupted.', alsoZh: '急診也可能同時有 eMBB 醫療影像與 mMTC 床邊設備。', alsoEn: 'An ER may also carry eMBB medical images and mMTC bedside-device traffic.', scaleLabel: 'Patients/devices', defaultScale: 650, maxScale: 2_000, durationSeconds: 180 },
  { type: 'iot_surge', label: 'IoT Surge', icon: '📡', description: 'many sensors connecting at once', slice: 'mMTC / MIoT', whyZh: '大量感測器同時上線，每台只傳少量資料。', whyEn: 'Many sensors connect at once, while each sends only a small amount of data.', alsoZh: '少數緊急告警仍可能需要低延遲、高可靠服務。', alsoEn: 'A few urgent alarms may still need low-latency, reliable service.', scaleLabel: 'Sensors', defaultScale: 50_000, maxScale: 100_000, durationSeconds: 180 },
]

const EVENT_BY_TYPE = new Map(EVENTS.map((event) => [event.type, event]))

export function EventConsole() {
  const { text } = useLocale()
  const {
    activeScenarios,
    isSimulating,
    isReportGenerating,
    runtimeBusy,
    free5gcStatus,
    packetFlows,
    agentDecision,
    runtimePrime,
    orchestrationStage,
    sliceStrategy,
    setSliceStrategy,
    setSimulating,
    beginAgentRound,
    recordAgentDecision,
    setRoundReportReady,
    setRuntimePrime,
    setOrchestrationStage,
    setFree5gcStatus,
    addActiveScenario,
    syncActiveScenarioWindow,
    removeActiveScenario,
    pruneActiveScenarios,
    requestReport,
    reset,
    appendAgentLog,
  } = useAppStore()
  const [loading, setLoading] = useState(false)
  const [reportPending, setReportPending] = useState(false)
  const [isResetting, setIsResetting] = useState(false)
  const [resetProgress, setResetProgress] = useState<ResetJobStatus | null>(null)
  const [resetError, setResetError] = useState<string | null>(null)
  const [cityResidents, setCityResidents] = useState(CITY_RESIDENT_DEFAULT)
  const [includedByType, setIncludedByType] = useState<Record<CityEventType, boolean>>(
    Object.fromEntries(EVENTS.map((event) => [event.type, ['concert', 'typhoon', 'iot_surge'].includes(event.type)])) as Record<CityEventType, boolean>
  )
  const [eventScaleByType, setEventScaleByType] = useState<Record<CityEventType, number>>(
    Object.fromEntries(EVENTS.map((event) => [event.type, event.defaultScale])) as Record<CityEventType, number>
  )
  const [now, setNow] = useState(Date.now())

  const free5gcOffline = free5gcStatus?.connected === false
  const selectedConfigs = useMemo(() => {
    return EVENTS
      .filter((event) => includedByType[event.type])
      .map((event): ScenarioTriggerConfig => ({
        eventType: event.type,
        eventScale: clamp(eventScaleByType[event.type], 1, event.maxScale),
      }))
  }, [eventScaleByType, includedByType])
  const canSubmit = selectedConfigs.length > 0 && !runtimeBusy && !loading && !reportPending && !isResetting && !isSimulating && !isReportGenerating && !free5gcOffline
  const controlsLocked = reportPending || isReportGenerating || isSimulating || loading || isResetting

  useEffect(() => {
    const timer = window.setInterval(() => {
      const current = Date.now()
      setNow(current)
      pruneActiveScenarios(current)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [pruneActiveScenarios])

  async function handleTriggerAll() {
    if (!canSubmit) return
    const startedAt = Date.now()
    const cityResidentsValue = clamp(cityResidents, 1, CITY_RESIDENT_MAX)
    setLoading(true)
    setSimulating(true)

    try {
      const response = await triggerCityEvents({
        cityResidents: cityResidentsValue,
        sliceStrategy,
        scenarios: selectedConfigs,
      })
      // A new round starts only after the backend accepts the batch. This keeps
      // the previous result available when submission itself fails.
      beginAgentRound()
      appendAgentLog(text(`[介面] 已送出情境：${selectedConfigs.map((item) => localizedEventLabel(item.eventType, text)).join(' + ')}；市民=${cityResidentsValue.toLocaleString()}`, `[UI] Trigger requested: ${selectedConfigs.map((item) => localizedEventLabel(item.eventType, text)).join(' + ')}; residents=${cityResidentsValue.toLocaleString()}`))
      const events = response.events.length > 0
        ? response.events
        : response.executionIds.map((executionId, index) => ({
          executionId,
          eventType: selectedConfigs[index]?.eventType ?? 'concert',
          eventScale: selectedConfigs[index]?.eventScale ?? 1,
          eventDurationSeconds: EVENT_BY_TYPE.get(selectedConfigs[index]?.eventType ?? 'concert')?.durationSeconds ?? 180,
        }))
      events.forEach((item) => {
        const config = EVENT_BY_TYPE.get(item.eventType)
        addActiveScenario({
          type: item.eventType,
          label: localizedEventLabel(item.eventType, text),
          startedAt,
          endsAt: startedAt + Number(item.eventDurationSeconds || config?.durationSeconds || 180) * 1000,
          eventScale: item.eventScale,
          cityResidents: cityResidentsValue,
          executionId: item.executionId,
        })
      })
      appendAgentLog(text(`[事件引擎] 已接受：${events.map((item) => `${EVENT_BY_TYPE.get(item.eventType)?.label ?? item.eventType}(${shortExecutionId(item.executionId)})`).join(', ')}`, `[Event Engine] Batch accepted: ${events.map((item) => `${EVENT_BY_TYPE.get(item.eventType)?.label ?? item.eventType}(${shortExecutionId(item.executionId)})`).join(', ')}`))
      appendAgentLog(text('[AI 觀察器] 已上線並監看流量；在量測到事件流量前不啟動規劃。', '[AI Observer] Online and watching traffic; planning remains locked until event traffic is measured.'))
      await pollExecutionBatch(events.map((item) => ({ executionId: item.executionId, eventType: item.eventType })))
      setRoundReportReady(true)
      appendAgentLog(text('[報告] 全部情境的調度已進入終態，開始產生本輪最終報告。', '[Report] All scenario orchestrations are terminal; generating the final round report.'))
      setReportPending(true)
      requestReport()
      await delay(900)
    } catch (err) {
      appendAgentLog(`[Error] ${String(err)}`)
      selectedConfigs.forEach((item) => removeActiveScenario(item.eventType))
      setSimulating(false)
    } finally {
      setReportPending(false)
      setLoading(false)
    }
  }

  async function pollExecutionBatch(items: Array<{ executionId: string; eventType: CityEventType }>) {
    const uniqueItems = Array.from(new Map(items.map((item) => [item.executionId, item])).values())
    const terminal = new Set(['AGENT_COMPLETE', 'AGENT_DEGRADED', 'AGENT_BLOCKED', 'AGENT_FAILED', 'AGENT_CANCELLED', 'SIMULATION_COMPLETE', 'SIMULATION_DEGRADED', 'SIMULATION_BLOCKED'])
    const done = new Set<string>()
    const lastStatus = new Map<string, string>()
    const trafficRenderAcks = new Set<string>()
    const longestConfiguredWindowMs = Math.max(...items.map((item) => EVENT_BY_TYPE.get(item.eventType)?.durationSeconds ?? 180)) * 1000
    let pollDeadline = Date.now() + longestConfiguredWindowMs + STATUS_POLL_GRACE_MS
    let attempt = 0
    while (Date.now() < pollDeadline && done.size < uniqueItems.length) {
      await delay(attempt === 0 ? FIRST_STATUS_POLL_MS : STATUS_POLL_MS)
      await Promise.all(uniqueItems.map(async (item) => {
        if (done.has(item.executionId)) return
        try {
          const status = await getSimulationStatus(item.executionId) as EventStatus
          if (status.status && status.status !== lastStatus.get(item.executionId)) {
            appendAgentLog(`[Agent Runtime] ${EVENT_BY_TYPE.get(item.eventType)?.label ?? item.eventType}(${shortExecutionId(item.executionId)}) ${status.status}`)
            lastStatus.set(item.executionId, status.status)
          }
          if (status.free5gc) setFree5gcStatus(status.free5gc)
          const trafficStartedAt = Number(status.runtimePrime?.trafficStartedEpochMillis || 0)
          const trafficEndsAt = Number(status.runtimePrime?.trafficEndsEpochMillis || 0)
          if (trafficStartedAt > 0 && trafficEndsAt > trafficStartedAt) {
            syncActiveScenarioWindow(item.executionId, trafficStartedAt, trafficEndsAt)
            pollDeadline = extendPollingDeadline(pollDeadline, trafficEndsAt)
          }
          const needsRenderAck = status.awaitingTrafficRenderAck === true
            && status.runtimePrime?.observedBeforePlanning === true
            && hasMeasuredBearerEdges(status.free5gc)
            && !trafficRenderAcks.has(item.executionId)
          if (needsRenderAck) {
            // Zustand has received the measured snapshot. Two animation frames
            // ensure CanvasCityMap painted at least one solid-particle frame
            // before the backend is allowed to enter AI stage 1.
            await afterNextPaint()
            await acknowledgeTrafficRendered(item.executionId)
            trafficRenderAcks.add(item.executionId)
            setRuntimePrime({ ...status.runtimePrime!, awaitingTrafficRenderAck: false })
            setOrchestrationStage('traffic_rendered')
          } else {
            if (status.runtimePrime && !status.awaitingTrafficRenderAck) setRuntimePrime(status.runtimePrime)
            // setRuntimePrime derives a generic traffic stage. Apply the more
            // specific backend stage afterwards so "planned/action/validation"
            // is not overwritten by stale "traffic observed" UI state.
            if (status.progressStage) setOrchestrationStage(status.progressStage)
          }
          if (status.agentDecision) recordAgentDecision({
            executionId: item.executionId,
            eventType: item.eventType,
            status: status.status || 'AGENT_RUNNING',
            decision: status.agentDecision,
            updatedAt: Date.now(),
          })
          if (status.status && terminal.has(status.status)) {
            done.add(item.executionId)
            setOrchestrationStage(['AGENT_COMPLETE', 'AGENT_DEGRADED', 'SIMULATION_COMPLETE', 'SIMULATION_DEGRADED'].includes(status.status) ? 'complete' : 'blocked')
            items
              .filter((scenario) => scenario.executionId === item.executionId)
              .forEach((scenario) => removeActiveScenario(scenario.eventType))
          }
        } catch (err) {
          if (attempt >= 4) appendAgentLog(`[Event Engine] status poll failed: ${String(err)}`)
        }
      }))
      attempt += 1
    }
    appendAgentLog(done.size === uniqueItems.length ? '[Event Engine] The network round reached terminal state' : '[Event Engine] status polling timed out; keeping scenario windows active')
    setSimulating(false)
  }

  async function handleReset() {
    if (isResetting) return
    setIsResetting(true)
    setResetError(null)
    try {
      const queued = await resetSimulation()
      setResetProgress(queued)
      const completed = await waitForResetCompletion(queued, setResetProgress)
      setResetProgress(completed)
      reset()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setResetError(message)
      appendAgentLog(`[Reset] ${message}`)
    } finally {
      setIsResetting(false)
    }
  }

  return (
    <div className="panel flex min-h-[320px] flex-col overflow-visible">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">{text('城市事件控制台', 'City Event Console')}</h2>
        {(activeScenarios.length > 0 || isSimulating || isResetting) && (
          <button
            onClick={handleReset}
            disabled={isResetting}
            className="btn bg-slate-100 text-xs text-slate-700 hover:bg-slate-200 disabled:cursor-wait disabled:opacity-60"
          >
            {isResetting
              ? text(`清理中 ${resetProgress?.progressPercent ?? 0}%`, `Resetting ${resetProgress?.progressPercent ?? 0}%`)
              : text('重設', 'Reset')}
          </button>
        )}
      </div>

      {(isResetting || resetError) && (
        <div
          className={`mb-3 rounded-lg border px-3 py-2 text-xs ${resetError ? 'border-red-200 bg-red-50 text-red-700' : 'border-blue-200 bg-blue-50 text-blue-700'}`}
          role={resetError ? 'alert' : 'status'}
          aria-live="polite"
        >
          <div className="flex items-center justify-between gap-3 font-semibold">
            <span>{resetError || resetProgress?.message || text('正在排程清理', 'Queueing cleanup')}</span>
            {!resetError && <span>{resetProgress?.progressPercent ?? 0}%</span>}
          </div>
          {!resetError && (
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-blue-100" aria-hidden="true">
              <div className="h-full rounded-full bg-blue-500 transition-[width] duration-300" style={{ width: `${resetProgress?.progressPercent ?? 0}%` }} />
            </div>
          )}
        </div>
      )}

      <Novice5gGuide />

      <div className="min-w-0 space-y-4">
        <ScenarioStatus
          activeScenarios={activeScenarios}
          decision={agentDecision}
          runtimePrime={runtimePrime}
          orchestrationStage={orchestrationStage}
          isRunning={isSimulating}
          isResetting={isResetting}
          isReportGenerating={isReportGenerating}
          offline={free5gcOffline}
          now={now}
        />

        <section className="rounded-xl border border-slate-200 bg-slate-50/80 p-3 text-xs sm:p-4" aria-labelledby="scenario-settings-title">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p id="scenario-settings-title" className="text-sm font-bold text-slate-800">{text('選擇模擬事件', 'Choose simulation events')}</p>
              <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{text('可同時選擇多個事件，再調整每個事件的規模。', 'Select multiple events, then adjust the scale of each one.')}</p>
            </div>
            <span className="rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-[11px] font-bold text-blue-700" aria-live="polite">
              {text(`已選 ${selectedConfigs.length} 個事件`, `${selectedConfigs.length} events selected`)}
            </span>
          </div>

          <fieldset className="mt-4 rounded-xl border border-violet-200 bg-white p-3 sm:p-4" disabled={controlsLocked}>
            <legend className="px-1 text-sm font-bold text-slate-800">{text('選擇切片策略', 'Choose slicing strategy')}</legend>
            <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
              {text('策略會在情境送出後鎖定。本輪只有選擇 AI 動態切片，才會開放 AI 決策分頁。', 'The strategy locks when the scenario is submitted. Only AI dynamic slicing enables the AI Decisions tab for this round.')}
            </p>
            <div className="grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label={text('切片策略', 'Slicing strategy')}>
              {([
                ['none', text('無切片', 'No slicing'), text('所有服務共用同一容量池', 'All services share one capacity pool')],
                ['static', text('靜態切片', 'Static slicing'), text('使用固定的服務容量比例', 'Use fixed service capacity shares')],
                ['ai', text('AI 動態切片', 'AI dynamic slicing'), text('開放 AI 決策並依量測調整', 'Enable AI decisions based on measurements')],
              ] as const).map(([value, label, description]) => {
                const selected = sliceStrategy === value
                return (
                  <label key={value} className={`relative flex min-h-[92px] cursor-pointer flex-col rounded-lg border p-3 transition ${selected ? 'border-violet-500 bg-violet-50 ring-2 ring-violet-100' : 'border-slate-200 bg-white hover:border-violet-300'} ${controlsLocked ? 'cursor-not-allowed opacity-65' : ''}`}>
                    <span className="flex items-center justify-between gap-2">
                      <span className={`font-bold ${selected ? 'text-violet-800' : 'text-slate-700'}`}>{label}</span>
                      <input
                        type="radio"
                        name="slice-strategy"
                        value={value}
                        checked={selected}
                        onChange={() => setSliceStrategy(value)}
                        className="h-4 w-4 accent-violet-600"
                      />
                    </span>
                    <span className="mt-2 text-[10px] leading-4 text-slate-600">{description}</span>
                    {value === 'ai' && <span className="mt-auto pt-2 text-[10px] font-bold text-violet-700">{text('啟用 AI 決策分頁', 'Enables AI Decisions tab')}</span>}
                  </label>
                )
              })}
            </div>
            <p className={`mt-3 rounded-lg px-3 py-2 text-[11px] font-semibold ${sliceStrategy === 'ai' ? 'bg-violet-100 text-violet-800' : 'bg-slate-100 text-slate-600'}`} role="status">
              {sliceStrategy === 'ai'
                ? text('AI 決策分頁已開放；送出後可觀看推理與驗證過程。', 'AI Decisions is enabled; after submission you can inspect reasoning and verification.')
                : text('AI 決策分頁目前鎖定；此策略仍可執行情境並查看 5GC 儀表板。', 'AI Decisions is locked; this strategy can still run scenarios and use the 5GC Dashboard.')}
            </p>
          </fieldset>

          <label className="mt-4 grid gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-[1fr_180px] sm:items-center">
            <span>
              <span className="block text-xs font-bold text-slate-700">{text('固定市民數', 'City residents')}</span>
              <span className="mt-0.5 block text-[10px] text-slate-500">{text('所有事件共用的城市基準人口', 'Shared baseline population for all events')}</span>
            </span>
            <input
              type="number"
              name="cityResidents"
              aria-label="City residents"
              min={1}
              max={CITY_RESIDENT_MAX}
              step={1000}
              value={cityResidents}
              disabled={controlsLocked}
              onChange={(event) => setCityResidents(clamp(Number(event.target.value), 1, CITY_RESIDENT_MAX))}
              className="h-11 w-full rounded-lg border border-slate-300 bg-white px-3 text-right text-sm font-bold text-slate-800 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100 disabled:bg-slate-100 disabled:text-slate-400"
            />
          </label>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
                {EVENTS.map((event) => {
                  const value = clamp(eventScaleByType[event.type], 1, event.maxScale)
                  const included = includedByType[event.type]
                  return (
                    <article
                      key={event.type}
                      className={`relative min-h-[210px] rounded-xl border bg-white p-4 transition-all ${included ? 'border-blue-400 shadow-[0_8px_22px_rgba(37,99,235,0.10)] ring-2 ring-blue-100' : 'border-slate-200 hover:border-slate-300'}`}
                    >
                      <label className="flex cursor-pointer items-start justify-between gap-3">
                        <span className="flex min-w-0 items-start gap-3">
                          <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-lg text-xl ${included ? 'bg-blue-100' : 'bg-slate-100'}`} aria-hidden="true">{event.icon}</span>
                          <span className="min-w-0">
                            <span className="block text-sm font-bold text-slate-800">{localizedEventLabel(event.type, text)}</span>
                            <span className="mt-1 inline-flex rounded-full border px-2 py-0.5 text-[10px] font-bold" style={{ color: SLICE_TEXT_COLOR[event.type === 'iot_surge' ? 'mMTC' : event.slice as SliceType], borderColor: SLICE_TEXT_COLOR[event.type === 'iot_surge' ? 'mMTC' : event.slice as SliceType] }}>
                            {text('主要需求：', 'Primary need: ')}{event.slice}
                            </span>
                          </span>
                        </span>
                        <input
                          type="checkbox"
                          name={`include-${event.type}`}
                          aria-label={`Include ${event.label}`}
                          checked={included}
                          disabled={controlsLocked}
                          onChange={(change) => setIncludedByType((current) => ({ ...current, [event.type]: change.target.checked }))}
                          className="mt-1 h-5 w-5 shrink-0 cursor-pointer accent-blue-600 disabled:cursor-not-allowed"
                        />
                      </label>
                      <p className="mt-3 text-xs font-semibold leading-5 text-slate-700"><span className="text-blue-700">{text('為什麼：', 'Why: ')}</span>{text(event.whyZh, event.whyEn)}</p>
                      {(event.alsoZh || event.alsoEn) && <p className="mt-1 text-[10px] leading-relaxed text-slate-500">{text(event.alsoZh || '', event.alsoEn || '')}</p>}
                      <div className="mt-3 rounded-lg bg-slate-50 p-3">
                        <div className="flex items-center justify-between gap-3">
                          <label htmlFor={`scale-number-${event.type}`} className="text-[11px] font-bold text-slate-600">{localizedScaleLabel(event.type, text)}</label>
                          <input
                            id={`scale-number-${event.type}`}
                            type="number"
                            min={1}
                            max={event.maxScale}
                            step={EVENT_SCALE_STEP}
                            value={value}
                            disabled={!included || controlsLocked}
                            onChange={(change) => setEventScaleByType((current) => ({ ...current, [event.type]: clamp(Number(change.target.value), 1, event.maxScale) }))}
                            className="h-11 w-32 rounded-lg border border-slate-300 bg-white px-2 text-right text-xs font-bold text-slate-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 disabled:bg-slate-100 disabled:text-slate-400"
                          />
                        </div>
                        <label className="mt-2 block">
                          <span className="sr-only">{localizedScaleLabel(event.type, text)}</span>
                        <input
                          type="range"
                          name={`scale-${event.type}`}
                          aria-label={`${event.label} ${event.scaleLabel}`}
                          min={1}
                          max={event.maxScale}
                          step={EVENT_SCALE_STEP}
                          value={value}
                          disabled={!included || controlsLocked}
                          onChange={(change) => {
                            const next = clamp(Number(change.target.value), 1, event.maxScale)
                            setEventScaleByType((current) => ({ ...current, [event.type]: next }))
                          }}
                            className="h-6 w-full cursor-pointer accent-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
                        />
                        </label>
                        <div className="flex justify-between text-[10px] text-slate-500">
                          <span>1</span>
                          <span>{text('最大', 'Max')} {event.maxScale.toLocaleString()}</span>
                        </div>
                      </div>
                    </article>
                  )
                })}
          </div>

          <div className="sticky bottom-2 z-20 mt-4 rounded-xl border border-blue-200 bg-blue-50/95 p-3 shadow-lg backdrop-blur sm:p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs font-bold text-blue-900">{text('準備送出', 'Ready to submit')}</p>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {selectedConfigs.length === 0 ? (
                    <span className="text-[11px] text-slate-500">{text('請至少選擇一個事件', 'Select at least one event')}</span>
                  ) : selectedConfigs.map((item) => {
                    const event = EVENT_BY_TYPE.get(item.eventType)
                    return <span key={item.eventType} className="rounded-full border border-blue-200 bg-white px-2 py-1 text-[10px] font-bold text-blue-700">{event?.icon} {localizedEventLabel(item.eventType, text)} · {item.eventScale.toLocaleString()}</span>
                  })}
                </div>
              </div>
              <button
                type="button"
                onClick={() => void handleTriggerAll()}
                disabled={!canSubmit}
                className="min-h-11 w-full rounded-lg border border-blue-600 bg-blue-600 px-6 py-2.5 text-sm font-bold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto"
              >
                {reportPending || isReportGenerating ? text('產生報告中…', 'Generating report...') : loading ? text('送出中…', 'Submitting...') : isSimulating ? text('情境執行中…', 'Scenario running...') : text('送出情境', 'Submit Scenario')}
              </button>
            </div>
            {(isSimulating || reportPending || isReportGenerating) && (
                <p className="mt-2 text-[11px] text-blue-700" role="status">
                  {text('流量生成、AI 調度與驗證完成前暫時鎖定。', 'Locked until traffic generation, AI orchestration, and verification complete.')}
                </p>
            )}
          </div>
        </section>

        <div className="grid gap-3 sm:grid-cols-2">
          <ActiveBearerPanel flows={packetFlows} activeScenarioTypes={activeScenarios.map((scenario) => scenario.type)} />
          <ControlSignalingPanel flows={packetFlows} />
        </div>

        {free5gcOffline && activeScenarios.length === 0 && !isResetting && (
          <div className="text-xs text-red-600">free5GC offline - event controls locked</div>
        )}
      </div>
    </div>
  )
}

function Novice5gGuide() {
  const { text } = useLocale()
  const [open, setOpen] = useState(false)
  return (
    <section data-testid="novice-5g-guide" className="mb-3 overflow-hidden rounded-lg border border-blue-200 bg-blue-50/60 text-xs">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="novice-5g-guide-content"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left transition hover:bg-blue-100/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-white text-sm shadow-sm" aria-hidden="true">💡</span>
          <span className="min-w-0">
            <span className="block font-bold text-blue-900">{text('不知道該選哪個事件？', 'Not sure which event to choose?')}</span>
            <span className="block truncate text-[10px] text-slate-600">{text('展開查看四種 5G 服務需求的新手提示', 'Open the beginner guide to four 5G service needs')}</span>
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2 font-bold text-blue-700">
          {text(open ? '收合提示' : '展開提示', open ? 'Hide tips' : 'Show tips')}
          <span className={`text-base transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true">⌄</span>
        </span>
      </button>
      <div id="novice-5g-guide-content" hidden={!open} className="border-t border-blue-200 px-3 pb-3 pt-3">
        <p className="mb-2 text-[10px] text-slate-600">{text('服務需求描述流量最在意什麼，不是四份憑空多出的頻寬。', 'Service needs describe what traffic values most; they are not four extra pools of bandwidth.')}</p>
        <div className="grid gap-2 sm:grid-cols-2">
          {SERVICE_NEEDS.map((need) => (
            <article key={need.type} className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: SLICE_COLOR[need.type] }} />
                <p className="font-bold text-slate-800">{text(need.titleZh, need.titleEn)}</p>
              </div>
              <p className="mt-1 leading-relaxed text-slate-700">{text(need.needZh, need.needEn)}</p>
              <p className="mt-1 text-[10px] text-slate-500">{text('例如：', 'Examples: ')}{text(need.exampleZh, need.exampleEn)}</p>
            </article>
          ))}
        </div>
        <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 leading-relaxed text-amber-950">
          <p className="font-bold">{text('服務需求類別 ≠ Network Slice', 'Service need ≠ Network Slice')}</p>
          <p className="mt-0.5">{text('把網路想成同一條固定總寬的道路：需求類別像不同車種，Slice 像網路依需求規劃的車道。切片會調整網路功能、政策與資源權重，但不會把道路變寬。畫面百分比是本模擬的資源權重，總量永遠是 100%，不代表所有真實 5G 網路都固定這樣切。', 'Think of one road with a fixed total width: service needs are different vehicle types, while slices are lanes planned for those needs. Slicing changes network functions, policy, and resource weights; it does not widen the road. Percentages here are simulation resource weights that always total 100%, not a universal real-world split.')}</p>
        </div>
      </div>
    </section>
  )
}

export function selectSessionBearerFlows(flows: PacketFlow[], activeScenarioTypes: CityEventType[]): PacketFlow[] {
  const selected = new Set(activeScenarioTypes)
  return flows.filter((flow) => flow.active && flow.plane !== 'control' && (
    isBaselineFlow(flow.scenario) || selected.has(flow.scenario as CityEventType)
  ))
}

function ActiveBearerPanel({ flows, activeScenarioTypes }: { flows: PacketFlow[]; activeScenarioTypes: CityEventType[] }) {
  const { text } = useLocale()
  const activeFlows = selectSessionBearerFlows(flows, activeScenarioTypes)
  const activeScenarioCount = activeScenarioTypes.length
  const throughput = activeFlows.reduce((sum, flow) => sum + (flow.bandwidthMbps ?? 0), 0)
  const { containerRef, newCount, scrollToBottom } = useStickToBottom(activeFlows)

  return (
    <div className="min-h-[104px] rounded border border-slate-200 bg-slate-50/80 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-[11px] font-bold uppercase tracking-wide text-slate-500">{text('使用中的承載', 'Active Bearer')}</p>
        <span className="text-[11px] text-slate-500">{formatNumber(throughput)} Mbps</span>
      </div>
      {activeFlows.length === 0 ? (
        <div className="flex h-[62px] items-center rounded bg-white px-3 text-xs text-slate-500">
          {activeScenarioCount > 0 ? text('等待情境流量的 UE bearer 實測值。', 'Waiting for UE bearer metrics from live scenario traffic.') : text('目前沒有突發情境 bearer；free5GC 有連線時仍會保留市民基線流量。', 'No burst scenario bearer. Baseline traffic may still be present when free5GC reports active sessions.')}
        </div>
      ) : (
        <div className="relative">
          <div
            ref={containerRef}
            role="region"
            aria-label={text('使用中的承載清單', 'Active bearer list')}
            tabIndex={0}
            className="max-h-[118px] space-y-1 overflow-y-auto pr-1"
          >
            {activeFlows.map((flow) => {
              const baseline = isBaselineFlow(flow.scenario)
              return (
                <div key={flow.id} className="grid grid-cols-[52px_1fr] gap-2 rounded bg-white px-2 py-1.5 text-[11px]">
                  <span className="font-bold" style={{ color: SLICE_TEXT_COLOR[flow.sliceType] }}>{flow.sliceType}</span>
                  <span className="truncate text-slate-600" title={`${baseline ? text('日常流量', 'Baseline') : localizedEventLabel(flow.scenario as CityEventType, text)}: ${flow.sourceNodeId} -> ${flow.targetNodeId}`}>
                    {baseline && (
                      <span className="mr-1 rounded border border-green-200 bg-green-50 px-1 text-[9px] font-bold text-green-700 align-middle">
                        {text('日常流量', 'Baseline')}
                      </span>
                    )}
                    {!baseline && `${localizedEventLabel(flow.scenario as CityEventType, text)} / `}{flow.sourceNodeId} {'->'} {flow.targetNodeId} / {formatNumber(flow.bandwidthMbps ?? 0)} Mbps / {formatNumber(flow.latencyMs ?? 0)} ms
                  </span>
                </div>
              )
            })}
          </div>
          {newCount > 0 && (
            <button
              type="button"
              onClick={scrollToBottom}
              className="absolute bottom-1 left-1/2 -translate-x-1/2 rounded-full border border-blue-200 bg-blue-600 px-2 py-0.5 text-[10px] font-bold text-white shadow-md hover:bg-blue-700"
            >
              ↓ {newCount} {text('則新訊息', 'new')}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function ControlSignalingPanel({ flows }: { flows: PacketFlow[] }) {
  const { text } = useLocale()
  const signals = flows.filter((flow) => flow.active && flow.plane === 'control')
  const { containerRef, newCount, scrollToBottom } = useStickToBottom(signals)
  return (
    <div className="min-h-[104px] rounded border border-violet-200 bg-violet-50/50 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-[11px] font-bold uppercase tracking-wide text-violet-700">{text('控制面訊號', 'Control Signaling')}</p>
        <span className="text-[11px] text-violet-700">{signals.length} {text('筆近期紀錄', 'recent')}</span>
      </div>
      {signals.length === 0 ? (
        <div className="flex h-[62px] items-center rounded bg-white px-3 text-xs text-slate-500">
          {text('目前沒有近期的網路功能互動；圖上的 SBI 虛線仍保留，協助理解控制面拓樸。', 'No recent NF-to-NF transaction. Static SBI links remain visible for topology context.')}
        </div>
      ) : (
        <div className="relative">
          <div
            ref={containerRef}
            role="region"
            aria-label={text('控制面訊號清單', 'Control signaling list')}
            tabIndex={0}
            className="max-h-[118px] space-y-1 overflow-y-auto pr-1"
          >
            {signals.map((flow) => (
              <div key={flow.id} className="rounded bg-white px-2 py-1.5 text-[11px]">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-bold text-violet-700" title={flow.protocol}>{flow.protocol}</span>
                  <span className="shrink-0 text-slate-500">{flow.evidenceCount ?? 1} hits</span>
                </div>
                <p className="truncate text-slate-500" title={`${flow.sourceNodeId} -> ${flow.targetNodeId}`}>
                  {flow.sourceNodeId} {'->'} {flow.targetNodeId}
                </p>
              </div>
            ))}
          </div>
          {newCount > 0 && (
            <button
              type="button"
              onClick={scrollToBottom}
              className="absolute bottom-1 left-1/2 -translate-x-1/2 rounded-full border border-violet-200 bg-violet-600 px-2 py-0.5 text-[10px] font-bold text-white shadow-md hover:bg-violet-700"
            >
              ↓ {newCount} {text('則新訊息', 'new')}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function ScenarioStatus({
  activeScenarios,
  decision,
  runtimePrime,
  orchestrationStage,
  isRunning,
  isResetting,
  isReportGenerating,
  offline,
  now,
}: {
  activeScenarios: ReturnType<typeof useAppStore.getState>['activeScenarios']
  decision: AgentDecision | null
  runtimePrime: RuntimePrimeStatus | null
  orchestrationStage: string
  isRunning: boolean
  isResetting: boolean
  isReportGenerating: boolean
  offline: boolean
  now: number
}) {
  const { locale, text } = useLocale()
  const hasActive = activeScenarios.length > 0
  const stateText = isResetting
    ? text('重設中', 'Resetting')
    : isReportGenerating
      ? text('正在產生報告', 'Generating report')
      : isRunning
        ? stageLabel(orchestrationStage, locale)
        : offline
          ? text('free5GC 離線', 'free5GC offline')
          : text('待命', 'Ready')

  return (
    <div className={`rounded border px-3 py-2 ${hasActive || isRunning ? 'border-blue-200 bg-blue-50/70' : 'border-slate-200 bg-slate-50/80'}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[11px] font-bold uppercase tracking-wide text-slate-600">{text('目前情境', 'Current Scenario')}</p>
          <p className={`truncate text-sm font-bold ${hasActive ? 'text-blue-700' : 'text-slate-600'}`}>
            {hasActive ? activeScenarios.map((scenario) => localizedEventLabel(scenario.type, text)).join(' + ') : text('目前沒有突發情境', 'No burst scenario active')}
          </p>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600">
          {(isRunning || hasActive || isReportGenerating) && <span className="h-2 w-2 animate-pulse rounded-full bg-blue-600" />}
          {stateText}
        </span>
      </div>
      <p className="mt-1 text-xs text-slate-600">
        {hasActive ? stageDescription(orchestrationStage, runtimePrime, locale) : text('即使沒有突發情境，固定市民的日常流量仍會持續。', 'Baseline traffic can remain active without a burst scenario.')}
      </p>
      <ClosedLoopTrace stage={orchestrationStage} runtimePrime={runtimePrime} decision={decision} />
      {activeScenarios.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {activeScenarios.map((scenario) => (
            <span key={scenario.type} className="rounded border border-blue-200 bg-white px-2 py-0.5 text-[10px] text-blue-700">
              {scenario.label}: {scenarioRuntimeLabel({
                trafficObserved: runtimePrime?.observedBeforePlanning === true,
                endsAt: scenario.endsAt,
                now,
                isRunning,
                decisionReady: Boolean(decision),
              }, text)}
            </span>
          ))}
        </div>
      )}
      {decision?.selectedPlan?.name && (
        <p className="mt-1 truncate text-xs text-slate-600" title={decision.selectedPlan.name}>
          {text('方案：', 'Plan: ')}{decision.selectedPlan.name}
        </p>
      )}
    </div>
  )
}

interface EventStatus {
  status?: 'AGENT_QUEUED' | 'AGENT_RUNNING' | 'AGENT_COMPLETE' | 'AGENT_DEGRADED' | 'AGENT_BLOCKED' | 'AGENT_FAILED' | 'AGENT_CANCELLED' | 'SIMULATION_COMPLETE' | 'SIMULATION_DEGRADED' | 'SIMULATION_BLOCKED'
  error?: string
  detail?: string
  progressStage?: string
  runtimePrime?: RuntimePrimeStatus
  free5gc?: Free5gcStatus
  agentDecision?: AgentDecision
  awaitingTrafficRenderAck?: boolean
}

export function scenarioRuntimeLabel(
  state: { trafficObserved: boolean; endsAt: number; now: number; isRunning: boolean; decisionReady: boolean },
  text: (zh: string, en: string) => string,
): string {
  if (!state.trafficObserved) return text('流量準備中', 'preparing traffic')
  const remainingSeconds = Math.ceil((state.endsAt - state.now) / 1000)
  if (remainingSeconds > 0) return `${remainingSeconds}s`
  if (state.isRunning && !state.decisionReady) return text('AI 推理中 · 流量持續', 'AI reasoning · traffic continues')
  if (state.isRunning) return text('驗證中 · 流量持續', 'validating · traffic continues')
  return text('即將完成', 'finishing')
}

interface ServiceNeed {
  type: SliceType
  titleZh: string
  titleEn: string
  needZh: string
  needEn: string
  exampleZh: string
  exampleEn: string
}

function hasMeasuredBearerEdges(status: Free5gcStatus | undefined): boolean {
  return Boolean(status?.networkSnapshot?.edges?.some((edge) =>
    edge.active && edge.plane !== 'control' && Number(edge.throughputMbps) > 0
  ))
}

function afterNextPaint(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()))
  })
}

function ClosedLoopTrace({
  stage,
  runtimePrime,
  decision,
}: {
  stage: string
  runtimePrime: RuntimePrimeStatus | null
  decision: AgentDecision | null
}) {
  const { text } = useLocale()
  const trafficObserved = runtimePrime?.observedBeforePlanning === true
  const steps = [
    { key: 'traffic', label: text('流量', 'Traffic'), done: trafficObserved, active: stage === 'runtime_priming' },
    { key: 'observe', label: text('量測', 'Measure'), done: trafficObserved, active: stage === 'runtime_primed' || stage === 'traffic_observed' },
    { key: 'plan', label: text('規劃', 'Plan'), done: Boolean(decision?.selectedPlan), active: stage === 'planned' || stage === 'planning' },
    { key: 'execute', label: text('執行', 'Execute'), done: Boolean(decision?.actions?.some((action) => action.status === 'success')), active: stage === 'action' },
    { key: 'verify', label: text('驗證', 'Verify'), done: Boolean(decision?.verificationSummary), active: stage === 'complete' },
  ]
  return (
    <div className="mt-2 grid grid-cols-5 gap-1.5">
      {steps.map((step) => (
        <div
          key={step.key}
          className={`rounded border px-2 py-1 text-center text-[10px] font-bold ${
            step.done
              ? 'border-green-200 bg-green-50 text-green-700'
              : step.active
                ? 'border-blue-200 bg-blue-50 text-blue-700'
                : 'border-slate-200 bg-white text-slate-600'
          }`}
        >
          {step.label}
        </div>
      ))}
    </div>
  )
}

function stageLabel(stage: string, locale: 'zh-TW' | 'en'): string {
  const labels: Record<string, string> = {
    queued: 'Queued',
    runtime_priming: 'Sending traffic',
    runtime_primed: 'Traffic observed',
    traffic_observed: 'Traffic observed',
    traffic_rendered: 'Measured traffic rendered',
    planned: 'Planner running',
    planning: 'Planner running',
    action: 'Executor running',
    blocked: 'Blocked',
    traffic_not_observed: 'Traffic not observed',
  }
  const zh: Record<string, string> = { queued: '排隊中', runtime_priming: '正在送出流量', runtime_primed: '已觀測流量', traffic_observed: '已觀測流量', traffic_rendered: '實測流量已呈現', planned: 'AI 正在規劃', planning: 'AI 正在規劃', action: '正在執行調度', blocked: '已阻擋', traffic_not_observed: '尚未觀測到流量' }
  return locale === 'zh-TW' ? zh[stage] ?? '調度進行中' : labels[stage] ?? 'Orchestration running'
}

function stageDescription(stage: string, runtimePrime: RuntimePrimeStatus | null, locale: 'zh-TW' | 'en'): string {
  if (locale === 'zh-TW') {
    if (stage === 'runtime_priming') return '後端正在啟動 UERANSIM／iperf 流量；實測證據出現前，AI 不得選擇調度動作。'
    if (runtimePrime?.observedBeforePlanning) return `已量測到 ${(runtimePrime.observedScenarios || []).join('、') || runtimePrime.eventType} 流量，AI 現在才可依即時指標規劃。`
    if (stage === 'traffic_not_observed') return `仍缺少 ${(runtimePrime?.missingScenarios || []).join('、') || runtimePrime?.eventType || '該情境'} 的流量證據，因此 AI 規劃維持鎖定。`
    return '已送出的流量設定會持續到各情境時間結束。'
  }
  if (stage === 'runtime_priming') return 'UERANSIM/iperf traffic is being started before the planner is allowed to choose actions.'
  if (runtimePrime?.observedBeforePlanning) {
    return `Traffic evidence observed for ${(runtimePrime.observedScenarios || []).join(', ') || runtimePrime.eventType}; Agent planning can now use live metrics.`
  }
  if (stage === 'traffic_not_observed') {
    return `Planner is blocked because traffic evidence is missing for ${(runtimePrime?.missingScenarios || []).join(', ') || runtimePrime?.eventType || 'the scenario'}.`
  }
  return 'Submitted traffic configuration is active until each scenario window expires.'
}

function localizedScaleLabel(type: CityEventType, text: (zh: string, en: string) => string): string {
  const labels: Record<CityEventType, [string, string]> = { concert: ['參與人數', 'Attendees'], typhoon: ['受影響人數', 'Affected people'], accident: ['受影響車輛', 'Impacted vehicles'], medical: ['病患／裝置數', 'Patients/devices'], iot_surge: ['感測器數', 'Sensors'] }
  return text(...labels[type])
}

function localizedEventLabel(type: CityEventType, text: (zh: string, en: string) => string): string {
  const labels: Record<CityEventType, [string, string]> = { concert: ['AR 演唱會', 'AR Concert'], typhoon: ['颱風', 'Typhoon'], accident: ['交通事故', 'Traffic Accident'], medical: ['急診壅塞', 'ER Surge'], iot_surge: ['物聯網暴增', 'IoT Surge'] }
  return text(...labels[type])
}

// A decision can arrive before the 120-second traffic window ends. It must never
// short-circuit status polling, otherwise the UI declares the simulation finished
// and clears its running state while Kubernetes is still generating traffic.
export function shouldEarlyExitOnWsSignal(
  _itemsCount: number,
  _agentDecision: AgentDecision | null | undefined,
  _orchestrationStage: string
): boolean {
  return false
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function shortExecutionId(executionId: string): string {
  return executionId ? executionId.slice(0, 8) : 'unknown'
}

export function isBaselineFlow(scenario: string | undefined): boolean {
  return !scenario || !EVENT_BY_TYPE.has(scenario as CityEventType)
}

export function extendPollingDeadline(currentDeadline: number, trafficEndsEpochMillis: number): number {
  if (!Number.isFinite(trafficEndsEpochMillis) || trafficEndsEpochMillis <= 0) return currentDeadline
  return Math.max(currentDeadline, trafficEndsEpochMillis + STATUS_POLL_GRACE_MS)
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (Math.abs(value) >= 100) return value.toFixed(0)
  if (Math.abs(value) >= 10) return value.toFixed(1)
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(Math.max(Math.round(value), min), max)
}
