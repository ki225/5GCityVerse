import { useEffect, useRef, useState } from 'react'
import { useAppStore } from '../../store/appStore'
import type { AgentAction, AgentDecision, AgentVerification, NetworkMetrics, RuntimePrimeStatus, SliceStatus } from '../../types'
import { useLocale, type Locale } from '../../i18n'

export function AgentPanel() {
  const { locale, text } = useLocale()
  const {
    agentDecision,
    agentDecisionHistory,
    roundReportReady,
    agentLog,
    isSimulating,
    isReportGenerating,
    metrics,
    slices,
    activeEvent,
    runtimePrime,
    orchestrationStage,
    reportRequestId,
    setReportGenerating,
    appendAgentLog,
  } = useAppStore()
  const [reportSummary, setReportSummary] = useState('')
  const handledReportRequest = useRef(0)
  const trafficObservedForDecision = agentDecision?.intent?.runtimePrime?.observedBeforePlanning === true || runtimePrime?.observedBeforePlanning === true
  const visibleReportSummary = roundReportReady && trafficObservedForDecision
    ? reportSummary || (agentDecision ? buildResearchReport(agentDecision, metrics, slices, agentLog, activeEvent, runtimePrime, locale) : '')
    : ''
  const plainSummary = buildPlainSummary(agentDecision, locale)
  const successfulActions = agentDecision?.actions.filter((action) => action.status === 'success').length ?? 0
  const totalActions = agentDecision?.actions.length ?? 0
  const timelineStages = buildDecisionTimelineStages({
    runtimePrime,
    decision: agentDecision,
    orchestrationStage,
    isSimulating,
    locale,
  })

  useEffect(() => {
    if (reportRequestId <= handledReportRequest.current) return
    handledReportRequest.current = reportRequestId
    void handleGenerateReport()
  }, [reportRequestId])

  useEffect(() => {
    if (orchestrationStage === 'queued' || orchestrationStage === 'runtime_priming') setReportSummary('')
  }, [orchestrationStage])

  return (
    <div className="panel flex flex-col gap-3">
      <div className="flex shrink-0 items-center justify-between gap-2">
        <h2 className="text-sm font-bold text-slate-700 tracking-wider uppercase">
          {text('AI Agent 決策中心', 'AI Agent Decision Center')}
        </h2>
        <button
          type="button"
          onClick={() => void handleGenerateReport()}
          disabled={isReportGenerating || !roundReportReady}
          className="rounded border border-blue-200 bg-blue-50 px-2 py-1 text-[11px] font-bold text-blue-700 transition hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-40"
          title={roundReportReady ? text('依本輪完整 Agent 軌跡與網路狀態產生摘要', 'Generate a summary from the completed round') : text('需等待本輪所有調度完成', 'Wait for every orchestration in this round to finish')}
        >
          {isReportGenerating ? text('產生中…', 'Generating...') : text('產生報告', 'Generate Report')}
        </button>
      </div>

      <DecisionTimeline stages={timelineStages} />

      {/* Body flows naturally; the panel root is the scroll container (page also scrolls). */}
      <div className="space-y-3">
      <section className="rounded-lg border border-violet-200 bg-violet-50 p-2.5 text-xs">
        <p className="font-bold text-violet-800">{text('AI 理解到的情境需求', 'What AI understood')}</p>
        <p className="mt-1 font-medium leading-relaxed text-slate-700">{timelineStages[0].summary}</p>
        {timelineStages[0].details && <p className="mt-1 rounded border border-violet-100 bg-white/70 px-2 py-1 text-[9px] text-slate-500"><span className="font-bold text-slate-600">{text('技術依據：', 'Evidence: ')}</span>{timelineStages[0].details}</p>}
        {agentDecision?.selectedPlan && (
          <div className="mt-2 rounded border border-violet-200 bg-white px-2 py-1.5">
            <p><span className="font-bold text-violet-700">{text('AI 為什麼這樣調整：', 'Why AI chose this: ')}</span>{timelineStages[1].summary}</p>
            {timelineStages[1].details && <p className="mt-1 text-[9px] text-slate-500"><span className="font-bold text-slate-600">{text('技術依據：', 'Evidence: ')}</span>{timelineStages[1].details}</p>}
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-2.5 text-xs" aria-label={text('本輪調整範圍', 'Change scope for this round')}>
        <div className="flex items-center justify-between gap-2"><p className="font-bold text-slate-800">{text('本輪調整範圍', 'Change scope for this round')}</p><span className="text-[10px] text-slate-500">{successfulActions}/{totalActions} {text('已完成', 'completed')}</span></div>
        <p className="mt-1 text-[10px] leading-relaxed text-slate-500">{text('以下只列出後端實際提供的變更內容；網路總容量固定為 100%，切片只會重新分配既有資源。', 'Only changes reported by the backend are listed below. Total network capacity stays fixed at 100%; slicing only reallocates existing resources.')}</p>
        <div className="mt-2 grid grid-cols-2 gap-1.5">
          <DecisionFact label="NF / API" value={agentDecision?.actions.map((action) => action.api || action.description).slice(0, 2).join(' · ') || text('等待規劃', 'Awaiting plan')} tone="text-blue-700" />
          <DecisionFact label="Pods" value={agentDecision?.validationReport ? Object.entries(agentDecision.validationReport.k8s_scaling_observed).map(([nf, count]) => `${nf} ${count}`).join(' · ') || text('不變', 'unchanged') : text('尚未回報', 'not reported')} tone="text-slate-700" />
          <DecisionFact label="QoS" value={agentDecision?.intent ? `5QI ${agentDecision.intent.targetSlice.fiveQi}` : text('等待規劃', 'Awaiting plan')} tone="text-green-700" />
          <DecisionFact label={text('資源比例', 'Resource share')} value={text('總量固定 100%，比例依策略重分配', 'Fixed 100% total; shares are reallocated')} tone="text-violet-700" />
        </div>
      </section>
      {visibleReportSummary && (
        <div className="rounded border border-blue-200 bg-blue-50 p-3 text-xs text-slate-700">
          <div className="mb-1 flex items-center justify-between gap-2">
            <p className="font-bold uppercase tracking-wide text-blue-700">{text('報告', 'Report')}</p>
            <span className="rounded border border-blue-200 bg-white px-1 text-[9px] text-blue-700">
              {text('本輪完成', 'round complete')}
            </span>
          </div>
          {plainSummary && (
            <p className="mb-2 rounded bg-white px-2 py-1.5 font-bold text-slate-800">{plainSummary}</p>
          )}
          <p className="whitespace-pre-line leading-relaxed">{visibleReportSummary}</p>
        </div>
      )}
      </div>{/* end scrollable body */}
    </div>
  )

  async function handleGenerateReport() {
    if (isReportGenerating || !roundReportReady) return
    setReportGenerating(true)
    appendAgentLog('[Report] Generating 5G research summary')
    try {
      await delay(650)
      const reports = agentDecisionHistory.map((record, index) => {
        const heading = locale === 'zh-TW' ? `情境 ${index + 1}（${record.eventType}）` : `Scenario ${index + 1} (${record.eventType})`
        return `${heading}：${buildResearchReport(record.decision, metrics, slices, agentLog, record.eventType, undefined, locale)}`
      })
      setReportSummary(reports.length > 0 ? reports.join('\n\n') : buildResearchReport(agentDecision, metrics, slices, agentLog, activeEvent, undefined, locale))
      appendAgentLog('[Report] Summary generated')
    } finally {
      setReportGenerating(false)
    }
  }
}

export function buildPlainSummary(decision: AgentDecision | null | undefined, locale: Locale = 'zh-TW'): string {
  if (!decision) return ''
  if (locale === 'en') {
    const event = decision.intent?.eventType ?? 'network'
    const actions = decision.actions?.length ?? 0
    const status = decision.verificationSummary?.status ?? 'pending'
    return `AI observed the ${event} traffic event, executed ${actions} network orchestration action(s), and verification is ${status}.`
  }

  const eventName = decision.intent?.eventType
  const actionCount = decision.actions?.length ?? 0
  const status = decision.verificationSummary?.status
  const latencyCheck = decision.verification?.find((check) => check.metric === 'latencyMs')
  const throughputCheck = decision.verification?.find((check) => check.metric === 'throughputMbps')
  const baselineCheck = decision.verification?.find(isBaselinePreservationCheck)

  const clauses: string[] = []
  if (eventName) clauses.push(`AI 偵測到${eventName}事件`)
  if (actionCount > 0) clauses.push(`執行${actionCount}項網路調度`)
  if (status === 'passed') {
    clauses.push('驗證全部達標')
  } else if (status === 'degraded' || status === 'failed') {
    clauses.push('驗證部分未達標')
  }

  const verificationDetails: string[] = []
  if (latencyCheck && latencyCheck.before !== undefined && latencyCheck.target !== undefined) {
    verificationDetails.push(`延遲 ${latencyCheck.before} ms（門檻 ${latencyCheck.target}）`)
  }
  if (throughputCheck && throughputCheck.before !== undefined && throughputCheck.target !== undefined) {
    verificationDetails.push(`吞吐 ${throughputCheck.before} Mbps（門檻 ${throughputCheck.target}）`)
  }

  if (clauses.length === 0) return ''
  const detailSuffix = verificationDetails.length > 0 ? `：${verificationDetails.join('、')}` : ''
  const baselineSuffix = status === 'passed' && baselineCheck && baselineCheck.before !== undefined
    ? `，市民日常流量維持 ${baselineCheck.before} Mbps 未受影響`
    : ''
  return `${clauses.join('，')}${detailSuffix}${baselineSuffix}`
}

function buildResearchReport(
  decision: AgentDecision | null,
  metrics: NetworkMetrics | null,
  slices: SliceStatus[],
  logs: string[],
  activeEvent: string | null,
  runtimePrime?: ReturnType<typeof useAppStore.getState>['runtimePrime'],
  locale: Locale = 'zh-TW'
): string {
  const eventName = decision?.intent?.eventType ?? activeEvent ?? 'baseline observation'
  const targetSlice = decision?.intent?.targetSlice
  const busiestSlice = slices.length > 0
    ? slices.reduce((max, slice) => slice.load > max.load ? slice : max, slices[0])
    : undefined
  const successfulActions = decision?.actions?.filter((action) => action.status === 'success').map((action) => action.tool || action.type) ?? []
  const failedActions = decision?.actions?.filter((action) => action.status === 'failed').map((action) => action.tool || action.type) ?? []
  const observations = decision?.observations?.map((item) => `${item.label}: ${item.value}`).slice(0, 3) ?? []
  const validation = decision?.validationReport
  const runtimeEvidence = runtimePrime?.observedBeforePlanning
    ? `已先觀測到 ${runtimePrime.observedScenarios?.join('、') || runtimePrime.eventType} 的情境流量，再進入 planner。`
    : runtimePrime
      ? `尚未確認 pre-plan traffic evidence：${runtimePrime.missingScenarios?.join('、') || runtimePrime.eventType || runtimePrime.status}。`
      : ''
  const recentLogs = logs.slice(-4).join(' / ')
  const resourceLine = [
    `UPF CPU ${formatNumber(metrics?.upfCpuPercent ?? 0)}%`,
    `latency ${formatNumber(metrics?.latencyMs ?? 0)} ms`,
    `throughput ${formatNumber(metrics?.throughputMbps ?? 0)} Mbps`,
    `PDU sessions ${metrics?.pduSessionCount ?? 0}`,
    busiestSlice ? `highest slice load ${busiestSlice.type} ${formatNumber(busiestSlice.load)}%` : '',
  ].filter(Boolean).join(', ')

  if (locale === 'en') {
    return [
      `This ${eventName} event is a 5G Core resilience case centered on ${targetSlice ? `${targetSlice.name} (SST=${targetSlice.sst}, 5QI=${targetSlice.fiveQi})` : 'the available network slices'}.`,
      runtimePrime?.observedBeforePlanning
        ? `Scenario traffic (${runtimePrime.observedScenarios?.join(', ') || runtimePrime.eventType}) was generated and observed before AI planning began.`
        : `Pre-plan traffic evidence is not confirmed yet.`,
      `Measured resources: ${resourceLine}. Capacity conclusions use observed NF CPU, PDU sessions, GTP and throughput—not frontend animation.`,
      successfulActions.length ? `Completed control actions: ${unique(successfulActions).join(', ')}.` : `No confirmed control action has completed yet.`,
      failedActions.length ? `Failed or degraded actions: ${unique(failedActions).join(', ')}.` : `No blocking action failure was observed.`,
      validation ? `Verification phase ${validation.phase} completed ${validation.steps_completed}; throughput delta ${validation.sla_result.throughput_mbps.delta_from_baseline} Mbps and maximum isolation degradation ${validation.sla_result.isolation_check.max_degradation_percent}%.` : `A complete SLA validation report is not available yet.`,
    ].join(' ')
  }

  return [
    `本次 ${eventName} 事件可視為一個以 ${targetSlice ? `${targetSlice.name} slice (SST=${targetSlice.sst}, 5QI=${targetSlice.fiveQi})` : '目前可用 slice'} 為核心的 5G Core 韌性觀測案例。`,
    observations.length > 0 ? `Agent 先以現網量測建立判斷脈絡，重點觀察包含 ${observations.join('；')}。` : `Agent 主要依據即時 telemetry 與事件紀錄建立判斷脈絡。`,
    runtimeEvidence,
    `資源狀態顯示 ${resourceLine}，因此報告中的容量判斷以實測 NF CPU、PDU session 與 GTP/throughput 指標為準，而非前端情境動畫。`,
    successfulActions.length > 0 ? `控制流程已執行 ${unique(successfulActions).join('、')}，用來串接 subscriber/profile、QoS、traffic influence、UERANSIM 或 SLA 驗證等步驟。` : `目前尚未完成可確認的控制動作，建議先確認 Agent 執行鏈與 free5GC 連線狀態。`,
    failedActions.length > 0 ? `仍需注意 ${unique(failedActions).join('、')} 出現失敗或降級，這通常代表 WebUI/API profile refresh、NEF tool 或 runtime 驗證有局部不一致。` : `未觀察到阻斷性失敗，後續可著重比較事件前後的 slice isolation 與 SLA 收斂。`,
    validation ? `驗證結果顯示 phase ${validation.phase}、${validation.steps_completed}，throughput delta 為 ${validation.sla_result.throughput_mbps.delta_from_baseline} Mbps，隔離檢查最大退化 ${validation.sla_result.isolation_check.max_degradation_percent}%。` : `目前尚未取得完整 SLA validation report，可先用 console trace 追蹤 planner/executor/verification 三段流程。`,
    recentLogs ? `近期執行脈絡：${recentLogs}` : '',
  ].filter(Boolean).join('')
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)))
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (Math.abs(value) >= 100) return value.toFixed(0)
  if (Math.abs(value) >= 10) return value.toFixed(1)
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export function skippedReason(action: AgentAction): string {
  const result = action.result as { reason?: unknown } | null | undefined
  return typeof result?.reason === 'string' ? result.reason : ''
}

