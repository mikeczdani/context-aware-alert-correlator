# Context-Aware Alert Correlator

> Thesis project — Context-aware alert noise filtering and root cause analysis for microservice environments using Kiali topology graphs, FastAPI, Redis, and React.

## Overview

Modern microservice environments generate a high volume of alerts, many of which are operational noise — transient errors caused by pod restarts, HPA scale-out events, or rolling updates rather than actual failures. This system addresses the **alert fatigue** problem by:

1. **Filtering operational noise** — suppressing alerts that are caused by known Kubernetes lifecycle events (pod restarts, scaling)
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
| Graph Engine | NetworkX | Build DiGraph from Kiali topology, run RCA traversal |
| Storage | Redis (AOF) | TTL correlation window, NoSQL incident store, Pub/Sub |
| Alert Input | HTTP Webhook | Event-driven — Prometheus Alertmanager pushes alerts |
| Real-time Output | SSE (Server-Sent Events) | Push notifications to React without polling |
| Frontend | React + TanStack Query + React Flow | Interactive topology graph and incident dashboard |

## Project Structure

```
thesis-alert-correlator/
├── docker-compose.yml          # Redis AOF container
├── backend/
│   ├── requirements.txt
│   ├── scenarios/              # Synthetic Kiali + K8s test JSON scenarios
│   ├── simulator/
│   │   └── chaos_runner.py     # Scenario replay script
│   └── app/
│       ├── main.py             # FastAPI app + router registration
│       ├── core/
│       │   └── config.py       # Settings (REDIS_URL, DATA_MODE, KIALI_URL)
│       ├── schemas/
│       │   ├── alert.py        # RawAlert, WebhookPayload
│       │   ├── topology.py     # KialiNode, KialiEdge, KialiGraph, K8sContext
│       │   └── incident.py     # CorrelatedIncident, NodeStatus, SystemMetrics
│       ├── api/
│       │   ├── webhook_routes.py    # POST /api/v1/webhook/alerts
│       │   ├── stream_routes.py     # GET  /api/v1/stream (SSE)
│       │   └── dashboard_routes.py  # GET  /incidents /topology /metrics, POST /simulate
│       ├── collectors/
│       │   ├── kiali_collector.py   # Kiali REST API client + Mock JSON loader
│       │   └── k8s_collector.py     # Kubernetes pod lifecycle state
│       ├── engine/
│       │   ├── noise_filter.py      # Pod restart / scale-up suppression rules
│       │   ├── graph_correlator.py  # NetworkX DiGraph builder + RCA
│       │   └── severity_scorer.py   # Graph centrality-based severity scoring
│       └── storage/
│           ├── redis_client.py      # Async Redis connection + Pub/Sub
│           ├── window_store.py      # 60s TTL sliding correlation window
│           └── incident_store.py    # Persistent incident store + ARR counters
└── frontend/
    └── src/
        ├── api/incidentsApi.ts
        ├── hooks/useSSEStream.ts
        ├── components/
        │   ├── topology/ServiceGraph.tsx
        │   ├── incidents/IncidentFeed.tsx
        │   ├── metrics/ArrStatsCards.tsx
        │   └── simulator/ChaosControls.tsx
        └── pages/DashboardPage.tsx
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/webhook/alerts` | Receive raw Prometheus Alertmanager alerts |
| `GET` | `/api/v1/stream` | SSE stream — notifies React when a new incident is created |
| `GET` | `/api/v1/incidents` | List all correlated incidents and suppressed noise |
| `GET` | `/api/v1/topology/current` | Current Kiali graph with RCA node labels |
| `GET` | `/api/v1/metrics/summary` | ARR %, raw alert count, suppressed count |
| `POST` | `/api/v1/simulate/{scenario}` | Trigger a test scenario |
| `DELETE` | `/api/v1/incidents/reset` | Flush Redis and reset all counters |

## Test Scenarios

| ID | Description | Expected Outcome |
|---|---|---|
| `healthy_baseline` | All services healthy | No alerts |
| `pod_restart_noise` | `inventory` pods restarting (3x/min) | All alerts suppressed — no incident |
| `cascading_db_timeout` | `db` high latency → cascading failure | `db` = ROOT_CAUSE, 4 SYMPTOMs |
| `mixed_storm` | `auth` HPA scaling + real `db` failure | 1 suppressed + 1 correlated incident |

## Getting Started

### Prerequisites

- Docker (for Redis)
- Python 3.11+
- Node.js 18+ (for frontend)

### 1. Start Redis

```bash
docker-compose up -d
```

### 2. Set up the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```env
DATA_MODE=MOCK
REDIS_URL=redis://localhost:6379
KIALI_URL=http://localhost:20001
KIALI_NAMESPACE=default
WINDOW_TTL=60
SCALE_NOISE_WINDOW_SECONDS=90
```

Run the backend:

```bash
uvicorn app.main:app --reload
```

API docs available at `http://localhost:8000/docs`

### 3. Set up the frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard available at `http://localhost:3000`

## Key Metric: Alert Reduction Rate (ARR %)

$$\text{ARR} = \left(1 - \frac{N_{\text{correlated incidents}}}{N_{\text{raw alerts}}}\right) \times 100\%$$

Example from the `mixed_storm` scenario: 6 raw alerts → 1 suppressed + 1 incident = **ARR 83.3%**

## Operating Modes

**`MOCK` mode** (default) — loads pre-built JSON scenarios from `backend/scenarios/`. No Kubernetes cluster needed. Deterministic results for thesis measurements.

**`LIVE` mode** — connects to a real Kiali REST API and Kubernetes cluster. Requires a local `k3d` / Minikube cluster with Istio + Kiali + Prometheus running.

Switch modes by setting `DATA_MODE=LIVE` in `.env`.
