"""XAUUSD 周末休市判断（北京时间）。

默认休市窗口：周六 06:00 -> 周一 06:00（北京时间），即 UTC 周五 22:00 ->
周日 22:00。窗口内暂停全部采集、评分、落库与推送；keeper 仍链式运行，
以便休市结束后自动恢复。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from config import MARKET_CLOSED_END, MARKET_CLOSED_START, MARKET_TIMEZONE_OFFSET_HOURS

MARKET_TZ = timezone(timedelta(hours=int(MARKET_TIMEZONE_OFFSET_HOURS)))
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _parse_spec(value: str, fallback: tuple[int, time]) -> tuple[int, time]:
    """Parse ``"Sat 06:00"``; invalid values fall back to the default."""
    try:
        day_text, clock = str(value).strip().split()
        weekday = _WEEKDAYS[day_text.lower()[:3]]
        hour, minute = (int(part) for part in clock.split(":"))
        return weekday, time(hour, minute)
    except (AttributeError, KeyError, ValueError):
        return fallback


CLOSED_START = _parse_spec(MARKET_CLOSED_START, (5, time(6, 0)))
CLOSED_END = _parse_spec(MARKET_CLOSED_END, (0, time(6, 0)))


def _as_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _window_minutes() -> int:
    start_wd, start_at = CLOSED_START
    end_wd, end_at = CLOSED_END
    minutes = ((end_wd - start_wd) % 7) * 24 * 60
    minutes += (end_at.hour * 60 + end_at.minute) - (start_at.hour * 60 + start_at.minute)
    return minutes if minutes > 0 else 7 * 24 * 60


def market_closed_status(now: datetime | None = None) -> tuple[bool, str]:
    """Return ``(is_closed, reason)`` for the configured weekly closure."""
    local = _as_utc(now).astimezone(MARKET_TZ)
    start_wd, start_at = CLOSED_START
    span = timedelta(minutes=_window_minutes())
    for back in range(8):
        day = local.date() - timedelta(days=back)
        if day.weekday() != start_wd:
            continue
        start = datetime.combine(day, start_at, tzinfo=MARKET_TZ)
        if start <= local < start + span:
            end = start + span
            return True, (
                f"周末休市（{_DAY_NAMES[start_wd]} {start_at:%H:%M} -> "
                f"{_DAY_NAMES[CLOSED_END[0]]} {end.astimezone(MARKET_TZ):%H:%M}，北京时间）"
            )
    return False, "XAUUSD 正常交易时段"


def is_market_closed(now: datetime | None = None) -> bool:
    return market_closed_status(now)[0]
