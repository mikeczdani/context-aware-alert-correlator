"""
storage/incident_store.py – Tartós NoSQL dokumentumtár és ARR számlálók (4. Réteg)

Ez a modul kezeli a Redis-ben az összes tartós (lejárat nélküli) adatot:
  1. incident:{id}         (String/JSON) – a teljes incidens dokumentum
  2. incidents:timeline    (List)        – incidens ID-k időrendi sorban
  3. stats:metrics         (Hash)        – atomi ARR számlálók

A Redis AOF (Append-Only File) perzisztenciával fut (docker-compose.yml-ben
--appendonly yes), így az adatok szerver-újraindítás után is megmaradnak.

Miért Redis NoSQL és nem SQLite / PostgreSQL?
  A szakdolgozatban a Redis egy motorral old meg három különböző feladatot:
  az átmeneti korrelációs ablakot (window_store.py), a tartós dokumentumtárat
  (ez a fájl), és a valós idejű Pub/Sub csatornát (redis_client.py).
  Ez a "pehelysúlyú egységesítés" az egyik fő architekturális döntés,
  amelyet a szakdolgozat 4. fejezetében indokolni kell.
"""

from __future__ import annotations

import json
from typing import List, Optional

from app.schemas.incident import CorrelatedIncident, SystemMetrics
from app.schemas.topology import KialiGraph
from app.storage.redis_client import get_redis, publish_update

# ---------------------------------------------------------------------------
# Redis kulcsok
# ---------------------------------------------------------------------------

INCIDENTS_TIMELINE_KEY = "incidents:timeline"
TOPOLOGY_KEY = "topology:current"
METRICS_KEY = "stats:metrics"


# ---------------------------------------------------------------------------
# Incidens mentés és lekérdezés
# ---------------------------------------------------------------------------

async def save_incident(incident: CorrelatedIncident) -> None:
    """
    Elmenti a korrelált incidenst a Redis-be, és értesíti az SSE csatornát.

    Két helyre ment egyszerre (pipeline-ban, atomian):
      1. incident:{id}       → a teljes CorrelatedIncident JSON dokumentum.
         Ezt a GET /api/v1/incidents endpoint ID alapján visszaolvassa.
      2. incidents:timeline  → csak az ID-t tárolja itt időrendi listában.
         Ez teszi lehetővé az összes incidens listázását KEYS * nélkül
         (ami production Redis-ben tiltott, mert blokkolja a szervert).

    Miért pipeline(transaction=True)?
      A két Redis parancsot (SET + RPUSH) atomiként hajtja végre.
      Ha az alkalmazás az első parancs után összeomlik, a második
      sem fut le → az adatbázis nem kerül inkonzisztens állapotba
      (ne legyen timeline-ban ID, amelyhez nem létezik dokumentum).

    A mentés után publish_update()-t hív → az SSE csatorna jelzést küld
    a React frontendnek, hogy frissítse az incidens listát.
    """
    r = await get_redis()

    incident_key = f"incident:{incident.id}"
    # model_dump_json(): a Pydantic v2 natív JSON serializer-je,
    # gyorsabb mint json.dumps(model.model_dump()), és kezeli a datetime-ot is
    incident_json = incident.model_dump_json()

    async with r.pipeline(transaction=True) as pipe:
        pipe.set(incident_key, incident_json)
        pipe.rpush(INCIDENTS_TIMELINE_KEY, incident.id)
        await pipe.execute()

    # SSE értesítés: a React frontend invalidálja a cache-t és újralekérdez
    await publish_update("new_incident")


async def get_incident_by_id(incident_id: str) -> Optional[CorrelatedIncident]:
    """
    Visszaad egyetlen incidenst az ID alapján.

    Ha nem létezik a kulcs (pl. manuálisan törölték vagy soha nem jött létre),
    None-t ad vissza – a hívó kódnak ezt le kell kezelnie.
    """
    r = await get_redis()
    data = await r.get(f"incident:{incident_id}")
    if data is None:
        return None
    return CorrelatedIncident.model_validate_json(data)


async def get_all_incidents() -> List[CorrelatedIncident]:
    """
    Visszaadja az összes incidenst időrendi sorban (legrégebbitől a legújabbig).

    Lépések:
      1. LRANGE 0 -1: az összes ID lekérése az incidents:timeline listából.
      2. Minden ID-hoz GET incident:{id}: a teljes dokumentum visszaolvasása.
      3. Ha valamelyik dokumentum közben törlődött (reset vagy manuális törlés),
         egyszerűen kihagyja – a lista konzisztens marad.

    A GET /api/v1/incidents dashboard_routes.py hívja ezt a függvényt.
    """
    r = await get_redis()

    ids = await r.lrange(INCIDENTS_TIMELINE_KEY, 0, -1)
    if not ids:
        return []

    incidents: List[CorrelatedIncident] = []
    for incident_id in ids:
        incident = await get_incident_by_id(incident_id)
        if incident is not None:
            incidents.append(incident)
    return incidents


# ---------------------------------------------------------------------------
# Aktuális topológia mentés és lekérdezés
# ---------------------------------------------------------------------------

