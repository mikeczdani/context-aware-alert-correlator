"""
schemas/incident.py – Kimeneti incidens és metrika modellek (3–4. Réteg)

Ez a fájl a rendszer kimeneti adatstruktúráit tartalmazza:
  - NodeStatus / IncidentStatus: enum-ok az állapotok egységes jelöléséhez
  - AffectedNode: egy érintett csomópont az incidensen belül
  - CorrelatedIncident: a motor teljes kimenete, ez kerül a Redis-be
  - SystemMetrics: az ARR % és a többi KPI kártya adatai

Adatfolyam:
  engine/ (graph_correlator + noise_filter + severity_scorer)
    → CorrelatedIncident objektum
      → storage/incident_store.py (Redis: incident:{id})
        → GET /api/v1/incidents (dashboard_routes.py)
          → React IncidentFeed + ArrStatsCards
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerációk
# ---------------------------------------------------------------------------

class NodeStatus(str, Enum):
    """
    Egy gráf-csomópont lehetséges állapotai a korrelációs motor kiértékelése után.

    Ezek az értékek kerülnek a Redis-be és a React Flow csomópontjaira,
    amelyek alapján a frontend színezi a gráfot:
      HEALTHY           → zöld csomópont
      ROOT_CAUSE        → piros, pulzáló csomópont
      SYMPTOM           → sárga csomópont
      SUPPRESSED_SCALING / SUPPRESSED_RESTART → kék/szürke szaggatott csomópont

    Értékek:
      HEALTHY:             Nincs aktív riasztás, normális működés.
      ROOT_CAUSE:          A hívási lánc legalsó hibás eleme – ez okozza a többi hibát.
                           A graph_correlator.py jelöli meg NetworkX bejárással.
      SYMPTOM:             Következményes hiba – egy upstream szolgáltatás hibájára reagál.
                           Ha a ROOT_CAUSE meggyógyul, ez is megszűnik.
      SUPPRESSED:          Általános elnyomás (pl. rövid Envoy inicializálási hiba,
                           amely nem köthető skálázódáshoz vagy újrainduláshoz).
      SUPPRESSED_SCALING:  A pod HPA miatt skálázódik (desired != ready),
                           az átmeneti hiba várható és nem valódi incidens.
      SUPPRESSED_RESTART:  A pod az elmúlt percben újraindult (restarts_last_minute > 0),
                           az átmeneti 503-as hiba ehhez köthető.
    """

    HEALTHY = "HEALTHY"
    ROOT_CAUSE = "ROOT_CAUSE"
    SYMPTOM = "SYMPTOM"
    SUPPRESSED = "SUPPRESSED"
    SUPPRESSED_SCALING = "SUPPRESSED_SCALING"
    SUPPRESSED_RESTART = "SUPPRESSED_RESTART"


class IncidentStatus(str, Enum):
    """
    Egy korrelált incidens életciklus-állapota.

    Értékek:
      OPEN:       Aktív, nyitott incidens – a ROOT_CAUSE még fennáll.
      SUPPRESSED: Az összes érintett riasztás zajnak bizonyult,
                  valódi incidens nem keletkezett (pl. minden alert SUPPRESSED_SCALING).
      RESOLVED:   A hiba megszűnt. Jövőbeli bővítési pont: az Alertmanager
                  "resolved" webhook eseményére állítja be a rendszer.
    """

    OPEN = "OPEN"
    SUPPRESSED = "SUPPRESSED"
    RESOLVED = "RESOLVED"


# ---------------------------------------------------------------------------
# Incidens részlet modellek
# ---------------------------------------------------------------------------

class AffectedNode(BaseModel):
    """
    Egyetlen érintett szolgáltatás-csomópont egy incidensen belül.

    A CorrelatedIncident.affected_nodes listájában szerepel minden olyan
    csomópont, amelyre riasztás érkezett az időablakon belül.

    Mezők:
      service:            A szolgáltatás neve (pl. "payment").
      status:             A NodeStatus enum értéke – a frontend ez alapján
                          színezi a React Flow csomópontot.
      http_error_rate:    Hibaarány a Kiali gráfból – az IncidentFeed kártyán
                          jelenik meg a szöveges indoklás mellett.
      response_time_ms:   Válaszidő a Kiali gráfból (ms-ban).
      severity_score:     A severity_scorer.py által számított prioritáspontszám.
                          Képlete: w1*ErrorRate + w2*PageRank(v) + w3*TrafficShare(v)
                          Minél magasabb, annál kritikusabb a csomópont.
      suppression_reason: Ha a csomópont SUPPRESSED_* státuszú, itt szerepel
                          szövegesen az ok. Ez jelenik meg az IncidentFeed
                          lenyitható "Indoklás" szekciójában.
                          Pl. "Pod restarted 3x in last 60s – transient Envoy 503"
    """

    service: str
    status: NodeStatus
    http_error_rate: float = 0.0
    response_time_ms: float = 0.0
    severity_score: float = 0.0
    suppression_reason: Optional[str] = None


class CorrelatedIncident(BaseModel):
    """
    Egyetlen korrelált (csoportosított és kiértékelt) incidens bejegyzés.

    Ez a rendszer legfontosabb kimeneti adatstruktúrája – minden feldolgozási
    ciklus eredménye egy ilyen objektum. A storage/incident_store.py menti el
    Redis-be, a GET /api/v1/incidents végpont adja vissza a frontendnek.

    Redis kulcsok, ahol ez az objektum megjelenik:
      - incident:{id}          → a teljes JSON dokumentum (lejárat nélkül, AOF mentés)
      - incidents:timeline     → csak az id szerepel itt, az időrendi sorhoz

    Mezők:
      id:                  UUID – egyben a Redis kulcs utolsó tagja: incident:{id}.
                           A uuid4() garantálja az egyediséget több párhuzamos
                           feldolgozás esetén is.
      created_at:          Az incidens keletkezésének időpontja (UTC).
      status:              OPEN / SUPPRESSED / RESOLVED (IncidentStatus enum).
      root_cause_service:  Annak a szolgáltatásnak a neve, amelyet a
                           graph_correlator.py a hívási lánc legalsó hibás
                           elemeként azonosított. None, ha az összes riasztás
                           zajnak minősült (minden node SUPPRESSED_*).
      affected_nodes:      Az összes érintett csomópont részletes listája
                           (AffectedNode objektumok, státusszal és indoklással).
      raw_alert_count:     Hány nyers Prometheus riasztásból született ez az incidens.
                           Az ARR % számításhoz kell.
      suppressed_count:    Ebből hány riasztás lett zajként elnyomva.
      correlated_count:    Hány riasztás lett valódi incidenssé korrelálva
                           (raw_alert_count - suppressed_count).
      summary:             Emberi olvasásra szánt, automatikusan generált
                           szöveges összefoglaló. Pl.:
                           "db caused cascading latency in payment, inventory, order"
      raw_alerts:          Az eredeti nyers riasztások listája dict formában.
                           Auditálhatóság és a szakdolgozati ábrák miatt tároljuk.
      topology_snapshot:   A Kiali gráf pillanatképe dict formában az incidens
                           keletkezésekor. Utólag visszajátszható, mi volt a
                           topológia állapota a hiba pillanatában.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: IncidentStatus = IncidentStatus.OPEN
    root_cause_service: Optional[str] = None
    affected_nodes: List[AffectedNode] = []
    raw_alert_count: int = 0
    suppressed_count: int = 0
    correlated_count: int = 0
    summary: str = ""
    raw_alerts: List[Dict[str, Any]] = []
    topology_snapshot: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Validációs metrikák – GET /api/v1/metrics/summary válasza
# ---------------------------------------------------------------------------

class SystemMetrics(BaseModel):
    """
    A Redis stats:metrics Hash-ből kiolvasott összesített rendszerstatisztikák.

    Ez jelenik meg a React ArrStatsCards komponens 4 kártyáján.
    Az incident_store.py get_metrics() függvénye állítja össze.

    Mezők:
      raw_alerts_total:          Az összes beérkezett nyers Prometheus riasztás száma
                                 (HINCRBY raw_alerts_total).
      suppressed_total:          Ebből hány lett zajként elnyomva
                                 (HINCRBY suppressed_total).
      correlated_incidents_total: Hány valódi, korrelált incidens keletkezett
                                 (HINCRBY correlated_incidents_total).
      alert_reduction_rate_pct:  ARR % – a szakdolgozat fő mérőszáma.
                                 Képlete: (1 - incidensek / nyers_riasztások) * 100
                                 Ha raw_alerts_total == 0, az értéke 0.0
                                 (nullával való osztás elkerülése).
    """

    raw_alerts_total: int = 0
    suppressed_total: int = 0
    correlated_incidents_total: int = 0
    alert_reduction_rate_pct: float = 0.0
