import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchIncidents } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import type { CorrelatedIncident } from '../../types/api'
import { parseUtc } from '../../utils/time'
import styles from './AlertTimeline.module.css'

// A csúszóablak hossza másodpercben – ennyi időt mutat a chart visszafelé.
// A header "SLIDING WINDOW: 60S" feliratával egyezik.
const WINDOW_SECONDS = 60

// Hány bucketre osztjuk az ablakot. 60 bucket @ 60s = 1 másodperc / bar.
const BUCKET_COUNT = 60
const BUCKET_MS = (WINDOW_SECONDS * 1000) / BUCKET_COUNT

interface Bucket {
  alertCount: number // az ebbe a bucketbe eső incidensek raw_alert_count összege
  hasOpen: boolean // van-e OPEN státuszú incidens ebben a bucketben
}

/**
 * Az incidenseket beosztja az [now - WINDOW, now] ablak bucketjeibe.
 * A bucket[0] a legrégebbi (ablak eleje), a bucket[BUCKET_COUNT-1] a "most".
 */
function buildBuckets(incidents: CorrelatedIncident[], now: number): Bucket[] {
  const windowStart = now - WINDOW_SECONDS * 1000
  const buckets: Bucket[] = Array.from({ length: BUCKET_COUNT }, () => ({
    alertCount: 0,
    hasOpen: false,
  }))

  for (const incident of incidents) {
    const t = parseUtc(incident.created_at)
    if (t < windowStart || t > now) continue // ablakon kívül

    let idx = Math.floor((t - windowStart) / BUCKET_MS)
    if (idx < 0) idx = 0
    if (idx >= BUCKET_COUNT) idx = BUCKET_COUNT - 1

    buckets[idx].alertCount += incident.raw_alert_count
    if (incident.status === 'OPEN') buckets[idx].hasOpen = true
  }

  return buckets
}

export default function AlertTimeline() {
  const { data: incidents } = useQuery({
    queryKey: QUERY_KEYS.incidents,
    queryFn: fetchIncidents,
  })

  // Másodpercenként léptetjük a "most" időpontot, hogy az ablak valóban
  // csússzon akkor is, ha épp nem érkezik új incidens.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const buckets = buildBuckets(incidents ?? [], now)

  // A magasságot az ablakon belüli legmagasabb bucket értékéhez skálázzuk,
  // így mindig kitölti a teret, és nincs önkényes "/10" osztó.
  const maxCount = Math.max(1, ...buckets.map(b => b.alertCount))

  return (
    <div className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionHeaderLabel}>
          SIGNAL RESPONSE MAP // HISTORICAL TIMELINE
        </span>
        <span className={styles.sectionHeaderBadge}>
          SLIDING WINDOW: {WINDOW_SECONDS}S
        </span>
      </div>
      <div className={styles.chart}>
        {buckets.map((bucket, i) => {
          // Üres bucket: alig látszó alapvonal. Adattal: arányos magasság.
          const height = bucket.alertCount === 0
            ? 2
            : Math.max(6, (bucket.alertCount / maxCount) * 100)
          return (
            <div
              key={i}
              className={`${styles.bar} ${bucket.hasOpen ? styles['bar--alert'] : ''}`}
              style={{ height: `${height}%` }}
              title={`${bucket.alertCount} raw alert(s)`}
            />
          )
        })}
      </div>
    </div>
  )
}
