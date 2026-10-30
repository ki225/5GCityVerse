import { Application, Building, CityMap, CityObject, DataCenter, Gnb, Landscape, NetworkFunction, NetworkLink, Ue } from '../domain/model'
import type { CityMapPlugin, ObjectConfig } from '../domain/model'
import { CityLandmark } from '../domain/landmarks'
import { infraConfig } from '../services/infra'

// Coordinates are expressed in the 24 km reference world and scale with map dimensions.
const GNB_POSITIONS = [[3, 6], [9, 9], [15, 3], [21, 9], [3, 18], [6, 21], [21, 18]] as const
const DISTRIBUTED_SITES = [
  { id: 'distributed', label: 'Distributed 1', x: 15.5, y: 12.5 },
  { id: 'distributed-north', label: 'Distributed 2', x: 12, y: 5 },
  { id: 'distributed-south', label: 'Distributed 3', x: 12, y: 19 },
] as const

/** A bundle owns exactly the objects it installs and rolls back a failed installation. */
export class ObjectBundle implements CityMapPlugin {
  constructor(readonly id: string, private factory: (map: CityMap) => CityObject[], private linkFactory?: (map: CityMap) => NetworkLink[]) {}
  install(map: CityMap) {
    const installed: CityObject[] = [], links: NetworkLink[] = []
    const dispose = () => {
      for (const link of links) if (map.getLink(link.id) === link) map.removeLink(link.id)
      for (const object of [...installed].reverse()) if (map.get(object.id) === object) map.remove(object.id)
    }
    try {
      for (const object of this.factory(map)) { map.add(object); installed.push(object) }
      for (const link of this.linkFactory?.(map) ?? []) { map.addLink(link); links.push(link) }
    } catch (error) { dispose(); throw error }
    return dispose
  }
}

export function createCityPreset(options: { includeUes?: boolean } = {}) {
  const map = new CityMap({ width: 24, height: 24 })
  const plugins = createPresetPlugins()
  map.batch(() => { for (const plugin of plugins) if (plugin.id !== 'ue' || options.includeUes) map.install(plugin) })
  return { map, plugins }
}

