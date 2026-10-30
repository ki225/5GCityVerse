export interface Point { readonly x: number; readonly y: number }
export interface Footprint { readonly width: number; readonly depth: number; readonly height: number }
export interface Bounds { readonly x: number; readonly y: number; readonly width: number; readonly height: number }
export type MapLayer = 'landscape' | 'network' | 'ue'
export type SiteTier = 'edge' | 'distributed' | 'central'
export type ObjectSpace = 'city' | 'external'
export type ObjectDetails = Readonly<Record<string, string | number>>

export interface ObjectConfig {
  id: string
  label: string
  x: number
  y: number
  width?: number
  depth?: number
  height?: number
  color?: string
  details?: ObjectDetails
  space?: ObjectSpace
}
const APPLY_POSITION = Symbol('applyPosition')

function positive(value: number, name: string) {
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} 必須大於 0`)
  return value
}

function finite(value: number, name: string) {
  if (!Number.isFinite(value)) throw new Error(`${name} 必須是有限數字`)
  return value
}
function nonnegative(value: number, name: string) {
  if (!Number.isFinite(value) || value < 0) throw new Error(`${name} 不可小於 0`)
  return value
}

/** Every drawable component is an instance; state changes go through validated methods. */
export class CityObject {
  readonly id: string
  readonly label: string
  readonly color: string
  readonly space: ObjectSpace
  readonly details: ObjectDetails
  private location: Point
  private dimensions: Footprint
  private owner: CityMap | null = null

  constructor(readonly kind: string, readonly layer: MapLayer, config: ObjectConfig) {
    if (!config.id.trim()) throw new Error('物件 ID 不可空白')
    this.id = config.id
    this.label = config.label
    this.color = config.color ?? '#eef1df'
    this.space = config.space ?? 'city'
    this.details = Object.freeze({ ...config.details })
    this.location = Object.freeze({ x: finite(config.x, 'x'), y: finite(config.y, 'y') })
    this.dimensions = Object.freeze({
      width: positive(config.width ?? 0.5, 'width'),
      depth: positive(config.depth ?? 0.5, 'depth'),
      height: nonnegative(config.height ?? 0.5, 'height'),
    })
  }

  get position(): Point { return this.location }
  get size(): Footprint { return this.dimensions }
  get bounds(): Bounds {
    return { x: this.location.x - this.size.width / 2, y: this.location.y - this.size.depth / 2, width: this.size.width, height: this.size.depth }
  }

  setPosition(x: number, y: number): this {
    const next = Object.freeze({ x: finite(x, 'x'), y: finite(y, 'y') })
    if (next.x === this.location.x && next.y === this.location.y) return this
    if (this.owner) this.owner.moveObject(this.id, x, y)
    else this[APPLY_POSITION](next)
    return this
  }
  [APPLY_POSITION](point: Point) { this.location = Object.freeze(point); this.owner?.changed(this.layer) }

  setSize(width: number, depth: number, height = this.size.height): this {
    this.dimensions = Object.freeze({ width: positive(width, 'width'), depth: positive(depth, 'depth'), height: nonnegative(height, 'height') })
    this.owner?.changed(this.layer)
    return this
  }

  /** Ownership prevents the same mutable instance from being installed in two maps. */
  attach(map: CityMap | null) {
    if (map && this.owner && this.owner !== map) throw new Error(`${this.id} 已屬於另一張地圖`)
    this.owner = map
  }
}

export type BuildingType = 'house' | 'school' | 'hospital' | 'shop' | 'arena' | 'factory' | 'warehouse' | 'station'
export class Building extends CityObject {
  constructor(readonly buildingType: BuildingType, config: ObjectConfig) { super(`building:${buildingType}`, 'landscape', config) }
}
export class Landscape extends CityObject {
  constructor(type: 'road' | 'water' | 'tree' | 'park', config: ObjectConfig) { super(type, 'landscape', config) }
}
export class Gnb extends CityObject {
  constructor(config: ObjectConfig) { super('gnb', 'network', { width: 0.16, depth: 0.16, height: 0.65, ...config }) }
}
export class Ue extends CityObject {
  constructor(readonly demand: 'web_browsing' | 'video_streaming' | 'realtime_control' | 'factory_iot' | 'connected_traffic', config: ObjectConfig) {
    super(`ue:${demand}`, 'ue', { width: 0.14, depth: 0.14, height: 0.1, ...config })
  }
}
export class DataCenter extends CityObject {
  constructor(readonly tier: SiteTier, config: ObjectConfig) {
    super('datacenter', 'network', { width: 1, depth: 1, height: 0.7, ...config })
  }
}
export class NetworkFunction extends CityObject {
  constructor(readonly siteId: string, readonly functionName: string, config: ObjectConfig) {
    super('nf', 'network', { width: 0.2, depth: 0.2, height: 0.16, ...config })
  }
}
export class Application extends CityObject {
  constructor(readonly siteId: string, config: ObjectConfig) {
    super('application', 'network', { width: .3, depth: .3, height: .2, color: '#e2bc78', ...config })
  }
}

export class Quadrant {
  readonly bounds: Bounds
  constructor(readonly id: string, readonly label: string, bounds: Bounds, readonly color: string) {
    finite(bounds.x, '象限 x'); finite(bounds.y, '象限 y')
    positive(bounds.width, '象限寬'); positive(bounds.height, '象限長')
    this.bounds = Object.freeze({ ...bounds })
  }
  contains(point: Point) {
    const b = this.bounds
    return point.x >= b.x && point.y >= b.y && point.x < b.x + b.width && point.y < b.y + b.height
  }
}

export class NetworkLink {
  constructor(readonly id: string, readonly from: string, readonly to: string, readonly protocol: 'N2' | 'N3' | 'N4' | 'N6' | 'transport' = 'transport') {}
}

export interface CityMapConfig { width: number; height: number; splitX?: number; splitY?: number }
export interface CityMapPlugin { readonly id: string; install(map: CityMap): () => void }

/** Spatial hash bounds candidate lookup independently of the total number of UEs. */
class SpatialIndex {
  private cells = new Map<string, CityObject[]>()
  private oversized: CityObject[] = []
  constructor(private cellSize = 1.5) {}
  rebuild(objects: Iterable<CityObject>) {
    this.cells.clear(); this.oversized = []
    for (const object of objects) {
      if (object.space !== 'city') continue
      const b = object.bounds
      const x0 = Math.floor(b.x / this.cellSize), y0 = Math.floor(b.y / this.cellSize)
      const x1 = Math.floor((b.x + b.width) / this.cellSize), y1 = Math.floor((b.y + b.height) / this.cellSize)
      if ((x1 - x0 + 1) * (y1 - y0 + 1) > 64) { this.oversized.push(object); continue }
      for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) {
        const key = `${x}:${y}`
        const bucket = this.cells.get(key)
        if (bucket) bucket.push(object); else this.cells.set(key, [object])
      }
    }
  }
  query(point: Point, radius: number) {
    const result = new Set(this.oversized)
    for (let x = Math.floor((point.x - radius) / this.cellSize); x <= Math.floor((point.x + radius) / this.cellSize); x++) {
      for (let y = Math.floor((point.y - radius) / this.cellSize); y <= Math.floor((point.y + radius) / this.cellSize); y++) {
        for (const object of this.cells.get(`${x}:${y}`) ?? []) result.add(object)
      }
    }
    return result
  }
}

export class CityMap {
  private config: Readonly<Required<CityMapConfig>>
  private items = new Map<string, CityObject>()
  private connections = new Map<string, NetworkLink>()
  private adjacency = new Map<string, Set<string>>()
  private hosted = new Map<string, Set<string>>()
  private regions: readonly Quadrant[] = []
  private listeners = new Set<() => void>()
  private plugins = new Map<string, () => void>()
  private index = new SpatialIndex()
  private indexDirty = true
  private sorted = new Map<MapLayer, readonly CityObject[]>()
  private batchDepth = 0
  private pending = false
  private version = 0
  private groundVersion = 0

  constructor(config: CityMapConfig) { this.config = this.normalizeConfig(config); this.createQuadrants() }
  get dimensions() { return this.config }
  get quadrants() { return this.regions }
  get objects(): readonly CityObject[] { return [...this.items.values()] }
  get links(): readonly NetworkLink[] { return [...this.connections.values()] }
  get landscapeVersion() { return this.groundVersion }
  get count() { return this.items.size }
  getRevision = () => this.version
  subscribe = (callback: () => void) => { this.listeners.add(callback); return () => { this.listeners.delete(callback) } }
  get(id: string) { return this.items.get(id) }
  getLink(id: string) { return this.connections.get(id) }

  /** Moving a site also moves its hosted NF objects, preserving their local offsets. */
  moveObject(id: string, x: number, y: number) {
    const object = this.items.get(id)
    if (!object) throw new Error('找不到物件')
    const dx = x - object.position.x, dy = y - object.position.y
    const targets = [object, ...[...(this.hosted.get(id) ?? [])].map(childId => this.items.get(childId)!).filter(Boolean)]
    // Validate the whole move before mutating any member.
    for (const item of targets) this.validatePosition(item, { x: item.position.x + dx, y: item.position.y + dy })
    this.batch(() => { for (const item of targets) item[APPLY_POSITION]({ x: item.position.x + dx, y: item.position.y + dy }) })
  }

  private normalizeConfig(config: CityMapConfig): Readonly<Required<CityMapConfig>> {
    const width = positive(config.width, '地圖寬'), height = positive(config.height, '地圖長')
    const splitX = positive(config.splitX ?? width / 2, '分區 x'), splitY = positive(config.splitY ?? height / 2, '分區 y')
    if (splitX >= width || splitY >= height) throw new Error('象限分界必須位於地圖內')
    return Object.freeze({ width, height, splitX, splitY })
  }
  private createQuadrants() {
    const { width: w, height: h, splitX: x, splitY: y } = this.config
    this.regions = Object.freeze([
      new Quadrant('nw', '住宅・醫療', { x: 0, y: 0, width: x, height: y }, '#d8e4b5'),
      new Quadrant('ne', '演唱會・商業', { x, y: 0, width: w - x, height: y }, '#e5e6be'),
      new Quadrant('sw', '工業・港口', { x: 0, y, width: x, height: h - y }, '#cad9b4'),
      new Quadrant('se', '交通・車站', { x, y, width: w - x, height: h - y }, '#d6e1c1'),
    ])
  }

  /** Optionally scale city-space objects and their footprints; external sites keep real coordinates. */
  configure(config: CityMapConfig, scaleObjects = false) {
    const next = this.normalizeConfig(config)
    if (!scaleObjects) for (const item of this.items.values()) {
      if (item.space === 'city' && (item.position.x > next.width || item.position.y > next.height)) throw new Error(`${item.label} 超出新地圖範圍；請啟用等比配置`)
    }
    const sx = next.width / this.config.width, sy = next.height / this.config.height
    this.batch(() => {
      this.config = next
      this.createQuadrants()
      if (scaleObjects) for (const item of this.items.values()) if (item.space === 'city') {
        item[APPLY_POSITION]({ x: item.position.x * sx, y: item.position.y * sy })
        item.setSize(item.size.width * sx, item.size.depth * sy, item.size.height)
      }
      this.changed('landscape')
    })
  }
  validatePosition(object: CityObject, point: Point) {
    finite(point.x, 'x'); finite(point.y, 'y')
    if (object.space === 'city' && (point.x < 0 || point.y < 0 || point.x > this.config.width || point.y > this.config.height)) throw new Error('座標必須位於城市範圍內')
  }
  add<T extends CityObject>(object: T): T {
    if (this.items.has(object.id)) throw new Error(`重複物件 ID：${object.id}`)
    this.validatePosition(object, object.position)
    if ((object instanceof NetworkFunction || object instanceof Application) && !(this.items.get(object.siteId) instanceof DataCenter)) throw new Error('NF／應用必須放在已存在的機房')
    object.attach(this)
    this.items.set(object.id, object)
    if (object instanceof NetworkFunction || object instanceof Application) {
      if (!this.hosted.has(object.siteId)) this.hosted.set(object.siteId, new Set())
      this.hosted.get(object.siteId)!.add(object.id)
    }
    this.changed(object.layer)
    return object
  }
  remove(id: string): boolean {
    const object = this.items.get(id)
    if (!object) return false
    this.batch(() => {
      for (const childId of [...(this.hosted.get(id) ?? [])]) this.remove(childId)
      this.hosted.delete(id)
      if (object instanceof NetworkFunction || object instanceof Application) this.hosted.get(object.siteId)?.delete(id)
      for (const linkId of [...(this.adjacency.get(id) ?? [])]) this.removeLink(linkId)
      object.attach(null); this.items.delete(id); this.changed(object.layer)
    })
    return true
  }
  addLink(link: NetworkLink) {
    if (this.connections.has(link.id)) throw new Error(`重複連線 ID：${link.id}`)
    if (!this.items.has(link.from) || !this.items.has(link.to)) throw new Error('連線端點不存在')
    this.connections.set(link.id, link)
    for (const id of [link.from, link.to]) {
      if (!this.adjacency.has(id)) this.adjacency.set(id, new Set())
      this.adjacency.get(id)!.add(link.id)
    }
    this.changed('network')
    return link
  }
  removeLink(id: string) {
    const link = this.connections.get(id)
    if (!link) return
    for (const endpoint of [link.from, link.to]) {
      const entries = this.adjacency.get(endpoint)
      entries?.delete(id)
      if (!entries?.size) this.adjacency.delete(endpoint)
    }
    this.connections.delete(id); this.changed('network')
  }
  install(plugin: CityMapPlugin) {
    if (this.plugins.has(plugin.id)) return
    this.batch(() => { this.plugins.set(plugin.id, plugin.install(this)) })
  }
  uninstall(id: string) {
    const dispose = this.plugins.get(id)
    if (!dispose) return
    this.batch(() => { dispose(); this.plugins.delete(id) })
  }
  hasPlugin(id: string) { return this.plugins.has(id) }
  batch<T>(fn: () => T): T {
    this.batchDepth++
    try { return fn() } finally {
      this.batchDepth--
      if (!this.batchDepth && this.pending) { this.pending = false; this.emit() }
    }
  }
  changed(layer: MapLayer) {
    this.indexDirty = true; this.sorted.delete(layer)
    if (layer === 'landscape') this.groundVersion++
    if (this.batchDepth) this.pending = true; else this.emit()
  }
  private emit() { this.version++; for (const callback of this.listeners) callback() }
  ordered(layer: MapLayer): readonly CityObject[] {
    let result = this.sorted.get(layer)
    if (!result) {
      const ground = (item: CityObject) => ['road', 'water', 'park'].includes(item.kind) ? 0 : 1
      result = [...this.items.values()].filter(item => item.layer === layer).sort((a, b) => ground(a) - ground(b) || a.position.x + a.position.y - b.position.x - b.position.y)
      this.sorted.set(layer, result)
    }
    return result
  }
  nearby(point: Point, radius: number) {
    if (this.indexDirty) { this.index.rebuild(this.items.values()); this.indexDirty = false }
    return this.index.query(point, radius)
  }
}
