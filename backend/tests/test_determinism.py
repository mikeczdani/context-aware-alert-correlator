"""
tests/test_determinism.py – a motor kimenetének ismételhetősége

A szakdolgozati mérésekhez ugyanarra a bemenetre mindig ugyanazt a kimenetet kell
adni. A veszélyforrások:
  - a riasztások beérkezési sorrendje,
  - a sztring-halmazok bejárási sorrendje (a Python hash-randomizálása folyamatonként
    változik),
  - a döntetlenek (azonos hibaarányú gyökérjelöltek, azonos méretű csoportok).
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import (
    correlate_sync,
    healthy,
    make_alert,
    make_graph,
    run_scenario,
    snapshot,
    statuses,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ["pod_restart_noise", "cascading_db_timeout", "mixed_storm"]


# ---------------------------------------------------------------------------
# Sorrendfüggetlenség
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(10))
def test_result_does_not_depend_on_alert_order(seed):
    baseline = snapshot(run_scenario("mixed_storm"))

    def shuffled(alerts):
        order = list(alerts)
        random.Random(seed).shuffle(order)
        return order

    assert snapshot(run_scenario("mixed_storm", alert_order=shuffled)) == baseline


# ---------------------------------------------------------------------------
# Hash-randomizálás elleni védelem: külön folyamatok, eltérő PYTHONHASHSEED-del
# ---------------------------------------------------------------------------

_SNIPPET = """
import json
from tests.helpers import run_scenario, snapshot
names = ["pod_restart_noise", "cascading_db_timeout", "mixed_storm"]
print(json.dumps({n: snapshot(run_scenario(n)) for n in names}))
"""


@pytest.mark.parametrize("hash_seed", ["0", "1", "42"])
def test_result_does_not_depend_on_python_hash_seed(hash_seed):
    env = {
        **os.environ,
        "PYTHONHASHSEED": hash_seed,
        "DATA_MODE": "MOCK",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    in_other_process = json.loads(completed.stdout.strip().splitlines()[-1])
    here = {name: snapshot(run_scenario(name)) for name in SCENARIOS}
    assert in_other_process == here


# ---------------------------------------------------------------------------
# Döntetlenek
# ---------------------------------------------------------------------------

def test_equal_error_rate_root_candidates_resolve_alphabetically():
    graph = make_graph(
        {"gateway": (0.05, 100), "x": (0.30, 50), "y": (0.30, 50)},
        [("gateway", "x"), ("gateway", "y")],
    )
    incident = correlate_sync(
        [make_alert("gateway"), make_alert("x"), make_alert("y")],
        graph,
        [healthy("gateway"), healthy("x"), healthy("y")],
    )

    assert statuses(incident) == {
        "gateway": "SYMPTOM",
        "x": "ROOT_CAUSE",
        "y": "SYMPTOM",
    }


def test_primary_root_cause_is_root_of_largest_group():
    # a1 → a2 → a3 (3 szolgáltatásos csoport, gyökér: a3) és egy magányos, nagyobb
    # hibaarányú b1: a méret előbbre való, mint a hibaarány
    graph = make_graph(
        {"a1": (0.05, 100), "a2": (0.10, 80), "a3": (0.20, 60), "b1": (0.35, 20)},
        [("a1", "a2"), ("a2", "a3")],
    )
    incident = correlate_sync(
        [make_alert(s) for s in ("a1", "a2", "a3", "b1")],
        graph,
        [healthy(s) for s in ("a1", "a2", "a3", "b1")],
    )

    assert incident.root_cause_service == "a3"
    assert statuses(incident) == {
        "a1": "SYMPTOM",
        "a2": "SYMPTOM",
        "a3": "ROOT_CAUSE",
        "b1": "ROOT_CAUSE",
    }


@pytest.mark.parametrize(
    "rates, expected_primary",
    [
        ({"c1": 0.20, "c2": 0.30}, "c2"),  # azonos méret → nagyobb hibaarány nyer
        ({"c1": 0.25, "c2": 0.25}, "c1"),  # teljes döntetlen → névsorban első
    ],
)
def test_primary_root_cause_tie_breaks(rates, expected_primary):
    graph = make_graph({name: (rate, 50) for name, rate in rates.items()}, [])
    incident = correlate_sync(
        [make_alert(name) for name in rates],
        graph,
        [healthy(name) for name in rates],
    )
    assert incident.root_cause_service == expected_primary
