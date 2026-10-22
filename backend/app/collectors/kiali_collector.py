"""
collectors/kiali_collector.py – Kiali gráf adapter (2. Réteg)

Ez a modul felelős a Kiali mikroszolgáltatás-topológia lekéréséért.
Két üzemmódban működik, amelyet a core/config.py DATA_MODE beállítása vezérel:

  MOCK mód (fejlesztés és szakdolgozati mérések):
    A backend/scenarios/ mappából tölti be az előre elkészített JSON fájlokat.
    Nincs szükség futó K8s kluszterre – bármelyik gépen reprodukálhatók a mérések.
    A betöltendő forgatókönyvet a get_graph() hívójának kell megadnia (scenario_id).

  LIVE mód (demó és integrációs teszt):
    A Kiali REST API GET /api/namespaces/{namespace}/graph végpontját hívja.
    Ehhez futó k3d / Minikube kluszter kell Istio + Kiali-val, és a Kiali UI
    elérhetőnek kell lennie a KIALI_URL-en (pl. kubectl port-forward után).

A modul kimenete minden esetben egy egységes KialiGraph Pydantic objektum,
így az engine/ réteg nem tudja, és nem is kell tudnia, hogy MOCK vagy LIVE módban fut.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import DataMode, settings
from app.schemas.topology import KialiEdge, KialiGraph, KialiNode

# ---------------------------------------------------------------------------
# Belső segédfüggvények: MOCK mód
# ---------------------------------------------------------------------------

# A scenarios/ mappa abszolút elérési útja.
# __file__ = .../backend/app/collectors/kiali_collector.py
# Három szinttel feljebb: .../backend/
# Onnan: scenarios/
_BACKEND_ROOT = Path(__file__).parent.parent.parent
_SCENARIOS_DIR = _BACKEND_ROOT / settings.SCENARIOS_DIR

# Forgatókönyv ID → fájlnév leképezés.
# Így a webhook_routes.py és a simulator egyszerű string-azonosítókkal hivatkozhat
# a JSON fájlokra, anélkül hogy ismernie kellene a tényleges fájlneveket.
SCENARIO_FILE_MAP: dict[str, str] = {
    "healthy_baseline":    "00_healthy_baseline.json",
    "pod_restart_noise":   "01_pod_restart_noise.json",
    "cascading_db_timeout": "02_cascading_latency.json",
    "mixed_storm":         "03_mixed_failure.json",
}


def _load_scenario_json(scenario_id: str) -> dict:
    """
    Betölti a megadott forgatókönyv JSON fájlját a scenarios/ mappából.

    Ha a scenario_id nem szerepel a SCENARIO_FILE_MAP-ben, ValueError-t dob,
    hogy a hívó azonnal értesüljön az elírásról (ne fusson tovább hibás adattal).

    Ha a fájl nem létezik a lemezen, FileNotFoundError-t dob.
    """
    filename = SCENARIO_FILE_MAP.get(scenario_id)
    if filename is None:
        available = list(SCENARIO_FILE_MAP.keys())
        raise ValueError(
            f"Ismeretlen scenario_id: '{scenario_id}'. "
            f"Elérhető forgatókönyvek: {available}"
        )

    filepath = _SCENARIOS_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(
            f"A forgatókönyv fájl nem található: {filepath}. "
            f"Futtasd le a projektet a backend/ mappából."
        )

    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def _parse_kiali_graph_from_scenario(data: dict) -> KialiGraph:
    """
    Átalakítja a scenarios/*.json kiali_graph szekcióját KialiGraph objektummá.

    A JSON struktúra a scenarios fájlokban:
      {
        "kiali_graph": {
          "namespace": "default",
          "nodes": [...],
          "edges": [...]
        }
      }

    A nodes és edges listákat KialiNode, ill. KialiEdge objektumokká alakítjuk
    a Pydantic model_validate() segítségével, amely validálja a mezőket és
    alapértelmezéseket alkalmaz a hiányzó opcionális mezőkre.
    """
    graph_data = data["kiali_graph"]

    nodes = [KialiNode.model_validate(n) for n in graph_data.get("nodes", [])]
    edges = [KialiEdge.model_validate(e) for e in graph_data.get("edges", [])]

    return KialiGraph(
        namespace=graph_data.get("namespace", settings.KIALI_NAMESPACE),
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Belső segédfüggvények: LIVE mód
# ---------------------------------------------------------------------------

async def _fetch_live_kiali_graph(namespace: str) -> KialiGraph:
    """
    Lekéri a Kiali REST API-tól az aktuális szolgáltatás-topológiát.

    A Kiali /api/namespaces/{namespace}/graph végpontja egy összetett JSON-t
    ad vissza. Ebből csak a "nodes" és "edges" elemeket vesszük ki, és
    leképezzük a saját KialiGraph modellünkre.

    Kiali API válasz struktúrája (egyszerűsítve):
      {
        "elements": {
          "nodes": [
            { "data": { "id": "...", "app": "payment", "responseTime": 1450, ... } }
          ],
          "edges": [
            { "data": { "id": "...", "source": "...", "target": "...", "traffic": {...} } }
          ]
        }
      }

    Megjegyzés: A Kiali API válaszának pontos struktúrája Kiali verziónként
    kissé eltérhet. Ez az implementáció a Kiali v1.x / v2.x formátumot követi.

    httpx.AsyncClient: aszinkron HTTP kliens, FastAPI event loop-kompatibilis.
    Az időtúllépés (timeout=10.0) megakadályozza, hogy egy lassú Kiali válasz
    blokkolja a webhook feldolgozást.
    """
    url = f"{settings.KIALI_URL}/api/namespaces/{namespace}/graph"
    params = {
        "graphType": "service",
        "injectServiceNodes": "true",
        "appenders": "deadNode,sidecarsCheck,istio,serviceEntry,meshCheck",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    elements = data.get("elements", {})
    raw_nodes = elements.get("nodes", [])
    raw_edges = elements.get("edges", [])

    nodes: list[KialiNode] = []
    for raw in raw_nodes:
        d = raw.get("data", {})
        # A Kiali "app" mezője felel meg a mi "service" nevünknek
        service_name = d.get("app") or d.get("workload") or d.get("service", "unknown")
        nodes.append(KialiNode(
            id=d["id"],
            service=service_name,
            namespace=d.get("namespace", namespace),
            # A Kiali ms-ban adja meg a válaszidőt (responseTime mező)
            response_time_ms=float(d.get("responseTime", 0.0)),
            # A httpPercentErr 0–100 skálán van a Kiali-ban, nekünk 0.0–1.0 kell
            http_error_rate=float(d.get("httpPercentErr", 0.0)) / 100.0,
            traffic_rate_rps=float(d.get("rps", 0.0)),
        ))

    edges: list[KialiEdge] = []
    for raw in raw_edges:
        d = raw.get("data", {})
        traffic = d.get("traffic", {})
        edges.append(KialiEdge(
            id=d["id"],
            source=d["source"],
            target=d["target"],
            traffic_rate_rps=float(traffic.get("rates", {}).get("http", 0.0)),
        ))

    return KialiGraph(namespace=namespace, nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Publikus API – ezt hívja a webhook_routes.py és a simulator
# ---------------------------------------------------------------------------

async def get_graph(
    scenario_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> KialiGraph:
    """
    Visszaadja az aktuális Kiali topológia-gráfot.

    MOCK módban: betölti a megadott scenario_id JSON fájlját.
      - Ha scenario_id=None, az alapállapotot (00_healthy_baseline) adja vissza.
    LIVE módban: lekéri a valódi Kiali API-tól.
      - A namespace paraméter felülírja a config.py KIALI_NAMESPACE értékét.

    Ezt a függvényt hívja:
      - webhook_routes.py: riasztás beérkezésekor lekéri az aktuális gráfot
      - chaos_runner.py: a szimulátor forgatókönyv betöltésekor
      - dashboard_routes.py: a /api/v1/simulate/{scenario} végpont
    """
    ns = namespace or settings.KIALI_NAMESPACE

    if settings.DATA_MODE == DataMode.MOCK:
        sid = scenario_id or "healthy_baseline"
        raw_data = _load_scenario_json(sid)
        return _parse_kiali_graph_from_scenario(raw_data)
    else:
        return await _fetch_live_kiali_graph(ns)
