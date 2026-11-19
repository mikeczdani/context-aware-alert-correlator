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
  3. _propagate_suppression(): az elnyomás továbbterjesztése a hívókra.
     Ha egy csomópont összes riasztó downstream függősége elnyomott zaj, a
     csomópont riasztása is csak ennek a zajnak a következménye (pl. az inventory
     újraindul, ezért az order latenciája nő) → SUPPRESSED_DOWNSTREAM.
  4. correlate(): a nem-zajnak minősült riasztó csomópontok közül megkeresi
     az összefüggőket (van-e hívási útvonal köztük), és csoportosítja őket.
  5. find_root_cause(): a csoporton belül megkeresi a legmélyebb downstream
     hibás csomópontot – ez a ROOT_CAUSE, a többiek SYMPTOM-ok.

Determinizmus:
  A szakdolgozati mérésekhez a kimenetnek ismételhetőnek kell lennie. A Python
  sztring-halmazainak bejárási sorrendje folyamatonként változhat (hash-
  randomizálás), ezért minden bejárás rendezett, és minden döntetlen
  explicit szabállyal dől el (lásd a függvények leírását).

Kimenet: CorrelatedIncident objektum, amelyet a webhook_routes.py ment el.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from app.engine.noise_filter import HARD_ERROR_THRESHOLD, filter_alerts_batch
from app.engine.severity_scorer import score_incident
from app.schemas.alert import RawAlert
from app.schemas.incident import (
    AffectedNode,
    CorrelatedIncident,
    IncidentStatus,
    NodeStatus,
)
from app.schemas.topology import KialiGraph, KialiNode, K8sContext


# ---------------------------------------------------------------------------
# Segédfüggvény
# ---------------------------------------------------------------------------

def _error_rate(node_map: Dict[str, KialiNode], service: str) -> float:
    """A szolgáltatás Kiali hibaaránya, vagy 0.0, ha a Kiali nem ismeri."""
    node = node_map.get(service)
    return node.http_error_rate if node else 0.0


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
# 2/b lépés: az elnyomás továbbterjesztése a hívókra
# ---------------------------------------------------------------------------

def _propagate_suppression(
    real_services: Set[str],
    suppressed: Dict[str, Tuple[NodeStatus, str]],
    graph: nx.DiGraph,
    node_map: Dict[str, KialiNode],
) -> Tuple[Set[str], Dict[str, Tuple[NodeStatus, str]]]:
    """
    A zajszűrő csak a csomópont SAJÁT K8s állapotát nézi. Ha az inventory újraindul,
    az order latencia-riasztása is zaj – az order saját K8s állapota viszont rendben
    van, ezért a szűrő valódi hibának tekintené, és (egyetlen valódi riasztóként)
    hamis ROOT_CAUSE-ot csinálna belőle. Ez a lépés ezt javítja.

    Szabály: egy valódinak ítélt csomópont (X) elnyomottá válik, ha MIND teljesül:
      1. X hibaaránya a kemény küszöb (noise_filter.HARD_ERROR_THRESHOLD) alatt van.
         (A kemény hibát a zajszűrő sem nyomja el, itt sem.)
      2. X-nek van legalább egy riasztó downstream (tranzitív) függősége.
      3. X MINDEN riasztó downstream függősége elnyomott. Ha van köztük valódi,
         X annak a valódi hibának a következménye (SYMPTOM), nem zaj.
      4. Védőfeltétel: X hibaaránya nem nagyobb, mint a legnagyobb hibaarányú
         elnyomott downstream függőségé. A hiba felfelé a láncon jellemzően csillapodik;
         ha a hívó rosszabb a forrásnál, valószínűleg saját problémája is van.
         Ez heurisztika, a forgatókönyvek mintáját követi.

    A szabályt fixpontig ismételjük, így a többlépéses láncok is jól működnek
    (api-gateway → order → inventory[újraindul]: előbb az order, aztán a gateway
    is elnyomott). A feltételek monotonok (az elnyomott halmaz csak nő), ezért a
    végeredmény nem függ a feldolgozási sorrendtől.

    Paraméterek:
      real_services: a zajszűrés után valódinak ítélt riasztó szolgáltatások.
      suppressed:    { service → (NodeStatus, indoklás) } a zajszűrő elnyomottjai.
      graph:         a topológia DiGraph-ja (caller → callee).
      node_map:      { service → KialiNode } a hibaarányokhoz.

    Visszatér: (frissített valódi halmaz, frissített elnyomott szótár).
    A bemeneti objektumokat nem módosítja.
    """
    real = set(real_services)
    result = dict(suppressed)

    changed = True
    while changed:
        changed = False
        # Az összes riasztó szolgáltatás halmaza nem változik a körökben
        # (csak a valódi → elnyomott átsorolás történik), ezért elég itt számolni.
        alerting = real | set(result)

        for service in sorted(real):
            node = node_map.get(service)
            if node is None or service not in graph:
                continue

            # 1. feltétel: a kemény hibát nem nyomjuk el
            if node.http_error_rate >= HARD_ERROR_THRESHOLD:
                continue

            # 2. feltétel: van riasztó downstream függőség
            downstream_alerting = nx.descendants(graph, service) & alerting
            if not downstream_alerting:
                continue

            # 3. feltétel: nincs köztük valódi hiba
            if downstream_alerting & real:
                continue

            # A legnagyobb hibaarányú elnyomott függőség a "forrás"
            # (döntetlennél a névsorban első – determinisztikus).
            source = max(
                sorted(downstream_alerting),
                key=lambda s: _error_rate(node_map, s),
            )
            source_rate = _error_rate(node_map, source)

            # 4. feltétel (védőfeltétel)
            if node.http_error_rate > source_rate:
                continue

            source_status = result[source][0]
            reason = (
                f"Alert on '{service}' is a downstream effect of suppressed "
                f"'{source}' ({source_status.value}): all alerting dependencies "
                f"of '{service}' are operational noise "
                f"(error_rate={node.http_error_rate:.1%} <= {source_rate:.1%})."
            )
            result[service] = (NodeStatus.SUPPRESSED_DOWNSTREAM, reason)
            real.discard(service)
            changed = True

    return real, result


