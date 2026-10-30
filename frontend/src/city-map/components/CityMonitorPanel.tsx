import { useEffect, useMemo, useRef, useState } from 'react'
import { useCityDemand } from '../context/CityDemandContext'
import { useCityMonitor } from '../context/CityMonitorContext'
import { DataCenter, Gnb } from '../domain/model'
import { InfraCard } from './InfraCard'
import { HttpInfraSource } from '../services/infra'
import { apiBaseUrl } from '../../services/runtimeConfig'
import { sessionHeaders } from '../../services/browserSession'
import { demandNames } from '../domain/demand'
import type { ScenarioGroup } from '../../domain/scenarios'
import { freshUntil, healthLabels, healthSymbols, reasonLabels, segmentState, siteState } from '../services/observability'

export function CityMonitorPanel({ onClose }: { onClose?: () => void }) {
  const monitor = useCityMonitor()!, city = useCityDemand()!
  const [podSite, setPodSite] = useState<string | null>(null)
  const source = useMemo(() => new HttpInfraSource(`${apiBaseUrl()}/api/onprem/city-infra`, sessionHeaders), [])
  const object = monitor.selectedId ? city.map.get(monitor.selectedId) : null
  const site = monitor.snapshot?.infra.find(s => s.id === monitor.selectedId)
  const paths = monitor.snapshot?.paths.filter(p => monitor.pathId ? p.id === monitor.pathId : p.sourceLocation.buildingId === monitor.selectedId) ?? []
  const state = siteState(site, monitor.now, Boolean(monitor.error))
  const label = (id: string | null) => id ? city.map.get(id)?.label ?? '位置尚未確認' : '位置尚未確認'
  const closeButton = useRef<HTMLButtonElement>(null)
  const reset = () => { monitor.setSelectedId(null); monitor.setPathId(null); onClose?.() }
  const closeAction = useRef(reset)
  closeAction.current = reset
  useEffect(() => {
    closeButton.current?.focus({ preventScroll: true })
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); closeAction.current() } }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [])
  const age = (at: string | null) => at ? `觀測於 ${new Date(at).toLocaleTimeString('zh-TW')}` : '尚無觀測'
  return <div className="city-monitor-panel" data-testid="city-monitor-panel">
    <div className="city-monitor-heading"><div><p className="city-eyebrow">NETWORK MONITOR</p><h2>{object?.label ?? '城市網路監看'}</h2></div><button ref={closeButton} onClick={reset} aria-label="關閉網路資訊卡">×</button></div>
    {monitor.error && <p role="status" className="city-monitor-notice">{monitor.error}。目前狀態顯示未知；城市需求仍可編輯。</p>}
    {object instanceof Gnb || object instanceof DataCenter ? <>
      <div className="city-monitor-section"><h3>站點 NF 健康</h3>
        <p><span className="city-health-inline" data-health={state}>{healthSymbols[state]} {healthLabels[state]}</span>　{site?.readyReplicas ?? '—'} / {site?.desiredReplicas ?? '—'} Ready</p>
        <p>{state === 'unknown' ? '觀測資料缺失或已逾期' : reasonLabels[site?.reason ?? ''] ?? '工作負載觀測'} · {age(site?.observedAt ?? null)}</p>
        <p className="city-monitor-muted">來源：Kubernetes 工作負載；健康燈不代表居民 SLA 已達標。</p>
        {site?.workloads.map(w => <div className="city-monitor-row" key={w.id}><div><strong>{w.function}</strong><small>{w.id}</small></div><span>{w.readyReplicas ?? '—'} / {w.desiredReplicas ?? '—'} Ready<br/><small>{state === 'unknown' ? '未知' : reasonLabels[w.reason] ?? healthLabels[w.health]}</small></span></div>)}
      </div>
      <details key={object.id} open={podSite === object.id} onToggle={e => setPodSite(e.currentTarget.open ? object.id : null)} className="city-monitor-pods"><summary>Pod 明細與資源配置</summary>{podSite === object.id && <InfraCard key={object.id} target={object} source={source} onClose={reset}/>}</details>
    </> : <>
      {object && <div className="city-monitor-actions"><button onClick={() => monitor.setMode('demand')}>編輯這棟建築需求</button><span>以下為目前輪次觀測，與下一輪草稿分開。</span></div>}
      {!paths.length && <p className="city-monitor-empty">{object ? '目前沒有此建築可顯示的服務路徑。啟動需求後，等待原生連線與流量觀測。' : '點選建築查看服務路徑，或點選 infra 健康燈查看 NF 狀態。'}</p>}
      {paths.map(path => {
        const traffic = !monitor.error && path.traffic && freshUntil(path.traffic.validUntil, monitor.now) ? path.traffic : null
        return <article key={path.id} className="city-monitor-section">
          <h3>{demandNames[path.profileId as ScenarioGroup] ?? path.profileId} · {path.slice}</h3>
          <p>{path.deviceCount ?? '—'} UE · {path.sessionCount ?? '—'} sessions{path.state === 'configured' ? ' · 配置預計路徑' : ' · 原生服務關聯'}</p>
          <ol className="city-monitor-hops"><li>{label(path.sourceLocation.buildingId ?? null)}</li>{path.segments.map((s, i) => <li key={i} data-state={segmentState(s, monitor.now, Boolean(monitor.error))}><small>{s.interface === 'access' ? '接入關聯' : s.interface} · {({ verified: '已驗證', configured: '配置預計', unknown: '未知' })[segmentState(s, monitor.now, Boolean(monitor.error))]}</small>{label(s.toId)}{s.interface === 'N6' ? ' / 應用接收端' : ''}</li>)}{!path.application && <li data-state="unknown"><small>N6 · 未驗證</small>應用位置尚未確認</li>}</ol>
          <h3>服務接收流量</h3><div className="city-monitor-metrics"><div>上傳 UL<strong>{traffic?.ulMbps?.toFixed(2) ?? '—'} <small>Mbps</small></strong></div><div>下載 DL<strong>{traffic?.dlMbps?.toFixed(2) ?? '—'} <small>Mbps</small></strong></div></div>
          <p className="city-monitor-muted">{traffic ? `${age(traffic.observedAt)}；量測區間 ${new Date(traffic.windowStart).toLocaleTimeString('zh-TW')}–${new Date(traffic.windowEnd).toLocaleTimeString('zh-TW')}` : '目前沒有有效的接收端流量量測。'}<br/>此為服務流量，並非每條實體鏈路的獨立量測。</p>
        </article>
      })}
    </>}
    <div className="city-monitor-section"><h3>Infra 健康摘要</h3>{city.map.objects.filter(o => o instanceof Gnb || o instanceof DataCenter && o.tier !== 'edge').map(o => {
      const s = monitor.snapshot?.infra.find(s => s.id === o.id), health = siteState(s, monitor.now, Boolean(monitor.error))
      return <button className="city-monitor-site" key={o.id} onClick={() => { monitor.setSelectedId(o.id); monitor.setPathId(null) }}><span className="city-health-inline" data-health={health}>{healthSymbols[health]}</span><span>{o.label}</span><small>{healthLabels[health]} · {s?.readyReplicas ?? '—'} / {s?.desiredReplicas ?? '—'}</small></button>
    })}</div>
  </div>
}
