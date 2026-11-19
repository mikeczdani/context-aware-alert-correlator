/**
 * types/api.ts – TypeScript típusdefiníciók a backend API válaszaihoz
 *
 * Ezek a típusok pontosan tükrözik a backend schemas/ Pydantic modelljeit,
 * így a frontend type-safe módon kezelheti az API válaszokat.
 */

// ---------------------------------------------------------------------------
// Enumerációk – megegyeznek a backend NodeStatus és IncidentStatus enum-okkal
// ---------------------------------------------------------------------------

export type NodeStatus =
  | 'HEALTHY'
  | 'ROOT_CAUSE'
  | 'SYMPTOM'
  | 'SUPPRESSED'
  | 'SUPPRESSED_SCALING'
  | 'SUPPRESSED_RESTART'
  | 'SUPPRESSED_DOWNSTREAM'

export type IncidentStatus = 'OPEN' | 'SUPPRESSED' | 'RESOLVED'

// ---------------------------------------------------------------------------
// Incidens típusok – CorrelatedIncident és AffectedNode
// ---------------------------------------------------------------------------

export interface AffectedNode {
  service: string
  status: NodeStatus
  http_error_rate: number
  response_time_ms: number
  severity_score: number
  suppression_reason: string | null
}

export interface CorrelatedIncident {
  id: string
  created_at: string
  status: IncidentStatus
  root_cause_service: string | null
  affected_nodes: AffectedNode[]
  raw_alert_count: number
  suppressed_count: number
  correlated_count: number
  summary: string
}

// ---------------------------------------------------------------------------
// Metrika típusok – SystemMetrics
// ---------------------------------------------------------------------------

export interface SystemMetrics {
  raw_alerts_total: number
  suppressed_total: number
  correlated_incidents_total: number
  alert_reduction_rate_pct: number
}

// ---------------------------------------------------------------------------
// Topológia típusok – KialiGraph
// ---------------------------------------------------------------------------

export interface KialiNode {
  id: string
  service: string
  namespace: string
  response_time_ms: number
  http_error_rate: number
  traffic_rate_rps: number
}

export interface KialiEdge {
  id: string
  source: string
  target: string
  traffic_rate_rps: number
}

export interface KialiGraph {
  namespace: string
  timestamp: string
  nodes: KialiNode[]
  edges: KialiEdge[]
}