# ---------------------------------------------------------------------------
# 3. lépés: Összefüggő riasztó csomópontok azonosítása
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

    Determinizmus:
      A szolgáltatásokat névsorban dolgozza fel, és a csoportokat a legkisebb
      tagjuk neve szerint rendezve adja vissza.

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

    services = sorted(alerting_services)
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

    return sorted(groups.values(), key=lambda group: min(group))


# ---------------------------------------------------------------------------
# 4. lépés: ROOT_CAUSE azonosítása egy csoporton belül
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
         Teljes döntetlen esetén a névsorban első nyer (determinisztikus).
      4. Ha nincs egyértelmű "legmélyebb" (kör az algráfban), a legmagasabb
         hibaarányú csomópontot választjuk (ugyanazzal a döntetlen-szabállyal).

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
    candidates = sorted(node for node in group if subgraph.out_degree(node) == 0)

    if not candidates:
        # Kör van az algráfban (ritka, de lehetséges) – legmagasabb hibaarányút választjuk
        candidates = sorted(group)

    # A jelöltek közül a legmagasabb http_error_rate-ű nyeri. A max() döntetlennél
    # az első elemet adja, a lista rendezett, így a névsorban első nyer.
    return max(candidates, key=lambda s: _error_rate(node_map, s))


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
         skálázódási és újraindulási zajt, majd az elnyomást továbbterjeszti
         a hívókra (_propagate_suppression).
      3. A nem-zajnak minősült riasztókat csoportosítja gráf-útvonal alapján.
      4. Minden csoportban megkeresi a ROOT_CAUSE-t, a többit SYMPTOM-nak jelöli.
      5. Összeállítja és visszaadja a CorrelatedIncident objektumot.

    Paraméterek:
      alerts:   Az időablakon belüli összes aktív (firing) riasztás.
      graph:    Az aktuális Kiali topológia-pillanatkép.
      k8s_map:  { service_name → K8sContext } – a K8s állapotok a zajszűrőhöz.

    Visszatér: egyetlen CorrelatedIncident objektum, amely tartalmazza
      az összes érintett csomópontot státusszal és indoklással.

    Több független csoport esetén:
      Minden csoport gyökéroka ROOT_CAUSE státuszt kap az affected_nodes-ban.
      Az incidens root_cause_service mezője az ELSŐDLEGES gyökérokot adja:
        1. a legnagyobb csoport gyökéroka (a legtöbb szolgáltatást érinti),
        2. döntetlennél a nagyobb hibaarányú,
        3. további döntetlennél a névsorban első.

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

    # 2/b. Az elnyomás továbbterjesztése azokra a hívókra, amelyek riasztása
    # kizárólag elnyomott zaj következménye
    real_alerting_services, suppressed_services = _propagate_suppression(
        real_alerting_services, suppressed_services, G, node_map
    )

    # -------------------------------------------------------------------
    # 3. Az elnyomott csomópontok felvétele (névsorban – determinisztikus)
    # -------------------------------------------------------------------
    affected_nodes: List[AffectedNode] = []

    for service, (status, reason) in sorted(suppressed_services.items()):
        kiali_node = node_map.get(service)
        affected_nodes.append(AffectedNode(
            service=service,
            status=status,
            http_error_rate=kiali_node.http_error_rate if kiali_node else 0.0,
            response_time_ms=kiali_node.response_time_ms if kiali_node else 0.0,
            severity_score=0.0,  # a severity_scorer.py tölti ki utólag
            suppression_reason=reason,
        ))

    # -------------------------------------------------------------------
    # 4. Összefüggő csoportok, ROOT_CAUSE és SYMPTOM azonosítása
    # -------------------------------------------------------------------
    group_results: List[Tuple[str, Set[str]]] = []

    if real_alerting_services:
        for group in _find_connected_alerting_nodes(real_alerting_services, G):
            root_cause = _find_root_cause_in_group(group, G, node_map)
            group_results.append((root_cause, group))

        # Csoportok sorrendje = az elsődleges gyökérok szabálya:
        # legnagyobb csoport, majd nagyobb hibaarány, majd név szerint.
        group_results.sort(
            key=lambda item: (
                -len(item[1]),
                -_error_rate(node_map, item[0]),
                item[0],
            )
        )

    root_cause_service: Optional[str] = group_results[0][0] if group_results else None

    for root_cause, group in group_results:
        for service in sorted(group):
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

    # Automatikus szöveges összefoglaló generálása (csoportonként)
    summary = _generate_summary(
        groups=[
            (root_cause, sorted(group - {root_cause}))
            for root_cause, group in group_results
        ],
        suppressed_services=sorted(suppressed_services),
    )

    return score_incident(
        incident=CorrelatedIncident(
            status=incident_status,
            root_cause_service=root_cause_service,
            affected_nodes=affected_nodes,
            raw_alert_count=raw_alert_count,
            suppressed_count=suppressed_count,
            correlated_count=correlated_count,
            summary=summary,
            raw_alerts=[a.model_dump(mode="json") for a in alerts],
            topology_snapshot=graph.model_dump(mode="json"),
        ),
        graph=graph,
        nx_graph=G,
    )


