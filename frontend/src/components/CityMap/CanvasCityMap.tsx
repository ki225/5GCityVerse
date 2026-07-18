import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import * as d3 from 'd3'
import { useAppStore } from '../../store/appStore'
import { CITY_NODES, SLICE_COLOR } from './cityData'
import { FlowEdgeModel } from '../../models/FlowEdgeModel'
import { interpolateNetworkSnapshots } from '../../services/networkSnapshot'
import type { CityEventType, FlowEdgeState, NetworkMetrics, NetworkSnapshot, PacketFlow, RuntimePrimeStatus, SliceStatus, SliceType } from '../../types'
import { useLocale } from '../../i18n'

const QI_FLICKER: Record<number, number> = {
  1: 6,
  6: 1,
  9: 0,
  79: 4,
}

const QI_LABEL: Record<number, string> = {
  1: 'GBR delay-critical',
  2: 'GBR emergency telemetry',
  6: 'mMTC non-GBR',
  9: 'Best effort',
  79: 'V2X unicast',
}

const SLICE_QI: Record<SliceType, number> = {
  eMBB: 9,
  URLLC: 1,
  mMTC: 79,
  V2X: 79,
}

// Mirrors backend scenario_flow_metadata() source_node mapping (backend/aws-app/app.py)
// so the map can highlight the node the active event is actually driving traffic to/from.
// Exported for a test that locks every value here to an id present in TOPOLOGY_NODES.
export const EVENT_TARGET_NODE: Record<CityEventType, string> = {
  concert: 'mall',
  medical: 'hospital',
  typhoon: 'hospital',
  iot_surge: 'factory',
  accident: 'highway',
}

// Mirrors event icons in EventConsole.tsx for a consistent event marker on the map.
const EVENT_ICON: Record<CityEventType, string> = {
  concert: '🎤',
  typhoon: '🌀',
  accident: '🚗',
  medical: '🏥',
  iot_surge: '📡',
}

// Mirrors each event's dominant slice (see EVENTS in EventConsole.tsx) so a batch of
// concurrently active events can be told apart by ring color at their respective target
// node (U1), instead of every marker using the same amber ring.
const EVENT_SLICE_TYPE: Record<CityEventType, SliceType> = {
  concert: 'eMBB',
  typhoon: 'URLLC',
  accident: 'V2X',
  medical: 'URLLC',
  iot_surge: 'mMTC',
}

// Requested iperf rate before runtime evidence is available. These values are
// informational targets only; the map never renders them as packet particles.
export const EVENT_REQUEST_MBPS: Record<CityEventType, number> = {
  concert: 800,
  medical: 10,
  typhoon: 5,
  iot_surge: 2.4,
  accident: 150,
}

type NodeKind = 'scenario' | 'ran' | 'user-plane' | 'control-plane' | 'data' | 'k8s'

interface TopologyNode {
  id: string
  label: string
  sublabel: string
  x: number
  y: number
  w: number
  h: number
  kind: NodeKind
  component?: string
  sliceTypes?: SliceType[]
}

export const NF_ROLE: Record<string, { receives: string; does: string; handsTo: string }> = {
  gnb1: { receives: 'UE 的無線訊號與資料', does: '作為 RAN 基地台，把無線連線接到 5G 核心網；gNB 不是 5GC NF', handsTo: '控制訊息給 AMF，真正的上網資料給 UPF' },
  amf: { receives: 'gNB 的註冊、移動與連線請求', does: '確認 UE 要連線、移動或建立服務', handsTo: '驗證交給 AUSF/UDM，工作階段交給 SMF' },
  smf: { receives: 'AMF 的 PDU 工作階段需求與 PCF 政策', does: '建立會話、選 UPF 並設定資料路徑', handsTo: '規則交給 UPF，結果回覆 AMF' },
  upf: { receives: 'gNB 的使用者封包與 SMF 的轉送規則', does: '套用 QoS、轉送與統計真正的上網資料', handsTo: '送往 Internet/MEC，回程再交回 gNB' },
  pcf: { receives: '服務需求、訂閱資料與網路狀態', does: '決定 QoS、優先權與流量政策', handsTo: '政策交給 SMF／AMF 執行' },
  nssf: { receives: 'AMF 的切片選擇需求', does: '依 UE 與區域選出可用 Slice', handsTo: '選擇結果交回 AMF' },
  nrf: { receives: 'NF 的註冊與服務查詢', does: '像電話簿一樣登記、尋找網路功能', handsTo: '把可用 NF 位址交給查詢者' },
  udm: { receives: 'AMF 的身分與驗證需求（畫面因空間將兩個 NF 合併）', does: 'UDM 像會員資料管理員，管理訂閱與驗證資料；AUSF 像驗證櫃台，執行 UE 身分驗證', handsTo: 'AUSF 把驗證結果交回 AMF；UDM 提供驗證與用戶資料' },
  udr: { receives: 'UDM／PCF 的資料讀寫要求', does: '保存用戶與政策資料', handsTo: '把查詢資料交回提出要求的 NF' },
  nef: { receives: '外部應用的網路能力與政策需求', does: '安全地把外部需求轉成 5GC 可用的 API', handsTo: '政策交給 PCF／SMF，結果回給應用' },
}

export const NODE_HOTSPOT_BASE_CLASS = 'group absolute block -translate-x-1/2 -translate-y-1/2 rounded-md'

interface PodRuntime {
  running: number
  desired: number
  total: number
}

interface StaticLink {
  from: string
  to: string
  type: 'access' | 'n2' | 'n3' | 'n4' | 'sbi' | 'data' | 'observe'
  label?: string
}

export const TOPOLOGY_NODES: TopologyNode[] = [
  { id: 'mall', label: 'Mall UEs', sublabel: 'eMBB AR media', x: 95, y: 165, w: 78, h: 48, kind: 'scenario', sliceTypes: ['eMBB'] },
  { id: 'factory', label: 'Factory IoT', sublabel: 'mMTC sensors', x: 95, y: 265, w: 78, h: 48, kind: 'scenario', sliceTypes: ['mMTC', 'URLLC'] },
  { id: 'hospital', label: 'Hospital', sublabel: 'URLLC medical', x: 95, y: 365, w: 78, h: 48, kind: 'scenario', sliceTypes: ['URLLC'] },
  { id: 'residential', label: '市民手機', sublabel: 'eMBB phone', x: 95, y: 465, w: 78, h: 48, kind: 'scenario', sliceTypes: ['eMBB'] },
  { id: 'highway', label: 'Highway V2X', sublabel: 'vehicle UEs', x: 95, y: 565, w: 78, h: 48, kind: 'scenario', sliceTypes: ['V2X'] },

  { id: 'gnb1', label: 'gNB', sublabel: 'single coverage cell', x: 265, y: 330, w: 82, h: 52, kind: 'ran' },

  { id: 'upf', label: 'UPF', sublabel: 'N3/N6 user plane', x: 470, y: 335, w: 92, h: 56, kind: 'user-plane', component: 'UPF' },
  { id: 'dn', label: 'Data Network', sublabel: 'internet / MEC', x: 470, y: 505, w: 110, h: 52, kind: 'data' },

  { id: 'amf', label: 'AMF', sublabel: 'registration / N2', x: 650, y: 170, w: 82, h: 48, kind: 'control-plane', component: 'AMF' },
  { id: 'smf', label: 'SMF', sublabel: 'PDU session / N4', x: 650, y: 300, w: 82, h: 48, kind: 'control-plane', component: 'SMF' },
  { id: 'pcf', label: 'PCF', sublabel: 'policy / QoS', x: 785, y: 225, w: 82, h: 48, kind: 'control-plane', component: 'PCF' },
  { id: 'nssf', label: 'NSSF', sublabel: 'slice selection', x: 785, y: 355, w: 82, h: 48, kind: 'control-plane', component: 'NSSF' },
  { id: 'nrf', label: 'NRF', sublabel: 'NF discovery', x: 905, y: 170, w: 82, h: 48, kind: 'control-plane', component: 'NRF' },
  { id: 'udm', label: 'UDM/AUSF', sublabel: 'auth / identity', x: 905, y: 300, w: 82, h: 48, kind: 'control-plane', component: 'AUSF' },
  { id: 'udr', label: 'UDR', sublabel: 'subscriber data', x: 905, y: 430, w: 82, h: 48, kind: 'control-plane', component: 'UDR' },
  { id: 'nef', label: 'NEF', sublabel: 'policy exposure', x: 785, y: 505, w: 82, h: 48, kind: 'control-plane', component: 'NEF' },
]

const STATIC_LINKS: StaticLink[] = [
  { from: 'mall', to: 'gnb1', type: 'access' },
  { from: 'hospital', to: 'gnb1', type: 'access' },
  { from: 'residential', to: 'gnb1', type: 'access' },
  { from: 'factory', to: 'gnb1', type: 'access' },
  { from: 'highway', to: 'gnb1', type: 'access' },
  { from: 'gnb1', to: 'amf', type: 'n2', label: 'N2' },
  { from: 'gnb1', to: 'upf', type: 'n3', label: 'N3' },
  { from: 'upf', to: 'dn', type: 'data', label: 'N6' },
  { from: 'smf', to: 'upf', type: 'n4', label: 'N4' },
  { from: 'amf', to: 'smf', type: 'sbi', label: 'SBI' },
  { from: 'amf', to: 'nssf', type: 'sbi' },
  { from: 'amf', to: 'udm', type: 'sbi' },
  { from: 'smf', to: 'pcf', type: 'sbi' },
  { from: 'pcf', to: 'udr', type: 'sbi' },
  { from: 'smf', to: 'nrf', type: 'sbi' },
  { from: 'nef', to: 'pcf', type: 'sbi' },
  { from: 'nef', to: 'smf', type: 'observe' },
]

