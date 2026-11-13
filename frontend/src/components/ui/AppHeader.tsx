import { useMutation, useQueryClient } from '@tanstack/react-query'
import { resetAll, runSimulation } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import styles from './AppHeader.module.css'

const SCENARIOS = [
  { id: 'pod_restart_noise',    label: '01. Scaling Noise', danger: false },
  { id: 'cascading_db_timeout', label: '02. Cascading DB',  danger: false },
  { id: 'mixed_storm',          label: '03. Storm',          danger: true  },
]

export default function AppHeader() {
  const queryClient = useQueryClient()

  const simulateMutation = useMutation({
    mutationFn: (scenarioId: string) => runSimulation(scenarioId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.incidents })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.metrics })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topology })
    },
  })

  const resetMutation = useMutation({
    mutationFn: resetAll,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.incidents })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.metrics })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topology })
    },
  })

  return (
    <header className={styles.header}>
      <div className={styles.logoArea}>
        <p>SYS.AIOPS // ARCHITECTURE v2.4</p>
        <h1>Context Engine</h1>
      </div>

      <div className={styles.actions}>
        {SCENARIOS.map(s => (
          <button
            key={s.id}
            className={`${styles.btn} ${s.danger ? styles.btnDanger : ''}`}
            onClick={() => simulateMutation.mutate(s.id)}
            disabled={simulateMutation.isPending}
          >
            {s.label}
          </button>
        ))}
        <button
          className={`${styles.btn} ${styles.btnReset}`}
          onClick={() => resetMutation.mutate()}
          disabled={resetMutation.isPending}
        >
          Reset
        </button>
      </div>
    </header>
  )
}
