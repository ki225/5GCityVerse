import { CityMap, CityObject, DataCenter } from '../domain/model'
import { colors } from '../../theme/colors'
import type { MapLayer, Point } from '../domain/model'
import { IsometricProjection } from './projection'
import { box, builtinPainters, polygon, textAt, tile } from './painters/painters'
import type { ObjectPainter, PaintContext } from './painters/painters'
import { getActorBatchPainter } from './painters/actor-painters'
import { canvasPixelRatio, ViewportDetailCache } from './resolution'

export interface RenderStats { durationMs: number; rendered: number; total: number; frames: number; baseBuilds: number; detailBuilds: number; pixelRatio: number }
export interface RendererOptions { onSelect?: (object: CityObject | null) => void; onMapClick?: (point: Point, object: CityObject | null) => boolean; onStats?: (stats: RenderStats) => void; showLabels?: boolean }

/** Demand-driven renderer: one RAF per invalidation, zero rendering while idle. */
export class CityMapRenderer {
  readonly projection = new IsometricProjection()
  readonly stats: RenderStats = { durationMs: 0, rendered: 0, total: 0, frames: 0, baseBuilds: 0, detailBuilds: 0, pixelRatio: 1 }
  private ctx: CanvasRenderingContext2D
  private base: HTMLCanvasElement
  private baseContext: CanvasRenderingContext2D
  private sceneSurface: HTMLCanvasElement
  private sceneContext: CanvasRenderingContext2D
  private reference = new IsometricProjection()
  private painters = builtinPainters()
  private visibility: Record<MapLayer, boolean> = { landscape: true, network: true, ue: true }
  private observer: ResizeObserver
  private unsubscribe: () => void
  private width = 1
  private height = 1
  private zoom = 1
  private pan: Point = { x: 0, y: 0 }
  private frame: number | null = null
  private baseKey = ''
  private sceneKey = ''
  private painterVersion = 0
  private cachedObjects = 0
  private selected: CityObject | null = null
  private pointer: { id: number; x: number; y: number; startPan: Point; moved: boolean } | null = null
  private lastStats = 0
  private statsTimer: ReturnType<typeof setTimeout> | null = null
  private destroyed = false
  private frameListeners = new Set<() => void>()
  private transportLinksVisible = true
  private showLabels: boolean
  private detail = new ViewportDetailCache()
  private refineTimer: ReturnType<typeof setTimeout> | null = null
  private refineRequested = false

  constructor(readonly canvas: HTMLCanvasElement, readonly map: CityMap, private options: RendererOptions = {}) {
    this.showLabels = options.showLabels ?? true
    const ctx = canvas.getContext('2d', { alpha: false })
    if (!ctx) throw new Error('瀏覽器無法建立 Canvas 2D')
    this.ctx = ctx
    this.base = document.createElement('canvas')
    this.baseContext = this.base.getContext('2d', { alpha: false })!
    this.sceneSurface = document.createElement('canvas')
    this.sceneContext = this.sceneSurface.getContext('2d', { alpha: false })!
    this.observer = new ResizeObserver(this.resize)
    this.observer.observe(canvas)
    this.unsubscribe = map.subscribe(() => {
      if (this.selected && !map.get(this.selected.id)) { this.selected = null; this.options.onSelect?.(null) }
      this.invalidate()
      if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement()
    })
    canvas.addEventListener('pointerdown', this.pointerDown)
    canvas.addEventListener('pointermove', this.pointerMove)
    canvas.addEventListener('pointerup', this.pointerUp)
    canvas.addEventListener('pointercancel', this.pointerCancel)
    canvas.addEventListener('wheel', this.wheel, { passive: false })
    document.addEventListener('visibilitychange', this.visible)
    this.resize()
  }

