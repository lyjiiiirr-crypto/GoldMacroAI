"""美联储 FOMC 官方文件索引自动采集（Statement / Minutes / 发布会 / SEP / 点阵图）。

从美联储官网 FOMC 日历页解析公开文件链接，只存索引（标题、日期、链接），
不抓取或转载文件正文。点阵图包含在 SEP（经济预测摘要）页面中。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from config import FOMC_CALENDAR_URL
from data.common import fetch_text

CALENDAR_URL = FOMC_CALENDAR_URL
FED_BASE = "https://www.federalreserve.gov"
SOURCE_NAME = "Federal Reserve 官方日历"
DOC_PATTERNS = (
    ("Statement", r"/newsevents/pressreleases/monetary(\d{8})a\.htm"),
    ("Minutes", r"/monetarypolicy/fomcminutes(\d{8})\.htm"),
    ("Press Conference", r"/monetarypolicy/fomcpresconf(\d{8})\.htm"),
    ("SEP/Dot Plot", r"/monetarypolicy/fomcprojtabl(\d{8})\.htm"),
)


def parse_calendar_html(text: str, limit: int = 24) -> list[dict]:
    """解析日历页，返回去重后的 FOMC 文件索引（按日期倒序）。"""
    found: dict[tuple[str, str], dict] = {}
    for doc_type, pattern in DOC_PATTERNS:
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            raw_date = match.group(1)
            try:
                day = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
            except ValueError:
                continue
            path = match.group(0)
            found[(doc_type, day)] = {
                "doc_type": doc_type,
                "date": day,
                "title": f"FOMC {doc_type} {day}",
                "url": FED_BASE + path,
            }
    docs = sorted(found.values(), key=lambda d: (d["date"], d["doc_type"]),
                  reverse=True)
    return docs[:limit]


def collect(db, limit: int = 24) -> dict:
    text = fetch_text(CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    docs = parse_calendar_html(text, limit=limit)
    if not docs:
        raise RuntimeError("FOMC 日历页未解析到文件链接")
    saved = db.upsert_fomc_documents(docs)
    latest = docs[0]
    return {
        "ok": True, "source": SOURCE_NAME,
        "documents": len(docs), "saved": saved,
        "latest": {"date": latest["date"], "type": latest["doc_type"]},
    }
