"""4H CRT（Candle Range Theory）模型高低点。

优先使用 TradingView 公开 scanner 行情（OANDA:XAUUSD 4H），失败时回退
Yahoo Finance 1H 行情本地聚合为 UTC 4H。CRT 只关心每根 K 线的高点/低点，
4H 用来判断延续或反转与关键价位，2M/5M 才用于执行。

数据来自免费公开行情，结果为近似值，未回测，非交易信号。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import CRT_SYMBOLS, TV_CRT_SYMBOL, TV_CRT_URL, YHOO_CRT_URL
from data.common import fetch_json, fetch_yahoo_texts

SOURCE_NAME = "TradingView 公开行情（4H CRT）"
FALLBACK_SOURCE = "Yahoo Finance 公开行情（本地聚合 4H CRT）"
SYMBOL_LABELS = {
    "GC=F": "COMEX黄金期货",
    "XAUUSD": "现货黄金 XAUUSD",
    "OANDA:XAUUSD": "现货黄金 XAUUSD",
}
TIMEFRAME = "4h"
BUCKET_SECONDS = 4 * 3600
DISCLAIMER = (
    "4H CRT 高低点优先取自 TradingView 公开行情（OANDA:XAUUSD 4H），"
    "失败时回退 Yahoo Finance 1H 本地聚合；仅为价格结构研究与流程提醒，"
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
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        open_ = _number(columns["open"][i]) if i < len(columns["open"]) else None
        high = _number(columns["high"][i]) if i < len(columns["high"]) else None
        low = _number(columns["low"][i]) if i < len(columns["low"]) else None
        open_value = open_ if open_ is not None else close
        rows.append({
            "ts": int(value),
            "time": moment.isoformat(),
            "open": open_value,
            "high": max(high if high is not None else close, close, open_value),
            "low": min(low if low is not None else close, close, open_value),
            "close": close,
        })
    rows.sort(key=lambda r: r["ts"])
    return rows[-limit:]


def aggregate_4h(bars: list[dict], *, drop_unclosed: bool = True) -> list[dict]:
    """把 1H K 线聚合为 UTC 对齐的 4H K 线；默认丢弃尚未收盘的当前桶。"""
    buckets: dict[int, dict] = {}
    for bar in bars:
        start = int(bar["ts"]) // BUCKET_SECONDS * BUCKET_SECONDS
        row = buckets.setdefault(start, {
            "ts": start,
            "time": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "open": bar["open"], "high": bar["high"],
            "low": bar["low"], "close": bar["close"], "bars": 0,
        })
        row["high"] = max(row["high"], bar["high"])
        row["low"] = min(row["low"], bar["low"])
        row["close"] = bar["close"]  # bars 按时间升序，最后一次覆盖即末价
        row["bars"] += 1
    candles = [buckets[key] for key in sorted(buckets)]
    if drop_unclosed and candles:
        current = int(time.time()) // BUCKET_SECONDS * BUCKET_SECONDS
        candles = [row for row in candles if row["ts"] < current]
    return candles


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
    if not time_value and ts_value:
        time_value = datetime.fromtimestamp(ts_value, tz=timezone.utc).isoformat()
    return {
        "ts": ts_value,
        "time": time_value or datetime.now(timezone.utc).isoformat(),
        "open": float(open_value) if open_value is not None else middle,
        "high": high, "low": low,
        "close": float(close_value) if close_value is not None else middle,
    }


def parse_tv_scan(payload: dict, *, now_ts: int | None = None) -> dict | None:
    """解析 TradingView scanner 响应中的当前 4H OHLC。

    scanner 的 `time` 字段可能是日线时间，不能用于 4H 对齐；这里按当前 UTC
    4H 桶计算收线时间（03:59 采集即为 04:00 收线）。
    """
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    row = rows[0]
    values = row.get("d") if isinstance(row, dict) else None
    if not isinstance(values, list) or len(values) < 4:
        return None
    open_, high, low, close = (_number(values[i]) for i in range(4))
    if None in (open_, high, low, close):
        return None
    now_value = int(now_ts if now_ts is not None else time.time())
    bucket_start = now_value // BUCKET_SECONDS * BUCKET_SECONDS
    close_ts = bucket_start + BUCKET_SECONDS
    return {
        "ts": close_ts,
        "time": datetime.fromtimestamp(close_ts, tz=timezone.utc).isoformat(),
        "open": float(open_), "high": float(high),
        "low": float(low), "close": float(close),
    }


def _tv_scan(symbol: str) -> dict | None:
    """从 TradingView 公开 scanner 获取当前 4H OHLC。"""
    body = {
        "symbols": {"tickers": [symbol], "query": {"types": []}},
        "columns": ["open|240", "high|240", "low|240", "close|240"],
    }
    try:
        payload = fetch_json(
            TV_CRT_URL, post_json=body,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
        )
    except Exception:  # noqa: BLE001 - 回退 Yahoo
        return None
    return parse_tv_scan(payload)


def _fetch_yahoo_candles(symbol: str) -> list[dict]:
    """Yahoo 1H 行情回退：聚合为 UTC 4H 已收盘 K 线。"""
    yahoo_symbol = "XAUUSD=X" if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD"} else symbol
    url = YHOO_CRT_URL.replace("{symbol}", yahoo_symbol).replace("{interval}", "60m")
    payload = None
    try:
        texts = fetch_yahoo_texts([url], timeout=45)
        if texts and texts[0]:
            payload = json.loads(texts[0])
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        payload = json.loads(fetch_text(url, timeout=45))
    return aggregate_4h(parse_yahoo_intraday(payload))


def _previous_closed_from_db(db, symbol: str) -> dict | None:
    """从 DB 取最近一个已收盘 CRT 快照作为前一根 4H K 线。"""
    try:
        rows = db.crt_snapshots(symbol=symbol, limit=6)
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        high = _number(row.get("current_high"))
        low = _number(row.get("current_low"))
        as_of = str(row.get("as_of") or payload.get("as_of") or "")
        if high is None or low is None or not as_of:
            continue
        ts_value = None
        try:
            dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            ts_value = int(dt.timestamp()) // BUCKET_SECONDS * BUCKET_SECONDS
        except (TypeError, ValueError):
            pass
        candle = _synthesize_candle(
            high, low,
            payload.get("current_open"), payload.get("current_close"),
            as_of, ts_value,
        )
        candle["source"] = row.get("source")
        return candle
    return None


def crt_snapshot(symbol: str, candles: list[dict]) -> dict:
    """由至少 2 根已收盘 4H K 线计算 CRT 高低点、扫盘与延续/反转情景。"""
    if len(candles) < 2:
        raise ValueError(f"{symbol} 4H 已收盘 K 线不足 2 根，无法计算 CRT")
    current, previous = candles[-1], candles[-2]
    cur_high, cur_low = float(current["high"]), float(current["low"])
    prev_high, prev_low = float(previous["high"]), float(previous["low"])
    span = cur_high - cur_low
    body_pct = (
        round(abs(current["close"] - current["open"]) / span * 100.0, 1)
        if span > 0 else 0.0
    )
    if current["close"] > current["open"]:
        direction = "偏多"
    elif current["close"] < current["open"]:
        direction = "偏空"
    else:
        direction = "中性"

    swept_high = cur_high > prev_high
    swept_low = cur_low < prev_low
    strong = body_pct >= 40.0
    if strong and swept_low and direction == "偏多":
        scenario, scenario_bias = "反转", "偏多"
    elif strong and swept_high and direction == "偏空":
        scenario, scenario_bias = "反转", "偏空"
    elif strong and direction != "中性":
        scenario, scenario_bias = "延续", direction
    else:
        scenario, scenario_bias = "待确认", "中性"

    target = prev_high if scenario_bias == "偏多" else prev_low if scenario_bias == "偏空" else None
    stop_ref = prev_low if scenario_bias == "偏多" else prev_high if scenario_bias == "偏空" else None
    swept = "已扫前低（卖出流动性）" if swept_low else (
        "已扫前高（买入流动性）" if swept_high else "未扫前一根高低点"
    )
    summary = (
        f"4H（UTC {current['time'][11:16]}）高 {cur_high:.2f} / 低 {cur_low:.2f}，"
        f"实体 {body_pct:.0f}% {direction}；前一根高 {prev_high:.2f} / 低 {prev_low:.2f}；"
        f"{swept}；情景 {scenario}（{scenario_bias}）。"
    )
    return {
        "source": current.get("source") or SOURCE_NAME,
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "as_of": current["time"],
        "timeframe": TIMEFRAME,
        "current_close": round(float(current.get("close") or cur_high), 2),
        "current_high": round(cur_high, 2),
        "current_low": round(cur_low, 2),
        "prev_high": round(prev_high, 2),
        "prev_low": round(prev_low, 2),
        "equilibrium": round(span / 2.0 + cur_low, 2),
        "body_pct": body_pct,
        "direction": direction,
        "swept_prev_high": swept_high,
        "swept_prev_low": swept_low,
        "scenario": scenario,
        "bias": scenario_bias,
        "target": round(target, 2) if target is not None else None,
        "stop_ref": round(stop_ref, 2) if stop_ref is not None else None,
        "summary": summary,
        "candles": candles[-3:],
        "disclaimer": DISCLAIMER,
    }


def snapshot_from_data(symbol: str, data: dict) -> dict:
    """从手动导入数据构造 CRT 快照：支持 chart 原始响应 / candles / 直接高低点。"""
    if not isinstance(data, dict):
        raise ValueError("CRT 导入数据必须是 JSON 对象")
    if "chart" in data:
        candles = aggregate_4h(parse_yahoo_intraday(data), drop_unclosed=False)
        return crt_snapshot(symbol, candles)
    if "candles" in data:
        candles = aggregate_4h(data["candles"], drop_unclosed=False)
        return crt_snapshot(symbol, candles)
    required = ("current_high", "current_low", "prev_high", "prev_low")
    if not all(key in data for key in required):
        raise ValueError("CRT 快照需要 current_high/current_low/prev_high/prev_low")
    current = _synthesize_candle(
        data["current_high"], data["current_low"],
        data.get("current_open"), data.get("current_close"), data.get("as_of"),
    )
    previous = _synthesize_candle(data["prev_high"], data["prev_low"])
    snap = crt_snapshot(symbol, [previous, current])
    for key in ("scenario", "bias", "as_of", "summary"):
        if data.get(key):
            snap[key] = data[key]
    return snap


def collect(db, symbols: list[str] | tuple[str, ...] | None = None) -> dict:
    """采集 CRT 4H 高低点并写入 crt_snapshots（来源+标的+时间幂等）。"""
    snapshots: list[dict] = []
    errors: list[str] = []
    for symbol in list(symbols or CRT_SYMBOLS):
        try:
            snap = _collect_one(db, symbol)
            raw = json.dumps({"candles": snap.get("candles", [])[-3:]},
                             ensure_ascii=False)
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
    """优先 TradingView，失败回退 Yahoo 聚合。"""
    tv_symbol = TV_CRT_SYMBOL if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD"} else symbol
    tv_bar = _tv_scan(tv_symbol)
    if tv_bar:
        tv_bar["source"] = SOURCE_NAME
        prev = _previous_closed_from_db(db, symbol)
        # 若 DB 前一根与当前同桶或更新，则回退 Yahoo 找前一根
        if prev and int(prev.get("ts") or 0) >= int(tv_bar.get("ts") or 0):
            prev = None
        if prev is None:
            yahoo_candles = _fetch_yahoo_candles(symbol)
            # 取最后两根已收盘的 Yahoo 4H；若 TV bar 是当前桶，前一根即 prev
            if yahoo_candles:
                prev = yahoo_candles[-1]
                if int(prev.get("ts") or 0) >= int(tv_bar.get("ts") or 0) and len(yahoo_candles) >= 2:
                    prev = yahoo_candles[-2]
        first_run = prev is None
        if first_run:
            # 首次使用 TradingView、DB 尚无前一根时：用同根近似填充，避免整个采集失败
            prev = dict(tv_bar)
            prev["source"] = tv_bar["source"]
        prev["source"] = prev.get("source") or FALLBACK_SOURCE
        snap = crt_snapshot(symbol, [prev, tv_bar])
        if first_run:
            snap["summary"] = "首次采集暂无前一根4H，扫盘判断待下次更新；" + snap["summary"]
            snap["history_unavailable"] = True
        return snap

    candles = _fetch_yahoo_candles(symbol)
    if len(candles) < 2:
        raise ValueError(f"{symbol} Yahoo 回退也不足 2 根 4H K 线")
    for c in candles[-2:]:
        c["source"] = FALLBACK_SOURCE
    snap = crt_snapshot(symbol, candles)
    snap["source"] = FALLBACK_SOURCE
    return snap
