"""
Caspian Monitor — серверная часть системы экологического мониторинга
акватории Каспийского моря на базе сети LoRaWAN.

Основной тракт данных:

    буй (STM32 + SX1262)
        -> LoRaWAN-шлюз
        -> сетевой сервер ChirpStack v4
        -> HTTP-интеграция, POST /api/uplink   <-- точка входа этого сервиса
        -> декодирование кадра (app/codec.py)
        -> запись в БД + проверка порогов (app/alerts.py)
        -> REST API -> веб-панель оператора

Эмулятор буя (simulator/simulator.py) подключается к тому же эндпоинту
/api/uplink в том же формате, что и реальный сетевой сервер. За счёт этого
демонстрационный режим проходит по полному тракту, а замена эмулятора на
реальную сеть не требует изменений в коде сервиса.
"""

from __future__ import annotations

import base64
import csv
import io
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts, codec, db, seed

APP_VERSION = "1.0.0"
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
OFFLINE_AFTER_SEC = int(os.environ.get("OFFLINE_AFTER_SEC", "600"))
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")

app = FastAPI(
    title="Caspian Monitor API",
    description=(
        "Открытый программный интерфейс системы экологического мониторинга "
        "акватории Каспийского моря. Приём телеметрии буёв через LoRaWAN, "
        "хранение наблюдений, пороговая сигнализация, выгрузка открытых данных."
    ),
    version=APP_VERSION,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Схемы запросов
# --------------------------------------------------------------------------

class DeviceInfo(BaseModel):
    devEui: str = ""
    deviceName: str = ""


class RxInfo(BaseModel):
    gatewayId: str = ""
    rssi: float | None = None
    snr: float | None = None


class Modulation(BaseModel):
    lora: dict[str, Any] | None = None


class TxInfo(BaseModel):
    frequency: int | None = None
    modulation: Modulation | None = None


class UplinkEvent(BaseModel):
    """Событие uplink в формате HTTP-интеграции ChirpStack v4."""

    deviceInfo: DeviceInfo = Field(default_factory=DeviceInfo)
    devEui: str | None = None  # упрощённая форма для эмулятора и curl
    time: str | None = None
    fPort: int | None = None
    fCnt: int | None = None
    dr: int | None = None
    data: str | None = None  # полезная нагрузка в base64
    hex: str | None = None  # альтернативная форма — HEX
    rxInfo: list[RxInfo] = Field(default_factory=list)
    txInfo: TxInfo | None = None


class DecodeRequest(BaseModel):
    hex: str


class ThresholdUpdate(BaseModel):
    param: str
    warn_min: float | None = None
    warn_max: float | None = None
    alert_min: float | None = None
    alert_max: float | None = None


# --------------------------------------------------------------------------
# Служебное
# --------------------------------------------------------------------------

def require_token(authorization: str = Header(default="")) -> None:
    """Проверка токена приёма данных. Если INGEST_TOKEN не задан — приём открыт."""
    if not INGEST_TOKEN:
        return
    expected = f"Bearer {INGEST_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Недействительный токен приёма данных")


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(ts_value: str | None) -> float | None:
    if not ts_value:
        return None
    return (datetime.now(timezone.utc) - _parse_ts(ts_value)).total_seconds()


@app.on_event("startup")
def on_startup() -> None:
    db.connect()
    result = seed.run()
    if result["measurements"]:
        print(f"[seed] реестр буёв: {result['devices']}, наблюдений записано: {result['measurements']}")


# --------------------------------------------------------------------------
# Приём телеметрии
# --------------------------------------------------------------------------

