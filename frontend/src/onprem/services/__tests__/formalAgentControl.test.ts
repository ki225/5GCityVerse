import { expect, it } from 'vitest'
import { formalAgentControl } from '../formalAgentControl'
import type { ScenarioSnapshot } from '../onpremScenarios'
import type { IntentSubmission, ServiceIntentView } from '../../../services/intents'

function fixture() {
  const now = Date.now()
  const round = { roundId: 'round-1', requestId: 'traffic-1' } as ScenarioSnapshot
  const submission: IntentSubmission = { intent_id: 'intent-1', operation_id: 'op-1', status: 'queued', traffic_request_id: 'traffic-1' }
  const intent: ServiceIntentView = { intent_id: 'intent-1', revision: 1, latest_operation: {
    intent_id: 'intent-1', operation_id: 'op-1', status: 'running',
    control: { nodes: ['planner', 'executor', 'validator'], role: 'planner', phase: 'plan' },
    expected_traffic_request_id: 'traffic-1', kind: 'reconcile',
  } as ServiceIntentView['latest_operation'] }
  return { now, round, submission, intent }
}

it('projects an exact, freshly read formal operation without inventing tool calls', () => {
  const f = fixture()
  expect(formalAgentControl(f.intent, f.submission, f.round, f.now, f.now)).toMatchObject({
    role: 'planner', phase: 'plan', status: 'running', tool: null, cycle: null,
  })
})

it('does not reuse old intents, operations, traffic requests, release operations or stale responses', () => {
  const f = fixture()
  expect(formalAgentControl(f.intent, { ...f.submission, intent_id: 'old' }, f.round, f.now, f.now)).toBeNull()
  expect(formalAgentControl(f.intent, { ...f.submission, operation_id: 'old' }, f.round, f.now, f.now)).toBeNull()
  expect(formalAgentControl(f.intent, f.submission, { ...f.round, requestId: 'other' }, f.now, f.now)).toBeNull()
  expect(formalAgentControl(f.intent, f.submission, f.round, f.now - 16000, f.now)).toBeNull()
  expect(formalAgentControl(f.intent, f.submission, f.round, undefined, f.now)).toBeNull()
  ;(f.intent.latest_operation as unknown as { kind: string }).kind = 'release'
  expect(formalAgentControl(f.intent, f.submission, f.round, f.now, f.now)).toBeNull()
})

it('does not represent a blocked or malformed graph as a currently executing agent', () => {
  const f = fixture()
  f.intent.latest_operation!.status = 'blocked'
  expect(formalAgentControl(f.intent, f.submission, f.round, f.now, f.now)).toBeNull()
  f.intent.latest_operation!.status = 'running'
  f.intent.latest_operation!.control!.nodes = ['planner', 'planner', 'validator']
  expect(formalAgentControl(f.intent, f.submission, f.round, f.now, f.now)).toBeNull()
})