// Padding (px) reserved inside the canvas on every side so node boxes, labels, and the
// pulsing event marker (ring + emoji, which extends past a node's plain w/h box) never
// touch the canvas edge. Sized to cover the event-marker overflow plus label breathing room.
const MAP_PADDING = 34

// The topology footprint in the original 1000x620 authoring space, expanded by the
// event-marker overflow the pulsing ring/icon can occupy beyond a node's w/h box. Every
// node's authored (x, y) is normalized against these bounds to (nx, ny) in [0, 1], so the
// layout is resolution-independent: pos() below maps (nx, ny) into whatever size the
// canvas is actually measured at (see MapLayout / useMap layout in the component).
const nodeById = Object.fromEntries(TOPOLOGY_NODES.map((node) => [node.id, node]))

// The simulator runs one UERANSIM gNB. Normalize every source and legacy two-gNB
// payload to that single topology node.
const SCENARIO_SOURCE_GNB: Record<string, string> = {
  mall: 'gnb1',
  hospital: 'gnb1',
  residential: 'gnb1',
  factory: 'gnb1',
  highway: 'gnb1',
}

// Mirrors backend scenario_flow_metadata()'s scenario -> source_node mapping, resolved
// through SCENARIO_SOURCE_GNB, so every edge tagged with a given scenario (both the RAN
// access hop AND the N3 gNB->UPF hop) resolves to the SAME gNB. Keying by scenario string
// (rather than by the RAN edge's source/target node id, as the old per-endpoint check did)
// is what lets this reach the N3 segment: N3 edges never have a scenario source node
// (mall/hospital/...) as an endpoint, only "scenario" metadata, so the previous endpoint-based
// check silently skipped them and left N3 on whatever gNB the backend hardcoded (U3a).
const SCENARIO_GNB: Record<string, string> = {
  baseline: SCENARIO_SOURCE_GNB.residential,
  'baseline-embb': SCENARIO_SOURCE_GNB.residential,
  concert: SCENARIO_SOURCE_GNB.mall,
  medical: SCENARIO_SOURCE_GNB.hospital,
  typhoon: SCENARIO_SOURCE_GNB.hospital,
  iot_surge: SCENARIO_SOURCE_GNB.factory,
  accident: SCENARIO_SOURCE_GNB.highway,
}

function isGnbNodeId(nodeId: string): boolean {
  return nodeId === 'gnb1' || nodeId === 'gnb2'
}

// Rewrites either legacy gNB endpoint to the one physical simulator gNB.
export function withCorrectedGnb<T extends { sourceNodeId: string; targetNodeId: string; scenario?: string }>(edge: T): T {
  const correctGnb = edge.scenario ? SCENARIO_GNB[edge.scenario] : undefined
  if (!correctGnb) return edge
  if (isGnbNodeId(edge.sourceNodeId) && edge.sourceNodeId !== correctGnb) return { ...edge, sourceNodeId: correctGnb }
  if (isGnbNodeId(edge.targetNodeId) && edge.targetNodeId !== correctGnb) return { ...edge, targetNodeId: correctGnb }
  return edge
}

// drawEventMarker draws a pulsing ring (up to baseRadius + 6 + 8) and an emoji icon
// above the node, both of which can extend past the node's plain w/h rectangle.
// Any node can be the active event target (EVENT_TARGET_NODE); the normalized bounds
// below reserve this much extra room on every side so a marker is never clipped.
const EVENT_MARKER_MAX_OVERFLOW = 14 + 12

// Raw topology bounding box in the 1000x620 authoring space, expanded by the marker
// overflow. Used to normalize every node's authored (x, y) into [0, 1].
export const TOPO_BOUNDS = {
  minX: Math.min(...TOPOLOGY_NODES.map((n) => n.x - n.w / 2)) - EVENT_MARKER_MAX_OVERFLOW,
  maxX: Math.max(...TOPOLOGY_NODES.map((n) => n.x + n.w / 2)) + EVENT_MARKER_MAX_OVERFLOW,
  minY: Math.min(...TOPOLOGY_NODES.map((n) => n.y - n.h / 2)) - EVENT_MARKER_MAX_OVERFLOW,
  maxY: Math.max(...TOPOLOGY_NODES.map((n) => n.y + n.h / 2)) + EVENT_MARKER_MAX_OVERFLOW,
}
const TOPO_WIDTH = Math.max(1, TOPO_BOUNDS.maxX - TOPO_BOUNDS.minX)
const TOPO_HEIGHT = Math.max(1, TOPO_BOUNDS.maxY - TOPO_BOUNDS.minY)

// Normalizes an authored x/y coordinate to [0, 1] against the topology bounds. This is a
// one-time coordinate-system change: instead of a CSS transform + fit/spread math, every
// draw call resolves node/edge/marker positions through pos() below, which maps the
// normalized coordinate into the live canvas size. Wide canvases therefore spread nodes
// out horizontally (non-uniform, which a topology diagram tolerates) with no side
// whitespace and no left/right clipping.
export function normalizeX(x: number): number {
  return (x - TOPO_BOUNDS.minX) / TOPO_WIDTH
}
export function normalizeY(y: number): number {
  return (y - TOPO_BOUNDS.minY) / TOPO_HEIGHT
}

// Maps a normalized coordinate (nx, ny in [0, 1]) into the padded drawing area of a
// canvas of the given CSS size. pad is inset on every side so nodes never touch the edge.
export function pos(nx: number, ny: number, w: number, h: number, pad: number = MAP_PADDING): { x: number; y: number } {
  const usableW = Math.max(1, w - pad * 2)
  const usableH = Math.max(1, h - pad * 2)
  return { x: pad + nx * usableW, y: pad + ny * usableH }
}

// Clamps a rendered point to stay `inset` px inside the canvas (defends against a node
// box or marker whose half-extent would otherwise poke past the edge).
export function clampToCanvas(x: number, y: number, w: number, h: number, inset = 0): { x: number; y: number } {
  return {
    x: Math.max(inset, Math.min(w - inset, x)),
    y: Math.max(inset, Math.min(h - inset, y)),
  }
}

