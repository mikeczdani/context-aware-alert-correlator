"""
tests/helpers.py – segédfüggvények a tesztekhez

Két csoportja van:
  1. Forgatókönyv-futtatás: a scenarios/*.json fájlokból a valódi collector
     függvényekkel (MOCK mód) építi fel a bemenetet, és lefuttatja a correlate()-et.
  2. Szintetikus esetek: kis gráfok és riasztások kézi összeállítása egy-egy
     szabály célzott teszteléséhez.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.collectors.k8s_collector import get_k8s_contexts
from app.collectors.kiali_collector import SCENARIO_FILE_MAP, get_graph
from app.engine.graph_correlator import correlate
from app.schemas.alert import RawAlert
from app.schemas.incident import CorrelatedIncident
from app.schemas.topology import K8sContext, KialiEdge, KialiGraph, KialiNode

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


# ---------------------------------------------------------------------------
# Forgatókönyv-futtatás
# ---------------------------------------------------------------------------

def load_scenario(scenario_id: str) -> dict:
    """Betölti a forgatókönyv teljes JSON-ját (az expected_outcome-mal együtt)."""
    with open(SCENARIOS_DIR / SCENARIO_FILE_MAP[scenario_id], encoding="utf-8") as f:
        return json.load(f)


def scenario_alerts(data: dict) -> List[RawAlert]:
    """
    A forgatókönyv firing riasztásai RawAlert-ként.

    A '_'-lel kezdődő (dokumentációs) kulcsokat kiszűri, a resolved riasztásokat
    eldobja – pontosan úgy, ahogy a /simulate végpont is teszi.
    """
    alerts: List[RawAlert] = []
    for raw in data.get("alerts", []):
        filtered = {k: v for k, v in raw.items() if not k.startswith("_")}
        alert = RawAlert.model_validate(filtered)
        if alert.status == "firing":
            alerts.append(alert)
    return alerts


async def _run_scenario_async(
    scenario_id: str,
    alert_order: Optional[Callable[[List[RawAlert]], List[RawAlert]]],
) -> CorrelatedIncident:
    alerts = scenario_alerts(load_scenario(scenario_id))
    if alert_order is not None:
        alerts = alert_order(alerts)

    graph = await get_graph(scenario_id=scenario_id)
    k8s_map = await get_k8s_contexts(
        services=[a.service for a in alerts],
        scenario_id=scenario_id,
    )
    return await correlate(alerts=alerts, graph=graph, k8s_map=k8s_map)


def run_scenario(
    scenario_id: str,
    alert_order: Optional[Callable[[List[RawAlert]], List[RawAlert]]] = None,
) -> CorrelatedIncident:
    """
    Lefuttatja a forgatókönyvet a valódi collectorokkal és a correlate()-tel.

    alert_order: opcionális függvény, amely átrendezi a riasztások sorrendjét
                 (a sorrendfüggetlenség teszteléséhez).
    """
    return asyncio.run(_run_scenario_async(scenario_id, alert_order))


# ---------------------------------------------------------------------------
# Szintetikus esetek
# ---------------------------------------------------------------------------

def make_graph(
    nodes: Dict[str, Tuple[float, float]],
    edges: List[Tuple[str, str]],
) -> KialiGraph:
    """
    Kis topológia összeállítása.

    nodes: { service → (hibaarány, forgalom rps) }
    edges: [(hívó service, hívott service)]
    """
    kiali_nodes = [
        KialiNode(
            id=f"node-{service}",
            service=service,
            http_error_rate=error_rate,
            traffic_rate_rps=rps,
        )
        for service, (error_rate, rps) in nodes.items()
    ]
    kiali_edges = [
        KialiEdge(id=f"edge-{i}", source=f"node-{src}", target=f"node-{dst}")
        for i, (src, dst) in enumerate(edges)
    ]
    return KialiGraph(nodes=kiali_nodes, edges=kiali_edges)


def make_alert(service: str, name: str = "HighErrorRate") -> RawAlert:
    return RawAlert(alert_name=name, service=service)


def healthy(service: str) -> K8sContext:
    """Egészséges szolgáltatás: nincs skálázódás, nincs újraindulás."""
    return K8sContext(service=service)


def restarting(service: str, restarts: int = 3) -> K8sContext:
    return K8sContext(service=service, restarts_last_minute=restarts)


def scaling(service: str) -> K8sContext:
    return K8sContext(
        service=service,
        desired_replicas=4,
        ready_replicas=2,
        is_scaling=True,
        last_scale_event_age_seconds=20.0,
    )


def correlate_sync(
    alerts: List[RawAlert],
    graph: KialiGraph,
    k8s_contexts: List[K8sContext],
) -> CorrelatedIncident:
    k8s_map = {ctx.service: ctx for ctx in k8s_contexts}
    return asyncio.run(correlate(alerts=alerts, graph=graph, k8s_map=k8s_map))


# ---------------------------------------------------------------------------
# Eredmények összehasonlítása
# ---------------------------------------------------------------------------

def statuses(incident: CorrelatedIncident) -> Dict[str, str]:
    """{ service → státusz-név } az érintett csomópontokra."""
    return {node.service: node.status.value for node in incident.affected_nodes}


def snapshot(incident: CorrelatedIncident) -> dict:
    """
    Az incidens összehasonlítható, JSON-szerializálható összefoglalója.
    A node-ok SORRENDJÉT is tartalmazza, mert a determinizmus erre is kiterjed.
    """
    return {
        "root": incident.root_cause_service,
        "status": incident.status.value,
        "summary": incident.summary,
        "counts": [
            incident.raw_alert_count,
            incident.suppressed_count,
            incident.correlated_count,
        ],
        "nodes": [
            [node.service, node.status.value, node.severity_score]
            for node in incident.affected_nodes
        ],
    }
