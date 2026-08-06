"""
Первичное наполнение БД: реестр буёв и ретроспектива наблюдений.

Ретроспектива нужна для того, чтобы вкладка «История» (7/30/90 суток) имела
содержательные данные сразу после развёртывания. Это тестовый набор данных,
что прямо допускается п. 5.3 Технического задания. Оперативные данные
поступают только через эндпоинт /api/uplink — то есть по реальному тракту.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import db

# Реестр буёв. Координаты — акватория казахстанского сектора Каспийского моря.
DEVICES: list[dict] = [
    # dev_eui,           code,    name,                          lat,    lng,    базовые значения
    ("70b3d57ed0060001", "BY-01", "Актау — офшор юг", 43.68, 50.75, 8.12, 14.3, 7.8, 12.4, 24.6, 286, 0.04),
    ("70b3d57ed0060002", "BY-02", "Платформа CK-2", 44.05, 50.42, 7.62, 13.8, 4.2, 28.6, 24.1, 198, 0.38),
    ("70b3d57ed0060003", "BY-03", "Офшор Мангыстау", 44.42, 50.08, 8.24, 12.9, 8.6, 6.2, 25.1, 312, 0.02),
    ("70b3d57ed0060004", "BY-04", "Кашаган — зона А", 45.22, 52.18, 8.10, 13.0, 7.5, 10.0, 24.5, 280, 0.03),
    ("70b3d57ed0060005", "BY-05", "Актау — офшор север", 44.00, 50.60, 8.18, 13.1, 8.1, 9.5, 24.8, 299, 0.01),
    ("70b3d57ed0060006", "BY-06", "Глубоководный рубеж", 43.82, 50.28, 8.09, 14.7, 7.4, 7.8, 24.4, 278, 0.03),
    ("70b3d57ed0060007", "BY-07", "Каламкас — акватория", 45.48, 52.62, 7.71, 11.8, 5.1, 31.2, 23.9, 205, 0.12),
    ("70b3d57ed0060008", "BY-08", "Южный Каспий KZ", 43.25, 50.52, 8.21, 15.2, 8.9, 5.4, 25.3, 320, 0.01),
    ("70b3d57ed0060009", "BY-09", "Западный шельф", 43.90, 49.82, 8.15, 14.0, 7.6, 8.2, 24.9, 291, 0.03),
    ("70b3d57ed006000a", "BY-10", "Центральная банка", 44.30, 50.65, 8.07, 13.6, 7.2, 11.0, 24.5, 275, 0.04),
    ("70b3d57ed006000b", "BY-11", "Промысловый р-н №3", 44.75, 51.10, 7.89, 12.2, 6.1, 18.5, 24.2, 242, 0.07),
    ("70b3d57ed006000c", "BY-12", "Жетыбай — акватория", 43.52, 50.88, 8.22, 14.8, 8.3, 7.0, 25.0, 308, 0.02),
    ("70b3d57ed006000d", "BY-13", "Северный сектор KZ", 45.32, 51.55, 8.11, 11.4, 7.9, 9.8, 24.7, 284, 0.03),
    ("70b3d57ed006000e", "BY-14", "Дальний офшор", 43.78, 49.52, 8.19, 15.5, 8.5, 4.8, 25.2, 315, 0.01),
    ("70b3d57ed006000f", "BY-15", "Шельф Дунга", 45.05, 51.78, 8.28, 11.9, 9.1, 5.2, 25.4, 328, 0.01),
    ("70b3d57ed0060010", "BY-16", "Северная банка Кашаган", 45.72, 52.90, 8.14, 12.5, 7.7, 8.9, 24.8, 290, 0.02),
]

BASELINE: dict[str, tuple] = {row[0]: row[5:] for row in DEVICES}

HISTORY_DAYS = 90
HISTORY_STEP_MINUTES = 60


def seed_devices() -> None:
    db.executemany(
        "INSERT OR IGNORE INTO devices (dev_eui, code, name, lat, lng) VALUES (?, ?, ?, ?, ?)",
        [(row[0], row[1], row[2], row[3], row[4]) for row in DEVICES],
    )


def seed_history(days: int = HISTORY_DAYS, step_minutes: int = HISTORY_STEP_MINUTES) -> int:
    """Генерирует ретроспективу наблюдений с суточной и сезонной динамикой."""
    row = db.query_one("SELECT COUNT(*) AS n FROM measurements")
    if row and row["n"] > 0:
        return 0

    rng = random.Random(20260805)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    steps = int(days * 24 * 60 / step_minutes)
    batch: list[tuple] = []

    for dev_eui, code, name, lat, lng, ph0, temp0, do0, turb0, ec0, orp0, hc0 in DEVICES:
        for i in range(steps, 0, -1):
            ts = now - timedelta(minutes=i * step_minutes)
            # суточный цикл: температура и кислород колеблются в течение суток
            hour_phase = math.sin((ts.hour / 24.0) * 2 * math.pi - math.pi / 2)
            # сезонный тренд по прогреву воды за период ретроспективы
            season = (steps - i) / steps

            temp = temp0 + hour_phase * 1.2 + season * 3.5 + rng.gauss(0, 0.35)
            do = do0 - hour_phase * 0.4 - season * 0.6 + rng.gauss(0, 0.25)
            ph = ph0 + hour_phase * 0.05 + rng.gauss(0, 0.04)
            ec = ec0 + rng.gauss(0, 0.12)
            turb = max(0.5, turb0 + rng.gauss(0, turb0 * 0.15))
            orp = orp0 + rng.gauss(0, 6)
            hc = max(0.0, hc0 + rng.gauss(0, max(hc0 * 0.25, 0.004)))
            tds = ec * 520 + rng.gauss(0, 40)
            battery = max(15.0, 100.0 - (steps - i) / steps * 12.0 + rng.gauss(0, 0.3))

            batch.append(
                (
                    dev_eui,
                    ts.isoformat(timespec="seconds"),
                    round(ph, 2),
                    round(temp, 1),
                    round(do, 2),
                    round(turb, 1),
                    round(ec, 1),
                    round(tds, 0),
                    round(orp, 0),
                    round(hc, 3),
                    round(battery, 1),
                    round(-85 - rng.random() * 20, 1),
                    round(2.0 + rng.random() * 6.0, 1),
                    steps - i,
                )
            )

    db.executemany(
        "INSERT INTO measurements "
        "(dev_eui, ts, ph, temp, do_mgl, turb, ec, tds, orp, hc, battery, rssi, snr, fcnt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    return len(batch)


def run() -> dict[str, int]:
    from . import alerts

    seed_devices()
    alerts.seed_thresholds()
    inserted = seed_history()
    return {"devices": len(DEVICES), "measurements": inserted}
