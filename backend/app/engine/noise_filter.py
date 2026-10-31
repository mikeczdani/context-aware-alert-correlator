"""
engine/noise_filter.py – Pod Restart / Scale-up zajszűrő szabálymotor (3. Réteg)

Ez a modul a szakdolgozat egyik kulcsalgoritmusát valósítja meg: eldönti,
hogy egy beérkező Prometheus riasztás valódi hibát jelez-e, vagy csak egy
tervezett Kubernetes infrastruktúra-esemény (HPA skálázódás, pod újraindulás)
átmeneti mellékhatása.

Miért kell zajszűrés?
  A Kubernetes dinamikusan kezeli a podok életciklusát:
    - HPA (Horizontal Pod Autoscaler): ha a forgalom megnő, új podokat indít.
      Az új podok 10–30 másodpercig nem ready-ek, közben az Istio Envoy proxy
      503-as "upstream connect error" hibákat adhat a befelé irányuló kérésekre.
    - Rolling Update / CrashloopBackoff: pod-újraindulás közben az Envoy
      proxy szintén átmeneti 503-as hibákat produkál.
  Ezek a hibák NORMÁLISAK és VÁRHATÓK – nem jeleznek valódi alkalmazáshibát.
  Ha mégis riasztást generálnak (és ez megtörténik Prometheus-szal), az üzemeltető
  "riasztási fáradtságot" (alert fatigue) él meg: rengeteg hamis riasztást kap.

A zajszűrési szabály (a markdown 3. réteg leírása alapján):
  Ha a riasztás előtt Δt ≤ 90 másodpercen belül:
    - skálázódás történt (is_scaling == True VAGY last_scale_event_age_seconds ≤ 90)
    - VAGY a pod az elmúlt 60 másodpercben újraindult (restarts_last_minute > 0)
  ÉS a Kiali gráfon a hibaarány átmeneti jellegű (http_error_rate < HARD_ERROR_THRESHOLD)
  → a riasztás SUPPRESSED státuszt kap (nem keletkezik incidens).

Kimenet:
  filter_alert() visszaad egy (NodeStatus, reason_string) tuple-t:
    - Ha a riasztás zaj: (SUPPRESSED_SCALING / SUPPRESSED_RESTART, szöveges indoklás)
    - Ha valódi hiba: (None, None) → a graph_correlator.py feldolgozza tovább
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.core.config import settings
from app.schemas.alert import RawAlert
from app.schemas.incident import NodeStatus
from app.schemas.topology import KialiNode, K8sContext

# ---------------------------------------------------------------------------
# Konfigurálható küszöbértékek
# ---------------------------------------------------------------------------

# Ha a http_error_rate meghaladja ezt az értéket, a hibát valódinak tekintjük
# még skálázódás / újraindulás esetén is – nem nyomjuk el.
# Indoklás: ha a hibaarány > 40%, az már nem magyarázható csak az Envoy
# inicializálási késleltetésével (ahol általában 5–15% a tranzitórikus hiba).
HARD_ERROR_THRESHOLD = 0.40

# Ha a hibaarány ezen érték alatt van, az szinte biztosan csak Envoy
# inicializálási zaj, nem valódi alkalmazáshiba.
TRANSIENT_ERROR_THRESHOLD = 0.05

# ---------------------------------------------------------------------------
# Fő zajszűrő függvény
# ---------------------------------------------------------------------------

def filter_alert(
    alert: RawAlert,
    node: Optional[KialiNode],
    k8s_ctx: Optional[K8sContext],
) -> Tuple[Optional[NodeStatus], Optional[str]]:
    """
    Eldönti, hogy egy riasztás valódi hibát jelez-e, vagy üzemeltetési zajt.

    Paraméterek:
      alert:   A beérkező nyers Prometheus riasztás – a service neve alapján
               kapcsoljuk össze a Kiali csomóponttal és a K8s kontextussal.
      node:    A riasztó szolgáltatás Kiali gráf-csomópontja (hibaarány, válaszidő).
               Ha a Kiali nem ismeri a service-t (None), nem tudunk zajszűrni
               → a riasztást valódinak tekintjük.
      k8s_ctx: A riasztó pod K8s állapota (restarts, is_scaling, scale event kora).
               Ha nem áll rendelkezésre (None), nem tudunk zajszűrni
               → a riasztást valódinak tekintjük.

    Visszatér: (NodeStatus, indoklás) tuple:
      - Ha zaj: (SUPPRESSED_SCALING | SUPPRESSED_RESTART, szöveges ok)
      - Ha valódi hiba (vagy nem tudunk dönteni): (None, None)

    Megjegyzés: a None visszatérési érték NEM jelent hibát – azt jelzi,
    hogy a riasztást a graph_correlator.py-nak kell tovább feldolgoznia
    (RCA + severity scoring).
    """

    # Ha nincs Kiali adat vagy K8s kontextus, nem tudunk zajszűrni
    if node is None or k8s_ctx is None:
        return None, None

    error_rate = node.http_error_rate

    # Ha a hibaarány extrém magas, valódi hiba van – nem nyomjuk el,
    # még akkor sem, ha skálázódás zajlik közben (pl. db összeomlik és
    # közben HPA is skálázódik – a db hiba valódi).
    if error_rate >= HARD_ERROR_THRESHOLD:
        return None, None

    # -------------------------------------------------------------------
    # 1. Szabály: Skálázódási zaj (SUPPRESSED_SCALING)
    # -------------------------------------------------------------------
    # Feltételek (OR kapcsolat – elég az egyik):
    #   a) is_scaling == True: a desired != ready, aktív skálázódás zajlik
    #   b) last_scale_event_age_seconds <= SCALE_NOISE_WINDOW_SECONDS (90s):
    #      nemrég volt ScalingReplicaSet esemény, az Envoy még nem stabilizálódott
    is_recent_scale = (
        k8s_ctx.is_scaling
        or k8s_ctx.last_scale_event_age_seconds <= settings.SCALE_NOISE_WINDOW_SECONDS
    )

    if is_recent_scale:
        if k8s_ctx.is_scaling:
            # Aktív skálázódás: desired és ready replikaszám eltér
            reason = (
                f"Pod scaling in progress for '{alert.service}': "
                f"desired={k8s_ctx.desired_replicas}, ready={k8s_ctx.ready_replicas}. "
                f"Transient Envoy 503 errors are expected during scale-out "
                f"(error_rate={error_rate:.1%})."
            )
        else:
            # Közelmúltbeli skálázódás: már ready, de Envoy még nem stabilizálódott
            age = int(k8s_ctx.last_scale_event_age_seconds)
            reason = (
                f"Recent ScalingReplicaSet event for '{alert.service}' "
                f"{age}s ago (threshold: {settings.SCALE_NOISE_WINDOW_SECONDS}s). "
                f"Pods are ready but Envoy proxy may still be warming up "
                f"(error_rate={error_rate:.1%})."
            )
        return NodeStatus.SUPPRESSED_SCALING, reason

    # -------------------------------------------------------------------
    # 2. Szabály: Pod újraindulási zaj (SUPPRESSED_RESTART)
    # -------------------------------------------------------------------
    # Feltétel: restarts_last_minute > 0
    #   A pod az elmúlt 60 másodpercben legalább egyszer újraindult.
    #   Újraindulás közben és után rövid ideig az Envoy proxy
    #   503-as hibákat adhat, amíg a pod ismét ready állapotba nem kerül.
    if k8s_ctx.restarts_last_minute > 0:
        restarts = k8s_ctx.restarts_last_minute
        reason = (
            f"Pod '{alert.service}' restarted {restarts}x in the last 60s. "
            f"Transient Envoy 503 errors are expected during container restart "
            f"(error_rate={error_rate:.1%}). "
            f"Monitor for CrashLoopBackOff if restarts continue."
        )
        return NodeStatus.SUPPRESSED_RESTART, reason

    # -------------------------------------------------------------------
    # Nincs zajszűrési feltétel – a riasztás valódi hibát jelezhet
    # -------------------------------------------------------------------
    # A graph_correlator.py fogja feldolgozni: megnézi, hogy más
    # szolgáltatások is riasztanak-e az időablakon belül, és ha igen,
    # RCA-val megkeresi a gyökérokot.
    return None, None


# ---------------------------------------------------------------------------
# Kötegelt zajszűrés – több riasztáshoz egyszerre
# ---------------------------------------------------------------------------

def filter_alerts_batch(
    alerts: list[RawAlert],
    node_map: dict[str, KialiNode],
    k8s_map: dict[str, K8sContext],
) -> dict[str, Tuple[Optional[NodeStatus], Optional[str]]]:
    """
    Egyszerre szűri meg az időablakon belüli összes riasztást.

    Ezt a függvényt a graph_correlator.py hívja, miután lekérte
    az összes aktív riasztást a window_store-ból.

    Paraméterek:
      alerts:    Az időablakon belüli összes nyers riasztás listája.
      node_map:  { service_name → KialiNode } – a Kiali gráf csomópontjai.
                 A kiali_collector.py állítja össze.
      k8s_map:   { service_name → K8sContext } – a K8s állapotok.
                 A k8s_collector.py állítja össze.

    Visszatér:
      { service_name → (NodeStatus | None, reason | None) }
      Ahol NodeStatus != None → a riasztás zajnak minősült (SUPPRESSED_*).
      Ahol NodeStatus == None → a riasztás valódi hibát jelezhet, az RCA
                                motornak kell megvizsgálnia.

    Megjegyzés: ha egy service-re több riasztás is érkezett az ablakon belül
    (pl. HighErrorRate ÉS HighLatency), az első zajnak minősített riasztás
    dönt – mindkét riasztás SUPPRESSED státuszt kap a service szintjén.
    """
    results: dict[str, Tuple[Optional[NodeStatus], Optional[str]]] = {}

    for alert in alerts:
        # Csak a firing riasztásokat vizsgáljuk
        if alert.status != "firing":
            continue

        service = alert.service

        # Ha már eldöntöttük ezt a service-t (több riasztás ugyanarról),
        # nem futtatjuk le újra a szűrést
        if service in results:
            continue

        node = node_map.get(service)
        k8s_ctx = k8s_map.get(service)

        status, reason = filter_alert(alert, node, k8s_ctx)
        results[service] = (status, reason)

    return results
