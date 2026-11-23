"""美国宏观数据自动采集：BLS CPI / 核心 CPI / 非农就业（API v1，免 Key）。

BLS 只发布实际值，不发布市场一致预期；因此这里把数据存为时间序列观测值，
不伪造 events 表的"预期值/意外值"。人工导入通道仍可用于带预期的事件。
PCE 与初请失业金数据来自 FRED（BEA / 美国劳工部原始序列），见 config.SERIES。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from config import BLS_API_URL
from data.common import fetch_json

BLS_URL = BLS_API_URL
# BLS 原始序列 -> 本项目派生序列
BLS_SERIES = {
    "CUSR0000SA0": {"key": "CPI_YOY", "label": "CPI同比", "mode": "yoy"},
    "CUSR0000SA0L1E": {"key": "CORE_CPI_YOY", "label": "核心CPI同比", "mode": "yoy"},
    "CES0000000001": {"key": "NFP_CHG", "label": "非农就业变动", "mode": "mom"},
}


def _to_ordinal(year: str, period: str) -> int | None:
    match = re.fullmatch(r"M(\d{1,2})", str(period).strip(), re.IGNORECASE)
    if not match:  # M13 为年度平均，Q* 为季度，均跳过
        return None
    month = int(match.group(1))
    if month < 1 or month > 12:
        return None
    return int(year) * 12 + month


def _ordinal_date(ordinal: int) -> str:
    return f"{ordinal // 12:04d}-{ordinal % 12:02d}-01"


def parse_bls_payload(payload: dict) -> dict[str, list[dict]]:
    """把 BLS v1 响应解析成 {本项目序列ID: [{date, value}]}。"""
    if not isinstance(payload, dict):
        raise RuntimeError("BLS 响应格式异常")
    status = payload.get("status")
    if status != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS 请求失败: {status} / {payload.get('message')}")
    series_list = (payload.get("Results") or {}).get("series") or []
    found: dict[str, dict[int, float]] = {}

    for series in series_list:
        sid = str(series.get("seriesID") or "")
        meta = BLS_SERIES.get(sid)
        if not meta:
            continue
        points: dict[int, float] = {}
        for item in series.get("data") or []:
            ordinal = _to_ordinal(item.get("year", ""), item.get("period", ""))
            if ordinal is None:
                continue
            try:
                points[ordinal] = float(item["value"])
            except (KeyError, TypeError, ValueError):
                continue
        found[meta["key"]] = points

    result: dict[str, list[dict]] = {}
    for sid, meta in BLS_SERIES.items():
        points = found.get(meta["key"], {})
        rows = []
        for ordinal, value in points.items():
            if meta["mode"] == "yoy":
                prior = points.get(ordinal - 12)
                if prior in (None, 0):
                    continue
                derived = (value / prior - 1.0) * 100.0
            else:
                prior = points.get(ordinal - 1)
                if prior is None:
                    continue
                derived = value - prior  # 千人
            rows.append({"date": _ordinal_date(ordinal), "value": round(derived, 4)})
        result[meta["key"]] = sorted(rows, key=lambda r: r["date"], reverse=True)
    return result


def collect(db, years_back: int = 2) -> dict:
    year = datetime.now(timezone.utc).year
    payload = fetch_json(BLS_URL, timeout=45, post_json={
        "seriesid": list(BLS_SERIES),
        "startyear": str(year - years_back),
        "endyear": str(year),
    })
    parsed = parse_bls_payload(payload)
    details = {}
    total = 0
    for key, rows in parsed.items():
        if not rows:
            continue
        saved = db.upsert_observations(key, rows)
        total += saved
        details[key] = {"saved": saved, "latest": rows[0]}
    return {
        "ok": bool(details), "source": "BLS API v1 (免 Key)",
        "series": details, "saved": total,
        "error": None if details else "BLS 未返回可用的 CPI / 非农序列",
    }