export function CanvasCityMap() {
  const { locale, text } = useLocale()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const viewportRef = useRef<HTMLDivElement>(null)
  const {
    pods,
    packetFlows,
    metrics,
    slices,
    networkSnapshot,
    previousNetworkSnapshot,
    snapshotTransitionStartedAt,
    snapshotTransitionDurationMs,
    activeEvent,
    activeScenarios,
    runtimePrime,
  } = useAppStore()
  const animationFrameRef = useRef<number | null>(null)
  const timeRef = useRef(0)
  const [zoom, setZoom] = useState(1)
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 })
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [selectedNode, setSelectedNode] = useState<TopologyNode | null>(null)
  // zoom/pan are read by the rAF loop via refs so they never tear down the loop.
  const zoomRef = useRef(zoom)
  zoomRef.current = zoom
  const panRef = useRef(pan)
  panRef.current = pan
  const snapshotRef = useRef<{
    current: NetworkSnapshot | null
    previous: NetworkSnapshot | null
    startedAt: number
    durationMs: number
  }>({ current: null, previous: null, startedAt: 0, durationMs: 900 })
  // Mirrors the same store.packetFlows the Control Signaling panel counts "Nnef hits"
  // from. The animation loop below reads this via a ref (like snapshotRef) so control-plane
  // node highlighting (drawNodes) never misses NF-to-NF activity that briefly drops out of
  // the interpolated snapshot's edge set.
  const controlFlowsRef = useRef<PacketFlow[]>([])
  // U2: last-seen residential/City UE baseline edges, carried frame-to-frame so a brief
  // sample gap can still render the held (stale-marked) edge instead of vanishing. See
  // holdBaselineEdges.
  const heldBaselineEdgesRef = useRef<FlowEdgeState[]>([])

  const podRuntime = useMemo(() => {
    const runtimes: Record<string, PodRuntime> = {}
    pods.forEach((c) => {
      const running = c.pods.filter((p) => p.phase === 'Running').length
      const desired = Math.max(c.desired ?? 0, running)
      runtimes[c.component] = { running, desired, total: Math.max(running, desired) }
    })
    return runtimes
  }, [pods])
  const podCount = useMemo(
    () => Object.fromEntries(Object.entries(podRuntime).map(([component, runtime]) => [component, runtime.total])),
    [podRuntime]
  )

  const activeFlows = useMemo(() => packetFlows.filter((flow) => flow.active), [packetFlows])
  const nodeLoads = useMemo(
    () => calculateNodeLoads(metrics, slices),
    [metrics, slices]
  )
  // All concurrently active scenarios (not just the most recently triggered one), so a
  // batch of events (e.g. concert + typhoon + iot_surge) marks every target node at once
  // instead of only the last one (U1).
  const activeEventTypes = useMemo(() => activeScenarios.map((scenario) => scenario.type), [activeScenarios])
  // U2: whether a residential/City UE baseline is expected to exist at all right now (a
  // running UERANSIM baseline pod backs the claim). Only edges backed by this are held
  // across a sample gap - we never fabricate a baseline that was never there.
  const baselinePresence = (podRuntime.UERANSIM?.running ?? 0) > 0
  const baselinePresenceRef = useRef(baselinePresence)
  baselinePresenceRef.current = baselinePresence

  useEffect(() => {
    snapshotRef.current = {
      current: networkSnapshot,
      previous: previousNetworkSnapshot,
      startedAt: snapshotTransitionStartedAt,
      durationMs: snapshotTransitionDurationMs,
    }
  }, [networkSnapshot, previousNetworkSnapshot, snapshotTransitionStartedAt, snapshotTransitionDurationMs])

  useEffect(() => {
    controlFlowsRef.current = activeFlows.filter((flow) => flow.plane === 'control')
  }, [activeFlows])

  // U2: surfaces holdBaselineEdges' staleness (age since the baseline edge was last truly
  // observed) for the edge hover tooltip below. Polled on an interval rather than every
  // rAF frame - a "last observed Xs ago" label doesn't need frame-accurate updates.
  const [baselineStaleAgeMs, setBaselineStaleAgeMs] = useState<number | null>(null)
  useEffect(() => {
    const interval = setInterval(() => {
      const held = heldBaselineEdgesRef.current
      const oldest = held.reduce<number | null>((min, edge) => {
        const at = edge.lastObservedEpochMillis
        return at === undefined ? min : min === null ? at : Math.min(min, at)
      }, null)
      setBaselineStaleAgeMs(oldest === null ? null : Date.now() - oldest)
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  // Measures the canvas host element (its size is driven purely by the page grid/flex
  // layout now — no CSS transform wrapper). The canvas backing store is sized to this
  // measured size * devicePixelRatio each render so drawing stays crisp and 1:1 with CSS
  // pixels regardless of viewport width.
  useEffect(() => {
    const host = viewportRef.current
    if (!host) return
    const updateSize = () => {
      const rect = host.getBoundingClientRect()
      setContainerSize((prev) => (prev.width === rect.width && prev.height === rect.height ? prev : { width: rect.width, height: rect.height }))
    }
    updateSize()
    const raf = requestAnimationFrame(updateSize)
    const observer = new ResizeObserver(updateSize)
    observer.observe(host)
    window.addEventListener('resize', updateSize)
    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      window.removeEventListener('resize', updateSize)
    }
  }, [])

  // Read by the rAF loop without listing containerSize as a dependency, so a resize
  // doesn't tear down and restart the whole loop.
  const containerSizeRef = useRef(containerSize)
  containerSizeRef.current = containerSize

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let lastTime = Date.now()
    const animate = () => {
      const now = Date.now()
      const deltaTime = (now - lastTime) / 1000
      lastTime = now
      timeRef.current += deltaTime

      const { width: cssW, height: cssH } = containerSizeRef.current
      if (cssW <= 0 || cssH <= 0) {
        animationFrameRef.current = requestAnimationFrame(animate)
        return
      }

      // Size the backing store to CSS size * DPR, then set the base transform so all
      // draw calls work in CSS pixels. zoom/pan are layered on top via ctx.scale/translate
      // around the canvas center, so hotspots (which use the same normalized pos()) stay
      // aligned. reset returns zoom=1/pan=0 (identity beyond DPR).
      const dpr = window.devicePixelRatio || 1
      const bw = Math.max(1, Math.round(cssW * dpr))
      const bh = Math.max(1, Math.round(cssH * dpr))
      if (canvas.width !== bw) canvas.width = bw
      if (canvas.height !== bh) canvas.height = bh

      const z = zoomRef.current
      const p = panRef.current
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, cssW, cssH)
      ctx.translate(cssW / 2 + p.x, cssH / 2 + p.y)
      ctx.scale(z, z)
      ctx.translate(-cssW / 2, -cssH / 2)

      const displaySnapshot = interpolatedSnapshot(snapshotRef.current)
      const liveEdges = (displaySnapshot?.edges.filter((edge) => edge.active) ?? []).map(withCorrectedGnb)
      const { edges: displayFlows, held } = holdBaselineEdges(liveEdges, heldBaselineEdgesRef.current, baselinePresenceRef.current, Date.now())
      heldBaselineEdgesRef.current = held
      const displayPacketFlows = packetFlowsFromEdges(displayFlows)
      const highlightFlows = mergeControlFlowsForHighlight(displayPacketFlows, controlFlowsRef.current)

      drawMapBase(ctx, cssW, cssH, locale)
      drawStaticLinks(ctx, cssW, cssH)
      drawPacketFlows(ctx, displayFlows, cssW, cssH, timeRef.current, activeEvent)
      drawNodes(ctx, podRuntime, highlightFlows, nodeLoads, metrics, cssW, cssH, activeEventTypes, timeRef.current)

      animationFrameRef.current = requestAnimationFrame(animate)
    }

    animate()

    return () => {
      if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current)
    }
  }, [podRuntime, nodeLoads, metrics, activeEvent, activeEventTypes, runtimePrime, locale])

  function changeZoom(nextZoom: number) {
    const clamped = Math.max(1, Math.min(2.5, nextZoom))
    setZoom(clamped)
    if (clamped === 1) setPan({ x: 0, y: 0 })
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return
    const startX = event.clientX
    const startY = event.clientY
    const startPan = pan
    const target = event.currentTarget
    target.setPointerCapture(event.pointerId)

    const handleMove = (moveEvent: PointerEvent) => {
      setPan({
        x: startPan.x + (moveEvent.clientX - startX),
        y: startPan.y + (moveEvent.clientY - startY),
      })
    }
    const handleUp = () => {
      try {
        target.releasePointerCapture(event.pointerId)
      } catch {
        // capture can already be released if the browser cancels the pointer
      }
      target.removeEventListener('pointermove', handleMove)
      target.removeEventListener('pointerup', handleUp)
      target.removeEventListener('pointercancel', handleUp)
    }

    target.addEventListener('pointermove', handleMove)
    target.addEventListener('pointerup', handleUp)
    target.addEventListener('pointercancel', handleUp)
  }

  return (
    <div className="panel h-full flex flex-col">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-bold text-slate-700 tracking-wider uppercase">
            {text('城市網路拓樸圖', 'City Network Map')}
          </h2>
          <p className="text-[10px] text-slate-500">
            {text('情境流量、RAN、free5GC 網路功能、使用者面與 Kubernetes 執行狀態', 'Scenario traffic, RAN, free5GC NFs, user plane, and Kubernetes runtime')}
          </p>
        </div>
        <div className="flex flex-wrap gap-3 text-xs text-slate-500">
          {(['eMBB', 'URLLC', 'mMTC', 'V2X'] as const).map((s) => (
            <span key={s} className="flex items-center gap-1">
              <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: SLICE_COLOR[s] }} />
              {s}
            </span>
          ))}
        </div>
      </div>

      <details className="mb-2 rounded border border-blue-100 bg-blue-50 px-2 py-1.5 text-[10px] text-slate-600">
        <summary className="cursor-pointer font-bold text-blue-800">{text('小白導覽：這張圖怎麼看？', 'Beginner guide: how do I read this map?')}</summary>
        <p className="mt-1 leading-relaxed">{text('由左到右：手機、感測器與車輛（UE）先連到基地台 gNB；控制訊息交給核心網判斷要怎麼連線，真正的上網資料則經 UPF 前往網際網路。本模擬使用一個 gNB；真實世界的數量會依涵蓋、容量與備援設計。點擊任何藍框元件可看「收到什麼、做什麼、交給誰」。', 'Read left to right: phones, sensors, and vehicles (UEs) attach to a gNB. Control messages go to the core to set up the connection, while actual internet data travels through the UPF. This simulation uses one gNB; real deployments use as many as coverage, capacity, and resilience require. Tap any blue-framed component to see what it receives, does, and hands off.')}</p>
      </details>

      <p className="mb-2 rounded border border-blue-100 bg-white px-2 py-1 text-[10px] font-semibold text-blue-700">{text('👆 點擊 gNB 或任一核心網路方塊，查看它在 5G 裡扮演的角色。', '👆 Tap the gNB or any core-network box to learn its role in 5G.')}</p>

      <div className="mb-2 flex items-center justify-end">
        <MapZoomControls zoom={zoom} onZoom={changeZoom} />
      </div>

      <div
        className="relative overflow-hidden rounded-md border border-slate-200 bg-slate-50"
        style={{ height: 'clamp(380px, 45vw, 560px)' }}
      >
        <div
          ref={viewportRef}
          onPointerDown={handlePointerDown}
          className="absolute inset-0 cursor-grab active:cursor-grabbing"
        >
          <canvas
            ref={canvasRef}
            className="absolute inset-0 h-full w-full"
            style={{ background: 'linear-gradient(135deg, #f8fafc 0%, #eef4f8 100%)' }}
          />
          {isScenarioGenerationWaiting(activeEventTypes.length, runtimePrime) && (
            <ScenarioGenerationWaiting eventTypes={activeEventTypes} />
          )}
          {TOPOLOGY_NODES
            .filter((node) => node.kind === 'ran' || node.kind === 'user-plane' || node.kind === 'control-plane')
            .map((node) => (
              <NodeHoverHotspot
                key={node.id}
                node={node}
                zoom={zoom}
                pan={pan}
                containerSize={containerSize}
                load={nodeLoads[node.id] ?? componentLoad(node, metrics)}
                loadMeasured={hasMeasuredLoad(node, metrics)}
                runtime={node.component ? podRuntime[node.component] : undefined}
                detail={nodeDetail(node, metrics, podCount)}
                selected={selectedNode?.id === node.id}
                onSelect={() => setSelectedNode(node)}
              />
            ))}
          {baselineStaleAgeMs !== null && baselineStaleAgeMs > 1000 && (
            <BaselineStaleHotspot zoom={zoom} pan={pan} containerSize={containerSize} ageMs={baselineStaleAgeMs} />
          )}
        </div>

        <PlaneLegend activeEvent={activeEvent} />

        <FiveQiLegend flows={activeFlows} />
        <AnimationDisclaimerBadge />
      </div>
      {selectedNode && NF_ROLE[selectedNode.id] && (
        <section className="mt-2 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs" aria-live="polite">
          <div className="flex items-center justify-between gap-2">
            <p className="font-bold text-blue-800">{selectedNode.label} · {selectedNode.id === 'gnb1' ? text('RAN 基地台（不是 5GC NF）', 'RAN base station (not a 5GC NF)') : text('5GC 網路功能', '5GC network function')}</p>
            <button type="button" onClick={() => setSelectedNode(null)} className="rounded px-2 py-0.5 text-slate-500 hover:bg-white" aria-label={text('關閉說明', 'Close explanation')}>×</button>
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            <RoleFact label={text('收到什麼', 'Receives')} value={NF_ROLE[selectedNode.id].receives} />
            <RoleFact label={text('做什麼', 'Does')} value={NF_ROLE[selectedNode.id].does} />
            <RoleFact label={text('交給誰', 'Hands to')} value={NF_ROLE[selectedNode.id].handsTo} />
          </div>
        </section>
      )}
    </div>
  )
}

