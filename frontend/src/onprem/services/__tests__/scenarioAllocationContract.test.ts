import { describe, expect, it } from 'vitest'
import { allocationAdmitted, parseScenarioAllocation, type ScenarioRequest } from '../onpremScenarios'
import { SCENARIO_EVENTS } from '../../../domain/scenarios'
import { validateCityIntent } from '../../../services/cityIntent'
const empty = Object.fromEntries(SCENARIO_EVENTS.map(key => [key, 0])) as ScenarioRequest['events']
const caps = { start: true, applicationMaxUes: 100, verifiedMaxUes: 0, verifiedMaxUesScope: 'scenario-offered-load-v1', commissioningEnabled: true, candidateMaxUes: 100, admissionMaxUes: 100, commissioningProfile: 'scenario-offered-load-v1', staticPolicy: false, aiPolicy: false } as const
function request(events = empty, baselineUes = 0) {
  const groups = Object.entries({ web_browsing: baselineUes, ...events }).filter(([, count]) => count > 0)
  return { text: 'Protect the specified city services for 60 seconds.',
    city_traffic: { requestId: 'city-request', strategy: 'none' as const, trafficMode: 'qos' as const, baselineUes, events, topologyRevision: 1,
      demandGroups: groups.map(([profileId, ueCount]) => ({ groupId: profileId, profileId: profileId as 'web_browsing', ueCount, sourceLocation: { type: 'point' as const, position: { x: 6, y: 6 } } })),
      serviceRequirements: { schemaVersion: 'cityverse.service-requirements/v2' as const, services: Object.fromEntries(groups.map(([event]) => [event, { traffic: { protocol: 'udp', offeredUplinkMbpsPerUe: 1, offeredDownlinkMbpsPerUe: 1 } }])) } } }
}
describe('formal city intent allocation', () => {
  it.each(SCENARIO_EVENTS.flatMap(key => [1, 10, 50, 99, 100].map(count => [key, count] as const)))('keeps %s / %s actual UEs in one formal envelope', (key, count) => {
    const value = request({ ...empty, [key]: count })
    expect(() => validateCityIntent(value)).not.toThrow()
    const allocation = parseScenarioAllocation('0', Object.fromEntries(SCENARIO_EVENTS.map(event => [event, String(value.city_traffic.events[event])])) as Record<typeof SCENARIO_EVENTS[number], string>)!
    expect(allocation.total).toBe(count); expect(allocationAdmitted(caps, allocation)).toBe(true)
    expect(value.city_traffic.demandGroups[0].ueCount).toBe(count)
  })
  it.each([0, -1, 101, 1.5])('rejects invalid total %s', count => expect(() => validateCityIntent(request({ ...empty, video_streaming: count }))).toThrow())
  it('conserves mixed baseline and event populations', () => {
    const value = request({ ...empty, video_streaming: 30, realtime_control: 10 }, 20)
    expect(() => validateCityIntent(value)).not.toThrow()
    expect(value.city_traffic.demandGroups.reduce((sum, row) => sum + row.ueCount, 0)).toBe(60)
  })
})