  private resize = () => {
    const bounds = this.canvas.getBoundingClientRect()
    this.width = Math.max(1, bounds.width); this.height = Math.max(1, bounds.height)
    // Four surfaces, at most ~64 MB of RGBA storage; zoom detail uses a viewport crop.
    const ratio = canvasPixelRatio(this.width, this.height, window.devicePixelRatio)
    this.stats.pixelRatio = ratio
    for (const surface of [this.canvas, this.base, this.sceneSurface]) {
      const width = Math.round(this.width * ratio), height = Math.round(this.height * ratio)
      if (surface.width !== width) surface.width = width
      if (surface.height !== height) surface.height = height
    }
    this.detail.resize(this.canvas.width, this.canvas.height)
    this.baseKey = ''; this.sceneKey = ''; this.invalidate()
    if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement()
  }
  invalidate = () => {
    if (this.destroyed || this.frame !== null || document.hidden) return
    this.frame = requestAnimationFrame(() => { this.frame = null; this.render() })
  }
  private context(ctx = this.ctx): PaintContext { return { ctx, projection: this.projection, showLabels: this.showLabels } }
  private applyTransform(ctx: CanvasRenderingContext2D) { ctx.setTransform(this.stats.pixelRatio, 0, 0, this.stats.pixelRatio, 0, 0) }

  private render() {
    const started = performance.now(), { width, height } = this.map.dimensions
    // Cache in a reference camera. Panning/zooming composites this bitmap instead
    // of re-running thousands of object painters on each pointer event.
    this.projection.fit(width, height, this.width, this.height, 1, { x: 0, y: 0 })
    this.reference.scale = this.projection.scale; this.reference.origin = this.projection.origin
    const key = `${this.map.landscapeVersion}:${this.width}:${this.height}:${this.visibility.landscape}:${this.painterVersion}`
    let rendered = 0
    if (key !== this.baseKey) {
      this.applyTransform(this.baseContext)
      this.drawGround(this.baseContext)
      if (this.visibility.landscape) for (const object of this.map.ordered('landscape')) if (isGroundObject(object)) rendered += this.drawObject(object, this.baseContext)
      this.baseKey = key; this.stats.baseBuilds++
    }
    const sceneKey = `${key}:${this.map.getRevision()}:${this.visibility.network}:${this.visibility.ue}:${this.transportLinksVisible}`
    if (sceneKey !== this.sceneKey) {
      this.sceneContext.setTransform(1, 0, 0, 1, 0, 0); this.sceneContext.drawImage(this.base, 0, 0)
      this.applyTransform(this.sceneContext)
      rendered += this.drawCityContents(this.sceneContext)
      this.sceneKey = sceneKey; this.cachedObjects = rendered
    }
    this.projection.fit(width, height, this.width, this.height, this.zoom, this.pan)
    if (this.refineRequested && !this.pointer) {
      this.refineRequested = false
      this.detail.capture(this.projection, this.stats.pixelRatio, this.sceneKey, ctx => {
        this.drawGround(ctx)
        if (this.visibility.landscape) for (const object of this.map.ordered('landscape')) if (isGroundObject(object)) this.drawObject(object, ctx)
        this.drawCityContents(ctx)
      })
      this.stats.detailBuilds = this.detail.builds
    }
    this.applyTransform(this.ctx)
    this.ctx.fillStyle = colors.map.background; this.ctx.fillRect(0, 0, this.width, this.height)
    const ratio = this.projection.scale / this.reference.scale
    this.ctx.save()
    this.ctx.translate(this.projection.origin.x - this.reference.origin.x * ratio, this.projection.origin.y - this.reference.origin.y * ratio)
    this.ctx.scale(ratio, ratio)
    this.ctx.drawImage(this.sceneSurface, 0, 0, this.width, this.height)
    this.ctx.restore()
    this.detail.composite(this.ctx, this.projection, this.width, this.height, this.sceneKey)
    if (this.showLabels) this.drawLabels()
    if (this.selected?.space === 'city' && this.visibility[this.selected.layer]) this.drawSelection(this.selected)
    this.frameListeners.forEach(listener => listener())
    this.stats.durationMs = performance.now() - started
    this.stats.rendered = this.cachedObjects; this.stats.total = this.map.count; this.stats.frames++
    if (performance.now() - this.lastStats > 250) this.publishStats()
    else if (!this.statsTimer) this.statsTimer = setTimeout(() => { this.statsTimer = null; if (!this.destroyed) this.publishStats() }, 250)
  }
  private publishStats() { this.lastStats = performance.now(); this.options.onStats?.({ ...this.stats }) }

