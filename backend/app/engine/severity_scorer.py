"""
engine/severity_scorer.py – Gráf-centralitás alapú súlyosság-értékelő (3. Réteg)

Ez a modul kiszámolja az egyes érintett csomópontok prioritáspontszámát
(SeverityScore), és frissíti a CorrelatedIncident affected_nodes listáját.

Miért kell súlyosság-értékelés?
  Az RCA algoritmus megmondja, hogy melyik csomópont a ROOT_CAUSE és
  melyek a SYMPTOM-ok. De nem minden ROOT_CAUSE egyforma súlyosságú:
    - Egy "db" service amely 500 req/s forgalmat kezel és 30%-os hibaaránnyal
      rendelkezik, sokkal kritikusabb, mint egy "auth" service amely
      csak 10 req/s forgalmat kezel és 6%-os hibaaránnyal.
  A SeverityScore ezt a különbséget számszerűsíti, és segít az üzemeltetőnek
  prioritizálni, ha egyszerre több incidens is nyitott.

A SeverityScore képlete (a markdown leírása szerint):
  SeverityScore = w1 * ErrorRate + w2 * PageRank(v) + w3 * TrafficShare(v)

  ahol:
    ErrorRate:    A csomópont HTTP hibaaránya (0.0–1.0)
                  w1 = 0.5 – a legfontosabb tényező: ha sok kérés hibás, az kritikus
    PageRank(v):  A csomópont NetworkX PageRank értéke (0.0–1.0 normalizálva)
                  w2 = 0.3 – minél több service függ ettől a csomóponttól,
                  annál nagyobb a kiesés hatása a teljes rendszerre
    TrafficShare: A csomópont forgalmának aránya a teljes gráf forgalmához képest
                  w3 = 0.2 – a nagy forgalmú csomópontok kiesése több felhasználót érint

  A súlyok összege: w1 + w2 + w3 = 1.0 (a score 0.0–1.0 tartományban marad)

Kimenet:
  score_incident() visszaadja a frissített CorrelatedIncident-et,
  ahol minden AffectedNode.severity_score ki van töltve.
  A graph_correlator.py correlate() függvénye hívja ezt közvetlenül
  a CorrelatedIncident összeállítása után.
"""

from __future__ import annotations

from typing import Dict

import networkx as nx

from app.schemas.incident import AffectedNode, CorrelatedIncident
from app.schemas.topology import KialiGraph

# ---------------------------------------------------------------------------
# Súlyok – a markdown képlete alapján
# ---------------------------------------------------------------------------

W1_ERROR_RATE = 0.5     # HTTP hibaarány súlya
W2_PAGE_RANK = 0.3      # PageRank (gráf-centralitás) súlya
W3_TRAFFIC_SHARE = 0.2  # Forgalomrészesedés súlya


# ---------------------------------------------------------------------------
# Segédfüggvények
# ---------------------------------------------------------------------------

def _compute_pagerank(graph: nx.DiGraph) -> Dict[str, float]:
    """
    Kiszámolja a NetworkX PageRank értékeket az összes csomópontra.

    Miért PageRank?
      A PageRank eredetileg a weboldalak fontosságát mérte az oda mutató
      linkek alapján. Mikroszolgáltatás-gráfban ugyanez az elv működik:
      ha sok service hív egy csomópontot (pl. a "db"-t hívja a payment
      és az inventory is), az a csomópont "fontosabb" – kiesése nagyobb
      hatást okoz.

    A PageRank értékek összege = 1.0 a teljes gráfra, tehát automatikusan
    normalizáltak (0.0–1.0 tartomány).

    Ha a gráf üres (nincs él), minden csomópont azonos értéket kap
    (1 / csomópontok_száma). Ha a gráf egyetlen csomópontból áll,
    az PageRank értéke 1.0.

    alpha=0.85: az alapértelmezett damping factor (szokásos értéke 0.85)
    """
    if len(graph.nodes) == 0:
        return {}

    try:
        return nx.pagerank(graph, alpha=0.85)
    except nx.PowerIterationFailedConvergence:
        # Ha a PageRank iteráció nem konvergál (nagyon ritka),
        # egyenletes eloszlással térünk vissza
        n = len(graph.nodes)
        return {node: 1.0 / n for node in graph.nodes}


def _compute_traffic_shares(graph: nx.DiGraph) -> Dict[str, float]:
    """
    Kiszámolja az egyes csomópontok forgalomrészesedését.

    TrafficShare(v) = traffic_rate_rps(v) / sum(traffic_rate_rps az összes csomópontra)

    A traffic_rate_rps értékeket a Kiali gráfból vesszük (node attribútum).
    Ha az összes csomópont forgalma 0 (pl. MOCK adatokban nincs adat),
    egyenletes eloszlást adunk vissza, hogy a score kiszámítható maradjon.

    Eredmény: { service_name → 0.0...1.0 }
    """
    traffic_values: Dict[str, float] = {
        node: graph.nodes[node].get("traffic_rate_rps", 0.0)
        for node in graph.nodes
    }

    total_traffic = sum(traffic_values.values())

    if total_traffic == 0.0:
        # Ha nincs forgalmi adat, egyenletes elosztás
        n = len(graph.nodes)
        return {node: 1.0 / n if n > 0 else 0.0 for node in graph.nodes}

    return {
        node: rps / total_traffic
        for node, rps in traffic_values.items()
    }


# ---------------------------------------------------------------------------
# Fő pontozó függvény
# ---------------------------------------------------------------------------