# ---------------------------------------------------------------------------
# Segédfüggvény: szöveges összefoglaló generálása
# ---------------------------------------------------------------------------

def _generate_summary(
    groups: List[Tuple[str, List[str]]],
    suppressed_services: List[str],
) -> str:
    """
    Automatikusan generál egy emberi olvasásra szánt összefoglalót az incidenshez.

    Ezt az IncidentFeed kártya tetején jeleníti meg a React frontend,
    hogy az üzemeltető egy mondatból megértse, mi történt.

    Csoportonként egy mondat készül: minden gyökérokhoz CSAK a saját csoportjának
    következményei tartoznak. (Korábban az összes valódi riasztó szolgáltatást
    egyetlen gyökérokhoz sorolta, ami több független hibánál félrevezető volt.)

    Paraméterek:
      groups:              [(gyökérok, [következmények])] az incidens csoport-
                           sorrendjében (az első az elsődleges).
      suppressed_services: az elnyomott szolgáltatások nevei.

    Példák:
      - "'db' caused cascading failures in inventory, order, payment."
      - "Suppressed as noise: inventory, order."
      - "'db' caused cascading failures in payment. 'shipping' is failing
         (no downstream symptoms detected). Suppressed as noise: auth."
    """
    parts: List[str] = []

    for root_cause, symptoms in groups:
        if symptoms:
            symptom_list = ", ".join(sorted(symptoms))
            parts.append(f"'{root_cause}' caused cascading failures in {symptom_list}.")
        else:
            parts.append(f"'{root_cause}' is failing (no downstream symptoms detected).")

    if suppressed_services:
        suppressed_list = ", ".join(sorted(suppressed_services))
        parts.append(f"Suppressed as noise: {suppressed_list}.")

    if not parts:
        return "No actionable alerts in this window."

    return " ".join(parts)