@app.post("/api/uplink", tags=["Приём данных"], dependencies=[Depends(require_token)])
def ingest_uplink(event: UplinkEvent) -> dict[str, Any]:
    """
    Приём кадра телеметрии буя.

    Совместим с HTTP-интеграцией ChirpStack v4 (event=up). Полезная нагрузка
    принимается в поле `data` (base64) либо `hex`. Кадр декодируется,
    записывается в базу и проверяется по таблице порогов.
    """
    dev_eui = (event.deviceInfo.devEui or event.devEui or "").lower().replace("-", "").replace(":", "")
    if not dev_eui:
        raise HTTPException(status_code=422, detail="Не указан DevEUI устройства")

    device = db.query_one("SELECT * FROM devices WHERE dev_eui = ?", (dev_eui,))
    if device is None:
        raise HTTPException(status_code=404, detail=f"Устройство {dev_eui} отсутствует в реестре")

    if event.data:
        try:
            payload = base64.b64decode(event.data, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Некорректная base64-нагрузка: {exc}") from exc
    elif event.hex:
        try:
            payload = bytes.fromhex("".join(event.hex.split()))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Некорректная HEX-нагрузка: {exc}") from exc
    else:
        raise HTTPException(status_code=422, detail="Кадр не содержит полезной нагрузки")

    rx = event.rxInfo[0] if event.rxInfo else None
    ts = _parse_ts(event.time).isoformat(timespec="seconds")
    payload_hex = payload.hex()

    try:
        values = codec.decode(payload)
    except codec.CodecError as exc:
        db.execute(
            "INSERT INTO packets (dev_eui, ts, payload_hex, rssi, snr, fcnt, dr, valid, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (dev_eui, ts, payload_hex, rx.rssi if rx else None, rx.snr if rx else None,
             event.fCnt, event.dr, str(exc)),
        )
        raise HTTPException(status_code=422, detail=f"Ошибка декодирования кадра: {exc}") from exc

    db.execute(
        "INSERT INTO packets (dev_eui, ts, payload_hex, rssi, snr, fcnt, dr, frequency, gateway_id, valid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            dev_eui, ts, payload_hex,
            rx.rssi if rx else None, rx.snr if rx else None,
            event.fCnt, event.dr,
            event.txInfo.frequency if event.txInfo else None,
            rx.gatewayId if rx else None,
        ),
    )

    measurement = {
        "ph": values["ph"],
        "temp": values["temp"],
        "do_mgl": values["do"],
        "turb": values["turb"],
        "ec": values["ec"],
        "tds": values["tds"],
        "orp": values["orp"],
        "hc": values["hc"],
        "battery": values["battery"],
    }

    db.execute(
        "INSERT INTO measurements "
        "(dev_eui, ts, ph, temp, do_mgl, turb, ec, tds, orp, hc, battery, rssi, snr, fcnt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dev_eui, ts, measurement["ph"], measurement["temp"], measurement["do_mgl"],
            measurement["turb"], measurement["ec"], measurement["tds"], measurement["orp"],
            measurement["hc"], measurement["battery"],
            rx.rssi if rx else None, rx.snr if rx else None, event.fCnt,
        ),
    )

    triggered = alerts.evaluate(dev_eui, ts, measurement, device["name"])

    return {
        "accepted": True,
        "dev_eui": dev_eui,
        "code": device["code"],
        "ts": ts,
        "decoded": values,
        "alerts": triggered,
    }


@app.post("/api/decode", tags=["Приём данных"])
def decode_payload(request: DecodeRequest) -> dict[str, Any]:
    """Разбор кадра по HEX-строке. Тот же кодек, что и при приёме телеметрии."""
    try:
        return {"ok": True, "decoded": codec.decode_hex(request.hex)}
    except codec.CodecError as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Данные для панели оператора
# --------------------------------------------------------------------------

@app.get("/api/buoys", tags=["Мониторинг"])
def list_buoys() -> list[dict[str, Any]]:
    """Реестр буёв с последними значениями и сводным статусом."""
    devices = db.query("SELECT * FROM devices ORDER BY code")
    result: list[dict[str, Any]] = []

    for device in devices:
        latest = db.query_one(
            "SELECT * FROM measurements WHERE dev_eui = ? ORDER BY ts DESC LIMIT 1",
            (device["dev_eui"],),
        )
        age = _age_seconds(latest["ts"]) if latest else None
        status = alerts.device_status(latest, age, OFFLINE_AFTER_SEC)
        online = status != "offline"

        result.append(
            {
                "dev_eui": device["dev_eui"],
                "id": device["code"],
                "name": device["name"],
                "lat": device["lat"],
                "lng": device["lng"],
                "status": status,
                "pH": latest["ph"] if online and latest else None,
                "temp": latest["temp"] if online and latest else None,
                "DO": latest["do_mgl"] if online and latest else None,
                "turb": latest["turb"] if online and latest else None,
                "ec": latest["ec"] if online and latest else None,
                "tds": latest["tds"] if online and latest else None,
                "orp": latest["orp"] if online and latest else None,
                "hc": latest["hc"] if online and latest else None,
                "battery": latest["battery"] if online and latest else None,
                "rssi": latest["rssi"] if latest else None,
                "snr": latest["snr"] if latest else None,
                "last_seen": latest["ts"] if latest else None,
                "age_sec": round(age) if age is not None else None,
            }
        )
    return result


