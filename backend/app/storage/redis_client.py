"""
storage/redis_client.py – Async Redis kapcsolat és Pub/Sub kezelő (4. Réteg)

Ez a fájl két dolgot csinál:
  1. Biztosítja a globális async Redis klienst a többi storage modul számára.
     (window_store.py és incident_store.py innen importálja a get_redis()-t.)
  2. Kezeli a Redis Pub/Sub csatornát, amelyen az SSE stream értesítést kap,
     ha új incidens keletkezett.

Miért async Redis?
  A FastAPI aszinkron (asyncio) alapú. Ha szinkron Redis hívásokat használnánk,
  az event loop blokkolódna minden Redis műveletnél – ez különösen az SSE
  stream végpontot rontaná, ahol egyidejűleg kell figyelni az eseményeket
  és kiszolgálni a webhook kéréseket.
  A redis.asyncio csomag natívan asyncio-kompatibilis, blokkolás nélkül működik.

Kapcsolódás:
  A REDIS_URL-t a core/config.py-ból olvassuk.
  Alapértelmezett: redis://localhost:6379 (lokális fejlesztés)
  Docker Compose-ban: redis://redis:6379 (a redis service neve a compose-ban)
"""

from __future__ import annotations

from typing import AsyncGenerator, Optional

import redis.asyncio as aioredis

from app.core.config import settings

# ---------------------------------------------------------------------------
# Globális Redis kliens singleton
# ---------------------------------------------------------------------------

# A kliens globális változóként él – a FastAPI startup eventjén hozzuk létre,
# így nem nyitunk új kapcsolatot minden egyes beérkező kérésnél.
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """
    Visszaadja a globális async Redis klienst.
    Ha még nem létezik (első hívás), automatikusan létrehozza.

    decode_responses=True: a Redis bináris byte válaszát automatikusan str-ré
    alakítja, így a window_store.py és az incident_store.py nem kénytelen
    mindenhol .decode("utf-8")-t hívni.
    """
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    """
    Lezárja a Redis kapcsolatot.

    A FastAPI shutdown eventjén hívjuk meg (app/main.py),
    hogy ne maradjon nyitva kapcsolat az alkalmazás leállása után.
    """
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ---------------------------------------------------------------------------
# Pub/Sub – SSE értesítési csatorna
# ---------------------------------------------------------------------------

# A csatorna neve, amelyre a stream_routes.py SSE végpontja feliratkozik,
# és amelyre a incident_store.py PUBLISH parancsot küld új incidens esetén.
SSE_CHANNEL = "channel:sse_updates"


async def publish_update(message: str = "update") -> None:
    """
    Üzenetet küld a Redis Pub/Sub csatornára.

    Ezt a függvényt az incident_store.py hívja meg, miután elmentett
    egy új CorrelatedIncident objektumot a Redis-be.

    Hogyan kapcsolódik az SSE-hez?
      PUBLISH → channel:sse_updates → subscribe_to_updates() generátor
        → stream_routes.py SSE végpont
          → React EventSource
            → queryClient.invalidateQueries()
              → TanStack Query újralekérdezi az /incidents és /metrics végpontokat

    Az üzenet tartalma itt nem fontos (csak a "van új adat" jelzés számít),
    de stringnek kell lennie, mert a Redis Pub/Sub csak stringet tud kezelni
    decode_responses=True módban.
    """
    r = await get_redis()
    await r.publish(SSE_CHANNEL, message)


async def subscribe_to_updates() -> AsyncGenerator[str, None]:
    """
    Feliratkozik a Redis Pub/Sub csatornára, és async generátorként adja vissza
    az érkező üzeneteket a stream_routes.py SSE végpontjának.

    Miért kell külön Redis kapcsolat a Pub/Sub-hoz?
      A Redis subscribe() parancs után a kapcsolat "feliratkozási módba" kerül:
      innentől csak Pub/Sub parancsokat fogad el (SUBSCRIBE, UNSUBSCRIBE, PING).
      Ha ugyanazt a kapcsolatot használnánk a window_store és incident_store
      hívásaihoz is, azok "WRONGTYPE" hibát dobnának. Ezért itt mindig
      új, dedikált kapcsolatot nyitunk.

    Használat a stream_routes.py-ban:
      async for message in subscribe_to_updates():
          yield f"event: update\ndata: {message}\n\n"

    A finally blokk garantálja, hogy a kapcsolat lezárul, ha a frontend
    lecsatlakozik (pl. böngésző bezárása, navigálás más oldalra).
    """
    # Dedikált kapcsolat CSAK a Pub/Sub-hoz
    pubsub_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = pubsub_client.pubsub()
    await pubsub.subscribe(SSE_CHANNEL)

    try:
        async for message in pubsub.listen():
            # A listen() kétféle üzenetet küld vissza:
            #   "subscribe" típus: a feliratkozás visszaigazolása – ezt kihagyjuk
            #   "message" típus:   tényleges Pub/Sub adat – ezt továbbítjuk az SSE-nek
            if message["type"] == "message":
                yield message["data"]
    finally:
        # Leiratkozás és kapcsolat lezárása, amikor a frontend lecsatlakozik
        await pubsub.unsubscribe(SSE_CHANNEL)
        await pubsub_client.aclose()
