"""CME FedWatch 概率的自动采集（免费公开快照）。

CME 官方 FedWatch API 为付费服务；本项目读取社区项目
zuowood1234/cme-fedwatch-tracker 每日公开归档的 JSON 快照，
其数据来自 CME FedWatch 页面，仅作参考并保留来源标注。
原始人工导入通道（--import-fedwatch）继续保留，可覆盖自动数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import FEDWATCH_ARCHIVE_URL
from data.common import FetchError, fetch_json

SOURCE_NAME = "auto:cme-fedwatch-tracker"
DAILY_URL = FEDWATCH_ARCHIVE_URL


def parse_daily_snapshot(payload: dict) -> dict | None:
    """把每日 JSON 解析成 fedwatch_snapshots 记录；结构不符返回 None。"""
    if not isinstance(payload, dict):
        return None
    snapshot_date = str(payload.get("snapshot_date") or "")[:10]
    meetings = payload.get("meetings") or []
    if not snapshot_date or not meetings or not isinstance(meetings[0], dict):
        return None
    meeting = meetings[0]
    summary = meeting.get("summary") or {}
    cut = summary.get("ease")
    if cut is None:
        return None
    hold = summary.get("no_change")
    hike = summary.get("hike")
    return {
        "date": snapshot_date,
        "cut": float(cut),
        "hold": float(hold) if hold is not None else None,
        "hike": float(hike) if hike is not None else None,
        "meeting": str(meeting.get("meeting_date") or "")[:10],
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


def collect(db, lookback_days: int = 14) -> dict:
    """抓取最近 N 天可用的 FedWatch 快照并写入数据库（按日期幂等）。"""
    today = datetime.now(timezone.utc).date()
    saved = 0
    latest = None
    failures = 0
    for offset in range(lookback_days + 1):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            payload = fetch_json(DAILY_URL.format(date=day), timeout=30)
        except FetchError:
            failures += 1
            continue
        snap = parse_daily_snapshot(payload)
        if snap is None:
            continue
        db.upsert_fedwatch_snapshot(
            snap["date"], snap["cut"], snap["hold"], snap["hike"],
            snap["meeting"], source=SOURCE_NAME, raw_json=snap["raw_json"],
        )
        saved += 1
        if latest is None or snap["date"] > latest["date"]:
            latest = snap
    result = {
        "ok": saved > 0, "source": "cme-fedwatch-tracker (社区归档)",
        "snapshots": saved, "missed_days": failures, "latest": latest,
    }
    if not saved:
        result["error"] = "近 %d 天未取得可用 FedWatch 快照" % lookback_days
    return result
