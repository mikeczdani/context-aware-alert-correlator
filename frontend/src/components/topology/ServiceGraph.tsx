/**
 * components/topology/ServiceGraph.tsx – React Flow Kiali topológia gráf
 *
 * A "Network Response Matrix" panel tartalma. Két forrást kombinál:
 *   1. GET /topology/current  → a nyers Kiali gráf (csomópontok + élek + metrikák)
 *   2. GET /incidents          → a legutóbbi incidens affected_nodes státuszai
 *      (ROOT_CAUSE / SYMPTOM / SUPPRESSED_*), amivel a csomópontokat színezzük.
 *
 * A Kiali gráf maga NEM tartalmaz korrelációs státuszt – azt az engine írja
 * az incidensbe. Ezért a service név alapján párosítjuk a kettőt.
 */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactFlow, {
  Background,
  BackgroundVariant,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from 'reactflow'
import 'reactflow/dist/style.css'

import { fetchTopology, fetchIncidents } from '../../api/incidentsApi'
import { QUERY_KEYS } from '../../hooks/useSSEStream'
import type { KialiGraph, NodeStatus } from '../../types/api'
import styles from './ServiceGraph.module.css'

// ---------------------------------------------------------------------------
// Egyedi csomópont adat + komponens
// ---------------------------------------------------------------------------

interface ServiceNodeData {
  service: string
  status: NodeStatus
  httpErrorRate: number
}

/**
 * A NodeStatus-ból a megfelelő CSS moduldekorációs osztályt adja vissza.
 * HEALTHY → nincs extra osztály (alap neon). ROOT_CAUSE → piros.
 * SYMPTOM → sárga. Bármely SUPPRESSED* → szaggatott sárga.
 */
function statusClass(status: NodeStatus): string {
  if (status === 'ROOT_CAUSE') return styles['node--root']
  if (status === 'SYMPTOM') return styles['node--symptom']
  if (status.startsWith('SUPPRESSED')) return styles['node--suppressed']
  return ''
}

/** A csomópont státusz-feliratának szövege. */
function statusLabel(status: NodeStatus, httpErrorRate: number): string {
  switch (status) {
    case 'ROOT_CAUSE':
      return 'ROOT CAUSE'
    case 'SYMPTOM':
      return 'SYMPTOM'
    case 'SUPPRESSED':
    case 'SUPPRESSED_SCALING':
    case 'SUPPRESSED_RESTART':
      return 'SUPPRESSED'
    default:
      return `OK (${(httpErrorRate * 100).toFixed(0)}%)`
  }
}

function ServiceNode({ data }: NodeProps<ServiceNodeData>) {
  return (
    <div className={`${styles.node} ${statusClass(data.status)}`}>
      {/* Handle-ök az élekhez – vizuálisan elrejtve (lásd CSS) */}
      <Handle type="target" position={Position.Left} className={styles.handle} />
      <div className={styles.nodeName}>{data.service}</div>
      <div className={styles.nodeStatus}>
        {statusLabel(data.status, data.httpErrorRate)}
      </div>
      <Handle type="source" position={Position.Right} className={styles.handle} />
    </div>
  )
}

const nodeTypes = { service: ServiceNode }

// ---------------------------------------------------------------------------
// Layout: a hívási lánc mélysége (depth) szerint oszlopokba rendezés
// ---------------------------------------------------------------------------

const COLUMN_WIDTH = 230
const ROW_HEIGHT = 90
const X_OFFSET = 30
const Y_OFFSET = 30

/**
 * Minden csomóponthoz kiszámítja a "mélységét" (hány hívási lépésre van a
 * belépési pontoktól). A belépési pontok (nincs bejövő él) depth = 0.
 * Az X koordináta a depth-ből, az Y az azonos depth-en belüli sorszámból jön.
 */
function computeLayout(graph: KialiGraph): Map<string, { x: number; y: number }> {
  // service → bejövő élek száma (indegree) a belépési pontok megtalálásához
  const indegree = new Map<string, number>()
  const adjacency = new Map<string, string[]>()

  // A Kiali élek node ID-kra hivatkoznak (source/target = KialiNode.id),
  // ezért előbb egy id → service leképezést készítünk.
  const idToService = new Map<string, string>()
  for (const n of graph.nodes) {
    idToService.set(n.id, n.service)
    indegree.set(n.service, 0)
    adjacency.set(n.service, [])
  }

  for (const e of graph.edges) {
    const src = idToService.get(e.source)
    const tgt = idToService.get(e.target)
    if (!src || !tgt) continue
    adjacency.get(src)!.push(tgt)
    indegree.set(tgt, (indegree.get(tgt) ?? 0) + 1)
  }

  // BFS a belépési pontoktól (indegree 0), hogy minden node depth-et kapjon.
  const depth = new Map<string, number>()
  const queue: string[] = []
  for (const [service, deg] of indegree) {
    if (deg === 0) {
      depth.set(service, 0)
      queue.push(service)
    }
  }
  // Ha minden node-nak van bejövő éle (kör), fallback: az első node depth 0.
  if (queue.length === 0 && graph.nodes.length > 0) {
    const first = graph.nodes[0].service
    depth.set(first, 0)
    queue.push(first)
  }

  while (queue.length > 0) {
    const current = queue.shift()!
    const d = depth.get(current) ?? 0
    for (const next of adjacency.get(current) ?? []) {
      if (!depth.has(next) || (depth.get(next) ?? 0) < d + 1) {
        depth.set(next, d + 1)
        queue.push(next)
      }
    }
  }

  // Node-ok, amik kimaradtak (izolált, nincs él sem be, sem ki): depth 0.
  for (const n of graph.nodes) {
    if (!depth.has(n.service)) depth.set(n.service, 0)
  }

  // Oszloponkénti sorszám az Y-hoz
  const rowInColumn = new Map<number, number>()
  const positions = new Map<string, { x: number; y: number }>()
  for (const n of graph.nodes) {
    const d = depth.get(n.service) ?? 0
    const row = rowInColumn.get(d) ?? 0
    rowInColumn.set(d, row + 1)
    positions.set(n.service, {
      x: X_OFFSET + d * COLUMN_WIDTH,
      y: Y_OFFSET + row * ROW_HEIGHT,
    })
  }

  return positions
}

// ---------------------------------------------------------------------------
// Fő komponens
// ---------------------------------------------------------------------------

export default function ServiceGraph() {
  const { data: topology } = useQuery({
    queryKey: QUERY_KEYS.topology,
    queryFn: fetchTopology,
  })

  const { data: incidents } = useQuery({
    queryKey: QUERY_KEYS.incidents,
    queryFn: fetchIncidents,
  })

  const { nodes, edges } = useMemo<{ nodes: Node[]; edges: Edge[] }>(() => {
    if (!topology || topology.nodes.length === 0) {
      return { nodes: [], edges: [] }
    }

    // service → státusz a LEGUTÓBBI incidensből (ha van).
    // Több incidens esetén az utolsó (legfrissebb) affected_nodes-ait használjuk.
    const statusByService = new Map<string, NodeStatus>()
    if (incidents && incidents.length > 0) {
      const latest = incidents[incidents.length - 1]
      for (const n of latest.affected_nodes) {
        statusByService.set(n.service, n.status)
      }
    }

    const positions = computeLayout(topology)
    const idToService = new Map(topology.nodes.map(n => [n.id, n.service]))

    const rfNodes: Node[] = topology.nodes.map(n => ({
      id: n.service,
      type: 'service',
      position: positions.get(n.service) ?? { x: X_OFFSET, y: Y_OFFSET },
      data: {
        service: n.service,
        status: statusByService.get(n.service) ?? ('HEALTHY' as NodeStatus),
        httpErrorRate: n.http_error_rate,
      } satisfies ServiceNodeData,
      draggable: true,
    }))

    const rfEdges: Edge[] = topology.edges.flatMap(e => {
      const src = idToService.get(e.source)
      const tgt = idToService.get(e.target)
      if (!src || !tgt) return []
      // Ha a cél ROOT_CAUSE vagy SYMPTOM, az él kiemelve (piros/sárga).
      const tgtStatus = statusByService.get(tgt)
      const stroke =
        tgtStatus === 'ROOT_CAUSE'
          ? 'var(--danger)'
          : tgtStatus === 'SYMPTOM'
            ? 'var(--warning)'
            : 'rgba(0, 255, 102, 0.35)'
      return [{
        id: e.id,
        source: src,
        target: tgt,
        animated: tgtStatus === 'ROOT_CAUSE' || tgtStatus === 'SYMPTOM',
        style: { stroke, strokeWidth: 2 },
      }]
    })

    return { nodes: rfNodes, edges: rfEdges }
  }, [topology, incidents])

  if (nodes.length === 0) {
    return (
      <div className={styles.empty}>
        [ NO TOPOLOGY — RUN A SCENARIO ]
      </div>
    )
  }

  return (
    <div className={styles.matrixBox}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        nodesConnectable={false}
        edgesFocusable={false}
        panOnDrag
        zoomOnScroll={false}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color="rgba(0, 255, 102, 0.08)"
        />
      </ReactFlow>
    </div>
  )
}
