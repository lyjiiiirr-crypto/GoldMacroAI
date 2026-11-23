"""Telegram 消息模板（HTML parse_mode）。"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from analysis.gold_bias import DISCLAIMER, bias_label, score_bar
from config import CHINA_TIMEZONE_OFFSET_HOURS, SERIES

ESC = html.escape


def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS)))


def render_daily_report(score, analysis: dict, market: dict | None = None) -> str:
    market = market or {}
    now = china_now()
    lines = [
        "<b>📊 GOLD MACRO BOT · XAUUSD 宏观日报</b>",
        f"<i>{now:%Y-%m-%d} 北京时间 {now:%H:%M}</i>",
        "",
        f"宏观方向：<b>{ESC(bias_label(score))}</b>",
        f"<b>{score.total:+.1f} / 100</b>  置信度 {score.confidence:.0%}",
        f"<code>{score_bar(score.total)}</code>",
        "",
        "<b>关键市场数据</b>",
    ]
    for series_id, label in (
        ("DFII10", "10Y实际收益率"), ("DGS2", "2Y国债收益率"),
        ("DGS10", "10Y国债收益率"), ("DTWEXBGS", "美元广义指数"),
        ("T10YIE", "10Y通胀预期"), ("DFF", "有效联邦基金利率"),
    ):
        item = market.get(series_id)
        if not item or item.get("value") is None:
            continue
        unit = SERIES[series_id]["unit"]
        delta = item.get("delta")
        delta_text = f"（{item['change_text']}）" if item.get("change_text") else ""
        stale = " ⚠️数据过期" if item.get("stale") else ""
        lines.append(f"- {label}：{item['value']:.2f}{unit}{delta_text}{stale}")
    if not any(market.get(k) for k in SERIES):
        lines.append("- 暂无 FRED 数据（配置 FRED_API_KEY 后自动采集）")

    fed = market.get("fedwatch")
    lines += ["", "<b>美联储政策预期</b>", f"- {ESC(fed.get('text', '无 FedWatch 快照')) if fed else '无 FedWatch 快照（可用 --import-fedwatch 录入）'}"]

    events = market.get("events") or []
    if events:
        lines += ["", "<b>经济数据意外</b>"]
        lines += [f"- {ESC(e['name'])}：实际 {e['actual']}，预期 {e['expected']}" for e in events[:3]]

    flows = market.get("flows") or []
    if flows:
        lines += ["", "<b>资金流向</b>"]
        lines += [f"- {ESC(f['name'])}：{f['text']}" for f in flows[:3]]

    calendar = market.get("calendar") or []
    if calendar:
        lines += ["", "<b>未来24小时事件</b>"]
        lines += [f"- {ESC(e['time'])} {ESC(e['name'])}" for e in calendar[:5]]

    source = {"ollama": "本地Ollama", "cloud": "云端备用模型", "rule": "规则模板"}.get(analysis.get("source", ""), "规则模板")
    lines += [
        "", f"<b>AI 综合判断（{source}）</b>",
        ESC(analysis.get("text", "")),
        "", "<b>交易执行提醒</b>",
        "- HTF：等待日线/周线结构确认",
        "- LTF：等待流动性清扫与结构转变",
        "- 不追价，不因宏观偏多直接做多",
        "", f"⚠️ {ESC(DISCLAIMER)}",
    ]
    if score.warnings:
        lines.append("⚠️ 数据提示：" + ESC("；".join(score.warnings)))
    return "\n".join(lines)


def render_alert(kind: str, title: str, body: str) -> str:
    return f"<b>⚡ {ESC(title)}</b>\n{ESC(body)}\n\n⚠️ {ESC(DISCLAIMER)}"
