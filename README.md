# 5GCityVerse

5GCityVerse is a 5G network experimentation platform that combines smart city scenarios, an AI orchestrator, and free5GC. City events create simulated UEs and traffic so operators can observe network behavior under different service requirements and experiment with AI driven resource changes.

## Main features

- **City scenarios and visualization:** Present eMBB, URLLC, mMTC, and V2X demand through city events, with UE, traffic, and execution status.
- **Three AI roles:** The Planner analyzes observations and proposes strategies; the Validator checks proposals, effects, and restoration; the Executor applies or restores validated strategies.
- **free5GC integration:** free-ran-ue simulates access and UEs. AMF, SMF, and UPF establish the data path, while the project's SMF QER API adjusts QoS for existing sessions.
- **Observability and records:** Combine Kubernetes, native QER, and receiver traffic evidence with checkpoints and verified lessons.

## Architecture

![5GCityVerse layered architecture](src/img/architecture-16x9.png)

## On-prem deployment

### Prerequisites

- Ubuntu 24.04
- Linux Python 3.10+
- Node.js 22.12+
- Docker

### AI API key

1. Copy `.env.example` to `.env`.
2. Set `OPENAI_API_KEY`.

### Start the system and network environment

Follow these guides in order:

1. [On-prem deployment guide](infrastructure/onprem/README.md): Prepare nodes, networking, and the 5G core.
2. [On-prem startup guide](scripts/onprem/README.md): Configure and start the local backend and frontend.

```bash
# Install frontend dependencies before the first start.
cd frontend && npm ci
cd ..

# Check dependencies, configuration, and ports.
bash scripts/onprem/start-onprem.sh --preflight-only

# Build the frontend, start both services, wait for readiness, and save logs and process identities.
bash scripts/onprem/start-onprem.sh

# Stop the frontend and backend managed by the launcher.
bash scripts/onprem/stop-onprem.sh
```

By default, the backend is available at `http://127.0.0.1:8000`, the frontend at `http://127.0.0.1:5173`, and logs at `.tmp/onprem/logs/`.

**The regular startup mode does not deploy infrastructure and disables local Kubernetes integration by default.** To use native 5G features, prepare the infrastructure first, then start the native backend with a cluster and profile through `start_native_backend.py` as described in the startup guide. Once the native backend is ready, start only the frontend and connect it to that backend:

```bash
bash scripts/onprem/start-onprem.sh --use-existing-backend --backend-port 8000
```
