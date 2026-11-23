"""数据清洗：日期对齐、变化值计算、时效检查。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def age_days(value: str, now: date | None = None) -> int:
    now = now or datetime.now(timezone.utc).date()
    return (now - parse_date(value)).days


def change_by_days(rows: list[dict], days: int) -> dict | None:
    """在按日期降序排列的观测中，找到最新值与 N 个自然日前最近的值。

    不使用未来数据；缺失返回 None。
    """
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: str(r["date"]), reverse=True)
    latest = rows[0]
    target = parse_date(latest["date"]) - timedelta(days=days)
    older = next((r for r in rows if parse_date(r["date"]) <= target), None)
    if older is None:
        return None
    return {
        "date": latest["date"],
        "value": float(latest["value"]),
        "previous_date": older["date"],
        "previous": float(older["value"]),
        "delta": float(latest["value"]) - float(older["value"]),
        "span_days": (parse_date(latest["date"]) - parse_date(older["date"])).days,
    }


def pct_change(change: dict | None) -> float | None:
    if not change or change["previous"] == 0:
        return None
    return change["delta"] / abs(change["previous"]) * 100.0
