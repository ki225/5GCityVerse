import { describe, expect, it } from 'vitest'
import { Application, CityMap, CityObject, DataCenter, Gnb, NetworkFunction, NetworkLink, Ue } from './model'
import { IsometricProjection } from '../rendering/projection'
import { addStressUes, createCityPreset, ObjectBundle } from '../data/presets'

describe('城市物件與插件生命週期', () => {
  it('使用預定城市座標建立設備、UE 與可分別識別的 NF', () => {
    const { map } = createCityPreset()
    expect(map.objects.filter(object => object instanceof Gnb)).toHaveLength(7)
    expect(map.objects.filter(object => object instanceof DataCenter && object.tier === 'central')).toHaveLength(1)
    expect(map.objects.filter(object => object instanceof DataCenter && object.tier === 'distributed')).toHaveLength(3)
    expect(map.objects.filter(object => object instanceof DataCenter && object.tier === 'edge')).toHaveLength(4)
    expect(map.get('edge-nw')?.position).toEqual({ x: 6, y: 6 })
    expect(map.get('edge-se')?.position).toEqual({ x: 18, y: 18 })
    expect(map.get('distributed')?.position).toEqual({ x: 15.5, y: 12.5 })
    expect(map.get('distributed-north')?.position).toEqual({ x: 12, y: 5 })
    expect(map.get('distributed-south')?.position).toEqual({ x: 12, y: 19 })
    expect(map.get('central')?.position).toEqual({ x: 12, y: 12 })
    expect(map.objects.filter(object => object instanceof NetworkFunction)).toHaveLength(16)
    expect((map.get('nf-amf') as NetworkFunction).siteId).toBe('distributed')
    expect((map.get('nf-upf-embb') as NetworkFunction).siteId).toBe('distributed-north')
    expect((map.get('nf-upf-v2x') as NetworkFunction).siteId).toBe('distributed-south')
    expect((map.get('nf-nrf') as NetworkFunction).siteId).toBe('central')
    expect((map.get('nf-upf-mmtc') as NetworkFunction).siteId).toBe('central')
    expect(map.objects.filter(object => object instanceof Ue)).toHaveLength(0)
    expect(map.hasPlugin('ue')).toBe(false)
    expect(map.links.filter(link => link.protocol === 'N3')).toHaveLength(7)
    expect(map.getLink('transport-nw')?.to).toBe('distributed-north')
    expect(map.getLink('transport-se')?.to).toBe('distributed-south')
  })
  it('插件可以卸載和重新插入，連線不留下失效端點或重複項目', () => {
    const { map, plugins } = createCityPreset({ includeUes: true })
    for (let round = 0; round < 3; round++) for (const id of ['core', 'ran', 'ue', 'landscape']) {
      const count = map.count, links = map.links.length
      map.uninstall(id)
      for (const link of map.links) { expect(map.get(link.from)).toBeDefined(); expect(map.get(link.to)).toBeDefined() }
      map.install(plugins.find(plugin => plugin.id === id)!)
      expect(map.count).toBe(count); expect(map.links).toHaveLength(links)
    }
  })
  it('移動機房帶著 NF，移除機房移除其 NF 與相關連線', () => {
    const map = new CityMap({ width: 24, height: 24 })
    map.add(new DataCenter('edge', { id: 'e', label: 'E', x: 6, y: 6 }))
    const nf = map.add(new NetworkFunction('e', 'UPF', { id: 'nf', label: 'UPF', x: 7, y: 6 }))
    const app = map.add(new Application('e', { id: 'app', label: 'MEC', x: 6, y: 7 }))
    map.add(new Gnb({ id: 'g', label: 'gNB', x: 3, y: 3 }))
    map.addLink(new NetworkLink('link', 'g', 'e', 'N3'))
    map.get('e')!.setPosition(8, 9)
    expect(nf.position).toEqual({ x: 9, y: 9 })
    expect(app.position).toEqual({ x: 8, y: 10 })
    expect(() => map.moveObject('e', 24, 24)).toThrow()
    expect(map.get('e')?.position).toEqual({ x: 8, y: 9 })
    map.remove('e'); expect(map.get('nf')).toBeUndefined(); expect(map.get('app')).toBeUndefined(); expect(map.links).toHaveLength(0)
  })
  it('防止不合法座標、尺寸、重複 ID 與跨地圖共享可變物件', () => {
    const map = new CityMap({ width: 24, height: 24 })
    const object = map.add(new Ue('realtime_control', { id: 'ue', label: 'UE', x: 4, y: 10 }))
    expect(() => object.setPosition(-1, 10)).toThrow()
    expect(() => object.setSize(Number.NaN, 1)).toThrow()
    expect(() => object.setSize(1, 1, -1)).toThrow()
    expect(() => new CityMap({ width: 2, height: 2 }).add(object)).toThrow()
    expect(() => map.add(object)).toThrow()
    expect(() => map.addLink(new NetworkLink('bad', 'ue', 'missing'))).toThrow()
    expect(object.position).toEqual({ x: 4, y: 10 })
  })
  it('插件安裝失敗會撤回已加入物件', () => {
    const map = new CityMap({ width: 24, height: 24 })
    const plugin = new ObjectBundle('bad', () => [new CityObject('custom', 'network', { id: 'a', label: 'a', x: 3, y: 3 }), new CityObject('custom', 'network', { id: 'a', label: 'a', x: 5, y: 5 })])
    expect(() => map.install(plugin)).toThrow(); expect(map.count).toBe(0); expect(map.hasPlugin('bad')).toBe(false)
  })
  it('卸載插件不會誤刪外部重新建立的同 ID 物件', () => {
    const map = new CityMap({ width: 24, height: 24 })
    map.install(new ObjectBundle('bundle', () => [new Gnb({ id: 'g', label: '舊設備', x: 1, y: 1 })]))
    map.remove('g')
    const replacement = map.add(new Gnb({ id: 'g', label: '新設備', x: 2, y: 2 }))
    map.uninstall('bundle')
    expect(map.get('g')).toBe(replacement)
  })
})

