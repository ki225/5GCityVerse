import { describe, it, expect } from 'vitest'
import type { FlowEdgeState } from '../../types'
import {
  clampToCanvas,
  EVENT_TARGET_NODE,
  NF_ROLE,
  NODE_HOTSPOT_BASE_CLASS,
  holdBaselineEdges,
  isScenarioGenerationWaiting,
  normalizeX,
  normalizeY,
  pos,
  TOPO_BOUNDS,
  TOPOLOGY_NODES,
  withCorrectedGnb,
} from './CanvasCityMap'

describe('plain-language network roles', () => {
  it('labels the gNB as RAN rather than a 5GC NF', () => {
    expect(NF_ROLE.gnb1.does).toContain('RAN')
    expect(NF_ROLE.gnb1.does).toContain('不是 5GC NF')
  })

  it('explains the separate UDM and AUSF jobs even though the map combines their box', () => {
    expect(NF_ROLE.udm.does).toContain('UDM 像會員資料管理員')
    expect(NF_ROLE.udm.does).toContain('AUSF 像驗證櫃台')
    expect(NF_ROLE.udm.receives).toContain('兩個 NF 合併')
  })

  it('keeps UDR and NEF discoverable with all three novice questions answered', () => {
    for (const id of ['udr', 'nef']) {
      expect(NF_ROLE[id]).toBeDefined()
      expect(NF_ROLE[id].receives.length).toBeGreaterThan(0)
      expect(NF_ROLE[id].does.length).toBeGreaterThan(0)
      expect(NF_ROLE[id].handsTo.length).toBeGreaterThan(0)
    }
  })

  it('does not hide NF click targets on small screens', () => {
    expect(NODE_HOTSPOT_BASE_CLASS).toContain('block')
    expect(NODE_HOTSPOT_BASE_CLASS).not.toContain('hidden')
  })
})

describe('scenario generation waiting state', () => {
  it('shows waiting before measured backend traffic exists', () => {
    expect(isScenarioGenerationWaiting(2, { status: 'running', observedBeforePlanning: false })).toBe(true)
    expect(isScenarioGenerationWaiting(1, null)).toBe(true)
  })

  it('stops waiting once measured traffic exists or generation fails', () => {
    expect(isScenarioGenerationWaiting(1, { status: 'success', observedBeforePlanning: true })).toBe(false)
    expect(isScenarioGenerationWaiting(1, { status: 'traffic_not_observed', observedBeforePlanning: false })).toBe(false)
    expect(isScenarioGenerationWaiting(0, { status: 'running' })).toBe(false)
  })
})

// MAP_PADDING is not exported; mirror its value for the in-bounds assertions below.
const MAP_PADDING = 34
const EVENT_MARKER_MAX_OVERFLOW = 14 + 12

function baselineEdge(overrides: Partial<FlowEdgeState> = {}): FlowEdgeState {
  return {
    id: 'live-residential-gnb1',
    sourceNodeId: 'residential',
    targetNodeId: 'gnb1',
    sliceType: 'eMBB',
    active: true,
    throughputMbps: 5,
    uplinkMbps: 2,
    downlinkMbps: 3,
    latencyMs: 10,
    ...overrides,
  }
}

describe('holdBaselineEdges', () => {
  it('passes live baseline edges through unchanged and records them as held', () => {
    const edge = baselineEdge()
    const { edges, held } = holdBaselineEdges([edge], [], true, 1000)
    expect(edges).toEqual([edge])
    expect(held).toEqual([{ ...edge, lastObservedEpochMillis: 1000 }])
  })

  it('re-injects a held edge that dropped out of a live snapshot within the hold window', () => {
    const previouslyHeld = [{ ...baselineEdge(), lastObservedEpochMillis: 1000 }]
    const { edges } = holdBaselineEdges([], previouslyHeld, true, 1000 + 5000)
    expect(edges).toHaveLength(1)
    expect(edges[0]).toMatchObject({ id: 'live-residential-gnb1', active: true })
  })

  it('drops a held edge once the gap exceeds the 30s hold window', () => {
    const previouslyHeld = [{ ...baselineEdge(), lastObservedEpochMillis: 1000 }]
    const { edges, held } = holdBaselineEdges([], previouslyHeld, true, 1000 + 31_000)
    expect(edges).toEqual([])
    expect(held).toEqual([])
  })

  it('never fabricates a baseline edge when there is no baseline presence at all', () => {
    const previouslyHeld = [{ ...baselineEdge(), lastObservedEpochMillis: 1000 }]
    const { edges, held } = holdBaselineEdges([], previouslyHeld, false, 1000 + 100)
    expect(edges).toEqual([])
    expect(held).toEqual([])
  })

  it('does not track a non-baseline edge (e.g. a mall scenario edge) for holding', () => {
    const mallEdge = baselineEdge({ id: 'mall-edge', sourceNodeId: 'mall', scenario: 'concert' })
    const { held } = holdBaselineEdges([mallEdge], [], true, 1000)
    expect(held).toEqual([])
  })
})

