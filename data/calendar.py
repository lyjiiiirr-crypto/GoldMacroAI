"""经济日历：用户可编辑的本地 JSON，V1.0 不依赖付费日历接口。

复制 calendar.example.json 为 calendar.json 后填入自己的事件（北京时间日期）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import BASE_DIR, CHINA_TIMEZONE_OFFSET_HOURS

CALENDAR_PATH = BASE_DIR / "data" / "calendar.json"


def upcoming_events(hours: int = 24) -> list[dict]:
    path = CALENDAR_PATH
    if not path.exists():
        return []
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    now = datetime.now(timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS)))
    end = now + timedelta(hours=hours)
    result = []
    for event in events:
        try:
            when = datetime.fromisoformat(str(event["time"]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS)))
        except (KeyError, ValueError, TypeError):
            continue
        if now <= when <= end:
            result.append({
                "name": str(event.get("name", "未命名事件")),
                "time": when.strftime("%m-%d %H:%M"),
                "impact": str(event.get("impact", "中")),
                "consensus": event.get("consensus", ""),
            })
    return sorted(result, key=lambda e: e["time"])


def format_calendar(events: list[dict]) -> str:
    if not events:
        return "未来24小时无已录入的重点事件（可编辑 data/calendar.json 添加）"
    return "\n".join(
        f"- {e['time']} {e['name']}（影响：{e['impact']}，预期：{e['consensus'] or '-'}）"
        for e in events
    )