export function isScenarioGenerationWaiting(activeScenarioCount: number, runtimePrime: RuntimePrimeStatus | null | undefined): boolean {
  if (activeScenarioCount <= 0 || runtimePrime?.observedBeforePlanning === true) return false
  return !['traffic_not_observed', 'error', 'cancelled'].includes(runtimePrime?.status ?? '')
}

function ScenarioGenerationWaiting({ eventTypes }: { eventTypes: CityEventType[] }) {
  const { text } = useLocale()
  return (
    <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center bg-slate-50/45 backdrop-blur-[1px]" role="status" aria-live="polite" data-testid="scenario-generation-waiting">
      <div className="mx-4 max-w-sm rounded-xl border border-blue-200 bg-white/95 px-5 py-4 text-center shadow-lg shadow-blue-100/70">
        <div className="mx-auto grid h-11 w-11 place-items-center rounded-full bg-blue-50">
          <span className="h-6 w-6 animate-spin rounded-full border-[3px] border-blue-200 border-t-blue-600" aria-hidden="true" />
        </div>
        <p className="mt-3 text-sm font-bold text-blue-800">{text('正在生成模擬情境', 'Generating scenario traffic')}</p>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
          {text('後端正在建立 UE bearer 與啟動實際流量；收到量測證據前，拓樸圖不會顯示流量球。', 'The backend is creating UE bearers and starting live traffic. No packet particles appear until measured evidence arrives.')}
        </p>
        <div className="mt-3 flex flex-wrap justify-center gap-1.5">
          {eventTypes.map((eventType) => (
            <span key={eventType} className="inline-flex items-center gap-1 rounded-full border border-blue-100 bg-blue-50 px-2 py-1 text-[10px] font-semibold text-blue-700">
              <span className="animate-pulse" aria-hidden="true">{EVENT_ICON[eventType]}</span>
              {eventType.replace('_', ' ')}
            </span>
          ))}
        </div>
        <div className="mt-3 flex justify-center gap-1" aria-hidden="true">
          {[0, 1, 2].map((index) => <span key={index} className="h-1.5 w-1.5 animate-bounce rounded-full bg-blue-500" style={{ animationDelay: `${index * 140}ms` }} />)}
        </div>
      </div>
    </div>
  )
}

function drawMapBase(ctx: CanvasRenderingContext2D, w: number, h: number, locale: 'zh-TW' | 'en') {
  ctx.clearRect(0, 0, w, h)
  const gradient = ctx.createLinearGradient(0, 0, w, h)
  gradient.addColorStop(0, '#f8fafc')
  gradient.addColorStop(1, '#edf4f8')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, w, h)

  drawLane(ctx, w, h, 22, 80, 170, 510, locale === 'zh-TW' ? '城市需求 / UE 來源' : 'City demand / UE sources', '#eff6ff', '#bfdbfe')
  drawLane(ctx, w, h, 215, 120, 125, 355, 'RAN', '#f0fdf4', '#bbf7d0')
  drawLane(ctx, w, h, 365, 145, 165, 365, locale === 'zh-TW' ? '使用者面' : 'User plane', '#ecfeff', '#a5f3fc')
  drawLane(ctx, w, h, 585, 90, 390, 490, locale === 'zh-TW' ? '5G Core 控制面 / SBI 網狀連線' : '5G Core control plane / SBI mesh', '#f8fafc', '#cbd5e1')
}

function drawLane(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  x: number,
  y: number,
  width: number,
  height: number,
  label: string,
  fill: string,
  stroke: string
) {
  ctx.save()
  roundedRect(ctx, sx(x, w), sy(y, h), sw(width, w), sh(height, h), 14)
  ctx.fillStyle = fill
  ctx.strokeStyle = stroke
  ctx.lineWidth = 1
  ctx.fill()
  ctx.stroke()
  ctx.fillStyle = '#64748b'
  ctx.font = '600 10px ui-sans-serif'
  ctx.textAlign = 'left'
  ctx.fillText(label, sx(x + 12, w), sy(y + 19, h))
  ctx.restore()
}

function drawStaticLinks(ctx: CanvasRenderingContext2D, w: number, h: number) {
  STATIC_LINKS.forEach((link) => {
    const from = nodeById[link.from]
    const to = nodeById[link.to]
    if (!from || !to) return
    const style = linkStyle(link.type)
    drawLink(ctx, from, to, w, h, {
      color: style.color,
      width: style.width,
      alpha: style.alpha,
      dash: style.dash,
      label: link.label,
      curve: style.curve,
    })
  })
}

function drawPacketFlows(
  ctx: CanvasRenderingContext2D,
  flows: FlowEdgeState[],
  w: number,
  h: number,
  time: number,
  activeEvent: CityEventType | null
) {
  flows.forEach((flow) => {
    const src = nodeById[flow.sourceNodeId]
    const tgt = nodeById[flow.targetNodeId] ?? (flow.targetNodeId === 'core' ? nodeById.smf : undefined)
    if (!src || !tgt) return
    const isEventHighlighted = Boolean(activeEvent) && flow.scenario === activeEvent
    drawPacketFlow(ctx, src, tgt, flow, time, w, h, isEventHighlighted)
  })
}

function drawPacketFlow(
  ctx: CanvasRenderingContext2D,
  src: TopologyNode,
  tgt: TopologyNode,
  flow: FlowEdgeState,
  time: number,
  w: number,
  h: number,
  isEventHighlighted: boolean
) {
  const model = new FlowEdgeModel(flow)
  if (!model.active) return
  const sourcePoint = nodeCenter(src, w, h)
  const targetPoint = nodeCenter(tgt, w, h)
  const start = model.direction === 'reverse' ? targetPoint : sourcePoint
  const end = model.direction === 'reverse' ? sourcePoint : targetPoint
  const lineWidth = isEventHighlighted ? Math.min(model.strokeWidth * 2, 10) : model.strokeWidth
  const particleCount = isEventHighlighted ? Math.min(model.particleCount + 3, 18) : model.particleCount
  const fiveQi = flow.fiveQi ?? SLICE_QI[flow.sliceType]
  const sliceColor = SLICE_COLOR[flow.sliceType]
  const isControlPlane = flow.plane === 'control'
  const flickerFreq = QI_FLICKER[fiveQi] || 0
  const latencyMs = model.latencyMs
  const particleSpeed = (1 / model.animationDuration) * (isEventHighlighted ? 1.6 : 1)
  const jitterAmount = latencyMs > 50 ? Math.sin(time * 10) * (w * 0.004) : 0
  const packetLossPercent = flow.packetLossPercent ?? 0
  const upfCongestion = (flow.upfCongestionPercent ?? 0) / 100
  const hasDirectEvidence = Boolean((flow.evidenceCount ?? 0) > 0 || flow.lastObservedAt)
  // A particle's color answers "which Slice?" and must match the source
  // node dots and the dashboard legend. 5QI remains a separate QoS property
  // represented by flicker/labels; otherwise mMTC's deployment-compatible
  // 5QI 79 is easily mistaken for an orange V2X bearer.
  let pathColor = isControlPlane ? '#7c3aed' : sliceColor

  if (!isControlPlane && upfCongestion > 0.7) {
    const congestionScale = d3
      .scaleLinear<string, string>()
      .domain([0.7, 1])
      .range([sliceColor, '#dc2626'])
      .clamp(true)
    pathColor = congestionScale(upfCongestion)
  }

  const flowCurve = src.kind === 'control-plane' || tgt.kind === 'control-plane' ? -0.12 : 0.08
  if (isEventHighlighted) {
    drawCurvedPath(ctx, start.x, start.y, end.x, end.y, {
      color: pathColor,
      width: lineWidth + 5,
      alpha: 0.22,
      curve: flowCurve,
    })
  }
  drawCurvedPath(ctx, start.x, start.y, end.x, end.y, {
    color: pathColor,
    width: isControlPlane ? Math.min(lineWidth, 2.2) : lineWidth,
    alpha: (isControlPlane ? 0.78 : isEventHighlighted ? 0.95 : 0.68) * (hasDirectEvidence ? 1 : 0.55),
    dash: isControlPlane ? [6, 4] : packetLossPercent > 5 ? [8, 4] : undefined,
    curve: flowCurve,
  })
  if (model.direction !== 'idle') {
    drawDirectionArrow(ctx, start.x, start.y, end.x, end.y, pathColor, lineWidth + 1, flowCurve, 0.86)
  }

  if (isControlPlane && flow.protocol) {
    drawFlowProtocolLabel(ctx, start.x, start.y, end.x, end.y, flowCurve, flow.protocol)
  }

  const renderedParticleCount = isControlPlane ? Math.min(particleCount + 1, 4) : particleCount
  for (let i = 0; i < renderedParticleCount; i += 1) {
    const offset = (time * particleSpeed + i / renderedParticleCount) % 1
    const p = curvePoint(start.x, start.y, end.x, end.y, offset, flowCurve)
    let alpha = 0.75
    if (flickerFreq > 0) {
      const flicker = Math.sin(time * flickerFreq * Math.PI * 2) * 0.5 + 0.5
      alpha *= 0.45 + flicker * 0.55
    }
    ctx.save()
    const lossGhost = !isControlPlane && packetLossPercent > 8 && deterministicPacketDrop(flow.id, i, time) < packetLossPercent / 100
    ctx.fillStyle = lossGhost ? '#ef4444' : isControlPlane ? '#a78bfa' : pathColor
    ctx.globalAlpha = alpha
    ctx.beginPath()
    const jitter = isControlPlane ? 0 : jitterAmount
    ctx.arc(p.x + jitter, p.y + jitter, isControlPlane ? 2.4 : model.particleRadius, 0, Math.PI * 2)
    ctx.fill()
    ctx.restore()
  }
}

