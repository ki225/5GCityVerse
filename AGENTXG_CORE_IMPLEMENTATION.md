# AgentxG Core Implementation Plan for 5GCityVerse

This document records how 5GCityVerse should evolve if it wants to reproduce the ideas from **AgentxG Core: Agentic AI for Next-Generation Mobile Core Network**.

Reference paper: <https://arxiv.org/html/2606.00417v1>

## 1. What the Paper Proposes

The paper proposes an Agentic AI-native layer for next-generation mobile core networks. The main idea is not to let an LLM directly control every network function, but to add an intelligent layer above the core network that can:

1. Accept high-level operator intents.
2. Observe the current network state.
3. Plan actions using a network planner agent.
4. Validate and execute actions through a network executor agent.
5. Use tools exposed through an MCP-style tool server.
6. Continuously verify whether the network intent or SLA is satisfied.

The architecture is a closed loop:

```text
Intent -> Observe -> Plan -> Execute -> Verify -> Adapt
```

The important AgentxG Core components are:

| Paper Component | Meaning |
| --- | --- |
| Intent Manager | Stores and manages operator intents, including one-shot and continuous-control intents. |
| Network Planner Agent | Reads monitoring data and creates a plan to satisfy the intent. |
| Monitor Tools | Build network context from NWDAF, Prometheus, Kubernetes, 3GPP APIs, and external context. |
| Network Executor Agent | Checks the plan, selects tools, executes actions, and records feedback. |
| Execution Tools | Perform concrete actions against MANO, Kubernetes, PCF, SMF, NEF, UPF, or subscriber systems. |
| MCP Server / Tool Gateway | Lets agents discover and invoke available network tools. |
| Closed-loop Verification | Re-observes the network after actions and adapts if the SLA is not met. |

## 2. Current 5GCityVerse State

The current project already has many building blocks, but they are not yet assembled into an AgentxG Core-style closed loop.

### Existing Strengths

| Capability | Current Location |
| --- | --- |
| Event trigger path | `backend/aws-app/app.py` |
| Fixed agent decision narration | `backend/aws-app/decision_service.py` |
| free5GC subscriber operations | `backend/aws-app/free5gc_utils.py` |
| Prometheus metrics | `backend/aws-app/metrics_service.py` |
| EKS / UERANSIM / HPA orchestration | `backend/aws-app/scenario_environment.py` |
| Bedrock agent instructions | `backend/bedrock-agent/agent-instructions.md` |
| Bedrock action schema | `backend/bedrock-agent/action-group-schema.yaml` |
| Bedrock orchestrator draft | `backend/bedrock-orchestrator/index.py` |
| NEF tool Lambdas | `backend/nef-tools/` |
| Step Functions workflow draft | `backend/step-functions/slice-reconfiguration.asl.json` |
| Frontend agent display | `frontend/src/components/AgentPanel/AgentPanel.tsx` |

### Main Gaps

1. `AgentDecisionService` is still rule-based and mostly generates explanation.
2. The current trigger path directly performs subscriber and environment actions before the agent actually plans.
3. Bedrock Agent is not yet deployed as the production decision path through Terraform.
4. The action schema lacks several tools needed for a complete loop.
5. Verification exists as UI/state metadata, but is not yet a real post-action SLA check.
6. Planner and executor are not separated.
7. Tool execution traces are not first-class runtime records.

## 3. Target Project Architecture

The target architecture should look like this:

```text
Frontend Event Console
        |
        v
HTTP API /events/trigger
        |
        v
Intent Manager
        |
        v
Network Planner Agent
        |
        +--> Monitor Tools
        |      - get_network_analytics
        |      - list_subscribers
        |      - get_free5gc_runtime_state
        |      - get_hpa_state
        |
        v
Structured Plan
        |
        v
Network Executor Agent
        |
        +--> Execution Tools through ToolGateway
               - upsert_subscriber_profile
               - activate_qos_policy
               - request_traffic_influence
               - create_pfd_rule
               - patch_hpa
               - verify_sla
        |
        v
Post-action Verification
        |
        v
Adapt or Complete
        |
        v
DynamoDB + WebSocket + Frontend Agent Trace
```

The agent should make minute-level orchestration decisions. Fast control should remain with Kubernetes HPA, Prometheus Adapter, PCF, SMF, UPF, and other lower-level controllers.

## 4. Recommended Module Changes

### 4.1 Add an Intent Manager

Create:

```text
backend/agent-runtime/intent_manager.py
```

Responsibilities:

1. Convert frontend events into structured network intents.
2. Define target slice, QoS, and SLA goals.
3. Store intent state in DynamoDB.
4. Distinguish one-shot intents from continuous-control intents.

Example intent:

```json
{
  "executionId": "uuid",
  "eventType": "medical",
  "intentType": "continuous_control",
  "targetSlice": {
    "sst": 2,
    "sd": "000002",
    "name": "URLLC"
  },
  "sla": {
    "latencyMsMax": 10,
    "minThroughputMbps": 50,
    "minPduSessions": 1,
    "maxUpfCpuPercent": 75
  }
}
```

### 4.2 Add a ToolGateway

Create:

```text
backend/agent-runtime/tool_gateway.py
backend/agent-runtime/tools/
```

The ToolGateway should wrap existing project capabilities behind stable tool contracts.

Required tools:

| Tool | Backing Implementation |
| --- | --- |
| `get_network_analytics` | `PrometheusMetricsService`, EKS HPA state, free5GC runtime state |
| `list_subscribers` | `Free5gcClient.list_subscribers` |
| `upsert_subscriber_profile` | `Free5gcClient.upsert_subscriber` / `upsert_subscribers` |
| `activate_qos_policy` | `backend/nef-tools/fn-nef-qos-subscription` |
| `request_traffic_influence` | `backend/nef-tools/fn-nef-traffic-influence` |
| `create_pfd_rule` | `backend/nef-tools/fn-nef-pfd-create` |
| `patch_hpa` | `backend/nef-tools/fn-k8s-hpa-update` or EKS Kubernetes client |
| `verify_sla` | New SLA verifier |

The gateway should enforce:

1. Tool allowlists.
2. Parameter validation.
3. Timeouts.
4. Idempotency keys.
5. Execution logging.
6. Safe bounds for sensitive actions, such as HPA max replicas.

### 4.3 Split Planner and Executor Behavior

Create:

```text
backend/agent-runtime/planner.py
backend/agent-runtime/executor.py
```

Short-term implementation:

1. Use one Bedrock Agent.
2. Use prompt sections to force Planner and Executor phases.
3. Return structured JSON.

Long-term implementation:

1. Use separate Planner and Executor agents.
2. Pass structured messages between them.
3. Store bounded memory of previous actions and outcomes.

Planner output should be declarative:

```json
{
  "riskLevel": "critical",
  "observations": [],
  "plan": [
    {
      "step": 1,
      "tool": "get_network_analytics",
      "reason": "Capture baseline before changing policy"
    },
    {
      "step": 2,
      "tool": "upsert_subscriber_profile",
      "reason": "Ensure UE is mapped to URLLC slice"
    },
    {
      "step": 3,
      "tool": "activate_qos_policy",
      "reason": "Request low-latency QoS"
    },
    {
      "step": 4,
      "tool": "verify_sla",
      "reason": "Confirm the intent was satisfied"
    }
  ]
}
```

Executor responsibilities:

1. Reject unknown tools.
2. Reject unsafe parameters.
3. Execute tools in order.
4. Stop on critical failures.
5. Save each action result.
6. Feed observations back to the planner if adaptation is needed.

### 4.4 Replace Rule-Based Decision as the Main Path

Current file:

```text
backend/aws-app/decision_service.py
```

Recommended change:

1. Rename conceptually to `FallbackDecisionService`.
2. Use it only when Bedrock Agent or ToolGateway is unavailable.
3. Do not let it be the primary decision engine.

Current trigger flow in:

```text
backend/aws-app/app.py
```

Should change from:

```text
observe metrics -> upsert subscriber -> trigger environment -> build rule-based decision
```

To:

```text
create intent -> invoke agent loop -> execute tools -> verify SLA -> store trace
```

### 4.5 Add Real SLA Verification

Create:

```text
backend/agent-runtime/sla_verifier.py
```

Verification should inspect:

1. Latency.
2. Throughput.
3. PDU session count.
4. GTP packets per second.
5. UPF CPU.
6. UPF / AMF / SMF pod counts.
7. HPA desired and current replicas.
8. Target slice load.

Example result:

```json
{
  "status": "passed",
  "checks": [
    {
      "metric": "latencyMs",
      "actual": 8.7,
      "target": "<= 10",
      "status": "passed"
    },
    {
      "metric": "pduSessionCount",
      "actual": 3,
      "target": ">= 1",
      "status": "passed"
    }
  ],
  "adaptationRequired": false
}
```

If verification fails, allow a bounded adaptation loop:

```text
maxAdaptationRounds = 1 or 2
```