describe('尺寸、座標與大量資料', () => {
  it('地圖長寬與象限分界可設定，機房跟著城市比例尺移動', () => {
    const { map } = createCityPreset()
    map.configure({ width: 48, height: 12, splitX: 18, splitY: 4 }, true)
    expect(map.get('edge-nw')?.position).toEqual({ x: 12, y: 3 })
    expect(map.quadrants[0].bounds).toEqual({ x: 0, y: 0, width: 18, height: 4 })
    expect(map.quadrants[3].bounds).toEqual({ x: 18, y: 4, width: 30, height: 8 })
    expect(map.get('central')?.position).toEqual({ x: 24, y: 6 })
    expect(() => map.configure({ width: 10, height: 10, splitX: 11 })).toThrow()
    expect(map.dimensions.width).toBe(48)
  })
  it('批次加入 5000 UE 只發出一次更新，座標索引會在移動後更新', () => {
    const map = new CityMap({ width: 24, height: 24 })
    let updates = 0
    const unsubscribe = map.subscribe(() => { updates++ })
    addStressUes(map)
    expect(map.count).toBe(5000); expect(updates).toBe(1)
    const object = map.add(new Ue('realtime_control', { id: 'realtime_control', label: '急救', x: 3, y: 3 }))
    expect(map.nearby({ x: 3, y: 3 }, .1).has(object)).toBe(true)
    object.setPosition(20, 20)
    expect(map.nearby({ x: 3, y: 3 }, .1).has(object)).toBe(false)
    expect(map.nearby({ x: 20, y: 20 }, .1).has(object)).toBe(true)
    unsubscribe()
  })
  it('等角投影與反向座標換算在平移、縮放後一致', () => {
    const projection = new IsometricProjection()
    projection.fit(36, 18, 1200, 700, 1.7, { x: 40, y: -50 })
    for (const point of [{ x: 0, y: 0 }, { x: 6, y: 18 }, { x: 35.7, y: 2.4 }]) {
      const restored = projection.unproject(projection.project(point))
      expect(restored.x).toBeCloseTo(point.x, 10); expect(restored.y).toBeCloseTo(point.y, 10)
    }
  })
})
