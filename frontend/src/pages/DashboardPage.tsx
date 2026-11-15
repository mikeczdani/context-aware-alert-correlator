import AppHeader from '../components/ui/AppHeader'
import Panel from '../components/ui/Panel'
import SystemVitals from '../components/metrics/SystemVitals'
import IncidentFeed from '../components/incidents/IncidentFeed'
import AlertTimeline from '../components/metrics/AlertTimeline'
import ServiceGraph from '../components/topology/ServiceGraph'
import styles from './DashboardPage.module.css'

export default function DashboardPage() {
  return (
    <>
      <AppHeader />

      <main className={styles.main}>
        {/* Bal oszlop: Gráf + Terminál */}
        <div className={styles.heroSection}>
          <Panel title="Network Response Matrix" badge="REDIS + NETWORKX">
            <ServiceGraph />
          </Panel>

          <Panel title="Terminal // SSE Stream & Audit" badge="LIVE SYNC" badgeColor="var(--accent-purple)">
            <IncidentFeed />
          </Panel>
        </div>

        {/* Jobb oszlop: System Vitals + Node Cluster State */}
        <div className={styles.sidebarSection}>
          <SystemVitals />

          <Panel title="Node Cluster State">
            <div className={styles.nodeMap}>
              [ K8s CLUSTER: MOCK_MODE ]<br />
              ISTIO SERVICE MESH: ACTIVE<br />
              NODES ONLINE: 3 / 3
            </div>
          </Panel>
        </div>
      </main>

      <AlertTimeline />
    </>
  )
}