  private drawCityContents(ctx: CanvasRenderingContext2D) {
    if (this.visibility.network) this.drawLinks(ctx, false)
    const objects: CityObject[] = []
    if (this.visibility.landscape) for (const object of this.map.ordered('landscape')) if (!isGroundObject(object)) objects.push(object)
    if (this.visibility.network) for (const object of this.map.ordered('network')) if (object.space === 'city') objects.push(object)
    if (this.visibility.ue) objects.push(...this.map.ordered('ue'))
    objects.sort((a, b) => a.position.x + a.position.y - b.position.x - b.position.y)
    let rendered = 0
    for (let i = 0; i < objects.length;) {
      const painter = this.painters.get(objects[i].kind), batchPainter = getActorBatchPainter(painter)
      if (batchPainter) {
        let end = i + 1
        while (end < objects.length && this.painters.get(objects[end].kind) === painter) end++
        if (end - i >= 16) { batchPainter(objects.slice(i, end), this.context(ctx)); rendered += end - i; i = end; continue }
      }
      rendered += this.drawObject(objects[i], ctx); i++
    }
    return rendered
  }
  private scheduleRefinement() {
    if (this.destroyed) return
    if (this.refineTimer) clearTimeout(this.refineTimer)
    this.refineTimer = setTimeout(() => { this.refineTimer = null; this.refineRequested = true; this.invalidate() }, 100)
  }

  private drawGround(ctx: CanvasRenderingContext2D) {
    ctx.fillStyle = colors.map.background; ctx.fillRect(0, 0, this.width, this.height)
    const { width, height } = this.map.dimensions, context = this.context(ctx), p = this.projection
    const bottom = [p.project({ x: 0, y: height }), p.project({ x: width, y: height }), p.project({ x: width, y: 0 })]
    const below = bottom.map(point => ({ x: point.x, y: point.y + Math.min(12, p.scale * .32) }))
    polygon(ctx, [bottom[0], bottom[1], below[1], below[0]], '#a0ab80')
    polygon(ctx, [bottom[1], bottom[2], below[2], below[1]], '#859371')
    // Bounded tile count even when callers create a very large world.
    const step = Math.max(1, Math.ceil(Math.max(width, height) / 32))
    for (const quadrant of this.map.quadrants) {
      const b = quadrant.bounds
      for (let x = b.x; x < b.x + b.width; x += step) for (let y = b.y; y < b.y + b.height; y += step) {
        tile(context, x, y, Math.min(step, b.x + b.width - x), Math.min(step, b.y + b.height - y), quadrant.color, 0, '#92a27340')
      }
    }
  }
  private screenPosition(object: CityObject): Point {
    return this.projection.project(object.position)
  }
  private drawObject(object: CityObject, ctx = this.ctx) {
    if (object.space === 'external') return 0
    const point = this.projection.project(object.position)
    const margin = (object.size.width + object.size.depth + object.size.height) * this.projection.scale + 12
    if (point.x < -margin || point.y < -margin || point.x > this.width + margin || point.y > this.height + margin) return 0
    const painter = this.painters.get(object.kind)
    if (painter) painter(object, this.context(ctx))
    else box(this.context(ctx), object, object.color, '#8b9e8f', '#abbcac')
    return 1
  }
  private drawLinks(ctx: CanvasRenderingContext2D, external: boolean) {
    if (!this.transportLinksVisible) return
    ctx.lineWidth = 1.1; ctx.strokeStyle = '#39889a99'; ctx.setLineDash([])
    for (const link of this.map.links) {
      const from = this.map.get(link.from), to = this.map.get(link.to)
      if (!from || !to) continue
      if ((to.space === 'external' || from.space === 'external') !== external) continue
      const a = this.screenPosition(from), b = this.screenPosition(to)
      ctx.setLineDash(to.space === 'external' || from.space === 'external' ? [4, 5] : [])
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
    }
    ctx.setLineDash([])
  }
  private drawLabels() {
    const context = this.context(), { width, height } = this.map.dimensions
    for (const quadrant of this.map.quadrants) {
      const b = quadrant.bounds
      const point = this.projection.project({ x: b.x + b.width / 2, y: b.y + b.height / 2 })
      const north = b.y === 0
      textAt(context, quadrant.label, point.x, point.y + (north ? -1 : 1) * Math.max(50, this.projection.scale * 3), '#48624b', 12)
    }
    const origin = this.projection.project({ x: 0, y: 0 })
    textAt(context, '(0, 0)', origin.x, origin.y - 18, '#7c8c78', 10)
    const far = this.projection.project({ x: width, y: height })
    textAt(context, `(${width}, ${height}) km`, far.x, far.y + 25, '#7c8c78', 10)
    if (this.visibility.network) for (const object of this.map.ordered('network')) {
      if (object instanceof DataCenter && object.space === 'city' && this.projection.scale >= 12 && object !== this.selected) {
        const point = this.projection.project(object.position)
        const distributed = object.tier === 'distributed'
        const capacity = object.tier === 'central' ? '16' : distributed ? '4' : '1'
        textAt(context, `${object.label} · ${capacity}×`, point.x, point.y + (distributed ? 31 : 21), '#42686b', 9)
      }
    }
    ctxScale(context.ctx, this.width, this.height, this.projection.scale)
  }
  private drawSelection(object: CityObject) {
    const point = this.screenPosition(object), ctx = this.ctx
    const b = object.bounds, padding = .12
    const corners = [
      { x: b.x - padding, y: b.y - padding },
      { x: b.x + b.width + padding, y: b.y - padding },
      { x: b.x + b.width + padding, y: b.y + b.height + padding },
      { x: b.x - padding, y: b.y + b.height + padding },
    ].map(corner => this.projection.project(corner, .03))
    // Short corners follow the object's footprint without a ring or filled halo.
    ctx.save()
    ctx.strokeStyle = colors.primary[600]; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'
    ctx.beginPath()
    corners.forEach((corner, index) => {
      const previous = corners[(index + 3) % 4], next = corners[(index + 1) % 4]
      ctx.moveTo(corner.x + (previous.x - corner.x) * .24, corner.y + (previous.y - corner.y) * .24)
      ctx.lineTo(corner.x, corner.y)
      ctx.lineTo(corner.x + (next.x - corner.x) * .24, corner.y + (next.y - corner.y) * .24)
    })
    ctx.stroke(); ctx.restore()
    textAt(this.context(), object.label, point.x, point.y - object.size.height * this.projection.scale - 17, colors.primary[700], 11)
  }