export function isBaselinePreservationCheck(check: { metric: string }): boolean {
  return check.metric === 'baselinePreservationMbps'
}

export function baselinePreservationTone(status: string): string {
  if (status === 'passed' || status === 'pass') return 'border-green-200 bg-green-50 text-green-700'
  if (status === 'failed' || status === 'fail') return 'border-red-200 bg-red-50 text-red-700'
  return 'border-slate-200 bg-slate-50 text-slate-500'
}

export function baselinePreservationText(check: AgentVerification): string {
  const passed = check.status === 'passed' || check.status === 'pass'
  const symbol = passed ? '✓' : ''
  return `實測 ${String(check.before)} Mbps ≥ 保障線 ${String(check.target)} Mbps ${symbol}`.trim()
}

function DecisionFact({
  label,
  value,
  tone = 'text-slate-700',
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div className="rounded bg-white border border-slate-200 px-2 py-1">
      <p className="text-[9px] uppercase tracking-wider text-slate-500">{label}</p>
      <p className={`truncate font-bold ${tone}`} title={value}>{value}</p>
    </div>
  )
}

export function selectPrimarySlice(slices: SliceStatus[]): SliceStatus | undefined {
  const stressedSlices = slices
    .filter((slice) => slice.load >= 70 || slice.trend === 'up')
    .sort((a, b) => b.load - a.load)
  return stressedSlices[0] ?? (
    slices.length > 0
      ? slices.reduce((max, slice) => slice.load > max.load ? slice : max, slices[0])
      : undefined
  )
}

