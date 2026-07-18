import { describe, it, expect } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { AgentPanel, baselinePreservationText, baselinePreservationTone, buildDecisionReason, buildDecisionTimelineStages, buildPlainSummary, isBaselinePreservationCheck, selectPrimarySlice, skippedReason } from './AgentPanel'
import type { AgentAction, AgentDecision, AgentVerification, SliceStatus } from '../../types'

function baseDecision(overrides: Partial<AgentDecision> = {}): AgentDecision {
  return {
    agentName: 'test-agent',
    riskLevel: 'medium',
    decision: 'scale up',
    actions: [],
    expectedOutcome: 'improved throughput',
    startedAt: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('buildDecisionTimelineStages', () => {
  const idleInput = {
    runtimePrime: null,
    decision: null,
    orchestrationStage: 'idle',
    isSimulating: false,
    locale: 'zh-TW' as const,
  }

  it('shows five readable waiting summaries before a scenario starts', () => {
    const stages = buildDecisionTimelineStages(idleInput)
    expect(stages.map((stage) => stage.key)).toEqual(['intent', 'plan', 'scope', 'execute', 'verify'])
    expect(stages).toHaveLength(5)
    expect(stages.every((stage) => stage.status === 'waiting')).toBe(true)
    expect(stages.every((stage) => stage.summary.length > 0)).toBe(true)
  })

  it('summarizes measured traffic before planning starts', () => {
    const stages = buildDecisionTimelineStages({
      ...idleInput,
      runtimePrime: {
        status: 'success',
        observedBeforePlanning: true,
        observedScenarios: ['concert'],
      },
      orchestrationStage: 'traffic_rendered',
      isSimulating: true,
    })
    expect(stages[0]).toMatchObject({ key: 'intent', status: 'complete' })
    expect(stages[0].summary).toContain('真的進入網路')
    expect(stages[0].details).toContain('concert')
    expect(stages[0].details).toContain('bearer／iperf')
    expect(stages[1]).toMatchObject({ key: 'plan', status: 'active' })
    expect(stages[1].summary).not.toContain('SLA')
    expect(stages[1].details).toContain('SLA')
  })

  it('keeps honest summaries for plan, change scope, execution, and measured SLA results', () => {
    const decision = baseDecision({
      intent: {
        eventType: 'concert',
        targetSlice: { name: 'eMBB', fiveQi: 9 },
        sla: { latencyMsMax: 30, minThroughputMbps: 25 },
      } as unknown as AgentDecision['intent'],
      selectedPlan: {
        name: 'eMBB capacity rebalance',
        rationale: 'Measured concert traffic is congested.',
        expectedImpact: 'Improve live streaming',
      },
      actions: [
        { type: 'nef_qos', description: 'apply QoS', status: 'success' },
        { type: 'k8s_hpa', description: 'scale UPF', status: 'skipped' },
      ],
      verificationSummary: { status: 'passed' },
      validationReport: {
        scenario: 'concert',
        phase: 'verify',
        baseline_captured: { source: 'iperf3', per_slice_throughput_mbps: {}, total_pdu_sessions: 2, upf_cpu_percent: 10 },
        steps_completed: '5/5',
        required_steps: [],
        nef_apis_called: [],
        nef_apis_required: [],
        sla_result: {
          latency_ms: { value: 19, threshold: 30, passed: true },
          throughput_mbps: { value: 32, threshold: 25, passed: true, delta_from_baseline: 14 },
          isolation_check: { max_degradation_percent: 0, passed: true },
          status: 'passed',
          data_source: 'iperf3',
        },
        k8s_scaling_observed: {},
        improvements_vs_previous: [],
        remaining_issues: [],
      },
    })
    const stages = buildDecisionTimelineStages({ ...idleInput, decision, orchestrationStage: 'complete' })
    expect(stages[1]).toMatchObject({ status: 'complete' })
    expect(stages[1].summary).toContain('總頻寬不會增加')
    expect(stages[1].details).toContain('eMBB capacity rebalance')
    expect(stages[2]).toMatchObject({ key: 'scope', title: '確認影響範圍', status: 'complete' })
    expect(stages[2].summary).toContain('網路功能與資源')
    expect(stages[2].details).toContain('NF／API：nef_qos、k8s_hpa')
    expect(stages[2].details).toContain('QoS：5QI 9')
    expect(stages[2].details).toContain('資源總量固定 100%')
    expect(`${stages[2].summary}${stages[2].details}`).not.toContain('安全檢查')
    expect(stages[3]).toMatchObject({ status: 'complete' })
    expect(stages[3].summary).toContain('使用體驗是否真的改善')
    expect(stages[3].details).toContain('成功 1、失敗 0、略過 1')
    expect(stages[4]).toMatchObject({ status: 'complete' })
    expect(stages[4].summary).toContain('觀眾應能更穩定觀看')
    expect(stages[4].details).toContain('延遲 19/≤30 ms')
    expect(stages[4].details).toContain('吞吐 32/≥25 Mbps')
  })

  it('shows the current action while execution is in progress', () => {
    const decision = baseDecision({
      selectedPlan: { name: 'plan', rationale: 'reason', expectedImpact: 'impact' },
      actions: [
        { type: 'nef_qos', description: 'completed action', status: 'success' },
        { type: 'k8s_hpa', description: 'scale UPF now', status: 'running' },
      ],
    })
    const execute = buildDecisionTimelineStages({ ...idleInput, decision, orchestrationStage: 'executing', isSimulating: true })[3]
    expect(execute.status).toBe('active')
    expect(execute.summary).toContain('正在逐項套用調整')
    expect(execute.details).toContain('1/2')
    expect(execute.details).toContain('scale UPF now')
  })

  it('treats backend running verification as active until a terminal SLA result arrives', () => {
    const decision = baseDecision({
      actions: [{ type: 'prometheus', description: 'verify post-change metrics', status: 'running' }],
      verificationSummary: { status: 'running' } as unknown as AgentDecision['verificationSummary'],
    })
    const verify = buildDecisionTimelineStages({ ...idleInput, decision, orchestrationStage: 'verification', isSimulating: true })[4]
    expect(verify.status).toBe('active')
    expect(verify.summary).toContain('現在還不能下結論')
    expect(verify.details).toContain('等待執行後實測結果')
  })

  it('surfaces missing traffic and failed SLA instead of reporting success', () => {
    const blocked = buildDecisionTimelineStages({
      ...idleInput,
      runtimePrime: { status: 'traffic_not_observed', missingScenarios: ['medical'] },
      orchestrationStage: 'blocked',
    })
    expect(blocked[0]).toMatchObject({ status: 'failed' })
    expect(blocked[0].summary).toContain('不讓 AI 猜測')
    expect(blocked[0].details).toContain('medical')
    expect(blocked[1]).toMatchObject({ status: 'failed' })
    expect(blocked[2]).toMatchObject({ status: 'warning' })
    expect(blocked[2]).toMatchObject({ key: 'scope', title: '確認影響範圍', status: 'warning' })
    expect(blocked[2].summary).toContain('沒有準備任何網路變更')
    expect(blocked[3]).toMatchObject({ status: 'warning' })
    expect(blocked[3].summary).toContain('選擇不動網路')
    expect(blocked[4]).toMatchObject({ status: 'warning' })
    expect(blocked[4].summary).toContain('無法比較調整前後')

    const failedDecision = baseDecision({ verificationSummary: { status: 'failed' } })
    const verify = buildDecisionTimelineStages({ ...idleInput, decision: failedDecision, orchestrationStage: 'complete' })[4]
    expect(verify.status).toBe('failed')
    expect(verify.summary).toContain('可能繼續遇到卡頓或延遲')
    expect(verify.details).toContain('SLA failed')
  })

  it('hides backend network_round wording behind a plain-language experience statement', () => {
    const decision = baseDecision({
      intent: {
        eventType: 'network_round',
        targetSlice: { name: 'eMBB', fiveQi: 9 },
        sla: { latencyMsMax: 50, minThroughputMbps: 1 },
      } as unknown as AgentDecision['intent'],
    })
    const intent = buildDecisionTimelineStages({ ...idleInput, decision, orchestrationStage: 'planning' })[0]
    expect(intent.summary).toContain('直播、視訊與一般上網維持順暢')
    expect(intent.summary).not.toContain('network_round')
    expect(intent.details).toContain('延遲 ≤ 50 ms')
  })
})

describe('AgentPanel beginner-facing render', () => {
  it('shows the five-stage summary and actual change scope without legacy engineering consoles or unsupported guardrails', () => {
    const markup = renderToStaticMarkup(createElement(AgentPanel))

    expect(markup).toContain('AI 決策時間軸')
    expect(markup).toContain('確認影響範圍')
    expect(markup).toContain('NF / API')
    expect(markup).toContain('Pods')
    expect(markup).toContain('QoS')
    expect(markup).toContain('總量固定 100%')
    for (const legacyLabel of [
      'Dry run',
      'RBAC',
      'Policy check',
      'Rollback',
      'Decision Inputs',
      'Intent',
      'Hypotheses',
      'Actions',
      'Verification Loop',
      'Scenario Validation',
      'AI decision history',
      'AI 決策紀錄',
      '技術證據（除錯用）',
    ]) {
      expect(markup).not.toContain(legacyLabel)
    }
  })
})

describe('buildPlainSummary', () => {
  it('returns an empty string when there is no decision', () => {
    expect(buildPlainSummary(null)).toBe('')
    expect(buildPlainSummary(undefined)).toBe('')
  })

  it('builds a full sentence when event, actions, verification status, and metrics are all present', () => {
    const decision = baseDecision({
      intent: { eventType: 'typhoon' } as AgentDecision['intent'],
      actions: [{ type: 'k8s_hpa', description: 'scale UPF', status: 'success' }, { type: 'nef_qos', description: 'raise QoS', status: 'success' }],
      verificationSummary: { status: 'passed' },
      verification: [
        { metric: 'latencyMs', before: 12, target: '<= 50', status: 'passed', passCondition: 'latencyMs must stay at or below 50' },
        { metric: 'throughputMbps', before: 120, target: '>= 100', status: 'passed', passCondition: 'throughputMbps must be at least 100' },
      ],
    })

    const summary = buildPlainSummary(decision)
    expect(summary).toContain('AI 偵測到typhoon事件')
    expect(summary).toContain('執行2項網路調度')
    expect(summary).toContain('驗證全部達標')
    expect(summary).toContain('延遲 12 ms（門檻 <= 50）')
    expect(summary).toContain('吞吐 120 Mbps（門檻 >= 100）')
  })

  it('reports partial failure when verification is degraded', () => {
    const decision = baseDecision({
      intent: { eventType: 'concert' } as AgentDecision['intent'],
      actions: [{ type: 'k8s_hpa', description: 'scale UPF', status: 'success' }],
      verificationSummary: { status: 'degraded' },
    })
    expect(buildPlainSummary(decision)).toContain('驗證部分未達標')
  })

  it('omits clauses for missing fields instead of printing undefined or NaN', () => {
    const decision = baseDecision()
    const summary = buildPlainSummary(decision)
    expect(summary).not.toContain('undefined')
    expect(summary).not.toContain('NaN')
    expect(summary).toBe('')
  })

  it('omits the metric sub-clause when only one of latency/throughput checks is present', () => {
    const decision = baseDecision({
      intent: { eventType: 'medical' } as AgentDecision['intent'],
      verification: [
        { metric: 'latencyMs', before: 8, target: '<= 20', status: 'passed', passCondition: 'latencyMs must stay at or below 20' },
      ],
    })
    const summary = buildPlainSummary(decision)
    expect(summary).toContain('延遲 8 ms（門檻 <= 20）')
    expect(summary).not.toContain('吞吐')
    expect(summary).not.toContain('undefined')
  })

  it('appends the baseline preservation clause when verification passed and the check is present', () => {
    const decision = baseDecision({
      intent: { eventType: 'concert' } as AgentDecision['intent'],
      actions: [{ type: 'k8s_hpa', description: 'scale UPF', status: 'success' }],
      verificationSummary: { status: 'passed' },
      verification: [
        { metric: 'baselinePreservationMbps', before: 42, target: 30, status: 'passed', passCondition: 'baseline must stay at or above floor' },
      ],
    })
    expect(buildPlainSummary(decision)).toContain('市民日常流量維持 42 Mbps 未受影響')
  })

  it('omits the baseline preservation clause when the field is missing', () => {
    const decision = baseDecision({
      intent: { eventType: 'concert' } as AgentDecision['intent'],
      actions: [{ type: 'k8s_hpa', description: 'scale UPF', status: 'success' }],
      verificationSummary: { status: 'passed' },
    })
    const summary = buildPlainSummary(decision)
    expect(summary).not.toContain('市民日常流量')
    expect(summary).not.toContain('undefined')
  })

  it('omits the baseline preservation clause when verification did not pass', () => {
    const decision = baseDecision({
      intent: { eventType: 'concert' } as AgentDecision['intent'],
      verificationSummary: { status: 'degraded' },
      verification: [
        { metric: 'baselinePreservationMbps', before: 42, target: 30, status: 'degraded', passCondition: 'baseline must stay at or above floor' },
      ],
    })
    expect(buildPlainSummary(decision)).not.toContain('市民日常流量')
  })
})

describe('isBaselinePreservationCheck', () => {
  it('matches the baselinePreservationMbps metric', () => {
    expect(isBaselinePreservationCheck({ metric: 'baselinePreservationMbps' })).toBe(true)
  })

  it('does not match other metrics', () => {
    expect(isBaselinePreservationCheck({ metric: 'throughputMbps' })).toBe(false)
  })
})

describe('baselinePreservationTone', () => {
  it('returns green tone for passed/pass', () => {
    expect(baselinePreservationTone('passed')).toContain('green')
    expect(baselinePreservationTone('pass')).toContain('green')
  })

  it('returns red tone for failed/fail', () => {
    expect(baselinePreservationTone('failed')).toContain('red')
    expect(baselinePreservationTone('fail')).toContain('red')
  })

  it('returns a neutral tone for inconclusive/other statuses', () => {
    expect(baselinePreservationTone('degraded')).toContain('slate')
    expect(baselinePreservationTone('pending')).toContain('slate')
  })
})

describe('baselinePreservationText', () => {
  function baseCheck(overrides: Partial<AgentVerification> = {}): AgentVerification {
    return {
      metric: 'baselinePreservationMbps',
      before: 42,
      target: 30,
      status: 'passed',
      passCondition: 'baseline must stay at or above floor',
      ...overrides,
    }
  }

  it('renders measured vs floor with a checkmark when passed', () => {
    expect(baselinePreservationText(baseCheck())).toBe('實測 42 Mbps ≥ 保障線 30 Mbps ✓')
  })

  it('omits the checkmark when not passed', () => {
    expect(baselinePreservationText(baseCheck({ status: 'failed' }))).toBe('實測 42 Mbps ≥ 保障線 30 Mbps')
  })
})

describe('selectPrimarySlice', () => {
  function slice(overrides: Partial<SliceStatus> = {}): SliceStatus {
    return {
      sst: 1,
      type: 'eMBB',
      sd: '000001',
      load: 0,
      sessions: 0,
      trend: 'stable',
      ...overrides,
    }
  }

  it('picks the highest-load stressed slice instead of the first matching one', () => {
    const slices: SliceStatus[] = [
      slice({ type: 'eMBB', load: 7, trend: 'up' }),
      slice({ type: 'mMTC', load: 86, trend: 'up' }),
    ]
    const primary = selectPrimarySlice(slices)
    expect(primary?.type).toBe('mMTC')
    expect(primary?.load).toBe(86)

    const reason = buildDecisionReason('high', primary?.type, primary?.load ?? 0, 0, false)
    expect(reason).toContain('mMTC')
    expect(reason).toContain('86%')
  })

  it('falls back to the highest-load slice overall when every slice is below 30% load', () => {
    const slices: SliceStatus[] = [
      slice({ type: 'eMBB', load: 12, trend: 'stable' }),
      slice({ type: 'mMTC', load: 25, trend: 'stable' }),
      slice({ type: 'URLLC', load: 5, trend: 'stable' }),
    ]
    const primary = selectPrimarySlice(slices)
    expect(primary?.type).toBe('mMTC')
    expect(primary?.load).toBe(25)

    const noDominantPressure = slices.every((s) => s.load < 30)
    const reason = buildDecisionReason('high', primary?.type, primary?.load ?? 0, 0, noDominantPressure)
    expect(reason).toBe('no dominant slice pressure (highest mMTC at 25%); preemptive orchestration due to high risk')
  })
})

describe('skippedReason', () => {
  function baseAction(overrides: Partial<AgentAction> = {}): AgentAction {
    return {
      type: 'k8s_hpa',
      description: 'patch_hpa',
      status: 'skipped',
      ...overrides,
    }
  }

  it('returns the reason string from action.result when present', () => {
    const action = baseAction({ result: { reason: 'EKS_CLUSTER_NAME is not configured' } })
    expect(skippedReason(action)).toBe('EKS_CLUSTER_NAME is not configured')
  })

  it('returns an empty string when result has no reason', () => {
    expect(skippedReason(baseAction({ result: { httpStatus: 200 } }))).toBe('')
  })

  it('returns an empty string when result is missing entirely', () => {
    expect(skippedReason(baseAction())).toBe('')
  })

  it('returns an empty string when result.reason is not a string', () => {
    expect(skippedReason(baseAction({ result: { reason: 42 } }))).toBe('')
  })
})
