"""
api/dashboard_routes.py – Lekérdező és szimulációs REST végpontok (2–4. Réteg)

Ez a fájl tartalmazza az összes lekérdező és vezérlő végpontot,
amelyeket a React frontend TanStack Query-n keresztül hív:

  GET  /api/v1/incidents          – az összes incidens listája
  GET  /api/v1/topology/current   – aktuális Kiali gráf + státusz-cimkék
  GET  /api/v1/metrics/summary    – ARR % és számlálók
  POST /api/v1/simulate/{scenario} – teszt-forgatókönyv indítása
  DELETE /api/v1/incidents/reset  – Redis törlése, új tesztfuttatás előtt

A szimulátor végpont a chaos_runner.py előzménye nélkül is lehetővé teszi
a forgatókönyvek egykattintásos elindítását a React UI-ból – a backend
maga hívja meg a saját webhook végpontját a scenario JSON riasztásaival.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status

from app.collectors.kiali_collector import SCENARIO_FILE_MAP, get_graph
from app.collectors.k8s_collector import get_k8s_contexts
from app.engine.graph_correlator import correlate
from app.schemas.alert import RawAlert
from app.schemas.incident import CorrelatedIncident, SystemMetrics
from app.schemas.topology import KialiGraph
from app.storage.incident_store import (
    flush_all,
    get_all_incidents,
    get_metrics,
    get_topology,
    increment_metrics,
    save_incident,
    save_topology,
)
from app.storage.window_store import clear_window, push_alert_to_window
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Dashboard"])

# Scenarios mappa elérési útja (a simulate végponthoz kell)
_BACKEND_ROOT = Path(__file__).parent.parent.parent
_SCENARIOS_DIR = _BACKEND_ROOT / settings.SCENARIOS_DIR


# ---------------------------------------------------------------------------
# GET /api/v1/incidents
# ---------------------------------------------------------------------------

@router.get(
    "/incidents",
    response_model=List[CorrelatedIncident],
    summary="Összes incidens listája",
    description=(
        "Visszaadja a Redis incidents:timeline listájából az összes korrelált "
        "incidenst időrendi sorban. Tartalmazza a valódi incidenseket (OPEN) "
        "és az elnyomott zajokat (SUPPRESSED) egyaránt, az indoklásokkal együtt."
    ),
)
async def get_incidents() -> List[CorrelatedIncident]:
    """
    Visszaadja az összes tárolt incidenst.

    A React IncidentFeed komponens ezt a végpontot kérdezi le TanStack Query-vel,
    és az SSE jelzés hatására automatikusan újralekérdezi (invalidateQueries).

    Visszatér: CorrelatedIncident objektumok listája, legrégebbitől a legújabbig.
    Ha még nincs egyetlen incidens sem (pl. friss indítás vagy reset után),
    üres listát ad vissza (nem 404-et – az üres lista valid állapot).
    """
    return await get_all_incidents()


# ---------------------------------------------------------------------------
# GET /api/v1/topology/current
# ---------------------------------------------------------------------------

@router.get(
    "/topology/current",
    response_model=Optional[KialiGraph],
    summary="Aktuális Kiali topológia gráf",
    description=(
        "Visszaadja a Redis topology:current kulcsból az utolsó webhook "
        "feldolgozásakor elmentett Kiali gráfot. A React Flow ServiceGraph "
        "komponens ezt használja a csomópontok és élek megjelenítéséhez."
    ),
)
async def get_current_topology() -> Optional[KialiGraph]:
    """
    Visszaadja az aktuális Kiali topológia-pillanatképet.

    Ha még nem érkezett egyetlen webhook sem (pl. friss indítás),
    None-t ad vissza. A React Flow ilyenkor üres gráfot jelenít meg.

    Megjegyzés: ez mindig az UTOLSÓ webhook által elmentett topológiát
    adja vissza. A historikus pillanatképek az egyes incidens objektumok
    topology_snapshot mezőjében találhatók.
    """
    topology = await get_topology()
    if topology is None:
        # Nincs még mentett topológia – üres gráf visszaadása
        return KialiGraph()
    return topology


# ---------------------------------------------------------------------------
# GET /api/v1/metrics/summary
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/summary",
    response_model=SystemMetrics,
    summary="ARR % és rendszerstatisztikák",
    description=(
        "Visszaadja a Redis stats:metrics Hash-ből a szakdolgozati KPI mutatókat: "
        "nyers riasztások száma, elnyomott zajok, korrelált incidensek, ARR %."
    ),
)
async def get_metrics_summary() -> SystemMetrics:
    """
    Visszaadja az összesített rendszerstatisztikákat.

    A React ArrStatsCards komponens 4 kártyáját táplálja:
      - Beérkezett nyers riasztások (raw_alerts_total)
      - Kiszűrt zaj (suppressed_total)
      - Valódi incidensek (correlated_incidents_total)
      - ARR % = (1 - incidensek / nyers) * 100

    Ha még nem érkezett riasztás, minden érték 0 és ARR = 0.0%.
    """
    return await get_metrics()


# ---------------------------------------------------------------------------
# POST /api/v1/simulate/{scenario}
# ---------------------------------------------------------------------------

@router.post(
    "/simulate/{scenario_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Teszt-forgatókönyv indítása",
    description=(
        "Betölti a megadott scenarios/*.json fájlt, és a benne lévő riasztásokat "
        "egy az egyben feldolgozza, mintha a Prometheus Alertmanager küldte volna. "
        "Így a React UI-ból egyetlen gombnyomással demonstrálható a rendszer működése."
    ),
)
async def run_simulation(scenario_id: str) -> dict:
    """
    Elindít egy előre definiált teszt-forgatókönyvet.

    Elérhető scenario_id értékek:
      - healthy_baseline       (00_healthy_baseline.json)
      - pod_restart_noise      (01_pod_restart_noise.json)
      - cascading_db_timeout   (02_cascading_latency.json)
      - mixed_storm            (03_mixed_failure.json)

    Lépések:
      1. Betölti a scenario JSON fájlt a scenarios/ mappából.
      2. A "alerts" szekció minden riasztását beteszi a Redis időablakba.
      3. Lekéri a scenario Kiali gráfját és K8s kontextusát.
      4. Lefuttatja a korrelációs motort (zajszűrés + RCA + severity scoring).
      5. Elmenti az incidenst és frissíti a metrikákat.
      6. SSE értesítést küld a frontendnek.

    Ez funkcionálisan megegyezik azzal, mintha a chaos_runner.py
    POST kéréseket küldene a webhook végpontra – de egyetlen HTTP hívásban.
    """
    # Scenario ID validálása
    if scenario_id not in SCENARIO_FILE_MAP:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown scenario_id: '{scenario_id}'. "
                f"Available: {list(SCENARIO_FILE_MAP.keys())}"
            ),
        )

    # Scenario JSON betöltése
    filename = SCENARIO_FILE_MAP[scenario_id]
    filepath = _SCENARIOS_DIR / filename
    if not filepath.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scenario file not found: {filepath}",
        )

    with open(filepath, encoding="utf-8") as f:
        scenario_data = json.load(f)

    # Riasztások kinyerése a scenario JSON-ből és RawAlert-té alakítása
    raw_alert_dicts = scenario_data.get("alerts", [])
    if not raw_alert_dicts:
        return {
            "status": "accepted",
            "incident_id": None,
            "summary": f"Scenario '{scenario_id}' has no alerts – nothing to process.",
        }

    firing_alerts: List[RawAlert] = []
    for raw in raw_alert_dicts:
        # _comment mezők kiszűrése
        filtered = {k: v for k, v in raw.items() if not k.startswith("_")}
        alert = RawAlert.model_validate(filtered)
        if alert.status == "firing":
            firing_alerts.append(alert)

    if not firing_alerts:
        return {
            "status": "accepted",
            "incident_id": None,
            "summary": f"Scenario '{scenario_id}' has no firing alerts.",
        }

    logger.info(
        "Simulation started: scenario=%s, alerts=%d",
        scenario_id,
        len(firing_alerts),
    )

    # Riasztások betétele az időablakba
    for alert in firing_alerts:
        await push_alert_to_window(alert)

    # Kiali gráf és K8s kontextus betöltése a scenario-ból
    kiali_graph = await get_graph(scenario_id=scenario_id)
    await save_topology(kiali_graph)

    alerting_services = [a.service for a in firing_alerts]
    k8s_map = await get_k8s_contexts(
        services=alerting_services,
        scenario_id=scenario_id,
    )

    # Korrelációs motor futtatása
    incident = await correlate(
        alerts=firing_alerts,
        graph=kiali_graph,
        k8s_map=k8s_map,
    )

    # Mentés és metrikák frissítése
    await save_incident(incident)
    await increment_metrics(
        raw_alerts=incident.raw_alert_count,
        suppressed=incident.suppressed_count,
        correlated=1 if incident.root_cause_service is not None else 0,
    )

    logger.info(
        "Simulation complete: scenario=%s, incident=%s, root_cause=%s",
        scenario_id,
        incident.id,
        incident.root_cause_service,
    )

    return {
        "status": "accepted",
        "incident_id": incident.id,
        "summary": incident.summary,
        "root_cause": incident.root_cause_service,
        "suppressed_count": incident.suppressed_count,
        "correlated_count": incident.correlated_count,
    }


# ---------------------------------------------------------------------------
# DELETE /api/v1/incidents/reset
# ---------------------------------------------------------------------------

@router.delete(
    "/incidents/reset",
    status_code=status.HTTP_200_OK,
    summary="Redis törlése – új mérési futtatás előtt",
    description=(
        "FLUSHDB paranccsal törli az összes Redis kulcsot: incidenseket, "
        "topológiát, metrikákat és a korrelációs időablakot. "
        "Ezt a gombot a React SimulatorBar 'Reset' gombja hívja a következő "
        "tesztforgatókönyv előtt."
    ),
)
async def reset_all() -> dict:
    """
    Törli az összes adatot a Redis-ből.

    A törlés sorrendje:
      1. clear_window()  – a 60 mp-es korrelációs időablak törlése
      2. flush_all()     – az összes többi kulcs törlése (FLUSHDB)

    Miért kell a clear_window() külön, ha flush_all() úgyis mindent töröl?
      A FLUSHDB blokkolja a Redist a törlés idejére. Ha épp fut egy
      push_alert_to_window() hívás, versenyhelyzet keletkezhet.
      A clear_window() explicit DELETE paranccsal biztonságosan távolítja
      el az ablakot, mielőtt a teljes flush lefut.

    Visszatér: { "status": "ok", "message": "Redis flushed successfully" }
    """
    await clear_window()
    await flush_all()

    logger.info("Redis flushed – ready for next test run")

    return {
        "status": "ok",
        "message": "Redis flushed successfully. Ready for next test run.",
    }