export function buildDecisionReason(
  riskLevel: string,
  sliceType: string | undefined,
  sliceLoad: number,
  upfCpuPercent: number,
  noDominantPressure = false
): string {
  if (noDominantPressure && sliceType) {
    return `no dominant slice pressure (highest ${sliceType} at ${sliceLoad}%); preemptive orchestration due to ${riskLevel} risk`
  }
  const reasons: string[] = []
  if (sliceType) reasons.push(`${sliceType} is the dominant pressure point at ${sliceLoad}% load`)
  if (upfCpuPercent >= 70) reasons.push(`UPF CPU is elevated at ${upfCpuPercent}%`)
  if (riskLevel === 'critical') reasons.push('critical risk requires priority QoS and capacity protection')
  if (riskLevel === 'high') reasons.push('high risk requires preemptive orchestration')
  return reasons.length > 0 ? reasons.join('; ') : 'no pressure threshold is currently breached'
}

export type DecisionTimelineStageStatus = 'waiting' | 'active' | 'complete' | 'warning' | 'failed'

export interface DecisionTimelineStage {
  key: 'intent' | 'plan' | 'scope' | 'execute' | 'verify'
  title: string
  status: DecisionTimelineStageStatus
  summary: string
  details?: string
}

interface DecisionTimelineInput {
  runtimePrime: RuntimePrimeStatus | null
  decision: AgentDecision | null
  orchestrationStage: string
  isSimulating: boolean
  locale: Locale
}