async def save_topology(graph: KialiGraph) -> None:
    """
    Elmenti a legutóbbi Kiali gráf pillanatképet a topology:current kulcsba.

    Ez a kulcs mindig felülíródik az újabb gráffal – csak az aktuális
    topológia számít a React Flow számára. A historikus pillanatképeket
    az egyes incident:{id} dokumentumokban tároljuk (topology_snapshot mező),
    így az incidensek utólag is "visszajátszhatók".

    A GET /api/v1/topology/current endpoint ezt olvassa vissza.
    """
    r = await get_redis()
    await r.set(TOPOLOGY_KEY, graph.model_dump_json())


async def get_topology() -> Optional[KialiGraph]:
    """
    Visszaadja az aktuális Kiali gráfot, vagy None-t, ha még nem érkezett.

    A React Flow az oldal betöltésekor lekérdezi ezt az endpointon keresztül,
    hogy megjelenítse a kezdeti topológiát.
    """
    r = await get_redis()
    data = await r.get(TOPOLOGY_KEY)
    if data is None:
        return None
    return KialiGraph.model_validate_json(data)


# ---------------------------------------------------------------------------
# Atomi ARR számlálók
# ---------------------------------------------------------------------------

async def increment_metrics(
    raw_alerts: int = 0,
    suppressed: int = 0,
    correlated: int = 0,
) -> None:
    """
    Atomian növeli a Redis Hash számlálókat az ARR % számításhoz.

    Miért HINCRBY és nem GET → számol → SET?
      Ha két webhook kérés egyszerre érkezik (párhuzamos asyncio task-ok),
      a sima GET+SET versenyhelyzetet okoz (race condition):
        Task A: GET → 5
        Task B: GET → 5   (még nem látja A változtatását)
        Task A: SET → 6
        Task B: SET → 6   ← A növelése elveszett!
      A HINCRBY atomi Redis-szinten: garantáltan nem veszhet el számláló,
      még párhuzamos kérések esetén sem.

    Paraméterek:
      raw_alerts:  Hány nyers riasztás érkezett ebben a batch-ben.
      suppressed:  Ebből hány lett zajként elnyomva (SUPPRESSED_*).
      correlated:  Hány valódi incidens keletkezett (általában 0 vagy 1).
    """
    r = await get_redis()

    # Pipeline: a három HINCRBY egyetlen hálózati round-trip alatt megy ki
    async with r.pipeline(transaction=True) as pipe:
        if raw_alerts > 0:
            pipe.hincrby(METRICS_KEY, "raw_alerts_total", raw_alerts)
        if suppressed > 0:
            pipe.hincrby(METRICS_KEY, "suppressed_total", suppressed)
        if correlated > 0:
            pipe.hincrby(METRICS_KEY, "correlated_incidents_total", correlated)
        await pipe.execute()


async def get_metrics() -> SystemMetrics:
    """
    Visszaadja az összes számlálót és kiszámolja az ARR %-ot.

    HGETALL: az összes mező-érték párt kéri le a stats:metrics Hash-ből
    egyetlen round-trip alatt. Ha a kulcs nem létezik (még nem érkezett
    egyetlen riasztás sem), üres dict-et ad vissza → minden érték 0.

    ARR % képlete (a markdownból):
      ARR = (1 - correlated_incidents / raw_alerts) * 100

    Példa (03_mixed_storm forgatókönyv):
      raw_alerts = 6, correlated_incidents = 1
      ARR = (1 - 1/6) * 100 = 83.3%
    """
    r = await get_redis()

    data = await r.hgetall(METRICS_KEY)

    raw = int(data.get("raw_alerts_total", 0))
    suppressed = int(data.get("suppressed_total", 0))
    correlated = int(data.get("correlated_incidents_total", 0))

    # Nullával való osztás védelme: ha még nem érkezett riasztás, ARR = 0.0
    arr = round((1 - correlated / raw) * 100, 2) if raw > 0 else 0.0

    return SystemMetrics(
        raw_alerts_total=raw,
        suppressed_total=suppressed,
        correlated_incidents_total=correlated,
        alert_reduction_rate_pct=arr,
    )


# ---------------------------------------------------------------------------
# Reset – teljes adatbázis törlés
# ---------------------------------------------------------------------------

async def flush_all() -> None:
    """
    Törli az összes kulcsot az aktuális Redis adatbázisból (db 0).

    A DELETE /api/v1/incidents/reset végpont hívja, hogy a következő
    tesztfuttatás előtt tiszta lappal lehessen kezdeni.

    FLUSHDB vs FLUSHALL:
      FLUSHDB: csak az aktuálisan kiválasztott adatbázist (db 0) törli.
      FLUSHALL: az összes Redis adatbázist törli (db 0–15).
      Mi FLUSHDB-t használunk, mert csak a saját adatainkat akarjuk törölni.

    FIGYELEM: ez a művelet visszafordíthatatlan. Production környezetben
    ezt a végpontot le kell tiltani vagy jelszóval védeni.
    """
    r = await get_redis()
    await r.flushdb()