function drawNodes(
  ctx: CanvasRenderingContext2D,
  podRuntime: Record<string, PodRuntime>,
  flows: PacketFlow[],
  nodeLoads: Record<string, number>,
  metrics: NetworkMetrics | null,
  w: number,
  h: number,
  activeEvents: CityEventType[],
  time: number
) {
  // A node can be the target of more than one concurrently active scenario (e.g. no
  // current mapping collides, but this stays correct if one ever does); dedupe by node id
  // so the marker isn't drawn twice on top of itself.
  const eventsByTargetNode = new Map<string, CityEventType>()
  activeEvents.forEach((event) => {
    eventsByTargetNode.set(EVENT_TARGET_NODE[event], event)
  })
  TOPOLOGY_NODES.forEach((node) => {
    const activeFlow = flows.find((flow) => flow.sourceNodeId === node.id || flow.targetNodeId === node.id)
    const load = nodeLoads[node.id] ?? 0
    drawNode(ctx, node, w, h, activeFlow?.sliceType, Boolean(activeFlow), load, hasMeasuredLoad(node, metrics), node.component ? podRuntime[node.component] : undefined)
    const targetedByEvent = eventsByTargetNode.get(node.id)
    if (targetedByEvent) {
      drawEventMarker(ctx, node, w, h, time, targetedByEvent)
    }
  })
}

function drawEventMarker(
  ctx: CanvasRenderingContext2D,
  node: TopologyNode,
  w: number,
  h: number,
  time: number,
  activeEvent: CityEventType
) {
  const { x, y } = nodeCenter(node, w, h)
  const { width, height } = nodeSize(node, w, h)
  const baseRadius = Math.max(width, height) / 2
  const pulse = (Math.sin(time * 2.4) + 1) / 2
  const ringColor = SLICE_COLOR[EVENT_SLICE_TYPE[activeEvent]] ?? '#f59e0b'

  ctx.save()
  ctx.beginPath()
  ctx.arc(x, y, baseRadius + 6 + pulse * 8, 0, Math.PI * 2)
  ctx.strokeStyle = ringColor
  ctx.globalAlpha = 0.85 - pulse * 0.25
  ctx.lineWidth = 5
  ctx.stroke()
  ctx.restore()

  ctx.save()
  ctx.font = '20px "Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji", ui-sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillStyle = '#0f172a'
  ctx.fillText(EVENT_ICON[activeEvent] ?? '⚠️', x + width / 2 - 4, y - height / 2 - 2)
  ctx.restore()
}

function drawFlowProtocolLabel(
  ctx: CanvasRenderingContext2D,
  sx0: number,
  sy0: number,
  tx0: number,
  ty0: number,
  curve: number,
  protocol: string
) {
  const mid = curvePoint(sx0, sy0, tx0, ty0, 0.5, curve)
  const text = protocol.length > 20 ? `${protocol.slice(0, 19)}...` : protocol
  ctx.save()
  ctx.font = '700 7px ui-sans-serif'
  const width = Math.min(118, Math.max(34, ctx.measureText(text).width + 10))
  ctx.fillStyle = 'rgba(250, 245, 255, 0.9)'
  ctx.strokeStyle = 'rgba(167, 139, 250, 0.7)'
  roundedRect(ctx, mid.x - width / 2, mid.y - 8, width, 14, 4)
  ctx.fill()
  ctx.stroke()
  ctx.fillStyle = '#6d28d9'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, mid.x, mid.y - 1)
  ctx.restore()
}

function drawNode(
  ctx: CanvasRenderingContext2D,
  node: TopologyNode,
  w: number,
  h: number,
  activeSlice: SliceType | undefined,
  activeScenario: boolean,
  load: number,
  loadMeasured: boolean,
  runtime: PodRuntime | undefined
) {
  const size = nodeSize(node, w, h)
  const width = size.width
  const height = size.height
  const center = nodeCenter(node, w, h)
  // Keep the whole node box inside the canvas even on narrow widths where the padded
  // center could still let a half-box poke past the edge.
  const clamped = clampToCanvas(center.x, center.y, w, h, Math.max(width, height) / 2 + 2)
  const x = clamped.x
  const y = clamped.y
  const palette = nodePalette(node.kind)
  const border = activeSlice ? SLICE_COLOR[activeSlice] : palette.border
  const instanceCount = node.component ? Math.max(1, runtime?.total ?? 1) : 1

  ctx.save()
  if (node.component && instanceCount > 1) {
    drawReplicaCards(ctx, x, y, width, height, instanceCount, runtime?.running ?? 0, palette, border, activeScenario, node.id === 'upf')
  }
  ctx.shadowColor = activeScenario ? 'rgba(37, 99, 235, 0.28)' : 'rgba(15, 23, 42, 0.14)'
  ctx.shadowBlur = activeScenario ? 18 : 12
  ctx.shadowOffsetY = 7
  ctx.fillStyle = palette.fill
  ctx.strokeStyle = border
  ctx.lineWidth = activeScenario ? 2.3 : 1.3
  roundedRect(ctx, x - width / 2, y - height / 2, width, height, 8)
  ctx.fill()
  ctx.stroke()
  ctx.shadowColor = 'transparent'

  ctx.fillStyle = palette.top
  roundedRect(ctx, x - width / 2 + 4, y - height / 2 + 4, width - 8, 7, 4)
  ctx.fill()

  if ((node.kind === 'control-plane' || node.kind === 'user-plane') && loadMeasured) {
    const barWidth = Math.max(6, (width - 12) * Math.min(100, load) / 100)
    ctx.fillStyle = loadColor(load)
    roundedRect(ctx, x - width / 2 + 6, y + height / 2 - 9, barWidth, 4, 2)
    ctx.fill()
  }

  ctx.fillStyle = '#0f172a'
  ctx.font = '700 10px ui-sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  const icon = topologyIcon(node)
  if (icon) {
    ctx.font = '16px "Segoe UI Emoji", "Noto Color Emoji", ui-sans-serif'
    ctx.fillText(icon, x, y - 10)
  }
  ctx.font = '700 9px ui-sans-serif'
  ctx.fillText(node.label, x, icon ? y + 3 : y - 5)
  ctx.fillStyle = '#64748b'
  ctx.font = '600 7px ui-sans-serif'
  ctx.fillText(node.sublabel, x, icon ? y + 15 : y + 10)

  if (node.sliceTypes?.length) {
    node.sliceTypes.forEach((slice, i) => {
      ctx.fillStyle = SLICE_COLOR[slice]
      ctx.beginPath()
      ctx.arc(x - width / 2 + 8 + i * 9, y + height / 2 - 8, 3, 0, Math.PI * 2)
      ctx.fill()
    })
  }

  ctx.restore()
}

function topologyIcon(node: TopologyNode): string {
  if (node.kind === 'ran') return '📡'
  const icons: Record<string, string> = {
    mall: '📱', factory: '🏭', hospital: '🏥', residential: '👥', highway: '🚗', dn: '🌐',
  }
  return icons[node.id] ?? ''
}