function compactTimelineText(value: string, maxLength = 132): string {
  const compact = value.replace(/\s+/g, ' ').trim()
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact
}

function timelineScenarioNames(prime: RuntimePrimeStatus | null, decision: AgentDecision | null): string[] {
  const networkRound = decision as (AgentDecision & { networkRound?: { scenarios?: string[] } }) | null
  return Array.from(new Set([
    ...(prime?.observedScenarios ?? []),
    ...(networkRound?.networkRound?.scenarios ?? []),
    decision?.validationReport?.scenario,
    decision?.intent?.eventType,
  ].filter((value): value is string => Boolean(value && value !== 'network_round'))))
}

function plainScenarioNeed(scenarios: string[], targetSlice: string | undefined, zh: boolean): string {
  const value = scenarios.join(' ').toLowerCase()
  if (value.includes('concert')) return zh ? '現場有大量直播與互動需求，觀眾最在意的是畫面不要卡住、聲音不要中斷。' : 'A large live audience is streaming and interacting; viewers mainly need stable video and uninterrupted audio.'
  if (value.includes('medical')) return zh ? '醫療資料需要快速而穩定地送達，避免影像延遲或重要資訊中斷。' : 'Medical data must arrive quickly and reliably so images and critical information are not delayed.'
  if (value.includes('typhoon') || value.includes('disaster')) return zh ? '災害期間的重要聯絡與警報不能被一般流量擠掉，必須優先保持暢通。' : 'During a disaster, alerts and critical communications must stay available even when ordinary traffic surges.'
  if (value.includes('iot')) return zh ? '大量感測器正在同時回報，重點是讓告警準時送達，而且不要漏資料。' : 'Many sensors are reporting at once; alerts need to arrive on time without losing data.'
  if (value.includes('traffic') || value.includes('v2x') || value.includes('vehicle')) return zh ? '車輛與道路警示需要即時送達，延遲太久可能讓駕駛來不及反應。' : 'Vehicle and road warnings need to arrive immediately so drivers have time to react.'
  if (targetSlice === 'eMBB') return zh ? '目前主要是高流量服務需要更多空間，目標是讓直播、視訊與一般上網維持順暢。' : 'High-volume services need more room so streaming, video, and everyday internet use stay smooth.'
  if (targetSlice === 'URLLC') return zh ? '目前最重要的是縮短等待時間，讓關鍵訊息能更快送達。' : 'The priority is reducing delay so critical messages arrive faster.'
  if (targetSlice === 'mMTC') return zh ? '目前有大量裝置同時連線，重點是讓感測資料穩定送達。' : 'Many devices are connected at once, so sensor reports need to remain reliable.'
  if (targetSlice === 'V2X') return zh ? '目前需要優先保障道路與車輛之間的即時警示。' : 'Real-time road and vehicle warnings need priority protection.'
  return zh ? 'AI 正在判斷哪些服務最容易受到壅塞影響，以及應該先保護誰。' : 'AI is identifying which services are most affected by congestion and should be protected first.'
}

