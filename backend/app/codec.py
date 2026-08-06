"""
Кодек полезной нагрузки LoRaWAN для буя экологического мониторинга.

Формат кадра (big-endian, fPort = 2), минимум 12 байт, расширяемый до 18:

  Смещение  Размер  Тип     Параметр                Масштаб    Единица
  --------  ------  ------  ----------------------  ---------  --------
   0        2       uint16  pH                      /100       pH
   2        2       int16   Температура             /10        °C
   4        2       uint16  Растворённый кислород   /100       мг/л
   6        2       uint16  Мутность                /1         NTU
   8        2       uint16  Удельная электропров.   /10        мСм/см
  10        2       uint16  Сумма углеводородов     /1000      мг/л
  --- опционально ---
  12        2       int16   ORP (окисл.-восст.)     /1         мВ
  14        2       uint16  TDS                     x10        мг/л
  16        1       uint8   Заряд батареи           /1         %
  17        1       uint8   Флаги состояния         битовое поле

Флаги состояния (байт 17):
  bit 0 — отказ датчика pH
  bit 1 — отказ датчика DO
  bit 2 — низкий заряд батареи
  bit 3 — сработал детектор нефтепродуктов
  bit 4 — вскрытие корпуса

Кодек намеренно вынесен в отдельный модуль: он используется бэкендом при
приёме uplink, эмулятором при формировании кадра и фронтендом через
эндпоинт /api/decode. Один формат — один источник истины.
"""

from __future__ import annotations

import struct
from typing import Any

MIN_PAYLOAD_LEN = 12
FULL_PAYLOAD_LEN = 18

FLAG_PH_FAULT = 0x01
FLAG_DO_FAULT = 0x02
FLAG_LOW_BATTERY = 0x04
FLAG_HC_DETECTED = 0x08
FLAG_TAMPER = 0x10

FLAG_LABELS = {
    FLAG_PH_FAULT: "Отказ датчика pH",
    FLAG_DO_FAULT: "Отказ датчика DO",
    FLAG_LOW_BATTERY: "Низкий заряд батареи",
    FLAG_HC_DETECTED: "Детектор нефтепродуктов сработал",
    FLAG_TAMPER: "Вскрытие корпуса",
}


class CodecError(ValueError):
    """Кадр не соответствует формату."""


def decode(payload: bytes) -> dict[str, Any]:
    """Декодирует кадр буя в словарь физических величин."""
    if len(payload) < MIN_PAYLOAD_LEN:
        raise CodecError(
            f"недостаточная длина кадра: {len(payload)} байт, требуется минимум {MIN_PAYLOAD_LEN}"
        )

    ph_raw, temp_raw, do_raw, turb_raw, ec_raw, hc_raw = struct.unpack_from(">HhHHHH", payload, 0)

    result: dict[str, Any] = {
        "ph": round(ph_raw / 100.0, 2),
        "temp": round(temp_raw / 10.0, 1),
        "do": round(do_raw / 100.0, 2),
        "turb": float(turb_raw),
        "ec": round(ec_raw / 10.0, 1),
        "hc": round(hc_raw / 1000.0, 3),
        "orp": None,
        "tds": None,
        "battery": None,
        "flags": [],
        "flags_raw": 0,
    }

    if len(payload) >= 14:
        result["orp"] = float(struct.unpack_from(">h", payload, 12)[0])
    if len(payload) >= 16:
        result["tds"] = float(struct.unpack_from(">H", payload, 14)[0] * 10)
    if len(payload) >= 17:
        result["battery"] = float(payload[16])
    if len(payload) >= 18:
        flags = payload[17]
        result["flags_raw"] = flags
        result["flags"] = [label for bit, label in FLAG_LABELS.items() if flags & bit]

    _validate_ranges(result)
    return result


def _validate_ranges(values: dict[str, Any]) -> None:
    """Отбраковка физически невозможных значений — защита от битых кадров."""
    limits = {
        "ph": (0.0, 14.0),
        "temp": (-5.0, 45.0),
        "do": (0.0, 20.0),
        "turb": (0.0, 4000.0),
        "ec": (0.0, 100.0),
        "hc": (0.0, 65.0),
    }
    for key, (lo, hi) in limits.items():
        value = values.get(key)
        if value is not None and not (lo <= value <= hi):
            raise CodecError(f"значение {key}={value} вне физически допустимого диапазона [{lo}; {hi}]")


def encode(
    ph: float,
    temp: float,
    do: float,
    turb: float,
    ec: float,
    hc: float,
    orp: float | None = None,
    tds: float | None = None,
    battery: float | None = None,
    flags: int = 0,
) -> bytes:
    """Формирует кадр буя. Используется эмулятором и для юнит-тестов кодека."""
    frame = struct.pack(
        ">HhHHHH",
        _clamp(round(ph * 100), 0, 65535),
        _clamp(round(temp * 10), -32768, 32767),
        _clamp(round(do * 100), 0, 65535),
        _clamp(round(turb), 0, 65535),
        _clamp(round(ec * 10), 0, 65535),
        _clamp(round(hc * 1000), 0, 65535),
    )
    if orp is None:
        return frame
    frame += struct.pack(">h", _clamp(round(orp), -32768, 32767))
    if tds is None:
        return frame
    frame += struct.pack(">H", _clamp(round(tds / 10), 0, 65535))
    if battery is None:
        return frame
    frame += struct.pack(">BB", _clamp(round(battery), 0, 255), _clamp(flags, 0, 255))
    return frame


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def decode_hex(hex_string: str) -> dict[str, Any]:
    """Декодирует кадр, заданный шестнадцатеричной строкой (пробелы допускаются)."""
    cleaned = "".join(hex_string.split()).replace("0x", "")
    if len(cleaned) % 2 != 0:
        raise CodecError("нечётное количество шестнадцатеричных символов")
    try:
        payload = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise CodecError(f"недопустимый символ в HEX-строке: {exc}") from exc
    return decode(payload)