export function createPresetPlugins(): CityMapPlugin[] {
  return [
    new ObjectBundle('landscape', map => {
      const { width: w, height: h, splitX: sx, splitY: sy } = map.dimensions
      const items: CityObject[] = []
      const config = (id: string, label: string, x: number, y: number, width: number, depth: number, height: number): ObjectConfig => ({ id, label, x: x * w / 24, y: y * h / 24, width: width * w / 24, depth: depth * h / 24, height })
      items.push(new Landscape('road', { id: 'road-ew', label: '東西幹道', x: w / 2, y: sy, width: w, depth: .7, height: 0 }))
      items.push(new Landscape('road', { id: 'road-ns', label: '南北幹道', x: sx, y: h / 2, width: .7, depth: h, height: 0 }))
      for (const n of [3, 9, 15, 21]) {
        items.push(new Landscape('road', config(`road-x-${n}`, '區域道路', n, 12, .35, 24, 0)))
        items.push(new Landscape('road', config(`road-y-${n}`, '區域道路', 12, n, 24, .35, 0)))
      }
      items.push(new Landscape('water', config('port-water', '港口水域', 1, 19.5, 2, 7, 0)))
      for (let i = 0; i < 16; i++) {
        items.push(new Building('house', config(`home-${i}`, `住宅 ${i + 1}`, 2.2 + (i % 4) * 2.3, 2.2 + Math.floor(i / 4) * 2.2, .9, .75, .65)))
      }
      items.push(new Building('hospital', config('hospital', '都會醫院', 4.2, 10.2, 2.4, 1.8, 1.05)))
      items.push(new Building('school', config('school', '社區學校', 9.7, 4.4, 2.2, 1.6, .85)))
      items.push(new Building('arena', config('arena', '演唱會場', 18.5, 4.5, 4.3, 3.3, .8)))
      for (let i = 0; i < 6; i++) items.push(new Building('shop', config(`shop-${i}`, `商業大樓 ${i + 1}`, 14.2 + (i % 3) * 3, 8.3 + Math.floor(i / 3) * 1.9, 1, .8, 1.25 + (i % 3) * .5)))
      for (let i = 0; i < 4; i++) items.push(new Building(i % 2 ? 'warehouse' : 'factory', config(`factory-${i}`, i % 2 ? '物流倉庫' : '智慧工廠', 4.5 + (i % 2) * 5, 15.5 + Math.floor(i / 2) * 5, 2.4, 1.6, .9)))
      items.push(new Building('station', config('station', '都會交通站', 19, 19.5, 3.4, 2, 1)))
      items.push(new CityLandmark('railway', config('railway', '車站軌道', 19, 21, 9, .55, .04)))
      items.push(new CityLandmark('train', config('train', '列車', 18.6, 21, 3.8, .38, .35)))
      items.push(new CityLandmark('ship', config('cargo-ship', '貨櫃船', 1, 18.6, 1.6, .7, .5)))
      items.push(new CityLandmark('crane', config('port-crane', '港口吊車', 2.7, 19.5, 1.2, .9, 1.7)))
      for (let i = 0; i < 5; i++) items.push(new Building('shop', config(`office-${i}`, '車站周邊辦公樓', 14.2 + (i % 3) * 3.1, 14.2 + Math.floor(i / 3) * 2.4, .9, .8, 1 + (i % 2) * .5)))
      for (let i = 0; i < 42; i++) {
        const x = .8 + ((i * 7) % 23), y = .8 + ((i * 11) % 23)
        if (Math.abs(x - 12) < 1 || Math.abs(y - 12) < 1) continue
        items.push(new Landscape('tree', config(`tree-${i}`, '行道樹', x, y, .28, .28, .4)))
      }
      // Ground elements precede buildings so roads never paint over a roof.
      return items
    }),
    new ObjectBundle('core', map => {
      const items: CityObject[] = []
      for (const quadrant of map.quadrants) {
        const b = quadrant.bounds, x = b.x + b.width / 2, y = b.y + b.height / 2
        const site = new DataCenter('edge', { id: `edge-${quadrant.id}`, label: `Edge ${quadrant.id.toUpperCase()}`, x, y, details: { 容量: '1×', 單位成本: '高', 用途: '區域 UPF 與低延遲應用' } })
        items.push(site, new NetworkFunction(site.id, 'UPF', { id: `${site.id}-upf`, label: `${site.label} / UPF`, x: x + .75, y, details: { 功能: '使用者面轉送', Slices: '可配置共享或專用' } }))
        items.push(new Application(site.id, { id: `${site.id}-app`, label: `${site.label} / MEC App`, x, y: y + .9, details: { 用途: '區域應用實例' } }))
      }
      const x = map.dimensions.splitX, y = map.dimensions.splitY
      for (const site of DISTRIBUTED_SITES) {
        items.push(new DataCenter('distributed', { id: site.id, label: site.label, x: site.x * map.dimensions.width / 24, y: site.y * map.dimensions.height / 24, width: 1.3, depth: 1.3, height: 1, details: { 容量: '4×', 單位成本: '中' } }))
      }
      items.push(new DataCenter('central', { id: 'central', label: 'Central', x, y, width: 2, depth: 1.5, height: 1.65, details: { 容量: '16×', 單位成本: '低', 位置: '城市地圖中央；圖面配置位置', 用途: '延遲容忍型應用與備援' } }))
      const offsets = new Map<string, number>()
      for (const workload of infraConfig.workloads.filter(w => w.category === 'nf')) {
        const site = items.find(item => item.id === workload.siteId) as DataCenter
        const i = offsets.get(site.id) ?? 0
        offsets.set(site.id, i + 1)
        const id = `nf-${workload.workload.replace('free5gc-', '')}`
        items.push(new NetworkFunction(site.id, workload.function, { id, label: `${site.label} / ${workload.function}`, x: site.position.x + (site.space === 'external' ? 0 : .85 + i * .35), y: site.position.y + (site.space === 'external' ? 0 : -.4), space: site.space, details: { 所屬機房: site.label, 狀態: '配置定義；點選查看即時 Pod' } }))
      }
      return items
    }, map => {
      const distributed = map.objects.filter((item): item is DataCenter => item instanceof DataCenter && item.tier === 'distributed')
      const links = map.quadrants.map(q => {
        const edge = map.get(`edge-${q.id}`)!
        const nearest = distributed.reduce((best, site) => {
          // Display relocation must not silently reassign configured transport links.
          const distance = (item: DataCenter) => {
            const configured = infraConfig.sites.find(site => site.id === item.id)?.position
            const position = configured ? { x: configured[0] * map.dimensions.width / 24, y: configured[1] * map.dimensions.height / 24 } : item.position
            return Math.hypot(position.x - edge.position.x, position.y - edge.position.y)
          }
          return distance(site) < distance(best) ? site : best
        })
        return new NetworkLink(`transport-${q.id}`, edge.id, nearest.id)
      })
      links.push(new NetworkLink('transport-distributed-north', 'distributed-north', 'distributed'))
      links.push(new NetworkLink('transport-distributed-south', 'distributed-south', 'distributed'))
      links.push(new NetworkLink('transport-central', 'distributed', 'central'))
      return [...links, ...ranLinks(map)]
    }),
    new ObjectBundle('ran', map => {
      const items: Gnb[] = []
      const { width, height } = map.dimensions
      for (const [index, [x, y]] of GNB_POSITIONS.entries()) {
        items.push(new Gnb({ id: `gnb-0-${index}`, label: `gNB ${index + 1}`, x: x * width / 24, y: y * height / 24, height: 1.2, details: { 模型: '獨立 cell 接入容量與負載', 狀態: '前端示意' } }))
      }
      return items
    }, ranLinks),
    new ObjectBundle('ue', map => {
      const items: Ue[] = [], { width, height } = map.dimensions
      const add = (demand: Ue['demand'], count: number, cx: number, cy: number, spread: number) => {
        for (let i = 0; i < count; i++) {
          const angle = i * 2.39996, radius = Math.sqrt((i + .5) / count) * spread
          items.push(new Ue(demand, { id: `${demand}-${i}`, label: `${demand === 'video_streaming' ? '演唱會' : demand === 'factory_iot' ? 'IoT' : demand === 'connected_traffic' ? '交通' : '居民'} UE ${i + 1}`, x: (cx + Math.cos(angle) * radius) * width / 24, y: (cy + Math.sin(angle) * radius) * height / 24 }))
        }
      }
      add('web_browsing', 38, 6, 6, 4.7); add('video_streaming', 90, 18.2, 4.5, 1.05)
      add('factory_iot', 30, 6, 18, 4); add('connected_traffic', 35, 18, 18, 4)
      items.push(new Ue('realtime_control', { id: 'ambulance-1', label: '移動救護車', x: width * .46, y: height * .58 }))
      items.push(new Ue('realtime_control', { id: 'ambulance-2', label: '移動救護車', x: width * .72, y: height * .46 }))
      return items
    }),
  ]
}

function ranLinks(map: CityMap) {
  const links: NetworkLink[] = []
  for (const object of map.objects) if (object instanceof Gnb) {
    const quadrant = map.quadrants.find(q => q.contains(object.position))
    if (quadrant && map.get(`edge-${quadrant.id}`)) links.push(new NetworkLink(`access-${object.id}`, object.id, `edge-${quadrant.id}`, 'N3'))
  }
  return links
}

export function addStressUes(map: CityMap, count = 5000) {
  const prefix = `stress-${map.getRevision()}`, { width, height } = map.dimensions
  map.batch(() => {
    for (let i = 0; i < count; i++) map.add(new Ue('web_browsing', {
      id: `${prefix}-${i}`, label: `測試 UE ${i + 1}`,
      x: ((i * .61803398875) % 1) * width, y: ((i * .754877666) % 1) * height,
    }))
  })
}
