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


def render_crt_lines(crt: list[dict], *, limit: int = 2) -> list[str]:
    """渲染 4H CRT 高低点摘要（/crt 命令用，日报在执行提醒中引用）。"""
    lines = ["<b>4H CRT 高低点（UTC 对齐，近似）</b>"]
    if not crt:
        lines.append("  - 暂无 CRT 数据（运行 --sync-only 或 --import-crt 导入）")
        return lines
    for row in crt[:limit]:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        symbol = str(row.get("symbol") or payload.get("symbol") or "")
        label = payload.get("label") or row.get("symbol") or symbol
        as_of = str(row.get("as_of") or payload.get("as_of") or "")[:16]
        head = f"- {ESC(str(label))}（{ESC(as_of)}）：{ESC(str(row.get('scenario') or '待确认'))}"
        lines.append(head)
        lines.append(
            f"  当前 4H 高 {float(row.get('current_high') or 0):.2f} / "
            f"低 {float(row.get('current_low') or 0):.2f}；"
            f"前根 高 {float(row.get('prev_high') or 0):.2f} / "
            f"低 {float(row.get('prev_low') or 0):.2f}"
        )
        if row.get("summary"):
            lines.append("  " + ESC(str(row["summary"])))
    lines.append("  <i>来源：Yahoo Finance 公开 1H 行情本地聚合 4H，未回测，非交易信号。</i>")
    return lines


def render_execution_lines(market: dict, fusion: dict | None = None,
                           macro_bias: str | None = None) -> list[str]:
    """补全交易执行提醒：4H CRT 定结构与关键位，2M/5M 才做执行。"""
    lines = ["<b>交易执行提醒（4H CRT → LTF 执行）</b>"]

    def num(value, fallback: str = "—") -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return fallback

    crt = market.get("crt") or []
    if crt:
        row = crt[0]
        data = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        def pick(key, default=None):
            value = row.get(key)
            return value if value is not None else data.get(key, default)

        as_of = str(pick("as_of") or "")
        clock = as_of[11:16] if len(as_of) >= 16 else as_of
        scenario = str(pick("scenario") or "待确认")
        bias = str(pick("bias") or "中性")
        lines.append(
            f"- 4H 结构（UTC {ESC(clock)}）：高 {num(pick('current_high'))} / "
            f"低 {num(pick('current_low'))}，情景 {ESC(scenario)}（{ESC(bias)}）"
        )
        swept = ("本根已扫前低（卖出流动性）" if pick("swept_prev_low")
                 else "本根已扫前高（买入流动性）" if pick("swept_prev_high")
                 else "本根未扫前根高低点")
        lines.append(
            f"- 流动性位：前根高 {num(pick('prev_high'))} / 低 {num(pick('prev_low'))}；{ESC(swept)}"
        )
        equity = pick("equilibrium")
        if equity is not None:
            lines.append(f"- CRT 均衡位：{num(equity)}（中轴，回测不破才确认结构）")
        target, stop = pick("target"), pick("stop_ref")
        if target is not None and stop is not None:
            side = "高" if bias == "偏多" else "低"
            opposite = "低" if bias == "偏多" else "高"
            lines.append(
                f"- 结构参考：目标前根{side} {num(target)}，止损参考前根{opposite} {num(stop)}"
                "（仅为结构位，非挂单建议）"
            )
    else:
        lines.append("- 4H CRT：暂无数据（运行 --sync-only 或 --import-crt 导入）")
        lines.append("- 4H 结构：等待至少 2 根已收盘 4H K 线后再判断延续 / 反转")

    if fusion and fusion.get("direction"):
        try:
            score_text = f"{float(fusion.get('score') or 0):+.2f}"
        except (TypeError, ValueError):
            score_text = "—"
        lines.append(
            f"- 融合方向：{ESC(str(fusion['direction']))}（{score_text}）"
            " → 与 4H 情景冲突时降级观望，先等结构确认"
        )
    lines += [
        "- LTF：切 2M/5M，等 inducement 清扫 + 未缓解 order flow 回测，不追价",
        f"- 宏观{macro_bias or '方向'} ≠ 入场信号；止损先行，单笔风险可控",
    ]
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
    fusion = None
    if technical or gex:
        fusion = fuse(market.get("technical") or [], market.get("gex") or [])
        lines += [""] + render_fusion_lines(fusion)

    calendar = market.get("calendar") or []
    if calendar:
        lines += ["", "<b>未来24小时事件</b>"]
        lines += [f"- {ESC(e['time'])} {ESC(e['name'])}" for e in calendar[:5]]

    source = {"ollama": "本地Ollama", "cloud": "云端备用模型", "rule": "规则模板"}.get(analysis.get("source", ""), "规则模板")
    lines += [
        "", f"<b>AI 综合判断（{source}）</b>",
        ESC(analysis.get("text", "")),
        "",
    ]
    lines += render_execution_lines(market, fusion, bias_label(score))
    lines += ["", f"⚠️ {ESC(DISCLAIMER)}"]
    if score.warnings:
        lines.append("⚠️ 数据提示：" + ESC("；".join(score.warnings)))
    return "\n".join(lines)


def render_alert(kind: str, title: str, body: str) -> str:
    return f"<b>⚡ {ESC(title)}</b>\n{ESC(body)}\n\n⚠️ {ESC(DISCLAIMER)}"
