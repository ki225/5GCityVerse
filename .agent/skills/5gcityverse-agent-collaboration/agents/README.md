# 5GCityVerse Agent Roster

Use these specialist agents when assigning future Codex work in this repository. Every specialist must first apply the parent `5gcityverse-agent-collaboration` skill and load the matching reference file before changing code or infrastructure.

## Routing

| Agent | Assign when the task is about | Primary paths |
| --- | --- | --- |
| UI Engineer | Frontend screens, flow visualization, scenario controls, dashboard interaction, visual polish | `frontend/`, `FLOW_ANIMATION_GUIDE.md`, `DESIGN.md` |
| AI Engineer | Agent planning, intent mapping, Bedrock instructions, NEF action schemas, closed-loop orchestration | `backend/aws-app/agent_runtime/`, `backend/bedrock-agent/`, `backend/nef-tools/`, `AGENTXG_CORE_IMPLEMENTATION.md` |
| SRE | Kubernetes, EKS, free5GC, UERANSIM, HPA, Prometheus, deployment scripts | `k8s/`, `infrastructure/terraform/`, `scripts/`, `real-simulation.md`, `ue-action.md` |
| Backend Engineer | APIs, WebSocket contracts, Lambda backend, data models, frontend/AI/free5GC integration | `backend/aws-app/`, `backend/ws-handler/`, `backend/state-bridge/`, `frontend/src/services/` |

## Shared Handoff Rules

- Keep the real event chain intact: frontend scenario -> backend API/WebSocket -> AI agent plan -> free5GC/NEF/UERANSIM/K8s action -> metrics/dashboard verification.
- Preserve slice semantics: eMBB `SST=1`, URLLC `SST=2`, mMTC `SST=3`, V2X `SST=4`.
- Prefer bounded policy, subscriber, NEF, and UERANSIM profile actions before direct scaling.
- Validate with the smallest meaningful local command first. Do not run live AWS, Helm, kubectl apply, or Terraform apply unless the user explicitly asks for it.
- When a task crosses agent boundaries, state the handoff clearly and keep API/data contracts stable.