function drawReplicaCards(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  instanceCount: number,
  runningPods: number,
  palette: ReturnType<typeof nodePalette>,
  border: string,
  activeScenario: boolean,
  userPlane: boolean
) {
  const visible = Math.min(instanceCount, 4)
  for (let i = visible - 1; i > 0; i -= 1) {
    const offsetX = i * 12
    const offsetY = i * 9
    const isRunning = i < runningPods
    ctx.save()
    ctx.shadowColor = activeScenario ? 'rgba(37, 99, 235, 0.18)' : 'rgba(15, 23, 42, 0.08)'
    ctx.shadowBlur = 8
    ctx.shadowOffsetY = 5
    ctx.fillStyle = isRunning ? palette.fill : '#f8fafc'
    ctx.strokeStyle = isRunning ? border : '#cbd5e1'
    ctx.lineWidth = 1
    roundedRect(ctx, x - width / 2 + offsetX, y - height / 2 + offsetY, width, height, 8)
    ctx.fill()
    ctx.stroke()
    ctx.shadowColor = 'transparent'
    ctx.fillStyle = isRunning ? (userPlane ? '#06b6d4' : '#22c55e') : '#cbd5e1'
    roundedRect(ctx, x + width / 2 + offsetX - 16, y - height / 2 + offsetY + 8, 8, 8, 2)
    ctx.fill()
    ctx.restore()
  }
}

function MapZoomControls({
  zoom,
  onZoom,
}: {
  zoom: number
  onZoom: (zoom: number) => void
}) {
  return (
    <div className="flex shrink-0 items-center overflow-hidden rounded border border-slate-200 bg-white/95 text-xs text-slate-700 shadow-sm shadow-slate-200/70">
      <button
        type="button"
        aria-label="Zoom out network map"
        className="h-11 w-11 border-r border-slate-200 font-bold hover:bg-slate-50 disabled:text-slate-300"
        onClick={() => onZoom(zoom - 0.15)}
        disabled={zoom <= 1}
        title="Zoom out"
      >
        -
      </button>
      <button
        type="button"
        aria-label="Reset network map zoom"
        className="h-11 min-w-16 border-r border-slate-200 px-2 font-semibold hover:bg-slate-50"
        onClick={() => onZoom(1)}
        title="Reset zoom"
      >
        {Math.round(zoom * 100)}%
      </button>
      <button
        type="button"
        aria-label="Zoom in network map"
        className="h-11 w-11 font-bold hover:bg-slate-50 disabled:text-slate-300"
        onClick={() => onZoom(zoom + 0.15)}
        disabled={zoom >= 2.5}
        title="Zoom in"
      >
        +
      </button>
    </div>
  )
}

function PlaneLegend({ activeEvent }: { activeEvent: CityEventType | null }) {
  const { text } = useLocale()
  return (
    <div className="absolute left-3 top-[86px] hidden rounded border border-slate-200 bg-white/90 p-2 text-[10px] text-slate-600 shadow-sm shadow-slate-200/70 backdrop-blur 2xl:block">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        <LegendLine color="#2563eb" label="User plane N3/N6" />
        <LegendLine color="#64748b" label="Control plane SBI" dashed />
        <LegendLine color="#ef4444" label="Congestion / priority" />
        <LegendLine color="#f97316" label="K8s scaling heat" />
        <LegendLine color="#2563eb" label="Edge with direct evidence" />
        <LegendLine color="#2563eb" label="Edge from aggregate metrics" faded />
        {activeEvent && <LegendLine color="#f59e0b" label={text('事件路徑高亮', 'Active event path')} />}
        {activeEvent && <LegendLine color="#f59e0b" label={text('⭕ 事件目標節點', '⭕ Event target node')} />}
      </div>
    </div>
  )
}

function AnimationDisclaimerBadge() {
  const { text } = useLocale()
  return (
    <div
      className="pointer-events-none absolute bottom-3 left-3 right-3 rounded border border-slate-200 bg-white/95 px-2 py-1.5 text-[10px] leading-relaxed text-slate-600 shadow-sm shadow-slate-200/70 backdrop-blur"
      title="Particle motion, position, and timing are a client-side rendering technique. They are not runtime telemetry, even when line color/width/opacity are driven by real metrics."
    >
      <span className="font-bold">{text('如何讀圖：', 'How to read this map: ')}</span>
      {text('彩色箭頭是依實測指標繪製的 UE → gNB → UPF → Data Network 使用者面路徑；紫色虛線是 N2／N4／SBI 控制面。粒子動畫只是視覺化，並非封包擷取。', 'Colored arrows show the metric-driven UE → gNB → UPF → Data Network user-plane path. Dashed purple links are N2/N4/SBI control-plane signaling. Particle motion is visualization, not packet capture.')}
    </div>
  )
}

function LegendLine({
  color,
  label,
  dashed = false,
  faded = false,
}: {
  color: string
  label: string
  dashed?: boolean
  faded?: boolean
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className="h-px w-6"
        style={{
          background: dashed ? `repeating-linear-gradient(90deg, ${color} 0 5px, transparent 5px 9px)` : color,
          opacity: faded ? 0.55 : 1,
        }}
      />
      {label}
    </span>
  )
}

interface ViewTransform {
  zoom: number
  pan: { x: number; y: number }
  containerSize: { width: number; height: number }
}

// Maps a canvas-pixel point (as produced by pos()/nodeCenter in CSS pixels) to a screen
// pixel inside the host element, reproducing the exact zoom/pan transform the rAF loop
// applies to the 2D context. This keeps the HTML hover hotspots aligned with the nodes
// drawn on the canvas at any zoom/pan.
function screenPos(cx: number, cy: number, view: ViewTransform): { x: number; y: number } {
  const { width: w, height: h } = view.containerSize
  const z = view.zoom
  return {
    x: w / 2 + view.pan.x + z * (cx - w / 2),
    y: h / 2 + view.pan.y + z * (cy - h / 2),
  }
}

function NodeHoverHotspot({
  node,
  zoom,
  pan,
  containerSize,
  load,
  loadMeasured,
  detail,
  runtime,
  selected,
  onSelect,
}: {
  node: TopologyNode
  zoom: number
  pan: { x: number; y: number }
  containerSize: { width: number; height: number }
  load: number
  loadMeasured: boolean
  detail: string
  runtime?: PodRuntime
  selected: boolean
  onSelect: () => void
}) {
  const { text } = useLocale()
  const clamped = Math.max(0, Math.min(100, load))
  const { width: cssW, height: cssH } = containerSize
  const center = nodeCenter(node, cssW, cssH)
  const size = nodeSize(node, cssW, cssH)
  const replicaW = node.component && (runtime?.total ?? 1) > 1 ? size.width + Math.min((runtime?.total ?? 1) - 1, 3) * 12 : size.width
  const replicaH = node.component && (runtime?.total ?? 1) > 1 ? size.height + Math.min((runtime?.total ?? 1) - 1, 3) * 9 : size.height
  // Clamp to keep the hotspot box inside the host (mirrors drawNode's clamp, using the
  // replica half-extent) so it never contributes horizontal overflow at the map edges.
  const boxCenter = clampToCanvas(center.x, center.y, cssW, cssH, Math.max(replicaW, replicaH) / 2 + 2)
  const screen = screenPos(boxCenter.x, boxCenter.y, { zoom, pan, containerSize })
  const status = runtime
    ? `${runtime.running}/${runtime.desired || runtime.running} pods`
    : detail
  if (cssW <= 0 || cssH <= 0) return null
  return (
    <button
      type="button"
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => { event.stopPropagation(); onSelect() }}
      aria-label={text(`查看 ${node.label} 角色說明`, `Explain ${node.label}`)}
      aria-pressed={selected}
      className={`${NODE_HOTSPOT_BASE_CLASS} ${selected ? 'ring-2 ring-blue-500 ring-offset-2' : 'hover:ring-2 hover:ring-blue-300'}`}
      style={{
        left: `${screen.x}px`,
        top: `${screen.y}px`,
        width: `${replicaW * zoom}px`,
        height: `${replicaH * zoom}px`,
      }}
    >
      <div className="h-full w-full rounded-md" />
      <div className={`pointer-events-none absolute left-1/2 top-0 z-20 hidden min-w-[150px] -translate-x-1/2 -translate-y-[calc(100%+8px)] rounded border px-2.5 py-2 text-[10px] shadow-lg shadow-slate-300/70 backdrop-blur group-hover:block ${loadFrame(clamped)}`}>
        <div className="flex items-center justify-between gap-3">
          <span className="font-bold text-slate-800">{node.label}</span>
          <span className={loadMeasured ? loadTone(clamped) : 'text-slate-400'}>
            {loadMeasured ? `${Math.round(clamped)}%` : 'n/a'}
          </span>
        </div>
        {loadMeasured && (
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-200">
            <div className="h-full rounded-full transition-all duration-700" style={{ width: `${clamped}%`, background: loadColor(clamped) }} />
          </div>
        )}
        <div className="mt-1 space-y-0.5 text-slate-500">
          <p className="truncate" title={detail}>{detail}</p>
          {runtime && <p>{status}</p>}
        </div>
      </div>
    </button>
  )
}

function RoleFact({ label, value }: { label: string; value: string }) {
  return <div className="rounded border border-blue-100 bg-white px-2 py-1.5"><p className="text-[9px] font-bold uppercase tracking-wider text-blue-600">{label}</p><p className="mt-0.5 leading-relaxed text-slate-700">{value}</p></div>
}

