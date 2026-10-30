import { describe, expect, it } from 'vitest'
import type { NetworkMetrics } from '../../types'
import { normalizeNetworkSnapshot } from '../networkSnapshot'

const metrics: NetworkMetrics = {
  upfCpuPercent: 0,
  upfPodCount: 1,
  amfPodCount: 1,
  gtpPacketsPerSec: 0,
  pduSessionCount: 1,
  latencyMs: 0,
  throughputMbps: 1.03,
  timestamp: 1,
  dataSource: 'kubernetes+iperf3',
}

describe('local bearer edge normalization', () => {
  it('preserves explicitly unavailable latency and downlink measurements', () => {
    const snapshot = normalizeNetworkSnapshot({
      id: 'local-live',
      timestamp: 1,
      source: 'kubernetes+iperf3',
      metrics,
      slices: [],
      edges: [{
        id: 'local-video_streaming-ran',
        sourceNodeId: 'mall',
        targetNodeId: 'gnb1',
        sliceType: 'eMBB',
        active: true,
        throughputMbps: 1.03,
        uplinkMbps: 1.03,
        downlinkMbps: null,
        latencyMs: null,
      }],
    }, metrics, [])

    expect(snapshot.edges[0]).toMatchObject({ throughputMbps: 1.03, uplinkMbps: 1.03 })
    expect(snapshot.edges[0].downlinkMbps).toBeUndefined()
    expect(snapshot.edges[0].latencyMs).toBeUndefined()
  })
})
