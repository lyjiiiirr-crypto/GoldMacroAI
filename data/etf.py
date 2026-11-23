"""世界黄金协会（WGC）黄金 ETF 持仓与资金流自动采集。

XLSX 下载对匿名请求返回 403，因此使用 WGC Goldhub 页面自身调用的公开
fsapi.gold.org 图表 JSON。数据版权归 World Gold Council，本项目仅做个人监控。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config import WGC_API_BASE
from data.common import fetch_json

WGC_HOLDINGS_URL = WGC_API_BASE.rstrip("/") + "/holdings-chart2"
WGC_FLOWS_URL = WGC_API_BASE.rstrip("/") + "/flows-chart2"
WGC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows",
}
REGIONS = ("North America", "Europe", "Asia", "Other")
SOURCE_NAME = "World Gold Council (公开图表接口)"


def _ms_to_date(value) -> str:
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date().isoformat()


def parse_holdings(payload: dict, limit: int = 60) -> list[dict]:
    """解析 WGC 持仓图表 JSON，按周汇总全球 ETF 持仓吨数。"""
    try:
        weekly = payload["chartData"]["data"]["Weekly"]
    except (KeyError, TypeError):
        return []
    tonnes_table = weekly.get("tonnes") or {}
    usd_table = weekly.get("usd") or {}
    columns = tonnes_table.get("columns") or []
    region_idx = [i for i, name in enumerate(columns) if name in REGIONS]
    price_idx = columns.index("Gold, US$/oz") if "Gold, US$/oz" in columns else None
    if not region_idx:
        return []

    by_date: dict[str, dict] = {}
    for row in tonnes_table.get("set") or []:
        if not row or row[0] is None:
            continue
        day = _ms_to_date(row[0])
        regions = {}
        for i in region_idx:
            if i < len(row) and row[i] is not None:
                regions[columns[i]] = round(float(row[i]), 2)
        price = None
        if price_idx is not None and price_idx < len(row) and row[price_idx] is not None:
            price = float(row[price_idx])
        by_date[day] = {
            "date": day,
            "tonnes": round(sum(regions.values()), 2),
            "regions": regions,
            "gold_price": price,
        }

    usd_columns = usd_table.get("columns") or []
    usd_idx = [i for i, name in enumerate(usd_columns) if name in REGIONS]
    for row in usd_table.get("set") or []:
        if not row or row[0] is None:
            continue
        day = _ms_to_date(row[0])
        if day not in by_date:
            continue
        aum = sum(float(row[i]) for i in usd_idx
                  if i < len(row) and row[i] is not None)
        by_date[day]["aum_usd"] = round(aum, 2)

    return sorted(by_date.values(), key=lambda r: r["date"], reverse=True)[:limit]


def parse_flows(payload: dict) -> dict[str, dict]:
    """解析 WGC 资金流图表 JSON，返回 {日期: {tonnes, usd, by_region}}。"""
    try:
        weekly = payload["chartData"]["data"]["Weekly"]
        series = weekly.get("series") or {}
    except (KeyError, TypeError):
        return {}
    result: dict[str, dict] = {}
    for unit in ("tonnes", "usd"):
        for group in series.get(unit) or []:
            name = str(group.get("name") or "Unknown")
            for point in group.get("data") or []:
                if not point or point[0] is None or point[1] is None:
                    continue
                day = _ms_to_date(point[0])
                record = result.setdefault(
                    day, {"tonnes": 0.0, "usd": 0.0, "by_region": {}}
                )
                value = float(point[1])
                record[unit] += value
                record["by_region"].setdefault(name, {})[unit] = value
    return result


def collect(db, limit: int = 60) -> dict:
    holdings_payload = fetch_json(WGC_HOLDINGS_URL, headers=WGC_HEADERS, timeout=60)
    flows_payload = fetch_json(WGC_FLOWS_URL, headers=WGC_HEADERS, timeout=60)
    holdings = parse_holdings(holdings_payload, limit=limit)
    if not holdings:
        raise RuntimeError("WGC 持仓接口未返回有效周度数据")
    flows = parse_flows(flows_payload)
    as_of = str((holdings_payload.get("chartData") or {}).get("asOfDate") or "")

    for item in holdings:
        flow = flows.get(item["date"], {})
        detail = dict(item)
        detail["weekly_flow_tonnes"] = round(flow.get("tonnes", 0.0), 2)
        detail["weekly_flow_usd"] = round(flow.get("usd", 0.0), 2)
        detail["as_of"] = as_of
        db.upsert_flow("ETF", item["date"], item["tonnes"], scale=50,
                       source=SOURCE_NAME, raw_json=json.dumps(detail, ensure_ascii=False))

    latest = holdings[0]
    flow = flows.get(latest["date"], {})
    return {
        "ok": True, "source": SOURCE_NAME, "as_of": as_of,
        "weeks": len(holdings),
        "latest": {
            "date": latest["date"], "tonnes": latest["tonnes"],
            "weekly_flow_tonnes": round(flow.get("tonnes", 0.0), 2),
            "weekly_flow_usd": round(flow.get("usd", 0.0), 0),
        },
    }
