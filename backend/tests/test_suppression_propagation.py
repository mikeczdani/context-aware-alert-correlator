"""
tests/test_suppression_propagation.py – az elnyomás továbbterjesztésének szabályai

A zajszűrő csak a csomópont saját K8s állapotát nézi. A propagációs lépés azokat a
hívókat is elnyomja, amelyek riasztása kizárólag elnyomott downstream zaj
következménye. Itt minden szabályt külön, kis gráfon próbálunk ki.
"""

from tests.helpers import (
    correlate_sync,
    healthy,
    make_alert,
    make_graph,
    restarting,
    scaling,
    statuses,
)


def test_caller_of_suppressed_service_is_suppressed():
    graph = make_graph(
        {"order": (0.04, 80), "inventory": (0.18, 55), "db": (0.0, 90)},
        [("order", "inventory"), ("inventory", "db")],
    )
    incident = correlate_sync(
        [make_alert("inventory"), make_alert("order", "HighLatency")],
        graph,
        [restarting("inventory"), healthy("order")],
    )

    assert statuses(incident) == {
        "inventory": "SUPPRESSED_RESTART",
        "order": "SUPPRESSED_DOWNSTREAM",
    }
    assert incident.status.value == "SUPPRESSED"
    assert incident.root_cause_service is None
    assert incident.correlated_count == 0


def test_propagation_follows_multi_hop_chain_regardless_of_alert_order():
    graph = make_graph(
        {"gateway": (0.03, 120), "order": (0.04, 80), "inventory": (0.18, 55)},
        [("gateway", "order"), ("order", "inventory")],
    )
    alerts = [make_alert("gateway"), make_alert("order"), make_alert("inventory")]
    k8s = [healthy("gateway"), healthy("order"), restarting("inventory")]

    expected = {
        "gateway": "SUPPRESSED_DOWNSTREAM",
        "order": "SUPPRESSED_DOWNSTREAM",
        "inventory": "SUPPRESSED_RESTART",
    }
    assert statuses(correlate_sync(alerts, graph, k8s)) == expected
    assert statuses(correlate_sync(list(reversed(alerts)), graph, k8s)) == expected


def test_propagation_reaches_through_non_alerting_intermediate():
    # order → cache (nem riaszt) → inventory (elnyomott)
    graph = make_graph(
        {"order": (0.04, 80), "cache": (0.0, 60), "inventory": (0.18, 55)},
        [("order", "cache"), ("cache", "inventory")],
    )
    incident = correlate_sync(
        [make_alert("inventory"), make_alert("order", "HighLatency")],
        graph,
        [restarting("inventory"), healthy("order")],
    )
    assert statuses(incident)["order"] == "SUPPRESSED_DOWNSTREAM"


def test_caller_with_higher_error_rate_than_source_stays_real():
    # Védőfeltétel: az order (30%) rosszabb, mint a forrás (10%) – saját problémája is van
    graph = make_graph(
        {"order": (0.30, 80), "inventory": (0.10, 55)},
        [("order", "inventory")],
    )
    incident = correlate_sync(
        [make_alert("inventory"), make_alert("order")],
        graph,
        [restarting("inventory"), healthy("order")],
    )

    assert statuses(incident) == {
        "inventory": "SUPPRESSED_RESTART",
        "order": "ROOT_CAUSE",
    }
    assert incident.status.value == "OPEN"
    assert incident.root_cause_service == "order"


def test_equal_error_rate_is_still_suppressed():
    # A feltétel "nem nagyobb": az egyenlőség elnyomást jelent
    graph = make_graph(
        {"order": (0.18, 80), "inventory": (0.18, 55)},
        [("order", "inventory")],
    )
    incident = correlate_sync(
        [make_alert("inventory"), make_alert("order")],
        graph,
        [restarting("inventory"), healthy("order")],
    )
    assert statuses(incident)["order"] == "SUPPRESSED_DOWNSTREAM"


def test_caller_with_hard_error_rate_stays_real():
    graph = make_graph(
        {"order": (0.45, 80), "inventory": (0.20, 55)},
        [("order", "inventory")],
    )
    incident = correlate_sync(
        [make_alert("inventory"), make_alert("order")],
        graph,
        [restarting("inventory"), healthy("order")],
    )
    assert statuses(incident)["order"] == "ROOT_CAUSE"


def test_caller_with_real_and_suppressed_dependencies_stays_symptom():
    # order → payment → db (valódi hiba) és order → notification (újraindul)
    graph = make_graph(
        {
            "order": (0.10, 80),
            "payment": (0.20, 45),
            "db": (0.30, 90),
            "notification": (0.12, 30),
        },
        [("order", "payment"), ("payment", "db"), ("order", "notification")],
    )
    incident = correlate_sync(
        [
            make_alert("db"),
            make_alert("payment"),
            make_alert("order"),
            make_alert("notification"),
        ],
        graph,
        [healthy("db"), healthy("payment"), healthy("order"), restarting("notification", 2)],
    )

    assert statuses(incident) == {
        "notification": "SUPPRESSED_RESTART",
        "db": "ROOT_CAUSE",
        "order": "SYMPTOM",
        "payment": "SYMPTOM",
    }
    assert incident.root_cause_service == "db"


def test_suppression_does_not_spread_to_siblings():
    # A gateway két gyereke független: az auth zaja nem érinti az order-t
    graph = make_graph(
        {"gateway": (0.0, 200), "order": (0.08, 80), "auth": (0.08, 95)},
        [("gateway", "order"), ("gateway", "auth")],
    )
    incident = correlate_sync(
        [make_alert("order"), make_alert("auth")],
        graph,
        [healthy("order"), scaling("auth")],
    )

    assert statuses(incident) == {
        "auth": "SUPPRESSED_SCALING",
        "order": "ROOT_CAUSE",
    }


def test_suppression_does_not_spread_to_callees():
    # Az auth (hívó) zaja nem teszi zajjá a hívott profile valódi hibáját
    graph = make_graph(
        {"auth": (0.08, 95), "profile": (0.20, 40)},
        [("auth", "profile")],
    )
    incident = correlate_sync(
        [make_alert("auth"), make_alert("profile")],
        graph,
        [scaling("auth"), healthy("profile")],
    )

    assert statuses(incident) == {
        "auth": "SUPPRESSED_SCALING",
        "profile": "ROOT_CAUSE",
    }