This avoids unbounded autonomous changes.

### 4.6 Use Step Functions or EventBridge for Wait-and-Verify

Do not keep API Gateway requests open while waiting 15-30 seconds for metrics to stabilize.

Recommended flow:

```text
POST /events/trigger
  -> returns executionId quickly
  -> starts Step Functions workflow
  -> workflow invokes planner/executor
  -> waits 15-30 seconds
  -> invokes verify_sla
  -> optionally adapts
  -> updates DynamoDB and WebSocket
```

The existing file can evolve:

```text
backend/step-functions/slice-reconfiguration.asl.json
```

It should become less hard-coded by event type and more agent-plan driven.

### 4.7 Extend Bedrock Action Group Schema

Current file:

```text
backend/bedrock-agent/action-group-schema.yaml
```

Add operations:

```text
list_subscribers
upsert_subscriber_profile
verify_sla
patch_hpa
```

Also consider renaming:

```text
scale_upf_pods -> patch_hpa
activate_qos_subscription -> activate_qos_policy
```

The naming should describe network intent rather than a single implementation detail.

### 4.8 Update Agent Instructions

Current file:

```text
backend/bedrock-agent/agent-instructions.md
```

Update it so the agent must follow this discipline:

1. Always observe before acting.
2. Use the minimum necessary action.
3. Prefer policy/profile changes before scaling.
4. Never exceed configured HPA bounds.
5. Never modify core-network state without a tool result.
6. Always verify after acting.
7. If verification fails, adapt at most the configured number of rounds.
8. Return structured Planner, Executor, and Verification sections.

### 4.9 Deploy the Agent Path Through Terraform

Current Terraform mainly deploys:

```text
backend/aws-app
```

Terraform should add:

1. Bedrock Agent.
2. Bedrock Agent Alias.
3. Bedrock Action Group.
4. ToolGateway Lambda.
5. Bedrock Orchestrator Lambda.
6. Step Functions state machine for the agent loop.
7. IAM policies for:
   - Bedrock invoke.
   - Lambda invoke.
   - DynamoDB read/write.
   - EKS describe and patch.
   - CloudWatch logs.
8. Lambda permissions for action group invocation.

Relevant draft:

```text
backend/bedrock-orchestrator/index.py
```

This should be connected to the production path instead of remaining a standalone draft.

### 4.10 Upgrade the Frontend Agent Trace

Current file:

```text
frontend/src/components/AgentPanel/AgentPanel.tsx
```

The UI should show the full AgentxG Core loop:

1. Intent.
2. Observations.
3. Planner output.
4. Executor approvals.
5. Tool calls.
6. Tool results.
7. Verification checks.
8. Adaptation round, if any.
9. Final SLA status.

The frontend types should also model these fields:

```text
frontend/src/types/index.ts
```

## 5. Proposed Runtime Data Model

Store an event execution record like this:

```json
{
  "executionId": "uuid",
  "eventType": "medical",
  "status": "VERIFYING",
  "intent": {},
  "baseline": {},
  "planner": {
    "model": "bedrock-agent",
    "observations": [],
    "plan": []
  },
  "executor": {
    "approved": true,
    "actions": [
      {
        "tool": "activate_qos_policy",
        "status": "success",
        "startedAt": "timestamp",
        "completedAt": "timestamp",
        "result": {}
      }
    ]
  },
  "verification": {
    "status": "passed",
    "checks": []
  },
  "adaptation": {
    "round": 0,
    "maxRounds": 1
  }
}
```

## 6. New Project Architecture

After the changes, the project architecture should be:

```text
5GCityVerse
├── frontend
│   └── AgentxG Core trace UI
│
├── backend
│   ├── aws-app
│   │   ├── HTTP / WebSocket API
│   │   ├── fallback decision service
│   │   └── compatibility layer
│   │
│   ├── agent-runtime
│   │   ├── intent_manager.py
│   │   ├── planner.py
│   │   ├── executor.py
│   │   ├── tool_gateway.py
│   │   ├── sla_verifier.py
│   │   └── tools
│   │       ├── network_analytics.py
│   │       ├── subscribers.py
│   │       ├── nef_qos.py
│   │       ├── nef_traffic_influence.py
│   │       ├── nef_pfd.py
│   │       ├── hpa.py
│   │       └── ueransim.py
│   │
│   ├── bedrock-agent
│   │   ├── agent-instructions.md
│   │   └── action-group-schema.yaml
│   │
│   ├── bedrock-orchestrator
│   │   └── invokes Bedrock Agent and streams trace
│   │
│   ├── nef-tools
│   │   └── Lambda handlers for NEF / HPA actions
│   │
│   └── step-functions
│       └── agent closed-loop workflow
│
├── infrastructure
│   └── terraform
│       ├── backend API
│       ├── Bedrock Agent
│       ├── ToolGateway Lambda
│       ├── Step Functions
│       └── IAM / permissions
│
└── k8s
    ├── free5GC
    ├── UERANSIM
    ├── Prometheus
    └── HPA
```

