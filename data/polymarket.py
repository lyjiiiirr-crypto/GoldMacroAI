"""Polymarket 利率预测（公开 Gamma API，无需密钥）。

只读取市场元数据与当前 Yes 赔率，作为「美联储政策预期」的报告上下文；
属于预测市场定价，非官方利率数据，不参与宏观评分。失败时优雅降级，
并保留人工导入（--import-polymarket）入口。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import quote

from config import POLYMARKET_GAMMA_URL
from data.common import fetch_json

SOURCE_NAME = "Polymarket 公开预测市场（Gamma API）"
DISCLAIMER = (
    "Polymarket 赔率为公开预测市场定价，非官方利率数据；仅作报告上下文，"
    "未回测，不参与宏观评分，不构成交易信号。"
)
RATE_KEYWORDS = (
    "fed", "fomc", "interest rate", "rate cut", "rate hike", "rate decision",
    "federal funds", "target range", "basis point", "利率", "降息", "加息",
)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def extract_markets(payload) -> list[dict]:
    """从 Gamma 搜索/列表响应中抽出市场对象列表。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("markets", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    markets: list[dict] = []
    for event in payload.get("events") or []:
        if isinstance(event, dict):
            markets.extend(
                item for item in (event.get("markets") or []) if isinstance(item, dict)
            )
    if markets:
        return markets
    if payload.get("question") or payload.get("slug"):
        return [payload]
    return []


def fetch_rate_markets() -> list[dict]:
    """公开接口获取市场：优先在营市场，搜索结果作为补充。"""
    urls = (
        f"{POLYMARKET_GAMMA_URL}/markets?active=true&closed=false"
        f"&limit=100&order=volume24hr&ascending=false",
        f"{POLYMARKET_GAMMA_URL}/public-search"
        f"?q={quote('Fed interest rate')}&limit_per_type=15",
    )
    combined: list[dict] = []
    seen: set[str] = set()
    for url in urls:
        try:
            payload = fetch_json(
                url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
            )
        except Exception:  # noqa: BLE001 - 逐个接口兜底
            continue
        for market in extract_markets(payload):
            key = str(market.get("question") or market.get("slug") or id(market))
            if key in seen:
                continue
            seen.add(key)
            combined.append(market)
    return combined


def _market_is_open(market: dict) -> bool:
    """过滤已结算 / 已过期市场（搜索接口会返回大量历史市场）。"""
    if market.get("closed") is True or market.get("active") is False:
        return False
    end_date = str(market.get("endDate") or "")[:10]
    if end_date and end_date < date.today().isoformat():
        return False
    return True


def parse_rate_markets(markets: list[dict], *, limit: int = 3) -> list[dict]:
    """筛出利率相关市场并提取 Yes 概率、成交额与链接。"""
    items: list[dict] = []
    for market in markets:
        if not _market_is_open(market):
            continue
        question = str(market.get("question") or market.get("title") or "").strip()
        slug = str(market.get("slug") or "")
        haystack = f"{question} {slug}".lower()
        if not any(keyword in haystack for keyword in RATE_KEYWORDS):
            continue
        outcomes = _as_list(market.get("outcomes"))
        prices = _as_list(market.get("outcomePrices"))
        if not prices and market.get("lastTradePrice") is not None:
            prices = [market["lastTradePrice"]]
        if not prices:
            continue
        # 按 outcomes 中 Yes 的位置取价，避免结果顺序假设
        selected = 0
        for i, name in enumerate(outcomes):
            if str(name).strip().lower() in {"yes", "是"} and i < len(prices):
                selected = i
                break
        try:
            probability = float(prices[selected])
        except (TypeError, ValueError, IndexError):
            continue
        if probability > 1:  # 以百分比录入的历史数据
            probability /= 100.0
        if not 0.0 <= probability <= 1.0:
            continue
        label = str(outcomes[selected]) if selected < len(outcomes) else "Yes"
        volume = (market.get("volume24hr") or market.get("volumeNum")
                  or market.get("volume") or 0)
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 0.0
        url = ""
        if market.get("eventSlug"):
            url = f"https://polymarket.com/event/{market['eventSlug']}"
        elif slug:
            url = f"https://polymarket.com/market/{slug}"
        items.append({
            "question": question or slug,
            "label": label,
            "yes_probability": round(probability * 100.0, 1),
            "volume": round(volume, 2),
            "end_date": str(market.get("endDate") or "")[:10],
            "url": url,
        })
    items.sort(key=lambda item: item["volume"], reverse=True)
    return items[:limit]


def build_snapshot(markets: list[dict]) -> dict:
    """由原始市场列表构造可入库、可渲染的快照。"""
    items = parse_rate_markets(markets)
    if not items:
        raise ValueError("Polymarket 响应中没有可用的利率市场")
    top = items[0]
    text = f"{top['question']}：{top['label']} {top['yes_probability']:.1f}%"
    return {
        "source": SOURCE_NAME,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "markets": items,
        "text": text,
        "disclaimer": DISCLAIMER,
    }


def snapshot_from_data(data) -> dict:
    """人工导入：接受 Gamma 原始响应或已构造的快照。"""
    if isinstance(data, list):
        return build_snapshot(data)
    if not isinstance(data, dict):
        raise ValueError("Polymarket 导入数据必须是 JSON 对象或数组")
    parsed = data.get("markets") or []
    already_built = (
        data.get("text") and parsed
        and all(isinstance(item, dict) and "yes_probability" in item for item in parsed)
    )
    if already_built:
        snap = dict(data)  # 已构造快照，直接沿用
    else:
        snap = build_snapshot(extract_markets(data))
    snap.setdefault("source", "manual")
    snap.setdefault("as_of", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    snap.setdefault("disclaimer", DISCLAIMER)
    return snap


def collect(db) -> dict:
    """采集 Polymarket 利率预测并写入数据库；失败不影响日报。"""
    markets = fetch_rate_markets()
    if not markets:
        return {"ok": False, "source": SOURCE_NAME, "error": "Polymarket 公开接口不可用"}
    try:
        snap = build_snapshot(markets)
    except ValueError as exc:
        return {"ok": False, "source": SOURCE_NAME, "error": str(exc)}
    db.upsert_polymarket_snapshot(
        source=snap["source"], as_of_date=snap["as_of"][:10],
        question=snap["text"],
        yes_probability=(snap["markets"][0]["yes_probability"]
                         if snap["markets"] else None),
        volume=(snap["markets"][0]["volume"] if snap["markets"] else None),
        payload=snap,
        raw_json=json.dumps(markets[:10], ensure_ascii=False),
    )
    return {
        "ok": True, "source": SOURCE_NAME,
        "markets": len(snap["markets"]),
        "latest": {"date": snap["as_of"][:10], "text": snap["text"]},
    }
