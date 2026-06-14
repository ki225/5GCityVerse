import { useEffect, useRef } from 'react'
import { useAppStore } from '../../store/appStore'
import { CITY_NODES, CITY_LINKS, SLICE_COLOR } from './cityData'
import type { PacketFlow } from '../../types'
import * as d3 from 'd3'

// 5QI Priority & Color Mapping
const QI_COLORS: Record<number, string> = {
  1: '#ef4444',  // URLLC - Red (highest priority)
  2: '#f97316',  // V2X - Orange
  3: '#22c55e',  // mMTC - Green
  9: '#3b82f6',  // eMBB - Blue (lowest priority)
}

// 5QI Flicker Frequency (Hz)
const QI_FLICKER: Record<number, number> = {
  1: 6,   // URLLC - fast flicker
  2: 4,
  3: 1,
  9: 0,   // eMBB - no flicker
}

export function CanvasCityMap() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const { pods, packetFlows, activeEvent, metrics } = useAppStore()
  const animationFrameRef = useRef<number | null>(null)
  const timeRef = useRef(0)

  // Calculate pod counts
  const podCount: Record<string, number> = {}
  pods.forEach((c) => {
    podCount[c.component] = c.pods.filter((p) => p.phase === 'Running').length
  })

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // Setup canvas resolution
    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    ctx.scale(dpr, dpr)

    // Node map for lookups
    const nodeMap: Record<string, any> = {}
    CITY_NODES.forEach((n) => {
      nodeMap[n.id] = n
    })

    // Animation loop
    let lastTime = Date.now()
    const animate = () => {
      const now = Date.now()
      const deltaTime = (now - lastTime) / 1000 // seconds
      lastTime = now
      timeRef.current += deltaTime

      // Clear canvas
      ctx.fillStyle = 'rgba(10, 10, 20, 1)'
      ctx.fillRect(0, 0, rect.width, rect.height)

      // Draw static links first
      drawStaticLinks(ctx, rect.width, rect.height)

      // Draw packet flows with multi-dimensional animation
      packetFlows.forEach((flow) => {
        if (!flow.active) return
        const src = nodeMap[flow.sourceNodeId]
        const tgt = nodeMap[flow.targetNodeId]
        if (!src || !tgt) return

        drawPacketFlow(
          ctx,
          src,
          tgt,
          flow,
          timeRef.current,
          rect
        )
      })

      // Draw nodes on top
      drawNodes(ctx, podCount, packetFlows, rect)

      animationFrameRef.current = requestAnimationFrame(animate)
    }

    animate()

    return () => {
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current)
      }
    }
  }, [pods, packetFlows, activeEvent, metrics])

  return (
    <div className="panel h-full flex flex-col">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-sm font-bold text-slate-300 tracking-wider uppercase">
          City Map (Multi-Dimensional Flow)
        </h2>
        <div className="flex gap-3 text-xs">
          {(['eMBB', 'URLLC', 'mMTC', 'V2X'] as const).map((s) => (
            <span key={s} className="flex items-center gap-1">
              <span
                className="w-2.5 h-2.5 rounded-full inline-block"
                style={{ background: SLICE_COLOR[s] }}
              />
              {s}
            </span>
          ))}
        </div>
      </div>
      <canvas
        ref={canvasRef}
        className="w-full flex-1 cursor-default"
        style={{
          background: 'linear-gradient(135deg, rgba(10, 10, 20, 1) 0%, rgba(20, 20, 40, 1) 100%)',
        }}
      />
    </div>
  )
}

// ─── Draw Static Links ────────────────────────────────────────────────────────
function drawStaticLinks(ctx: CanvasRenderingContext2D, w: number, h: number) {
  ctx.strokeStyle = 'rgba(30, 41, 59, 0.5)'
  ctx.lineWidth = 1
  ctx.setLineDash([])

  CITY_LINKS.forEach(({ from, to }) => {
    const srcNode = CITY_NODES.find((n) => n.id === from)
    const tgtNode = CITY_NODES.find((n) => n.id === to)
    if (!srcNode || !tgtNode) return

    // Scale coordinates to canvas
    const sx = (srcNode.x / 700) * w
    const sy = (srcNode.y / 480) * h
    const tx = (tgtNode.x / 700) * w
    const ty = (tgtNode.y / 480) * h

    ctx.beginPath()
    ctx.moveTo(sx, sy)
    ctx.lineTo(tx, ty)
    ctx.stroke()
  })
}

