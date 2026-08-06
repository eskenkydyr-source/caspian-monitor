"""Слой доступа к данным. SQLite в режиме WAL, без внешних ORM."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get("DB_PATH", "/data/caspian.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    dev_eui     TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    commissioned INTEGER NOT NULL DEFAULT 1,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS measurements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_eui  TEXT NOT NULL REFERENCES devices(dev_eui),
    ts       TEXT NOT NULL,
    ph       REAL, temp REAL, do_mgl REAL, turb REAL,
    ec       REAL, tds  REAL, orp     REAL, hc   REAL,
    battery  REAL, rssi REAL, snr     REAL, fcnt INTEGER
);
CREATE INDEX IF NOT EXISTS idx_meas_dev_ts ON measurements(dev_eui, ts DESC);
CREATE INDEX IF NOT EXISTS idx_meas_ts     ON measurements(ts DESC);

CREATE TABLE IF NOT EXISTS packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_eui     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    payload_hex TEXT NOT NULL,
    rssi        REAL, snr REAL, fcnt INTEGER, dr INTEGER,
    frequency   INTEGER,
    gateway_id  TEXT,
    valid       INTEGER NOT NULL DEFAULT 1,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_packets_ts ON packets(ts DESC);

CREATE TABLE IF NOT EXISTS thresholds (
    param     TEXT PRIMARY KEY,
    label     TEXT NOT NULL,
    unit      TEXT NOT NULL,
    warn_min  REAL, warn_max REAL,
    alert_min REAL, alert_max REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_eui   TEXT NOT NULL REFERENCES devices(dev_eui),
    ts        TEXT NOT NULL,
    param     TEXT NOT NULL,
    value     REAL NOT NULL,
    threshold REAL NOT NULL,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL,
    acked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            directory = os.path.dirname(DB_PATH)
            if directory:
                os.makedirs(directory, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid


def executemany(sql: str, seq: list[tuple]) -> None:
    with cursor() as cur:
        cur.executemany(sql, seq)


def is_empty() -> bool:
    row = query_one("SELECT COUNT(*) AS n FROM devices")
    return not row or row["n"] == 0