// U2: hover hotspot at the residential ("City UEs") -> gnb1 baseline edge midpoint, shown
// only while holdBaselineEdges is bridging a sample gap. Tells hover users the path is
// being held from the last real observation rather than fabricated, per the "last observed
// Xs ago" requirement - it never renders when the baseline is flowing normally (age <= 1s)
// or when there's no baseline to hold at all.
function BaselineStaleHotspot({ zoom, pan, containerSize, ageMs }: { zoom: number; pan: { x: number; y: number }; containerSize: { width: number; height: number }; ageMs: number }) {
  const residential = nodeById.residential
  const gnb1 = nodeById.gnb1
  const { width: cssW, height: cssH } = containerSize
  const a = nodeCenter(residential, cssW, cssH)
  const b = nodeCenter(gnb1, cssW, cssH)
  const screen = screenPos((a.x + b.x) / 2, (a.y + b.y) / 2, { zoom, pan, containerSize })
  const ageSeconds = Math.round(ageMs / 1000)
  if (cssW <= 0 || cssH <= 0) return null
  return (
    <div
      className="group absolute hidden h-6 w-10 -translate-x-1/2 -translate-y-1/2 md:block"
      style={{ left: `${screen.x}px`, top: `${screen.y}px` }}
    >
      <div className="h-full w-full rounded-md" />
      <div className="pointer-events-none absolute left-1/2 top-0 z-20 hidden min-w-[170px] -translate-x-1/2 -translate-y-[calc(100%+8px)] rounded border border-slate-200 bg-white/90 px-2.5 py-2 text-[10px] text-slate-600 shadow-lg shadow-slate-300/70 backdrop-blur group-hover:block">
        <p className="font-bold text-slate-800">市民手機基線</p>
        <p>最後觀測 {ageSeconds}s 前 (last observed {ageSeconds}s ago)</p>
      </div>
    </div>
  )
}

