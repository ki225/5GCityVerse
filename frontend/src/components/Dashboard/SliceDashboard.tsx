import { useEffect, useRef } from 'react'
import * as d3 from 'd3'
import { useAppStore } from '../../store/appStore'
import { SLICE_COLOR } from '../CityMap/cityData'
import type { SliceType } from '../../types'

const SLICE_LABEL: Record<SliceType, string> = {
  eMBB: 'eMBB (SST=1)', URLLC: 'URLLC (SST=2)', mMTC: 'mMTC (SST=3)', V2X: 'V2X (SST=4)',
}

export function SliceDashboard() {
  const { slices, metrics, metricsHistory, pods, free5gcStatus } = useAppStore()
  const sparkRef = useRef<SVGSVGElement>(null)

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

  const upfPods = pods.find((c) => c.component === 'UPF')?.pods ?? []
  const amfPods = pods.find((c) => c.component === 'AMF')?.pods ?? []

  return (
    <div className="panel flex flex-col gap-4 h-full overflow-y-auto">
      <h2 className="text-sm font-bold text-slate-300 tracking-wider uppercase shrink-0">
        5GC Dashboard
      </h2>

      <div className="bg-slate-800/60 rounded p-2 text-xs shrink-0">
        <div className="flex items-center justify-between">
          <span className="text-slate-400">free5GC Live</span>
          <div className="flex items-center gap-1.5">
            {metrics?.dataSource && <DataSourceBadge source={metrics.dataSource} />}
            <span className={free5gcStatus?.connected ? 'text-green-400' : 'text-red-400'}>
              {free5gcStatus?.connected ? 'connected' : 'offline'}
            </span>
          </div>
        </div>
        <div className="mt-1 grid grid-cols-2 gap-2 text-[10px] text-slate-500">
          <span>Subscribers</span>
          <span className="text-right text-slate-300">{free5gcStatus?.subscriberCount ?? 0}</span>
          <span>City records</span>
          <span className="text-right text-sky-300">{free5gcStatus?.eventSubscriberCount ?? 0}</span>
          <span>Registered UEs</span>
          <span className="text-right text-green-300">{free5gcStatus?.registeredUeCount ?? 0}</span>
          <span>Profiles</span>
          <span className="text-right text-purple-300">{free5gcStatus?.profileCount ?? 0}</span>
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
          <MetricCard label="Latency" value={`${metrics.latencyMs} ms`} color="text-red-400" source={metrics.dataSource} />
          <MetricCard label="Throughput" value={`${metrics.throughputMbps} Mbps`} color="text-blue-400" source={metrics.dataSource} />
          <MetricCard label="PDU Sessions" value={String(metrics.pduSessionCount)} color="text-green-400" source={metrics.dataSource} />
          <MetricCard label="GTP pkt/s" value={String(metrics.gtpPacketsPerSec)} color="text-orange-400" source={metrics.dataSource} />
        </div>
      )}

      {/* Throughput sparkline */}
      <div className="shrink-0">
        <p className="text-[10px] text-slate-500 mb-1 uppercase tracking-wider">Throughput history</p>
        <svg ref={sparkRef} className="w-full" height={36} />
      </div>

      {/* Slice bars */}
      <div className="space-y-2.5 shrink-0">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider">Network Slices</p>
        {slices.map((s) => (
          <div key={s.sst}>
            <div className="flex items-center justify-between text-xs mb-0.5">
              <span className="font-semibold" style={{ color: SLICE_COLOR[s.type] }}>
                {SLICE_LABEL[s.type]}
              </span>
              <span className="flex items-center gap-1 text-slate-400">
                <TrendArrow trend={s.trend} />
                {s.load}%
              </span>
            </div>
            <div className="w-full bg-slate-800 rounded-full h-2">
              <div
                className="h-2 rounded-full transition-all duration-700"
                style={{ width: `${s.load}%`, background: SLICE_COLOR[s.type], opacity: 0.85 }}
              />
            </div>
            <p className="text-[10px] text-slate-600 mt-0.5">{s.sessions.toLocaleString()} sessions</p>
          </div>
        ))}
      </div>

      {/* Pod status */}
      <div className="shrink-0">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-2">Pod Status</p>
        <div className="space-y-1">
          <PodRow label="UPF" pods={upfPods} color="#0ea5e9" />
          <PodRow label="AMF" pods={amfPods} color="#8b5cf6" />
          {pods
            .filter((c) => c.component !== 'UPF' && c.component !== 'AMF')
            .map((c) => (
              <PodRow key={c.component} label={c.component} pods={c.pods} color="#475569" />
            ))}
        </div>
      </div>
    </div>
  )
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
  source?: 'prometheus' | 'estimated' | 'simulated'
}) {
  return (
    <div className="bg-slate-800/60 rounded p-2 text-center">
      <div className="flex items-center justify-center gap-1">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider">{label}</p>
        {source && <DataSourceBadge source={source} />}
      </div>
      <p className={`text-base font-bold ${color}`}>{value}</p>
    </div>
  )
}

function DataSourceBadge({ source }: { source: 'prometheus' | 'estimated' | 'simulated' }) {
  const label = source === 'prometheus' ? 'LIVE' : source === 'estimated' ? 'EST' : 'SIM'
  const color =
    source === 'prometheus'
      ? 'text-green-300 bg-green-950/70 border-green-800'
      : source === 'estimated'
        ? 'text-yellow-300 bg-yellow-950/70 border-yellow-800'
        : 'text-slate-500 bg-slate-900 border-slate-700'
  return (
    <span className={`rounded border px-1 py-0 text-[9px] leading-3 ${color}`}>
      {label}
    </span>
  )
}

function TrendArrow({ trend }: { trend: 'up' | 'down' | 'stable' }) {
  if (trend === 'up')   return <span className="text-red-400">↑</span>
  if (trend === 'down') return <span className="text-green-400">↓</span>
  return <span className="text-slate-500">→</span>
}

function PodRow({
  label,
  pods,
  color,
}: {
  label: string
  pods: { name: string; phase: string }[]
  color: string
}) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-10 text-slate-400 text-right text-[10px]">{label}</span>
      <div className="flex gap-1 flex-wrap">
        {pods.map((p) => (
          <span
            key={p.name}
            title={`${p.name} — ${p.phase}`}
            className="w-3 h-3 rounded-sm"
            style={{
              background:
                p.phase === 'Running'     ? color
                : p.phase === 'Pending'   ? '#ca8a04'
                : '#7f1d1d',
            }}
          />
        ))}
        {pods.length === 0 && <span className="text-slate-700">—</span>}
      </div>
      <span className="text-slate-600 text-[10px]">{pods.length} running</span>
    </div>
  )
}
