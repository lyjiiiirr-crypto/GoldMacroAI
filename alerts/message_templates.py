"""Telegram 消息模板（HTML parse_mode）。"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from analysis.gold_bias import DISCLAIMER, bias_label, score_bar
from analysis.tech_gex_fusion import fuse
from config import CHINA_TIMEZONE_OFFSET_HOURS, SERIES

ESC = html.escape


def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS)))


GEX_SYMBOL_LABELS = {
    "GLD": "GLD 黄金ETF代理",
    "SPY": "SPY 大盘风险偏好",
}


def render_gex_lines(gex: list[dict], *, limit: int = 2) -> list[str]:
    """渲染 GEX / Gamma flip 摘要，日报与 /gex 命令共用。"""
    lines = ["<b>GEX / Gamma 翻转点（延迟近似）</b>"]
    if not gex:
        lines.append("  - 暂无 GEX 数据（运行 --sync-only 或 --import-gex 导入）")
        return lines
    for row in gex[:limit]:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        symbol = str(row.get("symbol") or payload.get("symbol") or "")
        label = payload.get("label") or GEX_SYMBOL_LABELS.get(symbol, symbol)
        as_of = row.get("as_of_date") or payload.get("as_of") or ""
        regime = row.get("regime") or payload.get("regime") or ""
        head = f"- {ESC(str(label))}（{ESC(str(as_of))}）：{ESC(str(regime))}"
        lines.append(head)
        summary = row.get("summary") or payload.get("summary") or ""
        if summary:
            lines.append("  " + ESC(str(summary)))
        spot = row.get("spot") if row.get("spot") is not None else payload.get("spot")
        flip = row.get("gamma_flip") if row.get("gamma_flip") is not None else payload.get("gamma_flip")
        call_wall = row.get("call_wall") if row.get("call_wall") is not None else payload.get("call_wall")
        put_wall = row.get("put_wall") if row.get("put_wall") is not None else payload.get("put_wall")
        bits = []
        if spot is not None:
            bits.append(f"现价 {float(spot):.2f}")
        if flip is not None:
            bits.append(f"flip {float(flip):.2f}")
        if call_wall is not None:
            bits.append(f"Call墙 {float(call_wall):.2f}")
        if put_wall is not None:
            bits.append(f"Put墙 {float(put_wall):.2f}")
        if bits:
            lines.append("  " + ESC("；".join(bits)))
    lines.append("  <i>来源：Cboe 免费延迟期权报价 + 本地 Black-Scholes 估算，非做市商真实库存，非交易信号。</i>")
    return lines


def render_fusion_lines(fusion: dict) -> list[str]:
    """渲染「技术面 × GEX 融合方向」结论（日报 /report 共用）。"""
    direction = str(fusion.get("direction") or "中性震荡")
    icon = {"偏多": "🟢", "偏空": "🔴", "中性震荡": "⚪"}.get(direction, "⚪")
    score = fusion.get("score")
    strength = fusion.get("strength") or ""
    lines = ["<b>技术面 × GEX 融合方向（延迟近似，未回测）</b>"]
    conclusion = f"- 结论：<b>{icon} {ESC(direction)}</b>"
    if score is not None:
        conclusion += f"（融合分 {float(score):+.2f}，强度 {ESC(str(strength))}）"
    lines.append(conclusion)
    if fusion.get("reason"):
        lines.append("  " + ESC(str(fusion["reason"])))
    if fusion.get("disclaimer"):
        lines.append(f"  <i>{ESC(str(fusion['disclaimer']))}</i>")
    return lines


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
        ("DGS10", "10Y国债收益率"), ("DXY", "美元指数 DXY"),
        ("DTWEXBGS", "美元广义指数"), ("T10YIE", "10Y通胀预期"),
        ("DFF", "有效联邦基金利率"),
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
    lines += ["", "<b>美联储政策预期</b>", f"- {ESC(fed.get('text', '无 FedWatch 快照')) if fed else '无 FedWatch 快照（自动采集失败时可用 --import-fedwatch 录入）'}"]

    fomc = market.get("fomc") or {}
    if fomc:
        lines += ["", "<b>FOMC 官方文件</b>"]
        if fomc.get("next_meeting"):
            lines.append(f"- 下次会议：{ESC(str(fomc['next_meeting']))}（FedWatch 隐含）")
        for doc in (fomc.get("documents") or [])[:3]:
            lines.append(
                f'- <a href="{ESC(doc["url"])}">{ESC(doc["doc_type"])} '
                f'{ESC(doc["doc_date"])}</a>'
            )

    macro = market.get("macro") or []
    if macro:
        lines += ["", "<b>美国宏观数据</b>"]
        for item in macro[:6]:
            stale = " ⚠️过期" if item.get("stale") else ""
            lines.append(
                f"- {ESC(item['name'])}：{item['value']:,.2f}{ESC(item['unit'])}"
                f"（{ESC(item['change_text'])}，{ESC(item['date'])}）{stale}"
            )

    events = market.get("events") or []
    if events:
        lines += ["", "<b>经济数据意外</b>"]
        lines += [f"- {ESC(e['name'])}：实际 {e['actual']}，预期 {e['expected']}" for e in events[:3]]

    flows = market.get("flows") or []
    if flows:
        lines += ["", "<b>资金流向</b>"]
        lines += [f"- {ESC(f['name'])}：{f['text']}" for f in flows[:3]]

    technical = market.get("technical") or []
    if technical:
        lines += ["", "<b>技术面（本地自算）</b>"]
        for report in technical[:2]:
            label = report.get("label") or report.get("symbol") or "技术面"
            as_of = report.get("as_of") or report.get("report_date") or ""
            head = (f"- {ESC(str(label))}（{ESC(str(as_of))}）："
                    f"{ESC(str(report.get('bias') or '中性震荡'))}")
            close = report.get("close")
            if close is not None:
                head += f"，收盘 {float(close):.2f}"
            lines.append(head)
            if report.get("summary"):
                lines.append("  " + ESC(str(report["summary"])))
            payload = report.get("payload")
            indicators = (payload.get("indicators") if isinstance(payload, dict) else {}) or {}
            levels = (payload.get("levels") if isinstance(payload, dict) else {}) or {}
            bits = []
            if indicators.get("rsi14") is not None:
                bits.append(f"RSI {float(indicators['rsi14']):.1f}")
            if indicators.get("histogram") is not None:
                bits.append(f"MACD柱 {float(indicators['histogram']):+.2f}")
            for key, name in (("support", "支撑"), ("resistance", "阻力")):
                values = levels.get(key) or []
                if values:
                    bits.append(f"{name} " + "/".join(f"{float(v):.2f}" for v in values))
            if bits:
                lines.append("  " + ESC("；".join(bits)))
        lines.append("  <i>来源：Yahoo Finance 公开行情 + 本地规则计算，非 Trading Central 官方研报。</i>")

    gex = market.get("gex") or []
    if gex:
        lines += [""] + render_gex_lines(gex, limit=2)

    # 技术面 × GEX 融合方向：只作报告上下文，不参与宏观评分
    if technical or gex:
        lines += [""] + render_fusion_lines(
            fuse(market.get("technical") or [], market.get("gex") or [])
        )

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
