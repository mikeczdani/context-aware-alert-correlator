"""
engine/graph_correlator.py – NetworkX DiGraph építő és Gyökérok (RCA) Motor (3. Réteg)

Ez a modul a szakdolgozat legfontosabb algoritmusát valósítja meg:
a Kiali topológiagráf alapján összekapcsolja az időablakon belül beérkező
riasztásokat, és megkeresi a hívási lánc gyökérokát (Root Cause Analysis – RCA).

Miért gráf-alapú RCA?
  Mikroszolgáltatás-környezetben egy hiba ritkán izolált: ha a "db" service
  lelassul, a "payment" és az "inventory" is lassan válaszol (mert mindkettő
  a db-t hívja), majd az "order" is lassan válaszol (mert payment-et és
  inventory-t hívja), és végül az "api-gateway" is hibás lesz.
  A Prometheus mind a 4–5 service-re külön riasztást küld – de ezek egyetlen
  ok következményei. A gráfbejárás ezt a kapcsolatot tárja fel.

Az algoritmus lépései (a markdown 3. réteg leírása alapján):
  1. build_topology_graph(): Kiali JSON → NetworkX DiGraph
     Az élek iránya: caller → callee (pl. order → payment)
  2. filter_alerts_batch(): zajszűrés (noise_filter.py delegálva)
  3. correlate(): a nem-zajnak minősült riasztó csomópontok közül megkeresi
     az összefüggőket (van-e hívási útvonal köztük), és csoportosítja őket.
  4. find_root_cause(): a csoporton belül megkeresi a legmélyebb downstream
     hibás csomópontot – ez a ROOT_CAUSE, a többiek SYMPTOM-ok.

Kimenet: CorrelatedIncident objektum, amelyet a webhook_routes.py ment el.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from app.engine.noise_filter import filter_alerts_batch
from app.schemas.alert import RawAlert
from app.schemas.incident import (
    AffectedNode,
    CorrelatedIncident,
    IncidentStatus,
    NodeStatus,
)
from app.schemas.topology import KialiGraph, KialiNode, K8sContext


# ---------------------------------------------------------------------------
# 1. lépés: Kiali JSON → NetworkX DiGraph
# ---------------------------------------------------------------------------

def build_topology_graph(graph: KialiGraph) -> nx.DiGraph:
    """
    Felépíti a Kiali topológiagráfból a NetworkX irányított gráfot.

    Miért NetworkX DiGraph?
      A DiGraph (directed graph) irányított gráf – az élek iránya
      megmutatja, ki kit hív (caller → callee). Az RCA algoritmushoz
      ez az irány döntő fontosságú: a "downstream" pozíciót (ki van
      a hívási lánc legvégén) csak irányított gráfban lehet meghatározni.

    Csomópontok (nodes):
      Minden Kiali csomópont egy NetworkX node lesz, ahol a node ID
      a service neve (pl. "payment"). A csomóponthoz eltároljuk a
      Kiali metrikákat (http_error_rate, response_time_ms, traffic_rate_rps)
      attribútumként – ezeket a severity_scorer.py használja.

    Élek (edges):
      Minden Kiali él egy irányított él lesz a gráfban: source → target.
      Az él iránya: a source hívja a target-et.
      Pl. order → payment azt jelenti: az order service hívja a payment-et.
      Ha a payment hibás, az order is hibás lesz (következményes hiba).

    Miért service nevet használunk node ID-ként és nem Kiali ID-t?
      A service név az a közös kulcs, amely összeköti a KialiNode-ot,
      a RawAlert-et és a K8sContext-et. A Kiali belső ID-k csak a Kiali
      JSON-en belül konzisztensek.
    """
    G = nx.DiGraph()

    # Csomópontok hozzáadása service névvel és metaadatokkal
    for node in graph.nodes:
        G.add_node(
            node.service,
            kiali_id=node.id,
            namespace=node.namespace,
            http_error_rate=node.http_error_rate,
            response_time_ms=node.response_time_ms,
            traffic_rate_rps=node.traffic_rate_rps,
        )

    # ID → service név leképezés az élek feldolgozásához
    # (a Kiali élekben Kiali ID-k szerepelnek, nem service nevek)
    id_to_service: Dict[str, str] = {node.id: node.service for node in graph.nodes}

    # Élek hozzáadása
    for edge in graph.edges:
        source_service = id_to_service.get(edge.source)
        target_service = id_to_service.get(edge.target)

        # Ha valamelyik végpont nem szerepel a csomópontok között
        # (pl. külső service, amit a Kiali nem figyel), kihagyjuk
        if source_service and target_service:
            G.add_edge(
                source_service,
                target_service,
                traffic_rate_rps=edge.traffic_rate_rps,
            )

    return G


# ---------------------------------------------------------------------------
# 2. lépés: Összefüggő riasztó csomópontok azonosítása
# ---------------------------------------------------------------------------

def _find_connected_alerting_nodes(
    alerting_services: Set[str],
    graph: nx.DiGraph,
) -> List[Set[str]]:
    """
    Megkeresi azokat a csoportokat az alerting_services-en belül,
    ahol a csomópontok között létezik hívási útvonal (path) a gráfban.

    Miért kell ez?
      Ha egyszerre több, egymástól FÜGGETLEN service is riaszt (pl. a "db"
      ÉS az "auth" is hibás, de az auth nem függ a db-től), azokat külön
      incidensekbe kell sorolni. Nem szabad összevonni őket egyetlen
      incidensbe, mert nincs közöttük oksági kapcsolat.

    Algoritmus:
      Union-Find (közel): iteratívan ellenőrizzük, hogy van-e irányított
      útvonal (nx.has_path) a riasztó csomópontok minden párja között.
      Ha van, ugyanabba a csoportba kerülnek. Mivel a csoportok száma
      általában kicsi (2–6 service), az O(n²) páronkénti ellenőrzés
      elfogadható teljesítményt nyújt.

    Visszatér: csoportok listája, ahol minden csoport összefüggő
    riasztó szolgáltatások halmaza.

    Példa:
      alerting_services = {"db", "payment", "inventory", "order", "auth"}
      Gráf: order→payment→db, order→inventory→db, api-gateway→auth
      auth nem függ db-től → két csoport:
        [{db, payment, inventory, order}, {auth}]
    """
    if not alerting_services:
        return []

    services = list(alerting_services)
    # Union-Find: group_id[service] = csoport képviselő service neve
    group_id: Dict[str, str] = {s: s for s in services}

    def find(s: str) -> str:
        """Path-compression union-find."""
        while group_id[s] != s:
            group_id[s] = group_id[group_id[s]]
            s = group_id[s]
        return s

    def union(a: str, b: str) -> None:
        group_id[find(a)] = find(b)

    # Páronként ellenőrizzük a hívási útvonalat
    for i in range(len(services)):
        for j in range(i + 1, len(services)):
            a, b = services[i], services[j]
            # Ha van útvonal bármelyik irányban, összetartoznak
            if (
                a in graph
                and b in graph
                and (nx.has_path(graph, a, b) or nx.has_path(graph, b, a))
            ):
                union(a, b)

    # Csoportok összegyűjtése
    groups: Dict[str, Set[str]] = {}
    for s in services:
        root = find(s)
        groups.setdefault(root, set()).add(s)

    return list(groups.values())


# ---------------------------------------------------------------------------
# 3. lépés: ROOT_CAUSE azonosítása egy csoporton belül
# ---------------------------------------------------------------------------

def _find_root_cause_in_group(
    group: Set[str],
    graph: nx.DiGraph,
    node_map: Dict[str, KialiNode],
) -> str:
    """
    Megkeresi a csoporton belüli ROOT_CAUSE csomópontot.

    Az RCA algoritmus alapelve (a markdown leírása szerint):
      A hívási lánc legmélyebb downstream pozícióban lévő hibás csomópont
      a gyökérok. "Downstream" = amelyet a többi hív, de ő maga nem hív
      hibás csomópontot (vagy senkit sem hív a csoporton belül).

    Algoritmus:
      1. Meghatározzuk a csoport részgráfját (csak a csoporthoz tartozó élek).
      2. Megkeressük azt a csomópontot, amelynek nincs kimenő éle más
         riasztó csomópont felé (out-degree = 0 a részgráfban).
         Ez a "legmélyebb" csomópont – innen indulnak ki a hibák.
      3. Ha több ilyen csomópont van (pl. két független db node), azt
         választjuk, amelynek a legmagasabb a hibaaránya (http_error_rate).
      4. Ha nincs egyértelmű "legmélyebb" (kör az algráfban), a legmagasabb
         hibaarányú csomópontot választjuk.

    Példa (02_cascading_latency forgatókönyv):
      Csoport: {db, payment, inventory, order, api-gateway}
      Részgráf élek: order→payment, order→inventory, payment→db, inventory→db,
                     api-gateway→order
      db out-degree = 0 (a csoporton belül nem hív senkit) → db = ROOT_CAUSE
      payment, inventory, order, api-gateway → SYMPTOM
    """
    # A csoport által indukált részgráf (csak a csoporton belüli élek)
    subgraph = graph.subgraph(group)

    # Azok a csomópontok, amelyeknek nincs kimenő éle a csoporton belül
    # → ők vannak a lánc legvégén (downstream)
    candidates = [
        node for node in group
        if subgraph.out_degree(node) == 0
    ]

    if not candidates:
        # Kör van az algráfban (ritka, de lehetséges) – legmagasabb hibaarányút választjuk
        candidates = list(group)

    # A jelöltek közül a legmagasabb http_error_rate-ű nyeri
    def error_rate(service: str) -> float:
        node = node_map.get(service)
        return node.http_error_rate if node else 0.0

    return max(candidates, key=error_rate)


# ---------------------------------------------------------------------------
# Fő belépési pont: teljes korrelációs pipeline
# ---------------------------------------------------------------------------

async def correlate(
    alerts: List[RawAlert],
    graph: KialiGraph,
    k8s_map: Dict[str, K8sContext],
) -> CorrelatedIncident:
    """
    A teljes korrelációs pipeline egy webhook feldolgozási cikluson belül.

    Lépések:
      1. Felépíti a NetworkX DiGraph-ot a Kiali gráfból.
      2. Zajszűrés (noise_filter.filter_alerts_batch) – kiszűri a
         skálázódási és újraindulási zajt.
      3. A nem-zajnak minősült riasztókat csoportosítja gráf-útvonal alapján.
      4. Minden csoportban megkeresi a ROOT_CAUSE-t, a többit SYMPTOM-nak jelöli.
      5. Összeállítja és visszaadja a CorrelatedIncident objektumot.

    Paraméterek:
      alerts:   Az időablakon belüli összes aktív (firing) riasztás.
      graph:    Az aktuális Kiali topológia-pillanatkép.
      k8s_map:  { service_name → K8sContext } – a K8s állapotok a zajszűrőhöz.

    Visszatér: egyetlen CorrelatedIncident objektum, amely tartalmazza
      az összes érintett csomópontot státusszal és indoklással.

    Megjegyzés: ha az összes riasztás zajnak minősül, az incidens státusza
    SUPPRESSED lesz, és root_cause_service = None.
    """

    # -------------------------------------------------------------------
    # 1. NetworkX DiGraph felépítése
    # -------------------------------------------------------------------
    G = build_topology_graph(graph)

    # Service név → KialiNode leképezés (a severity_scorer és az RCA-hoz)
    node_map: Dict[str, KialiNode] = {node.service: node for node in graph.nodes}

    # -------------------------------------------------------------------
    # 2. Zajszűrés
    # -------------------------------------------------------------------
    # filter_results: { service_name → (NodeStatus | None, reason | None) }
    # None státusz = valódi hibának tűnik, tovább kell vizsgálni
    filter_results = filter_alerts_batch(alerts, node_map, k8s_map)

    # Elnyomott és valódi riasztók szétválasztása
    suppressed_services: Dict[str, Tuple[NodeStatus, str]] = {}
    real_alerting_services: Set[str] = set()

    for service, (status, reason) in filter_results.items():
        if status is not None:
            # Zajnak minősült
            suppressed_services[service] = (status, reason or "")
        else:
            # Valódi hibának tűnik – RCA-ra kerül
            real_alerting_services.add(service)

    # -------------------------------------------------------------------
    # 3. Összefüggő csoportok keresése a valódi riasztók között
    # -------------------------------------------------------------------
    # Ha nincs valódi riasztó, az összes zaj volt → SUPPRESSED incidens
    affected_nodes: List[AffectedNode] = []

    # Elnyomott csomópontok hozzáadása az affected_nodes listához
    for service, (status, reason) in suppressed_services.items():
        kiali_node = node_map.get(service)
        affected_nodes.append(AffectedNode(
            service=service,
            status=status,
            http_error_rate=kiali_node.http_error_rate if kiali_node else 0.0,
            response_time_ms=kiali_node.response_time_ms if kiali_node else 0.0,
            severity_score=0.0,  # zajra nem számolunk severity score-t
            suppression_reason=reason,
        ))

    root_cause_service: Optional[str] = None

    if real_alerting_services:
        # -------------------------------------------------------------------
        # 4. ROOT_CAUSE és SYMPTOM azonosítása
        # -------------------------------------------------------------------
        # Összefüggő csoportok megkeresése
        groups = _find_connected_alerting_nodes(real_alerting_services, G)

        for group in groups:
            # ROOT_CAUSE megkeresése a csoporton belül
            root_cause = _find_root_cause_in_group(group, G, node_map)

            # Ha több csoport is van, az első csoport ROOT_CAUSE-át adjuk vissza
            # (a legsúlyosabbaknál általában egy nagy összefüggő komponens van)
            if root_cause_service is None:
                root_cause_service = root_cause

            for service in group:
                kiali_node = node_map.get(service)
                status = NodeStatus.ROOT_CAUSE if service == root_cause else NodeStatus.SYMPTOM

                affected_nodes.append(AffectedNode(
                    service=service,
                    status=status,
                    http_error_rate=kiali_node.http_error_rate if kiali_node else 0.0,
                    response_time_ms=kiali_node.response_time_ms if kiali_node else 0.0,
                    # severity_score-t a severity_scorer.py tölti ki utólag
                    severity_score=0.0,
                    suppression_reason=None,
                ))

    # -------------------------------------------------------------------
    # 5. CorrelatedIncident összeállítása
    # -------------------------------------------------------------------
    incident_status = (
        IncidentStatus.SUPPRESSED
        if root_cause_service is None
        else IncidentStatus.OPEN
    )

    suppressed_count = len(suppressed_services)
    correlated_count = len(real_alerting_services)
    raw_alert_count = len([a for a in alerts if a.status == "firing"])

    # Automatikus szöveges összefoglaló generálása
    summary = _generate_summary(
        root_cause_service=root_cause_service,
        suppressed_services=list(suppressed_services.keys()),
        real_alerting_services=real_alerting_services,
    )

    return CorrelatedIncident(
        status=incident_status,
        root_cause_service=root_cause_service,
        affected_nodes=affected_nodes,
        raw_alert_count=raw_alert_count,
        suppressed_count=suppressed_count,
        correlated_count=correlated_count,
        summary=summary,
        raw_alerts=[a.model_dump(mode="json") for a in alerts],
        topology_snapshot=graph.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Segédfüggvény: szöveges összefoglaló generálása
# ---------------------------------------------------------------------------

def _generate_summary(
    root_cause_service: Optional[str],
    suppressed_services: List[str],
    real_alerting_services: Set[str],
) -> str:
    """
    Automatikusan generál egy emberi olvasásra szánt összefoglalót az incidenshez.

    Ezt az IncidentFeed kártya tetején jeleníti meg a React frontend,
    hogy az üzemeltető egy mondatból megértse, mi történt.

    Példák:
      - "db caused cascading failures in payment, inventory, order."
      - "All 2 alert(s) suppressed: auth (scaling), inventory (restart)."
      - "db caused cascading failures in payment. auth suppressed (scaling)."
    """
    parts: List[str] = []

    if root_cause_service:
        symptoms = [s for s in real_alerting_services if s != root_cause_service]
        if symptoms:
            symptom_list = ", ".join(sorted(symptoms))
            parts.append(
                f"'{root_cause_service}' caused cascading failures in {symptom_list}."
            )
        else:
            parts.append(f"'{root_cause_service}' is failing (no downstream symptoms detected).")

    if suppressed_services:
        suppressed_list = ", ".join(sorted(suppressed_services))
        parts.append(f"Suppressed as noise: {suppressed_list}.")

    if not parts:
        return "No actionable alerts in this window."

    return " ".join(parts)
