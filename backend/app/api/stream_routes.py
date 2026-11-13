"""
api/stream_routes.py – SSE (Server-Sent Events) stream végpont (2. Réteg)

Ez a fájl valósítja meg a backend → React irányú valós idejű értesítési csatornát.

Miért SSE és nem WebSocket?
  A kommunikáció egyirányú: a backend értesíti a frontendet, hogy új incidens
  keletkezett. A React nem küld adatot vissza ezen a csatornán – csak figyel.
  WebSocket kétirányú csatornát nyit, ami felesleges komplexitást jelent.
  Az SSE szabványos HTTP kapcsolaton fut, tűzfalbarátos, és a böngésző natív
  EventSource API-ja automatikusan újracsatlakozik, ha a kapcsolat megszakad.

Hogyan működik a teljes SSE pipeline?
  1. A React frontend megnyitja az EventSource-t: new EventSource("/api/v1/stream")
  2. A FastAPI StreamingResponse nyitva tartja a HTTP kapcsolatot (chunked transfer)
  3. A subscribe_to_updates() async generátor feliratkozik a Redis Pub/Sub csatornára
  4. Amikor a webhook_routes.py feldolgoz egy riasztást, az incident_store.py
     PUBLISH "new_incident"-et küld a channel:sse_updates csatornára
  5. A subscribe_to_updates() generátor megkapja az üzenetet és yieldi
  6. A StreamingResponse elküldi az SSE event: update üzenetet a frontendnek
  7. A React EventSource tüzel, a handler meghívja queryClient.invalidateQueries()
  8. A TanStack Query újralekérdezi a /incidents és /metrics végpontokat

URL: GET /api/v1/stream
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.storage.redis_client import subscribe_to_updates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Stream"])

# SSE keepalive intervallum másodpercben.
# Ha hosszabb ideig nem érkezik Pub/Sub üzenet, a böngésző timeout-olhatja
# a kapcsolatot. A keepalive comment (": keepalive\n\n") megtartja a kapcsolatot
# anélkül, hogy a React EventSource eseményt tüzelne rá.
KEEPALIVE_INTERVAL_SECONDS = 15


@router.get(
    "/stream",
    summary="SSE valós idejű értesítési stream",
    description=(
        "Nyitott HTTP stream (text/event-stream). "
        "Csak akkor küld 'event: update' üzenetet, amikor a backend "
        "új incidenst dolgozott fel. A React frontend ezt az EventSource "
        "API-val figyeli, és invalidálja a TanStack Query cache-t."
    ),
    response_class=StreamingResponse,
)
async def sse_stream() -> StreamingResponse:
    """
    Az SSE stream végpont.

    Visszatér egy StreamingResponse-szal, amely nyitva tartja a HTTP kapcsolatot
    és folyamatosan küldi az SSE formátumú üzeneteket.

    SSE üzenet formátum (W3C szabvány):
      event: update\n
      data: new_incident\n
      \n
    A kettős újsor (\n\n) jelzi az üzenet végét – a böngésző addig pufferolja
    a beérkező bájtokat, amíg ezt nem látja.

    A StreamingResponse media_type="text/event-stream" beállítása szükséges,
    hogy a böngésző SSE streamként kezelje a választ, ne sima szövegként.
    """

    async def event_generator():
        """
        Async generátor, amely az SSE üzeneteket állítja elő.

        Két forrásból kaphat üzeneteket:
          1. Redis Pub/Sub (subscribe_to_updates): valódi incidens esemény
          2. asyncio timeout (KEEPALIVE_INTERVAL_SECONDS): keepalive ping

        Az asyncio.wait_for() + TimeoutError kombináció valósítja meg a
        keepalive mechanizmust: ha KEEPALIVE_INTERVAL_SECONDS másodpercig
        nem érkezik Pub/Sub üzenet, egy keepalive comment-et küldünk,
        hogy a böngésző ne bontsa a kapcsolatot.
        """
        logger.info("SSE client connected – subscribing to Redis Pub/Sub")

        try:
            async for message in subscribe_to_updates():
                # Valódi incidens esemény érkezett a Redis Pub/Sub-ból
                # SSE formátum: "event: update\ndata: {payload}\n\n"
                logger.debug("SSE event dispatched: %s", message)
                yield f"event: update\ndata: {message}\n\n"

        except asyncio.CancelledError:
            # A frontend lecsatlakozott (böngésző bezárva, navigáció)
            # Ez normális – nem logoljuk hibának
            logger.info("SSE client disconnected")
        except Exception as e:
            # Váratlan hiba (pl. Redis kapcsolat megszakadt)
            # Elküldjük a hibát SSE error eventként, hogy a frontend tudjon róla
            logger.error("SSE stream error: %s", e)
            yield f"event: error\ndata: {str(e)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Cache-Control: no-cache szükséges az SSE szabványhoz –
            # a proxy szerverek ne puffereozzák az üzeneteket
            "Cache-Control": "no-cache",
            # X-Accel-Buffering: nginx-specifikus header, letiltja a nginx
            # pufferelését, amely egyébként késleltetheti az SSE üzeneteket
            "X-Accel-Buffering": "no",
            # Connection: keep-alive fenntartja a HTTP kapcsolatot
            "Connection": "keep-alive",
        },
    )
