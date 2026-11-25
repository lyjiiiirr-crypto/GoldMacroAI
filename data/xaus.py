"""XAUS 免费公开实时行情：解析与新鲜度校验（仅标准库）。

XAUS 提供免密钥 GET 接口，所有响应都带 ``data_state``
（status / as_of / source / age_seconds）。本模块只接受 ``status=fresh``
且 ``data_state.as_of`` 在允许时差内的真实数据；不补全、不外推、不伪造价格。

实测 ``/chart`` 的 upstream 收盘价可能与 ``/spot`` 相差数十美元，因此 4H CRT
只用 ``/chart`` 的已收盘 60m 高低点确定 P1/P2 结构；P3 优先用 TradingView
scanner 已收盘列，缺失时才用 ``/spot`` 实时价覆盖收盘。
``/intraday`` 5 分钟采样保留为备用参考。

注意：``data_state`` 描述的是响应"生成时"的新鲜度，经过 CDN/代理缓存时
仍可能显示 fresh。因此必须自行比较 ``as_of`` 与当前 UTC 时间。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import (
    XAUS_CHART_URL, XAUS_HISTORY_URL, XAUS_INTRADAY_URL, XAUS_SPOT_URL,
)
from data.common import FetchError, fetch_json

SOURCE_NAME = "XAUS 公开实时行情"
SYMBOL_LABELS = {
    "xau": "现货黄金 XAUUSD",
    "XAUUSD": "现货黄金 XAUUSD",
    "GC=F": "现货黄金 XAUUSD",
    "OANDA:XAUUSD": "现货黄金 XAUUSD",
}
DISCLAIMER = (
    "行情来自 XAUS 免费公开实时接口（XAU/USD 现货），已做 data_state 新鲜度校验；"
    "结构与指标为本地近似计算，未回测，非交易信号。"
)

# 允许的最大生成时差（秒）：采样价数分钟；小时级 chart 数分钟~2 小时；日线可放宽到 36 小时
MAX_AGE_INTRADAY = 30 * 60
MAX_AGE_CHART_INTRADAY = 2 * 3600
MAX_AGE_CHART_DAILY = 36 * 3600
MAX_AGE_HISTORY = 36 * 3600
MAX_AGE_SPOT = 15 * 60


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def data_as_of(payload: dict) -> datetime | None:
    """取响应生成/报价时刻（UTC）；优先 data_state.as_of。"""
    if not isinstance(payload, dict):
        return None
    state = payload.get("data_state")
    state = state if isinstance(state, dict) else {}
    raw = state.get("as_of") or payload.get("updated_at") or payload.get("price_as_of")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_fresh(payload: dict, *, max_age_seconds: int) -> bool:
    """校验 XAUS 响应是否可用：fresh + 未标记 stale + as_of 在允许时差内。"""
    if not isinstance(payload, dict):
        return False
    state = payload.get("data_state")
    state = state if isinstance(state, dict) else {}
    if str(state.get("status", "")).lower() != "fresh":
        return False
    if payload.get("stale") is True:
        return False
    # 必须显式提供 data_state.as_of；updated_at 之类字段不能替代新鲜度依据
    raw_as_of = state.get("as_of")
    if not raw_as_of:
        return False
    moment = data_as_of(payload)
    if moment is None:
        return False
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    if age < -120:  # 未来时间戳不可信
        return False
    return age <= max_age_seconds


def fetch_chart(symbol: str = "xau", range_: str = "3mo", interval: str = "60m",
                *, max_age_seconds: int | None = None) -> dict:
    """拉取并通过新鲜度校验的 chart 响应；失败/不新鲜抛 FetchError。"""
    url = XAUS_CHART_URL.format(symbol=symbol, range=range_, interval=interval)
    payload = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
    limit = max_age_seconds or (
        MAX_AGE_CHART_DAILY if interval in ("1d", "5d", "1wk", "1mo")
        else MAX_AGE_CHART_INTRADAY
    )
    if not is_fresh(payload, max_age_seconds=limit):
        state = payload.get("data_state") if isinstance(payload, dict) else None
        raise FetchError(f"XAUS chart 数据不新鲜或不可用：{url} {state}")
    return payload


def fetch_intraday(symbol: str = "xau", hours: int = 24, *,
                   max_age_seconds: int | None = None) -> dict:
    """拉取第一方 5 分钟采样价；与 spot 同源，失败/不新鲜抛 FetchError。"""
    url = XAUS_INTRADAY_URL.format(symbol=symbol, hours=hours)
    payload = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
    if not is_fresh(payload, max_age_seconds=max_age_seconds or MAX_AGE_INTRADAY):
        state = payload.get("data_state") if isinstance(payload, dict) else None
        raise FetchError(f"XAUS intraday 数据不新鲜或不可用：{url} {state}")
    return payload


def fetch_history(days: int = 400, *, max_age_seconds: int | None = None) -> dict:
    """拉取已定稿的日线 history；失败/不新鲜抛 FetchError。"""
    url = XAUS_HISTORY_URL.format(days=days)
    payload = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
    if not is_fresh(payload, max_age_seconds=max_age_seconds or MAX_AGE_HISTORY):
        state = payload.get("data_state") if isinstance(payload, dict) else None
        raise FetchError(f"XAUS history 数据不新鲜或不可用：{url} {state}")
    return payload


def fetch_spot(*, max_age_seconds: int | None = None) -> float:
    """拉取实时现货参考价（USD/oz）；失败/不新鲜抛 FetchError。"""
    payload = fetch_json(XAUS_SPOT_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
    if not is_fresh(payload, max_age_seconds=max_age_seconds or MAX_AGE_SPOT):
        state = payload.get("data_state") if isinstance(payload, dict) else None
        raise FetchError(f"XAUS spot 数据不新鲜或不可用：{state}")
    price = _number((payload.get("xau") or {}).get("price"))
    if price is None:
        price = _number(payload.get("spot_usd_oz"))
    if price is None:
        raise FetchError("XAUS spot 响应缺少有效价格")
    return price


def parse_ohlc(payload: dict, *, interval_seconds: int = 3600,
               drop_unclosed: bool = True) -> list[dict]:
    """解析 XAUS chart points 为按起始时刻升序的 OHLC。

    XAUS 沿用日线口径：``t`` 为 K 线起始时刻（日线为 05:00Z，即纽约交易日
    起点），因此结束时刻 = ``t + interval_seconds``。``drop_unclosed`` 时
    丢弃结束时刻晚于当前时间的未完成根，避免把盘中快照当成收盘价。
    """
    points = payload.get("points") if isinstance(payload, dict) else None
    rows: list[dict] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        close = _number(point.get("c"))
        start = _number(point.get("t"))
        if close is None or start is None:
            continue
        start_ts = int(start)
        end_ts = start_ts + interval_seconds
        if drop_unclosed and end_ts > int(datetime.now(timezone.utc).timestamp()):
            continue
        open_ = _number(point.get("o"))
        high = _number(point.get("h"))
        low = _number(point.get("l"))
        open_value = open_ if open_ is not None else close
        rows.append({
            "ts": start_ts,
            "end_ts": end_ts,
            "time": datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
            "open": open_value,
            "high": max(high if high is not None else close, close, open_value),
            "low": min(low if low is not None else close, close, open_value),
            "close": close,
            "volume": _number(point.get("v")),
        })
    rows.sort(key=lambda row: row["ts"])
    return rows


def parse_intraday(payload: dict) -> list[dict]:
    """解析 XAUS 第一方 5 分钟采样为按时间升序的 ``(ts, price)``。"""
    points = payload.get("points") if isinstance(payload, dict) else None
    rows: list[dict] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        ts = _number(point.get("t"))
        price = _number(point.get("p"))
        if ts is None or price is None or price <= 0:
            continue
        rows.append({"ts": int(ts), "price": price})
    rows.sort(key=lambda row: row["ts"])
    return rows


def parse_history(payload: dict, *, limit: int = 400) -> list[dict]:
    """解析 XAUS history points 为日线 OHLC；今天未收盘的根会被丢弃。"""
    points = payload.get("points") if isinstance(payload, dict) else None
    today = datetime.now(timezone.utc).date()
    rows: list[dict] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        raw_day = point.get("d")
        close = _number(point.get("c"))
        if not raw_day or close is None:
            continue
        try:
            day = datetime.strptime(str(raw_day)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if day >= today:  # 当日交易时段尚未结束，不能当作收盘价
            continue
        open_ = _number(point.get("o"))
        high = _number(point.get("h"))
        low = _number(point.get("l"))
        rows.append({
            "date": day.isoformat(),
            "open": open_ if open_ is not None else close,
            "high": max(high if high is not None else close, close),
            "low": min(low if low is not None else close, close),
            "close": close,
            "volume": None,
        })
    rows.sort(key=lambda row: row["date"])
    return rows[-limit:]


def history_as_of(payload: dict) -> str:
    """把 XAUS 响应时刻转成 ISO 字符串，便于写入 DB 的 as_of。"""
    moment = data_as_of(payload)
    if moment is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return moment.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
