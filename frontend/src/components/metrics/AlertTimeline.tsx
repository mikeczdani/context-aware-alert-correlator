import { useQuery } from '@tanstack/react-query'
import { fetchIncidents } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import styles from './AlertTimeline.module.css'

// 50 bar-t mutatunk – az utolsó N incidens alapján az alert számokból,
// a többi random baseline (green)
const TOTAL_BARS = 50

export default function AlertTimeline() {
  const { data: incidents } = useQuery({
    queryKey: QUERY_KEYS.incidents,
    queryFn: fetchIncidents,
  })

  // Utolsó TOTAL_BARS incidens raw_alert_count értékei → bar magasság
  const bars = Array.from({ length: TOTAL_BARS }, (_, i) => {
    const incident = incidents?.[incidents.length - TOTAL_BARS + i]
    if (incident) {
      return {
        height: Math.min((incident.raw_alert_count / 10) * 100, 100),
        alert: incident.status === 'OPEN',
      }
    }
    // Baseline: véletlen magasságú zöld bar
    return { height: Math.random() * 60 + 15, alert: false }
  })

  return (
    <div className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionHeaderLabel}>
          SIGNAL RESPONSE MAP // HISTORICAL ALERT REDUCTION TIMELINE
        </span>
        <span className={styles.sectionHeaderBadge}>
          BAND: 2.4 GHZ | WINDOW: 60S
        </span>
      </div>
      <div className={styles.chart}>
        {bars.map((bar, i) => (
          <div
            key={i}
            className={`${styles.bar} ${bar.alert ? styles['bar--alert'] : ''}`}
            style={{ height: `${bar.height}%` }}
          />
        ))}
      </div>
    </div>
  )
}
