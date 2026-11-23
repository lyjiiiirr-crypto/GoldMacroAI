"""GoldMacroAI 本地 SQLite 存储层（仅使用标准库）。

保存 FRED 观测值、人工快照、历史报告与提醒去重记录。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config import DB_PATH

TABLES = (
    "observations",
    "fedwatch_snapshots",
    "events",
    "flows",
    "reports",
    "alert_log",
)


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    series_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (series_id, date)
                );
                CREATE TABLE IF NOT EXISTS fedwatch_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT NOT NULL UNIQUE,
                    cut_probability REAL,
                    hold_probability REAL,
                    hike_probability REAL,
                    meeting_date TEXT,
                    source TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    actual REAL NOT NULL,
                    expected REAL NOT NULL,
                    previous REAL,
                    scale REAL DEFAULT 0.3,
                    release_date TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS flows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_type TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    value REAL NOT NULL,
                    scale REAL DEFAULT 30000,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    score REAL NOT NULL,
                    bias TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alert_log_lookup
                    ON alert_log (kind, fingerprint, sent_at);
                """
            )

    def status(self) -> dict:
        counts = {}
        with self._connect() as conn:
            for table in TABLES:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        return {"db": str(self.path), "counts": counts}

    # ---- FRED 观测值 ----
    def upsert_observations(self, series_id: str, rows: list[dict]) -> int:
        saved = 0
        with self._connect() as conn:
            for row in rows:
                cur = conn.execute(
                    "INSERT OR REPLACE INTO observations (series_id, date, value) VALUES (?, ?, ?)",
                    (series_id, str(row["date"]), float(row["value"])),
                )
                saved += max(cur.rowcount, 0)
        return saved

    def observations(self, series_id: str, limit: int = 400) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, value FROM observations WHERE series_id = ? "
                "ORDER BY date DESC LIMIT ?",
                (series_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- FedWatch 人工快照 ----
    def add_fedwatch_snapshot(
        self, date: str, cut: float, hold: float | None = None,
        hike: float | None = None, meeting: str | None = None,
        source: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fedwatch_snapshots "
                "(snapshot_date, cut_probability, hold_probability, hike_probability, "
                " meeting_date, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(date), float(cut), hold, hike, meeting or "", source or "",
                 datetime.now().isoformat(timespec="seconds")),
            )

    def fedwatch_snapshots(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_date, cut_probability, hold_probability, "
                "hike_probability, meeting_date, source FROM fedwatch_snapshots "
                "ORDER BY snapshot_date DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 经济数据 ----
    def add_event(self, name: str, actual: float, expected: float,
                  previous: float | None, scale: float, date: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_name, actual, expected, previous, scale, "
                "release_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, float(actual), float(expected), previous, float(scale),
                 str(date), datetime.now().isoformat(timespec="seconds")),
            )

    def events(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_name, actual, expected, previous, scale, release_date "
                "FROM events ORDER BY release_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 资金流向 ----
    def add_flow(self, flow_type: str, date: str, value: float,
                 scale: float = 30000) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO flows (flow_type, as_of_date, value, scale, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (flow_type, str(date), float(value), float(scale),
                 datetime.now().isoformat(timespec="seconds")),
            )

    def flows(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT flow_type, as_of_date, value, scale FROM flows "
                "ORDER BY as_of_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 历史报告 ----
    def save_report(self, score: float, bias: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reports (score, bias, payload, created_at) VALUES (?, ?, ?, ?)",
                (float(score), bias, json.dumps(payload, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")),
            )

    def reports(self, limit: int = 2) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, score, bias, payload, created_at FROM reports "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            result.append(item)
        return result

    # ---- 提醒去重 ----
    def can_send_alert(self, kind: str, fingerprint: str,
                       cooldown_hours: int = 12) -> bool:
        """冷却窗口内未发送过则记录本次并返回 True。"""
        threshold = (
            datetime.now() - timedelta(hours=float(cooldown_hours))
        ).isoformat(timespec="seconds")
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM alert_log WHERE kind = ? AND fingerprint = ? "
                "AND sent_at >= ? LIMIT 1",
                (kind, fingerprint, threshold),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO alert_log (kind, fingerprint, sent_at) VALUES (?, ?, ?)",
                (kind, fingerprint, datetime.now().isoformat(timespec="seconds")),
            )
        return True
