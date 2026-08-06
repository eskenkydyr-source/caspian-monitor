"""
Движок пороговых алертов.

Каждое принятое измерение проверяется по таблице порогов. Пороговые значения
по умолчанию опираются на нормативы качества морских вод и практику
экологического мониторинга нефтепромысловых акваторий; они редактируемы
через API и хранятся в БД, а не в коде.

Уровни:
  warning — параметр вышел за границу нормы, требуется наблюдение;
  alert   — параметр вышел за критическую границу, требуется реагирование.
"""

from __future__ import annotations

from typing import Any

from . import db

# param -> (человекочитаемое имя, единица, warn_min, warn_max, alert_min, alert_max)
DEFAULT_THRESHOLDS: dict[str, tuple[str, str, float | None, float | None, float | None, float | None]] = {
    "ph": ("Кислотность", "pH", 7.8, 8.5, 7.5, 9.0),
    "temp": ("Температура", "°C", 0.0, 28.0, -2.0, 32.0),
    "do": ("Растворённый кислород", "мг/л", 6.0, None, 4.0, None),
    "turb": ("Мутность", "NTU", None, 15.0, None, 25.0),
    "ec": ("Удельная электропроводность", "мСм/см", 22.0, 27.0, 20.0, 30.0),
    "hc": ("Сумма углеводородов", "мг/л", None, 0.05, None, 0.10),
    "orp": ("Окислительно-восстановительный потенциал", "мВ", 200.0, 400.0, 150.0, 500.0),
    "battery": ("Заряд батареи", "%", 25.0, None, 10.0, None),
}

# Соответствие поля измерения имени параметра порога
FIELD_MAP = {
    "ph": "ph",
    "temp": "temp",
    "do_mgl": "do",
    "turb": "turb",
    "ec": "ec",
    "hc": "hc",
    "orp": "orp",
    "battery": "battery",
}


def seed_thresholds() -> None:
    """Записывает пороги по умолчанию, если таблица пуста."""
    row = db.query_one("SELECT COUNT(*) AS n FROM thresholds")
    if row and row["n"] > 0:
        return
    db.executemany(
        "INSERT INTO thresholds (param, label, unit, warn_min, warn_max, alert_min, alert_max) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(param, *values) for param, values in DEFAULT_THRESHOLDS.items()],
    )


def get_thresholds() -> dict[str, dict[str, Any]]:
    return {row["param"]: row for row in db.query("SELECT * FROM thresholds")}


def update_threshold(param: str, values: dict[str, Any]) -> dict[str, Any] | None:
    fields = [f for f in ("warn_min", "warn_max", "alert_min", "alert_max") if f in values]
    if not fields:
        return db.query_one("SELECT * FROM thresholds WHERE param = ?", (param,))
    assignments = ", ".join(f"{f} = ?" for f in fields)
    params = tuple(values[f] for f in fields) + (param,)
    db.execute(f"UPDATE thresholds SET {assignments} WHERE param = ?", params)
    return db.query_one("SELECT * FROM thresholds WHERE param = ?", (param,))


def evaluate(dev_eui: str, ts: str, measurement: dict[str, Any], device_name: str = "") -> list[dict[str, Any]]:
    """
    Проверяет измерение по порогам и записывает сработавшие алерты.
    Возвращает список созданных алертов.
    """
    thresholds = get_thresholds()
    created: list[dict[str, Any]] = []

    for field, param in FIELD_MAP.items():
        value = measurement.get(field)
        rule = thresholds.get(param)
        if value is None or rule is None:
            continue

        breach = _check(value, rule)
        if breach is None:
            continue

        level, limit, direction = breach
        label = rule["label"]
        unit = rule["unit"]
        word = "превысил" if direction == "high" else "опустился ниже"
        message = (
            f"{device_name or dev_eui}: параметр «{label}» {word} "
            f"{'верхнюю' if direction == 'high' else 'нижнюю'} "
            f"{'критическую' if level == 'alert' else 'предупредительную'} границу — "
            f"{value} {unit} при пороге {limit} {unit}"
        )

        if _is_duplicate(dev_eui, param, level):
            continue

        alert_id = db.execute(
            "INSERT INTO alerts (dev_eui, ts, param, value, threshold, level, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dev_eui, ts, param, float(value), float(limit), level, message),
        )
        created.append(
            {
                "id": alert_id,
                "dev_eui": dev_eui,
                "ts": ts,
                "param": param,
                "value": value,
                "threshold": limit,
                "level": level,
                "message": message,
            }
        )

    return created


def _check(value: float, rule: dict[str, Any]) -> tuple[str, float, str] | None:
    """Возвращает (уровень, нарушенный порог, направление) либо None."""
    if rule["alert_max"] is not None and value > rule["alert_max"]:
        return "alert", rule["alert_max"], "high"
    if rule["alert_min"] is not None and value < rule["alert_min"]:
        return "alert", rule["alert_min"], "low"
    if rule["warn_max"] is not None and value > rule["warn_max"]:
        return "warning", rule["warn_max"], "high"
    if rule["warn_min"] is not None and value < rule["warn_min"]:
        return "warning", rule["warn_min"], "low"
    return None


def _is_duplicate(dev_eui: str, param: str, level: str) -> bool:
    """
    Подавление дребезга: если по этому устройству и параметру уже есть
    неподтверждённый алерт того же уровня — новый не создаём.
    """
    row = db.query_one(
        "SELECT id FROM alerts WHERE dev_eui = ? AND param = ? AND level = ? AND acked = 0 "
        "ORDER BY ts DESC LIMIT 1",
        (dev_eui, param, level),
    )
    return row is not None


def device_status(measurement: dict[str, Any] | None, seconds_since_seen: float | None, offline_after: int) -> str:
    """Сводный статус буя для карты и списка устройств."""
    if measurement is None or seconds_since_seen is None or seconds_since_seen > offline_after:
        return "offline"

    thresholds = get_thresholds()
    worst = "online"
    for field, param in FIELD_MAP.items():
        value = measurement.get(field)
        rule = thresholds.get(param)
        if value is None or rule is None:
            continue
        breach = _check(value, rule)
        if breach is None:
            continue
        if breach[0] == "alert":
            return "alert"
        worst = "warning"
    return worst
