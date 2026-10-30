import { useEffect, useMemo, useRef } from 'react'
import type { CityMap, Point } from '../domain/model'
import { DataCenter, Gnb } from '../domain/model'
import type { CityMapRenderer } from '../rendering/renderer'
import { useCityMonitor } from '../context/CityMonitorContext'
import { freshUntil, healthLabels, healthSymbols, segmentState, siteState } from '../services/observability'
import { ribbonWaypoints, roundedRibbon } from '../rendering/trafficRibbon'

/** A small projection layer; camera updates move overlays, never recreate city objects. */
export function CityObservationOverlay({ map, renderer }: { map: CityMap; renderer: CityMapRenderer | null }) {
  const monitor = useCityMonitor()
  const root = useRef<HTMLDivElement>(null)
  const animations = useRef(new Map<HTMLElement, Animation>())
  const paths = useMemo(() => monitor?.showPaths && monitor.mode === 'monitor' ? monitor.snapshot?.paths.filter(p =>
    p.id === monitor.pathId || (!monitor.pathId && p.sourceLocation.buildingId === monitor.selectedId)) ?? [] : [],
    [monitor?.showPaths, monitor?.mode, monitor?.snapshot, monitor?.pathId, monitor?.selectedId])
  const sites = map.objects.filter(o => o instanceof Gnb || o instanceof DataCenter)
  useEffect(() => {
    renderer?.setTransportLinksVisible(!(monitor?.showPaths && monitor.mode === 'monitor'))
    return () => renderer?.setTransportLinksVisible(true)
  }, [renderer, monitor?.showPaths, monitor?.mode])
  useEffect(() => () => { animations.current.forEach(animation => animation.cancel()); animations.current.clear() }, [])
  useEffect(() => {
    if (!renderer || !root.current) return
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
    let settleTimer: ReturnType<typeof setTimeout> | null = null
    const stop = (element: HTMLElement) => {
      animations.current.get(element)?.cancel(); animations.current.delete(element)
      delete element.dataset.geometry; element.hidden = true
    }
    const update = () => {
      const bounds = renderer.canvas.getBoundingClientRect()
      const point = (id: string | null, fallback?: Point) => {
        const o = id ? map.get(id) : null
        if (o?.space === 'external') return renderer.projection.unproject({ x: bounds.width - 88, y: 42 })
        return o?.position ?? fallback ?? null
      }
      for (const element of animations.current.keys()) if (!root.current!.contains(element)) stop(element)
      for (const node of root.current!.querySelectorAll<HTMLElement>('[data-lamp-id]')) {
        const o = map.get(node.dataset.lampId!)
        if (!o || o.space === 'external') continue
        const p = renderer.projection.project(o.position, o.size.height)
        node.style.left = `${p.x}px`; node.style.top = `${p.y - 12}px`
      }
      paths.forEach((path, n) => path.segments.forEach((segment, i) => {
        const group = root.current!.querySelector<SVGGElement>(`[data-route-index="${n}-${i}"]`)
        if (!group) return
        const a = point(segment.fromId, i === 0 ? path.sourceLocation.position : undefined), b = point(segment.toId)
        const same = segment.interface === 'N6' && segment.fromId === segment.toId
        const d = a && b ? roundedRibbon(ribbonWaypoints(a, b, same).map(p => renderer.projection.project(p, .04))) : ''
        const core = group.querySelector<SVGPathElement>('.city-observed-route')!
        const moved = core.getAttribute('d') !== d
        group.querySelectorAll('path').forEach(node => node.setAttribute('d', d))
        for (const element of root.current!.querySelectorAll<HTMLElement>(`[data-flow-route="${n}-${i}"]`)) {
          if (!d || document.hidden || reducedMotion.matches) { stop(element); continue }
          // During drag/zoom, move the ribbon immediately and resume light segments
          // after the camera settles; don't rebuild animation keyframes every frame.
          if (moved) {
            stop(element)
            if (settleTimer) clearTimeout(settleTimer)
            settleTimer = setTimeout(() => { settleTimer = null; update() }, 100)
            continue
          }
          const geometry = `${d}:${element.dataset.direction}`
          if (element.dataset.geometry === geometry && animations.current.has(element)) continue
          const previous = animations.current.get(element)
          const phase = typeof previous?.currentTime === 'number' ? previous.currentTime : 0
          previous?.cancel()
          const reverse = element.dataset.direction === 'dl', length = core.getTotalLength()
          if (length < 1) { stop(element); continue }
          // Transform keyframes run without a per-frame JS loop or city repaint.
          const frames = Array.from({ length: 33 }, (_, index) => {
            const distance = length * (reverse ? 1 - index / 32 : index / 32)
            const p = core.getPointAtLength(distance)
            const next = core.getPointAtLength(Math.max(0, Math.min(length, distance + (reverse ? -.5 : .5))))
            const angle = Math.atan2(next.y - p.y, next.x - p.x)
            return { transform: `translate(${p.x - 3.5}px, ${p.y - 1.5}px) rotate(${angle}rad)` }
          })
          element.hidden = false; element.dataset.geometry = geometry
          const animation = element.animate(frames, { duration: 2400, iterations: Infinity, easing: 'linear' })
          animation.currentTime = phase % 2400
          animations.current.set(element, animation)
        }
      }))
    }
    update()
    const unsubscribe = renderer.subscribeFrame(update)
    document.addEventListener('visibilitychange', update)
    reducedMotion.addEventListener('change', update)
    return () => { unsubscribe(); if (settleTimer) clearTimeout(settleTimer); document.removeEventListener('visibilitychange', update); reducedMotion.removeEventListener('change', update) }
  }, [map, renderer, paths, monitor?.now, monitor?.error])
  if (!monitor) return null
  const transmitting = (p: typeof paths[number]) => !monitor.error && p.traffic && freshUntil(p.traffic.validUntil, monitor.now)
  let motionBudget = 12
  const choose = (id: string) => { monitor.setSelectedId(id); monitor.setPathId(null); monitor.setMode('monitor') }
  return <div ref={root} className="city-observation-overlay" aria-label="城市網路觀測">
    <svg className="city-route-layer" aria-label="所選服務路徑">
      {paths.flatMap((p, n) => p.segments.map((s, i) => <g key={`${p.id}-${i}`} data-route-index={`${n}-${i}`}
        className="city-ribbon-group" data-state={segmentState(s, monitor.now, Boolean(monitor.error))}
        role="button" tabIndex={0} aria-label={`${s.interface} 服務路徑`}
        onClick={() => monitor.setPathId(p.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); monitor.setPathId(p.id) } }}>
        <title>{s.interface} · {segmentState(s, monitor.now, Boolean(monitor.error))}</title>
        <path className="city-ribbon-bed"/><path className="city-observed-route"/><path className="city-ribbon-hit"/>
      </g>))}
    </svg>
    {paths.flatMap((p, n) => p.segments.flatMap((s, i) => ['ul', 'dl'].flatMap(direction => {
      const value = direction === 'ul' ? p.traffic?.ulMbps : p.traffic?.dlMbps
      if (!transmitting(p) || segmentState(s, monitor.now) !== 'verified' || !(value && value > 0) || motionBudget <= 0) return []
      motionBudget--
      return <span key={`${p.id}-${i}-${direction}`} className="city-flow-glint" data-flow-route={`${n}-${i}`} data-direction={direction} aria-hidden="true"/>
    })))}
    {sites.filter(site => monitor.showHealth || site.space === 'external' && paths.some(p => p.segments.some(s => s.toId === site.id))).map(site => {
      const health = monitor.snapshot?.infra.find(s => s.id === site.id)
      const state = site instanceof DataCenter && site.tier === 'edge' && !health ? 'reserved' : siteState(health, monitor.now, Boolean(monitor.error))
      return <button key={site.id} type="button" className={`city-health-lamp ${site.space === 'external' ? 'city-external-endpoint' : ''}`}
        data-lamp-id={site.id} data-health={state} onClick={() => choose(site.id)}
        title={`${site.label} · ${healthLabels[state]}`} aria-label={`${site.label} NF 健康：${healthLabels[state]}`}>
        {site.space === 'external' ? <>Central ↗ <span>{healthSymbols[state]}</span></> : healthSymbols[state]}
      </button>
    })}
  </div>
}
