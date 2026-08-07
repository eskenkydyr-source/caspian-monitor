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
    ("70b3d57ed0060001", "BY-01", "Актау — офшор юг", 43.74, 50.715, 8.12, 26.6, 7.1, 12.4, 20.2, 286, 0.04),
    ("70b3d57ed0060002", "BY-02", "Платформа CK-2", 44.547, 49.895, 7.62, 26.3, 4.1, 28.6, 19.7, 198, 0.38),
    ("70b3d57ed0060003", "BY-03", "Офшор Мангыстау", 44.613, 50.063, 8.24, 25.6, 7.4, 6.2, 20.7, 312, 0.02),
    ("70b3d57ed0060004", "BY-04", "Кашаган — зона А", 45.071, 51.037, 8.10, 25.7, 7.0, 10.0, 20.1, 280, 0.03),
    ("70b3d57ed0060005", "BY-05", "Актау — офшор север", 43.95, 50.9, 8.18, 25.7, 7.2, 9.5, 20.4, 299, 0.01),
    ("70b3d57ed0060006", "BY-06", "Глубоководный рубеж", 44.374, 49.803, 8.09, 26.9, 7.0, 7.8, 20.0, 278, 0.03),
    ("70b3d57ed0060007", "BY-07", "Каламкас — акватория", 44.939, 51.236, 7.71, 24.8, 4.7, 31.2, 19.5, 205, 0.12),
    ("70b3d57ed0060008", "BY-08", "Южный Каспий KZ", 43.309, 50.953, 8.21, 27.3, 7.5, 5.4, 20.9, 320, 0.01),
    ("70b3d57ed0060009", "BY-09", "Западный шельф", 44.829, 50.01, 8.15, 26.4, 7.0, 8.2, 20.5, 291, 0.03),
    ("70b3d57ed006000a", "BY-10", "Центральная банка", 44.75, 50.318, 8.07, 26.1, 6.9, 11.0, 20.1, 275, 0.04),
    ("70b3d57ed006000b", "BY-11", "Промысловый р-н №3", 44.764, 51.184, 7.89, 25.1, 5.4, 18.5, 19.8, 242, 0.07),
    ("70b3d57ed006000c", "BY-12", "Жетыбай — акватория", 43.603, 50.856, 8.22, 27.0, 7.3, 7.0, 20.6, 308, 0.02),
    ("70b3d57ed006000d", "BY-13", "Северный сектор KZ", 44.908, 50.565, 8.11, 24.5, 7.1, 9.8, 20.3, 284, 0.03),
    ("70b3d57ed006000e", "BY-14", "Дальний офшор", 43.906, 50.751, 8.19, 27.5, 7.4, 4.8, 20.8, 315, 0.01),
    ("70b3d57ed006000f", "BY-15", "Шельф Дунга", 43.469, 50.85, 8.28, 24.9, 7.6, 5.2, 21.0, 328, 0.01),
    ("70b3d57ed0060010", "BY-16", "Северная банка Кашаган", 45.083, 50.718, 8.14, 25.3, 7.1, 8.9, 20.4, 290, 0.02),
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
            tds = ec * 640 + rng.gauss(0, 40)
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
