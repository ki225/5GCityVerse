import { useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'
import { useAppStore } from '../../store/appStore'
import { SLICE_COLOR } from '../CityMap/cityData'
import { EVENT_REQUEST_MBPS } from '../CityMap/CanvasCityMap'
import type { AgentDecision, CityEventType, NetworkMetrics, PacketFlow, RuntimePrimeStatus, SliceStatus, SliceType } from '../../types'
import { useLocale } from '../../i18n'

const SLICE_LABEL: Record<SliceType, string> = {
  eMBB: 'eMBB (SST=1)', URLLC: 'URLLC (SST=2)', mMTC: 'mMTC (SST=3)', V2X: 'V2X (SST=4)',
}

export const INITIAL_AI_ALLOCATION: Record<SliceType, number> = {
  eMBB: 45,
  URLLC: 5,
  mMTC: 45,
  V2X: 5,
}

const SLICE_MUTATION_ACTIONS = new Set<AgentDecision['actions'][number]['type']>([
  'nef_traffic_influence',
  'nef_qos',
  'k8s_hpa',
])

export interface AppliedAiAllocationState {
  shares: Record<SliceType, number>
  decisionKey: string | null
}

export function committedAiDecisionKey(decision: AgentDecision | null): string | null {
  if (!decision?.completedAt) return null
  const changedNetworkPolicy = decision.actions.some((action) =>
    action.status === 'success' && SLICE_MUTATION_ACTIONS.has(action.type),
  )
  if (!changedNetworkPolicy) return null
  return decision.executionId || decision.completedAt
}

export function resolveAppliedAiAllocation(
  current: AppliedAiAllocationState,
  observedSlices: Pick<SliceStatus, 'type' | 'load'>[],
  decision: AgentDecision | null,
): AppliedAiAllocationState {
  const decisionKey = committedAiDecisionKey(decision)
  if (!decisionKey || decisionKey === current.decisionKey) return current
  return {
    shares: normalizeAllocation(observedSlices.map((slice) => ({
      type: slice.type,
      weight: Math.max(5, slice.load),
    }))),
    decisionKey,
  }
}

export function isSliceStrategyLocked(orchestrationStage: string): boolean {
  return orchestrationStage !== 'idle'
}

export function SliceDashboard() {
  const { text } = useLocale()
  const { slices, metrics, metricsHistory, pods, free5gcStatus, activeEvent, activeScenarios, packetFlows, agentDecision, agentDecisionHistory, runtimePrime, orchestrationStage, sliceStrategy, submittedSliceStrategy } = useAppStore()
  const sparkRef = useRef<SVGSVGElement>(null)
  const strategyLocked = isSliceStrategyLocked(orchestrationStage)
  const effectiveSliceStrategy = submittedSliceStrategy ?? sliceStrategy
  const [appliedAiAllocation, setAppliedAiAllocation] = useState<AppliedAiAllocationState>(() => ({
    shares: { ...INITIAL_AI_ALLOCATION },
    decisionKey: null,
  }))

  // D3 sparkline for throughput history
  useEffect(() => {
    if (!sparkRef.current || metricsHistory.length < 2) return
    const svg = d3.select(sparkRef.current)
    svg.selectAll('*').remove()
    const W = sparkRef.current.clientWidth || 200
    const H = 36
    const x = d3.scaleLinear().domain([0, metricsHistory.length - 1]).range([0, W])
    const y = d3.scaleLinear()
      .domain([0, d3.max(metricsHistory, (d) => d.throughputMbps) ?? 100])
      .range([H, 2])
    const line = d3.line<typeof metricsHistory[0]>()
      .x((_, i) => x(i))
      .y((d) => y(d.throughputMbps))
      .curve(d3.curveCatmullRom)
    svg.append('path')
      .datum(metricsHistory)
      .attr('d', line)
      .attr('fill', 'none')
      .attr('stroke', '#3b82f6')
      .attr('stroke-width', 1.5)
      .attr('opacity', 0.8)
  }, [metricsHistory])

  // Live slice load is observation input. It must not become an applied
  // allocation until a completed AI round successfully changes network policy.
  useEffect(() => {
    setAppliedAiAllocation((current) => resolveAppliedAiAllocation(current, slices, agentDecision))
  }, [agentDecision, slices])

  const upfComponent = pods.find((c) => c.component === 'UPF')
  const amfComponent = pods.find((c) => c.component === 'AMF')
  const allocation = useMemo(() => {
    if (effectiveSliceStrategy === 'none') return [{ type: 'eMBB' as SliceType, percent: 100, label: text('共用容量', 'Shared pool') }]
    const shares: Record<SliceType, number> = effectiveSliceStrategy === 'static'
      ? { eMBB: 40, URLLC: 30, mMTC: 20, V2X: 10 }
      : appliedAiAllocation.shares
    return (Object.keys(shares) as SliceType[]).map((type) => ({ type, percent: shares[type], label: type }))
  }, [appliedAiAllocation.shares, effectiveSliceStrategy, text])

  return (
    <div className="panel flex flex-col gap-4 max-h-[calc(100vh-2rem)] overflow-y-auto">
      <h2 className="text-sm font-bold text-slate-700 tracking-wider uppercase shrink-0">
        {text('5GC 核網儀表板', '5GC Dashboard')}
      </h2>

      <section className="shrink-0 rounded-lg border border-slate-200 bg-white p-2.5 text-xs">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div>
            <p className="font-bold text-slate-800">{text('切片策略', 'Slicing strategy')}</p>
            <p className="text-[10px] text-slate-500">{text('切片只重新分配既有資源，不會增加頻寬。', 'Slicing reallocates existing resources; it does not create bandwidth.')}</p>
          </div>
          <div className="flex shrink-0 flex-wrap justify-end gap-1">
            <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-1 font-bold text-blue-700">{text('總容量固定 100%', 'Fixed total 100%')}</span>
            {strategyLocked && <span data-testid="slice-strategy-locked" className="rounded-full border border-amber-200 bg-amber-50 px-2 py-1 font-bold text-amber-700">{text('本輪策略已鎖定', 'Strategy locked for this round')}</span>}
          </div>
        </div>
        <div className="flex items-center justify-between rounded-md border border-violet-200 bg-violet-50 px-3 py-2">
          <span className="text-[10px] font-semibold text-slate-500">{text('事件設定所選策略', 'Strategy selected in Event Setup')}</span>
          <span className="font-bold text-violet-800" data-testid="active-slice-strategy">
            {effectiveSliceStrategy === 'none' ? text('無切片', 'No slicing') : effectiveSliceStrategy === 'static' ? text('靜態切片', 'Static slicing') : text('AI 動態切片', 'AI dynamic slicing')}
          </span>
        </div>
        <p data-testid="slice-strategy-lock-note" className={`mt-1 text-[10px] ${strategyLocked ? 'font-semibold text-amber-700' : 'text-slate-500'}`}>
          {strategyLocked
            ? text('情境已送出，本輪切片策略固定；重新啟動模擬後才能再次選擇。', 'The scenario was submitted and this round is locked; restart the simulation to choose again.')
            : text('請在「事件設定」分頁選擇策略。', 'Choose the strategy in Event Setup.')}
        </p>
        <div className="mt-2 flex h-9 overflow-hidden rounded-md border border-slate-200 bg-slate-100" aria-label={text('固定總容量分配', 'Fixed total capacity allocation')}>
          {allocation.map((item) => <div key={item.type} className="flex min-w-0 items-center justify-center text-[10px] font-bold text-white transition-all duration-500" style={{ width: `${item.percent}%`, background: SLICE_COLOR[item.type] }} title={`${item.label}: ${item.percent}%`}>{item.percent >= 10 ? `${item.label} ${item.percent}%` : `${item.percent}%`}</div>)}
        </div>
        <p className="mt-1 text-[10px] text-slate-500">{effectiveSliceStrategy === 'none' ? text('所有服務競爭同一個資源池，尖峰時關鍵服務也可能受影響。', 'All services compete for one pool; critical traffic can be affected at peaks.') : effectiveSliceStrategy === 'static' ? text('比例預先固定，容易預測，但閒置容量不會自動讓給急需服務。', 'Predictable fixed shares, but idle capacity is not automatically reassigned.') : appliedAiAllocation.decisionKey ? text('已套用 AI 決策；此比例會鎖定至下一次成功執行。', 'AI decision applied; this allocation stays locked until the next successful execution.') : agentDecision?.completedAt ? text('AI 已完成；本輪沒有成功的切片配置變更，因此維持原比例。', 'AI completed without a successful slice-allocation change, so the current allocation remains locked.') : text('AI 尚未執行；目前維持基準配置。即時負載只供觀測，不會自行改變比例。', 'AI has not executed; the baseline allocation remains locked. Live load is observation only.')}</p>
      </section>

      <ExperienceOutcomes metrics={metrics} activeScenarios={activeScenarios} packetFlows={packetFlows} agentDecision={agentDecision} agentDecisionHistory={agentDecisionHistory} runtimePrime={runtimePrime} />

      <div className="bg-slate-50 border border-slate-200 rounded p-2 text-xs shrink-0">
        <div className="flex items-center justify-between">
          <span className="text-slate-400">free5GC Live</span>
          <div className="flex items-center gap-1.5">
            {metrics?.dataSource && <DataSourceBadge source={metrics.dataSource} />}
            {metrics?.evidenceLevel && <EvidenceLevelBadge level={metrics.evidenceLevel} />}
            <span className={free5gcStatus?.connected ? 'text-green-700' : 'text-red-600'}>
              {free5gcStatus?.connected ? text('已連線', 'connected') : text('離線', 'offline')}
            </span>
          </div>
        </div>
        <div className="mt-1 grid grid-cols-2 gap-2 text-[10px] text-slate-500">
          <span>{text('訂閱用戶', 'Subscribers')}</span>
          <span className="text-right text-slate-700">{free5gcStatus?.subscriberCount ?? 0}</span>
          <span>{text('城市情境紀錄', 'City records')}</span>
          <span className="text-right text-blue-700">{free5gcStatus?.eventSubscriberCount ?? 0}</span>
          <span>{text('已註冊 UE', 'Registered UEs')}</span>
          <span className="text-right text-green-700">
            {free5gcStatus?.registeredUeCount ?? 0}
            {!!free5gcStatus?.metrics?.staleRegistrations && (
              <span
                className="ml-1 text-slate-400"
                title={text('UE pod 已移除但 core 尚未釋放註冊，屬 5GC 正常 timeout 行為', 'The UE pod is gone while the core registration awaits its normal timeout.')}
              >
                ({text('含', 'includes')} {free5gcStatus.metrics.staleRegistrations} {text('殘留', 'stale')})
              </span>
            )}
          </span>
          <span>{text('用戶設定檔', 'Profiles')}</span>
          <span className="text-right text-indigo-700">{free5gcStatus?.profileCount ?? 0}</span>
        </div>
        <p className="mt-1 text-[10px] text-slate-600 truncate" title={free5gcStatus?.source}>
          {free5gcStatus?.checkedAt ? `sync ${free5gcStatus.checkedAt}` : 'sync pending'}
        </p>
        {free5gcStatus?.error && (
          <p className="mt-1 text-[10px] text-red-400 truncate" title={free5gcStatus.error}>
            {free5gcStatus.error}
          </p>
        )}
      </div>

      {/* Key metrics */}
      {metrics && (
        <div className="grid grid-cols-2 gap-2 shrink-0">
          <MetricCard label={text('延遲', 'Latency')} value={`${metrics.latencyMs} ms`} color="text-red-600" source={metrics.dataSource} />
          <MetricCard label={text('吞吐量', 'Throughput')} value={`${metrics.throughputMbps} Mbps`} color="text-blue-700" source={metrics.dataSource} />
          <MetricCard label={text('PDU 工作階段', 'PDU Sessions')} value={String(metrics.pduSessionCount)} color="text-green-700" source={metrics.dataSource} />
          <MetricCard label="GTP pkt/s" value={formatOptionalGtp(metrics.gtpPacketsPerSec, text)} color="text-orange-600" source={metrics.dataSource} />
        </div>
      )}

      <PacketJourney
        metrics={metrics}
        activeEvent={activeEvent}
        targetSlice={agentDecision?.intent?.targetSlice.name}
        targetSst={agentDecision?.intent?.targetSlice.sst}
        targetFiveQi={agentDecision?.intent?.targetSlice.fiveQi}
        ueId={agentDecision?.intent?.ueIds?.[0]}
        runtimeObserved={runtimePrime?.observedBeforePlanning === true}
      />

      {/* Throughput sparkline */}
      <div className="shrink-0">
        <p className="text-[10px] text-slate-500 mb-1 uppercase tracking-wider">{text('吞吐量歷史', 'Throughput history')}</p>
        <svg ref={sparkRef} className="w-full" height={36} />
      </div>

      {/* Slice bars */}
      <div className="space-y-2.5 shrink-0">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider">{text('網路切片', 'Network Slices')}</p>
        {slices.map((s) => (
          <div key={s.sst}>
            <div className="flex items-center justify-between text-xs mb-0.5">
              <span className="flex items-center gap-1.5 font-semibold" style={{ color: SLICE_COLOR[s.type] }}>
                {SLICE_LABEL[s.type]}
                {s.loadSource && <LoadSourceBadge source={s.loadSource} />}
                {s.selectionStage && <SelectionStageBadge stage={s.selectionStage} />}
              </span>
              <span className="flex items-center gap-1 text-slate-500">
                <TrendArrow trend={s.trend} />
                {s.load}%
              </span>
            </div>
            <div className="w-full bg-slate-200 rounded-full h-2">
              <div
                className="h-2 rounded-full transition-all duration-700"
                style={{ width: `${s.load}%`, background: SLICE_COLOR[s.type], opacity: 0.85 }}
              />
            </div>
            <p className="text-[10px] text-slate-600 mt-0.5">{s.sessions.toLocaleString()} {text('個工作階段', 'sessions')}</p>
          </div>
        ))}
      </div>

      {/* Pod status */}
      <div className="shrink-0">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-2">{text('Pod 狀態', 'Pod Status')}</p>
        <div className="space-y-1">
          <PodRow label="UPF" pods={upfComponent?.pods ?? []} color="#0ea5e9" cpu={metrics?.componentCpuPercent?.UPF ?? metrics?.upfCpuPercent} />
          <PodRow label="AMF" pods={amfComponent?.pods ?? []} color="#8b5cf6" cpu={metrics?.componentCpuPercent?.AMF ?? metrics?.amfCpuPercent} />
          {pods
            .filter((c) => c.component !== 'UPF' && c.component !== 'AMF')
            .map((c) => (
              <PodRow key={c.component} label={c.component} pods={c.pods} color="#475569" cpu={metrics?.componentCpuPercent?.[c.component]} />
            ))}
        </div>
      </div>
    </div>
  )
}

function normalizeAllocation(items: Array<{ type: SliceType; weight: number }>): Record<SliceType, number> {
  const fallback: Record<SliceType, number> = { eMBB: 40, URLLC: 30, mMTC: 20, V2X: 10 }
  if (items.length === 0) return fallback
  const weights: Record<SliceType, number> = { eMBB: 5, URLLC: 5, mMTC: 5, V2X: 5 }
  items.forEach(({ type, weight }) => { weights[type] = weight })
  const total = Object.values(weights).reduce((sum, value) => sum + value, 0)
  const result = {} as Record<SliceType, number>
  let assigned = 0
  ;(['eMBB', 'URLLC', 'mMTC'] as SliceType[]).forEach((type) => { result[type] = Math.round((weights[type] / total) * 100); assigned += result[type] })
  result.V2X = 100 - assigned
  return result
}

interface ScenarioExperienceMeasurement {
  throughputMbps: number
  latencyMs: number
  observed: boolean
}

const EMPTY_EXPERIENCE: ScenarioExperienceMeasurement = { throughputMbps: 0, latencyMs: 0, observed: false }

export interface CitizenUeExperienceMeasurement extends ScenarioExperienceMeasurement {
  packetLossPercent: number
  source: 'ue-tun-probe'
}

const EMPTY_CITIZEN_UE_EXPERIENCE: CitizenUeExperienceMeasurement = {
  throughputMbps: 0,
  latencyMs: 0,
  packetLossPercent: 0,
  observed: false,
  source: 'ue-tun-probe',
}

function measureFlowPath(flows: PacketFlow[]): ScenarioExperienceMeasurement {
  if (flows.length === 0) return EMPTY_EXPERIENCE
  const throughputValues = flows
    .map((flow) => Number(flow.throughputMbps ?? flow.bandwidthMbps ?? 0))
    .filter(Number.isFinite)
  const latencyValues = flows.map((flow) => Number(flow.latencyMs ?? 0)).filter(Number.isFinite)
  return {
    throughputMbps: throughputValues.length > 0 ? Math.min(...throughputValues) : 0,
    latencyMs: latencyValues.length > 0 ? Math.max(...latencyValues) : 0,
    observed: true,
  }
}

export function measureCitizenUeExperience(metrics: NetworkMetrics | null): CitizenUeExperienceMeasurement {
  const probe = metrics?.ueTunProbe
  if (!probe?.ready) return EMPTY_CITIZEN_UE_EXPERIENCE
  return {
    throughputMbps: Number.isFinite(probe.throughputMbps) ? Number(probe.throughputMbps) : 0,
    latencyMs: Number.isFinite(probe.latencyMs) ? Number(probe.latencyMs) : 0,
    packetLossPercent: Number.isFinite(probe.packetLossPercent) ? Number(probe.packetLossPercent) : 0,
    observed: true,
    source: 'ue-tun-probe',
  }
}

export function measureScenarioExperience(flows: PacketFlow[], eventType: CityEventType): ScenarioExperienceMeasurement {
  const userPlaneFlows = flows.filter((flow) => flow.active && flow.plane !== 'control' && flow.scenario === eventType)
  // A bearer is repeated on several topology edges; end-to-end capacity is
  // the path bottleneck, not the sum of those edges.
  return measureFlowPath(userPlaneFlows)
}

export function scenarioExperienceKey(
  scenario: ReturnType<typeof useAppStore.getState>['activeScenarios'][number],
): string {
  // A network-round batch intentionally shares one executionId across all
  // scenarios. Include the event type so one scenario cannot overwrite the
  // measurements (or baseline) of another scenario in the same batch.
  return `${scenario.executionId ?? scenario.startedAt}:${scenario.type}`
}

export function measureActiveScenarioExperiences(
  activeScenarios: ReturnType<typeof useAppStore.getState>['activeScenarios'],
  flows: PacketFlow[],
): Record<string, ScenarioExperienceMeasurement> {
  return Object.fromEntries(activeScenarios.map((scenario) => [
    scenarioExperienceKey(scenario),
    measureScenarioExperience(flows, scenario.type),
  ]))
}

export function hasPassedSla(decision: AgentDecision | null | undefined): boolean {
  const validation = decision?.validationReport?.sla_result
  if (validation) {
    const status = validation.status.toLowerCase()
    return (status === 'pass' || status === 'passed')
      && validation.latency_ms.passed
      && validation.throughput_mbps.passed
  }
  return decision?.verificationSummary?.status === 'passed'
}

export function shouldShowCitizenUeComparison(
  baseline: CitizenUeExperienceMeasurement | null,
  current: CitizenUeExperienceMeasurement,
  decision: AgentDecision | null | undefined,
): boolean {
  return Boolean(
    baseline?.observed
    && current.observed
    && committedAiDecisionKey(decision ?? null)
    && hasPassedSla(decision),
  )
}

function ExperienceOutcomes({
  metrics,
  activeScenarios,
  packetFlows,
  agentDecision,
  agentDecisionHistory,
  runtimePrime,
}: {
  metrics: NetworkMetrics | null
  activeScenarios: ReturnType<typeof useAppStore.getState>['activeScenarios']
  packetFlows: PacketFlow[]
  agentDecision: AgentDecision | null
  agentDecisionHistory: ReturnType<typeof useAppStore.getState>['agentDecisionHistory']
  runtimePrime: RuntimePrimeStatus | null
}) {
  const { text } = useLocale()
  const citizenCurrent = useMemo(() => measureCitizenUeExperience(metrics), [metrics])
  const [citizenBaseline, setCitizenBaseline] = useState<CitizenUeExperienceMeasurement | null>(null)
  const latestCitizenMeasurement = useRef<CitizenUeExperienceMeasurement | null>(null)
  const previousRoundKey = useRef('')
  const roundKey = useMemo(() => activeScenarios
    .map(scenarioExperienceKey)
    .sort()
    .join('|'), [activeScenarios])
  const measurements = useMemo(
    () => measureActiveScenarioExperiences(activeScenarios, packetFlows),
    [activeScenarios, packetFlows],
  )
  const [baselines, setBaselines] = useState<Record<string, ScenarioExperienceMeasurement>>({})

  useEffect(() => {
    const previousMeasurement = latestCitizenMeasurement.current
    const roundStarted = Boolean(roundKey) && roundKey !== previousRoundKey.current
    if (roundStarted) {
      const beforeTraffic = previousMeasurement?.observed ? previousMeasurement : citizenCurrent
      if (beforeTraffic.observed) setCitizenBaseline(beforeTraffic)
    } else if (!roundKey && !agentDecision && agentDecisionHistory.length === 0 && citizenCurrent.observed) {
      // While idle, keep the baseline aligned with the latest real UE probe.
      setCitizenBaseline(citizenCurrent)
    }
    if (citizenCurrent.observed) latestCitizenMeasurement.current = citizenCurrent
    previousRoundKey.current = roundKey
  }, [agentDecision, agentDecisionHistory.length, citizenCurrent, roundKey])

  useEffect(() => {
    setBaselines((previous) => {
      const next: Record<string, ScenarioExperienceMeasurement> = {}
      activeScenarios.forEach((scenario) => {
        const key = scenarioExperienceKey(scenario)
        const measured = measurements[key]
        if (previous[key]) next[key] = previous[key]
        else if (measured?.observed) next[key] = measured
      })
      const unchanged = Object.keys(previous).length === Object.keys(next).length
        && Object.entries(next).every(([key, value]) => previous[key] === value)
      return unchanged ? previous : next
    })
  }, [activeScenarios, measurements])

  const decisions = [agentDecision, ...[...agentDecisionHistory].reverse().map((record) => record.decision)].filter((item): item is AgentDecision => Boolean(item))
  const citizenDecision = decisions.find((decision) => shouldShowCitizenUeComparison(citizenBaseline, citizenCurrent, decision))
  const showCitizenComparison = Boolean(citizenDecision)
  const citizenImproved = Boolean(citizenBaseline) && (
    citizenCurrent.throughputMbps > citizenBaseline!.throughputMbps
    || (citizenCurrent.latencyMs > 0 && (citizenBaseline!.latencyMs <= 0 || citizenCurrent.latencyMs < citizenBaseline!.latencyMs))
    || citizenCurrent.packetLossPercent < citizenBaseline!.packetLossPercent
  )
  const citizenStatus = describeCitizenUeExperience(
    citizenCurrent,
    text,
    metrics?.pduSessionCount ?? 0,
    metrics?.ueTunProbe?.receivedPackets ?? 0,
  )

  return (
    <section className="shrink-0 rounded-lg border border-slate-200 bg-slate-50 p-2.5 text-xs">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="font-bold text-slate-800">{text('對生活體驗的影響', 'Impact on everyday experience')}</p>
        <span className="text-[10px] text-slate-500">{text('市民手機', 'Citizen phone')} + {activeScenarios.length} {text('個情境', 'scenarios')}</span>
      </div>
      <div className="space-y-2">
          <article data-testid="citizen-ue-experience" className="rounded border border-blue-200 bg-blue-50/40 px-2.5 py-2">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <p className="font-bold text-slate-800">● {text('市民手機：日常上網／影音', 'Citizen phone: everyday internet / video')}</p>
                  <span className={`rounded border px-1 text-[9px] font-bold ${citizenCurrent.observed ? 'border-emerald-200 bg-white text-emerald-700' : 'border-slate-200 bg-slate-50 text-slate-500'}`}>
                    {citizenCurrent.observed ? text('UE-TUN 實測', 'UE-TUN measured') : text('等待 UE-TUN', 'Waiting for UE-TUN')}
                  </span>
                </div>
                <p className={`mt-0.5 text-[10px] font-semibold ${citizenStatus.tone}`}>{citizenStatus.label}</p>
              </div>
              {citizenCurrent.observed ? (
                <div className="shrink-0 text-right">
                  <p className="font-bold text-blue-700">{formatExperienceMetric(citizenCurrent.throughputMbps)} Mbps</p>
                  <p className="text-[10px] font-bold text-red-600">{formatExperienceMetric(citizenCurrent.latencyMs)} ms · {formatExperienceMetric(citizenCurrent.packetLossPercent)}% loss</p>
                </div>
              ) : <span className="shrink-0 text-[10px] text-slate-400">{text('不以整體吞吐量代替 UE 體驗', 'Aggregate throughput is not used as UE experience')}</span>}
            </div>
            {showCitizenComparison && citizenBaseline && (
              <div className="mt-2 rounded border border-green-200 bg-green-50 px-2 py-1.5 text-[10px] text-green-800">
                <p className="font-bold">✓ {citizenImproved ? text('市民手機體驗改善（AI 執行、SLA 通過）', 'Citizen phone experience improved (AI executed, SLA passed)') : text('市民手機調整後（AI 執行、SLA 通過）', 'Citizen phone after adjustment (AI executed, SLA passed)')}</p>
                <p className="mt-0.5">
                  {text('吞吐量', 'Throughput')} {formatExperienceMetric(citizenBaseline.throughputMbps)} → {formatExperienceMetric(citizenCurrent.throughputMbps)} Mbps
                  <span className="mx-1.5 text-green-400">·</span>
                  {text('延遲', 'Latency')} {formatExperienceMetric(citizenBaseline.latencyMs)} → {formatExperienceMetric(citizenCurrent.latencyMs)} ms
                  <span className="mx-1.5 text-green-400">·</span>
                  {text('丟包', 'Loss')} {formatExperienceMetric(citizenBaseline.packetLossPercent)} → {formatExperienceMetric(citizenCurrent.packetLossPercent)}%
                </p>
              </div>
            )}
          </article>
          {activeScenarios.map((scenario) => {
            const key = scenarioExperienceKey(scenario)
            const current = measurements[key] ?? EMPTY_EXPERIENCE
            const baseline = baselines[key] ?? current
            const record = [...agentDecisionHistory].reverse().find((item) =>
              (scenario.executionId && item.executionId === scenario.executionId) || item.eventType === scenario.type)
            const decision = record?.decision ?? (agentDecision?.intent?.eventType === scenario.type ? agentDecision : null)
            const validation = decision?.validationReport?.sla_result
            const after = {
              throughputMbps: validation?.throughput_mbps.value ?? current.throughputMbps,
              latencyMs: validation?.latency_ms.value ?? current.latencyMs,
            }
            const showComparison = baseline.observed && hasPassedSla(decision)
            const improved = after.throughputMbps > baseline.throughputMbps || after.latencyMs < baseline.latencyMs
            const experience = describeExperience(scenario.type, current, text)
            const trafficState = resolveScenarioTrafficState(current, scenario.type, runtimePrime)
            return (
              <article key={key} className="rounded border border-slate-200 bg-white px-2.5 py-2">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-bold text-slate-800">{scenarioIcon(scenario.type)} {localizedScenarioName(scenario.type, text)}</p>
                    <p className={`mt-0.5 text-[10px] font-semibold ${experience.tone}`}>{experience.label}</p>
                  </div>
                  {current.observed ? (
                    <div className="shrink-0 text-right">
                      <p className="font-bold text-blue-700">{formatExperienceMetric(current.throughputMbps)} Mbps</p>
                      <p className="text-[10px] font-bold text-red-600">{formatExperienceMetric(current.latencyMs)} ms</p>
                    </div>
                  ) : trafficState === 'missing' ? (
                    <span className="shrink-0 rounded border border-red-200 bg-red-50 px-1.5 py-1 text-[10px] font-bold text-red-600">
                      {text('網路端未量測到', 'Not observed by network')}
                    </span>
                  ) : (
                    <div className="shrink-0 text-right">
                      <p className="font-bold text-blue-700">
                        {formatExperienceMetric(EVENT_REQUEST_MBPS[scenario.type])} Mbps {text('目標', 'target')}
                      </p>
                      <p className="text-[10px] font-semibold text-amber-700">
                        {text('流量生成中 · 等待實測', 'Generating traffic · awaiting measurement')}
                      </p>
                    </div>
                  )}
                </div>
                {!current.observed && trafficState === 'missing' && (
                  <p className="mt-1.5 text-[10px] text-red-600">
                    {text('本輪缺少此情境的 user-plane bearer 證據，未以請求值冒充實測值。', 'This round has no user-plane bearer evidence; requested traffic is not shown as measured traffic.')}
                  </p>
                )}
                {showComparison && (
                  <div className="mt-2 rounded border border-green-200 bg-green-50 px-2 py-1.5 text-[10px] text-green-800">
                    <p className="font-bold">✓ {improved ? text('改善（SLA 通過）', 'Improved (SLA passed)') : text('AI 調整後（SLA 通過）', 'After AI adjustment (SLA passed)')}</p>
                    <p className="mt-0.5">
                      {text('吞吐量', 'Throughput')} {formatExperienceMetric(baseline.throughputMbps)} → {formatExperienceMetric(after.throughputMbps)} Mbps
                      <span className="mx-1.5 text-green-400">·</span>
                      {text('延遲', 'Latency')} {formatExperienceMetric(baseline.latencyMs)} → {formatExperienceMetric(after.latencyMs)} ms
                    </p>
                  </div>
                )}
              </article>
            )
          })}
      </div>
    </section>
  )
}

export function resolveScenarioTrafficState(
  measurement: ScenarioExperienceMeasurement,
  scenarioType: CityEventType,
  runtimePrime: RuntimePrimeStatus | null | undefined,
): 'measured' | 'pending' | 'missing' {
  if (measurement.observed) return 'measured'
  if (
    runtimePrime?.status === 'traffic_not_observed'
    && runtimePrime.missingScenarios?.includes(scenarioType)
  ) return 'missing'
  return 'pending'
}

function describeExperience(type: CityEventType, measurement: ScenarioExperienceMeasurement, text: (zh: string, en: string) => string) {
  if (!measurement.observed) return { label: text('等待實測資料', 'Waiting for measurements'), tone: 'text-slate-500' }
  if (type === 'concert') return measurement.throughputMbps >= 25 && measurement.latencyMs <= 50
    ? { label: text('直播穩定，可維持 720p 以上', 'Stable stream at 720p or better'), tone: 'text-green-700' }
    : { label: text('直播可能卡頓或需要降低畫質', 'Stream may buffer or reduce quality'), tone: 'text-amber-700' }
  if (type === 'medical') return measurement.latencyMs > 0 && measurement.latencyMs <= 20
    ? { label: text('醫療影像低延遲穩定傳輸', 'Medical imaging is stable at low latency'), tone: 'text-green-700' }
    : { label: text('醫療影像有延遲風險', 'Medical imaging has a latency risk'), tone: 'text-red-600' }
  if (type === 'accident') return measurement.latencyMs > 0 && measurement.latencyMs <= 20
    ? { label: text('車聯網可即時改道', 'Vehicles can reroute in real time'), tone: 'text-green-700' }
    : { label: text('車聯網警示可能延後', 'Vehicle alerts may be delayed'), tone: 'text-amber-700' }
  if (type === 'typhoon') return measurement.latencyMs > 0 && measurement.latencyMs <= 50
    ? { label: text('防災告警可即時送達', 'Emergency alerts arrive promptly'), tone: 'text-green-700' }
    : { label: text('防災告警有延遲風險', 'Emergency alerts may be delayed'), tone: 'text-amber-700' }
  return measurement.latencyMs > 0 && measurement.latencyMs <= 100
    ? { label: text('大量感測資料可持續回報', 'Large-scale sensors continue reporting'), tone: 'text-green-700' }
    : { label: text('感測回報可能壅塞', 'Sensor reporting may be congested'), tone: 'text-amber-700' }
}

export function describeCitizenUeExperience(measurement: CitizenUeExperienceMeasurement, text: (zh: string, en: string) => string, pduSessionCount = 0, receivedPackets = 0) {
  if (!measurement.observed && pduSessionCount > 0) return { label: text('TUN bearer 已建立，品質探測啟動中', 'TUN bearer established; quality probe is starting'), tone: 'text-amber-700' }
  if (!measurement.observed) return { label: text('等待市民手機建立 TUN bearer', 'Waiting for the citizen phone TUN bearer'), tone: 'text-slate-500' }
  if (measurement.packetLossPercent >= 50) {
    return { label: text('一般市民連線中斷或嚴重不穩', 'Citizen connectivity is interrupted or severely unstable'), tone: 'text-red-600' }
  }
  if (receivedPackets > 0 && measurement.latencyMs > 0 && measurement.latencyMs <= 50 && measurement.packetLossPercent < 1) {
    return { label: text('TUN bearer 正常，探測封包低延遲且無丟包', 'TUN bearer is healthy; probe packets have low latency and no loss'), tone: 'text-green-700' }
  }
  if (measurement.throughputMbps >= 25 && measurement.latencyMs <= 50 && measurement.packetLossPercent < 1) {
    return { label: text('日常影音與上網順暢', 'Everyday video and internet are smooth'), tone: 'text-green-700' }
  }
  if (measurement.throughputMbps >= 5 && measurement.latencyMs <= 100 && measurement.packetLossPercent < 5) {
    return { label: text('一般上網可用，高畫質影音可能降級', 'Internet is usable; high-quality video may downgrade'), tone: 'text-amber-700' }
  }
  return { label: text('日常連線可能卡頓', 'Everyday connectivity may feel congested'), tone: 'text-red-600' }
}

function localizedScenarioName(type: CityEventType, text: (zh: string, en: string) => string): string {
  const labels: Record<CityEventType, [string, string]> = {
    concert: ['演唱會直播', 'Concert live stream'],
    typhoon: ['颱風防災', 'Typhoon response'],
    accident: ['交通事故', 'Traffic accident'],
    medical: ['醫療影像', 'Medical imaging'],
    iot_surge: ['物聯網感測', 'IoT sensors'],
  }
  return text(...labels[type])
}

function scenarioIcon(type: CityEventType): string {
  return ({ concert: '▣', medical: '✚', accident: '◆', typhoon: '◉', iot_surge: '⌁' } as Record<CityEventType, string>)[type]
}

export function formatExperienceMetric(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (Math.abs(value) > 0 && Math.abs(value) < 0.1) return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  if (Math.abs(value) < 10) return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
  return value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '')
}

