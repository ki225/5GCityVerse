import type { IntentOperation, IntentSubmission, ServiceIntentView } from '../../services/intents'
import type { ScenarioSnapshot } from './onpremScenarios'
import type { RoundObservability } from './roundObservability'

/** Current protected GET state, not a synthesized tool call or native effect. */
export function formalAgentControl(intent: ServiceIntentView | null | undefined,
  submission: IntentSubmission | null | undefined, round: ScenarioSnapshot | null,
  responseObservedAt: number | undefined, now: number): RoundObservability['agent'] {
  const op = intent?.latest_operation as (IntentOperation & { kind?: string; expected_traffic_request_id?: string }) | undefined
  const control = op?.control
  if (!intent || !submission || !round?.roundId || !round.requestId || !op || !control
    || intent.intent_id !== submission.intent_id || op.intent_id !== intent.intent_id
    || op.operation_id !== submission.operation_id || op.kind === 'release' || op.status !== 'running'
    || submission.traffic_request_id !== round.requestId || op.expected_traffic_request_id !== round.requestId
    || !['planner', 'executor', 'validator'].includes(control.role ?? '')
    || typeof control.phase !== 'string' || !/^[a-z][a-z0-9_]{0,63}$/.test(control.phase)
    || !control.nodes || control.nodes.length !== 3 || new Set(control.nodes).size !== 3
    || !['planner', 'executor', 'validator'].every(role => control.nodes!.includes(role))
    || !Number.isFinite(responseObservedAt) || now - responseObservedAt! < -1000 || now - responseObservedAt! > 15000) return null
  return { role: control.role, phase: control.phase, status: op.status,
    updatedAt: new Date(responseObservedAt!).toISOString(), tool: null, cycle: null }
}