## 7. Phased Implementation Plan

### Phase 1: Tool Contract Foundation

Status: partially implemented in the deployable `backend/aws-app/agent_runtime` package.

1. Add `backend/agent-runtime/tool_gateway.py`.
2. Wrap existing metrics, subscriber, HPA, UERANSIM, and NEF actions.
3. Add parameter validation and bounded action limits.
4. Add local unit tests for tool contracts.

Current implementation note:

```text
backend/aws-app/agent_runtime/tool_gateway.py
```

The deployable ToolGateway now invokes real NEF Lambda functions for:

1. `activate_qos_policy`
2. `request_traffic_influence`
3. `create_pfd_rule`

Terraform deploys these Lambda functions from:

```text
backend/nef-tools/fn-nef-qos-subscription
backend/nef-tools/fn-nef-traffic-influence
backend/nef-tools/fn-nef-pfd-create
```

The deployment must set `nef_base_url` to a free5GC NEF endpoint that is reachable by the Lambda runtime. If the default Kubernetes service DNS name is not reachable from Lambda, expose NEF through a reachable internal or public endpoint and set `var.nef_base_url`.

### Phase 2: Real Verification

1. Add `sla_verifier.py`.
2. Implement `verify_sla`.
3. Update agent decision payloads from `pending` to actual `passed`, `failed`, or `degraded`.
4. Broadcast verification results to the frontend.

### Phase 3: Agent Loop

1. Add intent manager.
2. Add planner and executor modules.
3. Invoke Bedrock Agent from the trigger path or Step Functions.
4. Keep `AgentDecisionService` as fallback.

### Phase 4: Bedrock / AgentCore Productionization

1. Deploy Bedrock Agent and Action Group through Terraform.
2. Add ToolGateway Lambda.
3. Add Step Functions closed loop.
4. Add observability and traces.
5. If using Bedrock AgentCore, map:
   - AgentCore Runtime -> Supervisor / Planner / Executor runtime.
   - AgentCore Gateway -> ToolGateway / MCP tool discovery.
   - AgentCore Identity -> AWS and network tool access boundaries.
   - AgentCore Observability -> execution traces and audit.
   - AgentCore Memory -> bounded network action history.

### Phase 5: UI and Demo Polish

1. Upgrade AgentPanel to show the full loop.
2. Show tool call status and verification result.
3. Show adaptation if SLA fails.
4. Keep the visual city simulation tied to real tool results.

## 8. Safety Boundaries

The agent must not directly perform unbounded network changes. Enforce these rules in ToolGateway and Executor:

1. HPA max replicas must stay inside configured bounds.
2. Subscriber changes must be limited to CityVerse IMSI ranges.
3. NEF operations must be scoped to known AF IDs and scenario app IDs.
4. UERANSIM launches must use known profiles.
5. Adaptation rounds must be limited.
6. Every sensitive tool call must be logged.
7. Failed verification should not trigger infinite retries.

## 9. Minimal Viable AgentxG Core for This Project

The smallest credible version is:

```text
Single Bedrock Agent
+ ToolGateway Lambda
+ get_network_analytics
+ upsert_subscriber_profile
+ activate_qos_policy
+ verify_sla
+ one bounded adaptation round
+ frontend trace display
```

This would already demonstrate the main AgentxG Core idea:

```text
Intent-driven, tool-based, closed-loop mobile core orchestration.
```

## 10. Final Recommendation

The project should not replace free5GC, Kubernetes, or HPA with an LLM. Instead, it should add an AgentxG Core-style intelligent layer above them.

The correct role of the agent is:

```text
Understand intent.
Choose strategy.
Invoke safe tools.
Verify outcome.
Adapt once if needed.
Explain the trace.
```

The correct role of the existing network stack is:

```text
Expose metrics.
Enforce policy.
Run UEs and traffic.
Scale pods.
Carry packets.
```

With this split, 5GCityVerse can become a practical reproduction of the paper's architecture while staying safe enough for a real free5GC and EKS environment.
