import { afterEach,describe,expect,it,vi } from 'vitest'
import { validSpatialDemand } from '../../onprem/services/spatialDemand'
import { compileCityDemand,type CityDemand } from './demand'
vi.mock('../../services/browserSession', () => ({ sessionHeaders: () => ({ 'X-CityVerse-Session-Id': 'city-demand-test' }) }))

const rows: CityDemand[] = [
  { groupId: 'home-a', label: 'Home A', temporary: false, profileId: 'web_browsing', ueCount: 2, sourceLocation: { type: 'point', buildingId: 'home-a', position: { x: 2, y: 3 } } },
  { groupId: 'emergency', label: 'Emergency', temporary: true, profileId: 'realtime_control', ueCount: 1, sourceLocation: { type: 'point', position: { x: 9, y: 12 } } },
]
const service = { traffic: { protocol: 'udp' as const, offeredUplinkMbpsPerUe: 1, offeredDownlinkMbpsPerUe: 2 } }
const compile = () => compileCityDemand(structuredClone(rows), { web_browsing: service, realtime_control: service }, 7)
afterEach(() => vi.unstubAllGlobals())

describe('City demand submission boundary', () => {
  it('conserves disjoint UE counts and original coordinates across building and temporary demands', () => {
    const request = compile()
    expect(request.baselineUes).toBe(2)
    expect(request.events).toEqual({ video_streaming: 0,  connected_traffic: 0, realtime_control: 1, factory_iot: 0 })
    expect(request.demandGroups![0].sourceLocation).toEqual(rows[0].sourceLocation)
    expect(request.demandGroups![1].sourceLocation).not.toHaveProperty('buildingId')
    expect(request.demandGroups![0]).not.toHaveProperty('label')
    expect(request.demandGroups![0]).not.toHaveProperty('temporary')
    expect(request.topologyRevision).toBe(7)
    request.demandGroups![0].sourceLocation.position.x = 22
    expect(rows[0].sourceLocation.position.x).toBe(2)
  })
  it('shares one service requirement across multiple buildings of the same category', () => {
    const other = { ...structuredClone(rows[0]), groupId: 'home-b', ueCount: 3 }
    const request = compileCityDemand([...rows, other], { web_browsing: service, realtime_control: service }, 1)
    expect(request.baselineUes).toBe(5)
    expect(Object.keys(request.serviceRequirements!.services)).toEqual(['web_browsing', 'realtime_control'])
  })
  it.each([
    ['count mismatch', (r: ReturnType<typeof compile>) => { r.events.realtime_control++ }],
    ['duplicate group', (r: ReturnType<typeof compile>) => { r.demandGroups![1].groupId = 'home-a' }],
    ['outside city', (r: ReturnType<typeof compile>) => { r.demandGroups![1].sourceLocation.position.y = 112 }],
    ['partial spatial contract', (r: ReturnType<typeof compile>) => { delete r.topologyRevision }],
  ])('rejects %s', (_name, mutate) => {
    const request = compile(); mutate(request); expect(validSpatialDemand(request)).toBe(false)
  })
  it('rejects empty drafts, missing service settings and over 100 actual UEs', () => {
    expect(() => compileCityDemand([], {}, 1)).toThrow()
    expect(() => compileCityDemand(rows, {}, 1)).toThrow()
    expect(() => compileCityDemand([{ ...rows[0], ueCount: 101 }], { web_browsing: service }, 1)).toThrow()
  })
})
