# Szakdolgozati Rendszerarchitektúra és Megvalósítási Terv
**Téma:** Kontextusalapú hibafelismerő és riasztásértékelő rendszer mikroszolgáltatás-alapú környezetben (Kiali API, Python FastAPI, Redis, React, SSE)

---

## 1. Rendszerarchitektúra Áttekintés (5 Logikai Réteg)

A rendszer egy **eseményvezérelt (Event-Driven), 5 rétegű architektúrára** épül. A cél a mikroszolgáltatásokban keletkező riasztási zaj (pl. konténer-újraindulás, horizontális skálázódás) kiszűrése, valamint a lépcsőzetesen tovagyűrűző hibák (cascading failures) gráf-alapú korrelációja és gyökérok-azonosítása (Root Cause Analysis – RCA).

```
[1. Infrastruktúra / Szimulátor] 
       │  (HTTP POST Webhook + REST Pull: Kiali Gráf & K8s állapot)
       ▼
[2. Adatbegyűjtő és API Réteg (FastAPI Routers & Collectors)]
       │  (Normalizált Pydantic modellek: Alert + TopologyGraph)
       ▼
[3. Kontextusalapú Elemző és Korrelációs Réteg (NetworkX Engine)]
       │  (Olvasás / Írás / TTL Időablak + Pub/Sub)
       ▼
[4. Egységesített Redis Adattárolási Réteg (TTL Window + AOF NoSQL DB)]
       │  (SSE: Server-Sent Events értesítés + REST lekérdezés)
       ▼
[5. Prezentációs Réteg (React Frontend: React Flow + TanStack Query)]
```

---

## 2. A Rétegek Felépítése: Mi mire jó és mit kell megvalósítani?

### 1. Réteg: Infrastruktúra, Telemetria és Szimulációs Réteg
* **Mire jó?** Ez a megfigyelt mikroszolgáltatás-környezet, amely a nyers riasztásokat és a hálózati topológiagráfot szolgáltatja.
* **Kettős (Hibrid) működési mód a teszteléshez:**
  1. **Szintetikus Szimulátor Mód (`MOCK`):** Python-alapú forgatókönyv-lejátszó (`simulator/chaos_runner.py`), amely előre definiált Kiali JSON gráfokat és Alertmanager webhookokat küld a backendnek (0 GB RAM igény, determinisztikus szakdolgozati mérések).
  2. **Lokális Mini-Kluszter Mód (`LIVE`):** `k3d` (Kubernetes) + `Istio` + `Kiali` + `Prometheus` + *Bookinfo* tesztalkalmazás, ahol `kubectl scale` és Istio `VirtualService` (delay/abort) szabályokkal generálunk valódi hibákat.

---

### 2. Réteg: Adatbegyűjtő és Kontextus-Aggregáló Réteg (`app/api/` és `app/collectors/`)
* **Mire jó?** Fogadja a bejövő nyers riasztásokat (Push), és riasztás esetén azonnal lekérdezi a hálózati és konténer-kontextust (Pull).
* **Mit kell megvalósítani?**
  1. **Alert Webhook Fogadó (`api/webhook_routes.py`):** Aszinkron `POST` végpont, amely átveszi az Alertmanager JSON csomagját.
  2. **Kiali Gráf Adapter (`collectors/kiali_collector.py`):** Lekéri a Kiali `/api/namespaces/graph` végpontjáról az aktuális irányított gráfot (csomópontok, élek, RPS, válaszidő, HTTP 5xx hibaarány).
  3. **Kubernetes Kontextus Adapter (`collectors/k8s_collector.py`):** Ellenőrzi az érintett Deployment/Pod állapotát (`restarts`, `readyReplicas` változás, `ScalingReplicaSet` események).

---

