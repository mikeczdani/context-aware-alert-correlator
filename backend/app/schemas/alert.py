"""
schemas/alert.py – Bejövő riasztás adatmodellek (2. Réteg)

Ez a fájl a Prometheus Alertmanager által küldött webhook payload
Pydantic modelljeit tartalmazza.

Adatfolyam:
  Prometheus Alertmanager
    → POST /api/v1/webhook/alerts
      → WebhookPayload (validálás)
        → RawAlert lista (feldolgozás az engine-ben)
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from pydantic import BaseModel, Field


class RawAlert(BaseModel):
    """
    Egyetlen Prometheus riasztás a webhook payload-ból.

    Mezők:
      alert_name:  A Prometheus rule neve (pl. "HighErrorRate", "HighLatency").
                   Az "alertname" aliast azért adjuk meg, mert a Prometheus
                   így küldi a JSON-ben, de Pythonban az alert_name szebb és
                   biztonságosabb (nem foglalt kulcsszó, jobb autocomplete).
      service:     Az érintett mikroszolgáltatás neve – ez köti össze a
                   topology.py KialiNode modelljével és a k8s_collector K8sContext-jével.
                   Pl. "payment", "db", "order".
      namespace:   Kubernetes névtér, ahol a szolgáltatás fut.
      severity:    Prometheus-oldali súlyosság ("warning" vagy "critical").
                   Fontos: ez NEM azonos a mi engine-ünk SeverityScore-jával,
                   amit a severity_scorer.py számol gráf-centralitás alapján.
      status:      "firing" = aktív riasztás, "resolved" = megszűnt.
                   A motorunk csak a "firing" riasztásokat dolgozza fel.
      starts_at:   Mikor kezdődött a riasztás – a 60 mp-es korrelációs időablak
                   számításának kiindulópontja.
      labels:      Eredeti Prometheus label-ek szabad kulcs-érték párokban.
                   Pl. {"job": "payment-svc", "env": "production"}.
      annotations: Prometheus annotációk, általában emberi leírás.
                   Pl. {"summary": "HTTP error rate above 5%"}.
    """

    alert_name: str = Field(..., alias="alertname")
    service: str
    namespace: str = "default"
    severity: str = "warning"
    status: str = "firing"
    starts_at: datetime = Field(default_factory=datetime.utcnow)
    labels: Dict[str, str] = {}
    annotations: Dict[str, str] = {}

    # populate_by_name=True: Python-kódból alert_name-mel is létrehozható a modell,
    # ne csak az "alertname" JSON alias-szal. Ez kell a simulator/chaos_runner.py-hoz,
    # ahol Pythonból hozunk létre RawAlert objektumokat, nem JSON-ból.
    model_config = {"populate_by_name": True}


class WebhookPayload(BaseModel):
    """
    A Prometheus Alertmanager által küldött teljes webhook POST body.

    Az Alertmanager egyszerre több riasztást is küldhet egy POST-ban
    (ha több rule tüzel egyszerre), ezért az alerts mező lista.

    Mezők:
      version:      Az Alertmanager webhook protokoll verziója (általában "4").
      group_key:    Az összetartozó riasztások Alertmanager-oldali csoportkulcsa.
                    Pl. "{alertname=\"HighErrorRate\"}:{namespace=\"default\"}".
      status:       A csoport összesített státusza ("firing" / "resolved").
      receiver:     A webhook receiver neve az Alertmanager konfigurációban.
      alerts:       A tényleges riasztások listája – ezeket adja át a webhook
                    route a collectors-nek és az engine-nek.
      common_labels:      Az összes riasztásra közös Prometheus label-ek.
      common_annotations: Az összes riasztásra közös Prometheus annotációk.
      external_url: Az Alertmanager saját UI URL-je (nem használjuk, de az
                    Alertmanager mindig elküldi).
    """

    version: str = "4"
    group_key: str = ""
    status: str = "firing"
    receiver: str = "thesis-backend"
    alerts: List[RawAlert] = []
    common_labels: Dict[str, str] = {}
    common_annotations: Dict[str, str] = {}
    external_url: str = ""
