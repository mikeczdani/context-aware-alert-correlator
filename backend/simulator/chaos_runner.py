"""
simulator/chaos_runner.py – Szintetikus hibageneráló / forgatókönyv-lejátszó (1. Réteg)

Ez a szkript a Prometheus Alertmanagert utánozza MOCK módban: beolvassa a
backend/scenarios/*.json forgatókönyveket, és a bennük lévő "alerts" listát
Alertmanager-kompatibilis webhook payloadként HTTP POST-tal elküldi a futó
backend valós webhook végpontjára:

    POST /api/v1/webhook/alerts?scenario_id=<id>

Így a teljes feldolgozási lánc (webhook → Kiali gráf lekérés → zajszűrés →
gráf-korreláció + RCA → Redis mentés → SSE értesítés) ugyanazon az úton fut le,
mint éles Alertmanager-riasztás esetén. Ez a /api/v1/simulate/{scenario}
végpont külső, parancssori megfelelője – determinisztikus szakdolgozati
mérésekhez, Kubernetes-klaszter nélkül.

Előfeltétel: a backend fusson (pl. `uvicorn app.main:app --reload`) és a Redis
is elérhető legyen (docker-compose up -d).

Használat (a backend/ mappából futtatva):

    # Elérhető forgatókönyvek listázása
    python -m simulator.chaos_runner --list

    # Egyetlen forgatókönyv lejátszása
    python -m simulator.chaos_runner --scenario cascading_db_timeout

    # Reset, majd az összes forgatókönyv sorban, 2 mp szünettel
    python -m simulator.chaos_runner --all --reset --delay 2

    # Más backend cím
    python -m simulator.chaos_runner --scenario mixed_storm --url http://localhost:8000

A szkript szándékosan NEM importál az app/ csomagból – külső kliensként
viselkedik, pontosan úgy, ahogy egy valódi Alertmanager tenné.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# ---------------------------------------------------------------------------
# Konfiguráció
# ---------------------------------------------------------------------------

# A szkript a backend/simulator/ mappában van, a scenarios/ a backend/ alatt.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SCENARIOS_DIR = _BACKEND_ROOT / "scenarios"

DEFAULT_BASE_URL = "http://localhost:8000"
WEBHOOK_PATH = "/api/v1/webhook/alerts"
METRICS_PATH = "/api/v1/metrics/summary"
RESET_PATH = "/api/v1/incidents/reset"
HEALTH_PATH = "/health"

# Forgatókönyv ID → fájlnév leképezés.
# Megegyezik a backend kiali_collector.py SCENARIO_FILE_MAP-jével – ez a
# szkript egyetlen helyen tartott tükörpéldánya, hogy a --list és a fájlnév
# fallback működjön anélkül, hogy az app-ot importálnánk.
SCENARIO_FILE_MAP: Dict[str, str] = {
    "healthy_baseline":     "00_healthy_baseline.json",
    "pod_restart_noise":    "01_pod_restart_noise.json",
    "cascading_db_timeout": "02_cascading_latency.json",
    "mixed_storm":          "03_mixed_failure.json",
}

# A lejátszás alapértelmezett sorrendje az --all kapcsolóhoz: előbb az egészséges
# alapállapot, majd a hibaforgatókönyvek növekvő súlyosság szerint.
SCENARIO_ORDER: List[str] = [
    "healthy_baseline",
    "pod_restart_noise",
    "cascading_db_timeout",
    "mixed_storm",
]


# ---------------------------------------------------------------------------
# Forgatókönyv-betöltés
# ---------------------------------------------------------------------------

def load_scenario(scenario_id: str) -> Dict[str, Any]:
    """
    Betölti a scenario_id-hoz tartozó JSON fájlt a scenarios/ mappából.

    Hibát dob, ha a scenario_id ismeretlen vagy a fájl hiányzik – így a szkript
    azonnal, érthető üzenettel áll le elírás esetén.
    """
    if scenario_id not in SCENARIO_FILE_MAP:
        raise ValueError(
            f"Ismeretlen scenario_id: '{scenario_id}'. "
            f"Elérhető: {', '.join(SCENARIO_FILE_MAP)}"
        )

    filepath = _SCENARIOS_DIR / SCENARIO_FILE_MAP[scenario_id]
    if not filepath.exists():
        raise FileNotFoundError(f"A forgatókönyv-fájl nem található: {filepath}")

    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def _strip_comment_keys(obj: Any) -> Any:
    """
    Rekurzívan eltávolítja a dokumentációs "_comment" (és bármilyen "_"
    kezdetű) kulcsokat a scenario JSON-ból, hogy a backend Pydantic
    validációja ne essen el rajtuk.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_comment_keys(v)
            for k, v in obj.items()
            if not k.startswith("_")
        }
    if isinstance(obj, list):
        return [_strip_comment_keys(item) for item in obj]
    return obj


