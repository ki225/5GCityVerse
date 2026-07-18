# Agent Design Rules

## Role Model

Design agents as telecom network-operation planners. They observe network state, translate city events into network intent, choose a bounded action plan, execute through approved tools, and verify the SLA after acting.

Keep the agent grounded in the real system:

- Bedrock agent instructions live in `backend/bedrock-agent/agent-instructions.md`.
- Action schemas live in `backend/bedrock-agent/action-group-schema.yaml`.
- Cloud backend orchestration lives under `backend/aws-app/`.
- NEF tool Lambdas live under `backend/nef-tools/`.

## Decision Flow

Every network-operation agent should follow this order:

1. Observe current state with analytics/status tools.
2. Classify the city event and map it to slice, QoS, SLA, and risk.
3. Plan the minimum safe tool sequence.
4. Validate tool parameters before execution.
5. Prefer NEF/subscriber/policy changes before scaling.
6. Execute only approved bounded actions.
7. Verify the outcome with SLA/status checks.
8. Report trace, risk, actions, and verification result.

## Slice Semantics

- `SST=1` eMBB: AR concert, high throughput, streaming pressure.
- `SST=2` URLLC: medical emergency, low latency, high priority.
- `SST=3` mMTC: typhoon sensors, massive IoT, bursty device pressure.
- `SST=4` V2X: traffic accident, vehicle routing, edge sensitivity.

Do not change these mappings without updating frontend labels, backend constants, Bedrock prompts, Kubernetes manifests, and docs together.

## Tool And Safety Boundaries

- Tool schemas must be explicit, narrow, and easy for the model to validate.
- Prefer idempotent tools and deterministic response shapes.
- Reject or clamp actions that exceed HPA replica bounds, configured QoS ranges, unsupported slice IDs, or unknown scenario types.
- Never let the agent invent live infrastructure state. If state is missing, surface uncertainty and choose a safe read/verify action.
- Keep response JSON stable for downstream UI parsing.

## Multi-Agent Shape

Use a supervisor plus specialist agents only when the separation changes behavior or safety:

- Supervisor: event classification, risk assessment, sequencing, final verification.
- Traffic agent: traffic influence and V2X/eMBB routing decisions.
- Medical agent: URLLC QoS and emergency prioritization.
- Disaster agent: mMTC/PFD/large sensor surge behavior.

Specialists should not bypass the supervisor's safety envelope. Shared constants for slices, scenarios, and SLA thresholds should live in backend code, not duplicated only in prompts.
