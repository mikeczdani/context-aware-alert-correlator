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
    <Panel title="System Vitals (ARR)" badge="LIVE" className={styles.vitalsPanel}>
      <div className={styles.row}>
        <div>
          <div className={styles.label}>RAW ALERTS</div>
          <div className={styles.sublabel}>Prometheus Ingest</div>
        </div>
        <div className={`${styles.value} ${styles['value--main']}`}>{raw}</div>
      </div>

      <div className={styles.row}>
        <div>
          <div className={styles.label}>SUPPRESSED NOISE</div>
          <div className={styles.sublabel}>Scale / Restart Filter</div>
        </div>
        <div className={`${styles.value} ${styles['value--amber']}`}>{suppressed}</div>
      </div>

      <div className={styles.row}>
        <div>
          <div className={styles.label}>CORRELATED INCIDENTS</div>
          <div className={styles.sublabel}>RCA Root Causes</div>
        </div>
        <div className={`${styles.value} ${styles['value--red']}`}>{incidents}</div>
      </div>

      <div className={styles.row}>
        <div>
          <div className={styles.label}>ALERT RED. RATE</div>
          <div className={styles.sublabel}>Efficiency Metric</div>
        </div>
        <div className={styles.value}>{arr.toFixed(1)}%</div>
      </div>
    </Panel>
  )
}