// ─── Sub-components ───────────────────────────────────────────────────────────
function MetricCard({
  label,
  value,
  color,
  source,
}: {
  label: string
  value: string
  color: string
  source?: NetworkMetrics['dataSource']
}) {
  return (
    <div className="bg-slate-50 border border-slate-200 rounded p-2 text-center">
      <div className="flex items-center justify-center gap-1">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider">{label}</p>
        {source && <DataSourceBadge source={source} />}
      </div>
      <p className={`text-base font-bold ${color}`}>{value}</p>
    </div>
  )
}

function PacketJourney({
  metrics,
  activeEvent,
  targetSlice,
  targetSst,
  targetFiveQi,
  ueId,
  runtimeObserved,
}: {
  metrics: NetworkMetrics | null
  activeEvent: string | null
  targetSlice?: SliceType
  targetSst?: number
  targetFiveQi?: number
  ueId?: string
  runtimeObserved?: boolean
}) {
  const { text } = useLocale()
  const pduSessions = metrics?.pduSessionCount ?? 0
  const gtpPackets = finiteMetric(metrics?.gtpPacketsPerSec)
  const throughput = metrics?.throughputMbps ?? 0
  const latency = metrics?.latencyMs ?? 0
  const observed = pduSessions > 0 || (gtpPackets ?? 0) > 0 || throughput > 0 || metrics?.ueTunProbe?.ready === true
  const trafficEvidenceObserved = runtimeObserved === true || observed
  const sliceLabel = targetSlice ? `${targetSlice} SST=${targetSst ?? '?'}` : 'selected slice'
  const stages = [
    {
      title: text('1 註冊', '1 Registration'),
      path: 'UE -> gNB -> AMF -> AUSF/UDM -> NSSF',
      signal: `${freeText(ueId, 'UE')} identity, AKA auth, slice selection`,
      metric: `${metrics?.registeredUeCount ?? 0} registered UEs`,
      status: (metrics?.registeredUeCount ?? 0) > 0 ? 'observed' : 'waiting',
    },
    {
      title: text('2 PDU 工作階段', '2 PDU Session'),
      path: 'UE -> gNB -> AMF -> SMF -> PCF -> UPF',
      signal: `QoS policy ${targetFiveQi ? `5QI=${targetFiveQi}` : '5QI'}, ${sliceLabel}`,
      metric: `${pduSessions} PDU sessions`,
      status: pduSessions > 0 ? 'observed' : 'waiting',
    },
    {
      title: text('3 上行封包', '3 Uplink Packet'),
      path: 'UE IP packet -> gNB -> N3 GTP-U UDP/2152 -> UPF -> N6',
      signal: 'UPF decapsulates GTP, applies PDR/FAR/QER, forwards IP traffic',
      metric: `${formatCompact(throughput)} Mbps / ${gtpPackets === null ? text('GTP 無可用資料', 'GTP unavailable') : `${formatCompact(gtpPackets)} GTP pkt/s`}`,
      status: observed ? 'observed' : 'waiting',
    },
    {
      title: text('4 下行回程', '4 Downlink Return'),
      path: 'Internet/DN -> UPF -> N3 GTP-U -> gNB -> UE',
      signal: activeEvent ? `${activeEvent} response traffic on live bearer` : 'return traffic on established bearer',
      metric: `${formatCompact(latency)} ms latency`,
      status: latency > 0 ? 'observed' : 'waiting',
    },
  ] as const

  return (
    <div className="shrink-0 rounded border border-slate-200 bg-slate-50 p-2 text-xs">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500">{text('封包旅程', 'Packet Journey')}</p>
        <div className="flex items-center gap-1.5">
          <span className={trafficEvidenceObserved ? 'text-[10px] font-bold text-green-700' : 'text-[10px] text-slate-400'}>
            {runtimeObserved ? text('AI 規劃前已觀測流量', 'pre-plan traffic observed') : trafficEvidenceObserved ? text('已量測流量', 'traffic measured') : text('等待流量證據', 'waiting traffic evidence')}
          </span>
          {metrics?.dataSource && <DataSourceBadge source={metrics.dataSource} />}
          {metrics?.evidenceLevel && <EvidenceLevelBadge level={metrics.evidenceLevel} />}
        </div>
      </div>
      <div className="space-y-1.5">
        {stages.map((stage) => (
          <div key={stage.title} className="rounded border border-slate-200 bg-white px-2 py-1.5">
            <div className="flex items-center justify-between gap-2">
              <p className="font-bold text-slate-700">{stage.title}</p>
              <span className={stage.status === 'observed' ? 'text-green-700' : 'text-slate-400'}>
                {stage.status === 'observed' ? text('已觀測', 'observed') : text('等待中', 'waiting')}
              </span>
            </div>
            <p className="mt-0.5 text-[10px] font-mono text-blue-700">{stage.path}</p>
            <p className="mt-0.5 text-[10px] text-slate-500">{stage.signal}</p>
            <p className="mt-0.5 text-[10px] font-semibold text-slate-700">{stage.metric}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

function DataSourceBadge({ source }: { source: NonNullable<NetworkMetrics['dataSource']> }) {
  const label =
    source === 'prometheus' || source === 'eks+prometheus'
      ? 'LIVE'
      : source === 'free5gc-oam+iperf3'
        ? 'OAM+IPERF'
      : source === 'eks+iperf3'
        ? 'IPERF'
      : source === 'eks+ueransim-logs'
        ? 'UE'
      : source === 'eks+ue-tun-probe'
        ? 'UE-PROBE'
      : source === 'free5gc-oam'
        ? 'OAM'
      : source === 'eks'
        ? 'EKS'
        : source === 'free5gc'
          ? '5GC'
          : 'N/A'
  const color =
    source === 'prometheus' || source === 'eks+prometheus'
      ? 'text-green-700 bg-green-50 border-green-200'
      : source === 'free5gc-oam+iperf3' || source === 'eks+iperf3'
        ? 'text-blue-700 bg-blue-50 border-blue-200'
      : source === 'eks+ueransim-logs'
        ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
      : source === 'eks+ue-tun-probe'
        ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
      : source === 'free5gc-oam'
        ? 'text-indigo-700 bg-indigo-50 border-indigo-200'
      : source === 'eks'
        ? 'text-cyan-700 bg-cyan-50 border-cyan-200'
      : source === 'free5gc'
        ? 'text-blue-700 bg-blue-50 border-blue-200'
        : 'text-slate-500 bg-slate-50 border-slate-200'
  return (
    <span className={`rounded border px-1 py-0 text-[9px] leading-3 ${color}`}>
      {label}
    </span>
  )
}

export function loadSourceBadgeContent(source: string, locale: 'zh-TW' | 'en' = 'zh-TW'): { label: string; color: string } | null {
  if (source === 'estimated-from-registered-ues') {
    return { label: locale === 'zh-TW' ? '推估' : 'estimated', color: 'text-amber-700 bg-amber-50 border-amber-200' }
  }
  if (source === 'prometheus') {
    return { label: locale === 'zh-TW' ? '實測' : 'measured', color: 'text-green-700 bg-green-50 border-green-200' }
  }
  if (source === 'eks-runtime-logs') {
    return { label: locale === 'zh-TW' ? '實測(UE log)' : 'measured (UE log)', color: 'text-green-700 bg-green-50 border-green-200' }
  }
  return null
}

function LoadSourceBadge({ source }: { source: string }) {
  const { locale } = useLocale()
  const content = loadSourceBadgeContent(source, locale)
  if (!content) return null
  return (
    <span className={`rounded border px-1 py-0 text-[9px] leading-3 ${content.color}`}>
      {content.label}
    </span>
  )
}

export function selectionStageBadgeContent(stage: string, locale: 'zh-TW' | 'en' = 'zh-TW'): { label: string; color: string } | null {
  if (stage === 'active-session') {
    return { label: locale === 'zh-TW' ? '會話中' : 'in session', color: 'text-green-700 bg-green-50 border-green-200' }
  }
  if (stage === 'configured') {
    return { label: locale === 'zh-TW' ? '已定義' : 'configured', color: 'text-slate-500 bg-slate-50 border-slate-200' }
  }
  return null
}

function SelectionStageBadge({ stage }: { stage: string }) {
  const { locale } = useLocale()
  const content = selectionStageBadgeContent(stage, locale)
  if (!content) return null
  return (
    <span className={`rounded border px-1 py-0 text-[9px] leading-3 ${content.color}`}>
      {content.label}
    </span>
  )
}

export function finiteMetric(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

export function formatOptionalGtp(value: unknown, text: (zh: string, en: string) => string): string {
  const numeric = finiteMetric(value)
  return numeric === null ? text('無可用資料', 'unavailable') : formatCompact(numeric)
}

export function evidenceLevelBadgeStyle(level: string): string {
  if (level === 'measured') return 'text-green-700 bg-green-50 border-green-200'
  if (level === 'estimated') return 'text-amber-700 bg-amber-50 border-amber-200'
  if (level === 'fallback') return 'text-slate-500 bg-slate-50 border-slate-200'
  if (level === 'demo') return 'text-purple-700 bg-purple-50 border-purple-200'
  return ''
}

function EvidenceLevelBadge({ level }: { level: string }) {
  const style = evidenceLevelBadgeStyle(level)
  if (!style) return null
  return (
    <span className={`rounded border px-1 py-0 text-[9px] leading-3 ${style}`}>
      {level}
    </span>
  )
}

function freeText(value: string | undefined, fallback: string): string {
  return value && value.trim() ? value : fallback
}

function formatCompact(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (Math.abs(value) >= 100) return value.toFixed(0)
  if (Math.abs(value) >= 10) return value.toFixed(1)
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function TrendArrow({ trend }: { trend: 'up' | 'down' | 'stable' }) {
  if (trend === 'up')   return <span className="text-red-600">↑</span>
  if (trend === 'down') return <span className="text-green-700">↓</span>
  return <span className="text-slate-500">→</span>
}

function PodRow({
  label,
  pods,
  color,
  cpu,
}: {
  label: string
  pods: { name: string; phase: string; reason?: string }[]
  color: string
  cpu?: number
}) {
  const running = pods.filter((p) => p.phase === 'Running').length
  const failed = pods.filter((p) => p.phase === 'Failed').length
  const pending = pods.filter((p) => p.phase === 'Pending').length
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-14 shrink-0 truncate text-right text-[10px] text-slate-500" title={label}>{label}</span>
      <div className="flex gap-1 flex-wrap">
        {pods.map((p) => (
          <span
            key={p.name}
            title={`${p.name} - ${p.phase}${p.reason ? `: ${p.reason}` : ''}`}
            className="w-3 h-3 rounded-sm"
            style={{
              background:
                p.phase === 'Running'     ? color
                : p.phase === 'Pending'   ? '#ca8a04'
                : '#7f1d1d',
            }}
          />
        ))}
        {pods.length === 0 && <span className="text-slate-300">—</span>}
      </div>
      <span className="text-slate-600 text-[10px]">
        {running} running{pending ? ` / ${pending} pending` : ''}{failed ? ` / ${failed} failed` : ''}
      </span>
      <span className="ml-auto text-[10px] text-slate-500">{cpu === undefined ? 'cpu n/a' : `${cpu.toFixed(1)}%`}</span>
    </div>
  )
}
