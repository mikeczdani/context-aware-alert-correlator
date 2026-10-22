"""
core/config.py – Alkalmazás-beállítások (Pydantic Settings)

Ez a fájl tartalmazza az összes környezeti változót, amelyeket a backend modulok
használnak. A Pydantic BaseSettings automatikusan beolvassa a .env fájlból
vagy a shell environment változókból, és validálja a típusokat.

Miért Pydantic Settings és nem sima os.getenv()?
  1. Típusvalidáció: ha REDIS_URL-t megkapjuk, de üres string, hibát dob induláskor.
  2. Egy helyen van minden beállítás – nem kell minden fájlban os.getenv()-et írni.
  3. A DataMode enum garantálja, hogy csak "MOCK" vagy "LIVE" értéket fogad el,
     elírás esetén azonnal ValueError-t dob, nem fut el hibásan.

Használat más modulokban:
  from app.core.config import settings
  settings.REDIS_URL
  settings.DATA_MODE
  settings.KIALI_URL
"""

from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings


class DataMode(str, Enum):
    """
    A rendszer két működési módja.

    MOCK:
      A collectors/ réteg a backend/scenarios/ mappából tölti be
      az előre elkészített Kiali JSON gráfokat és K8s kontextusokat.
      Nem kell futó Kubernetes kluszter – determinisztikus, reprodukálható
      tesztek a szakdolgozati mérésekhez.

    LIVE:
      A collectors/ réteg a valódi Kiali REST API-t hívja (KIALI_URL),
      és a valódi Kubernetes API-t kérdezi le (k8s_collector.py).
      Ehhez futó k3d / Minikube kluszter kell Istio + Kiali + Prometheus-szal.
    """

    MOCK = "MOCK"
    LIVE = "LIVE"


class Settings(BaseSettings):
    """
    Az összes konfigurálható beállítás egy helyen.

    Az értékek betöltési sorrendje (az utóbbi felülírja az előzőt):
      1. Alapértelmezett értékek (itt definiálva)
      2. .env fájl (ha létezik a projekt gyökérben)
      3. Shell environment változók (pl. Docker Compose environment: szekció)

    Mezők:
      REDIS_URL:        A Redis kapcsolati string.
                        Lokálisan: redis://localhost:6379
                        Docker Compose-ban: redis://redis:6379
                        (a "redis" a compose service neve)

      DATA_MODE:        MOCK vagy LIVE – melyik forrásból gyűjtsük az adatokat.
                        Alapértelmezett: MOCK (nincs szükség K8s kluszterre).

      KIALI_URL:        A Kiali API alap URL-je LIVE módban.
                        Pl. http://localhost:20001 (kubectl port-forward után)
                        vagy http://kiali.istio-system.svc:20001 (kluszteren belül).
                        MOCK módban nem használjuk.

      KIALI_NAMESPACE:  Melyik Kubernetes névtérből kérjük le a Kiali gráfot.
                        A Bookinfo demo alkalmazás alapértelmezetten a "default"
                        névtérben fut.

      SCENARIOS_DIR:    A scenarios/ mappa elérési útja.
                        Alapértelmezetten a backend/ mappához képest relatív,
                        de Docker-ben felülírható abszolút útvonallal.

      WINDOW_TTL:       A korrelációs időablak hossza másodpercben.
                        A window_store.py ezt használja az EXPIRE parancshoz.
                        Alapértelmezett: 60 mp (a markdown leírása szerint).

      SCALE_NOISE_WINDOW_SECONDS:
                        Hány másodpercen belüli ScalingReplicaSet esemény
                        minősül "közelmúltbeli skálázódásnak" a noise_filter-ben.
                        Alapértelmezett: 90 mp (a markdown leírása szerint: Δt ≤ 90s).
    """

    REDIS_URL: str = "redis://localhost:6379"
    DATA_MODE: DataMode = DataMode.MOCK
    KIALI_URL: str = "http://localhost:20001"
    KIALI_NAMESPACE: str = "default"
    SCENARIOS_DIR: str = "scenarios"
    WINDOW_TTL: int = 60
    SCALE_NOISE_WINDOW_SECONDS: int = 90

    model_config = {
        # A .env fájlt a backend/ mappában keresi (ahol az uvicorn indul)
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # A beállítások nem változtathatók futás közben (immutable config)
        "frozen": True,
    }


# Globális singleton – innen importálják az összes többi modul
# Pl.: from app.core.config import settings
settings = Settings()
