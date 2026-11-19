"""
tests/conftest.py – közös tesztbeállítások

A tesztek MOCK módban futnak: a collectorok a scenarios/*.json fájlokból dolgoznak,
nincs szükség Redisre, Kialira vagy Kubernetesre. A DATA_MODE-ot itt, az app
importálása ELŐTT állítjuk be, így egy esetleges backend/.env (pl. DATA_MODE=LIVE)
sem írja felül (a környezeti változó erősebb a .env fájlnál).
"""

import os

os.environ["DATA_MODE"] = "MOCK"