### 3. Réteg: Kontextusalapú Elemző és Korrelációs Réteg (`app/engine/`)
* **Mire jó?** Ez a szakdolgozat **üzleti logikája (Core Engine)**. Itt dől el, hogy egy riasztás csak üzemeltetési zaj-e, vagy egy nagyobb hiba része.
* **Mit kell megvalósítani?**
  1. **Zajszűrő Modul (`engine/noise_filter.py`):**
     * Összeveti a riasztó csomópontot a K8s állapottal.
     * *Szabály:* Ha a riasztás előtt $\Delta t \le 90\text{ mp}$-en belül skálázódás (`replica_delta > 0`) vagy tervezett pod-újraindulás történt, és a Kiali gráfon a hibaarány átmeneti, a riasztás **`SUPPRESSED` (Elnyomott zaj)** státuszt kap.
  2. **Gráf-Korrelációs és Gyökérok (RCA) Motor (`engine/graph_correlator.py`):**
     * A Kiali JSON-ből `networkx.DiGraph` irányított gráfot épít.
     * Lekéri a Redisből az aktív 60 másodperces időablakban lévő összes riasztó csomópontot.
     * Ha két vagy több riasztó szolgáltatás között él (hívási útvonal) van a gráfban, összevonja őket egyetlen **`CORRELATED` incidensbe**.
     * A hívási lánc legmélyén (leginkább *downstream* pozícióban) lévő hibás csomópontot megjelöli **`ROOT_CAUSE` (Gyökérok)**, a felette lévő hívókat pedig **`SYMPTOM` (Következmény)** címkével.
  3. **Kontextusos Súlyosság-értékelő (`engine/severity_scorer.py`):**
     * Kiszámítja az incidens prioritását a hibaarány és a csomópont gráfbeli fontossága (PageRank / fokszám) alapján.

---

### 4. Réteg: Egységesített Redis Adattárolási Réteg (`app/storage/`)
* **Mire jó?** Egyetlen pillekönnyű Redis konténer (`redis:alpine --appendonly yes`), amely három logikai szerepkört lát el külön kulcs-névterekkel:
  1. **Csúszó Korrelációs Időablak (`storage/window_store.py`):**
     * Kulcs: `window:active_alerts` (`TTL = 60 mp`). Amíg az ablak él, az újonnan befutó riasztások a meglévő incidenshez korrelálódnak.
  2. **Tartós NoSQL Dokumentumtár és Statisztikák (`storage/incident_store.py`):**
     * Kulcsok: `incident:{id}` (JSON dokumentum lejárat nélkül, benne a Kiali gráf-pillanatképpel), `incidents:timeline` (rendezett lista), és `stats:metrics` (Redis Hash atomi számlálókkal a szakdolgozati mérésekhez: `raw_alerts_total`, `suppressed_total`, `correlated_incidents_total`).
  3. **Valós idejű Eseménycsatorna (`storage/redis_client.py`):**
     * Redis Pub/Sub csatorna (`channel:sse_updates`), amely értesíti az SSE végpontot, ha új incidens született.

---

### 5. Réteg: Prezentációs Réteg (`frontend/src/`)
* **Mire jó?** Vizualizálja az operátor számára a Kiali topológiagráfot, a gyökérokokat és a zajcsökkentési metrikákat, felesleges hálózati polling nélkül.
* **Mit kell megvalósítani?**
  1. **SSE Feliratkozó Hook (`hooks/useSSEStream.ts`):** A böngésző natív `EventSource` API-jával feliratkozik a backend `/api/v1/stream` végpontjára. Csak akkor vált ki React Query adatfrissítést, amikor a backend ténylegesen jelez.
  2. **Interaktív Topológia Gráf (`components/topology/ServiceGraph.tsx`):** `React Flow` alapú gráf, ahol a gyökérok csomópont piros, a következményes csomópont sárga, az elnyomott (skálázódó) csomópont pedig kék *„Suppressed (Scale/Restart)”* jelvényt kap.
  3. **Incidens Feed és Szimulációs Vezérlő (`components/incidents/` & `simulator/`):** Megjeleníti az összevont incidenseket, és gombokat biztosít a teszt-forgatókönyvek egykattintásos elindításához.
  4. **Validációs KPI Panel (`components/metrics/ArrStatsCards.tsx`):** Élőben számolja és megjeleníti az **ARR (Alert Reduction Rate %)** mutatót a szakdolgozati kiértékeléshez.

---

## 3. API Végpontok Specifikációja (Rétegek szerint)