function plainPlanChoice(targetSlice: string | undefined, zh: boolean): string {
  if (targetSlice === 'eMBB') return zh ? 'AI 會把既有容量多分一些給直播與高流量服務，同時保留其他服務的基本資源；總頻寬不會增加。' : 'AI will give more of the existing capacity to streaming and high-volume services while protecting a baseline for others; total bandwidth does not increase.'
  if (targetSlice === 'URLLC') return zh ? 'AI 會優先讓怕延遲的關鍵訊息先通過，同時避免一般使用者被完全擠掉；總頻寬不會增加。' : 'AI will prioritize delay-sensitive critical messages without completely crowding out ordinary users; total bandwidth does not increase.'
  if (targetSlice === 'mMTC') return zh ? 'AI 會替大量感測器保留穩定的傳送空間，同時維持其他服務的基本體驗；總頻寬不會增加。' : 'AI will reserve stable room for many sensors while maintaining a baseline for other services; total bandwidth does not increase.'
  if (targetSlice === 'V2X') return zh ? 'AI 會優先保障車輛警示，並從既有容量中重新分配資源；總頻寬不會增加。' : 'AI will prioritize vehicle warnings by reallocating existing capacity; total bandwidth does not increase.'
  return zh ? 'AI 會重新分配既有容量，先照顧最迫切的服務，同時保留其他使用者的基本體驗。' : 'AI will reallocate existing capacity to the most urgent service while preserving a baseline experience for others.'
}

function plainVerificationResult(status: string, scenarios: string[], targetSlice: string | undefined, zh: boolean): string {
  if (['pending', 'waiting', 'running'].includes(status)) return zh ? '系統正在確認調整後是否真的改善使用體驗，現在還不能下結論。' : 'The system is checking whether the change actually improved user experience; it is too early to conclude.'
  if (status === 'passed') {
    const value = scenarios.join(' ').toLowerCase()
    if (value.includes('concert') || targetSlice === 'eMBB') return zh ? '調整後已達到直播需要的速度與反應時間，觀眾應能更穩定觀看，較不容易卡頓。' : 'The adjusted network now meets streaming speed and response-time needs, so viewers should see more stable video with less buffering.'
    if (value.includes('medical')) return zh ? '調整後的傳送速度與穩定度已達標，醫療影像與重要資料應能更可靠地送達。' : 'Speed and reliability now meet the target, so medical images and critical data should arrive more reliably.'
    if (value.includes('typhoon') || targetSlice === 'URLLC') return zh ? '重要訊息的傳送速度已達標，緊急聯絡與告警應能更快送達。' : 'Critical-message delivery now meets the target, so emergency communications and alerts should arrive faster.'
    if (value.includes('iot') || targetSlice === 'mMTC') return zh ? '大量感測器同時回報時仍符合需求，告警與資料應能更穩定送達。' : 'The network meets the target while many sensors report together, so alerts and data should arrive more reliably.'
    if (value.includes('traffic') || targetSlice === 'V2X') return zh ? '車輛警示的傳送已達標，駕駛應能更即時收到道路資訊。' : 'Vehicle-warning delivery now meets the target, so drivers should receive road information sooner.'
    return zh ? '調整後的網路表現已達到本次需求，使用者應能感受到連線更穩定、等待更少。' : 'The adjusted network meets this round’s needs, so users should experience a steadier connection and less waiting.'
  }
  if (status === 'failed' || status === 'degraded') return zh ? '調整後仍有需求未達標，使用者可能繼續遇到卡頓或延遲，系統應回滾或重新規劃。' : 'Some needs remain unmet, so users may still see buffering or delay; the system should roll back or replan.'
  return zh ? '目前證據不足，還不能確定使用體驗是否真的改善。' : 'There is not enough evidence yet to confirm that user experience improved.'
}