def build_webhook_payload(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """
    A scenario JSON "alerts" listájából összeállít egy Alertmanager-kompatibilis
    webhook payloadot (a backend WebhookPayload modelljéhez illeszkedve).

    Csak a "firing" riasztásokat küldi tovább – a backend amúgy is kiszűri a
    többit, de így a kimeneti log pontosabb.
    """
    raw_alerts = _strip_comment_keys(scenario.get("alerts", []))
    firing = [a for a in raw_alerts if a.get("status", "firing") == "firing"]

    return {
        "version": "4",
        "group_key": f"chaos-runner:{scenario.get('scenario_id', 'unknown')}",
        "status": "firing",
        "receiver": "thesis-backend",
        "alerts": firing,
        "common_labels": {"source": "chaos_runner"},
        "common_annotations": {},
        "external_url": "",
    }


# ---------------------------------------------------------------------------
# HTTP műveletek
# ---------------------------------------------------------------------------

def check_backend(client: httpx.Client) -> None:
    """Ellenőrzi, hogy a backend elérhető-e, mielőtt bármit küldenénk."""
    try:
        resp = client.get(HEALTH_PATH, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        print(f"[OK] Backend elérhető – status={data.get('status')}, "
              f"redis={data.get('redis')}")
    except Exception as e:
        print(f"[HIBA] A backend nem érhető el a {client.base_url} címen: {e}",
              file=sys.stderr)
        print("Indítsd el a backendet (uvicorn app.main:app --reload) "
              "és a Redis-t (docker-compose up -d), majd próbáld újra.",
              file=sys.stderr)
        raise SystemExit(1)


def reset_system(client: httpx.Client) -> None:
    """Törli a Redis-t (incidensek, topológia, metrikák, időablak) a méréshez."""
    resp = client.delete(RESET_PATH, timeout=10.0)
    resp.raise_for_status()
    print("[RESET] Redis törölve – tiszta kiindulási állapot.")


def send_scenario(client: httpx.Client, scenario_id: str) -> Optional[Dict[str, Any]]:
    """
    Lejátszik egy forgatókönyvet: betölti a JSON-t, elküldi a webhook
    payloadot, és kiírja a backend válaszát. Visszaadja a válasz JSON-t.
    """
    scenario = load_scenario(scenario_id)
    # A JSON-ben lévő scenario_id az elsődleges; ha hiányzik, a CLI-ből jövőt
    # használjuk – ez a scenario_id query paraméterként kell a backendnek,
    # hogy MOCK módban a megfelelő Kiali gráfot töltse be.
    effective_id = scenario.get("scenario_id", scenario_id)

    payload = build_webhook_payload(scenario)
    alert_count = len(payload["alerts"])

    desc = scenario.get("description", "")
    print(f"\n>> Forgatókönyv: {scenario_id}  ({alert_count} firing alert)")
    if desc:
        print(f"   {desc}")

    resp = client.post(
        WEBHOOK_PATH,
        params={"scenario_id": effective_id},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    result = resp.json()

    incident_id = result.get("incident_id")
    summary = result.get("summary", "")
    if incident_id:
        print(f"   [OK] incident={incident_id}")
        if summary:
            print(f"        {summary}")
    else:
        print(f"   [OK] Nincs incidens – {summary or 'minden riasztás elnyomva vagy nincs firing alert.'}")

    return result


def print_metrics(client: httpx.Client) -> None:
    """Lekéri és kiírja az aktuális rendszer-metrikákat (ARR %)."""
    resp = client.get(METRICS_PATH, timeout=10.0)
    resp.raise_for_status()
    m = resp.json()
    print("\n=== RENDSZER-METRIKÁK (stats:metrics) ===")
    print(f"  Nyers riasztások összesen : {m.get('raw_alerts_total', 0)}")
    print(f"  Elnyomott zaj összesen    : {m.get('suppressed_total', 0)}")
    print(f"  Korrelált incidensek      : {m.get('correlated_incidents_total', 0)}")
    print(f"  ARR (Alert Reduction Rate): {m.get('alert_reduction_rate_pct', 0.0)}%")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chaos_runner",
        description=(
            "Szintetikus forgatókönyv-lejátszó – Alertmanager webhookokat küld "
            "a backendnek a scenarios/*.json fájlokból."
        ),
    )
    parser.add_argument(
        "--scenario", "-s",
        help=f"Lejátszandó forgatókönyv ID-ja. Egyike: {', '.join(SCENARIO_FILE_MAP)}",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Az összes forgatókönyv lejátszása sorban (SCENARIO_ORDER szerint).",
    )
    parser.add_argument(
        "--reset", "-r",
        action="store_true",
        help="Redis törlése (DELETE /incidents/reset) a lejátszás ELŐTT.",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="Elérhető forgatókönyvek listázása, majd kilépés.",
    )
    parser.add_argument(
        "--url", "-u",
        default=DEFAULT_BASE_URL,
        help=f"A backend base URL-je (alapértelmezett: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=1.0,
        help="Másodpercben megadott szünet az --all forgatókönyvek között "
             "(alapértelmezett: 1.0).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.list:
        print("Elérhető forgatókönyvek:")
        for sid in SCENARIO_ORDER:
            print(f"  {sid:<22} → {SCENARIO_FILE_MAP[sid]}")
        return 0

    if not args.scenario and not args.all:
        print("Adj meg egy forgatókönyvet (--scenario <id>) vagy használd az "
              "--all kapcsolót. Lista: --list", file=sys.stderr)
        return 2

    # A lejátszandó forgatókönyvek listája
    if args.all:
        scenarios = list(SCENARIO_ORDER)
    else:
        if args.scenario not in SCENARIO_FILE_MAP:
            print(f"[HIBA] Ismeretlen forgatókönyv: '{args.scenario}'. "
                  f"Elérhető: {', '.join(SCENARIO_FILE_MAP)}", file=sys.stderr)
            return 2
        scenarios = [args.scenario]

    with httpx.Client(base_url=args.url) as client:
        check_backend(client)

        if args.reset:
            reset_system(client)

        for i, scenario_id in enumerate(scenarios):
            try:
                send_scenario(client, scenario_id)
            except httpx.HTTPStatusError as e:
                print(f"   [HIBA] A backend {e.response.status_code} státusszal "
                      f"válaszolt: {e.response.text}", file=sys.stderr)
            except (ValueError, FileNotFoundError) as e:
                print(f"   [HIBA] {e}", file=sys.stderr)

            # Szünet a forgatókönyvek között (az utolsó után nem várunk)
            if args.delay > 0 and i < len(scenarios) - 1:
                time.sleep(args.delay)

        print_metrics(client)

    print("\nKész.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
