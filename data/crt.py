"""4H CRT（Candle Range Theory）模型高低点。

CRT 只关心每根 K 线的高点 / 低点（即流动性所在）：4H 用来判断延续或反转
与关键价位，2M/5M 才用于执行。Yahoo 免费接口没有原生 4h interval，
因此取 60m 行情在 UTC 4 小时桶内本地聚合，只使用已收盘的 4H K 线。

数据来自免费公开行情，结果为近似值，未回测，非交易信号。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import CRT_SYMBOLS, YHOO_CRT_URL
from data.common import fetch_text, fetch_yahoo_texts

SOURCE_NAME = "Yahoo Finance 公开行情（本地聚合 4H CRT）"
SYMBOL_LABELS = {"GC=F": "COMEX黄金期货"}
TIMEFRAME = "4h"
BUCKET_SECONDS = 4 * 3600
DISCLAIMER = (
    "4H CRT 高低点由 Yahoo Finance 公开 1H 行情本地聚合，按 UTC 4 小时对齐；"
    "仅为价格结构研究与流程提醒，未回测，不构成交易信号。"
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
                       time_value=None) -> dict:
    high, low = float(high), float(low)
    if high < low:
        high, low = low, high
    middle = (high + low) / 2.0
    return {
        "ts": 0,
        "time": time_value or datetime.now(timezone.utc).isoformat(),
        "open": float(open_value) if open_value is not None else middle,
        "high": high, "low": low,
        "close": float(close_value) if close_value is not None else middle,
    }


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
        "source": SOURCE_NAME,
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "as_of": current["time"],
        "timeframe": TIMEFRAME,
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


def _fetch_candles(symbol: str) -> list[dict]:
    """先走 Yahoo crumb + cookie，失败时回退无鉴权请求。"""
    url = YHOO_CRT_URL.replace("{symbol}", symbol).replace("{interval}", "60m")
    payload = None
    try:
        texts = fetch_yahoo_texts([url], timeout=45)
        if texts and texts[0]:
            payload = json.loads(texts[0])
    except Exception:  # noqa: BLE001 - 回退无鉴权请求
        payload = None
    if payload is None:
        payload = json.loads(fetch_text(url, timeout=45))
    return aggregate_4h(parse_yahoo_intraday(payload))


def collect(db, symbols: list[str] | tuple[str, ...] | None = None) -> dict:
    """采集 CRT 4H 高低点并写入 crt_snapshots（来源+标的+时间幂等）。"""
    snapshots: list[dict] = []
    errors: list[str] = []
    for symbol in list(symbols or CRT_SYMBOLS):
        try:
            candles = _fetch_candles(symbol)
            snap = crt_snapshot(symbol, candles)
            raw = json.dumps({"candles": candles[-12:]}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 单标的失败不影响其他来源
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
        "ok": True, "source": SOURCE_NAME, "snapshots": len(snapshots),
        "errors": errors or None,
        "latest": {
            "date": latest["as_of"], "symbol": latest["symbol"],
            "high": latest["current_high"], "low": latest["current_low"],
            "scenario": latest["scenario"], "bias": latest["bias"],
        },
    }
