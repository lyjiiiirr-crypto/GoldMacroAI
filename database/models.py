"""GoldMacroAI 本地 SQLite 存储层（仅使用标准库）。

保存 FRED 观测值、人工快照、历史报告与提醒去重记录。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from config import DB_PATH

TABLES = (
    "observations",
    "fedwatch_snapshots",
    "events",
    "flows",
    "fomc_documents",
    "technical_reports",
    "gex_snapshots",
    "crt_snapshots",
    "polymarket_snapshots",
    "reports",
    "alert_log",
)


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self):
        """返回自动提交并关闭的上下文；避免 Windows 下未关闭连接锁定 DB 文件。"""
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
                CREATE TABLE IF NOT EXISTS fomc_documents (
                    doc_type TEXT NOT NULL,
                    doc_date TEXT NOT NULL,
                    title TEXT,
                    url TEXT NOT NULL,
                    source TEXT,
                    created_at TEXT,
                    PRIMARY KEY (doc_type, doc_date)
                );
                CREATE TABLE IF NOT EXISTS technical_reports (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    title TEXT,
                    bias TEXT,
                    summary TEXT,
                    payload TEXT,
                    raw_json TEXT,
                    created_at TEXT,
                    PRIMARY KEY (source, symbol, report_date, report_type)
                );
                CREATE INDEX IF NOT EXISTS idx_technical_reports_lookup
                    ON technical_reports (report_date DESC, symbol);
                CREATE TABLE IF NOT EXISTS gex_snapshots (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    spot REAL,
                    net_gex REAL,
                    gamma_flip REAL,
                    call_wall REAL,
                    put_wall REAL,
                    regime TEXT,
                    summary TEXT,
                    payload TEXT,
                    raw_json TEXT,
                    created_at TEXT,
                    PRIMARY KEY (source, symbol, as_of_date)
                );
                CREATE INDEX IF NOT EXISTS idx_gex_snapshots_lookup
                    ON gex_snapshots (as_of_date DESC, symbol);
                CREATE TABLE IF NOT EXISTS crt_snapshots (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    current_high REAL,
                    current_low REAL,
                    prev_high REAL,
                    prev_low REAL,
                    equilibrium REAL,
                    scenario TEXT,
                    bias TEXT,
                    summary TEXT,
                    payload TEXT,
                    raw_json TEXT,
                    created_at TEXT,
                    PRIMARY KEY (source, symbol, as_of)
                );
                CREATE INDEX IF NOT EXISTS idx_crt_snapshots_lookup
                    ON crt_snapshots (as_of DESC, symbol);
                CREATE TABLE IF NOT EXISTS polymarket_snapshots (
                    source TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    question TEXT,
                    yes_probability REAL,
                    volume REAL,
                    payload TEXT,
                    raw_json TEXT,
                    created_at TEXT,
                    PRIMARY KEY (source, as_of_date)
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
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧版数据库幂等补充字段（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
        additions = {
            "fedwatch_snapshots": {"dovish_pp": "REAL", "raw_json": "TEXT"},
            "events": {
                "direction": "TEXT DEFAULT 'normal'",
                "source": "TEXT",
            },
            "flows": {"source": "TEXT", "raw_json": "TEXT"},
        }
        for table, columns in additions.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

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

    # ---- FedWatch 概率快照（人工 / 自动共用，按日期幂等）----
    def upsert_fedwatch_snapshot(
        self, date: str, cut: float, hold: float | None = None,
        hike: float | None = None, meeting: str | None = None,
        source: str | None = None, *, dovish_pp: float | None = None,
        raw_json: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fedwatch_snapshots "
                "(snapshot_date, cut_probability, hold_probability, hike_probability, "
                " meeting_date, source, dovish_pp, raw_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(date), float(cut), hold, hike, meeting or "", source or "manual",
                 dovish_pp, raw_json, datetime.now().isoformat(timespec="seconds")),
            )

    def add_fedwatch_snapshot(self, *args, **kwargs) -> None:
        """兼容旧调用；人工导入默认来源为 manual。"""
        if len(args) < 6 and "source" not in kwargs:
            kwargs["source"] = "manual"
        self.upsert_fedwatch_snapshot(*args, **kwargs)

    def fedwatch_snapshots(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_date, cut_probability, hold_probability, "
                "hike_probability, meeting_date, source, dovish_pp "
                "FROM fedwatch_snapshots ORDER BY snapshot_date DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 经济数据事件 ----
    def upsert_event(self, name: str, actual: float, expected: float,
                     previous: float | None, scale: float, date: str, *,
                     direction: str = "normal", source: str = "manual") -> None:
        """按 (事件名, 发布日期) 去重；direction=inverse 表示数值越高越利多黄金。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM events WHERE event_name = ? AND release_date = ?",
                (name, str(date)),
            )
            conn.execute(
                "INSERT INTO events (event_name, actual, expected, previous, scale, "
                "release_date, direction, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, float(actual), float(expected), previous, float(scale),
                 str(date), direction, source,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def add_event(self, name: str, actual: float, expected: float,
                  previous: float | None, scale: float, date: str,
                  direction: str = "normal", source: str = "manual") -> None:
        self.upsert_event(name, actual, expected, previous, scale, date,
                          direction=direction, source=source)

    def events(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_name, actual, expected, previous, scale, release_date, "
                "direction, source FROM events "
                "ORDER BY release_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 资金流向 ----
    def upsert_flow(self, flow_type: str, date: str, value: float,
                    scale: float = 30000, *, source: str = "manual",
                    raw_json: str | None = None) -> None:
        """按 (类型, 日期) 去重，供 COT / ETF 每周自动刷新使用。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM flows WHERE flow_type = ? AND as_of_date = ?",
                (flow_type, str(date)),
            )
            conn.execute(
                "INSERT INTO flows (flow_type, as_of_date, value, scale, source, "
                "raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (flow_type, str(date), float(value), float(scale), source, raw_json,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def add_flow(self, flow_type: str, date: str, value: float,
                 scale: float = 30000) -> None:
        self.upsert_flow(flow_type, date, value, scale)

    def flows(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT flow_type, as_of_date, value, scale, source, raw_json "
                "FROM flows ORDER BY as_of_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- FOMC 官方文件索引 ----
    def upsert_fomc_documents(self, docs: list[dict]) -> int:
        saved = 0
        with self._connect() as conn:
            for doc in docs:
                cur = conn.execute(
                    "INSERT OR REPLACE INTO fomc_documents "
                    "(doc_type, doc_date, title, url, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc["doc_type"], doc["date"], doc.get("title", ""),
                     doc["url"], doc.get("source", "Federal Reserve"),
                     datetime.now().isoformat(timespec="seconds")),
                )
                saved += max(cur.rowcount, 0)
        return saved

    def fomc_documents(self, limit: int = 12) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_type, doc_date, title, url FROM fomc_documents "
                "ORDER BY doc_date DESC, doc_type LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 技术面报告（自动采集 / 人工导入共用） ----
    def upsert_technical_report(
        self, source: str, symbol: str, date: str, report_type: str,
        title: str, bias: str, summary: str, payload: dict | str,
        raw_json: str | None = None,
    ) -> None:
        """按 (来源, 标的, 日期, 类型) 幂等写入；人工来源与自动来源可并存。"""
        payload_text = payload if isinstance(payload, str) else json.dumps(
            payload or {}, ensure_ascii=False
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO technical_reports "
                "(source, symbol, report_date, report_type, title, bias, summary, "
                " payload, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, symbol, str(date), report_type, title, bias, summary,
                 payload_text, raw_json,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def technical_reports(self, symbol: str | None = None, limit: int = 10) -> list[dict]:
        sql = (
            "SELECT source, symbol, report_date, report_type, title, bias, summary, "
            "payload, raw_json, created_at FROM technical_reports"
        )
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY report_date DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            result.append(item)
        return result

    # ---- GEX / Gamma flip 快照 ----
    def upsert_gex_snapshot(
        self, source: str, symbol: str, date: str, spot: float,
        net_gex: float, gamma_flip: float | None, call_wall: float | None,
        put_wall: float | None, regime: str, summary: str,
        payload: dict | str, raw_json: str | None = None,
    ) -> None:
        payload_text = payload if isinstance(payload, str) else json.dumps(
            payload or {}, ensure_ascii=False
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gex_snapshots "
                "(source, symbol, as_of_date, spot, net_gex, gamma_flip, call_wall, "
                " put_wall, regime, summary, payload, raw_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, symbol, str(date), spot, net_gex, gamma_flip,
                 call_wall, put_wall, regime, summary, payload_text, raw_json,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def gex_snapshots(self, symbol: str | None = None, limit: int = 8) -> list[dict]:
        sql = (
            "SELECT source, symbol, as_of_date, spot, net_gex, gamma_flip, "
            "call_wall, put_wall, regime, summary, payload, raw_json, created_at "
            "FROM gex_snapshots"
        )
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY as_of_date DESC, symbol ASC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            result.append(item)
        return result

    # ---- 4H CRT 快照（自动 / 人工共用，按来源+标的+时间幂等）----
    def upsert_crt_snapshot(
        self, source: str, symbol: str, as_of: str, timeframe: str,
        current_high: float | None, current_low: float | None,
        prev_high: float | None, prev_low: float | None,
        equilibrium: float | None, scenario: str | None, bias: str | None,
        summary: str, payload: dict | str, raw_json: str | None = None,
    ) -> None:
        payload_text = payload if isinstance(payload, str) else json.dumps(
            payload or {}, ensure_ascii=False
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO crt_snapshots "
                "(source, symbol, as_of, timeframe, current_high, current_low, "
                " prev_high, prev_low, equilibrium, scenario, bias, summary, "
                " payload, raw_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, symbol, str(as_of), timeframe, current_high, current_low,
                 prev_high, prev_low, equilibrium, scenario, bias, summary,
                 payload_text, raw_json,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def crt_snapshots(self, symbol: str | None = None, limit: int = 8) -> list[dict]:
        sql = (
            "SELECT source, symbol, as_of, timeframe, current_high, current_low, "
            "prev_high, prev_low, equilibrium, scenario, bias, summary, payload, "
            "raw_json, created_at FROM crt_snapshots"
        )
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY as_of DESC, symbol ASC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            result.append(item)
        return result

    # ---- Polymarket 利率预测（自动 / 人工共用，按来源+日期幂等）----
    def upsert_polymarket_snapshot(
        self, source: str, as_of_date: str, question: str | None,
        yes_probability: float | None, volume: float | None,
        payload: dict | str, raw_json: str | None = None,
    ) -> None:
        payload_text = payload if isinstance(payload, str) else json.dumps(
            payload or {}, ensure_ascii=False
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO polymarket_snapshots "
                "(source, as_of_date, question, yes_probability, volume, "
                " payload, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source, str(as_of_date), question, yes_probability, volume,
                 payload_text, raw_json,
                 datetime.now().isoformat(timespec="seconds")),
            )

    def polymarket_snapshots(self, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, as_of_date, question, yes_probability, volume, "
                "payload, raw_json, created_at FROM polymarket_snapshots "
                "ORDER BY as_of_date DESC, created_at DESC LIMIT ?",
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