export function buildDecisionTimelineStages({ runtimePrime, decision, orchestrationStage, isSimulating, locale }: DecisionTimelineInput): DecisionTimelineStage[] {
  const zh = locale === 'zh-TW'
  const prime = runtimePrime ?? decision?.intent?.runtimePrime ?? null
  const scenarioNames = timelineScenarioNames(prime, decision)
  const targetSlice = decision?.intent?.targetSlice.name
  const observed = prime?.observedBeforePlanning === true
  const observedScenarios = prime?.observedScenarios?.filter(Boolean) ?? []
  const missingScenarios = prime?.missingScenarios?.filter(Boolean) ?? []
  const stage = orchestrationStage.toLowerCase()
  const blocked = ['blocked', 'traffic_not_observed', 'error', 'cancelled'].some((value) => stage.includes(value))
    || ['traffic_not_observed', 'error', 'cancelled'].includes(prime?.status ?? '')
  const actions = decision?.actions ?? []
  const successCount = actions.filter((action) => action.status === 'success').length
  const failedCount = actions.filter((action) => action.status === 'failed').length
  const skippedCount = actions.filter((action) => action.status === 'skipped').length
  const finishedCount = successCount + failedCount + skippedCount
  const runningAction = actions.find((action) => action.status === 'running') ?? actions.find((action) => action.status === 'pending')
  const nfApiScope = Array.from(new Set(actions.map((action) => action.api || action.tool || action.type).filter(Boolean))).join(zh ? '、' : ', ')
  const podScope = decision?.validationReport
    ? Object.entries(decision.validationReport.k8s_scaling_observed).map(([nf, count]) => `${nf} ${count}`).join(zh ? '、' : ', ')
    : ''
  const qosScope = decision?.intent ? `5QI ${decision.intent.targetSlice.fiveQi}` : ''

  let intent: DecisionTimelineStage
  if (decision?.intent) {
    intent = {
      key: 'intent',
      title: zh ? '理解需求' : 'Understand',
      status: 'complete',
      summary: plainScenarioNeed(scenarioNames, targetSlice, zh),
      details: zh
        ? `${scenarioNames.join('、') || decision.intent.eventType}；${targetSlice} 目標：延遲 ≤ ${decision.intent.sla.latencyMsMax} ms、吞吐 ≥ ${decision.intent.sla.minThroughputMbps} Mbps。`
        : `${scenarioNames.join(', ') || decision.intent.eventType}; ${targetSlice} target: latency ≤ ${decision.intent.sla.latencyMsMax} ms and throughput ≥ ${decision.intent.sla.minThroughputMbps} Mbps.`,
    }
  } else if (observed) {
    const scenarios = observedScenarios.join(zh ? '、' : ', ') || prime?.eventType || (zh ? '情境' : 'scenario')
    intent = {
      key: 'intent',
      title: zh ? '理解需求' : 'Understand',
      status: 'complete',
      summary: zh ? '已確認情境流量真的進入網路，AI 現在才開始判斷，不是預先猜測。' : 'Scenario traffic is now confirmed on the network, so AI can begin analysis from evidence rather than a guess.',
      details: zh ? `bearer／iperf 實測情境：${scenarios}。` : `Measured bearer/iperf scenario: ${scenarios}.`,
    }
  } else if (blocked) {
    const missing = missingScenarios.join(zh ? '、' : ', ') || prime?.eventType || (zh ? '情境' : 'scenario')
    intent = {
      key: 'intent',
      title: zh ? '理解需求' : 'Understand',
      status: 'failed',
      summary: zh ? '系統沒有看到情境流量真的出現，因此不讓 AI 猜測或擅自調整網路。' : 'The system did not see actual scenario traffic, so AI is blocked from guessing or changing the network.',
      details: zh ? `未觀測情境：${missing}。` : `Missing measured scenario: ${missing}.`,
    }
  } else if (prime?.status === 'running' || ['queued', 'runtime_priming', 'traffic_rendered'].includes(stage) || isSimulating) {
    intent = {
      key: 'intent',
      title: zh ? '理解需求' : 'Understand',
      status: 'active',
      summary: zh ? '正在確認情境流量是否真的出現在網路上；確認前 AI 不會開始決策。' : 'Checking whether scenario traffic is really present on the network; AI will not decide before it is confirmed.',
      details: zh ? '正在量測 bearer／iperf 流量。' : 'Measuring bearer/iperf traffic.',
    }
  } else {
    intent = { key: 'intent', title: zh ? '理解需求' : 'Understand', status: 'waiting', summary: zh ? '等待真實情境流量證據。' : 'Waiting for measured scenario traffic.' }
  }

  let plan: DecisionTimelineStage
  if (decision?.selectedPlan) {
    plan = {
      key: 'plan',
      title: zh ? '規劃切片' : 'Plan slice',
      status: 'complete',
      summary: plainPlanChoice(targetSlice, zh),
      details: compactTimelineText(`${decision.selectedPlan.name}：${decision.selectedPlan.rationale}`),
    }
  } else if (blocked) {
    plan = { key: 'plan', title: zh ? '規劃切片' : 'Plan slice', status: 'failed', summary: zh ? '因缺少可用流量證據，未產生切片方案。' : 'No slice plan was produced because usable traffic evidence is missing.' }
  } else if (observed || Boolean(decision?.intent) || stage.includes('plan')) {
    plan = { key: 'plan', title: zh ? '規劃切片' : 'Plan slice', status: 'active', summary: zh ? 'AI 正在比較不同調整方式，找出最能改善體驗、又不傷害其他使用者的方案。' : 'AI is comparing options to improve the affected experience without unnecessarily harming other users.', details: zh ? '比較候選策略、SLA 與固定 100% 容量的重分配方式。' : 'Comparing candidate strategies, SLA targets, and reallocation of fixed 100% capacity.' }
  } else {
    plan = { key: 'plan', title: zh ? '規劃切片' : 'Plan slice', status: 'waiting', summary: zh ? '等待需求理解完成。' : 'Waiting for intent analysis.' }
  }

  let scope: DecisionTimelineStage
  if (blocked && !decision?.selectedPlan) {
    scope = { key: 'scope', title: zh ? '確認影響範圍' : 'Confirm scope', status: 'warning', summary: zh ? '因為前面的流量證據不足，系統沒有準備任何網路變更。' : 'Because traffic evidence was insufficient, the system did not prepare any network change.', details: zh ? '前置流量證據不足；變更項目 0 項。' : 'Insufficient prerequisite traffic evidence; 0 changes prepared.' }
  } else if (!decision?.selectedPlan) {
    scope = { key: 'scope', title: zh ? '確認影響範圍' : 'Confirm scope', status: 'waiting', summary: zh ? '等待方案產生後，確認會動到哪些網路功能與資源。' : 'Waiting for a plan before identifying which network functions and resources would change.' }
  } else if (actions.length === 0) {
    scope = { key: 'scope', title: zh ? '確認影響範圍' : 'Confirm scope', status: 'active', summary: zh ? '方案已選好，正在等待後端列出實際會調整的功能與資源。' : 'A plan is selected; waiting for the backend to list the functions and resources that would actually change.', details: zh ? `QoS：${qosScope || '尚未回報'}；資源總量固定 100%。` : `QoS: ${qosScope || 'not reported'}; total capacity stays fixed at 100%.` }
  } else {
    scope = {
      key: 'scope',
      title: zh ? '確認影響範圍' : 'Confirm scope',
      status: 'complete',
      summary: zh ? '已確認這次會調整哪些網路功能與資源，接下來只會執行這份清單中的項目。' : 'The network functions and resources in scope are now identified; only the listed changes proceed to execution.',
      details: zh
        ? `變更 ${actions.length} 項；NF／API：${nfApiScope || '尚未回報'}；Pods：${podScope || '不變或尚未回報'}；QoS：${qosScope || '尚未回報'}；資源總量固定 100%。`
        : `${actions.length} changes; NF/API: ${nfApiScope || 'not reported'}; Pods: ${podScope || 'unchanged or not reported'}; QoS: ${qosScope || 'not reported'}; total capacity stays fixed at 100%.`,
    }
  }

  let execute: DecisionTimelineStage
  if (blocked && actions.length === 0) {
    execute = { key: 'execute', title: zh ? '執行變更' : 'Execute', status: 'warning', summary: zh ? '系統選擇不動網路，避免在看不清楚狀況時做出錯誤調整。' : 'The system left the network unchanged to avoid making a bad adjustment without enough evidence.', details: zh ? '前置流量證據未通過；執行動作 0 項。' : 'Prerequisite traffic evidence failed; 0 actions executed.' }
  } else if (actions.length === 0) {
    execute = {
      key: 'execute',
      title: zh ? '執行變更' : 'Execute',
      status: decision?.verificationSummary ? 'warning' : 'waiting',
      summary: decision?.verificationSummary
        ? (zh ? '系統已經開始檢查結果，但沒有資料能證明前面真的執行過網路調整。' : 'Result checking started, but there is no evidence that a network change was actually executed.')
        : (zh ? '等待影響範圍與變更清單確認。' : 'Waiting for the change scope and action list.'),
      details: decision?.verificationSummary ? (zh ? '後端回報執行動作 0 項。' : 'Backend reported 0 execution actions.') : undefined,
    }
  } else if (finishedCount < actions.length) {
    const currentAction = (runningAction?.description ?? (zh ? '等待下一項動作' : 'waiting for the next action')).replace(/[。.!?]+$/, '')
    execute = {
      key: 'execute',
      title: zh ? '執行變更' : 'Execute',
      status: 'active',
      summary: zh ? '網路正在逐項套用調整，目前尚未全部完成，使用體驗可能還在變化。' : 'Network changes are being applied one by one; the user experience may still be changing.',
      details: compactTimelineText(zh
        ? `已完成 ${finishedCount}/${actions.length}；目前：${currentAction}。`
        : `${finishedCount}/${actions.length} finished; current: ${currentAction}.`),
    }
  } else {
    const status: DecisionTimelineStageStatus = failedCount > 0 ? 'failed' : skippedCount === actions.length ? 'warning' : 'complete'
    execute = {
      key: 'execute',
      title: zh ? '執行變更' : 'Execute',
      status,
      summary: failedCount > 0
        ? (zh ? '有部分網路調整失敗，這次變更可能不完整，不能直接視為成功。' : 'Some network changes failed, so this round may be incomplete and cannot be treated as successful.')
        : skippedCount === actions.length
          ? (zh ? '這次沒有任何調整真正執行，網路維持原狀。' : 'No change was actually executed, so the network stayed as it was.')
          : (zh ? '預定的網路調整已完成，接下來要確認使用體驗是否真的改善。' : 'The planned network changes are complete; the next step is confirming whether user experience actually improved.'),
      details: zh
        ? `執行結果：成功 ${successCount}、失敗 ${failedCount}、略過 ${skippedCount}。`
        : `Execution result: ${successCount} succeeded, ${failedCount} failed, ${skippedCount} skipped.`,
    }
  }

  let verify: DecisionTimelineStage
  const verification = decision?.verificationSummary
  if (verification) {
    const verificationStatus = String(verification.status).toLowerCase()
    const verificationInProgress = ['pending', 'waiting', 'running'].includes(verificationStatus)
    const report = decision?.validationReport?.sla_result
    const checks = verification.checks ?? decision?.verification ?? []
    const passedChecks = checks.filter((check) => ['pass', 'passed'].includes(check.status)).length
    const measured = report
      ? (zh
          ? `延遲 ${report.latency_ms.value}/≤${report.latency_ms.threshold} ms；吞吐 ${report.throughput_mbps.value}/≥${report.throughput_mbps.threshold} Mbps。`
          : `Latency ${report.latency_ms.value}/≤${report.latency_ms.threshold} ms; throughput ${report.throughput_mbps.value}/≥${report.throughput_mbps.threshold} Mbps.`)
      : checks.length > 0
        ? (zh ? `${passedChecks}/${checks.length} 項檢查通過。` : `${passedChecks}/${checks.length} checks passed.`)
        : (zh ? '後端未提供逐項量測值。' : 'No per-check measurements were reported.')
    verify = {
      key: 'verify',
      title: zh ? '驗證 SLA' : 'Verify SLA',
      status: verificationInProgress ? 'active' : verificationStatus === 'passed' ? 'complete' : verificationStatus === 'failed' ? 'failed' : 'warning',
      summary: plainVerificationResult(verificationStatus, scenarioNames, targetSlice, zh),
      details: verificationInProgress
        ? (zh ? `SLA ${verificationStatus}；等待執行後實測結果。` : `SLA ${verificationStatus}; waiting for post-execution measurements.`)
        : `SLA ${verificationStatus}：${measured}`,
    }
  } else if (blocked) {
    verify = { key: 'verify', title: zh ? '驗證 SLA' : 'Verify SLA', status: 'warning', summary: zh ? '因為沒有執行網路調整，本輪無法比較調整前後的使用體驗。' : 'Because no network change was executed, this round cannot compare user experience before and after.', details: zh ? '無執行後 SLA 資料。' : 'No post-execution SLA data.' }
  } else if (stage === 'complete') {
    verify = { key: 'verify', title: zh ? '驗證 SLA' : 'Verify SLA', status: 'warning', summary: zh ? '這輪調整已結束，但目前沒有足夠資料證明使用體驗真的改善。' : 'The round has ended, but there is not enough evidence to prove that user experience improved.', details: zh ? '後端未回報 SLA 驗證摘要。' : 'Backend did not report an SLA verification summary.' }
  } else if ((actions.length > 0 && finishedCount === actions.length && isSimulating) || stage.includes('verif') || stage.includes('sla')) {
    verify = { key: 'verify', title: zh ? '驗證 SLA' : 'Verify SLA', status: 'active', summary: zh ? '系統正在確認調整後是否真的改善使用體驗，現在還不能下結論。' : 'The system is checking whether the adjustment really improved user experience; it is too early to conclude.', details: zh ? '正在比對執行後實測值與 SLA。' : 'Comparing post-execution measurements with the SLA.' }
  } else {
    verify = { key: 'verify', title: zh ? '驗證 SLA' : 'Verify SLA', status: 'waiting', summary: zh ? '等待變更執行完成。' : 'Waiting for execution to finish.' }
  }

  return [intent, plan, scope, execute, verify]
}

