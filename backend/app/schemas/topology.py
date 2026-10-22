"""
schemas/topology.py – Kiali topológia és Kubernetes kontextus modellek (2–3. Réteg)

Ez a fájl két különböző forrás adatstruktúráit tartalmazza:
  1. Kiali REST API → KialiNode, KialiEdge, KialiGraph
     A hálózati topológiát és a forgalmi metrikákat írja le.
  2. Kubernetes API → K8sContext
     Egy-egy pod / deployment életciklus-állapotát írja le.

A két forrás adatait a collectors/ réteg gyűjti össze,
és az engine/ réteg kombinálja a zajszűréshez és az RCA-hoz.
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Kiali gráf modellek
# ---------------------------------------------------------------------------

class KialiNode(BaseModel):
    """
    Egyetlen mikroszolgáltatás-csomópont a Kiali Service Graph API válaszából.

    A Kiali a GET /api/namespaces/{namespace}/graph végponton adja vissza
    az összes szolgáltatás csomópontját és az éleket. Ebből csak azokat
    a mezőket vesszük át, amelyeket a zajszűrő és az RCA algoritmus használ.

    Mezők:
      id:               A Kiali belső node azonosítója.
                        Az élekben (KialiEdge.source / .target) erre hivatkozunk.
      service:          A mikroszolgáltatás neve (pl. "payment", "db").
                        Ez a közös kulcs, amellyel a RawAlert és a K8sContext
                        összeköthető: mindhárom modellben ugyanez a string szerepel.
      namespace:        Kubernetes névtér.
      response_time_ms: Átlagos válaszidő milliszekundumban a Kiali által mért
                        forgalom alapján. Ha ez az érték magas (pl. > 500 ms),
                        a severity_scorer magasabb pontszámot ad.
      http_error_rate:  HTTP 5xx hibaarány 0.0–1.0 között.
                        Pl. 0.15 = az összes kérés 15%-a hibás választ kapott.
                        Az 5%-os küszöb felett a noise_filter vizsgálja meg,
                        hogy ez valódi hiba-e, vagy K8s esemény mellékhatása.
      traffic_rate_rps: Bejövő kérések száma másodpercenként (requests/sec).
                        A severity_scorer TrafficShare komponenséhez szükséges:
                        minél több forgalom megy át egy csomóponton, annál
                        kritikusabb, ha meghibásodik.
    """

    id: str
    service: str
    namespace: str = "default"
    response_time_ms: float = 0.0
    http_error_rate: float = 0.0
    traffic_rate_rps: float = 0.0


class KialiEdge(BaseModel):
    """
    Egy irányított hívási él két mikroszolgáltatás között.

    Az él source → target irányban értendő: a source hívja a target-et.
    Pl. "order" → "payment" azt jelenti, hogy az order service HTTP kérést
    küld a payment service-nek.

    Ezekből az élekből épül fel a NetworkX DiGraph a graph_correlator.py-ban.
    Az élirányok döntik el, hogy az RCA algoritmus melyik csomópontot jelöli
    ROOT_CAUSE-nak (a lánc legvégén, downstream pozícióban lévő hibás csomópont).

    Mezők:
      id:               Egyedi él-azonosító (a Kiali JSON-ből).
      source:           A hívó csomópont node ID-ja (KialiNode.id).
      target:           A hívott csomópont node ID-ja (KialiNode.id).
      traffic_rate_rps: Az élen átmenő forgalom – opcionálisan felhasználható
                        az él súlyozásához a gráf-bejárásnál.
    """

    id: str
    source: str
    target: str
    traffic_rate_rps: float = 0.0


class KialiGraph(BaseModel):
    """
    A Kiali REST API által visszaadott teljes topológia-pillanatkép.

    Ez a modell töltődik be a scenarios/*.json fájlokból MOCK módban,
    és ez kerül a Redis topology:current kulcsba minden feldolgozási ciklus után.
    A GET /api/v1/topology/current végpont ezt adja vissza a React Flow-nak.

    Mezők:
      namespace:  Az érintett Kubernetes névtér neve.
      timestamp:  Mikor kérdeztük le vagy töltöttük be a gráfot.
                  Az incidensekhez mentett topology_snapshot-ban fontos az
                  utólagos elemzhetőséghez.
      nodes:      Az összes mikroszolgáltatás-csomópont listája.
      edges:      Az összes irányított hívási él listája.
    """

    namespace: str = "default"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    nodes: List[KialiNode] = []
    edges: List[KialiEdge] = []


# ---------------------------------------------------------------------------
# Kubernetes konténer-állapot
# ---------------------------------------------------------------------------

class K8sContext(BaseModel):
    """
    Egy Kubernetes deployment / pod aktuális életciklus-állapota.

    A noise_filter.py ezt az objektumot vizsgálja meg annak eldöntéséhez,
    hogy egy riasztás valódi hibát jelez-e, vagy csak egy tervezett
    infrastruktúra-esemény (HPA skálázódás, Rolling Update, crashloop) mellékhatása.

    A két zajszűrési szabály:
      1. Pod-újraindulás zaj:  restarts_last_minute > 0
         → Az Envoy proxy újraindulás közben átmenetileg 503-as hibákat ad.
         → Státusz: SUPPRESSED_RESTART
      2. Skálázódási zaj:      is_scaling == True
                               VAGY last_scale_event_age_seconds <= 90
         → A HPA új podokat indít, amelyek 10–30 másodpercig nem ready-ek,
           közben az Envoy proxy kapcsolati hibákat adhat.
         → Státusz: SUPPRESSED_SCALING

    Mezők:
      service:                    A deployment neve – megegyezik a
                                  KialiNode.service és RawAlert.service értékével.
                                  Ez a közös kulcs a három forrás összeköteséhez.
      namespace:                  Kubernetes névtér.
      desired_replicas:           Hány podot szeretne futtatni a Deployment/HPA.
      ready_replicas:             Hány pod áll ténylegesen készen (readinessProbe OK).
                                  Ha desired != ready, skálázódás vagy crashloop zajlik.
      restarts_last_minute:       Hány pod-újraindulás történt az elmúlt 60 másodpercben.
                                  Ha > 0, a hálózati hiba ehhez köthető, nem valódi incidens.
      is_scaling:                 True, ha desired != ready, vagy ha az elmúlt
                                  Δt=90s-ban nőtt a replikaszám (HPA ScaleUp esemény).
                                  A k8s_collector.py állítja be ezt a jelzőt.
      last_scale_event_age_seconds: Hány másodperce volt az utolsó ScalingReplicaSet
                                  Kubernetes esemény. 999.0 = nem volt közelmúltbeli
                                  skálázódás (alapértelmezett "nincs esemény" érték).
    """

    service: str
    namespace: str = "default"
    desired_replicas: int = 1
    ready_replicas: int = 1
    restarts_last_minute: int = 0
    is_scaling: bool = False
    last_scale_event_age_seconds: float = 999.0
