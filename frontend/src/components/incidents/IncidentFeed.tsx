/**
 * components/incidents/IncidentFeed.tsx – Korrelált incidens kártyák listája
 *
 * Minden incidens egy lenyitható kártyaként jelenik meg:
 *   - Fejléc: státusz badge, root cause service, időbélyeg, összefoglaló
 *   - Lenyitva: érintett csomópontok státusszal, metrikákkal és indoklással
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchIncidents } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import type { AffectedNode, CorrelatedIncident, NodeStatus } from '../../types/api'
import styles from './IncidentFeed.module.css'

// ---------------------------------------------------------------------------
// Badge komponensek
// ---------------------------------------------------------------------------

function IncidentStatusBadge({ status }: { status: string }) {
  return (
    <span className={`${styles.badge} ${styles[`badge--${status}`]}`}>
      {status}
    </span>
  )
}

function NodeStatusBadge({ status }: { status: NodeStatus }) {
  return (
    <span className={`${styles.badge} ${styles[`badge--${status}`]}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleTimeString('hu-HU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

// ---------------------------------------------------------------------------
// Egyetlen incidens kártya
// ---------------------------------------------------------------------------

function IncidentCard({ incident }: { incident: CorrelatedIncident }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className={styles.card}>
      <button
        className={styles.cardHeader}
        onClick={() => setExpanded(prev => !prev)}
      >
        <div className={styles.headerTop}>
          <IncidentStatusBadge status={incident.status} />
          {incident.root_cause_service && (
            <span className={styles.rootCause}>
              Root cause: {incident.root_cause_service}
            </span>
          )}
          <span className={styles.timestamp}>
            {formatDate(incident.created_at)}
          </span>
        </div>

        <p className={styles.summary}>{incident.summary}</p>

        <div className={styles.counters}>
          <span>🔔 {incident.raw_alert_count} raw</span>
          <span>🔇 {incident.suppressed_count} suppressed</span>
          <span>🔗 {incident.correlated_count} correlated</span>
          <span className={styles.expandHint}>
            {expanded ? '▲ collapse' : '▼ details'}
          </span>
        </div>
      </button>

      {expanded && (
        <div className={styles.details}>
          {incident.affected_nodes.map((node: AffectedNode) => (
            <div
              key={node.service}
              className={`${styles.node} ${styles[`node--${node.status}`]}`}
            >
              <div className={styles.nodeHeader}>
                <span className={styles.nodeName}>{node.service}</span>
                <NodeStatusBadge status={node.status} />
                <span className={styles.nodeScore}>
                  score: {node.severity_score.toFixed(3)}
                </span>
              </div>

              <div className={styles.nodeMetrics}>
                <span>err: {(node.http_error_rate * 100).toFixed(1)}%</span>
                <span>latency: {node.response_time_ms.toFixed(0)}ms</span>
              </div>

              {node.suppression_reason && (
                <p className={styles.suppressionReason}>
                  💬 {node.suppression_reason}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Fő komponens
// ---------------------------------------------------------------------------

export default function IncidentFeed() {
  const { data: incidents, isLoading } = useQuery({
    queryKey: QUERY_KEYS.incidents,
    queryFn: fetchIncidents,
  })

  if (isLoading) {
    return <div className={styles.loading}>Loading incidents...</div>
  }

  if (!incidents || incidents.length === 0) {
    return (
      <div className={styles.empty}>
        No incidents yet. Run a simulation to get started.
      </div>
    )
  }

  return (
    <div className={styles.feed}>
      {[...incidents].reverse().map(incident => (
        <IncidentCard key={incident.id} incident={incident} />
      ))}
    </div>
  )
}
