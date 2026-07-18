import { describe, it, expect, beforeEach } from 'vitest'
import { handleMessage } from '../websocket'
import { useAppStore } from '../../store/appStore'
import type { SliceStatus, PodEvent } from '../../types'

beforeEach(() => {
  useAppStore.getState().reset()
})

describe('handleMessage dispatch', () => {
  it('event_started with AGENT_RUNNING sets runtime_priming stage and logs', () => {
    const before = useAppStore.getState().agentLog.length
    handleMessage({
      type: 'event_started',
      payload: { executionId: 'exec-1', eventType: 'concert', status: 'AGENT_RUNNING' },
    })
    const state = useAppStore.getState()
    expect(state.orchestrationStage).toBe('runtime_priming')
    expect(state.agentLog.length).toBeGreaterThan(before)
  })

  it('event_blocked sets blocked stage, clears active event and simulating', () => {
    useAppStore.getState().setActiveEvent('concert')
    useAppStore.getState().setSimulating(true)
    handleMessage({
      type: 'event_blocked',
      payload: { error: 'SLA violation', detail: 'too much load' },
    })
    const state = useAppStore.getState()
    expect(state.orchestrationStage).toBe('blocked')
    expect(state.activeEvent).toBeNull()
    expect(state.isSimulating).toBe(false)
  })

  it('free5gc_status updates free5gcStatus and metrics', () => {
    handleMessage({
      type: 'free5gc_status',
      payload: {
        connected: true,
        source: 'free5gc',
        subscribers: [],
        eventSubscribers: [],
        metrics: { upfCpuPercent: 42, upfPodCount: 1, amfPodCount: 1, gtpPacketsPerSec: 0, pduSessionCount: 0, latencyMs: 5, throughputMbps: 10, timestamp: Date.now() },
        slices: [],
        checkedAt: new Date().toISOString(),
      },
    })
    const state = useAppStore.getState()
    expect(state.free5gcStatus).not.toBeNull()
    expect(state.metrics?.upfCpuPercent).toBe(42)
  })

  it('agent_decision sets agentDecision and planning stage', () => {
    handleMessage({
      type: 'agent_decision',
      payload: {
        agentName: 'orchestrator',
        riskLevel: 'low',
        decision: 'scale up',
        actions: [],
        expectedOutcome: 'improved throughput',
        startedAt: new Date().toISOString(),
      },
    })
    const state = useAppStore.getState()
    expect(state.agentDecision).not.toBeNull()
    expect(state.orchestrationStage).toBe('planning')
  })

  it('agent_decision with executionId is stored without error', () => {
    handleMessage({
      type: 'agent_decision',
      payload: {
        executionId: 'exec-123',
        agentName: 'orchestrator',
        riskLevel: 'low',
        decision: 'scale up',
        actions: [],
        expectedOutcome: 'improved throughput',
        startedAt: new Date().toISOString(),
      },
    })
    const state = useAppStore.getState()
    expect(state.agentDecision?.executionId).toBe('exec-123')
  })

  it('event_reset clears active event and simulating, resets orchestration stage', () => {
    useAppStore.getState().setActiveEvent('concert')
    useAppStore.getState().setSimulating(true)
    useAppStore.getState().setOrchestrationStage('planning')
    handleMessage({
      type: 'event_reset',
      payload: { executionId: 'exec-1', eventType: 'concert', status: 'AGENT_CANCELLED', reason: 'Scenario was reset during execution.' },
    })
    const state = useAppStore.getState()
    expect(state.orchestrationStage).toBe('idle')
    expect(state.activeEvent).toBeNull()
    expect(state.isSimulating).toBe(false)
  })

  it('slice_update replaces slices with payload array', () => {
    const slices: SliceStatus[] = [
      { sst: 1, type: 'eMBB', sd: '000001', load: 55, sessions: 3, trend: 'up' },
    ]
    handleMessage({ type: 'slice_update', payload: slices })
    expect(useAppStore.getState().slices).toEqual(slices)
  })

  it('runtime_priming sets runtimePrime status to running', () => {
    handleMessage({
      type: 'runtime_priming',
      payload: { eventType: 'concert', executionId: 'exec-2' },
    })
    expect(useAppStore.getState().runtimePrime?.status).toBe('running')
  })

  it('runtime_primed updates runtimePrime and sets traffic_observed stage', () => {
    handleMessage({
      type: 'runtime_primed',
      payload: { eventType: 'concert', status: 'success', observedBeforePlanning: true, observedScenarios: ['concert'] },
    })
    const state = useAppStore.getState()
    expect(state.runtimePrime?.observedBeforePlanning).toBe(true)
    expect(state.orchestrationStage).toBe('traffic_observed')
  })

  it('metrics_update is a no-op', () => {
    const before = useAppStore.getState()
    const beforeSnapshot = {
      metrics: before.metrics,
      metricsHistory: before.metricsHistory,
      networkSnapshot: before.networkSnapshot,
      pods: before.pods,
      slices: before.slices,
    }
    handleMessage({ type: 'metrics_update', payload: { upfCpuPercent: 999 } })
    const after = useAppStore.getState()
    expect(after.metrics).toBe(beforeSnapshot.metrics)
    expect(after.metricsHistory).toBe(beforeSnapshot.metricsHistory)
    expect(after.networkSnapshot).toBe(beforeSnapshot.networkSnapshot)
    expect(after.pods).toBe(beforeSnapshot.pods)
    expect(after.slices).toBe(beforeSnapshot.slices)
  })

  it('pod_event adds the pod to the matching component', () => {
    const podEvent: PodEvent = {
      event: 'ADDED',
      pod: 'upf-0',
      phase: 'Running',
      component: 'UPF',
      namespace: 'free5gc',
      timestamp: new Date().toISOString(),
    }
    handleMessage({ type: 'pod_event', payload: podEvent })
    const upf = useAppStore.getState().pods.find((c) => c.component === 'UPF')
    expect(upf?.pods.some((p) => p.name === 'upf-0' && p.phase === 'Running')).toBe(true)
  })

  it('agent_action updates agent action status and httpStatus', () => {
    // First set an agent decision with actions
    handleMessage({
      type: 'agent_decision',
      payload: {
        agentName: 'orchestrator',
        riskLevel: 'medium',
        decision: 'execute scaling action',
        actions: [
          { type: 'k8s_hpa', description: 'Scale UPF replicas', status: 'pending' },
          { type: 'nef_qos', description: 'Apply QoS policy', status: 'pending' },
        ],
        expectedOutcome: 'improved performance',
        startedAt: new Date().toISOString(),
      },
    })

    // Update the first action's status to running
    handleMessage({
      type: 'agent_action',
      payload: {
        agentName: 'orchestrator',
        index: 0,
        status: 'running',
      },
    })

    let state = useAppStore.getState()
    expect(state.agentDecision?.agentName).toBe('orchestrator')
    expect(state.agentDecision?.actions[0].status).toBe('running')
    expect(state.agentDecision?.actions[1].status).toBe('pending')

    // Update the same action to success with httpStatus
    handleMessage({
      type: 'agent_action',
      payload: {
        agentName: 'orchestrator',
        index: 0,
        status: 'success',
        httpStatus: 201,
      },
    })

    state = useAppStore.getState()
    const action = state.agentDecision?.actions[0]
    expect(action?.status).toBe('success')
    expect(action?.httpStatus).toBe(201)
  })

  it('network_snapshot updates snapshot and derived fields without error', () => {
    const before = useAppStore.getState()
    const minimalSnapshot = {
      id: 'snap-001',
      timestamp: Date.now(),
      source: 'eks+prometheus',
      metrics: {
        upfCpuPercent: 45,
        upfPodCount: 2,
        amfPodCount: 1,
        gtpPacketsPerSec: 1000,
        pduSessionCount: 50,
        latencyMs: 10,
        throughputMbps: 500,
        timestamp: Date.now(),
      },
      slices: [
        { sst: 1, type: 'eMBB' as const, sd: '000001', load: 60, sessions: 5, trend: 'up' as const },
      ],
      edges: [
        {
          id: 'edge-1',
          sourceNodeId: 'gnb',
          targetNodeId: 'upf',
          sliceType: 'eMBB' as const,
          active: true,
          throughputMbps: 400,
          uplinkMbps: 150,
          downlinkMbps: 250,
          latencyMs: 10,
        },
      ],
    }

    handleMessage({
      type: 'network_snapshot',
      payload: minimalSnapshot,
    })

    const state = useAppStore.getState()
    expect(state.networkSnapshot).not.toBeNull()
    expect(state.networkSnapshot?.id).toBe('snap-001')
    expect(state.metrics?.throughputMbps).toBe(500)
    expect(state.packetFlows.length).toBeGreaterThan(0)
    expect(state.previousNetworkSnapshot).toBe(before.networkSnapshot)
  })
})
