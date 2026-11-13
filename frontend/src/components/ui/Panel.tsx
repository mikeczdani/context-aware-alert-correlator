import type { ReactNode } from 'react'
import styles from './Panel.module.css'

interface PanelProps {
  title: string
  badge?: string
  badgeColor?: string
  children: ReactNode
  className?: string
}

export default function Panel({ title, badge, children, className }: PanelProps) {
  return (
    <div className={`${styles.panel} ${className ?? ''}`}>
      <div className={styles.panelHeader}>
        <span>{title}</span>
        {badge && <span className={styles.panelHeaderBadge}>{badge}</span>}
      </div>
      {children}
    </div>
  )
}
