# Context-Aware Alert Correlator

> Thesis project — Context-aware alert noise filtering and root cause analysis for microservice environments using Kiali topology graphs, FastAPI, Redis, and React.

## Overview

Modern microservice environments generate a high volume of alerts, many of which are operational noise — transient errors caused by pod restarts, HPA scale-out events, or rolling updates rather than actual failures. This system addresses the **alert fatigue** problem by:

1. **Filtering operational noise** — suppressing alerts caused by known Kubernetes lifecycle events (pod restarts, scaling)
2. **Correlating cascading failures** — grouping related alerts into a single incident using the Kiali service topology graph
3. **Identifying the root cause** — pinpointing the deepest downstream failing node in the call chain using NetworkX graph traversal
4. **Measuring the result** — calculating the Alert Reduction Rate (ARR %) as the primary validation metric

## Architecture

The system is built on a 5-layer event-driven architecture:

```
[1. Infrastructure / Simulator]
       │  HTTP POST Webhook + REST Pull (Kiali Graph & K8s state)
       ▼
[2. Data Collection Layer]  ←  app/api/ + app/collectors/
       │  Normalized Pydantic models
       ▼
[3. Correlation & Analysis Engine]  ←  app/engine/
       │  Read / Write / TTL window + Pub/Sub
       ▼
[4. Redis Storage Layer]  ←  app/storage/
       │  SSE notification + REST queries
       ▼
[5. React Frontend Dashboard]  ←  frontend/src/
```

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Python / FastAPI | Async webhook receiver, SSE stream, REST API |
| Graph Engine | NetworkX | Build DiGraph from Kiali topology, run RCA traversal, PageRank severity |
| Storage | Redis (AOF) | TTL correlation window, NoSQL incident store, Pub/Sub |
| Alert Input | HTTP Webhook | Event-driven — Prometheus Alertmanager (or the chaos runner) pushes alerts |
| Real-time Output | SSE (Server-Sent Events) | Push notifications to React without polling |
| Frontend | React 19 + Vite + TanStack Query + React Flow | Incident dashboard, interactive topology graph, live metrics and timeline |

## Project Structure

Actual files present in the repository:

```
context-aware-alert-correlator/
├── docker-compose.yml          # Redis AOF container + backend service (thesis-network bridge)
├── kontextusalap_hibafelismer_rendszer_architekt_ra.md   # Thesis architecture design doc (HU)
├── backend/
│   ├── Dockerfile              # python:3.11-slim, uvicorn on :8000
│   ├── requirements.txt        # fastapi, uvicorn, pydantic, redis, networkx, numpy, scipy, httpx
│   ├── scenarios/              # Synthetic Kiali + K8s + alert JSON scenarios
│   │   ├── 00_healthy_baseline.json
│   │   ├── 01_pod_restart_noise.json
│   │   ├── 02_cascading_latency.json
│   │   └── 03_mixed_failure.json
│   ├── simulator/
│   │   └── chaos_runner.py     # CLI scenario replay — POSTs webhooks to the running backend
│   └── app/
│       ├── main.py             # FastAPI app + router registration + GET /health
│       ├── core/
│       │   └── config.py       # Settings (REDIS_URL, DATA_MODE, KIALI_URL, ...)
│       ├── schemas/
│       │   ├── alert.py        # RawAlert, WebhookPayload
│       │   ├── topology.py     # KialiNode, KialiEdge, KialiGraph, K8sContext
│       │   └── incident.py     # CorrelatedIncident, NodeStatus, SystemMetrics
│       ├── api/
│       │   ├── webhook_routes.py    # POST   /api/v1/webhook/alerts
│       │   ├── stream_routes.py     # GET    /api/v1/stream (SSE)
│       │   └── dashboard_routes.py  # GET /incidents /topology /metrics, POST /simulate, DELETE /reset
│       ├── collectors/
│       │   ├── kiali_collector.py   # Kiali REST client + Mock JSON loader (SCENARIO_FILE_MAP)
│       │   └── k8s_collector.py     # Kubernetes pod lifecycle / scaling state
│       ├── engine/
│       │   ├── noise_filter.py      # Pod restart / scale-up suppression rules
│       │   ├── graph_correlator.py  # NetworkX DiGraph builder + RCA traversal
│       │   └── severity_scorer.py   # PageRank + error-rate + traffic-share severity score
│       └── storage/
│           ├── redis_client.py      # Async Redis connection + Pub/Sub (channel:sse_updates)
│           ├── window_store.py      # 60s TTL sliding correlation window
│           └── incident_store.py    # Persistent incident store + ARR counters (HINCRBY)
└── frontend/
    ├── vite.config.ts                       # Vite config + dev proxy (/api → backend :8000)
    └── src/
        ├── main.tsx
        ├── App.tsx                          # QueryClientProvider + SSE listener
        ├── index.css                        # Theme variables + global layout
        ├── api/incidentsApi.ts              # REST fetch functions
        ├── hooks/useSSEStream.ts            # EventSource (SSE) subscription + query invalidation
        ├── types/api.ts                     # TypeScript mirrors of backend schemas
        ├── pages/DashboardPage.tsx          # Main dashboard layout
        └── components/
            ├── ui/AppHeader.tsx             # Logo + scenario trigger buttons + Reset
            ├── ui/Panel.tsx                 # Reusable panel wrapper
            ├── topology/ServiceGraph.tsx    # React Flow topology graph (status-colored nodes)
            ├── incidents/IncidentFeed.tsx   # Correlated incident cards (used as the terminal/feed)
            ├── metrics/SystemVitals.tsx     # Raw / Suppressed / Correlated / ARR vital cards
            ├── metrics/AlertTimeline.tsx    # Time-bucketed 60s sliding-window alert chart
            └── metrics/ArrStatsCards.tsx    # Alternative 4-card KPI strip (not currently mounted)
```

