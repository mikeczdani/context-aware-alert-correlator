"""
collectors/k8s_collector.py – Kubernetes Pod / Deployment állapot adapter (2. Réteg)

Ez a modul felelős az érintett mikroszolgáltatás K8s életciklus-állapotának
lekéréséért. Ezt az állapotot a noise_filter.py használja annak eldöntéséhez,
hogy egy riasztás valódi hibát jelez-e, vagy csak egy tervezett K8s esemény
(pod újraindulás, HPA skálázódás) mellékhatása.

Két üzemmódban működik (DATA_MODE):

  MOCK mód:
    A scenarios/*.json fájlok k8s_contexts szekciójából olvassa ki
    a K8sContext listát, amely a forgatókönyvhöz tartozó pod-állapotokat
    tartalmazza (restarts_last_minute, is_scaling, stb.).

  LIVE mód:
    A Kubernetes Python klienssel (kubernetes csomag) lekérdezi a valódi
    Deployment és Pod erőforrásokat a kluszterből.
    Megjegyzés: LIVE módban a kubernetes csomag telepítése szükséges,
    és a ~/.kube/config fájlnak (vagy in-cluster service account tokennek)
    elérhetőnek kell lennie.

Kimenet: minden esetben Dict[str, K8sContext], ahol a kulcs a service neve.
Így a noise_filter.py egyszerűen service_name alapján kikeresheti az állapotot:
  k8s_map = await get_k8s_contexts(...)
  ctx = k8s_map.get("payment")
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import DataMode, settings
from app.schemas.topology import K8sContext

# ---------------------------------------------------------------------------
# MOCK mód – scenarios/*.json betöltés
# ---------------------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).parent.parent.parent
_SCENARIOS_DIR = _BACKEND_ROOT / settings.SCENARIOS_DIR

# Forgatókönyv ID → fájlnév leképezés (megegyezik a kiali_collector.py-éval)
SCENARIO_FILE_MAP: dict[str, str] = {
    "healthy_baseline":     "00_healthy_baseline.json",
    "pod_restart_noise":    "01_pod_restart_noise.json",
    "cascading_db_timeout": "02_cascading_latency.json",
    "mixed_storm":          "03_mixed_failure.json",
}


def _load_k8s_contexts_from_scenario(scenario_id: str) -> List[K8sContext]:
    """
    Betölti a megadott forgatókönyv k8s_contexts szekcióját.

    A scenarios/*.json fájlok struktúrája:
      {
        "k8s_contexts": [
          {
            "service": "inventory",
            "desired_replicas": 1,
            "ready_replicas": 1,
            "restarts_last_minute": 3,
            "is_scaling": false,
            "last_scale_event_age_seconds": 999.0
          },
          ...
        ]
      }

    A Pydantic model_validate() validálja a mezőket és alapértelmezéseket
    alkalmaz a hiányzó opcionális mezőkre (pl. namespace = "default").
    """
    filename = SCENARIO_FILE_MAP.get(scenario_id)
    if filename is None:
        available = list(SCENARIO_FILE_MAP.keys())
        raise ValueError(
            f"Ismeretlen scenario_id: '{scenario_id}'. "
            f"Elérhető forgatókönyvek: {available}"
        )

    filepath = _SCENARIOS_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(
            f"A forgatókönyv fájl nem található: {filepath}."
        )

    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    contexts = []
    for raw in data.get("k8s_contexts", []):
        # A JSON-ben lévő "_comment" mezők ismeretlen mezők lennének a Pydantic-nak.
        # A model_config extra="ignore" kellene hozzá, de egyszerűbb kiszűrni itt,
        # hogy a schema tiszta maradjon.
        filtered = {k: v for k, v in raw.items() if not k.startswith("_")}
        contexts.append(K8sContext.model_validate(filtered))

    return contexts


# ---------------------------------------------------------------------------
# LIVE mód – valódi Kubernetes API lekérdezés
# ---------------------------------------------------------------------------

async def _fetch_live_k8s_contexts(
    services: List[str],
    namespace: str,
) -> List[K8sContext]:
    """
    Lekéri a megadott szolgáltatások K8s állapotát a valódi Kubernetes API-ból.

    A kubernetes Python csomag kubeconfig-alapú hitelesítéssel csatlakozik.
    Lokálisan (k3d / Minikube): a ~/.kube/config-ot használja.
    Kluszterben futva: az in-cluster service account tokent.

    Lekérdezett erőforrások:
      1. AppsV1Api – Deployment: desired_replicas (spec.replicas)
                                 ready_replicas (status.readyReplicas)
      2. CoreV1Api – Pod lista:  restarts_last_minute (containerStatuses[].restartCount
                                 változása az elmúlt 60s-ban – közelítésként a
                                 lastState.terminated.finishedAt időbélyegét vizsgáljuk)
      3. CoreV1Api – Event lista: ScalingReplicaSet esemény kora másodpercben
                                  (last_scale_event_age_seconds)

    Megjegyzés: a kubernetes csomag szinkron – asyncio event loop-ban egy
    ThreadPoolExecutor-on keresztül futtatjuk, hogy ne blokkolja a FastAPI-t.
    (Jövőbeli bővítési pont: az async-client-go vagy a kr8s csomag használata.)
    """
    # A kubernetes csomag csak LIVE módban szükséges – késleltetett importtal
    # elkerüljük, hogy MOCK módban hiányzó csomag miatt crasheljen az alkalmazás.
    try:
        from kubernetes import client as k8s_client, config as k8s_config
    except ImportError:
        raise ImportError(
            "A 'kubernetes' csomag nincs telepítve. "
            "LIVE módhoz futtasd: pip install kubernetes"
        )

    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def _sync_fetch() -> List[K8sContext]:
        """Szinkron belső függvény, amelyet threadpool-ban futtatunk."""
        k8s_config.load_kube_config()  # ~/.kube/config beolvasása
        apps_v1 = k8s_client.AppsV1Api()
        core_v1 = k8s_client.CoreV1Api()

        results: List[K8sContext] = []
        now = time.time()

        for service_name in services:
            # --- Deployment lekérdezése ---
            desired = 1
            ready = 1
            try:
                dep = apps_v1.read_namespaced_deployment(
                    name=service_name, namespace=namespace
                )
                desired = dep.spec.replicas or 1
                ready = dep.status.ready_replicas or 0
            except k8s_client.ApiException:
                pass  # Ha nem létezik deployment ilyen névvel, alapértéket használunk

            # --- Pod újraindulások az elmúlt 60 másodpercben ---
            restarts_last_minute = 0
            try:
                pods = core_v1.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=f"app={service_name}",
                )
                for pod in pods.items:
                    for cs in pod.status.container_statuses or []:
                        # Ha a pod újraindult és az utolsó leállás az elmúlt 60s-ban volt
                        last_state = cs.last_state
                        if last_state and last_state.terminated:
                            finished_at = last_state.terminated.finished_at
                            if finished_at:
                                age_seconds = now - finished_at.timestamp()
                                if age_seconds <= 60:
                                    restarts_last_minute += 1
            except k8s_client.ApiException:
                pass

            # --- ScalingReplicaSet esemény kora ---
            last_scale_age = 999.0
            is_scaling = desired != ready  # Alap: desired != ready → skálázódás
            try:
                events = core_v1.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"reason=ScalingReplicaSet,"
                                   f"involvedObject.kind=ReplicaSet",
                )
                for event in events.items:
                    # Az érintett ReplicaSet neve tartalmazza a deployment nevét
                    involved_name = event.involved_object.name or ""
                    if service_name in involved_name and event.last_timestamp:
                        age = now - event.last_timestamp.timestamp()
                        if age < last_scale_age:
                            last_scale_age = age

                # Ha az utolsó skálázódási esemény Δt ≤ 90s-on belül volt → zajszűrés
                if last_scale_age <= settings.SCALE_NOISE_WINDOW_SECONDS:
                    is_scaling = True
            except k8s_client.ApiException:
                pass

            results.append(K8sContext(
                service=service_name,
                namespace=namespace,
                desired_replicas=desired,
                ready_replicas=ready,
                restarts_last_minute=restarts_last_minute,
                is_scaling=is_scaling,
                last_scale_event_age_seconds=last_scale_age,
            ))

        return results

    # Szinkron K8s API hívás futtatása külön szálon, hogy ne blokkolja az event loop-t
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return await loop.run_in_executor(executor, _sync_fetch)


# ---------------------------------------------------------------------------
# Publikus API – ezt hívja a webhook_routes.py és a noise_filter
# ---------------------------------------------------------------------------

async def get_k8s_contexts(
    services: List[str],
    scenario_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Dict[str, K8sContext]:
    """
    Visszaadja a megadott szolgáltatások K8s állapotát szótárként.

    A visszatérési érték: { "payment": K8sContext(...), "db": K8sContext(...), ... }
    Így a noise_filter.py egyszerű dict lookup-pal kikeresheti bármelyik
    szolgáltatás állapotát:
      ctx = k8s_map.get("inventory")
      if ctx and ctx.restarts_last_minute > 0:
          ...

    Paraméterek:
      services:    Azoknak a szolgáltatásoknak a nevei, amelyekre szükségünk van.
                   Általában a riasztásokban szereplő service nevek listája.
      scenario_id: MOCK módban melyik forgatókönyvet töltse be.
                   Ha None, az alapállapotot (healthy_baseline) használja.
      namespace:   LIVE módban melyik Kubernetes névtérből kérdezzen.
                   Ha None, a config.py KIALI_NAMESPACE értékét használja.
    """
    ns = namespace or settings.KIALI_NAMESPACE

    if settings.DATA_MODE == DataMode.MOCK:
        sid = scenario_id or "healthy_baseline"
        all_contexts = _load_k8s_contexts_from_scenario(sid)
        # Csak azokat adjuk vissza, amelyeket a hívó kért
        # (a scenarios-ban mind a 6 service szerepel, de pl. 1 riasztásnál
        #  elég csak az érintett service K8s állapota)
        return {ctx.service: ctx for ctx in all_contexts if ctx.service in services}
    else:
        contexts_list = await _fetch_live_k8s_contexts(services, ns)
        return {ctx.service: ctx for ctx in contexts_list}