// ─── Draw Packet Flow with Multi-Dimensional Animation ────────────────────────
function drawPacketFlow(
  ctx: CanvasRenderingContext2D,
  src: any,
  tgt: any,
  flow: PacketFlow,
  time: number,
  rect: DOMRect
) {
  // Scale coordinates
  const w = rect.width
  const h = rect.height
  const sx = (src.x / 700) * w
  const sy = (src.y / 480) * h
  const tx = (tgt.x / 700) * w
  const ty = (tgt.y / 480) * h

  // ── Dimension 1: Bandwidth → Line Width & Particle Density ──────────────────
  const bandwidthMbps = flow.bandwidthMbps ?? 100
  const baseLineWidth = 1
  const lineWidth = baseLineWidth + (bandwidthMbps / 1000) * 3 // 0-1000 Mbps → 1-4 px
  const particleCount = Math.max(1, Math.ceil(bandwidthMbps / 150)) // 1 particle per 150 Mbps

  // ── Dimension 3: Priority (5QI) → Color & Flicker ──────────────────────────
  const fiveQi = flow.fiveQi ?? 9 // Default to eMBB
  const qiColor = QI_COLORS[fiveQi] || SLICE_COLOR[flow.sliceType]
  const flickerFreq = QI_FLICKER[fiveQi] || 0

  // ── Dimension 2: Latency → Particle Speed & Jitter ────────────────────────
  const latencyMs = flow.latencyMs ?? 50
  const particleSpeed = 1 / (1 + latencyMs / 100) // Higher latency = slower
  const jitterAmount = latencyMs > 50 ? Math.sin(time * 10) * (w * 0.01) : 0

  // ── Dimension 4: Packet Loss → Ghost Particles & Visibility ──────────────────
  const packetLossPercent = flow.packetLossPercent ?? 0
  const dropProbability = packetLossPercent / 100

  // ── Dimension 5: UPF Congestion → Path Color Shift ───────────────────────────
  const upfCongestion = (flow.upfCongestionPercent ?? 0) / 100
  let pathColor = qiColor

  if (upfCongestion > 0.7) {
    // Color transition: original → red
    const congestionScale = d3
      .scaleLinear<string, string>()
      .domain([0.7, 1])
      .range([qiColor, '#ff0000'])
      .clamp(true)
    pathColor = congestionScale(upfCongestion)
  }

  // ── Draw Main Flow Line ───────────────────────────────────────────────────────
  ctx.strokeStyle = pathColor
  ctx.globalAlpha = 0.4 + (1 - upfCongestion) * 0.3
  ctx.lineWidth = lineWidth

  // Congestion causes pulsing
  if (upfCongestion > 0.7) {
    const pulseAmount = Math.sin(time * 8) * 1
    ctx.lineWidth = lineWidth + pulseAmount
  }

  ctx.setLineDash(packetLossPercent > 5 ? [8, 4] : [])
  ctx.beginPath()
  ctx.moveTo(sx, sy)
  ctx.lineTo(tx, ty)
  ctx.stroke()
  ctx.setLineDash([])

  // ── Draw Particle Flow ─────────────────────────────────────────────────────────
  for (let i = 0; i < particleCount; i++) {
    const offset = (time * particleSpeed + (i / particleCount)) % 1 // Progress along path

    // Calculate particle position
    let px = sx + (tx - sx) * offset
    let py = sy + (ty - sy) * offset

    // Add jitter (latency effect)
    px += jitterAmount
    py += jitterAmount

    // Determine particle color & visibility (loss effect)
    let particleColor = pathColor
    let particleAlpha = 0.7

    if (Math.random() < dropProbability) {
      // Ghost particle (packet loss)
      particleColor = '#ff6b6b'
      particleAlpha = 0.2
    }

    // Flicker effect (5QI priority)
    if (flickerFreq > 0) {
      const flicker = Math.sin(time * flickerFreq * Math.PI * 2) * 0.5 + 0.5
      particleAlpha *= (0.5 + flicker * 0.5)
    }

    // Draw particle
    ctx.fillStyle = particleColor
    ctx.globalAlpha = particleAlpha

    const particleRadius = 3 + (lineWidth - baseLineWidth) * 0.5
    ctx.beginPath()
    ctx.arc(px, py, particleRadius, 0, Math.PI * 2)
    ctx.fill()
  }

  ctx.globalAlpha = 1
}

// ─── Draw Nodes ───────────────────────────────────────────────────────────────
function drawNodes(
  ctx: CanvasRenderingContext2D,
  podCount: Record<string, number>,
  flows: PacketFlow[],
  rect: DOMRect
) {
  const w = rect.width
  const h = rect.height

  CITY_NODES.forEach((node) => {
    const sx = (node.x / 700) * w
    const sy = (node.y / 480) * h

    // Determine node radius
    let radius = 0
    if (node.type === 'district') radius = 22
    else if (node.type === 'gnb') radius = 12
    else if (node.type === 'core') radius = 16
    else if (node.type === 'upf') {
      const count = podCount['UPF'] ?? 1
      radius = 14 + count * 3
    }

    // Determine fill color
    let fillColor = '#0f172a'
    switch (node.type) {
      case 'district':
        fillColor = '#1e3a5f'
        break
      case 'gnb':
        fillColor = '#1e293b'
        break
      case 'core':
        fillColor = '#312e81'
        break
      case 'upf':
        fillColor = '#164e63'
        break
    }

    // Determine stroke color (highlight active flows)
    let strokeColor = '#334155'
    const activeFlows = flows.filter(
      (f) => (f.sourceNodeId === node.id || f.targetNodeId === node.id) && f.active
    )
    if (activeFlows.length > 0) {
      strokeColor = SLICE_COLOR[activeFlows[0].sliceType]
    }

    // Draw node circle
    ctx.fillStyle = fillColor
    ctx.strokeStyle = strokeColor
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.arc(sx, sy, radius, 0, Math.PI * 2)
    ctx.fill()
    ctx.stroke()

    // UPF pod count badge
    if (node.type === 'upf' && podCount['UPF'] > 1) {
      ctx.fillStyle = '#fff'
      ctx.font = 'bold 10px monospace'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(`×${podCount['UPF']}`, sx, sy)
    }

    // Node label
    ctx.fillStyle = '#94a3b8'
    ctx.font = '11px monospace'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    ctx.fillText(node.label, sx, sy + radius + 12)
  })
}
