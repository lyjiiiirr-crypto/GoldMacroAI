"""CFTC 黄金期货持仓报告（COT）自动采集，官方免费 ZIP 数据。"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone

from config import CFTC_HISTORY_URL
from data.common import FetchError, fetch_bytes

CFTC_ZIP_URL = CFTC_HISTORY_URL.rstrip("/") + "/fut_disagg_txt_{year}.zip"
GOLD_MARKET = "GOLD - COMMODITY EXCHANGE INC."
SOURCE_NAME = "CFTC COT (官方)"


def parse_cot_zip(raw: bytes, limit: int = 60) -> list[dict]:
    """从 CFTC 年度 ZIP 中解析 COMEX 黄金 Managed Money 净多头。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [n for n in archive.namelist()
                   if n.lower().endswith((".txt", ".csv"))]
        if not members:
            return []
        text = archive.read(members[0]).decode("utf-8-sig", errors="replace")

    rows = []
    for item in csv.DictReader(io.StringIO(text)):
        if (item.get("Market_and_Exchange_Names") or "").strip() != GOLD_MARKET:
            continue
        if (item.get("FutOnly_or_Combined") or "").strip() not in ("", "FutOnly"):
            continue
        day = str(item.get("Report_Date_as_YYYY-MM-DD") or "")[:10]
        if not day:
            continue
        try:
            long_pos = float(item["M_Money_Positions_Long_All"])
            short_pos = float(item["M_Money_Positions_Short_All"])
            oi = float(item.get("Open_Interest_All") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "date": day,
            "value": round(long_pos - short_pos, 2),
            "long": round(long_pos, 2),
            "short": round(short_pos, 2),
            "oi": round(oi, 2),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


def collect(db, limit: int = 60) -> dict:
    year = datetime.now(timezone.utc).year
    errors = []
    raw = None
    for y in (year, year - 1):
        try:
            raw = fetch_bytes(CFTC_ZIP_URL.format(year=y), timeout=90)
            break
        except FetchError as exc:
            errors.append(str(exc))
    if raw is None:
        raise RuntimeError("CFTC ZIP 下载失败: " + " / ".join(errors[:2]))

    rows = parse_cot_zip(raw, limit=limit)
    if not rows:
        raise RuntimeError("CFTC ZIP 中未找到 COMEX 黄金 Managed Money 持仓")
    for row in rows:
        db.upsert_flow("COT", row["date"], row["value"], scale=30000,
                       source=SOURCE_NAME,
                       raw_json=json.dumps({k: row[k] for k in ("long", "short", "oi")}))
    return {
        "ok": True, "source": SOURCE_NAME,
        "weeks": len(rows), "latest": rows[0],
    }
