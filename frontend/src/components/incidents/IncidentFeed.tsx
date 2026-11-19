/**
 * components/incidents/IncidentFeed.tsx – Korrelált incidensek terminál-stílusú naplója
 *
 * A "Terminal // SSE Stream & Audit" panel tartalma. Minden incidens egy
 * naplóbejegyzés, amely lenyitható:
 *   - Fejléc: időbélyeg, státusz-címke, root cause, összefoglaló, számlálók
 *   - Lenyitva: érintett csomópontok státusszal, metrikákkal és indoklással
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchIncidents } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import type { AffectedNode, CorrelatedIncident, NodeStatus } from '../../types/api'
import { parseUtc } from '../../utils/time'
import styles from './IncidentFeed.module.css'

// ---------------------------------------------------------------------------
// Címkék
// ---------------------------------------------------------------------------

function IncidentStatusTag({ status }: { status: string }) {
  return (
    <span className={`${styles.tag} ${styles[`tag--${status}`]}`}>
      {status}
    </span>
  )
}

function NodeStatusTag({ status }: { status: NodeStatus }) {
  return (
    <span className={`${styles.tag} ${styles[`tag--${status}`]}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

/**
 * HH:MM:SS formátum 24 órás idővel, a felhasználó helyi időzónájában.
 * A created_at időzóna-jelző nélküli UTC, ezért a parseUtc-t használjuk –
 * a sima new Date(iso) helyi időként értelmezné, és eltolódna.
 */
function formatTime(iso: string): string {
  return new Date(parseUtc(iso)).toLocaleTimeString('hu-HU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

// ---------------------------------------------------------------------------
// Egyetlen incidens bejegyzés
// ---------------------------------------------------------------------------

function IncidentEntry({ incident }: { incident: CorrelatedIncident }) {
  const [expanded, setExpanded] = useState(false)
  const detailsId = `incident-details-${incident.id}`

  return (
    <div className={`${styles.entry} ${styles[`entry--${incident.status}`]}`}>
      <button
        type="button"
        className={styles.entryHeader}
        onClick={() => setExpanded(prev => !prev)}
        aria-expanded={expanded}
        aria-controls={detailsId}
      >
        <span className={styles.line}>
          <span className={styles.time}>[{formatTime(incident.created_at)}]</span>
          <IncidentStatusTag status={incident.status} />
          {incident.root_cause_service && (
            <span className={styles.rootCause}>
              ROOT CAUSE: {incident.root_cause_service}
            </span>
          )}
          <span className={styles.toggle} aria-hidden="true">
            {expanded ? '[−]' : '[+]'}
          </span>
        </span>

        <span className={styles.summary}>
          <span className={styles.prompt}>&gt;</span> {incident.summary}
        </span>

        <span className={styles.counters}>
          <span>RAW <b>{incident.raw_alert_count}</b></span>
          <span>SUPPRESSED <b>{incident.suppressed_count}</b></span>
          <span>CORRELATED <b>{incident.correlated_count}</b></span>
        </span>
      </button>

      {expanded && (
        <div id={detailsId} className={styles.details}>
          {incident.affected_nodes.map((node: AffectedNode) => (
            <div
              key={node.service}
              className={`${styles.node} ${styles[`node--${node.status}`]}`}
            >
              <div className={styles.nodeHeader}>
                <span className={styles.nodeName}>{node.service}</span>
                <NodeStatusTag status={node.status} />
                <span className={styles.nodeScore}>
                  SCORE {node.severity_score.toFixed(3)}
                </span>
              </div>

              <div className={styles.nodeMetrics}>
                <span>ERR {(node.http_error_rate * 100).toFixed(1)}%</span>
                <span>LAT {node.response_time_ms.toFixed(0)}ms</span>
              </div>

              {node.suppression_reason && (
                <p className={styles.reason}>// {node.suppression_reason}</p>
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
    return (
      <div className={styles.terminal}>
        <p className={styles.system}>
          &gt; LOADING INCIDENTS<span className={styles.cursor}>_</span>
        </p>
      </div>
    )
  }

  if (!incidents || incidents.length === 0) {
    return (
      <div className={styles.terminal}>
        <p className={styles.system}>
          &gt; NO INCIDENTS YET. RUN A SCENARIO<span className={styles.cursor}>_</span>
        </p>
      </div>
    )
  }

  return (
    <div className={styles.terminal}>
      {[...incidents].reverse().map(incident => (
        <IncidentEntry key={incident.id} incident={incident} />
      ))}
    </div>
  )
}