| Végpont (Method + URL) | Réteg / Modul | Irány | Feladat és Működés |
|---|---|---|---|
| `POST /api/v1/webhook/alerts` | 2. Réteg (`webhook_routes.py`) | Prometheus / Szimulátor $\rightarrow$ Backend | Fogadja a nyers riasztást, lekéri a Kiali gráfot, lefuttatja a 3. réteg motorját, ment a Redisbe, és jelez az SSE csatornán. |
| `GET /api/v1/stream` | 2. Réteg (`stream_routes.py`) | Backend $\rightarrow$ React (SSE) | Nyitott HTTP stream (`text/event-stream`). Csak akkor küld `"event: update"` jelet a frontendnek, amikor új riasztást dolgozott fel a backend. |
| `GET /api/v1/incidents` | 2-4. Réteg (`dashboard_routes.py`) | React $\rightarrow$ Backend | Visszaadja a Redisből a korrelált incidensek és az elnyomott zajok listáját a Kiali gráf-pillanatképekkel együtt. |
| `GET /api/v1/topology/current` | 2. Réteg (`dashboard_routes.py`) | React $\rightarrow$ Backend | Visszaadja az aktuális Kiali gráfot és a csomópontok korrelációs státuszát (`OK`, `SUPPRESSED`, `SYMPTOM`, `ROOT_CAUSE`). |
| `GET /api/v1/metrics/summary` | 4. Réteg (`dashboard_routes.py`) | React $\rightarrow$ Backend | Kiolvassa a Redis `stats:metrics` számlálóit és visszaadja a számított **ARR %** értéket. |
| `POST /api/v1/simulate/{scenario}` | 1-2. Réteg (`dashboard_routes.py`) | React $\rightarrow$ Backend | Elindít egy teszt-forgatókönyvet (pl. `pod_scaling_noise` vagy `cascading_db_fail`) a bemutatóhoz és a mérésekhez. |
| `DELETE /api/v1/incidents/reset` | 4. Réteg (`dashboard_routes.py`) | React $\rightarrow$ Backend | Nullázza a Redis időablakot és a statisztikákat egy új mérési futtatás előtt. |

---

## 4. Szerepkör-alapú Mappastruktúra (Clean Architecture)

```text
thesis-project/
├── docker-compose.yml                # 1 db Redis AOF konténer (redis:alpine --appendonly yes)
├── infra/                            # [1. RÉTEG] Opcionális K8s és Istio VirtualService YAML-ök
├── backend/
│   ├── requirements.txt              # fastapi, uvicorn, redis, networkx, httpx, pydantic, sse-starlette
│   ├── scenarios/                    # [1. RÉTEG] Szintetikus Kiali + Alert JSON forgatókönyvek
│   ├── simulator/                    # [1. RÉTEG] Hibageneráló szkript (chaos_runner.py)
│   └── app/
│       ├── main.py                   # FastAPI alkalmazás példányosítás és routerek bekötése
│       ├── core/
│       │   └── config.py             # Beállítások (REDIS_URL, KIALI_URL, DATA_MODE)
│       ├── schemas/                  # Pydantic adatmodellek
│       │   ├── alert.py              # Bejövő Webhook modellek
│       │   ├── topology.py           # Kiali Node, Edge és K8s Pod állapot modellek
│       │   └── incident.py           # Kimeneti Incidens és ARR Metrika modellek
│       ├── api/                      # [2. RÉTEG] HTTP és SSE Végpontok (Controllers)
│       │   ├── webhook_routes.py     # POST /api/v1/webhook/alerts
│       │   ├── stream_routes.py      # GET  /api/v1/stream (SSE)
│       │   └── dashboard_routes.py   # GET  /incidents, /topology, /metrics, POST /simulate
│       ├── collectors/               # [2. RÉTEG] Külső API Adapterek
│       │   ├── kiali_collector.py    # Kiali REST API kliens + Mock JSON betöltő
│       │   └── k8s_collector.py      # K8s Pod életciklus és skálázódás lekérdező
│       ├── engine/                   # [3. RÉTEG] Üzleti Logika (Zajszűrés és Gráf-Korreláció)
│       │   ├── noise_filter.py       # Pod Restart / Scale-up zajszűrő szabályok
│       │   ├── graph_correlator.py   # NetworkX DiGraph építés és Gyökérok (RCA) keresés
│       │   └── severity_scorer.py    # Gráf-centralitás alapú súlyosság számítás
│       └── storage/                  # [4. RÉTEG] Redis Adattárolási Réteg
│           ├── redis_client.py       # Async Redis kapcsolat és Pub/Sub kezelő
│           ├── window_store.py       # 60 mp-es TTL csúszó korrelációs ablak
│           └── incident_store.py     # Tartós NoSQL dokumentum-mentés és ARR számlálók
└── frontend/                         # [5. RÉTEG] React + TypeScript + Vite
    └── src/
        ├── api/incidentsApi.ts       # REST hívások és React Query kulcsok
        ├── hooks/useSSEStream.ts     # EventSource (SSE) feliratkozás hook
        ├── components/
        │   ├── topology/ServiceGraph.tsx   # React Flow interaktív gráf
        │   ├── incidents/IncidentFeed.tsx  # Korrelált incidens kártyák
        │   ├── metrics/ArrStatsCards.tsx   # ARR % és zajszűrési mutatók
        │   └── simulator/ChaosControls.tsx # Tesztforgatókönyv indító gombok
        └── pages/DashboardPage.tsx   # Fő vezérlőpult nézet
```

