"""
Проверка кодека полезной нагрузки.

Запуск:  python -m pytest backend/tests -v
или:     python backend/tests/test_codec.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import codec  # noqa: E402


class TestCodec(unittest.TestCase):
    def test_roundtrip_short_frame(self):
        """Кадр минимальной длины кодируется и декодируется без потерь."""
        frame = codec.encode(ph=8.12, temp=14.3, do=7.8, turb=12, ec=24.6, hc=0.041)
        self.assertEqual(len(frame), 12)
        decoded = codec.decode(frame)
        self.assertAlmostEqual(decoded["ph"], 8.12, places=2)
        self.assertAlmostEqual(decoded["temp"], 14.3, places=1)
        self.assertAlmostEqual(decoded["do"], 7.8, places=2)
        self.assertEqual(decoded["turb"], 12.0)
        self.assertAlmostEqual(decoded["ec"], 24.6, places=1)
        self.assertAlmostEqual(decoded["hc"], 0.041, places=3)

    def test_roundtrip_full_frame(self):
        """Расширенный кадр передаёт ORP, TDS, заряд батареи и флаги."""
        frame = codec.encode(
            ph=7.62, temp=-1.5, do=4.2, turb=286, ec=24.1,
            hc=0.38, orp=198, tds=12540, battery=87,
            flags=codec.FLAG_HC_DETECTED | codec.FLAG_LOW_BATTERY,
        )
        self.assertEqual(len(frame), 18)
        decoded = codec.decode(frame)
        self.assertAlmostEqual(decoded["temp"], -1.5, places=1)
        self.assertEqual(decoded["orp"], 198.0)
        self.assertEqual(decoded["tds"], 12540.0)
        self.assertEqual(decoded["battery"], 87.0)
        self.assertEqual(len(decoded["flags"]), 2)

    def test_negative_temperature(self):
        """Температура кодируется знаковым целым."""
        decoded = codec.decode(codec.encode(8.0, -3.4, 9.0, 5, 25.0, 0.01))
        self.assertAlmostEqual(decoded["temp"], -3.4, places=1)

    def test_short_frame_rejected(self):
        with self.assertRaises(codec.CodecError):
            codec.decode(b"\x03\x2c\x00\x8f")

    def test_out_of_range_rejected(self):
        """Физически невозможное значение pH отбраковывается."""
        broken = (9999).to_bytes(2, "big") + b"\x00" * 10
        with self.assertRaises(codec.CodecError):
            codec.decode(broken)

    def test_decode_hex_with_spaces(self):
        frame = codec.encode(8.12, 14.3, 7.8, 12, 24.6, 0.041)
        spaced = " ".join(frame.hex()[i:i + 2] for i in range(0, len(frame.hex()), 2))
        self.assertAlmostEqual(codec.decode_hex(spaced)["ph"], 8.12, places=2)

    def test_invalid_hex_rejected(self):
        with self.assertRaises(codec.CodecError):
            codec.decode_hex("ZZ11223344556677889900")


class TestAlertLogic(unittest.TestCase):
    def test_threshold_levels(self):
        from app.alerts import _check

        rule = {"warn_min": None, "warn_max": 0.05, "alert_min": None, "alert_max": 0.10}
        self.assertIsNone(_check(0.03, rule))
        self.assertEqual(_check(0.07, rule)[0], "warning")
        self.assertEqual(_check(0.38, rule)[0], "alert")

    def test_lower_bound_breach(self):
        from app.alerts import _check

        rule = {"warn_min": 6.0, "warn_max": None, "alert_min": 4.0, "alert_max": None}
        self.assertIsNone(_check(7.5, rule))
        self.assertEqual(_check(5.2, rule)[0], "warning")
        self.assertEqual(_check(3.1, rule)[2], "low")


if __name__ == "__main__":
    unittest.main(verbosity=2)