RANGE_BUCKETS = {
    "1h": (1, "%Y-%m-%dT%H:%M"),
    "6h": (6, "%Y-%m-%dT%H:%M"),
    "24h": (24, "%Y-%m-%dT%H:00"),
    "7d": (24 * 7, "%Y-%m-%dT%H:00"),
    "30d": (24 * 30, "%Y-%m-%d"),
    "90d": (24 * 90, "%Y-%m-%d"),
}


@app.get("/api/buoys/{dev_eui}/history", tags=["Мониторинг"])
def buoy_history(
    dev_eui: str,
    range: str = Query("1h", description="Глубина выборки: 1h, 6h, 24h, 7d, 30d, 90d"),
    points: int = Query(120, ge=10, le=1000, description="Максимальное число точек"),
) -> dict[str, Any]:
    """Ряд наблюдений по буям с агрегацией по временным интервалам."""
    if range not in RANGE_BUCKETS:
        raise HTTPException(status_code=422, detail=f"Недопустимая глубина выборки: {range}")

    hours, fmt = RANGE_BUCKETS[range]
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")

    rows = db.query(
        f"""
        SELECT strftime('{fmt}', ts) AS bucket,
               AVG(ph) AS ph, AVG(temp) AS temp, AVG(do_mgl) AS do_mgl,
               AVG(turb) AS turb, AVG(ec) AS ec, AVG(hc) AS hc, AVG(orp) AS orp
        FROM measurements
        WHERE dev_eui = ? AND ts >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (dev_eui, since),
    )
    rows = rows[-points:]

    def series(key: str, digits: int) -> list[float | None]:
        return [round(row[key], digits) if row[key] is not None else None for row in rows]

    return {
        "dev_eui": dev_eui,
        "range": range,
        "labels": [row["bucket"] for row in rows],
        "ph": series("ph", 2),
        "temp": series("temp", 1),
        "do_": series("do_mgl", 2),
        "turb": series("turb", 1),
        "ec": series("ec", 2),
        "hc": series("hc", 3),
        "orp": series("orp", 0),
    }


@app.get("/api/packets", tags=["Мониторинг"])
def recent_packets(limit: int = Query(30, ge=1, le=200)) -> list[dict[str, Any]]:
    """Журнал принятых кадров LoRaWAN."""
    return db.query(
        "SELECT p.*, d.code FROM packets p LEFT JOIN devices d ON d.dev_eui = p.dev_eui "
        "ORDER BY p.id DESC LIMIT ?",
        (limit,),
    )


@app.get("/api/alerts", tags=["Сигнализация"])
def list_alerts(
    limit: int = Query(50, ge=1, le=500),
    only_active: bool = Query(False, description="Только неподтверждённые события"),
) -> list[dict[str, Any]]:
    """Журнал сработавших пороговых событий."""
    condition = "WHERE a.acked = 0" if only_active else ""
    return db.query(
        f"SELECT a.*, d.code, d.name, d.lat, d.lng FROM alerts a "
        f"LEFT JOIN devices d ON d.dev_eui = a.dev_eui {condition} "
        f"ORDER BY a.id DESC LIMIT ?",
        (limit,),
    )


@app.post("/api/alerts/{alert_id}/ack", tags=["Сигнализация"])
def ack_alert(alert_id: int) -> dict[str, Any]:
    """Подтверждение (квитирование) события оператором."""
    db.execute("UPDATE alerts SET acked = 1 WHERE id = ?", (alert_id,))
    row = db.query_one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    return row


@app.get("/api/thresholds", tags=["Сигнализация"])
def get_thresholds() -> list[dict[str, Any]]:
    """Действующие пороговые значения."""
    return db.query("SELECT * FROM thresholds ORDER BY param")


@app.put("/api/thresholds", tags=["Сигнализация"])
def put_thresholds(updates: list[ThresholdUpdate]) -> list[dict[str, Any]]:
    """Изменение пороговых значений. Применяется к последующим измерениям."""
    for update in updates:
        alerts.update_threshold(update.param, update.model_dump(exclude={"param"}, exclude_none=True))
    return db.query("SELECT * FROM thresholds ORDER BY param")


@app.get("/api/network/stats", tags=["Мониторинг"])
def network_stats() -> dict[str, Any]:
    """Показатели работы сети LoRaWAN за последний час."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    row = db.query_one(
        "SELECT COUNT(*) AS total, SUM(valid) AS valid, AVG(rssi) AS rssi, AVG(snr) AS snr "
        "FROM packets WHERE ts >= ?",
        (since,),
    ) or {}
    total = row.get("total") or 0
    valid = row.get("valid") or 0
    devices_total = (db.query_one("SELECT COUNT(*) AS n FROM devices") or {}).get("n", 0)
    online = sum(1 for b in list_buoys() if b["status"] != "offline")

    return {
        "packets_per_hour": total,
        "packets_valid": valid,
        "loss_percent": round((total - valid) / total * 100, 1) if total else 0.0,
        "rssi_avg": round(row["rssi"], 1) if row.get("rssi") is not None else None,
        "snr_avg": round(row["snr"], 1) if row.get("snr") is not None else None,
        "devices_total": devices_total,
        "devices_online": online,
        "frequency_mhz": 868.1,
        "region": "EU868",
        "gateway": os.environ.get("GATEWAY_NAME", "BS-AKTAU"),
    }


