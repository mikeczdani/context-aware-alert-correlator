"""
app/main.py – FastAPI alkalmazás példányosítás és routerek bekötése

Ez a fájl a backend belépési pontja. Három dolgot csinál:
  1. Létrehozza a FastAPI app példányt
  2. Regisztrálja a három router-t (webhook, stream, dashboard)
  3. Kezeli az alkalmazás életciklusát (startup / shutdown):
     - startup:  Redis kapcsolat inicializálása
     - shutdown: Redis kapcsolat lezárása

Indítás:
  cd backend/
  uvicorn app.main:app --reload

Swagger UI (interaktív API dokumentáció):
  http://localhost:8000/docs

ReDoc (olvasható API dokumentáció):
  http://localhost:8000/redoc
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.dashboard_routes import router as dashboard_router
from app.api.stream_routes import router as stream_router
from app.api.webhook_routes import router as webhook_router
from app.storage.redis_client import close_redis, get_redis

# ---------------------------------------------------------------------------
# Logging konfiguráció
# ---------------------------------------------------------------------------

# Alap logging beállítás – az uvicorn saját loggere is fut mellette
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alkalmazás életciklus (lifespan)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Az alkalmazás startup és shutdown eseményeit kezeli.

    Miért lifespan és nem @app.on_event("startup")?
      A FastAPI ajánlott modern megközelítése a lifespan context manager,
      mert egyetlen helyen látható az indítás és leállítás logikája,
      és a @app.on_event decorator deprecated lett a FastAPI 0.93+ verzióban.

    Startup:
      Inicializálja a Redis kapcsolatot egy teszt ping-gel, hogy az alkalmazás
      induláskor azonnal kiderüljön, ha a Redis nem elérhető – ne csak az első
      kérés feldolgozásakor derüljön ki.

    Shutdown:
      Lezárja a Redis kapcsolatot, hogy ne maradjanak nyitott socketek
      az alkalmazás leállása után.
    """
    # --- Startup ---
    logger.info("Starting up – connecting to Redis...")
    try:
        r = await get_redis()
        await r.ping()
        logger.info("Redis connection established successfully.")
    except Exception as e:
        # Ha a Redis nem érhető el induláskor, csak warningot logolunk –
        # nem állítjuk le az alkalmazást, mert fejlesztés közben előfordulhat,
        # hogy a Redis konténer még nem indult el teljesen
        logger.warning("Redis connection failed at startup: %s", e)

    yield  # Az alkalmazás fut

    # --- Shutdown ---
    logger.info("Shutting down – closing Redis connection...")
    await close_redis()
    logger.info("Redis connection closed.")


# ---------------------------------------------------------------------------
# FastAPI alkalmazás példányosítás
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Context-Aware Alert Correlator",
    description=(
        "Kontextusalapú riasztáskorrelációs és zajszűrő rendszer mikroszolgáltatás-alapú "
        "környezetben. Kiali topológiagráf alapú gyökérok-azonosítás (RCA), "
        "Redis TTL korrelációs ablak, és valós idejű SSE értesítés."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # A Swagger UI és ReDoc automatikusan elérhető ezekkel az URL-ekkel
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

# A React frontend (localhost:3000) más originen fut mint a backend (localhost:8000),
# ezért CORS fejlécek szükségesek, különben a böngésző blokkolja a kéréseket.
# Fejlesztési környezetben az összes origint engedélyezzük – production-ban
# ezt szűkíteni kell a frontend pontos URL-jére.
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["http://localhost:3000"] – production-ben
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # SSE-hez szükséges: a böngésző a text/event-stream választ csak akkor
    # fogadja el cross-origin-ből, ha az expose_headers tartalmazza ezeket
    expose_headers=["Content-Type", "Cache-Control"],
)


# ---------------------------------------------------------------------------
# Routerek regisztrálása
# ---------------------------------------------------------------------------

# Webhook: POST /api/v1/webhook/alerts
app.include_router(webhook_router)

# SSE stream: GET /api/v1/stream
app.include_router(stream_router)

# Dashboard: GET /incidents, /topology/current, /metrics/summary
#            POST /simulate/{scenario_id}
#            DELETE /incidents/reset
app.include_router(dashboard_router)


# ---------------------------------------------------------------------------
# Health check végpont
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"], summary="Alkalmazás állapot ellenőrzés")
async def health_check() -> dict:
    """
    Egyszerű health check végpont.

    Ellenőrzi, hogy a Redis kapcsolat él-e.
    Docker Compose healthcheck és load balancer probe-ok számára hasznos.

    Visszatér:
      200 OK: { "status": "ok", "redis": "ok" }
      200 OK: { "status": "degraded", "redis": "unavailable" } – ha Redis nem él
    """
    try:
        r = await get_redis()
        await r.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unavailable"

    return {
        "status": "ok" if redis_status == "ok" else "degraded",
        "redis": redis_status,
        "version": "1.0.0",
    }
