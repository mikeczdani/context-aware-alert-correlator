import { useQuery } from '@tanstack/react-query'
import { fetchMetrics } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import Panel from '../ui/Panel'
import styles from './SystemVitals.module.css'

export default function SystemVitals() {
  const { data } = useQuery({
    queryKey: QUERY_KEYS.metrics,
    queryFn: fetchMetrics,
  })

  const raw        = data?.raw_alerts_total ?? 0
  const suppressed = data?.suppressed_total ?? 0
  const incidents  = data?.correlated_incidents_total ?? 0
  const arr        = data?.alert_reduction_rate_pct ?? 0

  return (
    <Panel title="System Vitals" badge="ARR %" className={styles.vitalsPanel}>
      <div className={styles.grid}>
        <div className={styles.card}>
          <div>
            <div className={styles.label}>Raw Alerts</div>
            <div className={styles.sublabel}>Prometheus Ingest</div>
          </div>
          <div className={styles.number}>{raw}</div>
        </div>

        <div className={styles.card}>
          <div>
            <div className={styles.label}>Suppressed Noise</div>
            <div className={styles.sublabel}>Scale / Restart Filter</div>
          </div>
          <div className={`${styles.number} ${styles['number--warn']}`}>{suppressed}</div>
        </div>

        <div className={styles.card}>
          <div>
            <div className={styles.label}>Correlated Incidents</div>
            <div className={styles.sublabel}>RCA Root Causes</div>
          </div>
          <div className={`${styles.number} ${styles['number--err']}`}>{incidents}</div>
        </div>

        <div className={`${styles.card} ${styles['card--arr']}`}>
          <div>
            <div className={`${styles.label} ${styles['label--arr']}`}>Alert Reduction (ARR)</div>
            <div className={styles.sublabel}>Efficiency Rate</div>
          </div>
          <div className={styles.number}>{arr.toFixed(1)}%</div>
        </div>
      </div>
    </Panel>
  )
}
