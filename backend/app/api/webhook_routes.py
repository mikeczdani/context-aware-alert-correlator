"""
api/webhook_routes.py – Alert Webhook Fogadó végpont (2. Réteg)

Ez a fájl a rendszer egyetlen bemeneti kapuja: a Prometheus Alertmanager
(vagy a chaos_runner.py szimulátor) ide POST-olja a nyers riasztásokat.

A végpont a teljes feldolgozási pipeline-t koordinálja:
  1. Fogadja és validálja a WebhookPayload-ot (Pydantic)
  2. Berakja a riasztásokat a Redis 60 mp-es korrelációs ablakba
  3. Lekéri az aktuális Kiali gráfot (MOCK vagy LIVE)
  4. Lekéri az érintett service-ek K8s állapotát (MOCK vagy LIVE)
  5. Lefuttatja a korrelációs motort (zajszűrés + RCA + severity scoring)
  6. Elmenti az incidenst a Redis-be (tartós NoSQL)
  7. Frissíti az ARR számlálókat (HINCRBY)
  8. PUBLISH üzenetet küld a Redis Pub/Sub csatornára → SSE értesítés a frontendnek

URL: POST /api/v1/webhook/alerts
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status

from app.collectors.k8s_collector import get_k8s_contexts
from app.collectors.kiali_collector import get_graph
from app.engine.graph_correlator import correlate
from app.schemas.alert import WebhookPayload
from app.storage.incident_store import (
    increment_metrics,
    save_incident,
    save_topology,
)
from app.storage.window_store import push_alert_to_window

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Webhook"])


@router.post(
    "/webhook/alerts",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Prometheus Alertmanager webhook fogadó",
    description=(
        "Fogadja a Prometheus Alertmanager (vagy a szimulátor) által küldött "
        "riasztás csomagot, lefuttatja a korrelációs motort, és elmenti az "
        "eredményt a Redis-be. A feldolgozás után SSE értesítést küld a frontendnek."
    ),
)
async def receive_alerts(
    payload: WebhookPayload,
    scenario_id: Optional[str] = None,
) -> dict:
    """
    A webhook végpont teljes feldolgozási pipeline-ja.

    Paraméterek:
      payload:     A Prometheus Alertmanager által küldött JSON body.
                   Pydantic automatikusan validálja – ha hibás a struktúra,
                   422 Unprocessable Entity választ küld vissza.
      scenario_id: Query paraméter MOCK módban (opcionális).
                   Megmondja a kiali_collector-nak és k8s_collector-nak,
                   melyik scenarios/*.json fájlt töltse be.
                   Pl.: POST /api/v1/webhook/alerts?scenario_id=cascading_db_timeout
                   Ha None, a healthy_baseline-t használja.

    Visszatér: { "status": "accepted", "incident_id": "uuid", "summary": "..." }

    HTTP státuszkódok:
      202 Accepted:            A feldolgozás sikeresen lefutott.
      422 Unprocessable Entity: A payload JSON struktúrája hibás.
      500 Internal Server Error: Redis kapcsolati hiba vagy feldolgozási hiba.
    """

    # Csak a "firing" státuszú riasztásokat dolgozzuk fel
    # A "resolved" eseményeket jelenleg nem kezeljük (jövőbeli bővítési pont)
    firing_alerts = [a for a in payload.alerts if a.status == "firing"]

    if not firing_alerts:
        # Ha nincs firing riasztás (pl. csak resolved érkezett), elfogadjuk,
        # de nem csinálunk semmit – nem akarunk üres incidenst létrehozni
        logger.info("Webhook received with no firing alerts – skipping processing.")
        return {"status": "accepted", "incident_id": None, "summary": "No firing alerts"}

    logger.info(
        "Webhook received: %d firing alert(s) for services: %s",
        len(firing_alerts),
        [a.service for a in firing_alerts],
    )

    # -------------------------------------------------------------------
    # 1. Riasztások betétele a 60 mp-es korrelációs időablakba
    # -------------------------------------------------------------------
    # Minden riasztást külön-külön rakunk be, hogy az EXPIRE újrainduljon
    # (csúszó ablak: az utolsó riasztástól számít a 60 mp)
    for alert in firing_alerts:
        await push_alert_to_window(alert)

    # -------------------------------------------------------------------
    # 2. Kiali gráf lekérése
    # -------------------------------------------------------------------
    # MOCK módban a scenario_id-hoz tartozó JSON-t tölti be,
    # LIVE módban a valódi Kiali API-t hívja
    try:
        kiali_graph = await get_graph(scenario_id=scenario_id)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Failed to load Kiali graph: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Kiali graph unavailable: {e}",
        )

    # Az aktuális topológiát elmentjük a Redis-be, hogy a GET /topology/current
    # végpont mindig a legfrissebb állapotot adja vissza
    await save_topology(kiali_graph)

    # -------------------------------------------------------------------
    # 3. Kubernetes kontextus lekérése az érintett service-ekhez
    # -------------------------------------------------------------------
    # Csak azokhoz a service-ekhez kérjük le, amelyekre riasztás érkezett –
    # nem szükséges az összes service K8s állapota
    alerting_services = [a.service for a in firing_alerts]
    k8s_map = await get_k8s_contexts(
        services=alerting_services,
        scenario_id=scenario_id,
    )

    # -------------------------------------------------------------------
    # 4. Korrelációs motor futtatása
    # -------------------------------------------------------------------
    # correlate() lefuttatja:
    #   - noise_filter.filter_alerts_batch() (zajszűrés)
    #   - _find_connected_alerting_nodes() (csoportosítás)
    #   - _find_root_cause_in_group() (RCA)
    #   - score_incident() (severity scoring)
    incident = await correlate(
        alerts=firing_alerts,
        graph=kiali_graph,
        k8s_map=k8s_map,
    )

    # -------------------------------------------------------------------
    # 5. Incidens mentése és metrikák frissítése
    # -------------------------------------------------------------------
    # save_incident() automatikusan PUBLISH-t is küld → SSE értesítés
    await save_incident(incident)

    # Atomi számlálók frissítése az ARR % számításhoz
    await increment_metrics(
        raw_alerts=incident.raw_alert_count,
        suppressed=incident.suppressed_count,
        correlated=1 if incident.root_cause_service is not None else 0,
    )

    logger.info(
        "Incident %s created: status=%s, root_cause=%s, suppressed=%d, correlated=%d",
        incident.id,
        incident.status,
        incident.root_cause_service,
        incident.suppressed_count,
        incident.correlated_count,
    )

    return {
        "status": "accepted",
        "incident_id": incident.id,
        "summary": incident.summary,
    }