const TIMELINE_TONE: Record<DecisionTimelineStageStatus, string> = {
  waiting: 'border-slate-200 bg-white text-slate-500',
  active: 'border-violet-300 bg-violet-50 text-violet-800',
  complete: 'border-green-200 bg-green-50 text-green-800',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  failed: 'border-red-200 bg-red-50 text-red-800',
}

function DecisionTimeline({ stages }: { stages: DecisionTimelineStage[] }) {
  const { text } = useLocale()
  const statusLabel: Record<DecisionTimelineStageStatus, string> = {
    waiting: text('等待', 'Waiting'),
    active: text('進行中', 'Active'),
    complete: text('完成', 'Complete'),
    warning: text('注意', 'Attention'),
    failed: text('失敗', 'Failed'),
  }
  return (
    <section className="shrink-0 rounded-lg border border-slate-200 bg-slate-50 p-2" aria-label={text('AI 決策時間軸', 'AI decision timeline')}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-700">{text('AI 決策時間軸', 'AI decision timeline')}</h3>
        <span className="text-[9px] text-slate-500">{text('白話結果＋技術依據', 'Plain result + evidence')}</span>
      </div>
      <ol className="space-y-1.5">
        {stages.map((step, index) => (
          <li
            key={step.key}
            data-testid={`timeline-stage-${step.key}`}
            aria-current={step.status === 'active' ? 'step' : undefined}
            className={`flex gap-2 rounded-md border px-2 py-1.5 ${TIMELINE_TONE[step.status]}`}
          >
            <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] font-bold ${step.status === 'active' ? 'animate-pulse border-violet-400 bg-white' : 'border-current bg-white/70'}`}>
              {step.status === 'complete' ? '✓' : step.status === 'failed' ? '!' : index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[10px] font-bold">{step.title}</p>
                <span className="shrink-0 rounded border border-current/20 bg-white/60 px-1 text-[8px] font-bold">{statusLabel[step.status]}</span>
              </div>
              <p className="mt-0.5 break-words text-[10px] font-medium leading-relaxed text-slate-700">{step.summary}</p>
              {step.details && (
                <p data-testid={`timeline-stage-details-${step.key}`} className="mt-1 break-words rounded border border-current/10 bg-white/70 px-1.5 py-1 text-[8px] leading-relaxed text-slate-500">
                  <span className="font-bold text-slate-600">{text('技術依據：', 'Evidence: ')}</span>{step.details}
                </p>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