## API Endpoints

All routes are served under the `/api/v1` prefix (the prefix is declared on each `APIRouter`).

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/webhook/alerts` | Receive raw alerts. Optional `?scenario_id=<id>` query param selects the mock Kiali graph. |
| `GET` | `/api/v1/stream` | SSE stream — emits an `update` event when a new incident is created |
| `GET` | `/api/v1/incidents` | List all correlated incidents and suppressed noise |
| `GET` | `/api/v1/topology/current` | Current Kiali graph snapshot with RCA node labels |
| `GET` | `/api/v1/metrics/summary` | ARR %, raw alert count, suppressed count, correlated count |
| `POST` | `/api/v1/simulate/{scenario_id}` | Trigger a test scenario end-to-end (internal shortcut for the webhook path) |
| `DELETE` | `/api/v1/incidents/reset` | Flush Redis (FLUSHDB) and reset all counters |
| `GET` | `/health` | Health check — reports Redis connectivity and app version |

## Test Scenarios

Scenario IDs are validated against `SCENARIO_FILE_MAP` in `backend/app/collectors/kiali_collector.py`. Note that the scenario **ID** and the backing **filename** are not always identical.

| Scenario ID | File | Expected Outcome |
|---|---|---|
| `healthy_baseline` | `00_healthy_baseline.json` | All services healthy — no alerts |
| `pod_restart_noise` | `01_pod_restart_noise.json` | Restart noise suppressed — no real incident |
| `cascading_db_timeout` | `02_cascading_latency.json` | `db` = ROOT_CAUSE, upstream services = SYMPTOM |
| `mixed_storm` | `03_mixed_failure.json` | Scaling noise suppressed + 1 correlated incident |

## Getting Started

### Prerequisites

- Docker + Docker Compose (for Redis, and optionally the backend)
- Python 3.11+ (for running the backend outside Docker)
- Node.js 20+ (required by Vite 8; the frontend uses React 19)

### 1. Start Redis (and optionally the backend)

The `docker-compose.yml` defines **two** services: a Redis AOF container and the backend, connected on a `thesis-network` bridge.

```bash
# Redis only (recommended while developing the backend locally)
docker compose up -d redis

