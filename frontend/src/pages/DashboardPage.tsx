import AppHeader from '../components/ui/AppHeader'
import Panel from '../components/ui/Panel'
import SystemVitals from '../components/metrics/SystemVitals'
import IncidentFeed from '../components/incidents/IncidentFeed'
import AlertTimeline from '../components/metrics/AlertTimeline'
import styles from './DashboardPage.module.css'

// ServiceGraph és további komponensek a következő fázisokban kerülnek ide

export default function DashboardPage() {
  return (
    <>
      <AppHeader />

      <main className={styles.main}>
        {/* Bal oldal: Gráf + Incidens feed */}
        <div className={styles.heroSection}>
          <Panel title="Network Response Matrix (Kiali Topology)" badge="REAL-TIME ANALYSIS">
            {/* ServiceGraph komponens ide kerül a 3. lépésben */}
          </Panel>

          <Panel title="Terminal // Event Stream & Audit Log" badge="SSE ACTIVE">
            <IncidentFeed />
          </Panel>
        </div>

        {/* Jobb oldal: System Vitals + Node Map */}
        <div className={styles.sidebarSection}>
          <SystemVitals />

          <Panel title="Global Node Map" badge="CLUSTER 01">
            <div className={styles.nodeMap}>
              [ KUBERNETES NODE MESH ]<br />
              NAMESPACES: istio-system, default<br />
              ACTIVE PODS: 12 / 12 RUNNING
            </div>
          </Panel>
        </div>
      </main>

      <AlertTimeline />
    </>
  )
}