@app.get("/api/stats", tags=["Мониторинг"])
def summary_stats() -> dict[str, Any]:
    """Сводные показатели системы."""
    measurements = (db.query_one("SELECT COUNT(*) AS n FROM measurements") or {}).get("n", 0)
    active = (db.query_one("SELECT COUNT(*) AS n FROM alerts WHERE acked = 0") or {}).get("n", 0)
    critical = (
        db.query_one("SELECT COUNT(*) AS n FROM alerts WHERE acked = 0 AND level = 'alert'") or {}
    ).get("n", 0)
    return {
        "measurements_total": measurements,
        "alerts_active": active,
        "alerts_critical": critical,
        "version": APP_VERSION,
    }


# --------------------------------------------------------------------------
# Открытые данные
# --------------------------------------------------------------------------

@app.get("/api/export", tags=["Открытые данные"])
def export_data(
    format: str = Query("csv", description="Формат выгрузки: csv или geojson"),
    hours: int = Query(24, ge=1, le=24 * 90, description="Глубина выборки в часах"),
    dev_eui: str | None = Query(None, description="Ограничить выборку одним устройством"),
):
    """
    Выгрузка наблюдений в машиночитаемом виде.

    Открытый доступ к данным предусмотрен намеренно: ограниченная доступность
    экологических данных названа в Техническом задании одной из проблем
    региона. Эндпоинт не требует аутентификации.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    condition = "AND m.dev_eui = ?" if dev_eui else ""
    params: tuple = (since, dev_eui) if dev_eui else (since,)

    rows = db.query(
        f"""
        SELECT d.code, d.name, d.lat, d.lng, m.ts, m.ph, m.temp, m.do_mgl,
               m.turb, m.ec, m.tds, m.orp, m.hc
        FROM measurements m JOIN devices d ON d.dev_eui = m.dev_eui
        WHERE m.ts >= ? {condition}
        ORDER BY m.ts
        """,
        params,
    )

    if format == "geojson":
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [row["lng"], row["lat"]]},
                    "properties": {k: v for k, v in row.items() if k not in ("lat", "lng")},
                }
                for row in rows
            ],
        }

    if format != "csv":
        raise HTTPException(status_code=422, detail="Поддерживаются форматы csv и geojson")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["buoy", "name", "lat", "lng", "timestamp_utc", "ph", "temperature_c",
         "dissolved_oxygen_mgl", "turbidity_ntu", "conductivity_mscm", "tds_mgl",
         "orp_mv", "hydrocarbons_mgl"]
    )
    for row in rows:
        writer.writerow(
            [row["code"], row["name"], row["lat"], row["lng"], row["ts"], row["ph"],
             row["temp"], row["do_mgl"], row["turb"], row["ec"], row["tds"], row["orp"], row["hc"]]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="caspian_monitor_export.csv"'},
    )


@app.get("/health", tags=["Служебные"])
def health() -> dict[str, Any]:
    """Проверка доступности сервиса и базы данных."""
    try:
        db.query_one("SELECT 1 AS ok")
        database = "ok"
    except Exception as exc:  # pragma: no cover
        database = f"error: {exc}"
    return {"status": "ok", "database": database, "version": APP_VERSION, "time": db.utcnow()}


# --------------------------------------------------------------------------
# Статическая веб-панель
# --------------------------------------------------------------------------

if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