  setLayerVisible(layer: MapLayer, visible: boolean) { this.visibility[layer] = visible; this.invalidate(); if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement() }
  setLabelsVisible(visible: boolean) { this.showLabels = visible; this.invalidate() }
  setZoom(value: number) { this.zoom = Math.max(.75, Math.min(4, value)); this.invalidate(); this.scheduleRefinement() }
  zoomBy(factor: number) { this.setZoom(this.zoom * factor) }
  resetView() { this.zoom = 1; this.pan = { x: 0, y: 0 }; if (this.refineTimer) clearTimeout(this.refineTimer); this.refineTimer = null; this.refineRequested = false; this.invalidate() }
  select(object: CityObject | null) { this.selected = object; this.options.onSelect?.(object); this.invalidate() }
  focus(object: CityObject) {
    const point = this.screenPosition(object)
    if (object.space === 'city') this.pan = { x: this.pan.x + this.width / 2 - point.x, y: this.pan.y + this.height / 2 - point.y }
    this.select(object); this.scheduleRefinement()
  }
  registerPainter(kind: string, painter: ObjectPainter) {
    const previous = this.painters.get(kind)
    this.painters.set(kind, painter); this.painterVersion++; this.invalidate()
    if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement()
    return () => { if (previous) this.painters.set(kind, previous); else this.painters.delete(kind); this.painterVersion++; this.invalidate(); if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement() }
  }
  /** Public screen projection lets consuming UIs position overlays without copying camera math. */
  toScreen(object: CityObject) { return this.screenPosition(object) }
  /** Lightweight overlays share the current camera without rebuilding the scene. */
  subscribeFrame(listener: () => void) { this.frameListeners.add(listener); return () => { this.frameListeners.delete(listener) } }
  setTransportLinksVisible(visible: boolean) {
    if (this.transportLinksVisible === visible) return
    this.transportLinksVisible = visible; this.invalidate()
    if (this.zoom > 1 || this.pan.x || this.pan.y) this.scheduleRefinement()
  }
  pick(point: Point): CityObject | null {
    const world = this.projection.unproject(point), tolerance = 10 / this.projection.scale
    const candidates = [...this.map.nearby(world, tolerance + 4)]
    candidates.sort((a, b) => {
      const priority = (item: CityObject) => item.layer === 'network' ? 3 : item.layer === 'ue' ? 2 : 1
      const distance = (item: CityObject) => Math.hypot(item.position.x - world.x, item.position.y - world.y)
      return priority(b) - priority(a) || distance(a) - distance(b)
    })
    // Prefer a true projected footprint before considering a nearby small target.
    // Otherwise an infra tolerance halo can steal a click on a large building.
    for (const padding of [0, tolerance]) for (const object of candidates) {
      if (object.space !== 'city' || !this.visibility[object.layer] || ['road', 'water', 'park', 'tree', 'landmark:railway'].includes(object.kind)) continue
      const b = object.bounds
      const minHeight = Math.max(0, b.x - padding - world.x, b.y - padding - world.y)
      const maxHeight = Math.min(Math.max(object.size.height, .1), b.x + b.width + padding - world.x, b.y + b.height + padding - world.y)
      if (minHeight <= maxHeight) return object
    }
    return null
  }
  private local(event: PointerEvent | WheelEvent) {
    const bounds = this.canvas.getBoundingClientRect()
    return { x: event.clientX - bounds.left, y: event.clientY - bounds.top }
  }
  private pointerDown = (event: PointerEvent) => {
    if (event.button !== 0) return
    this.canvas.setPointerCapture(event.pointerId)
    this.pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, startPan: this.pan, moved: false }
  }
  private pointerMove = (event: PointerEvent) => {
    if (!this.pointer || event.pointerId !== this.pointer.id) return
    const dx = event.clientX - this.pointer.x, dy = event.clientY - this.pointer.y
    if (Math.abs(dx) + Math.abs(dy) > 4) this.pointer.moved = true
    if (this.pointer.moved) { this.pan = { x: this.pointer.startPan.x + dx, y: this.pointer.startPan.y + dy }; this.invalidate() }
  }
  private pointerUp = (event: PointerEvent) => {
    if (!this.pointer || event.pointerId !== this.pointer.id) return
    if (!this.pointer.moved) {
      const point = this.local(event), object = this.pick(point)
      if (!this.options.onMapClick?.(this.projection.unproject(point), object)) this.select(object)
    }
    this.pointerCancel(event)
  }
  private pointerCancel = (event: PointerEvent) => {
    const moved = this.pointer?.moved
    if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId)
    this.pointer = null
    if (moved) this.scheduleRefinement()
  }
  private wheel = (event: WheelEvent) => { event.preventDefault(); this.zoomBy(Math.exp(-event.deltaY * .001)) }
  private visible = () => { if (!document.hidden) this.invalidate() }
  destroy() {
    this.destroyed = true
    this.frameListeners.clear()
    if (this.frame !== null) cancelAnimationFrame(this.frame)
    if (this.statsTimer) clearTimeout(this.statsTimer)
    if (this.refineTimer) clearTimeout(this.refineTimer)
    this.observer.disconnect(); this.unsubscribe()
    this.canvas.removeEventListener('pointerdown', this.pointerDown)
    this.canvas.removeEventListener('pointermove', this.pointerMove)
    this.canvas.removeEventListener('pointerup', this.pointerUp)
    this.canvas.removeEventListener('pointercancel', this.pointerCancel)
    this.canvas.removeEventListener('wheel', this.wheel)
    document.removeEventListener('visibilitychange', this.visible)
    this.base.width = 0; this.base.height = 0
    this.sceneSurface.width = 0; this.sceneSurface.height = 0
    this.detail.destroy()
  }
}

function isGroundObject(object: CityObject) {
  return ['road', 'water', 'park', 'landmark:railway', 'building:arena'].includes(object.kind)
}

function ctxScale(ctx: CanvasRenderingContext2D, width: number, height: number, scale: number) {
  const x = 28, y = height - 28, length = scale * 3 * Math.sqrt(1.25)
  ctx.strokeStyle = '#75826c'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, y - 4); ctx.lineTo(x, y); ctx.lineTo(x + length, y); ctx.lineTo(x + length, y - 4); ctx.stroke()
  ctx.font = '10px system-ui'; ctx.fillStyle = '#75826c'; ctx.textAlign = 'left'; ctx.fillText('3 km', x, y - 9)
  ctx.textAlign = 'right'; ctx.fillText('拖曳平移 · 滾輪縮放', width - 22, height - 24)
}
