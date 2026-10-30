import { describe, expect, it } from 'vitest'
import { infraTarget, parseInfraSnapshot } from './infra'
import { createCityPreset } from '../data/presets'

describe('Infra selection and observation boundaries', () => {
  it('resolves NF and application objects to their host and excludes ordinary buildings', () => {
    const { map } = createCityPreset()
    expect(infraTarget(map, map.get('nf-upf-embb')!)?.id).toBe('distributed-north')
    expect(infraTarget(map, map.get('nf-nrf')!)?.id).toBe('central')
    expect(infraTarget(map, map.get('edge-sw-app')!)?.id).toBe('edge-sw')
    expect(infraTarget(map, map.get('gnb-0-2')!)?.id).toBe('gnb-0-2')
    expect(infraTarget(map, map.get('home-0')!)).toBeNull()
    map.uninstall('core')
    expect(infraTarget(map, null)).toBeNull()
  })
  it('distinguishes an observed empty inventory from unavailable or malformed data', () => {
    const empty = { schemaVersion: 'cityverse.city-infra/v1', source: 'kubernetes-pod-spec', observedAt: '2026-10-29T00:00:00Z', namespace: 'lab', pods: [], usageAvailable: false }
    expect(parseInfraSnapshot(empty).pods).toEqual([])
    expect(() => parseInfraSnapshot({ error: 'unavailable' })).toThrow()
    expect(() => parseInfraSnapshot({ ...empty, observedAt: null })).toThrow()
    expect(() => parseInfraSnapshot({ ...empty, observedAt: 0 })).toThrow()
    expect(() => parseInfraSnapshot({ ...empty, pods: [{}] })).toThrow()
  })
})
