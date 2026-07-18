---
name: 5gcityverse-agent-collaboration
description: Project collaboration rules for agents working on 5GCityVerse. Use before changing code, Terraform, Kubernetes manifests, AWS Bedrock agent behavior, free5GC/NEF integrations, deployment scripts, or project documentation in this repository.
---

# 5GCityVerse Agent Collaboration

## Operating Mode

Treat this repository as a real cloud-native B5G smart-city simulator, not a mock demo. Preserve the chain from frontend scenario event to backend decision, Bedrock/tool orchestration, free5GC/NEF effect, EKS/Kubernetes runtime signal, and dashboard update.

Start every task by checking the relevant project files and current git state. Work with existing uncommitted changes; never revert unrelated user edits.

## Specialist Agents

When future Codex work should be delegated, use the definitions under `agents/`:

- `ui-engineer.yaml`: frontend screens, traffic-flow visualization, scenario controls, and dashboard UX.
- `ai-engineer.yaml`: AI agent design, intent planning, Bedrock/action schemas, and bounded network orchestration.
- `sre.yaml`: Kubernetes, EKS, free5GC, UERANSIM, HPA, Prometheus, and runtime connectivity.
- `backend-engineer.yaml`: APIs, WebSocket contracts, Lambda backend, persistence, and integration transport.

## Required References

Load only the reference that matches the task:

- For local development, commands, scripts, frontend/backend work, and validation, read `references/developing.md`.
- For Terraform, AWS infrastructure, modules, providers, state, and deployment scripts, read `references/terraform.md`.
- For Bedrock agents, tool schemas, network-operation policy, NEF behavior, and multi-agent design, read `references/agent-design.md`.

## Default Workflow

1. Identify the layer being changed: frontend, backend, Bedrock agent, NEF tool, Kubernetes manifest, Terraform, or deployment script.
2. Read the nearest existing implementation before editing. Follow local naming, config, error handling, and logging patterns.
3. Keep changes narrow. Do not refactor unrelated code while implementing a requested behavior.
4. Validate with the smallest meaningful command first, then broaden validation when the blast radius crosses layers.
5. Report what changed, what was validated, and any remaining risk.

## Project Invariants

- Keep runtime behavior real where the repo already uses real integrations. Do not replace free5GC, NEF, EKS, AWS, WebSocket, or Prometheus paths with fake endpoints unless the task explicitly asks for a test seam or local fixture.
- Keep scenario semantics stable: eMBB `SST=1`, URLLC `SST=2`, mMTC `SST=3`, and V2X `SST=4`.
- Prefer subscriber/profile and NEF policy changes before direct scaling actions.
- Never exceed configured HPA or infrastructure safety bounds.
- Avoid committing secrets, AWS account IDs, kubeconfigs, Terraform state, generated provider binaries, local logs, and `node_modules` variants.

## File Placement

Use the existing ownership layout:

- `frontend/`: React, D3/canvas city map, dashboard, WebSocket/API clients, UI state.
- `backend/aws-app/`: primary cloud backend Lambda application.
- `backend/nef-tools/`: individual NEF action Lambda functions.
- `backend/bedrock-agent/`: Bedrock agent instructions and action group schema.
- `k8s/`: free5GC, UERANSIM, HPA, and traffic-generation manifests.
- `infrastructure/terraform/`: AWS infrastructure managed by Terraform.
- `scripts/`: WSL/Linux deployment and runtime scripts.