def calculate_severity(
    service: str,
    graph: nx.DiGraph,
    pagerank: Dict[str, float],
    traffic_shares: Dict[str, float],
) -> float:
    """
    Kiszámolja egyetlen csomópont SeverityScore értékét.

    Képlet:
      SeverityScore = w1 * ErrorRate + w2 * PageRank(v) + w3 * TrafficShare(v)

    Paraméterek:
      service:        A csomópont service neve.
      graph:          A NetworkX DiGraph (a csomópont attribútumait tartalmazza).
      pagerank:       Előre kiszámított PageRank szótár { service → érték }.
      traffic_shares: Előre kiszámított forgalomrészesedés { service → érték }.

    Visszatér: 0.0–1.0 közötti float érték.
      0.0 = minimálisan kritikus (alacsony hibaarány, alacsony centralitás)
      1.0 = maximálisan kritikus (magas hibaarány, sok service függ tőle,
            nagy forgalmat kezel)
    """
    # ErrorRate a Kiali gráf node attribútumából
    error_rate = graph.nodes[service].get("http_error_rate", 0.0) if service in graph else 0.0

    # PageRank – ha a service nem szerepel a gráfban, 0.0
    pr = pagerank.get(service, 0.0)

    # TrafficShare – ha a service nem szerepel, 0.0
    ts = traffic_shares.get(service, 0.0)

    score = W1_ERROR_RATE * error_rate + W2_PAGE_RANK * pr + W3_TRAFFIC_SHARE * ts

    # Kerekítés 4 tizedesjegyre az olvashatósághoz
    return round(score, 4)


# ---------------------------------------------------------------------------
# Incidens-szintű pontozás – ezt hívja a graph_correlator.py
# ---------------------------------------------------------------------------

def score_incident(
    incident: CorrelatedIncident,
    graph: KialiGraph,
    nx_graph: nx.DiGraph,
) -> CorrelatedIncident:
    """
    Frissíti a CorrelatedIncident összes AffectedNode.severity_score mezőjét.

    A graph_correlator.py correlate() függvénye 0.0-val hagyja a severity_score-t,
    majd ezt a függvényt hívja a végleges értékek kitöltéséhez.

    Miért utólag, külön függvényben?
      A graph_correlator.py már így is sok feladatot lát el (zajszűrés,
      csoportosítás, RCA). A severity scoring külön modulban tartja az
      egységes felelősséget (Single Responsibility Principle), és könnyebben
      cserélhető/finomhangolható a súlyok módosítása nélkül a fő logika érintése.

    Paraméterek:
      incident: A graph_correlator.py által összeállított incidens
                (severity_score = 0.0 minden csomóponton).
      graph:    A KialiGraph Pydantic objektum (a node listához kell).
      nx_graph: Az ugyanabból a KialiGraph-ból épített NetworkX DiGraph
                (a build_topology_graph() által visszaadott objektum).

    Visszatér: ugyanaz az incident objektum, de feltöltött severity_score-okkal.
    Megjegyzés: a Pydantic modellek alapértelmezetten nem mutálhatók (frozen=False
    az incidentnél), de az affected_nodes lista elemei módosíthatók in-place.
    """
    # PageRank és TrafficShare előzetes kiszámítása az egész gráfra
    # (hatékonyabb, mint minden csomópontnál újra kiszámolni)
    pagerank = _compute_pagerank(nx_graph)
    traffic_shares = _compute_traffic_shares(nx_graph)

    # Minden érintett csomópont severity_score-jának frissítése
    updated_nodes = []
    for node in incident.affected_nodes:
        score = calculate_severity(
            service=node.service,
            graph=nx_graph,
            pagerank=pagerank,
            traffic_shares=traffic_shares,
        )
        # AffectedNode immutable-ként kezelve: új objektumot hozunk létre
        # a frissített severity_score-ral (Pydantic model_copy)
        updated_nodes.append(node.model_copy(update={"severity_score": score}))

    # CorrelatedIncident frissítése az új affected_nodes listával
    return incident.model_copy(update={"affected_nodes": updated_nodes})


# ---------------------------------------------------------------------------
# Standalone segédfüggvény – teszteléshez és Swagger UI-hoz
# ---------------------------------------------------------------------------

def score_node_standalone(
    service: str,
    error_rate: float,
    in_degree: int,
    total_nodes: int,
    traffic_rps: float,
    total_traffic_rps: float,
) -> float:
    """
    Kiszámolja egy csomópont SeverityScore-ját gráf nélkül, nyers értékekből.

    Ez a függvény teszteléshez és a Swagger UI /docs felületen való
    gyors kipróbáláshoz hasznos – nem kell felépíteni az egész DiGraph-ot.

    Paraméterek:
      service:           A csomópont neve (csak a visszaadott szótár kulcsához kell).
      error_rate:        HTTP hibaarány (0.0–1.0).
      in_degree:         Hány service hívja ezt a csomópontot (PageRank közelítés).
      total_nodes:       Az összes csomópont száma a gráfban (normalizáláshoz).
      traffic_rps:       A csomópont forgalma (req/s).
      total_traffic_rps: Az összes csomópont összesített forgalma (normalizáláshoz).

    PageRank közelítés in_degree alapján:
      Ha nincs teljes gráf, az in_degree / total_nodes egyszerű közelítése
      a PageRank-nek (nem pontos, de szemléltetésre elegendő).
    """
    pr_approx = in_degree / total_nodes if total_nodes > 0 else 0.0
    ts = traffic_rps / total_traffic_rps if total_traffic_rps > 0 else 0.0

    score = W1_ERROR_RATE * error_rate + W2_PAGE_RANK * pr_approx + W3_TRAFFIC_SHARE * ts
    return round(score, 4)