describe('withCorrectedGnb', () => {
  it('rewrites the mall scenario RAN hop (residential/mall -> gnb) to gnb1', () => {
    const edge = { sourceNodeId: 'mall', targetNodeId: 'gnb2', scenario: 'concert' }
    expect(withCorrectedGnb(edge).targetNodeId).toBe('gnb1')
  })

  it('rewrites the mall scenario N3 hop (gnb -> upf) to the SAME gnb1, keeping RAN/N3 paired', () => {
    const ranEdge = withCorrectedGnb({ sourceNodeId: 'mall', targetNodeId: 'gnb2', scenario: 'concert' })
    const n3Edge = withCorrectedGnb({ sourceNodeId: 'gnb2', targetNodeId: 'upf', scenario: 'concert' })

    // The RAN and N3 segments must land on the same gNB so a mall packet's path is
    // contiguous (regression: N3 has no scenario-source endpoint, so an endpoint-based
    // correction previously left it stuck on the backend's hardcoded gnb2).
    expect(ranEdge.targetNodeId).toBe('gnb1')
    expect(n3Edge.sourceNodeId).toBe('gnb1')
  })

  it('routes the factory/highway scenario through the same single gNB', () => {
    const ranEdge = withCorrectedGnb({ sourceNodeId: 'factory', targetNodeId: 'gnb1', scenario: 'iot_surge' })
    const n3Edge = withCorrectedGnb({ sourceNodeId: 'gnb1', targetNodeId: 'upf', scenario: 'iot_surge' })
    expect(ranEdge.targetNodeId).toBe('gnb1')
    expect(n3Edge.sourceNodeId).toBe('gnb1')
  })

  it('leaves edges with no recognized scenario untouched', () => {
    const edge = { sourceNodeId: 'gnb2', targetNodeId: 'upf', scenario: undefined }
    expect(withCorrectedGnb(edge)).toEqual(edge)
  })

  it('leaves edges with no gnb1/gnb2 endpoint untouched', () => {
    const edge = { sourceNodeId: 'amf', targetNodeId: 'smf', scenario: 'concert' }
    expect(withCorrectedGnb(edge)).toEqual(edge)
  })
})

describe('EVENT_TARGET_NODE', () => {
  const nodeIds = new Set(TOPOLOGY_NODES.map((node) => node.id))

  it('maps every city event to a node id that exists in TOPOLOGY_NODES', () => {
    for (const [eventType, nodeId] of Object.entries(EVENT_TARGET_NODE)) {
      expect(nodeIds.has(nodeId), `${eventType} -> "${nodeId}" is not a TOPOLOGY_NODES id`).toBe(true)
    }
  })

  it('contains exactly one gNB node, matching the real UERANSIM runtime', () => {
    expect(TOPOLOGY_NODES.filter((node) => node.kind === 'ran').map((node) => node.id)).toEqual(['gnb1'])
  })

  it('presents the citizen phone as an eMBB endpoint, not an mMTC device', () => {
    const citizenPhone = TOPOLOGY_NODES.find((node) => node.id === 'residential')
    expect(citizenPhone).toMatchObject({
      label: '市民手機',
      sublabel: 'eMBB phone',
      sliceTypes: ['eMBB'],
    })
  })
})

