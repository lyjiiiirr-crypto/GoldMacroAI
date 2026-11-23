"""美元指数 DXY 自动采集（免费、无 Key）。

首选 Yahoo Finance 公开行情（ICE 美元指数 DX-Y.NYB）；若被限流/拦截，
自动改用 Frankfurter 提供的欧洲央行参考汇率，按 ICE DXY 官方权重计算近似值。
两者精度差异约 0.1%，报告中会标注实际来源。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import FRANKFURTER_URL, YHOO_DXY_URL
from data.common import fetch_json

DXY_URL = YHOO_DXY_URL
# ICE DXY 货币权重：EUR / JPY / GBP / CAD / SEK / CHF
DXY_WEIGHTS = (0.576, 0.136, 0.119, 0.091, 0.042, 0.026)
DXY_CONSTANT = 50.14348112
DXY_CURRENCIES = ("EUR", "JPY", "GBP", "CAD", "SEK", "CHF")


def parse_yahoo_chart(payload: dict, limit: int = 60) -> list[dict]:
    """解析 Yahoo chart JSON 为 [{date, value}]（按日期降序）。"""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"] or []
        closes = result["indicators"]["quote"][0]["close"] or []
    except (KeyError, IndexError, TypeError):
        return []
    rows = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            day = datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).date().isoformat()
            rows.append({"date": day, "value": round(float(close), 4)})
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def compute_dxy(rates: dict) -> float | None:
    """由 USD 基准汇率计算 DXY 近似值。"""
    try:
        eurusd = 1.0 / float(rates["EUR"])   # Frankfurter 给出 EUR per USD
        usdjpy = float(rates["JPY"])
        gbpusd = 1.0 / float(rates["GBP"])
        usdcad = float(rates["CAD"])
        usdsek = float(rates["SEK"])
        usdchf = float(rates["CHF"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    we, wj, wg, wc, ws, wf = DXY_WEIGHTS
    value = (
        DXY_CONSTANT * eurusd ** (-we) * usdjpy ** wj * gbpusd ** (-wg)
        * usdcad ** wc * usdsek ** ws * usdchf ** wf
    )
    return round(value, 4)


def parse_frankfurter(payload: dict, limit: int = 60) -> list[dict]:
    """解析 ECB 汇率时间序列并换算为 DXY 近似值。"""
    rates_by_day = payload.get("rates") or {}
    rows = []
    for day, rates in sorted(rates_by_day.items()):
        value = compute_dxy(rates)
        if value is not None:
            rows.append({"date": str(day)[:10], "value": value})
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def collect(db) -> dict:
    errors: list[str] = []
    rows: list[dict] = []
    source = ""

    try:
        rows = parse_yahoo_chart(fetch_json(DXY_URL, timeout=40))
        if not rows:
            raise RuntimeError("响应中无有效收盘价")
        source = "Yahoo Finance (DX-Y.NYB)"
    except Exception as exc:  # noqa: BLE001 - 进入 ECB 回退
        errors.append(f"Yahoo: {exc}")

    if not rows:
        start = (datetime.now(timezone.utc).date() - timedelta(days=80)).isoformat()
        url = FRANKFURTER_URL.format(start=start)
        try:
            rows = parse_frankfurter(fetch_json(url, timeout=40))
            if not rows:
                raise RuntimeError("响应中无有效汇率")
            source = "Frankfurter/ECB 汇率计算（DXY 近似）"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Frankfurter: {exc}")

    if not rows:
        raise RuntimeError("DXY 采集失败：" + "；".join(errors[:2]))
    saved = db.upsert_observations("DXY", rows)
    return {
        "ok": True, "source": source, "fetched": len(rows),
        "saved": saved, "latest": rows[0],
    }