---

## 5. Megvalósítási Ütemterv (Timeline / Roadmap)

A fejlesztést **6 jól elkülönülő fázisban (egyenként 1–2 hetes blokkokban)** érdemes végigvinni, mert így minden lépés végén azonnal kipróbálható, működő részeredményt kapsz:

### 1. Fázis: Alapozás, Adatmodellek és Redis Réteg (1. hét)
* **Cél:** A projekt mappastruktúrájának felépítése, a Redis konténer elindítása és az adatmodellek rögzítése.
* **Feladatok:**
  1. `docker-compose.yml` létrehozása a Redis AOF konténerrel.
  2. Pydantic modellek megírása (`app/schemas/`): hogyan néz ki egy nyers riasztás, egy Kiali gráf (`nodes`, `edges`) és egy kiértékelt incidens.
  3. A `storage/window_store.py` (60 mp-es TTL ablak) és az `incident_store.py` (NoSQL mentés + ARR számlálók) megírása.

### 2. Fázis: Szintetikus Forgatókönyvek és Kontextus-begyűjtő (2. hét)
* **Cél:** Legyen min tesztelni az algoritmust anélkül, hogy órákat kellene Kubernetes-szel bajlódni.
* **Feladatok:**
  1. 3–4 darab valósághű Kiali JSON gráffájl elkészítése a `backend/scenarios/` mappába (1. Normál állapot, 2. Pod skálázódási zaj, 3. Lépcsőzetes adatbázis-lassulás, 4. Kombinált hiba).
  2. A `collectors/kiali_collector.py` és `k8s_collector.py` megírása úgy, hogy `DATA_MODE="MOCK"` esetén ezeket a JSON-öket adja vissza, `DATA_MODE="LIVE"` esetén pedig a valódi Kiali API-t hívja.

### 3. Fázis: A Korrelációs és Zajszűrő Motor – A Szakdolgozat Magja (3–4. hét)
* **Cél:** A 3. réteg (`app/engine/`) megvalósítása `NetworkX` segítségével.
* **Feladatok:**
  1. **`noise_filter.py`:** A konténer-újraindulási és replikaszám-növekedési (`ScalingReplicaSet`) zajszűrő szabály implementálása.
  2. **`graph_correlator.py`:** A Kiali JSON betöltése `nx.DiGraph`-ba, a 60 mp-es Redis ablakban lévő riasztó csomópontok összekötése, és a legalsó (*downstream*) hibás csomópont megjelölése `ROOT_CAUSE`-ként.
  3. **`severity_scorer.py`:** Kritikus pontszám számítása a csomópont fokszáma és a hibaarány alapján.

### 4. Fázis: FastAPI Végpontok, Webhook és SSE Stream (5. hét)
* **Cél:** A motor bekötése a HTTP végpontokba és a valós idejű értesítési csatorna megnyitása.
* **Feladatok:**
  1. `webhook_routes.py` (`POST /api/v1/webhook/alerts`) megírása.
  2. `stream_routes.py` (`GET /api/v1/stream`) SSE végpont megírása a Redis Pub/Sub csatornára kötve.
  3. `dashboard_routes.py` (`/incidents`, `/metrics/summary`, `/simulate/{scenario}`) elkészítése és tesztelése Swagger UI-on (`http://localhost:8000/docs`).

### 5. Fázis: React Frontend és Élő Topológia Vizualizáció (6–7. hét)
* **Cél:** A látványos operátori felület összeépítése.
* **Feladatok:**
  1. `useSSEStream.ts` hook megírása, amely az SSE jelzésre invalidálja a `TanStack Query` cache-t.
  2. `ServiceGraph.tsx` megvalósítása `React Flow`-val (a csomópontok automatikus színezése: Zöld = OK, Kék =