import type { FlowDirection, FlowEdgeState } from '../types'

export interface TrafficVisualScale {
  particleCount: number
  particleRadius: number
  strokeWidth: number
}

/**
 * Log scaling keeps low-rate URLLC/mMTC traffic visible while preserving an
 * obvious visual gap between (for example) 5, 150 and 800 Mbps flows.
 */
export function trafficVisualScale(throughputMbps: number): TrafficVisualScale {
  const throughput = Math.max(0, finite(throughputMbps))
  const magnitude = Math.log10(1 + throughput)
  return {
    particleCount: Math.round(clamp(1 + magnitude * 2.4, 1, 12)),
    particleRadius: clamp(2.2 + magnitude * 0.65, 2.2, 4.8),
    strokeWidth: clamp(1.2 + Math.sqrt(throughput) / 10, 1.2, 8),
  }
}

export class FlowEdgeModel {
  constructor(private readonly edge: FlowEdgeState) {}

  get id(): string {
    return this.edge.id
  }

  get active(): boolean {
    return this.edge.active && this.throughputMbps > 0
  }

  get throughputMbps(): number {
    return Math.max(0, finite(this.edge.throughputMbps))
  }

  get latencyMs(): number {
    return Math.max(1, finite(this.edge.latencyMs, 1))
  }

  get direction(): FlowDirection {
    const uplink = finite(this.edge.uplinkMbps)
    const downlink = finite(this.edge.downlinkMbps)
    const total = uplink + downlink
    if (total <= 0 || Math.abs(uplink - downlink) / Math.max(total, 1) < 0.08) return 'idle'
    return uplink > downlink ? 'forward' : 'reverse'
  }

  get animationDuration(): number {
    const latencyFactor = Math.min(this.latencyMs, 500) / 500
    const throughputMagnitude = Math.log10(1 + this.throughputMbps)
    return clamp(1.35 - throughputMagnitude * 0.22 + latencyFactor * 0.85, 0.42, 1.7)
  }

  get strokeWidth(): number {
    return trafficVisualScale(this.throughputMbps).strokeWidth
  }

  get particleCount(): number {
    return trafficVisualScale(this.throughputMbps).particleCount
  }

  get particleRadius(): number {
    return trafficVisualScale(this.throughputMbps).particleRadius
  }
}

function finite(value: number | undefined, fallback = 0): number {
  return Number.isFinite(value) ? Number(value) : fallback
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}
