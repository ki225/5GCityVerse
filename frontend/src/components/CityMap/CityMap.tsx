import { useEffect, useRef } from 'react'
import * as d3 from 'd3'
import { useAppStore } from '../../store/appStore'
import { CITY_NODES, CITY_LINKS, SLICE_COLOR } from './cityData'
import type { CityNode, PacketFlow } from '../../types'

const W = 700
const H = 480

export function CityMap() {
  const svgRef = useRef<SVGSVGElement>(null)
  const { pods, packetFlows } = useAppStore()

  // Map component → pod count for size scaling
  const podCount: Record<string, number> = {}
  pods.forEach((c) => { podCount[c.component] = c.pods.filter(p => p.phase === 'Running').length })

  useEffect(() => {
    const svg = d3.select(svgRef.current)

    // ── Draw static links ────────────────────────────────────────────────────
    const nodeMap = Object.fromEntries(CITY_NODES.map((n) => [n.id, n]))

    svg.selectAll('.static-link').remove()
    svg
      .selectAll('.static-link')
      .data(CITY_LINKS)
      .enter()
      .append('line')
      .attr('class', 'static-link')
      .attr('x1', (d) => nodeMap[d.from].x)
      .attr('y1', (d) => nodeMap[d.from].y)
      .attr('x2', (d) => nodeMap[d.to].x)
      .attr('y2', (d) => nodeMap[d.to].y)
      .attr('stroke', '#1e293b')
      .attr('stroke-width', 1.5)

    // ── Draw packet flow paths ────────────────────────────────────────────────
    svg.selectAll('.packet-path').remove()
    packetFlows.forEach((flow) => {
      if (!flow.active) return
      const src = nodeMap[flow.sourceNodeId]
      const tgt = nodeMap[flow.targetNodeId]
      if (!src || !tgt) return
      svg
        .append('line')
        .attr('class', 'packet-path')
        .attr('x1', src.x)
        .attr('y1', src.y)
        .attr('x2', tgt.x)
        .attr('y2', tgt.y)
        .attr('stroke', SLICE_COLOR[flow.sliceType])
        .attr('stroke-width', 2.5)
        .attr('opacity', 0.7)
        .attr('class', `packet-path packet-flow`)
        .style('stroke-dasharray', '8 6')
    })

    // ── Draw nodes ────────────────────────────────────────────────────────────
    svg.selectAll('.city-node').remove()
    svg.selectAll('.city-label').remove()

    CITY_NODES.forEach((node) => {
      const g = svg.append('g').attr('transform', `translate(${node.x},${node.y})`)

      const radius = nodeRadius(node)
      const fill = nodeFill(node)
      const stroke = nodeStroke(node, packetFlows)

      // Glow ring if UPF is scaling
      const upfRunning = podCount['UPF'] ?? 0
      if (node.type === 'upf' && upfRunning > 1) {
        g.append('circle')
          .attr('class', 'city-node node-scaling')
          .attr('r', radius + 8)
          .attr('fill', 'none')
          .attr('stroke', SLICE_COLOR['eMBB'])
          .attr('stroke-width', 1.5)
          .attr('opacity', 0.4)
          .style('color', SLICE_COLOR['eMBB'])
      }

      g.append('circle')
        .attr('class', 'city-node')
        .attr('r', radius)
        .attr('fill', fill)
        .attr('stroke', stroke)
        .attr('stroke-width', 2)

      // UPF pod count badge
      if (node.type === 'upf' && upfRunning > 1) {
        g.append('text')
          .attr('class', 'city-label')
          .attr('text-anchor', 'middle')
          .attr('dy', '0.35em')
          .attr('font-size', '10px')
          .attr('font-weight', 'bold')
          .attr('fill', '#fff')
          .text(`×${upfRunning}`)
      }

      g.append('text')
        .attr('class', 'city-label')
        .attr('text-anchor', 'middle')
        .attr('dy', radius + 14)
        .attr('font-size', '11px')
        .attr('fill', '#94a3b8')
        .text(node.label)

      // Node type icon
      if (node.type === 'gnb') {
        g.append('text')
          .attr('text-anchor', 'middle')
          .attr('dy', '0.35em')
          .attr('font-size', '10px')
          .attr('fill', '#e2e8f0')
          .text('📡')
      } else if (node.type === 'core') {
        g.append('text')
          .attr('text-anchor', 'middle')
          .attr('dy', '0.35em')
          .attr('font-size', '10px')
          .attr('fill', '#e2e8f0')
          .text('🧠')
      }
    })
  }, [pods, packetFlows, podCount])

  return (
    <div className="panel h-full flex flex-col">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-sm font-bold text-slate-300 tracking-wider uppercase">City Map</h2>
        <div className="flex gap-3 text-xs">
          {(['eMBB', 'URLLC', 'mMTC', 'V2X'] as const).map((s) => (
            <span key={s} className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: SLICE_COLOR[s] }} />
              {s}
            </span>
          ))}
        </div>
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full flex-1"
        style={{ background: 'transparent' }}
      />
    </div>
  )
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function nodeRadius(node: CityNode): number {
  if (node.type === 'district') return 28
  if (node.type === 'gnb') return 16
  if (node.type === 'core') return 22
  if (node.type === 'upf') return 22
  return 18
}

function nodeFill(node: CityNode): string {
  switch (node.type) {
    case 'district': return '#1e3a5f'
    case 'gnb':      return '#1e293b'
    case 'core':     return '#312e81'
    case 'upf':      return '#164e63'
    default:         return '#0f172a'
  }
}

function nodeStroke(node: CityNode, flows: PacketFlow[]): string {
  if (node.type === 'upf') {
    const active = flows.filter(f => f.sourceNodeId === 'upf' || f.targetNodeId === 'upf')
    if (active.length > 0) return SLICE_COLOR[active[0].sliceType]
  }
  if (node.type === 'core') return '#6366f1'
  if (node.type === 'gnb')  return '#38bdf8'
  return '#334155'
}
