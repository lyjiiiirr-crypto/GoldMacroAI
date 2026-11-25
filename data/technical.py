"""技术面报告：XAUS 公开实时行情 + 本地规则计算。

黄金优先使用 XAUS 免费公开实时行情的已定稿日线（免密钥、带 data_state
新鲜度校验），失败时回退 Yahoo Finance。数据与指标均来自公开行情和本项目
自算逻辑，**不是 Trading Central 官方报告**，也不转载任何授权投研内容。
报告仅描述价格结构，不构成交易建议。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config import TECHNICAL_SYMBOLS, YHOO_TECH_URL
from data.common import fetch_json
from data.xaus import (
    DISCLAIMER as XAUS_DISCLAIMER,
    SYMBOL_LABELS as XAUS_SYMBOL_LABELS,
    fetch_history, history_as_of, parse_history,
)

SOURCE_NAME = "Yahoo Finance 公开行情 + 本地规则计算"
XAUS_SOURCE = "XAUS 公开实时行情 + 本地规则计算"
GOLD_SYMBOLS = {"GC=F", "XAUUSD", "XAUUSD=X", "OANDA:XAUUSD", "xau"}
SYMBOL_LABELS = {
    "GC=F": "COMEX黄金期货",
    "DX-Y.NYB": "美元指数DXY",
    "CL=F": "WTI原油",
}
REPORT_TYPE = "daily"
MIN_BARS = 30
DISCLAIMER = "本地自算技术面，数据来自 Yahoo Finance 公开行情；非 Trading Central 官方研报，不构成投资建议。"


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def parse_yahoo_ohlc(payload: dict, limit: int = 400) -> list[dict]:
    """解析 Yahoo chart JSON 为按日期升序排列的日线 OHLC。"""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    columns = {
        key: quote.get(key) or []
        for key in ("open", "high", "low", "close", "volume")
    }
    rows: list[dict] = []
    for i, ts in enumerate(timestamps):
        close = _number(columns["close"][i]) if i < len(columns["close"]) else None
        if close is None:
            continue
        try:
            # Yahoo 不同标的/接口的 timestamp 单位可能是秒或毫秒，按量级自动识别
            value = float(ts)
            if value > 1e11:
                value /= 1000.0
            day = datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        open_ = _number(columns["open"][i]) if i < len(columns["open"]) else None
        high = _number(columns["high"][i]) if i < len(columns["high"]) else None
        low = _number(columns["low"][i]) if i < len(columns["low"]) else None
        volume = _number(columns["volume"][i]) if i < len(columns["volume"]) else None
        rows.append({
            "date": day,
            "open": open_ if open_ is not None else close,
            "high": max(high or close, close),
            "low": min(low or close, close),
            "close": close,
            "volume": volume,
        })
    rows.sort(key=lambda r: r["date"])
    return rows[-limit:]


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 4)


def ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(float(value) * alpha + out[-1] * (1.0 - alpha))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI；样本不足返回 None。"""
    if len(closes) <= period:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2)


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    fast_line = ema_series(closes, fast)
    slow_line = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_line, slow_line)]
    signal_line = ema_series(macd_line, signal)
    return {
        "macd": round(macd_line[-1], 4),
        "signal": round(signal_line[-1], 4),
        "histogram": round(macd_line[-1] - signal_line[-1], 4),
    }


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Wilder ATR；样本不足返回 None。"""
    if len(candles) <= period:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        current, previous = candles[i], candles[i - 1]
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return round(value, 4)


def _cluster_levels(values: list[float], tolerance_pct: float = 0.35) -> list[float]:
    """把相近价格聚类，避免支撑/阻力出现一堆重复价位。"""
    groups: list[list[float]] = []
    for value in sorted(v for v in values if v is not None):
        if groups and abs(value - groups[-1][-1]) / max(abs(groups[-1][-1]), 1e-9) * 100 <= tolerance_pct:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [round(sum(group) / len(group), 2) for group in groups]


def support_resistance(candles: list[dict], close: float,
                       lookback: int = 60) -> dict:
    recent = candles[-lookback:]
    supports = _cluster_levels([c["low"] for c in recent if c["low"] < close * 0.999])
    resistances = _cluster_levels([c["high"] for c in recent if c["high"] > close * 1.001])
    return {
        "support": sorted((v for v in supports if v < close), reverse=True)[:2],
        "resistance": sorted(v for v in resistances if v > close)[:2],
    }


def build_report(symbol: str, candles: list[dict], *, label: str | None = None,
                 report_type: str = REPORT_TYPE, source: str | None = None,
                 disclaimer: str | None = None) -> dict:
    """由日线序列生成结构化技术面报告。"""
    if len(candles) < MIN_BARS:
        raise ValueError(f"{symbol} 有效日线不足 {MIN_BARS} 根")
    closes = [c["close"] for c in candles]
    close = closes[-1]
    last = candles[-1]

    indicators: dict = {
        "sma20": sma(closes, 20),
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "rsi14": rsi(closes),
        "atr14": atr(candles),
    }
    macd_info = macd(closes)
    if macd_info:
        indicators.update(macd_info)

    score = 0
    signals: list[str] = []
    sma20, sma50, sma200 = indicators["sma20"], indicators["sma50"], indicators["sma200"]
    if sma20 is not None:
        if close > sma20:
            score += 1
            signals.append(f"收盘 {close:.2f} 高于 SMA20 {sma20:.2f}")
        else:
            score -= 1
            signals.append(f"收盘 {close:.2f} 低于 SMA20 {sma20:.2f}")
    if sma50 is not None:
        if close > sma50:
            score += 1
            signals.append(f"价格位于 SMA50 {sma50:.2f} 上方")
        else:
            score -= 1
            signals.append(f"价格跌破 SMA50 {sma50:.2f}")
    if sma200 is not None:
        if close > sma200:
            score += 1
            signals.append("长期趋势：价格高于 SMA200")
        else:
            score -= 1
            signals.append("长期趋势：价格低于 SMA200")
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 1
            signals.append("SMA20 高于 SMA50（多头排列）")
        else:
            score -= 1
            signals.append("SMA20 低于 SMA50（空头排列）")
    rsi_value = indicators["rsi14"]
    if rsi_value is not None:
        if rsi_value >= 70:
            score -= 1
            signals.append(f"RSI14 {rsi_value:.1f} 进入超买区")
        elif rsi_value <= 30:
            score += 1
            signals.append(f"RSI14 {rsi_value:.1f} 进入超卖区")
        else:
            signals.append(f"RSI14 {rsi_value:.1f} 处于中性区间")
    if macd_info:
        if macd_info["histogram"] > 0:
            score += 1
            signals.append("MACD 柱状图为正（动能偏强）")
        else:
            score -= 1
            signals.append("MACD 柱状图为负（动能偏弱）")

    bias = "偏多" if score >= 2 else "偏空" if score <= -2 else "中性震荡"
    levels = support_resistance(candles, close)
    levels["pivot"] = round((last["high"] + last["low"] + close) / 3.0, 2)
    atr_value = indicators["atr14"]
    if atr_value is not None and bias != "中性震荡":
        levels["invalidation"] = round(
            close - atr_value if bias == "偏多" else close + atr_value, 2
        )
    support_text = " / ".join(f"{v:.2f}" for v in levels["support"]) or "暂无"
    resistance_text = " / ".join(f"{v:.2f}" for v in levels["resistance"]) or "暂无"
    summary = (
        f"日线{bias}：收盘 {close:.2f}，"
        f"RSI14 {rsi_value:.1f}" if rsi_value is not None else
        f"日线{bias}：收盘 {close:.2f}，RSI 样本不足"
    )
    summary += (
        f"；MACD 柱 {macd_info['histogram']:+.2f}" if macd_info else "；MACD 样本不足"
    )
    summary += f"；支撑 {support_text}，阻力 {resistance_text}。"

    return {
        "source": source or SOURCE_NAME,
        "symbol": symbol,
        "label": label or SYMBOL_LABELS.get(symbol, XAUS_SYMBOL_LABELS.get(symbol, symbol)),
        "report_type": report_type,
        "as_of": last["date"],
        "close": round(close, 2),
        "bias": bias,
        "score": score,
        "summary": summary,
        "signals": signals,
        "indicators": indicators,
        "levels": levels,
        "disclaimer": disclaimer or DISCLAIMER,
    }


def collect(db, symbols: list[str] | tuple[str, ...] | None = None) -> dict:
    """采集配置中的标的并写入 technical_reports（按来源+标的+日期+类型幂等）。

    黄金优先使用 XAUS 实时公开行情的已定稿日线；失败时回退 Yahoo Finance。
    只有真正收盘的日线才参与指标计算，避免把盘中快照当成收盘价。
    """
    targets = list(symbols or TECHNICAL_SYMBOLS)
    reports: list[dict] = []
    errors: list[str] = []
    for symbol in targets:
        report: dict | None = None
        raw = ""
        if symbol.upper() in GOLD_SYMBOLS:
            try:
                payload = fetch_history(days=400)
                candles = parse_history(payload)
                if len(candles) < MIN_BARS:
                    raise RuntimeError(f"XAUS 有效日线仅 {len(candles)} 根")
                report = build_report(
                    symbol, candles,
                    label=XAUS_SYMBOL_LABELS.get(symbol, "现货黄金 XAUUSD"),
                    source=XAUS_SOURCE, disclaimer=XAUS_DISCLAIMER,
                )
                raw = json.dumps({
                    "source": XAUS_SOURCE,
                    "as_of": history_as_of(payload),
                    "candles": candles[-400:],
                }, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 - 回退 Yahoo
                errors.append(f"{symbol}: XAUS: {type(exc).__name__}: {exc}")

        if report is None:
            try:
                payload = fetch_json(
                    YHOO_TECH_URL.format(symbol=symbol),
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=40,
                )
                candles = parse_yahoo_ohlc(payload)
                if len(candles) < MIN_BARS:
                    raise RuntimeError(f"有效日线仅 {len(candles)} 根")
                report = build_report(symbol, candles)
                result = (payload.get("chart") or {}).get("result") or [{}]
                raw = json.dumps({
                    "meta": result[0].get("meta", {}),
                    "candles": candles[-400:],
                }, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 - 单标的失败不影响其他标的
                errors.append(f"{symbol}: Yahoo: {type(exc).__name__}: {exc}")
                continue

        db.upsert_technical_report(
            source=report["source"], symbol=symbol, date=report["as_of"],
            report_type=report["report_type"],
            title=f"{report['label']} 技术面日报",
            bias=report["bias"], summary=report["summary"],
            payload=report, raw_json=raw,
        )
        reports.append(report)

    if not reports:
        return {
            "ok": False, "source": SOURCE_NAME, "reports": 0,
            "error": "技术面报告采集失败：" + "；".join(errors[:2]),
        }
    latest = reports[0]
    return {
        "ok": True, "source": latest["source"], "reports": len(reports),
        "symbols": ",".join(r["symbol"] for r in reports),
        "errors": errors or None,
        "latest": {
            "date": latest["as_of"], "symbol": latest["symbol"],
            "bias": latest["bias"], "close": latest["close"],
        },
    }