describe('normalizeX / normalizeY', () => {
  it('maps the topology bounds corners to [0, 1]', () => {
    expect(normalizeX(TOPO_BOUNDS.minX)).toBeCloseTo(0, 6)
    expect(normalizeX(TOPO_BOUNDS.maxX)).toBeCloseTo(1, 6)
    expect(normalizeY(TOPO_BOUNDS.minY)).toBeCloseTo(0, 6)
    expect(normalizeY(TOPO_BOUNDS.maxY)).toBeCloseTo(1, 6)
  })

  it('keeps every node (including its event-marker overflow) within [0, 1] after normalization', () => {
    TOPOLOGY_NODES.forEach((node) => {
      const nxLeft = normalizeX(node.x - node.w / 2 - EVENT_MARKER_MAX_OVERFLOW)
      const nxRight = normalizeX(node.x + node.w / 2 + EVENT_MARKER_MAX_OVERFLOW)
      const nyTop = normalizeY(node.y - node.h / 2 - EVENT_MARKER_MAX_OVERFLOW)
      const nyBottom = normalizeY(node.y + node.h / 2 + EVENT_MARKER_MAX_OVERFLOW)
      expect(nxLeft, `${node.id} nx left`).toBeGreaterThanOrEqual(-1e-9)
      expect(nxRight, `${node.id} nx right`).toBeLessThanOrEqual(1 + 1e-9)
      expect(nyTop, `${node.id} ny top`).toBeGreaterThanOrEqual(-1e-9)
      expect(nyBottom, `${node.id} ny bottom`).toBeLessThanOrEqual(1 + 1e-9)
    })
  })
})

describe('pos', () => {
  it('maps normalized 0/1 to the padded edges of the canvas', () => {
    const w = 800
    const h = 500
    const pad = 34
    expect(pos(0, 0, w, h, pad)).toEqual({ x: pad, y: pad })
    expect(pos(1, 1, w, h, pad)).toEqual({ x: w - pad, y: h - pad })
    expect(pos(0.5, 0.5, w, h, pad)).toEqual({ x: w / 2, y: h / 2 })
  })

  it('spreads horizontally on a wide canvas (a normalized point lands further right than on a narrow canvas)', () => {
    const narrow = pos(0.75, 0.5, 700, 500)
    const wide = pos(0.75, 0.5, 1600, 500)
    expect(wide.x).toBeGreaterThan(narrow.x)
  })
})

// The core acceptance criterion: at every realistic canvas width, every TOPOLOGY node's
// box (center resolved via pos(), extended by its half-size + marker overflow) stays
// inside the canvas — no left/right clipping. Node box sizes here mirror nodeSize() in
// the component (node.w/h scaled by the usable-area ratio).
describe('all nodes stay in-bounds across canvas widths (no clipping)', () => {
  const TOPO_WIDTH = TOPO_BOUNDS.maxX - TOPO_BOUNDS.minX
  const TOPO_HEIGHT = TOPO_BOUNDS.maxY - TOPO_BOUNDS.minY
  const CANVAS_HEIGHT = 480

  function nodeBoxSize(node: (typeof TOPOLOGY_NODES)[number], w: number, h: number) {
    const usableW = Math.max(1, w - MAP_PADDING * 2)
    const usableH = Math.max(1, h - MAP_PADDING * 2)
    return { width: (node.w / TOPO_WIDTH) * usableW, height: (node.h / TOPO_HEIGHT) * usableH }
  }

  // Widths spanning the map panel at 1280 / 1440 / 1920 page widths and narrower stacked layouts.
  for (const canvasWidth of [360, 560, 700, 900, 1100, 1400]) {
    it(`keeps every node box inside a ${canvasWidth}x${CANVAS_HEIGHT} canvas`, () => {
      TOPOLOGY_NODES.forEach((node) => {
        const center = pos(normalizeX(node.x), normalizeY(node.y), canvasWidth, CANVAS_HEIGHT)
        const { width, height } = nodeBoxSize(node, canvasWidth, CANVAS_HEIGHT)
        const clamped = clampToCanvas(center.x, center.y, canvasWidth, CANVAS_HEIGHT, Math.max(width, height) / 2 + 2)
        const left = clamped.x - width / 2
        const right = clamped.x + width / 2
        const top = clamped.y - height / 2
        const bottom = clamped.y + height / 2
        expect(left, `${node.id} left @${canvasWidth}`).toBeGreaterThanOrEqual(-0.5)
        expect(right, `${node.id} right @${canvasWidth}`).toBeLessThanOrEqual(canvasWidth + 0.5)
        expect(top, `${node.id} top @${canvasWidth}`).toBeGreaterThanOrEqual(-0.5)
        expect(bottom, `${node.id} bottom @${canvasWidth}`).toBeLessThanOrEqual(CANVAS_HEIGHT + 0.5)
      })
    })
  }
})

describe('clampToCanvas', () => {
  it('leaves an in-bounds point untouched', () => {
    expect(clampToCanvas(100, 100, 800, 500, 10)).toEqual({ x: 100, y: 100 })
  })

  it('pulls an out-of-bounds point back inside by the inset', () => {
    expect(clampToCanvas(-20, 600, 800, 500, 10)).toEqual({ x: 10, y: 490 })
  })
})
