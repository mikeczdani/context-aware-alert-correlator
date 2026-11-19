"""
tests/test_scenarios.py – a scenarios/*.json forgatókönyvek regressziós tesztjei

Mindegyik teszt a valódi collectorokkal és a valódi correlate()-tel fut, és a
forgatókönyv saját expected_outcome mezőjét (ahol van) veti össze az eredménnyel.
Így a JSON a "kétirányú szerződés": ha a motor viselkedése megváltozik, a teszt
elbukik, és ha a JSON változik, az eltérés azonnal kiderül.
"""

from tests.helpers import load_scenario, run_scenario, scenario_alerts, statuses


def test_healthy_baseline_has_no_firing_alerts():
    assert scenario_alerts(load_scenario("healthy_baseline")) == []


def test_pod_restart_noise_is_fully_suppressed():
    expected = load_scenario("pod_restart_noise")["expected_outcome"]
    incident = run_scenario("pod_restart_noise")
    node_status = statuses(incident)

    # Nincs valódi incidens
    assert incident.status.value == "SUPPRESSED"
    assert incident.root_cause_service == expected["root_cause"]  # None
    assert set(node_status) == set(expected["suppressed"])

    # Az inventory a saját újraindulása miatt, az order az inventory következményeként
    assert node_status == {
        "inventory": "SUPPRESSED_RESTART",
        "order": "SUPPRESSED_DOWNSTREAM",
    }
    order = next(n for n in incident.affected_nodes if n.service == "order")
    assert "inventory" in order.suppression_reason

    assert incident.raw_alert_count == 2
    assert incident.suppressed_count == 2
    assert incident.correlated_count == 0
    assert incident.summary == "Suppressed as noise: inventory, order."


def test_cascading_db_timeout_finds_db_as_root_cause():
    expected = load_scenario("cascading_db_timeout")["expected_outcome"]
    incident = run_scenario("cascading_db_timeout")
    node_status = statuses(incident)

    assert incident.status.value == "OPEN"
    assert incident.root_cause_service == expected["root_cause"] == "db"
    assert node_status["db"] == "ROOT_CAUSE"

    symptoms = {s for s, status in node_status.items() if status == "SYMPTOM"}
    assert symptoms == set(expected["symptoms"])

    # Nincs K8s esemény, tehát zajszűrés sem történhet
    assert expected["suppressed"] == []
    assert not any(status.startswith("SUPPRESSED") for status in node_status.values())

    assert incident.suppressed_count == 0
    assert incident.summary == (
        "'db' caused cascading failures in api-gateway, inventory, order, payment."
    )


def test_mixed_storm_separates_three_independent_failures_from_noise():
    expected = load_scenario("mixed_storm")["expected_outcome"]
    incident = run_scenario("mixed_storm")
    node_status = statuses(incident)

    # Zajok: szolgáltatásonként a várt elnyomási típussal
    for service, status in expected["suppressed"].items():
        assert node_status[service] == status

    # Három független csoport, mindegyik a saját gyökérokával
    for group in expected["groups"]:
        assert node_status[group["root_cause"]] == "ROOT_CAUSE"
        for symptom in group["symptoms"]:
            assert node_status[symptom] == "SYMPTOM"

    # Pontosan az várt szolgáltatások szerepelnek (a nem riasztók nem)
    expected_services = (
        set(expected["suppressed"])
        | {g["root_cause"] for g in expected["groups"]}
        | {s for g in expected["groups"] for s in g["symptoms"]}
    )
    assert set(node_status) == expected_services
    assert not (set(expected["not_alerting"]) & set(node_status))

    # Számlálók (a lejárt riasztás nem számít bele)
    assert incident.raw_alert_count == expected["raw_alert_count"] == 12
    assert incident.suppressed_count == expected["suppressed_count"] == 3
    assert incident.correlated_count == expected["correlated_count"] == 7

    # ARR: egyetlen incidens keletkezik, ez 1 korrelált incidenst jelent a számlálóban
    arr = round((1 - 1 / incident.raw_alert_count) * 100, 2)
    assert arr == expected["arr_pct"]

    # Elsődleges gyökérok: a legnagyobb csoporté
    assert incident.status.value == "OPEN"
    assert incident.root_cause_service == "db-main"

    # Az összefoglaló csoportonként írja le a hibákat (nem egy gyökérokhoz sorol mindent)
    assert incident.summary == (
        "'db-main' caused cascading failures in inventory, order, payment. "
        "'search-index' caused cascading failures in search. "
        "'shipping' is failing (no downstream symptoms detected). "
        "Suppressed as noise: auth, catalog, notification."
    )