function FiveQiLegend({ flows }: { flows: PacketFlow[] }) {
  const activeProfiles = Array.from(new Map(flows.map((flow) => {
    const fiveQi = flow.fiveQi ?? SLICE_QI[flow.sliceType]
    return [`${flow.sliceType}:${fiveQi}`, { sliceType: flow.sliceType, fiveQi }]
  })).values())
  const profiles = activeProfiles.length > 0 ? activeProfiles : [
    { sliceType: 'eMBB' as const, fiveQi: 9 },
    { sliceType: 'URLLC' as const, fiveQi: 1 },
    { sliceType: 'mMTC' as const, fiveQi: 79 },
    { sliceType: 'V2X' as const, fiveQi: 79 },
  ]

  return (
    <div className="absolute right-3 top-3 hidden w-[210px] rounded border border-slate-200 bg-white/90 p-2 text-[10px] shadow-lg shadow-slate-200/70 backdrop-blur 2xl:block">
      <p className="mb-1 font-bold uppercase tracking-wide text-slate-500">Slice color · 5QI QoS</p>
      <div className="space-y-1">
        {profiles.map(({ sliceType, fiveQi }) => (
          <div key={`${sliceType}:${fiveQi}`} className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full" style={{ background: SLICE_COLOR[sliceType] }} />
              <span className="text-slate-700">{sliceType} · 5QI {fiveQi}</span>
            </span>
            <span className="truncate text-slate-500">{fiveQi === 79 && sliceType === 'mMTC' ? 'IoT compatibility' : QI_LABEL[fiveQi] ?? 'Custom QoS'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function interpolatedSnapshot(state: {
  current: NetworkSnapshot | null
  previous: NetworkSnapshot | null
  startedAt: number
  durationMs: number
}): NetworkSnapshot | null {
  if (!state.current) return null
  const elapsed = Date.now() - state.startedAt
  const progress = state.durationMs > 0 ? elapsed / state.durationMs : 1
  return interpolateNetworkSnapshots(state.previous, state.current, progress)
}

function packetFlowsFromEdges(edges: FlowEdgeState[]): PacketFlow[] {
  return edges.map((edge) => ({
    id: edge.id,
    sourceNodeId: edge.sourceNodeId,
    targetNodeId: edge.targetNodeId,
    sliceType: edge.sliceType,
    plane: edge.plane,
    protocol: edge.protocol,
    scenario: edge.scenario,
    active: edge.active,
    bandwidthMbps: edge.throughputMbps,
    throughputMbps: edge.throughputMbps,
    uplinkMbps: edge.uplinkMbps,
    downlinkMbps: edge.downlinkMbps,
    latencyMs: edge.latencyMs,
    fiveQi: edge.fiveQi,
    evidenceCount: edge.evidenceCount,
    lastObservedAt: edge.lastObservedAt,
    lastObservedEpochMillis: edge.lastObservedEpochMillis,
    packetLossPercent: edge.packetLossPercent,
    upfCongestionPercent: edge.upfCongestionPercent,
    qosFlows: edge.qosFlows,
  }))
}

// U2: how long a residential/City UE baseline edge is kept on screen (marked stale, via
// lastObservedAt/lastObservedEpochMillis) after it drops out of the live snapshot, so a
// momentary sample gap doesn't read as "citizens disconnected." Matches the "<30s" window
// from the bug report.
const BASELINE_HOLD_MS = 30_000

function isBaselineEdge(edge: { sourceNodeId: string; scenario?: string }): boolean {
  return edge.sourceNodeId === 'residential' || edge.scenario === 'baseline' || edge.scenario === 'baseline-embb'
}

// Keeps the residential/"City UEs" baseline edge(s) visible across brief sample gaps
// instead of letting them vanish the instant a single poll comes back without one. Only
// holds edges that were genuinely observed active before (never fabricates a baseline that
// never existed), and only while `baselinePresence` is true (a baseline sample or a running
// UERANSIM baseline pod backs the claim that citizen traffic is expected to exist) and the
// gap is under BASELINE_HOLD_MS. Held copies get `active: true` restored (the raw edge is
// inactive/missing that frame) plus a `lastObservedEpochMillis` so the UI can show
// "last observed Xs ago" instead of silently pretending the sample is fresh.
export function holdBaselineEdges(
  liveEdges: FlowEdgeState[],
  previouslyHeld: FlowEdgeState[],
  baselinePresence: boolean,
  now: number
): { edges: FlowEdgeState[]; held: FlowEdgeState[] } {
  const liveById = new Map(liveEdges.map((edge) => [edge.id, edge]))
  const heldById = new Map(previouslyHeld.map((edge) => [edge.id, edge]))
  const nextHeld: FlowEdgeState[] = []
  const extraEdges: FlowEdgeState[] = []

  if (baselinePresence) {
    liveEdges.filter(isBaselineEdge).forEach((edge) => {
      nextHeld.push({ ...edge, lastObservedEpochMillis: now })
    })
    heldById.forEach((held, id) => {
      if (liveById.has(id)) return
      const age = now - (held.lastObservedEpochMillis ?? now)
      if (age >= BASELINE_HOLD_MS) return
      const staleCopy = { ...held, active: true }
      nextHeld.push(staleCopy)
      extraEdges.push(staleCopy)
    })
  }

  return { edges: extraEdges.length === 0 ? liveEdges : [...liveEdges, ...extraEdges], held: nextHeld }
}

// Ensures every control-plane NF (including NEF, whose Nnef traffic is comparatively
// rare) gets the same node highlight as AMF/SMF whenever it appears in the store's
// packetFlows/"Nnef hits" data (see ControlSignalingPanel in EventConsole.tsx), even if
// the interpolated snapshot used for particle animation has momentarily dropped the edge.
function mergeControlFlowsForHighlight(displayFlows: PacketFlow[], controlFlows: PacketFlow[]): PacketFlow[] {
  if (controlFlows.length === 0) return displayFlows
  const seenIds = new Set(displayFlows.map((flow) => flow.id))
  const extra = controlFlows.filter((flow) => !seenIds.has(flow.id))
  return extra.length === 0 ? displayFlows : [...displayFlows, ...extra]
}

function calculateNodeLoads(metrics: NetworkMetrics | null, slices: SliceStatus[]): Record<string, number> {
  const loads: Record<string, number> = {}

  for (const node of CITY_NODES) {
    if (node.type === 'upf') {
      loads.upf = componentCpu(metrics, 'UPF') ?? metrics?.upfCpuPercent ?? 0
    }

    if (node.type === 'gnb') {
      loads[node.id] = 0
    }

    if (node.type === 'district') {
      loads[node.id] = Math.max(
        0,
        ...node.activeSlices.map((sliceType) => slices.find((slice) => slice.type === sliceType)?.load ?? 0)
      )
    }
  }

  loads.amf = componentCpu(metrics, 'AMF') ?? metrics?.amfCpuPercent ?? 0
  loads.smf = componentCpu(metrics, 'SMF') ?? 0
  return loads
}

function componentLoad(node: TopologyNode, metrics: NetworkMetrics | null): number {
  if (node.component) return componentCpu(metrics, node.component) ?? (node.id === 'upf' ? metrics?.upfCpuPercent ?? 0 : node.id === 'amf' ? metrics?.amfCpuPercent ?? 0 : 0)
  return 0
}

function hasMeasuredLoad(node: TopologyNode, metrics: NetworkMetrics | null): boolean {
  if (node.component && componentCpu(metrics, node.component) !== undefined) return true
  if (node.id === 'upf') return metrics?.upfCpuPercent !== undefined
  if (node.id === 'amf') return metrics?.amfCpuPercent !== undefined
  return false
}

function nodeDetail(node: TopologyNode, metrics: NetworkMetrics | null, podCount: Record<string, number>): string {
  if (node.id === 'upf') return `${formatCpu(componentCpu(metrics, 'UPF') ?? metrics?.upfCpuPercent)} CPU / ${podCount.UPF ?? 0} pod`
  if (node.id === 'amf') return `${formatCpu(componentCpu(metrics, 'AMF') ?? metrics?.amfCpuPercent)} CPU / ${metrics?.registeredUeCount ?? 0} UE / ${podCount.AMF ?? 0} pod`
  if (node.id === 'smf') return `${formatCpu(componentCpu(metrics, 'SMF'))} CPU / ${metrics?.pduSessionCount ?? 0} PDU / ${podCount.SMF ?? 0} pod`
  if (node.component) return `${formatCpu(componentCpu(metrics, node.component))} CPU / ${podCount[node.component] ?? 0} pod`
  if (node.id === 'gnb1') return `單一 RAN cell — 服務全部模擬區域 / live throughput ${formatNumber(metrics?.throughputMbps ?? 0)} Mbps`
  if (node.kind === 'ran') return `live throughput ${formatNumber(metrics?.throughputMbps ?? 0)} Mbps`
  return `${podCount[node.component ?? ''] ?? 0} pod`
}

function componentCpu(metrics: NetworkMetrics | null, component: string): number | undefined {
  const value = metrics?.componentCpuPercent?.[component]
  return Number.isFinite(value) ? value : undefined
}

function formatCpu(value: number | undefined): string {
  return value === undefined ? 'n/a' : `${value.toFixed(1)}%`
}

function linkStyle(type: StaticLink['type']) {
  switch (type) {
    case 'n3':
    case 'data':
      return { color: '#2563eb', width: 1.6, alpha: 0.42, curve: 0.08 }
    case 'n4':
      return { color: '#0f766e', width: 1.4, alpha: 0.42, dash: [5, 4], curve: -0.1 }
    case 'sbi':
      return { color: '#64748b', width: 1.1, alpha: 0.36, dash: [4, 5], curve: 0.1 }
    case 'n2':
      return { color: '#7c3aed', width: 1.2, alpha: 0.36, dash: [6, 4], curve: -0.1 }
    case 'observe':
      return { color: '#f97316', width: 1.1, alpha: 0.35, dash: [3, 5], curve: 0.18 }
    default:
      return { color: '#94a3b8', width: 1, alpha: 0.42, curve: 0 }
  }
}

function nodePalette(kind: NodeKind) {
  switch (kind) {
    case 'scenario':
      return { fill: '#ffffff', top: '#eff6ff', border: '#bfdbfe' }
    case 'ran':
      return { fill: '#f0fdf4', top: '#dcfce7', border: '#86efac' }
    case 'user-plane':
      return { fill: '#ecfeff', top: '#cffafe', border: '#67e8f9' }
    case 'control-plane':
      return { fill: '#ffffff', top: '#f1f5f9', border: '#cbd5e1' }
    case 'data':
      return { fill: '#f8fafc', top: '#e2e8f0', border: '#cbd5e1' }
    default:
      return { fill: '#fff7ed', top: '#ffedd5', border: '#fdba74' }
  }
}

function drawLink(
  ctx: CanvasRenderingContext2D,
  from: TopologyNode,
  to: TopologyNode,
  w: number,
  h: number,
  options: { color: string; width: number; alpha: number; dash?: number[]; label?: string; arrow?: boolean; curve?: number }
) {
  const start = nodeCenter(from, w, h)
  const end = nodeCenter(to, w, h)
  drawCurvedPath(ctx, start.x, start.y, end.x, end.y, options)
  if (options.arrow) drawDirectionArrow(ctx, start.x, start.y, end.x, end.y, options.color, options.width + 1, options.curve ?? 0, 0.84)
  if (options.label) {
    const mid = curvePoint(start.x, start.y, end.x, end.y, 0.5, options.curve ?? 0)
    ctx.save()
    ctx.fillStyle = 'rgba(255, 255, 255, 0.86)'
    ctx.strokeStyle = 'rgba(203, 213, 225, 0.8)'
    roundedRect(ctx, mid.x - 11, mid.y - 8, 22, 14, 4)
    ctx.fill()
    ctx.stroke()
    ctx.fillStyle = '#64748b'
    ctx.font = '700 8px ui-sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(options.label, mid.x, mid.y)
    ctx.restore()
  }
}

function drawCurvedPath(
  ctx: CanvasRenderingContext2D,
  sx0: number,
  sy0: number,
  tx0: number,
  ty0: number,
  options: { color: string; width: number; alpha: number; dash?: number[]; curve?: number }
) {
  const curve = options.curve ?? 0
  const mx = (sx0 + tx0) / 2
  const my = (sy0 + ty0) / 2
  const dx = tx0 - sx0
  const dy = ty0 - sy0
  const cx = mx - dy * curve
  const cy = my + dx * curve

  ctx.save()
  ctx.strokeStyle = options.color
  ctx.globalAlpha = options.alpha
  ctx.lineWidth = options.width
  ctx.setLineDash(options.dash ?? [])
  ctx.beginPath()
  ctx.moveTo(sx0, sy0)
  ctx.quadraticCurveTo(cx, cy, tx0, ty0)
  ctx.stroke()
  ctx.restore()
}

function curvePoint(sx0: number, sy0: number, tx0: number, ty0: number, t: number, curve: number) {
  const mx = (sx0 + tx0) / 2
  const my = (sy0 + ty0) / 2
  const dx = tx0 - sx0
  const dy = ty0 - sy0
  const cx = mx - dy * curve
  const cy = my + dx * curve
  const x = (1 - t) * (1 - t) * sx0 + 2 * (1 - t) * t * cx + t * t * tx0
  const y = (1 - t) * (1 - t) * sy0 + 2 * (1 - t) * t * cy + t * t * ty0
  return { x, y }
}

// Resolves a node's center into the live canvas via the normalized coordinate system.
// Every edge/particle/marker position derives from node coordinates through here, so the
// whole scene stays consistent as the canvas resizes.
function nodeCenter(node: TopologyNode, w: number, h: number) {
  return pos(normalizeX(node.x), normalizeY(node.y), w, h)
}

// Node box dimensions in canvas pixels. Widths scale with the usable drawing area so a
// wide canvas spreads nodes out (matching pos()) rather than leaving them tiny; heights
// scale similarly. Kept modest so boxes never overlap after the horizontal spread.
function nodeSize(node: TopologyNode, w: number, h: number) {
  const usableW = Math.max(1, w - MAP_PADDING * 2)
  const usableH = Math.max(1, h - MAP_PADDING * 2)
  return { width: (node.w / TOPO_WIDTH) * usableW, height: (node.h / TOPO_HEIGHT) * usableH }
}

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const radius = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + radius, y)
  ctx.arcTo(x + w, y, x + w, y + h, radius)
  ctx.arcTo(x + w, y + h, x, y + h, radius)
  ctx.arcTo(x, y + h, x, y, radius)
  ctx.arcTo(x, y, x + w, y, radius)
  ctx.closePath()
}

function drawDirectionArrow(
  ctx: CanvasRenderingContext2D,
  sx0: number,
  sy0: number,
  tx0: number,
  ty0: number,
  color: string,
  size: number,
  curve = 0,
  t = 0.82
) {
  const clampedT = Math.min(0.94, Math.max(0.08, t))
  const tip = curvePoint(sx0, sy0, tx0, ty0, clampedT, curve)
  const tail = curvePoint(sx0, sy0, tx0, ty0, Math.max(0.04, clampedT - 0.05), curve)
  const angle = Math.atan2(tip.y - tail.y, tip.x - tail.x)
  ctx.save()
  ctx.translate(tip.x, tip.y)
  ctx.rotate(angle)
  ctx.fillStyle = color
  ctx.globalAlpha = 0.9
  ctx.beginPath()
  ctx.moveTo(0, 0)
  ctx.lineTo(-size * 1.2, -size)
  ctx.lineTo(-size * 1.2, size)
  ctx.closePath()
  ctx.fill()
  ctx.restore()
}

function loadColor(load: number): string {
  if (load >= 85) return '#ef4444'
  if (load >= 70) return '#f97316'
  if (load >= 50) return '#eab308'
  return '#22c55e'
}

function deterministicPacketDrop(flowId: string, index: number, time: number): number {
  let hash = 2166136261
  const bucket = Math.floor(time * 6)
  const seed = `${flowId}:${index}:${bucket}`
  for (let i = 0; i < seed.length; i += 1) {
    hash ^= seed.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0) / 4294967295
}

function loadTone(load: number): string {
  if (load >= 85) return 'text-red-600'
  if (load >= 70) return 'text-orange-600'
  if (load >= 50) return 'text-yellow-600'
  return 'text-green-700'
}

function loadFrame(load: number): string {
  if (load >= 85) return 'border-red-300 bg-red-50/90'
  if (load >= 70) return 'border-orange-300 bg-orange-50/90'
  return 'border-slate-200 bg-white/90'
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (value >= 100) return String(Math.round(value))
  return value.toFixed(1)
}

// Maps an authored x-coordinate (1000-wide space) into the canvas through the normalized
// bounds, so decorative lanes/road/labels line up with nodes placed via pos(). Values are
// offsets/positions, so a bare coordinate resolves via normalizeX -> pos.
function sx(value: number, width: number): number {
  return pos(normalizeX(value), 0, width, 1).x
}

function sy(value: number, height: number): number {
  return pos(0, normalizeY(value), 1, height).y
}

// Widths/heights (deltas, not absolute positions) scale by the usable-area ratio so lane
// rectangles keep proportions in the normalized system.
function sw(value: number, width: number): number {
  return (value / TOPO_WIDTH) * Math.max(1, width - MAP_PADDING * 2)
}

function sh(value: number, height: number): number {
  return (value / TOPO_HEIGHT) * Math.max(1, height - MAP_PADDING * 2)
}
