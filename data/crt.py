"""4H CRT（Candle Range Theory）三根 K 线模型。

HTF 三根结构（ICT 单根 K 线版 Power of Three）：
  第 1 根：设定区间（range_high / range_low，仅作历史背景）；
  第 2 根：扫荡第 1 根一侧流动性并收回，形成当前区间（p2_high / p2_low）；
  第 3 根：在报告发出时已收线，因此当前有效信号看 P3 是否扫过 P2 高低点、
    以及 P3 收盘是否收回 P2 区间。
描述已收线结构，不预测下一根；未回测，非交易信号。

已收盘 4H 优先尝试 TradingView 公开 scanner 的 `[1]/[2]/[3]` 偏移列
（OANDA:XAUUSD，4H 收线为北京时间 10/14/18/22/02/06，即 UTC 02/06/10/14/18/22）；
该接口通常只返回未完成根，因此稳定来源为 Yahoo 1H 本地聚合，TradingView 仅作现价参考。
数据为免费公开近似值，未回测，非交易信号。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import CRT_SYMBOLS, TV_CRT_SYMBOL, TV_CRT_URL, YHOO_CRT_URL
from data.common import fetch_json, fetch_text, fetch_yahoo_texts

SOURCE_NAME = "TradingView 公开行情（4H CRT）"
FALLBACK_SOURCE = "Yahoo Finance 公开行情（本地聚合 4H CRT）"
SYMBOL_LABELS = {
    "GC=F": "COMEX黄金期货",
    "XAUUSD": "现货黄金 XAUUSD",
    "OANDA:XAUUSD": "现货黄金 XAUUSD",
}
TIMEFRAME = "4h"
BUCKET_SECONDS = 4 * 3600
# TradingView OANDA:XAUUSD 4H 收线：UTC 02/06/10/14/18/22（北京时间 10/14/18/22/02/06）
CLOSE_OFFSET_HOURS = 2
OFFSET_SECONDS = CLOSE_OFFSET_HOURS * 3600
DISCLAIMER = (
    "4H CRT 三根结构优先取自 TradingView 公开行情（OANDA:XAUUSD 4H），"
    "失败时回退 Yahoo Finance 1H 本地聚合；为价格结构研究与流程提醒，"
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


def _bucket_start(ts: int | float | None = None) -> int:
    """返回所处 4H K 线（收线时刻对齐 UTC 02/06/...）的起点。"""
    moment = int(ts if ts is not None else time.time())
    return ((moment - OFFSET_SECONDS) // BUCKET_SECONDS) * BUCKET_SECONDS + OFFSET_SECONDS


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


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
    """把 1H K 线聚合为收线对齐 UTC 02/06/... 的 4H K 线。"""
    buckets: dict[int, dict] = {}
    for bar in bars:
        start = _bucket_start(bar["ts"])
        row = buckets.setdefault(start + BUCKET_SECONDS, {
            "ts": start + BUCKET_SECONDS,
            "time": _iso(start + BUCKET_SECONDS),
            "open": bar["open"], "high": bar["high"],
            "low": bar["low"], "close": bar["close"], "bars": 0,
        })
        row["high"] = max(row["high"], bar["high"])
        row["low"] = min(row["low"], bar["low"])
        row["close"] = bar["close"]  # bars 按时间升序，最后一次覆盖即末价
        row["bars"] += 1
    candles = [buckets[key] for key in sorted(buckets)]
    if drop_unclosed and candles:
        # 当前所在桶尚未收线，其预计收线时刻必须排除；只保留已收盘 K 线
        current_bucket_close = _bucket_start(time.time()) + BUCKET_SECONDS
        candles = [row for row in candles if row["ts"] < current_bucket_close]
    return candles


def _candle(open_, high, low, close, close_ts: int, source: str | None = None) -> dict:
    return {
        "ts": int(close_ts), "time": _iso(int(close_ts)),
        "open": float(open_), "high": float(high),
        "low": float(low), "close": float(close),
        **({"source": source} if source else {}),
    }


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
        time_value = _iso(ts_value)
    return {
        "ts": ts_value,
        "time": time_value or datetime.now(timezone.utc).isoformat(),
        "open": float(open_value) if open_value is not None else middle,
        "high": high, "low": low,
        "close": float(close_value) if close_value is not None else middle,
    }


def parse_tv_candles(payload: dict, *, now_ts: int | None = None) -> list[dict]:
    """解析 scanner 的当前根 + `[1]`/`[2]`/`[3]` 偏移列，取最近 3 根已收盘 4H。

    `[1]` 最后一根已收盘，`[2]` 前一根，`[3]` 再前一根。收线时刻按
    TradingView 4H 对齐（UTC 02/06/...）计算，不依赖 scanner 的 `time` 字段。
    """
    rows = (payload or {}).get("data") or []
    if not rows:
        return []
    values = rows[0].get("d") if isinstance(rows[0], dict) else None
    if not isinstance(values, list) or len(values) < 12:
        return []
    # 偏移顺序：无偏移（当前）、[1]、[2]、[3]，每组 4 个 OHLC
    last_closed = _bucket_start(now_ts)
    candles: list[dict] = []
    for k in (1, 2, 3):
        base = k * 4
        open_, high, low, close = (_number(values[i]) for i in range(base, base + 4))
        if None in (open_, high, low, close):
            continue
        close_ts = last_closed - (k - 1) * BUCKET_SECONDS
        candles.append(_candle(open_, high, low, close, close_ts, SOURCE_NAME))
    candles.reverse()  # 按时间升序：第 1 根 → 第 2 根 → 第 3 根
    return candles


def parse_tv_scan(payload: dict, *, now_ts: int | None = None) -> dict | None:
    """解析 scanner 当前（未完成）4H OHLC；仅作 DB 历史不足时的兜底。"""
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
                   _bucket_start(now_ts) + BUCKET_SECONDS, SOURCE_NAME)


def _tv_scan(symbol: str) -> list[dict]:
    """从 TradingView 公开 scanner 获取最近 3 根已收盘 4H K 线。"""
    columns: list[str] = []
    for suffix in ("", "[1]", "[2]", "[3]"):
        columns += [f"open{suffix}|240", f"high{suffix}|240",
                    f"low{suffix}|240", f"close{suffix}|240"]
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
    if len(candles) >= 3:
        return candles
    # 偏移列不可用：退而求其次，只取当前根（由调用方用 DB/Yahoo 补历史）
    current = parse_tv_scan(payload)
    return [current] if current else []


def _fetch_yahoo_candles(symbol: str) -> list[dict]:
    """Yahoo 1H 行情回退：聚合为已收盘 4H K 线。

    现货 `XAUUSD=X` 可能没有分钟级数据，因此同时尝试 `GC=F`（COMEX 黄金期货
    代理）与 query1/query2 两个入口；任一返回足够 K 线即采用。
    """
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
        candles = aggregate_4h(parse_yahoo_intraday(payload))
        if len(candles) >= 3:
            return candles
    return []


def _history_from_db(db, symbol: str, limit: int = 3) -> list[dict]:
    """从 DB 快照的 payload.candles 取最近的已收盘 K 线（供 TV 单根兜底）。"""
    try:
        rows = db.crt_snapshots(symbol=symbol, limit=8)
    except Exception:  # noqa: BLE001
        return []
    seen: set[int] = set()
    candles: list[dict] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        for candle in reversed(payload.get("candles") or []):
            ts_value = _number(candle.get("ts"))
            if ts_value is None or int(ts_value) in seen:
                continue
            high, low = _number(candle.get("high")), _number(candle.get("low"))
            if high is None or low is None:
                continue
            seen.add(int(ts_value))
            candles.append(_candle(
                candle.get("open") or (high + low) / 2.0, high, low,
                candle.get("close") or high, int(ts_value),
                candle.get("source") or row.get("source"),
            ))
        if len(candles) >= limit:
            break
    candles.sort(key=lambda c: c["ts"])
    return candles[-limit:]


def p3_vs_p2(candles: list[dict]) -> dict:
    """P3 已收线时，以 P2 为当前区间判断扫荡与收盘收回。

    报告在 P3 收线后才发出，P1 区间已是历史背景；下一根的有效结构依据
    是「P3 有没有扫过 P2 高低点」和「P3 收盘有没有收回 P2 区间」。
    """
    if len(candles) < 3:
        raise ValueError("需要至少 3 根已收盘 4H K 线")
    second, third = candles[-2], candles[-1]
    p2_high, p2_low = float(second["high"]), float(second["low"])
    p3_high, p3_low = float(third["high"]), float(third["low"])
    p3_close = float(third.get("close") if third.get("close") is not None else third["low"])

    swept_high = p3_high > p2_high
    swept_low = p3_low < p2_low
    reclaimed = p2_low <= p3_close <= p2_high
    # 正数表示 P3 高点仍在 P2 高点之内 / P3 低点仍在 P2 低点之上；
    # 负数表示已扫过对应边界。
    dist_high = round(p3_high - p2_high, 2)
    dist_low = round(p3_low - p2_low, 2)

    if swept_low:
        sweep_side = "低"
        if reclaimed:
            scenario = "P3 扫 P2 低点后收回区间 · 偏多反转确认"
            bias = "偏多"
            target, stop_ref = p2_high, p3_low
        else:
            scenario = "P3 扫 P2 低点后未收回 · 偏空延续"
            bias = "偏空"
            target, stop_ref = None, p2_low
    elif swept_high:
        sweep_side = "高"
        if reclaimed:
            scenario = "P3 扫 P2 高点后收回区间 · 偏空反转确认"
            bias = "偏空"
            target, stop_ref = p2_low, p3_high
        else:
            scenario = "P3 扫 P2 高点后未收回 · 偏多延续"
            bias = "偏多"
            target, stop_ref = None, p2_high
    else:
        sweep_side = None
        scenario = "P3 未扫 P2 高低点 · 区间内待确认"
        bias = "中性"
        target, stop_ref = None, None

    return {
        "p2_high": round(p2_high, 2),
        "p2_low": round(p2_low, 2),
        "p3_high": round(p3_high, 2),
        "p3_low": round(p3_low, 2),
        "p3_close": round(p3_close, 2),
        "p3_sweep_side": sweep_side,
        "p3_swept_p2_high": swept_high,
        "p3_swept_p2_low": swept_low,
        "p3_reclaimed_p2_range": reclaimed,
        "p3_reclaim_text": "收盘收回 P2 区间" if reclaimed else "收盘未收回 P2 区间",
        "dist_p3_high_to_p2_high": dist_high,
        "dist_p3_low_to_p2_low": dist_low,
        "p3_scenario": scenario,
        "p3_bias": bias,
        "p3_target": round(target, 2) if target is not None else None,
        "p3_stop_ref": round(stop_ref, 2) if stop_ref is not None else None,
    }


def crt_snapshot(symbol: str, candles: list[dict]) -> dict:
    """由至少 3 根已收盘 4H K 线计算三根 CRT 结构与执行参考位。"""
    if len(candles) < 3:
        raise ValueError(f"{symbol} 4H 已收盘 K 线不足 3 根，无法计算三根 CRT")
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    range_high, range_low = float(c1["high"]), float(c1["low"])
    sweep_high, sweep_low = float(c2["high"]), float(c2["low"])
    cur_high, cur_low = float(c3["high"]), float(c3["low"])
    c2_open, c2_close = float(c2.get("open") or c2["low"]), float(c2.get("close") or c2["low"])
    c3_open, c3_close = float(c3.get("open") or c3["low"]), float(c3.get("close") or c3["low"])
    equilibrium = (range_high + range_low) / 2.0
    body_pct = (round(abs(c3_close - c3_open) / (cur_high - cur_low) * 100.0, 1)
                if cur_high > cur_low else 0.0)

    # P2 相对 P1 的扫荡仅作历史背景；报告发出时 P3 已收线，
    # 当前情景/方向/参考位一律由 P3 相对 P2 决定。
    swept_high = sweep_high > range_high and c2_close <= range_high
    swept_low = sweep_low < range_low and c2_close >= range_low
    sweep_side = "低" if swept_low else "高" if swept_high else None
    p3 = p3_vs_p2(candles)
    scenario, bias = p3["p3_scenario"], p3["p3_bias"]
    target, stop_ref = p3["p3_target"], p3["p3_stop_ref"]

    # 旧字段保留兼容（距离为相对 P1 区间，不再用于执行提醒）
    dist_high_to_resistance = round(range_high - cur_high, 2)
    dist_low_to_support = round(cur_low - range_low, 2)
    dist_sweep_low_to_support = round(sweep_low - range_low, 2)
    dist_sweep_high_to_resistance = round(range_high - sweep_high, 2)

    if p3["p3_sweep_side"]:
        sweep_desc = (
            f"P3 扫 P2 {p3['p3_sweep_side']}点（高 {p3['p3_high']:.2f} / "
            f"低 {p3['p3_low']:.2f}），收盘 {p3['p3_close']:.2f} "
            f"{'已收回' if p3['p3_reclaimed_p2_range'] else '未收回'} P2 区间"
            f"（{p3['p2_low']:.2f}~{p3['p2_high']:.2f}）"
        )
    else:
        sweep_desc = (
            f"P3 未扫 P2 高低点（高 {p3['p3_high']:.2f} / 低 {p3['p3_low']:.2f} / "
            f"收 {p3['p3_close']:.2f}）"
        )
    summary = (
        f"P1 背景区间 高 {range_high:.2f} / 低 {range_low:.2f}；"
        f"{sweep_desc}；情景 {scenario}（{bias}）。"
    )
    return {
        "source": c3.get("source") or SOURCE_NAME,
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "as_of": c3["time"],
        "timeframe": TIMEFRAME,
        "candle1": dict(c1), "candle2": dict(c2), "candle3": dict(c3),
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "sweep_high": round(sweep_high, 2),
        "sweep_low": round(sweep_low, 2),
        "current_high": round(cur_high, 2),
        "current_low": round(cur_low, 2),
        "current_close": round(c3_close, 2),
        "prev_high": round(range_high, 2),  # DB 兼容：第 1 根区间高 = 阻力
        "prev_low": round(range_low, 2),    # DB 兼容：第 1 根区间低 = 支撑
        "equilibrium": round(equilibrium, 2),
        "body_pct": body_pct,
        "sweep_side": sweep_side,
        "swept_prev_high": swept_high,
        "swept_prev_low": swept_low,
        **p3,
        "dist_high_to_resistance": dist_high_to_resistance,
        "dist_low_to_support": dist_low_to_support,
        "dist_sweep_low_to_support": dist_sweep_low_to_support,
        "dist_sweep_high_to_resistance": dist_sweep_high_to_resistance,
        "scenario": scenario,
        "bias": bias,
        "target": round(target, 2) if target is not None else None,
        "stop_ref": round(stop_ref, 2) if stop_ref is not None else None,
        "summary": summary,
        "candles": [dict(c1), dict(c2), dict(c3)],
        "disclaimer": DISCLAIMER,
    }


def snapshot_from_data(symbol: str, data: dict) -> dict:
    """手动导入：支持 chart 原始响应 / candles / 三根 OHLC / 旧版高低点。"""
    if not isinstance(data, dict):
        raise ValueError("CRT 导入数据必须是 JSON 对象")
    if "chart" in data:
        return crt_snapshot(symbol, aggregate_4h(parse_yahoo_intraday(data), drop_unclosed=False))
    if "candles" in data:
        return crt_snapshot(symbol, aggregate_4h(data["candles"], drop_unclosed=False))
    if all(isinstance(data.get(key), dict) for key in ("candle1", "candle2", "candle3")):
        candles = []
        for key in ("candle1", "candle2", "candle3"):
            item = data[key]
            candles.append(_synthesize_candle(
                item.get("high"), item.get("low"), item.get("open"), item.get("close"),
                item.get("time"), item.get("ts"),
            ))
        snap = crt_snapshot(symbol, candles)
        if data.get("as_of"):
            snap["as_of"] = str(data["as_of"])
        return snap
    required = ("range_high", "range_low", "sweep_high", "sweep_low",
                "current_high", "current_low")
    if all(key in data for key in required):
        c1 = _synthesize_candle(
            data["range_high"], data["range_low"], data.get("range_open"),
            data.get("range_close"), data.get("range_time"),
        )
        c2 = _synthesize_candle(
            data["sweep_high"], data["sweep_low"], data.get("sweep_open"),
            data.get("sweep_close"), data.get("sweep_time"),
        )
        c3 = _synthesize_candle(
            data["current_high"], data["current_low"], data.get("current_open"),
            data.get("current_close"), data.get("as_of"),
        )
        return crt_snapshot(symbol, [c1, c2, c3])
    # 旧版两根快照兼容：用相同区间补齐第 1 根，避免人工导入失效
    old_required = ("current_high", "current_low", "prev_high", "prev_low")
    if all(key in data for key in old_required):
        shared = _synthesize_candle(data["prev_high"], data["prev_low"])
        current = _synthesize_candle(
            data["current_high"], data["current_low"],
            data.get("current_open"), data.get("current_close"), data.get("as_of"),
        )
        snap = crt_snapshot(symbol, [dict(shared), dict(shared), current])
        snap["legacy_import"] = True
        for key in ("scenario", "bias", "summary"):
            if data.get(key):
                snap[key] = data[key]
        return snap
    raise ValueError("CRT 快照需要 candle1/candle2/candle3 或区间/扫荡/扩张高低点")


def collect(db, symbols: list[str] | tuple[str, ...] | None = None) -> dict:
    """采集三根 CRT 并写入 crt_snapshots（来源+标的+时间幂等）。"""
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
    """取最近 3 根已收盘 4H K 线。

    TradingView scanner 若返回 `[1]/[2]/[3]` 偏移列则直接计算；实测该接口通常
    只返回当前未完成根，因此已收盘 K 线以 Yahoo 1H 本地聚合为准，TradingView
    仅作为现价参考。两者都失败时退回本地历史快照。
    """
    tv_symbol = TV_CRT_SYMBOL if symbol.upper() in {"XAUUSD", "OANDA:XAUUSD"} else symbol
    tv_candles = _tv_scan(tv_symbol)

    # 1) TradingView 偏移列可用：直接得到 3 根已收盘 K 线
    if len(tv_candles) >= 3:
        snap = crt_snapshot(symbol, tv_candles[-3:])
        snap["source"] = SOURCE_NAME
        return snap

    # 2) Yahoo 1H 聚合：稳定提供按 UTC 02/06/... 收线的已收盘 4H K 线
    yahoo = _fetch_yahoo_candles(symbol)
    if len(yahoo) >= 3:
        candles = yahoo[-3:]
        for candle in candles:
            candle["source"] = FALLBACK_SOURCE
        snap = crt_snapshot(symbol, candles)
        snap["source"] = FALLBACK_SOURCE
        if tv_candles:
            snap["tv_price"] = tv_candles[-1].get("close")
        return snap

    # 3) 本地历史：最近 3 根已收盘快照（首次运行可能不足，优雅降级）
    history = _history_from_db(db, symbol, limit=3)
    if len(history) >= 3:
        snap = crt_snapshot(symbol, history[-3:])
        snap["source"] = "本地历史快照"
        if tv_candles:
            snap["tv_price"] = tv_candles[-1].get("close")
        return snap

    raise ValueError(f"{symbol} 无法获得 3 根已收盘 4H K 线（TradingView/Yahoo/本地历史均不可用）")
