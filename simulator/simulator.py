#!/usr/bin/env python3
"""
Эмулятор сети буёв экологического мониторинга.

Назначение: воспроизвести поведение реального сетевого сервера ChirpStack v4
для демонстрации и отладки системы в отсутствие развёрнутого оборудования.

Эмулятор кодирует значения датчиков в бинарный кадр тем же кодеком, что
используется на стороне буя, оборачивает его в событие uplink формата
ChirpStack и отправляет по HTTP на эндпоинт /api/uplink. Сервер не располагает
сведениями об источнике данных: замена эмулятора на реальный сетевой сервер
выполняется изменением адреса HTTP-интеграции в ChirpStack и не требует
правок в коде сервиса.

Дополнительно воспроизводятся два эксплуатационных сценария:

  * авария с розливом нефтепродуктов на буе BY-02 — плавный рост суммы
    углеводородов и мутности с одновременным падением растворённого
    кислорода; предназначен для демонстрации работы пороговой сигнализации;

  * потеря связи с буями BY-04 и BY-16 — кадры не передаются, что приводит
    к автоматическому переводу устройств в состояние «офлайн» по таймауту.

Запуск:
    python simulator.py --api http://localhost:8000 --interval 10
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Кодек кадра. Дублирует backend/app/codec.py намеренно: эмулятор
# представляет прошивку буя и не должен зависеть от кода сервера.
# --------------------------------------------------------------------------

FLAG_HC_DETECTED = 0x08
FLAG_LOW_BATTERY = 0x04


def encode_frame(ph, temp, do, turb, ec, hc, orp, tds, battery, flags=0) -> bytes:
    clamp = lambda v, lo, hi: max(lo, min(hi, int(round(v))))
    return struct.pack(
        ">HhHHHHhHBB",
        clamp(ph * 100, 0, 65535),
        clamp(temp * 10, -32768, 32767),
        clamp(do * 100, 0, 65535),
        clamp(turb, 0, 65535),
        clamp(ec * 10, 0, 65535),
        clamp(hc * 1000, 0, 65535),
        clamp(orp, -32768, 32767),
        clamp(tds / 10, 0, 65535),
        clamp(battery, 0, 255),
        clamp(flags, 0, 255),
    )


# --------------------------------------------------------------------------
# Парк устройств. DevEUI соответствуют реестру в backend/app/seed.py
# --------------------------------------------------------------------------

BUOYS = [
    # dev_eui,           code,     ph,   temp, do,  turb, ec,   orp, hc,   активен
    ("70b3d57ed0060001", "BY-01", 8.12, 14.3, 7.8, 12.4, 24.6, 286, 0.04, True),
    ("70b3d57ed0060002", "BY-02", 7.90, 13.8, 6.9, 12.0, 24.1, 250, 0.03, True),
    ("70b3d57ed0060003", "BY-03", 8.24, 12.9, 8.6, 6.2, 25.1, 312, 0.02, True),
    ("70b3d57ed0060004", "BY-04", 8.10, 13.0, 7.5, 10.0, 24.5, 280, 0.03, False),
    ("70b3d57ed0060005", "BY-05", 8.18, 13.1, 8.1, 9.5, 24.8, 299, 0.01, True),
    ("70b3d57ed0060006", "BY-06", 8.09, 14.7, 7.4, 7.8, 24.4, 278, 0.03, True),
    ("70b3d57ed0060007", "BY-07", 7.71, 11.8, 5.1, 31.2, 23.9, 205, 0.12, True),
    ("70b3d57ed0060008", "BY-08", 8.21, 15.2, 8.9, 5.4, 25.3, 320, 0.01, True),
    ("70b3d57ed0060009", "BY-09", 8.15, 14.0, 7.6, 8.2, 24.9, 291, 0.03, True),
    ("70b3d57ed006000a", "BY-10", 8.07, 13.6, 7.2, 11.0, 24.5, 275, 0.04, True),
    ("70b3d57ed006000b", "BY-11", 7.89, 12.2, 6.1, 18.5, 24.2, 242, 0.07, True),
    ("70b3d57ed006000c", "BY-12", 8.22, 14.8, 8.3, 7.0, 25.0, 308, 0.02, True),
    ("70b3d57ed006000d", "BY-13", 8.11, 11.4, 7.9, 9.8, 24.7, 284, 0.03, True),
    ("70b3d57ed006000e", "BY-14", 8.19, 15.5, 8.5, 4.8, 25.2, 315, 0.01, True),
    ("70b3d57ed006000f", "BY-15", 8.28, 11.9, 9.1, 5.2, 25.4, 328, 0.01, True),
    ("70b3d57ed0060010", "BY-16", 8.14, 12.5, 7.7, 8.9, 24.8, 290, 0.02, False),
]

SPILL_BUOY = "70b3d57ed0060002"  # BY-02, платформа CK-2
SPILL_START_TICK = 12  # через сколько циклов начинается развитие аварии
SPILL_DURATION = 40  # длительность развития аварии в циклах


class Buoy:
    """Модель одного буя: удерживает состояние датчиков между передачами."""

    def __init__(self, dev_eui, code, ph, temp, do, turb, ec, orp, hc, active):
        self.dev_eui = dev_eui
        self.code = code
        self.ph = ph
        self.temp = temp
        self.do = do
        self.turb = turb
        self.ec = ec
        self.orp = orp
        self.hc = hc
        self.active = active
        self.battery = random.uniform(72.0, 99.0)
        self.fcnt = random.randint(1000, 9000)
        self.rng = random.Random(hash(dev_eui) & 0xFFFFFFFF)

    def step(self, tick: int, spill_enabled: bool) -> None:
        """Случайное блуждание вокруг базового значения плюс суточный цикл."""
        hour = datetime.now(timezone.utc).hour
        daily = math.sin((hour / 24.0) * 2 * math.pi - math.pi / 2)

        self.ph = self._walk(self.ph, 0.015, 7.5, 8.6)
        self.temp = self._walk(self.temp + daily * 0.02, 0.05, 5.0, 28.0)
        self.do = self._walk(self.do, 0.06, 3.0, 11.0)
        self.turb = self._walk(self.turb, 0.4, 1.0, 120.0)
        self.ec = self._walk(self.ec, 0.03, 22.0, 26.5)
        self.orp = self._walk(self.orp, 2.0, 150.0, 400.0)
        self.hc = self._walk(self.hc, 0.002, 0.0, 3.0)
        self.battery = max(5.0, self.battery - self.rng.uniform(0.0, 0.004))

        if spill_enabled and self.dev_eui == SPILL_BUOY and tick >= SPILL_START_TICK:
            self._apply_spill(tick)

    def _apply_spill(self, tick: int) -> None:
        """Развитие аварийного розлива нефтепродуктов."""
        progress = min(1.0, (tick - SPILL_START_TICK) / SPILL_DURATION)
        curve = progress ** 0.6
        self.hc = round(0.03 + curve * 0.55, 3)
        self.turb = round(12.0 + curve * 34.0, 1)
        self.do = round(max(2.4, 6.9 - curve * 4.0), 2)
        self.ph = round(7.90 - curve * 0.45, 2)

    def _walk(self, value, step, lo, hi):
        return round(max(lo, min(hi, value + self.rng.uniform(-step, step))), 3)

    @property
    def tds(self) -> float:
        return round(self.ec * 520, 0)

    @property
    def flags(self) -> int:
        flags = 0
        if self.hc > 0.10:
            flags |= FLAG_HC_DETECTED
        if self.battery < 20:
            flags |= FLAG_LOW_BATTERY
        return flags

    def build_uplink(self) -> dict:
        """Событие uplink в формате HTTP-интеграции ChirpStack v4."""
        payload = encode_frame(
            self.ph, self.temp, self.do, self.turb, self.ec,
            self.hc, self.orp, self.tds, self.battery, self.flags,
        )
        self.fcnt += 1
        return {
            "deduplicationId": f"{self.dev_eui}-{self.fcnt}",
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "deviceInfo": {
                "tenantName": "Caspian Monitor",
                "applicationName": "caspian-buoys",
                "deviceProfileName": "buoy-v1",
                "deviceName": self.code,
                "devEui": self.dev_eui,
            },
            "devAddr": "01a2b3c4",
            "adr": True,
            "dr": 3,
            "fCnt": self.fcnt,
            "fPort": 2,
            "confirmed": False,
            "data": base64.b64encode(payload).decode(),
            "rxInfo": [
                {
                    "gatewayId": "ac1f09fffe0abcde",
                    "rssi": round(-85 - self.rng.random() * 22, 1),
                    "snr": round(2.0 + self.rng.random() * 6.0, 1),
                    "channel": self.rng.randint(0, 7),
                    "location": {},
                }
            ],
            "txInfo": {
                "frequency": 868100000,
                "modulation": {
                    "lora": {"bandwidth": 125000, "spreadingFactor": 10, "codeRate": "CR_4_5"}
                },
            },
        }


def post_uplink(api_url: str, token: str, event: dict, timeout: float = 10.0) -> tuple[bool, str]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/uplink",
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            triggered = body.get("alerts") or []
            note = f" | СОБЫТИЙ: {len(triggered)}" if triggered else ""
            return True, f"HTTP {response.status}{note}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:160]}"
    except urllib.error.URLError as exc:
        return False, f"нет связи с сервером: {exc.reason}"


def wait_for_api(api_url: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"{api_url.rstrip('/')}/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Эмулятор сети буёв Caspian Monitor")
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"),
                        help="Базовый адрес сервера")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SIM_INTERVAL", "10")),
                        help="Интервал передачи, секунд")
    parser.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""),
                        help="Токен приёма данных, если задан на сервере")
    parser.add_argument("--no-spill", action="store_true",
                        help="Отключить сценарий аварийного розлива на BY-02")
    parser.add_argument("--once", action="store_true", help="Передать один цикл и завершить работу")
    args = parser.parse_args()

    buoys = [Buoy(*row) for row in BUOYS]
    online = [b for b in buoys if b.active]

    print(f"[эмулятор] сервер: {args.api}")
    print(f"[эмулятор] буёв в парке: {len(buoys)}, передают: {len(online)}, "
          f"вне связи: {len(buoys) - len(online)}")
    print(f"[эмулятор] интервал передачи: {args.interval} с")
    if not args.no_spill:
        print(f"[эмулятор] сценарий розлива на BY-02 начнётся через {SPILL_START_TICK} циклов")

    if not wait_for_api(args.api):
        print("[эмулятор] сервер недоступен, работа прекращена", file=sys.stderr)
        return 1
    print("[эмулятор] сервер доступен, начата передача\n")

    tick = 0
    while True:
        tick += 1
        sent = failed = 0
        for buoy in online:
            buoy.step(tick, spill_enabled=not args.no_spill)
            ok, note = post_uplink(args.api, args.token, buoy.build_uplink())
            if ok:
                sent += 1
                if "СОБЫТИЙ" in note:
                    print(f"  {buoy.code}: pH={buoy.ph} ΣHC={buoy.hc} мг/л DO={buoy.do} мг/л — {note}")
            else:
                failed += 1
                print(f"  {buoy.code}: ошибка передачи — {note}", file=sys.stderr)

        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] цикл {tick}: передано {sent}, ошибок {failed}")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[эмулятор] работа прекращена оператором")