# Or bring up both Redis and the backend
docker compose up -d
```

### 2. Set up the backend

```bash
cd backend
python -m venv venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# macOS / Linux:
# source venv/bin/activate

pip install -r requirements.txt
```

> `requirements.txt` pins FastAPI, Uvicorn, Pydantic (+ pydantic-settings), the async Redis client, NetworkX, and **NumPy + SciPy** (required by NetworkX's PageRank backend used in `severity_scorer.py`), plus HTTPX for the Kiali collector and the chaos runner.

Create a `.env` file in `backend/` (all values shown are the defaults):

```env
DATA_MODE=MOCK
REDIS_URL=redis://localhost:6379
KIALI_URL=http://localhost:20001
KIALI_NAMESPACE=default
SCENARIOS_DIR=scenarios
WINDOW_TTL=60
SCALE_NOISE_WINDOW_SECONDS=90
```

Run the backend:

```bash
uvicorn app.main:app --reload
```

API docs (Swagger UI) available at `http://localhost:8000/docs`.

### 3. Set up the frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard available at `http://localhost:5173` (Vite default).

> **Dev proxy.** The frontend calls relative `/api/v1/...` URLs and opens an SSE stream. `vite.config.ts` proxies everything under `/api` to the backend (default `http://localhost:8000`), so there is no CORS setup needed in dev. Override the target with the `VITE_API_TARGET` env var if the backend runs elsewhere (e.g. `VITE_API_TARGET=http://localhost:8000 npm run dev`).

### 4. Replay scenarios with the chaos runner (optional)

With the backend and Redis running, the `chaos_runner.py` CLI replays scenarios by POSTing Alertmanager-style webhooks to the live backend — exactly as a real Alertmanager would.

```bash
cd backend

# List available scenarios
python -m simulator.chaos_runner --list

# Replay one scenario
python -m simulator.chaos_runner --scenario cascading_db_timeout

# Reset Redis, then replay all scenarios in order
python -m simulator.chaos_runner --all --reset --delay 2
```

It prints the resulting ARR metrics from `GET /api/v1/metrics/summary` at the end.

## Key Metric: Alert Reduction Rate (ARR %)

$$\text{ARR} = \left(1 - \frac{N_{\text{correlated incidents}}}{N_{\text{raw alerts}}}\right) \times 100\%$$

Computed in `incident_store.get_metrics()` from the atomic Redis counters in `stats:metrics` (`raw_alerts_total`, `suppressed_total`, `correlated_incidents_total`), guarded against divide-by-zero.

## Operating Modes

**`MOCK` mode** (default) — loads pre-built JSON scenarios from `backend/scenarios/`. No Kubernetes cluster needed. Deterministic results for thesis measurements.

**`LIVE` mode** — connects to a real Kiali REST API and Kubernetes cluster. Requires a local `k3d` / Minikube cluster with Istio + Kiali + Prometheus running.

Switch modes by setting `DATA_MODE=LIVE` in `.env`.

## Current Status

Implemented and working:

- Full backend: schemas, collectors (mock mode), noise filter, NetworkX correlation + RCA, PageRank severity scoring, Redis storage (TTL window, incident store, ARR counters), all API endpoints, SSE stream.
- Pinned `backend/requirements.txt` (incl. NumPy + SciPy for PageRank) and a working Docker build.
- `chaos_runner.py` CLI scenario replay.
- Frontend: SSE-driven live updates, the **React Flow `ServiceGraph`** (status-colored topology in the "Network Response Matrix" panel), `IncidentFeed`, `SystemVitals`, and a time-bucketed `AlertTimeline`.
- Vite dev proxy (`/api` → backend) configured in `vite.config.ts`.

Not yet implemented / known gaps:

- `config.WINDOW_TTL` exists but is not read by `window_store.py`, which hardcodes a 60s TTL constant.
- `resolved` Alertmanager events are accepted but not processed (only `firing` alerts are handled).
- `ArrStatsCards` exists but is not mounted; the dashboard uses `SystemVitals` for the ARR cards instead.
- `LIVE` mode (real Kiali + Kubernetes) is implemented in the collectors but has not been validated against a real cluster.
