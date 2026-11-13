/**
 * components/metrics/ArrStatsCards.tsx – ARR % és riasztásszám kártyák
 *
 * A dashboard tetején megjelenő 4 KPI kártya:
 *   1. Beérkezett nyers riasztások száma
 *   2. Kiszűrt zaj (pod restart / scale)
 *   3. Valódi korrelált incidensek
 *   4. ARR % – a szakdolgozat fő mérőszáma
 */

import { useQuery } from '@tanstack/react-query'
import { fetchMetrics } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import styles from './ArrStatsCards.module.css'

type Accent = 'blue' | 'yellow' | 'red' | 'green'

interface StatCardProps {
  label: string
  value: string | number
  accent: Accent
}

function StatCard({ label, value, accent }: StatCardProps) {
  return (
    <div className={`${styles.card} ${styles[`card--${accent}`]}`}>
      <div className={styles.label}>{label}</div>
      <div className={styles.value}>{value}</div>
    </div>
  )
}

export default function ArrStatsCards() {
  const { data, isLoading } = useQuery({
    queryKey: QUERY_KEYS.metrics,
    queryFn: fetchMetrics,
  })

  if (isLoading || !data) {
    return (
      <div className={styles.grid}>
        {[...Array(4)].map((_, i) => (
          <div key={i} className={styles.skeleton} />
        ))}
      </div>
    )
  }

  return (
    <div className={styles.grid}>
      <StatCard label="Raw Alerts"            value={data.raw_alerts_total}                        accent="blue"   />
      <StatCard label="Suppressed Noise"      value={data.suppressed_total}                        accent="yellow" />
      <StatCard label="Correlated Incidents"  value={data.correlated_incidents_total}              accent="red"    />
      <StatCard label="Alert Reduction Rate"  value={`${data.alert_reduction_rate_pct.toFixed(1)}%`} accent="green"  />
    </div>
  )
}
