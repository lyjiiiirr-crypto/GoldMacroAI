"""1H CRT（Candle Range Theory）六根 K 线模型。

六根结构（P1~P6，全部为已收盘 1H K 线）：
  对 P1~P5 中的任意一根 Ci：若 P6 最高价 > Ci 高点，或 P6 最低价 < Ci 低点，
  且 P6 收盘价回到 Ci 的 [低点, 高点] 内（即收回该根影线内），则 CRT 成立。
  扫该根低点后收回 → 偏多反转；扫高点后收回 → 偏空反转；
  扫后未收回 → 延续；同一根两侧都扫 → 中性待确认。
  多根同时满足时取最靠近 P6 的那根（P5 优先）作为主信号根。
P1~P5 的最高/最低价仅作背景区间，不再作为扫荡触发边界。
只描述已收线结构，不预测下一根；未回测，非交易信号。

数据源（免费公开、无密钥）：
  - XAUS ``/chart`` 60m 原生已收盘 OHLC（带 data_state 新鲜度校验）提供 6 根；
  - 最新一根 P6 优先用 TradingView scanner ``|60`` 的已收盘 ``[1]`` 列覆盖，
    且与 XAUS 收盘时间戳严格对齐，避免把未完成根当成收盘；
  - 不再用实时现货价覆盖收盘，确保 6 根都是完整 K 线；
  - 失败时依次回退 TradingView 偏移列 / Yahoo 1H / 本地历史快照。

1H 收线按 UTC 整点（北京时间整点）；云端采集检查在每小时 UTC hh:01，仅 CRT 成立时推送。
数据为免费公开近似值，未回测，非交易信号。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import BIQUOTE_CRT_URL, CRT_SYMBOLS, TV_CRT_SYMBOL, TV_CRT_URL, YHOO_CRT_URL
from data.common import fetch_json, fetch_text, fetch_yahoo_texts
from data.xaus import fetch_chart, fetch_intraday, parse_intraday, parse_ohlc

SOURCE_NAME = "TradingView 公开行情（1H CRT）"
XAUS_SOURCE = "XAUS 公开实时行情（60m 已收盘 OHLC）"
MIXED_SOURCE = "XAUS 60m 结构 + TradingView 已收盘 P6"
FALLBACK_SOURCE = "Yahoo Finance 公开行情（1H CRT）"
INTRADAY_SOURCE = "XAUS 5m采样聚合（1H CRT，近似）"
BIQUOTE_SOURCE = "Biquote 公开行情（1H CRT）"
SYMBOL_LABELS = {
    "GC=F": "COMEX黄金期货",
    "XAUUSD": "现货黄金 XAUUSD",
    "OANDA:XAUUSD": "现货黄金 XAUUSD",
}
TIMEFRAME = "1h"
BUCKET_SECONDS = 3600
CANDLE_COUNT = 6
RANGE_CANDLE_COUNT = 5
MAX_CANDLE_AGE_SECONDS = 3 * BUCKET_SECONDS  # 最新已收盘根超过 3 小时视为过期，不可用于 CRT
DISCLAIMER = (
    "1H CRT 的 P1~P5 优先取自 XAUS 免费公开实时行情（60m 已收盘 K 线），"
    "最新一根 P6 优先用 TradingView scanner 的已收盘列覆盖；"
    "失败时回退 TradingView / Yahoo Finance。逐根判定：P6 扫过 P1~P5 中任意一根"
    "K 线的高点或低点，且收盘回到该根 [低点, 高点] 内才算 CRT 成立；"
    "多根满足时取最靠近 P6 的那根（P5 优先）。为价格结构研究与流程提醒，"
    "未回测，不构成交易信号。"
)


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _hour_close(ts: int | float | None = None) -> int:
    """返回不晚于给定时刻的最近一个 UTC 整点（即 1H 已收盘时刻）。"""
    moment = int(ts if ts is not None else time.time())
    return (moment // BUCKET_SECONDS) * BUCKET_SECONDS


def parse_yahoo_intraday(payload: dict, limit: int = 400) -> list[dict]:
    """解析 Yahoo chart JSON，保留小时级时间戳（不折叠成日期）。"""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    columns = {
        key: quote.get(key) or []
        for key in ("open", "high", "low", "close")
    }
    rows: list[dict] = []
    for i, ts in enumerate(timestamps):
        close = _number(columns["close"][i]) if i < len(columns["close"]) else None
        if close is None:
            continue
        try:
            value = float(ts)
            if value > 1e11:
                value /= 1000.0
            datetime.fromtimestamp(value, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        open_ = _number(columns["open"][i]) if i < len(columns["open"]) else None
        high = _number(columns["high"][i]) if i < len(columns["high"]) else None
        low = _number(columns["low"][i]) if i < len(columns["low"]) else None
        open_value = open_ if open_ is not None else close
        rows.append({
            "ts": int(value),
            "time": _iso(int(value)),
            "open": open_value,
            "high": max(high if high is not None else close, close, open_value),
            "low": min(low if low is not None else close, close, open_value),
            "close": close,
        })
    rows.sort(key=lambda r: r["ts"])
    return rows[-limit:]


def drop_unclosed_bars(bars: list[dict], now_ts: int | float | None = None) -> list[dict]:
    """丢弃尚未收线的 1H K 线，禁止用盘中快照冒充收盘价。"""
    now = int(now_ts if now_ts is not None else time.time())
    rows: list[dict] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        end = _number(bar.get("end_ts"))
        if end is None:
            end = (_number(bar.get("ts")) or 0) + BUCKET_SECONDS
        if int(end) <= now:
            rows.append(bar)
    return rows


def _candle(open_, high, low, close, close_ts: int, source: str | None = None) -> dict:
    """统一为「ts = 起始时刻、end_ts/time = 收线时刻」的已收盘 1H K 线。"""
    close_ts = int(close_ts)
    return {
        "ts": close_ts - BUCKET_SECONDS,
        "end_ts": close_ts,
        "time": _iso(close_ts),
        "open": float(open_), "high": float(high),
        "low": float(low), "close": float(close),
        **({"source": source} if source else {}),
    }


def _closed_candle(bar: dict, source: str | None = None) -> dict:
    """把带起始 ts（或已有 end_ts）的原始 K 线规范为已收盘 1H K 线。"""
    end = _number(bar.get("end_ts"))
    if end is None:
        end = (_number(bar.get("ts")) or 0) + BUCKET_SECONDS
    return _candle(
        bar["open"], bar["high"], bar["low"], bar["close"], int(end),
        source or bar.get("source"),
    )


def _synthesize_candle(high, low, open_value=None, close_value=None,
                       time_value=None, ts_value: int | None = None) -> dict:
    high, low = float(high), float(low)
    if high < low:
        high, low = low, high
    middle = (high + low) / 2.0
    if ts_value is None and time_value:
        try:
            dt = datetime.fromisoformat(str(time_value).replace("Z", "+00:00"))
            ts_value = int(dt.timestamp())
        except (TypeError, ValueError):
            ts_value = 0
    ts_value = int(ts_value or 0)
    if ts_value and ts_value % BUCKET_SECONDS not in (0,):
        # 手工导入可能给的是区间内任意时刻，按所在小时收线对齐
        ts_value = ((ts_value // BUCKET_SECONDS) + 1) * BUCKET_SECONDS
    return _candle(
        open_value if open_value is not None else middle,
        high, low,
        close_value if close_value is not None else middle,
        ts_value or int(time.time() // BUCKET_SECONDS * BUCKET_SECONDS),
    )


def parse_tv_candles(payload: dict, *, now_ts: int | float | None = None) -> list[dict]:
    """解析 scanner 的 ``[1]``/``[2]``/``[3]`` 偏移列，取可用的已收盘 1H。

    ``[1]`` 是最近一根已收盘，`[2]` 前一根，`[3]` 再前一根。免费 scanner 常只
    返回 `[1]`，因此允许部分偏移：只要某组 4 个 OHLC 完整即保留。收线时刻按
    UTC 整点计算，不依赖 scanner 的 `time` 字段。
    """
    rows = (payload or {}).get("data") or []
    if not rows:
        return []
    values = rows[0].get("d") if isinstance(rows[0], dict) else None
    if not isinstance(values, list) or len(values) < 12:
        return []
    last_closed = _hour_close(now_ts)
    candles: list[dict] = []
    for k in (1, 2, 3):
        base = k * 4
        open_, high, low, close = (_number(values[i]) for i in range(base, base + 4))
        if None in (open_, high, low, close):
            continue
        close_ts = last_closed - (k - 1) * BUCKET_SECONDS
        candles.append(_candle(open_, high, low, close, close_ts, SOURCE_NAME))
    candles.reverse()  # 按时间升序：越早的偏移越靠前
    return candles


def parse_tv_scan(payload: dict, *, now_ts: int | float | None = None) -> dict | None:
    """解析 scanner 当前（未完成）1H OHLC；仅作 DB 历史不足时的兜底。"""
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    values = rows[0].get("d") if isinstance(rows[0], dict) else None
    if not isinstance(values, list) or len(values) < 4:
        return None
    open_, high, low, close = (_number(values[i]) for i in range(4))
    if None in (open_, high, low, close):
        return None
    return _candle(open_, high, low, close,
                   _hour_close(now_ts) + BUCKET_SECONDS, SOURCE_NAME)


def _tv_scan(symbol: str) -> list[dict]:
    """从 TradingView 公开 scanner 获取可用的已收盘 1H K 线（至少含 `[1]`）。"""
    columns: list[str] = []
    for suffix in ("", "[1]", "[2]", "[3]"):
        columns += [f"open{suffix}|60", f"high{suffix}|60",
                    f"low{suffix}|60", f"close{suffix}|60"]
    body = {
        "symbols": {"tickers": [symbol], "query": {"types": []}},
        "columns": columns,
    }
    try:
        payload = fetch_json(
            TV_CRT_URL, post_json=body,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
        )
    except Exception:  # noqa: BLE001 - 回退 Yahoo
        return []
    candles = parse_tv_candles(payload)
    if candles:
        return candles
    # 偏移列完全不可用：退而求其次，只取当前根（由调用方用 DB/Yahoo 补历史）
    current = parse_tv_scan(payload)
    return [current] if current else []


def _fetch_xaus_candles(symbol: str) -> list[dict]:
    """XAUS /chart 60m 原生已收盘 1H K 线（fetch_chart 已做新鲜度校验）。

    60m 就是目标时间框架，无需 4H 聚合；``parse_ohlc`` 已丢弃未完成根，
    这里再按收线时刻过滤一次，确保 P1~P6 全部为完整 K 线。
    """
    if symbol.upper() not in {"XAUUSD", "OANDA:XAUUSD", "GC=F"}:
        return []
    try:
        payload = fetch_chart("xau", "5d", "60m")
        bars = parse_ohlc(payload, interval_seconds=BUCKET_SECONDS)
        return [_closed_candle(bar, XAUS_SOURCE)
                for bar in drop_unclosed_bars(bars)]
    except Exception:  # noqa: BLE001 - 回退 TradingView / Yahoo
        return []


def _aggregate_intraday_to_1h(points: list[dict], *, now_ts: int | float | None = None) -> list[dict]:
    """将 XAUS 5m 采样点聚合为已收盘 1H OHLC（近似值，非交易所官方K线）。"""
    cutoff = _hour_close(now_ts)
    buckets: dict[int, list[dict]] = {}
    for point in points or []:
        ts = _number(point.get("ts"))
        price = _number(point.get("price"))
        if ts is None or price is None or price <= 0:
            continue
        end_ts = ((int(ts) // BUCKET_SECONDS) + 1) * BUCKET_SECONDS
        if end_ts > cutoff:
            continue
        buckets.setdefault(end_ts, []).append({"ts": int(ts), "price": float(price)})
    bars: list[dict] = []
    for end_ts in sorted(buckets):
        samples = sorted(buckets[end_ts], key=lambda row: row["ts"])
        if len(samples) < 6:  # 5m 采样完整小时应有~12点；过少则不可靠
            continue
        prices = [row["price"] for row in samples]
        bars.append(_candle(prices[0], max(prices), min(prices), prices[-1], end_ts, INTRADAY_SOURCE))
    return bars


def _fetch_xaus_intraday_candles(symbol: str) -> list[dict]:
    """XAUS /intraday 5m 采样聚合为 1H K 线回退（fetch_intraday 已做新鲜度校验）。"""
    if symbol.upper() not in {"XAUUSD", "OANDA:XAUUSD", "GC=F"}:
        return []
    try:
        payload = fetch_intraday("xau", 48)
        bars = _aggregate_intraday_to_1h(parse_intraday(payload))
        return [_closed_candle(bar, INTRADAY_SOURCE) for bar in drop_unclosed_bars(bars)]
    except Exception:  # noqa: BLE001
        return []


def _fetch_biquote_candles(symbol: str) -> list[dict]:
    """Biquote 免费公开 OHLC（无key）；openTime 为 bar 起始 UTC 时间。"""
    sym = "XAUUSD" if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD", "GC=F"} else symbol
    url = BIQUOTE_CRT_URL.replace("{symbol}", sym)
    try:
        payload = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    bars_out: list[dict] = []
    for bar in (payload or {}).get("bars") or []:
        if not isinstance(bar, dict) or bar.get("isOpen"):
            continue
        start = str(bar.get("openTime") or "").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        end_ts = int(dt.timestamp()) + BUCKET_SECONDS
        o, h, l, c = (_number(bar.get("open")), _number(bar.get("high")),
                      _number(bar.get("low")), _number(bar.get("close")))
        if None in (o, h, l, c):
            continue
        bars_out.append(_candle(o, h, l, c, end_ts, BIQUOTE_SOURCE))
    bars_out.sort(key=lambda r: r["end_ts"])
    return [_closed_candle(b, BIQUOTE_SOURCE) for b in drop_unclosed_bars(bars_out)]


def _fetch_yahoo_candles(symbol: str) -> list[dict]:
    """Yahoo 1H 行情回退；现货 XAUUSD=X 常无分钟数据，改用 GC=F 代理。"""
    spot = "XAUUSD=X" if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD"} else symbol
    candidates = [spot] + (["GC=F"] if spot == "XAUUSD=X" else [])
    urls: list[str] = []
    for host in ("query1", "query2"):
        for item in candidates:
            url = YHOO_CRT_URL.replace("{symbol}", item).replace("{interval}", "60m")
            urls.append(url.replace("query1.finance.yahoo.com",
                                    f"{host}.finance.yahoo.com"))
    payloads: list[dict] = []
    try:
        for text in fetch_yahoo_texts(urls[:2], timeout=45):
            if not text:
                continue
            try:
                payloads.append(json.loads(text))
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        pass
    if not payloads:
        for url in urls:
            try:
                payloads.append(json.loads(fetch_text(url, timeout=30)))
            except Exception:  # noqa: BLE001
                continue
    for payload in payloads:
        bars = drop_unclosed_bars(parse_yahoo_intraday(payload))
        if len(bars) >= CANDLE_COUNT:
            return [_closed_candle(bar, FALLBACK_SOURCE) for bar in bars]
    return []


def _reject_stale_candles(candles: list[dict], *, now_ts: int | float | None = None) -> list[dict]:
    """拒绝过期 K 线：最新已收盘根距当前整点过久则整组丢弃，避免陈旧报价冒充实时。"""
    if len(candles) < CANDLE_COUNT:
        return []
    latest_end = int(candles[-1].get("end_ts") or 0)
    if latest_end < _hour_close(now_ts) - MAX_CANDLE_AGE_SECONDS:
        return []
    return candles


def _history_from_db(db, symbol: str, limit: int = CANDLE_COUNT) -> list[dict]:
    """从 DB 快照的 payload.candles 取最近的已收盘 K 线（供兜底）。"""
    try:
        rows = db.crt_snapshots(symbol=symbol, limit=8)
    except Exception:  # noqa: BLE001
        return []
    seen: set[int] = set()
    candles: list[dict] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        for candle in reversed(payload.get("candles") or []):
            close_ts = _number(candle.get("end_ts"))
            if close_ts is None:
                close_ts = _number(candle.get("ts"))
            if close_ts is None or int(close_ts) in seen:
                continue
            high, low = _number(candle.get("high")), _number(candle.get("low"))
            if high is None or low is None:
                continue
            seen.add(int(close_ts))
            candles.append(_candle(
                candle.get("open") or (high + low) / 2.0, high, low,
                candle.get("close") or high, int(close_ts),
                candle.get("source") or row.get("source"),
            ))
        if len(candles) >= limit:
            break
    candles.sort(key=lambda c: c["end_ts"])
    return candles[-limit:]


def p6_vs_candles(candles: list[dict]) -> dict:
    """逐根判定 P6 是否扫过 P1~P5 中任意一根并收回该根影线内。

    - 扫过：P6 高点 > Ci 高点，或 P6 低点 < Ci 低点（严格越过）。
    - 收回：P6 收盘价落在 Ci 的 [低点, 高点] 内。
    - 主信号根：从 P5 往 P1 找，取最靠近 P6 的满足根；先找「扫过且收回」，
      都未收回时才退而取最近「扫过但未收回」的根用于延续情景。
    - P1~P5 的最高/最低价只作背景区间，不是触发边界。
    距离口径：dist_p6_high_to_signal_high = P6高 - 信号根高（正数=已扫该根高点）；
    dist_p6_low_to_signal_low = P6低 - 信号根低（负数=已扫该根低点）。
    """
    if len(candles) < CANDLE_COUNT:
        raise ValueError(f"需要至少 {CANDLE_COUNT} 根已收盘 1H K 线")
    range_candles = candles[-CANDLE_COUNT:-1]  # P1~P5，时间升序
    p6 = candles[-1]
    range_high = max(float(c["high"]) for c in range_candles)
    range_low = min(float(c["low"]) for c in range_candles)
    p6_high, p6_low = float(p6["high"]), float(p6["low"])
    p6_close = float(p6.get("close") if p6.get("close") is not None else p6["low"])

    reclaimed_candidates: list[dict] = []
    swept_candidates: list[dict] = []
    # P5 -> P1，保证第一个命中就是最靠近 P6 的信号根
    for idx in range(len(range_candles) - 1, -1, -1):
        candle = range_candles[idx]
        c_high, c_low = float(candle["high"]), float(candle["low"])
        swept_high = p6_high > c_high
        swept_low = p6_low < c_low
        if not (swept_high or swept_low):
            continue
        reclaimed = c_low <= p6_close <= c_high
        entry = {
            "signal_index": idx + 1,
            "signal_label": f"P{idx + 1}",
            "signal_open": round(float(candle.get("open") or c_low), 2),
            "signal_high": round(c_high, 2),
            "signal_low": round(c_low, 2),
            "signal_close": round(float(candle.get("close") or c_low), 2),
            "swept_high": swept_high,
            "swept_low": swept_low,
            "swept_side": "双" if swept_high and swept_low else ("高" if swept_high else "低"),
            "reclaimed_wick": reclaimed,
            "dist_p6_high_to_signal_high": round(p6_high - c_high, 2),
            "dist_p6_low_to_signal_low": round(p6_low - c_low, 2),
        }
        if reclaimed:
            reclaimed_candidates.append(entry)
        else:
            swept_candidates.append(entry)

    valid = bool(reclaimed_candidates)
    chosen = (reclaimed_candidates or swept_candidates or [None])[0]

    if chosen is None:
        side = None
        scenario = "P6 未扫 P1-P5 任一 K 线高低点 · CRT 未成立 · 待确认"
        bias = "中性"
        target = stop_ref = None
        reclaim_text = "未扫过 P1-P5 任一 K 线，无收回判定"
    else:
        side = chosen["swept_side"]
        label = chosen["signal_label"]
        reclaim_text = (
            f"收盘收回 {label} 影线内（{chosen['signal_low']:.2f}~{chosen['signal_high']:.2f}）"
            if chosen["reclaimed_wick"] else
            f"收盘未收回 {label} 影线内（{chosen['signal_low']:.2f}~{chosen['signal_high']:.2f}）"
        )
        if side == "双":
            scenario = (
                f"P6 同时扫 {label} 高低点后收回该根影线内 · CRT 成立 · 双向扫荡待确认"
                if valid else
                f"P6 同时扫 {label} 高低点但未收回该根影线内 · CRT 未成立"
            )
            bias = "中性"
            target = stop_ref = None
        elif side == "低":
            if valid:
                scenario = f"P6 扫 {label} 低点后收回该根影线内 · CRT 成立 · 偏多反转确认"
                bias = "偏多"
                target, stop_ref = chosen["signal_high"], p6_low
            else:
                scenario = f"P6 扫 {label} 低点后未收回该根影线内 · CRT 未成立 · 偏空延续"
                bias = "偏空"
                target, stop_ref = None, chosen["signal_low"]
        else:  # side == "高"
            if valid:
                scenario = f"P6 扫 {label} 高点后收回该根影线内 · CRT 成立 · 偏空反转确认"
                bias = "偏空"
                target, stop_ref = chosen["signal_low"], p6_high
            else:
                scenario = f"P6 扫 {label} 高点后未收回该根影线内 · CRT 未成立 · 偏多延续"
                bias = "偏多"
                target, stop_ref = None, chosen["signal_high"]

    result = {
        # 背景区间（仅参考，不是触发边界）
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "p6_high": round(p6_high, 2),
        "p6_low": round(p6_low, 2),
        "p6_close": round(p6_close, 2),
        "crt_valid": valid,
        "p6_scenario": scenario,
        "p6_bias": bias,
        "p6_target": round(target, 2) if target is not None else None,
        "p6_stop_ref": round(stop_ref, 2) if stop_ref is not None else None,
        "reclaim_text": reclaim_text,
    }
    if chosen:
        result.update(chosen)
    else:
        result.update({
            "signal_index": None, "signal_label": None,
            "signal_open": None, "signal_high": None,
            "signal_low": None, "signal_close": None,
            "swept_high": False, "swept_low": False, "swept_side": None,
            "reclaimed_wick": False,
            "dist_p6_high_to_signal_high": None,
            "dist_p6_low_to_signal_low": None,
        })

    # 旧字段名兼容：指向主信号根（而非聚合区间）
    result.update({
        "p6_sweep_side": result["swept_side"],
        "p6_swept_range_high": result["swept_high"],
        "p6_swept_range_low": result["swept_low"],
        "p6_reclaimed_range": result["reclaimed_wick"],
        "p6_reclaim_text": reclaim_text,
        "dist_p6_high_to_range_high": result["dist_p6_high_to_signal_high"],
        "dist_p6_low_to_range_low": result["dist_p6_low_to_signal_low"],
    })
    return result


# 旧名称兼容别名；语义已改为逐根判定
p6_vs_range = p6_vs_candles


def crt_snapshot(symbol: str, candles: list[dict]) -> dict:
    """由至少 6 根已收盘 1H K 线计算六根 CRT 结构与执行参考位。"""
    if len(candles) < CANDLE_COUNT:
        raise ValueError(
            f"{symbol} 1H 已收盘 K 线不足 {CANDLE_COUNT} 根，无法计算六根 CRT"
        )
    six = [dict(c) for c in candles[-CANDLE_COUNT:]]
    p6 = p6_vs_candles(six)
    range_high, range_low = p6["range_high"], p6["range_low"]
    signal_high, signal_low = p6["signal_high"], p6["signal_low"]
    ref_high = signal_high if signal_high is not None else range_high
    ref_low = signal_low if signal_low is not None else range_low
    sixth = six[-1]
    c6_open = float(sixth.get("open") or sixth["low"])
    c6_close = float(sixth.get("close") or sixth["low"])
    equilibrium = (ref_high + ref_low) / 2.0
    body_pct = (
        round(abs(c6_close - c6_open) / (p6["p6_high"] - p6["p6_low"]) * 100.0, 1)
        if p6["p6_high"] > p6["p6_low"] else 0.0
    )

    side, label = p6["swept_side"], p6["signal_label"]
    if label:
        if side == "双":
            sweep_text = f"同时扫 {label} 高低点"
        elif side:
            sweep_text = f"扫 {label} {side}点"
        else:
            sweep_text = f"未扫 {label} 高低点"
        sweep_desc = (
            f"P6 {sweep_text}（高 {p6['p6_high']:.2f} / 低 {p6['p6_low']:.2f} / "
            f"收 {p6['p6_close']:.2f}）；{p6['reclaim_text']}"
        )
    else:
        sweep_desc = (
            f"P6 未扫 P1-P5 任一 K 线高低点（高 {p6['p6_high']:.2f} / "
            f"低 {p6['p6_low']:.2f} / 收 {p6['p6_close']:.2f}）"
        )
    scenario, bias = p6["p6_scenario"], p6["p6_bias"]
    summary = (
        f"P1-P5 背景区间 高 {range_high:.2f} / 低 {range_low:.2f}；"
        f"{sweep_desc}；情景 {scenario}（{bias}）。"
    )
    return {
        "source": sixth.get("source") or SOURCE_NAME,
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "as_of": sixth["time"],
        "timeframe": TIMEFRAME,
        "rule_version": 2,  # 逐根 K 线判定（旧快照缺此字段时按 6 根重算）
        **{f"candle{i}": dict(c) for i, c in enumerate(six, start=1)},
        "range_high": range_high,
        "range_low": range_low,
        # 旧字段兼容：指向主信号根边界，P6 即当前 K 线
        "sweep_high": ref_high, "sweep_low": ref_low,
        "current_high": p6["p6_high"], "current_low": p6["p6_low"],
        "current_close": p6["p6_close"],
        "prev_high": ref_high, "prev_low": ref_low,
        "equilibrium": round(equilibrium, 2),
        "body_pct": body_pct,
        "sweep_side": side,
        "swept_prev_high": p6["swept_high"],
        "swept_prev_low": p6["swept_low"],
        **p6,
        "dist_high_to_resistance": round(ref_high - p6["p6_high"], 2),
        "dist_low_to_support": round(p6["p6_low"] - ref_low, 2),
        "dist_sweep_low_to_support": (
            round(p6["p6_low"] - ref_low, 2) if ref_low is not None else None
        ),
        "dist_sweep_high_to_resistance": (
            round(ref_high - p6["p6_high"], 2) if ref_high is not None else None
        ),
        "scenario": scenario,
        "bias": bias,
        "target": p6["p6_target"],
        "stop_ref": p6["p6_stop_ref"],
        "summary": summary,
        "candles": six,
        "disclaimer": DISCLAIMER,
    }


def _normalize_manual(bars: list) -> list[dict]:
    """手工导入：丢弃未完成根并统一时间戳；保留用户给的已收盘 K 线。"""
    rows = [bar for bar in (bars or []) if isinstance(bar, dict)]
    closed = drop_unclosed_bars(rows)
    return [_closed_candle(bar) for bar in closed]


def snapshot_from_data(symbol: str, data: dict) -> dict:
    """手动导入：支持 6 根 / chart 原始响应 / 旧版 3 根或高低点格式。"""
    if not isinstance(data, dict):
        raise ValueError("CRT 导入数据必须是 JSON 对象")

    if "chart" in data:
        return crt_snapshot(symbol, _normalize_manual(parse_yahoo_intraday(data)))
    if "candles" in data:
        return crt_snapshot(symbol, _normalize_manual(data["candles"]))

    if all(isinstance(data.get(f"candle{i}"), dict) for i in range(1, CANDLE_COUNT + 1)):
        candles = []
        for i in range(1, CANDLE_COUNT + 1):
            item = data[f"candle{i}"]
            candles.append(_synthesize_candle(
                item.get("high"), item.get("low"), item.get("open"), item.get("close"),
                item.get("time"), item.get("ts") or item.get("end_ts"),
            ))
        snap = crt_snapshot(symbol, candles)
        if data.get("as_of"):
            snap["as_of"] = str(data["as_of"])
        return snap

    # 旧版三根结构：补齐为 6 根，标记为兼容导入，不覆盖用户已有结论
    if all(isinstance(data.get(f"candle{i}"), dict) for i in range(1, 4)):
        items = [data[f"candle{i}"] for i in range(1, 4)]
        candles = [_synthesize_candle(
            item.get("high"), item.get("low"), item.get("open"), item.get("close"),
            item.get("time"), item.get("ts") or item.get("end_ts"),
        ) for item in items]
        snap = crt_snapshot(symbol, [candles[0]] * 4 + candles[1:])
        snap["legacy_import"] = True
        for key in ("scenario", "bias", "summary"):
            if data.get(key):
                snap[key] = data[key]
        return snap

    required = ("range_high", "range_low", "sweep_high", "sweep_low",
                "current_high", "current_low")
    if all(key in data for key in required):
        range_candle = _synthesize_candle(
            data["range_high"], data["range_low"], data.get("range_open"),
            data.get("range_close"), data.get("range_time"),
        )
        sweep_candle = _synthesize_candle(
            data["sweep_high"], data["sweep_low"], data.get("sweep_open"),
            data.get("sweep_close"), data.get("sweep_time"),
        )
        current = _synthesize_candle(
            data["current_high"], data["current_low"], data.get("current_open"),
            data.get("current_close"), data.get("as_of"),
        )
        snap = crt_snapshot(symbol, [range_candle] * 4 + [sweep_candle, current])
        snap["legacy_import"] = True
        for key in ("scenario", "bias", "summary"):
            if data.get(key):
                snap[key] = data[key]
        return snap

    # 旧版两根快照兼容：用相同区间补齐前 5 根，避免人工导入失效
    old_required = ("current_high", "current_low", "prev_high", "prev_low")
    if all(key in data for key in old_required):
        shared = _synthesize_candle(data["prev_high"], data["prev_low"])
        current = _synthesize_candle(
            data["current_high"], data["current_low"],
            data.get("current_open"), data.get("current_close"), data.get("as_of"),
        )
        snap = crt_snapshot(symbol, [shared] * (CANDLE_COUNT - 1) + [current])
        snap["legacy_import"] = True
        for key in ("scenario", "bias", "summary"):
            if data.get(key):
                snap[key] = data[key]
        return snap
    raise ValueError(
        f"CRT 快照需要 {CANDLE_COUNT} 根 K 线（candle1~candle{CANDLE_COUNT} / candles）"
        "或兼容的区间高低点字段"
    )


def collect(db, symbols: list[str] | tuple[str, ...] | None = None) -> dict:
    """采集六根 1H CRT 并写入 crt_snapshots（来源+标的+时间幂等）。"""
    snapshots: list[dict] = []
    errors: list[str] = []
    for symbol in list(symbols or CRT_SYMBOLS):
        try:
            snap = _collect_one(db, symbol)
            raw = json.dumps({"candles": snap.get("candles", [])}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        db.upsert_crt_snapshot(
            source=snap["source"], symbol=symbol, as_of=snap["as_of"],
            timeframe=snap["timeframe"], current_high=snap["current_high"],
            current_low=snap["current_low"], prev_high=snap["prev_high"],
            prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
            scenario=snap["scenario"], bias=snap["bias"],
            summary=snap["summary"], payload=snap, raw_json=raw,
        )
        snapshots.append(snap)

    if not snapshots:
        return {
            "ok": False, "source": SOURCE_NAME, "snapshots": 0,
            "error": "CRT 采集失败：" + "；".join(errors[:2]),
        }
    latest = snapshots[0]
    return {
        "ok": True, "source": latest.get("source", SOURCE_NAME),
        "snapshots": len(snapshots), "errors": errors or None,
        "latest": {
            "date": latest["as_of"], "symbol": latest["symbol"],
            "high": latest["current_high"], "low": latest["current_low"],
            "scenario": latest["scenario"], "bias": latest["bias"],
        },
    }


def _collect_one(db, symbol: str) -> dict:
    """取最近 6 根已收盘 1H K 线。

    优先 XAUS 60m 已收盘 K 线；P6 若能用时间戳对齐的 TradingView `[1]` 覆盖
    则用混合来源，否则保留 XAUS 收盘。失败时依次回退 TradingView / Yahoo 1H /
    本地历史快照。全程不使用实时现货价，确保 P1~P6 都是完整 K 线。
    """
    tv_symbol = TV_CRT_SYMBOL if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD"} else symbol
    tv_candles = _tv_scan(tv_symbol)

    # 1) XAUS 实时行情：fetch_chart 已校验新鲜度，parse_ohlc 已丢弃未完成根
    xaus = _fetch_xaus_candles(symbol)
    if len(xaus) >= CANDLE_COUNT:
        candles = [dict(c) for c in xaus[-CANDLE_COUNT:]]
        for candle in candles:
            candle["source"] = XAUS_SOURCE

        # TradingView 免费 scanner 通常只有 `[1]`（最近已收盘 P6）。时间戳必须
        # 与 XAUS 最新根一致，避免把当前未完成根误当成 P6。
        target_ts = candles[-1].get("end_ts")
        tv_latest = next(
            (item for item in reversed(tv_candles) if item.get("end_ts") == target_ts),
            None,
        )
        if tv_latest is not None:
            latest = candles[-1]
            latest["open"] = tv_latest["open"]
            latest["high"] = tv_latest["high"]
            latest["low"] = tv_latest["low"]
            latest["close"] = tv_latest["close"]
            latest["close_source"] = "TradingView 已收盘 [1]"
            latest["source"] = MIXED_SOURCE
            snap = crt_snapshot(symbol, candles)
            snap["source"] = MIXED_SOURCE
            return snap

        snap = crt_snapshot(symbol, candles)
        snap["source"] = XAUS_SOURCE
        return snap

    # 2) Biquote 免费公开 OHLC 回退（无key，完整H/L/C，优先于5m采样聚合）
    biquote = _reject_stale_candles(_fetch_biquote_candles(symbol))
    if len(biquote) >= CANDLE_COUNT:
        candles = [dict(c) for c in biquote[-CANDLE_COUNT:]]
        for candle in candles:
            candle["source"] = BIQUOTE_SOURCE
        target_ts = candles[-1].get("end_ts")
        tv_latest = next(
            (item for item in reversed(tv_candles) if item.get("end_ts") == target_ts),
            None,
        )
        if tv_latest is not None:
            latest = candles[-1]
            latest["open"] = tv_latest["open"]
            latest["high"] = tv_latest["high"]
            latest["low"] = tv_latest["low"]
            latest["close"] = tv_latest["close"]
            latest["close_source"] = "TradingView 已收盘 [1]"
            latest["source"] = "Biquote 1H 结构 + TradingView 已收盘 P6"
            snap = crt_snapshot(symbol, candles)
            snap["source"] = "Biquote 1H 结构 + TradingView 已收盘 P6"
            return snap
        snap = crt_snapshot(symbol, candles)
        snap["source"] = BIQUOTE_SOURCE
        return snap

    # 3) XAUS 5m 采样聚合回退：chart 60m 过期时，用新鲜 intraday 聚合 1H（近似）
    intraday = _reject_stale_candles(_fetch_xaus_intraday_candles(symbol))
    if len(intraday) >= CANDLE_COUNT:
        candles = [dict(c) for c in intraday[-CANDLE_COUNT:]]
        for candle in candles:
            candle["source"] = INTRADAY_SOURCE
        target_ts = candles[-1].get("end_ts")
        tv_latest = next(
            (item for item in reversed(tv_candles) if item.get("end_ts") == target_ts),
            None,
        )
        if tv_latest is not None:
            latest = candles[-1]
            latest["open"] = tv_latest["open"]
            latest["high"] = tv_latest["high"]
            latest["low"] = tv_latest["low"]
            latest["close"] = tv_latest["close"]
            latest["close_source"] = "TradingView 已收盘 [1]"
            latest["source"] = MIXED_SOURCE
            snap = crt_snapshot(symbol, candles)
            snap["source"] = MIXED_SOURCE
            return snap
        snap = crt_snapshot(symbol, candles)
        snap["source"] = INTRADAY_SOURCE
        return snap

    # 4) TradingView 偏移列可用：直接得到已收盘 K 线
    tv_fresh = _reject_stale_candles(tv_candles[-CANDLE_COUNT:]) if len(tv_candles) >= CANDLE_COUNT else []
    if len(tv_fresh) >= CANDLE_COUNT:
        snap = crt_snapshot(symbol, tv_fresh)
        snap["source"] = SOURCE_NAME
        return snap

    # 5) Yahoo 1H 聚合回退（拒绝过期报价，避免陈旧数据冒充实时）
    yahoo = _reject_stale_candles(_fetch_yahoo_candles(symbol))
    if len(yahoo) >= CANDLE_COUNT:
        candles = yahoo[-CANDLE_COUNT:]
        for candle in candles:
            candle["source"] = FALLBACK_SOURCE
        snap = crt_snapshot(symbol, candles)
        snap["source"] = FALLBACK_SOURCE
        if tv_candles:
            snap["tv_price"] = tv_candles[-1].get("close")
        return snap

    # 6) 本地历史：仅在最新根仍新鲜时兜底；过期历史不参与 CRT 判定
    history = _reject_stale_candles(_history_from_db(db, symbol, limit=CANDLE_COUNT))
    if len(history) >= CANDLE_COUNT:
        snap = crt_snapshot(symbol, history[-CANDLE_COUNT:])
        snap["source"] = "本地历史快照"
        if tv_candles:
            snap["tv_price"] = tv_candles[-1].get("close")
        return snap

    raise ValueError(
        f"{symbol} 无法获得 {CANDLE_COUNT} 根已收盘 1H K 线"
        "（XAUS chart/Biquote/intraday/TradingView/Yahoo/本地历史均不可用）"
    )
