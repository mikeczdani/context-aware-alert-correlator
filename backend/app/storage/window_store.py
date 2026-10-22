"""
storage/window_store.py – 60 mp-es TTL csúszó korrelációs időablak (4. Réteg)

Ez a modul kezeli a Redis-ben a rövid életű, átmeneti korrelációs ablakot.

Alapötlet:
  Amikor az első riasztás beérkezik, a Redis-ben létrejön egy lista
  (window:active_alerts). Minden új riasztás ehhez a listához adódik hozzá,
  és az ablak TTL-je (60 másodperc) újraindul (csúszó ablak).
  Ha 60 másodpercig nem érkezik újabb riasztás, a Redis automatikusan
  törli a listát – nincs szükség manuális cleanup-ra.

  Az engine/ réteg (graph_correlator.py) ezt a listát olvassa ki,
  és az összes benne lévő riasztót egyszerre vizsgálja meg a gráfon.
  Így lehet összefüggő, lépcsőzetes hibákat (cascading failures) egyetlen
  incidensbe vonni, ahelyett hogy minden riasztásból külön incidens keletkezne.

Redis kulcs: window:active_alerts (List, TTL=60s)
"""

from __future__ import annotations

import json
from typing import List

from app.schemas.alert import RawAlert
from app.storage.redis_client import get_redis

# Az időablak hossza másodpercben – a markdown 3. rétegének leírása alapján
WINDOW_KEY = "window:active_alerts"
WINDOW_TTL_SECONDS = 60


async def push_alert_to_window(alert: RawAlert) -> int:
    """
    Beteszi a nyers riasztást a csúszó korrelációs időablakba.

    Lépések:
      1. RPUSH: a riasztást JSON-ként a lista végére fűzi.
      2. EXPIRE: (újra)indítja a 60 mp-es TTL-t → csúszó ablak.

    Miért RPUSH + EXPIRE és nem egyszerű SET?
      Ha SET-et használnánk, minden riasztás felülírná az előzőt.
      A lista (RPUSH) megőrzi az összes riasztást, amelyek az ablakon
      belül érkeztek – ezeket egyszerre adja át az engine-nek a
      graph_correlator.py, hogy összefüggő incidenseket keressen.

    mode="json": a Pydantic model_dump() alapból datetime objektumokat
    adna vissza, amelyek nem JSON-szerializálhatók. A mode="json" ezt
    automatikusan ISO 8601 stringgé alakítja.

    Visszatér: a listában lévő elemek száma a push után (az engine
    dönthet úgy, hogy első riasztásnál azonnal lefut, vagy vár töbpre).
    """
    r = await get_redis()

    alert_json = json.dumps(alert.model_dump(mode="json"))
    length = await r.rpush(WINDOW_KEY, alert_json)

    # Minden push után újraindítjuk a TTL-t → csúszó ablak
    await r.expire(WINDOW_KEY, WINDOW_TTL_SECONDS)

    return length


async def get_alerts_from_window() -> List[RawAlert]:
    """
    Visszaadja az időablakban lévő összes riasztást lista formájában.

    LRANGE 0 -1: a teljes listát kéri le (0 = első, -1 = utolsó elem).
    Ezt a graph_correlator.py hívja, amikor összegyűjti a korrelálandó
    riasztásokat.

    Ha az ablak már lejárt (TTL = 0, a Redis törölte), üres listát ad vissza.
    """
    r = await get_redis()
    raw_items = await r.lrange(WINDOW_KEY, 0, -1)

    alerts = []
    for item in raw_items:
        data = json.loads(item)
        # populate_by_name=True kell a RawAlert-ben, mert a JSON-ben
        # "alert_name" kulcs szerepel (nem "alertname" alias)
        alerts.append(RawAlert.model_validate(data))
    return alerts


async def get_window_size() -> int:
    """
    Visszaadja az időablakban lévő riasztások számát.

    LLEN: a lista hosszát adja vissza egyetlen Redis round-trip alatt,
    anélkül hogy az összes elemet le kellene tölteni.
    Az engine döntési logikájában hasznos: ha size == 1, az első
    riasztás érkezett (ablak most nyílt meg).
    """
    r = await get_redis()
    return await r.llen(WINDOW_KEY)


async def clear_window() -> None:
    """
    Manuálisan törli az időablakot.

    Normál működésben a TTL automatikusan elvégzi ezt – de a
    DELETE /api/v1/incidents/reset végpont expliciten meghívja,
    hogy a következő tesztfuttatás előtt tiszta lappal lehessen kezdeni.
    """
    r = await get_redis()
    await r.delete(WINDOW_KEY)
